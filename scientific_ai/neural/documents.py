"""Builders for the model manifest (directive s.50) -- pure, no JAX.

A model manifest restates nothing by hand: every count is
``accounting.count`` of the configuration it names, every estimate is
``estimates.estimate`` with its assumptions, the configuration is embedded
in full and its digest re-derives from it, and ``claim_status`` is
``claims.evaluate`` of the evidence offered -- so a manifest that claims
more than its evidence is a manifest whose recomputation disagrees.
"""
from __future__ import annotations

from . import accounting, claims, estimates
from .config import SCHEMA_VERSION, ModelConfig
from .manifests import MODEL_MANIFEST


def model_manifest(cfg: ModelConfig, *, solve: dict | None,
                   evidence=(), family_members=(), source_commit,
                   allocation_mode: str = "meta") -> dict:
    if allocation_mode not in ("meta", "real"):
        raise ValueError(f"allocation_mode {allocation_mode!r}")
    pc = accounting.count(cfg)
    c = pc.to_dict()
    a = cfg.attention
    doc = {
        "schema": MODEL_MANIFEST, "schema_version": SCHEMA_VERSION,
        "architecture_family": cfg.to_dict()["family"],
        "architecture_variant": cfg.variant,
        "configuration_digest": pc.config_digest,
        "configuration": cfg.to_dict(),
        "total_parameters": c["total_parameters"],
        "trainable_parameters": c["trainable_parameters"],
        "non_trainable_parameters": c["non_trainable_parameters"],
        "shared_parameters": c["shared_parameters"],
        "attention_parameters": c["attention_parameters"],
        "expert_parameters": c["expert_parameters"],
        "router_parameters": c["router_parameters"],
        "embedding_parameters": c["embedding_parameters"],
        "output_parameters": c["output_head_parameters"],
        "uncertainty_head_parameters": c["uncertainty_head_parameters"],
        "moe_layers": c["moe_layer_count"],
        "experts_per_moe_layer": c["num_experts_per_moe_layer"],
        "experts_selected_per_token": c["experts_selected_per_token"],
        "expert_parameters_per_expert": c["expert_parameters_per_expert"],
        "shared_active_parameters": c["shared_active_parameters"],
        "expert_active_parameters": c["expert_active_parameters"],
        "estimated_active_parameters_per_token":
            c["estimated_total_active_parameters_per_token"],
        "routing_profiles": c["routing_profiles"],
        "hidden_size": cfg.hidden_size, "num_layers": cfg.num_layers,
        "num_attention_heads": a.num_heads, "num_kv_heads": a.num_kv_heads,
        "head_dimension": a.head_dim,
        "expert_hidden_size": cfg.moe.expert_hidden if cfg.moe else None,
        "parameter_accounting": c,
        "precision_memory_estimates": estimates.estimate(cfg),
        "allocation_mode": allocation_mode,
        "solve": None if solve is None else {
            k: v for k, v in solve.items() if k != "config"},
        "claim_status": {},
        "source_commit": source_commit,
    }
    table = claims.evaluate(pc.config_digest, list(evidence) + [doc],
                            family_members=family_members)
    doc["claim_status"] = {
        "claims": table,
        "status_history": claims.derive_status(table),
        "status": claims.current_status(table),
        "plain_language": plain_language(cfg, table),
    }
    return doc


def plain_language(cfg: ModelConfig, table: dict) -> str:
    """What "the model" means here, in the precision directive s.52 asks."""
    pc = accounting.count(cfg)
    held = [c for c, v in table.items() if v["holds"]
            and v["scope"] == "SUBJECT"]
    member = table["DEVELOPMENT_MODEL_TRAINED"]
    parts = [f"{cfg.variant}: {pc.trainable_parameters:,} trainable "
             f"parameters, {pc.active_parameters_per_token:,} active per "
             "token (exact counts)."]
    if "ARCHITECTURE_META_VALIDATED" in held:
        parts.append("Mathematically parameterised and structurally "
                     "validated by zero-allocation (abstract) construction; "
                     "its weights have not been allocated.")
    if member["holds"] and member["scope"] == "FAMILY_MEMBER":
        parts.append("A development-scale member of the same architecture "
                     "family has been trained end to end; this "
                     "configuration has not been trained.")
    elif "DEVELOPMENT_MODEL_TRAINED" in held:
        parts.append("This configuration has been trained end to end on "
                     "its development dataset.")
    else:
        parts.append("It has not been trained.")
    parts.append("Its scientific performance is not established.")
    return " ".join(parts)
