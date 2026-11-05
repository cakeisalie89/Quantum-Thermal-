"""The family's members: the development model, the scale ladder, the flagship.

Every member is the SAME architecture (``config``), so a feature schema, a
tokenizer, a head registry, a dataset manifest, a training manifest and a
checkpoint manifest mean the same thing at every scale. A member differs in
its structure (width, depth, attention heads, experts per layer), which is
declared here with the reason for it, and in two sizes the budget solver
resolves (expert width and top_k). Nothing above the development model is
trained by this tranche.

THE FLAGSHIP, AND WHY THIS STRUCTURE

* ``hidden_size`` 8192, 64 layers. At 1T the attention and normalisation
  that every token pays for are about 1.7% of the parameters, so what a
  token activates is decided by the expert FFNs -- which is what the
  activation target is about.
* Standard multi-head attention, 64 heads of 128. The inputs are SETS of
  feature tokens encoded in one non-autoregressive pass; there is no
  key/value cache to shrink, which is the benefit grouped-query attention
  buys. GQA stays available (``num_kv_heads``) and tested.
* Every layer a mixture-of-experts layer, 64 routed SwiGLU experts, no
  shared expert. Uniform layers keep one code path at every depth; with the
  default ``top_k`` near 12 every token already reaches a substantial,
  routed share of the model, so an always-on shared expert would buy
  little of what it is usually added for. 64 experts shard evenly onto
  8-device nodes and every power of two up to 64 devices -- an
  execution-planning convenience, recorded as such, not a scientific one.
* ``feature_vocab_size`` 4096 identities (33.5M parameters, 0.003%): room
  for the feature schemas of many datasets in one model.
* Four readout tokens, mean-pooled into the heads.
* The heads are the only REAL targets this repository has learned
  (``surface_adsorption`` outputs). Heads attach per task; their size is
  ``2 * (d + 1)`` each and is counted exactly wherever they sit.
* Parameters and compute in bfloat16 for the plan; routing in float32.

Targets: 1,000,000,000,000 trainable parameters (range 950B..1.05T) and
200,000,000,000 active per token (range 150B..250B), solved at an
alignment of 256 for the expert width. Routing profiles ``economical`` /
``standard`` / ``deep`` are ``top_k - 2`` / ``top_k`` / ``top_k + 2`` and
each reports its own exact active count.
"""
from __future__ import annotations

from .config import (AttentionConfig, HeadSpec, ModelConfig, MoEConfig,
                     PrecisionProfile)
from .solver import BudgetTarget, solve

#: The real targets of the development dataset, in head form. Fluxes,
#: inventories and fluences span decades and are learned as log10; the
#: coverage is a fraction and is learned bounded to [0, 1].
SURFACE_ADSORPTION_HEADS = (
    HeadSpec("admitted_fluence"),
    HeadSpec("final_coverage", "bounded_gaussian", 0.0, 1.0),
    HeadSpec("final_inventory"),
    HeadSpec("impingement_flux"),
)

BF16 = PrecisionProfile(param_dtype="bfloat16", compute_dtype="bfloat16")


def development() -> ModelConfig:
    """The member that is TRAINED in this tranche: small enough for a CPU
    and a CI runner, and the same architecture as the flagship (feature
    tokens, attention, top-k routed SwiGLU experts in every layer)."""
    return ModelConfig(
        variant="development", hidden_size=64, num_layers=2,
        attention=AttentionConfig(num_heads=4, head_dim=16, num_kv_heads=4),
        moe=MoEConfig(num_experts=8, top_k=2, expert_hidden=128,
                      aux_loss_coef=0.01),
        feature_vocab_size=8, value_encoder_hidden=32,
        num_readout_tokens=1, heads=SURFACE_ADSORPTION_HEADS).validate()


def _template(variant, d, layers, heads, experts, vocab, hv, readout=1,
              precision=BF16) -> ModelConfig:
    return ModelConfig(
        variant=variant, hidden_size=d, num_layers=layers,
        attention=AttentionConfig(num_heads=heads, head_dim=d // heads,
                                  num_kv_heads=heads),
        moe=MoEConfig(num_experts=experts, top_k=1, expert_hidden=64),
        feature_vocab_size=vocab, value_encoder_hidden=hv,
        num_readout_tokens=readout, heads=SURFACE_ADSORPTION_HEADS,
        precision=precision)


def flagship_template() -> ModelConfig:
    return ModelConfig(
        variant="flagship-1t", hidden_size=8192, num_layers=64,
        attention=AttentionConfig(num_heads=64, head_dim=128,
                                  num_kv_heads=64),
        moe=MoEConfig(num_experts=64, top_k=12, expert_hidden=256,
                      routing_profiles=(("economical", 10), ("standard", 12),
                                        ("deep", 14))),
        feature_vocab_size=4096, value_encoder_hidden=1024,
        num_readout_tokens=4, heads=SURFACE_ADSORPTION_HEADS,
        precision=BF16)


FLAGSHIP_TARGET = BudgetTarget(
    total=1_000_000_000_000, active=200_000_000_000,
    total_range=(950_000_000_000, 1_050_000_000_000),
    active_range=(150_000_000_000, 250_000_000_000))
FLAGSHIP_ALIGNMENT = 256


def solve_flagship() -> dict:
    return solve(flagship_template(), FLAGSHIP_TARGET,
                 alignment=FLAGSHIP_ALIGNMENT)


def _ladder_target(total: int) -> BudgetTarget:
    """Total within +-10%; active 20% of the total, accepted in 15..25%."""
    return BudgetTarget(total=total, active=total // 5,
                        total_range=(total - total // 10,
                                     total + total // 10),
                        active_range=(total * 15 // 100, total // 4))


#: name -> (template, target, alignment). The ladder a controlled scaling
#: study would climb; each rung is solved and meta-validated, none trained.
def ladder() -> list:
    rungs = [
        ("50m", _template("ladder-50m", 512, 8, 8, 16, 1024, 128),
         50_000_000, 64),
        ("300m", _template("ladder-300m", 1024, 12, 16, 16, 1024, 256),
         300_000_000, 128),
        ("1b", _template("ladder-1b", 1536, 16, 12, 32, 2048, 256),
         1_000_000_000, 128),
        ("7b", _template("ladder-7b", 3072, 32, 24, 32, 4096, 512),
         7_000_000_000, 128),
        ("30b", _template("ladder-30b", 4096, 40, 32, 64, 4096, 512),
         30_000_000_000, 128),
        ("100b", _template("ladder-100b", 6144, 48, 48, 64, 4096, 1024),
         100_000_000_000, 256),
        ("300b", _template("ladder-300b", 7168, 56, 56, 64, 4096, 1024),
         300_000_000_000, 256),
    ]
    return [(name, tpl, _ladder_target(total), align)
            for name, tpl, total, align in rungs]


def solve_ladder() -> list:
    out = []
    for name, tpl, target, align in ladder():
        out.append((name, solve(tpl, target, alignment=align)))
    out.append(("1t", solve_flagship()))
    return out
