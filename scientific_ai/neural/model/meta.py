"""Validate an architecture without allocating it -- and refuse to allocate.

``jax.eval_shape`` runs the REAL ``network.init`` and ``network.apply``
with abstract values: every tensor shape the code would create, every
matmul's operands, every routing buffer, is computed by tracing that code,
and no parameter is materialised. A dimension the code cannot combine fails
the trace exactly as it would fail a real run. This is the "equivalent
zero-allocation approach" of directive s.31 -- it is not a proof that the
model fits any device, and it is not an allocation.

WHAT :func:`validate` ESTABLISHES, AND HOW IT KNOWS NOTHING WAS ALLOCATED

* the abstract parameter tree's size, category by category, equals the
  closed-form count in ``accounting`` (two derivations, compared);
* the forward pass traces to heads of the declared shapes, with a router
  in every MoE layer selecting ``top_k`` of ``num_experts`` (int32
  indices, ``top_k <= num_experts``);
* the allocation guard: the number and bytes of live JAX arrays are the
  same before and after (``jax.live_arrays``), every leaf is a
  ``ShapeDtypeStruct``, and the process's peak RSS grew by less than
  ``META_RSS_LIMIT`` -- far below a single flagship expert tensor;
* :func:`materialize` refuses the configuration: its parameters exceed the
  real-allocation budget, so an ordinary command cannot allocate it.

REAL ALLOCATION IS OPT-IN

:func:`materialize` is the only way to get real parameters. It computes the
bytes from the exact count FIRST and refuses anything above
``max_bytes`` (default ``REAL_ALLOCATION_LIMIT``, 2 GiB) unless the caller
passes ``allow_large=True`` AND the environment sets
``QTA_NEURAL_ALLOW_LARGE_ALLOCATION=1``. Neither is set by any test, tool
or CI job in this repository.
"""
from __future__ import annotations

import math
import os
import resource

from .. import accounting
from ..config import ModelConfig
from ..estimates import DTYPE_BYTES
from . import network
from ._jax import require, versions

REAL_ALLOCATION_LIMIT = 2 * 2 ** 30
META_RSS_LIMIT = 1 * 2 ** 30
ALLOW_ENV = "QTA_NEURAL_ALLOW_LARGE_ALLOCATION"


class AllocationRefused(RuntimeError):
    """A real allocation above the budget was asked for without opt-in."""


def parameter_bytes(cfg: ModelConfig) -> int:
    pc = accounting.count(cfg)
    return (pc.trainable_parameters * DTYPE_BYTES[cfg.precision.param_dtype]
            + pc.non_trainable_parameters * 4)


def materialize(cfg: ModelConfig, key, *,
                max_bytes: int = REAL_ALLOCATION_LIMIT,
                allow_large: bool = False):
    """Real ``(params, buffers)`` -- refused before allocating anything when
    the configuration is above ``max_bytes`` without explicit opt-in."""
    need = parameter_bytes(cfg)
    opted_in = allow_large and os.environ.get(ALLOW_ENV) == "1"
    if need > max_bytes and not opted_in:
        raise AllocationRefused(
            f"{cfg.variant}: {need:,} parameter bytes exceed the "
            f"real-allocation budget {max_bytes:,}; use "
            "meta.validate (abstract, nothing allocated), or pass "
            f"allow_large=True with {ALLOW_ENV}=1 on hardware sized for it")
    # the approval network.init checks independently: granted only by the
    # opt-in, never by this function's own arithmetic
    return network.init(cfg, key,
                        approved_bytes=need if opted_in else None)


def abstract_init(cfg: ModelConfig):
    jax, _ = require()
    key = jax.random.key(0)
    return jax.eval_shape(lambda k: network.init(cfg, k), key)


def abstract_batch(cfg: ModelConfig, batch: int, features: int) -> dict:
    jax, jnp = require()
    s = jax.ShapeDtypeStruct
    return {"feature_ids": s((batch, features), jnp.int32),
            "context_ids": s((batch, features), jnp.int32),
            "validity": s((batch, features), jnp.int32),
            "dim_exponents": s((batch, features, 7), jnp.float32),
            "dim_class": s((batch, features), jnp.int32),
            "values": s((batch, features, 4), jnp.float32),
            "token_mask": s((batch, features), jnp.bool_)}


def _live():
    jax, _ = require()
    arrs = jax.live_arrays()
    return len(arrs), sum(int(a.nbytes) for a in arrs)


