"""Memory, compute and communication ESTIMATES for a configuration.

Every number here is arithmetic over exact parameter counts and a stated
set of assumptions, returned WITH those assumptions. None is a measurement:
nothing here ran on the hardware it describes, and a profile, a benchmark
or a hardware tranche is what turns an estimate into a number to rely on.

LABELS THAT ARE NOT OPTIONAL

"1T parameters needs 2 TB" is true only as ``parameter_storage.bfloat16``:
parameter-only bfloat16 bytes. Training state, inference state and a
checkpoint are different quantities and are reported separately:

* ``parameter_storage``   the parameters alone, per dtype (FP8 with one
                          float32 scale per 128 x 128 block, stated);
* ``training_state``      mixed-precision Adam, per component: compute
                          copy, float32 master weights, gradients, both
                          moments -- and their sum per parameter;
* ``inference_state``     the compute copy and one batch's activations; a
                          set encoder has no key/value cache;
* ``activations``         per token per layer, with and without activation
                          checkpointing, from the dominant tensors;
* ``communication``       routed dispatch and combine bytes per token if
                          experts live on other devices; router buffers;
* ``checkpoint``          parameters, and parameters + optimizer state.

FLOPs count multiply-adds as 2 and only the matmuls and attention scores;
training is forward + 2 x forward for the backward pass, plus one more
forward when activation checkpointing recomputes it.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import accounting
from .config import ModelConfig

DTYPE_BYTES = {"float32": 4, "bfloat16": 2, "float8_e4m3": 1}
FP8_BLOCK = 128


@dataclass(frozen=True)
class Assumptions:
    """What every estimate below assumes. Change one, the numbers change."""

    tokens_per_sample: int = 16
    batch_tokens: int = 1_048_576
    activation_dtype: str = "bfloat16"
    gradient_dtype: str = "float32"
    master_dtype: str = "float32"
    optimizer: str = "adam"
    #: Each routed expert takes at most capacity_factor * tokens * k / E
    #: choices in a dispatch buffer (planning only; the architecture itself
    #: drops nothing).
    dispatch_capacity_factor: float = 1.25
    #: Fraction of a token's routed choices whose expert sits on another
    #: device: (EP - 1) / EP for expert-parallel degree EP.
    expert_parallel_degree: int = 8

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @property
    def remote_fraction(self) -> float:
        ep = self.expert_parallel_degree
        return (ep - 1) / ep


def _bytes(n: int, dtype: str) -> int:
    return n * DTYPE_BYTES[dtype]


def parameter_storage(pc: accounting.ParameterCount) -> dict:
    n = pc.trainable_parameters
    blocks = -(-n // (FP8_BLOCK * FP8_BLOCK))
    return {
        "label": "parameter-only storage of the trainable parameters",
        "float32_bytes": _bytes(n, "float32"),
        "bfloat16_bytes": _bytes(n, "bfloat16"),
        "float8_e4m3_bytes": _bytes(n, "float8_e4m3") + 4 * blocks,
        "float8_note": "FP8 e4m3 weights plus one float32 scale per "
                       f"{FP8_BLOCK}x{FP8_BLOCK} block; an ESTIMATE of "
                       "storage only -- FP8 is not validated for any "
                       "scientific workload here",
        "non_trainable_float32_bytes": _bytes(pc.non_trainable_parameters,
                                              "float32"),
    }


def training_state(pc: accounting.ParameterCount, cfg: ModelConfig,
                   a: Assumptions) -> dict:
    n = pc.trainable_parameters
    compute = _bytes(n, cfg.precision.param_dtype)
    master = _bytes(n, a.master_dtype) \
        if cfg.precision.param_dtype != a.master_dtype else 0
    grads = _bytes(n, a.gradient_dtype)
    moments = 2 * _bytes(n, "float32")
    total = compute + master + grads + moments
    return {
        "label": "mixed-precision Adam training state, excluding "
                 "activations and communication buffers",
        "compute_copy_bytes": compute, "master_weights_bytes": master,
        "gradient_bytes": grads, "optimizer_moments_bytes": moments,
        "total_bytes": total, "bytes_per_parameter": total / n,
    }


def activations(cfg: ModelConfig, a: Assumptions) -> dict:
    """Bytes per token per layer kept for the backward pass.

    Without checkpointing: the residual input, the attention q/k/v and
    output, the FFN input, and each selected expert's gate, up and product
    activations (3 x expert_hidden per choice). With checkpointing only the
    block input is kept and the rest is recomputed."""
    d = cfg.hidden_size
    att = cfg.attention
    inner = att.num_heads * att.head_dim
    kv = att.num_kv_heads * att.head_dim
    b = DTYPE_BYTES[a.activation_dtype]
    per_token_layer = {}
    for i in range(cfg.num_layers):
        if cfg.is_moe_layer(i):
            m = cfg.moe
            ffn = (m.top_k * 3 * m.expert_hidden
                   + m.num_shared_experts * 3 * m.shared_hidden)
        else:
            ffn = 3 * cfg.dense_ffn_hidden
        per_token_layer[i] = (2 * d + inner + 2 * kv + inner + d + ffn) * b
    full = sum(per_token_layer.values())
    ckpt = cfg.num_layers * d * b
    return {
        "label": "activation bytes per token kept for backward",
        "without_checkpointing_bytes_per_token": full,
        "with_checkpointing_bytes_per_token": ckpt,
        "batch_tokens": a.batch_tokens,
        "batch_without_checkpointing_bytes": full * a.batch_tokens,
        "batch_with_checkpointing_bytes": ckpt * a.batch_tokens,
        "category": _category(full * a.batch_tokens),
    }


def _category(n: float) -> str:
    for limit, name in ((2 ** 30, "under 1 GiB"), (2 ** 40, "under 1 TiB"),
                        (2 ** 50, "under 1 PiB")):
        if n < limit:
            return name
    return "1 PiB or more"


def flops(pc: accounting.ParameterCount, cfg: ModelConfig,
          a: Assumptions) -> dict:
    att = cfg.attention
    inner = att.num_heads * att.head_dim
    # QK^T and AV: 2 * 2 * S * inner per token per layer
    attn_scores = 4 * cfg.num_layers * a.tokens_per_sample * inner
    fwd = 2 * pc.active_parameters_per_token + attn_scores
    train = 3 * fwd
    return {
        "label": "matmul and attention FLOPs per token (multiply-add = 2)",
        "active_parameters_per_token": pc.active_parameters_per_token,
        "forward_flops_per_token": fwd,
        "training_flops_per_token": train,
        "training_flops_per_token_with_checkpointing": train + fwd,
        "batch_tokens": a.batch_tokens,
        "training_flops_per_batch": train * a.batch_tokens,
    }


def communication(pc: accounting.ParameterCount, cfg: ModelConfig,
                  a: Assumptions) -> dict:
    if cfg.moe is None:
        return {"label": "no routed experts; no dispatch"}
    d = cfg.hidden_size
    k = cfg.moe.top_k
    b = DTYPE_BYTES[a.activation_dtype]
    n_moe = pc.moe_layer_count
    dispatch = k * d * b
    per_token = 2 * dispatch * n_moe * a.remote_fraction
    fwd_flops = 2 * pc.active_parameters_per_token
    capacity = -(-int(a.dispatch_capacity_factor * a.batch_tokens * k)
                 // cfg.moe.num_experts)
    return {
        "label": "expert-parallel routing traffic, forward pass",
        "experts_selected_per_token": k,
        "dispatch_bytes_per_token_per_moe_layer": dispatch,
        "combine_bytes_per_token_per_moe_layer": dispatch,
        "remote_fraction": a.remote_fraction,
        "routed_bytes_per_token_forward": per_token,
        "routed_bytes_per_batch_forward": per_token * a.batch_tokens,
        "bytes_per_forward_flop": per_token / fwd_flops,
        "router_logit_buffer_bytes_per_moe_layer":
            a.batch_tokens * cfg.moe.num_experts * 4,
        "dispatch_buffer_bytes_per_moe_layer":
            cfg.moe.num_experts * capacity * d * b,
        "note": "the cost of high activation: each token's representation "
                "travels to top_k experts and back in every MoE layer",
    }


def checkpoint(pc: accounting.ParameterCount, cfg: ModelConfig,
               a: Assumptions) -> dict:
    n = pc.trainable_parameters
    params = _bytes(n, a.master_dtype) + _bytes(
        pc.non_trainable_parameters, "float32")
    return {
        "label": "checkpoint bytes",
        "parameters_only_bytes": params,
        "with_optimizer_state_bytes": params + 2 * _bytes(n, "float32"),
    }


def inference_state(pc: accounting.ParameterCount, cfg: ModelConfig,
                    a: Assumptions) -> dict:
    d = cfg.hidden_size
    b = DTYPE_BYTES[a.activation_dtype]
    return {
        "label": "inference: the compute copy plus a few live activations "
                 "per token; no key/value cache (set encoder)",
        "parameter_bytes": _bytes(pc.trainable_parameters,
                                  cfg.precision.param_dtype),
        "activation_bytes_per_token": 4 * d * b,
    }


def estimate(cfg: ModelConfig, assumptions: Assumptions | None = None) \
        -> dict:
    a = assumptions or Assumptions()
    pc = accounting.count(cfg)
    return {
        "status": "ESTIMATE -- arithmetic over exact parameter counts and "
                  "the assumptions below; not a benchmark, not a "
                  "measurement on any hardware",
        "config_digest": pc.config_digest,
        "assumptions": a.to_dict(),
        "parameter_storage": parameter_storage(pc),
        "training_state": training_state(pc, cfg, a),
        "inference_state": inference_state(pc, cfg, a),
        "activations": activations(cfg, a),
        "flops": flops(pc, cfg, a),
        "communication": communication(pc, cfg, a),
        "checkpoint": checkpoint(pc, cfg, a),
    }
