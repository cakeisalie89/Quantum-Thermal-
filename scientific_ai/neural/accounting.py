"""The exact parameter count of a configuration -- and what "active" means.

Closed-form arithmetic over :class:`~.config.ModelConfig`, written apart from
the network code ON PURPOSE: ``model.network`` builds tensors from its own
layer code, and ``model.meta`` counts the shapes those tensors would have
(``jax.eval_shape``, nothing allocated). The two are compared category by
category, so a formula that drifts from the code, or code that drifts from
the formula, is a failing test rather than a number nobody re-derived.

EVERY TRAINABLE TENSOR, ONCE

=====================  =====================================================
category               tensors (d = hidden_size)
=====================  =====================================================
feature_embedding      the feature-identity table [feature_vocab_size, d]
readout_embedding      the learned readout tokens [num_readout_tokens, d]
context_embedding      context table [context_vocab_size, d] and validity
                       table [len(VALIDITY), d]
dimensional_encoder    SI-exponent projection [7, d] and the dimension-class
                       table [len(DIMENSION_CLASSES), d]
numerical_encoder      value MLP: [4, h_v] + [h_v] + [h_v, d] + [d]
attention              per layer q [d, H], k [d, KV], v [d, KV], o [H, d];
                       H = heads * head_dim, KV = kv_heads * head_dim; no
                       biases
normalization          RMSNorm scales: 2 per layer + 1 final, [d] each
dense_ffn              per dense layer SwiGLU gate, up [d, f_d], down [f_d, d]
router                 per MoE layer [d, num_experts]; no bias
expert                 per MoE layer num_experts SwiGLU experts, each
                       3 * d * expert_hidden
shared_expert          per MoE layer num_shared SwiGLU experts, each
                       3 * d * shared_hidden, applied to every token
output_head            per head: mean projection [d] + bias
uncertainty_head       per head: variance projection [d] + bias
reconstruction_head    ``untied``: decoder [feature_vocab_size, d] + bias;
                       ``tied``: the bias only -- its decoder IS the
                       feature-identity table, counted once, under
                       feature_embedding, and reported as ``tied_parameters``
=====================  =====================================================

NOT counted as trainable: the normalisation statistics (a mean and a scale
per feature identity and per target) are fixed by the TRAINING split and
are ``non_trainable_parameters``; optimizer state, gradients, activations
and communication buffers are memory (``estimates``), not parameters.

ACTIVE PARAMETERS PER TOKEN -- THE DEFINITION

The trainable parameters whose values enter the inference forward pass of
ONE feature token through the backbone, plus the heads that turn the
backbone's output into a prediction, where

* an embedding table contributes only the rows that token reads (one
  identity row, one context row, one validity row, one class row);
* a dense projection contributes all of its weights (attention, norms, the
  router -- every token is scored against every expert --, dense FFNs and
  shared experts);
* a routed expert contributes ALL its weights if the token is routed to it
  and NONE otherwise: ``top_k`` experts in each MoE layer;
* the readout-token table and the reconstruction head (a training-only
  auxiliary objective) contribute nothing;
* no choice is dropped by a capacity limit. A dropped choice computes less
  than this; ``model.network`` reports every drop.

So ``active = shared_active + expert_active`` with
``expert_active = moe_layers * top_k * parameters_per_expert``. This is NOT
the number of parameters a BATCH touches: across many tokens the union of
selected experts grows toward every expert, which
:func:`batch_touched_upper_bound` bounds and the router statistics measure.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import tokens
from .config import ModelConfig
from .units import DIMENSION_CLASSES

CATEGORIES = ("feature_embedding", "readout_embedding", "context_embedding",
              "dimensional_encoder", "numerical_encoder", "attention",
              "normalization", "dense_ffn", "router", "expert",
              "shared_expert", "output_head", "uncertainty_head",
              "reconstruction_head")
#: The input-representation categories, reported together as
#: ``embedding_parameters``.
EMBEDDING_CATEGORIES = ("feature_embedding", "readout_embedding",
                        "context_embedding", "dimensional_encoder",
                        "numerical_encoder")

N_VALUE = len(tokens.VALUE_CHANNELS)
N_VALIDITY = len(tokens.VALIDITY)
N_DIM_CLASSES = len(DIMENSION_CLASSES)


def _swiglu(d: int, f: int) -> int:
    return 3 * d * f


def parameters_per_expert(cfg: ModelConfig) -> int:
    if cfg.moe is None:
        return 0
    return _swiglu(cfg.hidden_size, cfg.moe.expert_hidden)


@dataclass(frozen=True)
class ParameterCount:
    """Exact counts for one configuration. Every field is an int."""

    config_digest: str
    by_category: dict
    trainable_parameters: int
    non_trainable_parameters: int
    tied_parameters: int
    moe_layer_count: int
    num_experts_per_moe_layer: int
    experts_selected_per_token: int
    expert_parameters_per_expert: int
    active_by_category: dict
    shared_active_parameters: int
    expert_active_parameters: int
    routing_profiles: dict

    @property
    def total_parameters(self) -> int:
        """Trainable + non-trainable: every number a checkpoint stores for
        the model (optimizer state excluded)."""
        return self.trainable_parameters + self.non_trainable_parameters

    @property
    def expert_parameters(self) -> int:
        return self.by_category["expert"]

    @property
    def shared_parameters(self) -> int:
        """Every trainable parameter that is not in a routed expert."""
        return self.trainable_parameters - self.by_category["expert"]

    @property
    def embedding_parameters(self) -> int:
        return sum(self.by_category[c] for c in EMBEDDING_CATEGORIES)

    @property
    def active_parameters_per_token(self) -> int:
        return self.shared_active_parameters + self.expert_active_parameters

    def to_dict(self) -> dict:
        c = self.by_category
        return {
            "config_digest": self.config_digest,
            "total_parameters": self.total_parameters,
            "trainable_parameters": self.trainable_parameters,
            "non_trainable_parameters": self.non_trainable_parameters,
            "tied_parameters": self.tied_parameters,
            "shared_parameters": self.shared_parameters,
            "attention_parameters": c["attention"],
            "expert_parameters": c["expert"],
            "router_parameters": c["router"],
            "embedding_parameters": self.embedding_parameters,
            "output_head_parameters": c["output_head"],
            "uncertainty_head_parameters": c["uncertainty_head"],
            "by_category": dict(c),
            "num_experts_per_moe_layer": self.num_experts_per_moe_layer,
            "experts_selected_per_token": self.experts_selected_per_token,
            "moe_layer_count": self.moe_layer_count,
            "expert_parameters_per_expert":
                self.expert_parameters_per_expert,
            "shared_active_parameters": self.shared_active_parameters,
            "expert_active_parameters": self.expert_active_parameters,
            "estimated_total_active_parameters_per_token":
                self.active_parameters_per_token,
            "active_by_category": dict(self.active_by_category),
            "routing_profiles": dict(self.routing_profiles),
        }


def count(cfg: ModelConfig) -> ParameterCount:
    cfg.validate()
    d = cfg.hidden_size
    a = cfg.attention
    inner = a.num_heads * a.head_dim
    kv = a.num_kv_heads * a.head_dim
    n_layers = cfg.num_layers
    moe_layers = len(cfg.moe_layer_indices)
    dense_layers = len(cfg.dense_layer_indices)
    n_heads = len(cfg.heads)
    hv = cfg.value_encoder_hidden
    vf = cfg.feature_vocab_size

    per_attn = d * inner + 2 * d * kv + inner * d
    c = dict.fromkeys(CATEGORIES, 0)
    c["feature_embedding"] = vf * d
    c["readout_embedding"] = cfg.num_readout_tokens * d
    c["context_embedding"] = (cfg.context_vocab_size + N_VALIDITY) * d
    c["dimensional_encoder"] = (tokens.N_BASE_DIMS + N_DIM_CLASSES) * d
    c["numerical_encoder"] = N_VALUE * hv + hv + hv * d + d
    c["attention"] = n_layers * per_attn
    c["normalization"] = 2 * d * n_layers + d
    c["dense_ffn"] = dense_layers * _swiglu(d, cfg.dense_ffn_hidden)
    tied = 0
    if cfg.reconstruction_head == "untied":
        c["reconstruction_head"] = vf * d + 1
    elif cfg.reconstruction_head == "tied":
        c["reconstruction_head"] = 1
        tied = vf * d
    e = k = per_expert = 0
    profiles: dict = {}
    if cfg.moe is not None:
        m = cfg.moe
        e, k = m.num_experts, m.top_k
        per_expert = _swiglu(d, m.expert_hidden)
        c["router"] = moe_layers * d * e
        c["expert"] = moe_layers * e * per_expert
        c["shared_expert"] = moe_layers * m.num_shared_experts * _swiglu(
            d, m.shared_hidden)
    c["output_head"] = n_heads * (d + 1)
    c["uncertainty_head"] = n_heads * (d + 1)
    trainable = sum(c.values())
    non_trainable = 2 * vf + 2 * n_heads

    act = dict.fromkeys(CATEGORIES, 0)
    act["feature_embedding"] = d
    act["context_embedding"] = 2 * d
    act["dimensional_encoder"] = tokens.N_BASE_DIMS * d + d
    for cat in ("numerical_encoder", "attention", "normalization",
                "dense_ffn", "router", "shared_expert", "output_head",
                "uncertainty_head"):
        act[cat] = c[cat]
    expert_active = moe_layers * k * per_expert
    act["expert"] = expert_active
    shared_active = sum(v for cat, v in act.items() if cat != "expert")
    if cfg.moe is not None:
        for name, pk in (cfg.moe.routing_profiles or (("standard", k),)):
            profiles[name] = {
                "top_k": pk,
                "expert_active_parameters": moe_layers * pk * per_expert,
                "active_parameters_per_token":
                    shared_active + moe_layers * pk * per_expert}
    return ParameterCount(
        config_digest=cfg.digest(), by_category=c,
        trainable_parameters=trainable,
        non_trainable_parameters=non_trainable, tied_parameters=tied,
        moe_layer_count=moe_layers, num_experts_per_moe_layer=e,
        experts_selected_per_token=k,
        expert_parameters_per_expert=per_expert, active_by_category=act,
        shared_active_parameters=shared_active,
        expert_active_parameters=expert_active, routing_profiles=profiles)


def batch_touched_upper_bound(cfg: ModelConfig, n_tokens: int) -> int:
    """At most how many trainable parameters ``n_tokens`` tokens touch.

    Every shared parameter, embedding tables in full (a bound, not a row
    count), and per MoE layer at most ``min(num_experts, top_k * n_tokens)``
    experts. Measured batches report the experts actually used."""
    if isinstance(n_tokens, bool) or not isinstance(n_tokens, int) \
            or n_tokens < 1:
        raise ValueError(f"n_tokens must be a positive int: {n_tokens!r}")
    pc = count(cfg)
    if cfg.moe is None:
        return pc.trainable_parameters - pc.by_category[
            "reconstruction_head"]
    used = min(cfg.moe.num_experts, cfg.moe.top_k * n_tokens)
    return (pc.shared_parameters - pc.by_category["reconstruction_head"]
            + pc.moe_layer_count * used * pc.expert_parameters_per_expert)