def _rss() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def validate(cfg: ModelConfig, *, batch: int = 2, features: int = 16) \
        -> dict:
    """The meta-validation report (``manifests.META_REPORT``)."""
    jax, jnp = require()
    cfg.validate()
    pc = accounting.count(cfg)
    key_ready = jax.random.key(0)   # the one tiny array the trace needs
    del key_ready
    live0, rss0 = _live(), _rss()
    p_abs, b_abs = abstract_init(cfg)
    leaves = jax.tree_util.tree_leaves((p_abs, b_abs))
    all_abstract = all(isinstance(x, jax.ShapeDtypeStruct) for x in leaves)
    by_cat = dict.fromkeys(accounting.CATEGORIES, 0)
    dtypes = set()
    for path, leaf in network.leaves_with_paths(p_abs):
        by_cat[network.category_of(path)] += math.prod(leaf.shape)
        dtypes.add(str(leaf.dtype))
    non_trainable = sum(math.prod(x.shape)
                        for x in jax.tree_util.tree_leaves(b_abs))
    forward, routing = {"traced": False}, {"legal": False}
    try:
        out = jax.eval_shape(
            lambda p, b, x: network.apply(p, b, x, cfg), p_abs, b_abs,
            abstract_batch(cfg, batch, features))
        n_heads = len(cfg.heads)
        forward = {
            "traced": True, "batch": batch, "features": features,
            "mean": list(out["mean"].shape), "var": list(out["var"].shape),
            "pooled": list(out["pooled"].shape),
            "heads_resolve": list(out["mean"].shape) == [batch, n_heads]
            and list(out["var"].shape) == [batch, n_heads]}
        tokens_n = batch * (features + cfg.num_readout_tokens)
        sel = [list(r["selected"].shape) for r in out["router"]]
        k = cfg.moe.top_k if cfg.moe else 0
        e = cfg.moe.num_experts if cfg.moe else 0
        routing = {
            "moe_layers_traced": len(out["router"]),
            "moe_layers_declared": len(cfg.moe_layer_indices),
            "selected_shapes": sorted({tuple(s) for s in map(tuple, sel)}),
            "index_dtype": sorted({str(r["selected"].dtype)
                                   for r in out["router"]}),
            "top_k": k, "num_experts": e,
            "capacity_per_expert": network.capacity(cfg, tokens_n)
            if cfg.moe else None,
            "legal": (len(out["router"]) == len(cfg.moe_layer_indices)
                      and all(s == [tokens_n, k] for s in sel)
                      and 1 <= k <= e and all(
                          str(r["selected"].dtype) == "int32"
                          for r in out["router"])) if cfg.moe else True}
        routing["selected_shapes"] = [list(s) for s in
                                      routing["selected_shapes"]]
    except Exception as exc:   # a trace failure IS the finding
        forward = {"traced": False, "error": f"{type(exc).__name__}: "
                                             f"{exc}"[:500]}
    live1, rss1 = _live(), _rss()
    guard = {
        "live_arrays_before": live0[0], "live_arrays_after": live1[0],
        "live_bytes_before": live0[1], "live_bytes_after": live1[1],
        "every_leaf_abstract": all_abstract,
        "peak_rss_growth_bytes": max(0, rss1 - rss0),
        "peak_rss_limit_bytes": META_RSS_LIMIT,
        "would_be_parameter_bytes": parameter_bytes(cfg),
    }
    guard["passed"] = (all_abstract and live1 == live0
                       and guard["peak_rss_growth_bytes"] < META_RSS_LIMIT)
    # above the budget a real allocation MUST be refused (and nothing is
    # allocated trying); within it, allocation is the ordinary path and the
    # question does not apply
    over = parameter_bytes(cfg) > REAL_ALLOCATION_LIMIT
    guard["real_allocation_limit_bytes"] = REAL_ALLOCATION_LIMIT
    refused = None
    if over:
        try:
            materialize(cfg, None)
            refused = False
        except AllocationRefused:
            refused = True
        except Exception:
            refused = False
    counts_equal = (by_cat == pc.by_category
                    and non_trainable == pc.non_trainable_parameters)
    ok = (counts_equal and forward.get("traced") is True
          and forward.get("heads_resolve") is True
          and routing.get("legal") is True and guard["passed"])
    if over:
        ok = ok and refused is True
    return {
        "schema": "scientific-moe-meta-validation/1",
        "config_digest": pc.config_digest,
        "framework": {k: v for k, v in versions().items()
                      if k != "devices"},
        "method": "jax.eval_shape of network.init and network.apply: the "
                  "real code traced with abstract values",
        "allocation_mode": "meta",
        "analytic_by_category": pc.by_category,
        "abstract_by_category": by_cat,
        "abstract_trainable": sum(by_cat.values()),
        "abstract_non_trainable": non_trainable,
        "counts_equal": counts_equal,
        "abstract_dtypes": sorted(dtypes),
        "forward": forward, "routing": routing,
        "allocation_guard": guard,
        "real_allocation_refused": refused,
        "result": "PASS" if ok else "FAIL",
    }
