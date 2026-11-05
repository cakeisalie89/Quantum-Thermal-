"""The network: init and apply for one member of the family, in plain JAX.

Parameters are a nested dict of arrays; their PATHS are the contract with
``accounting``: :func:`category_of` assigns every path to exactly one
parameter category and refuses a path it does not know, so a tensor added
here without a category is an error, not an uncounted parameter.

INITIALISATION

Each tensor's key is ``fold_in(root, sha256(path))``: the same seed gives
the same tensor whatever order the tree is built in. Embedding tables and
heads ~ N(0, 0.02^2); a projection [fan_in, fan_out] ~ N(0, 1/fan_in); the
two projections that write into the residual stream (attention output,
FFN/expert down) are further scaled by 1/sqrt(2 num_layers) so the stream's
variance does not grow with depth; biases 0, RMSNorm scales 1.

ROUTING (``moe``)

Router logits in float32, softmax over experts, ``lax.top_k`` (distinct
indices by construction), gates renormalised over the selected experts when
``normalize_top_k``. Each expert has a buffer of ``capacity`` slots; a
token's choices take slots in GShard priority order -- every token's first
choice, then every token's second, in token order -- and a choice that
finds its expert full is DROPPED: it computes nothing, contributes nothing,
and is counted. With ``capacity_factor=None`` the capacity is the number of
tokens and nothing can drop. Padding tokens are never routed. The router
statistics of every MoE layer come back with the outputs, so a collapse
cannot hide behind a training loss.
"""
from __future__ import annotations

import hashlib
import math

from .. import tokens
from ..config import ModelConfig
from ..units import DIMENSION_CLASSES
from ._jax import require

EMBED_STD = 0.02
NORM_EPS = 1e-6
#: Floor of the predicted variance (standardised target units squared): a
#: parametrisation that keeps the Gaussian likelihood finite, stated here.
VAR_FLOOR = 1e-6
NEG_INF = -1e30


class NetworkError(ValueError):
    pass


# -- parameter paths and categories --------------------------------------

def category_of(path: tuple) -> str:
    """The accounting category of the tensor at ``path`` (a tuple of keys
    and list indices)."""
    p = [str(x) for x in path]
    head = p[0]
    if head == "embed":
        return {"feature": "feature_embedding",
                "readout": "readout_embedding",
                "context": "context_embedding",
                "validity": "context_embedding",
                "dim_proj": "dimensional_encoder",
                "dim_class": "dimensional_encoder"}[p[1]]
    if head == "value_encoder":
        return "numerical_encoder"
    if head == "final_norm":
        return "normalization"
    if head == "heads":
        return {"mean_w": "output_head", "mean_b": "output_head",
                "var_w": "uncertainty_head",
                "var_b": "uncertainty_head"}[p[1]]
    if head == "recon":
        return "reconstruction_head"
    if head == "layers":
        part = p[2]
        if part in ("attn_norm", "ffn_norm"):
            return "normalization"
        if part == "attn":
            return "attention"
        if part == "ffn":
            return "dense_ffn"
        if part == "moe":
            return {"router": "router", "experts": "expert",
                    "shared": "shared_expert"}[p[3]]
    raise NetworkError(f"no parameter category for path {'/'.join(p)}")


def _key_for(root, path: str):
    jax, _ = require()
    h = int.from_bytes(hashlib.sha256(path.encode()).digest()[:4], "big")
    return jax.random.fold_in(root, h)


def _normal(root, path, shape, std, dtype):
    jax, jnp = require()
    return (std * jax.random.normal(_key_for(root, path), shape,
                                    jnp.float32)).astype(dtype)


class RealAllocationRefused(RuntimeError):
    """``init`` was asked for real arrays it was not approved to create."""


#: The parameter bytes ``init`` builds for real without an approval from
#: ``meta.materialize``; the same number as ``meta.REAL_ALLOCATION_LIMIT``.
INIT_LIMIT_BYTES = 2 * 2 ** 30


def _real_bytes(cfg: ModelConfig) -> int:
    from ..accounting import count
    from ..estimates import DTYPE_BYTES
    pc = count(cfg)
    return (pc.trainable_parameters * DTYPE_BYTES[cfg.precision.param_dtype]
            + pc.non_trainable_parameters * 4)


def init(cfg: ModelConfig, key, *, approved_bytes: int | None = None):
    """``(params, buffers)`` for ``cfg``. Allocates them: call it through
    ``meta.materialize`` (guarded) or under ``jax.eval_shape``.

    A second, independent guard (directive s.32): called with a CONCRETE
    key -- not under an abstract trace -- for a configuration above
    ``INIT_LIMIT_BYTES``, it refuses unless ``approved_bytes`` covers it, so
    a path that reaches the network without going through ``materialize``
    fails at once with a diagnostic instead of allocating terabytes."""
    jax, jnp = require()
    cfg.validate()
    if not isinstance(key, jax.core.Tracer):
        need = _real_bytes(cfg)
        if need > INIT_LIMIT_BYTES and (approved_bytes is None
                                        or approved_bytes < need):
            raise RealAllocationRefused(
                f"network.init: {cfg.variant} would create {need:,} bytes "
                "of real parameters without approval")
    dt = jnp.dtype(cfg.precision.param_dtype)
    d = cfg.hidden_size
    a = cfg.attention
    inner = a.num_heads * a.head_dim
    kv = a.num_kv_heads * a.head_dim
    resid = 1.0 / math.sqrt(2 * cfg.num_layers)
    n = len(cfg.heads)

    def nrm(path, shape, std):
        return _normal(key, path, shape, std, dt)

    def ones(shape):
        return jnp.ones(shape, dt)

    def zeros(shape):
        return jnp.zeros(shape, dt)

    params = {
        "embed": {
            "feature": nrm("embed/feature", (cfg.feature_vocab_size, d),
                           EMBED_STD),
            "readout": nrm("embed/readout", (cfg.num_readout_tokens, d),
                           EMBED_STD),
            "context": nrm("embed/context", (cfg.context_vocab_size, d),
                           EMBED_STD),
            "validity": nrm("embed/validity", (len(tokens.VALIDITY), d),
                            EMBED_STD),
            "dim_proj": nrm("embed/dim_proj", (tokens.N_BASE_DIMS, d),
                            EMBED_STD),
            "dim_class": nrm("embed/dim_class", (len(DIMENSION_CLASSES), d),
                             EMBED_STD),
        },
        "value_encoder": {
            "w1": nrm("value_encoder/w1", (len(tokens.VALUE_CHANNELS),
                                           cfg.value_encoder_hidden),
                      1.0 / math.sqrt(len(tokens.VALUE_CHANNELS))),
            "b1": zeros((cfg.value_encoder_hidden,)),
            "w2": nrm("value_encoder/w2", (cfg.value_encoder_hidden, d),
                      1.0 / math.sqrt(cfg.value_encoder_hidden)),
            "b2": zeros((d,)),
        },
        "layers": [],
        "final_norm": ones((d,)),
        "heads": {
            "mean_w": nrm("heads/mean_w", (d, n), EMBED_STD),
            "mean_b": zeros((n,)),
            "var_w": nrm("heads/var_w", (d, n), EMBED_STD),
            "var_b": zeros((n,)),
        },
    }
    for i in range(cfg.num_layers):
        pre = f"layers/{i}"
        layer = {
            "attn_norm": ones((d,)),
            "attn": {
                "q": nrm(f"{pre}/attn/q", (d, inner), 1 / math.sqrt(d)),
                "k": nrm(f"{pre}/attn/k", (d, kv), 1 / math.sqrt(d)),
                "v": nrm(f"{pre}/attn/v", (d, kv), 1 / math.sqrt(d)),
                "o": nrm(f"{pre}/attn/o", (inner, d),
                         resid / math.sqrt(inner)),
            },
            "ffn_norm": ones((d,)),
        }
        if cfg.is_moe_layer(i):
            m = cfg.moe_block
            e, f = m.num_experts, m.expert_hidden
            moe = {
                "router": nrm(f"{pre}/moe/router", (d, e), 1 / math.sqrt(d)),
                "experts": {
                    "gate": nrm(f"{pre}/moe/experts/gate", (e, d, f),
                                1 / math.sqrt(d)),
                    "up": nrm(f"{pre}/moe/experts/up", (e, d, f),
                              1 / math.sqrt(d)),
                    "down": nrm(f"{pre}/moe/experts/down", (e, f, d),
                                resid / math.sqrt(f)),
                },
            }
            if m.num_shared_experts:
                s, fs = m.num_shared_experts, m.shared_hidden
                moe["shared"] = {
                    "gate": nrm(f"{pre}/moe/shared/gate", (s, d, fs),
                                1 / math.sqrt(d)),
                    "up": nrm(f"{pre}/moe/shared/up", (s, d, fs),
                              1 / math.sqrt(d)),
                    "down": nrm(f"{pre}/moe/shared/down", (s, fs, d),
                                resid / math.sqrt(fs)),
                }
            layer["moe"] = moe
        else:
            fd = cfg.dense_ffn_hidden
            layer["ffn"] = {
                "gate": nrm(f"{pre}/ffn/gate", (d, fd), 1 / math.sqrt(d)),
                "up": nrm(f"{pre}/ffn/up", (d, fd), 1 / math.sqrt(d)),
                "down": nrm(f"{pre}/ffn/down", (fd, d),
                            resid / math.sqrt(fd)),
            }
        params["layers"].append(layer)
    if cfg.reconstruction_head == "untied":
        params["recon"] = {"decoder": nrm("recon/decoder",
                                          (cfg.feature_vocab_size, d),
                                          EMBED_STD),
                           "bias": zeros((1,))}
    elif cfg.reconstruction_head == "tied":
        params["recon"] = {"bias": zeros((1,))}
    # non-trainable: (location, scale) per feature identity and per target,
    # filled from the TRAINING split's statistics; identity until then
    buffers = {
        "feature_norm": jnp.concatenate(
            [jnp.zeros((cfg.feature_vocab_size, 1), jnp.float32),
             jnp.ones((cfg.feature_vocab_size, 1), jnp.float32)], axis=1),
        "target_norm": jnp.concatenate(
            [jnp.zeros((n, 1), jnp.float32), jnp.ones((n, 1), jnp.float32)],
            axis=1),
    }
    return params, buffers


def leaves_with_paths(tree) -> list:
    jax, _ = require()
    out = []
    for path, leaf in jax.tree_util.tree_flatten_with_path(tree)[0]:
        keys = []
        for p in path:
            keys.append(getattr(p, "key", getattr(p, "idx", None)))
        out.append((tuple(keys), leaf))
    return out


def count_by_category(params) -> dict:
    from ..accounting import CATEGORIES
    out = dict.fromkeys(CATEGORIES, 0)
    for path, leaf in leaves_with_paths(params):
        out[category_of(path)] += math.prod(leaf.shape)
    return out


# -- forward -------------------------------------------------------------

def _rmsnorm(x, scale):
    _, jnp = require()
    x32 = x.astype(jnp.float32)
    y = x32 * jnp.reciprocal(jnp.sqrt(jnp.mean(x32 * x32, axis=-1,
                                               keepdims=True) + NORM_EPS))
    return (y * scale.astype(jnp.float32)).astype(x.dtype)


def _swiglu(x, gate, up, down):
    jax, jnp = require()
    return (jax.nn.silu(x @ gate) * (x @ up)) @ down


def embed(params, batch, cfg: ModelConfig):
    """[B, F + R, d] token representations and the [B, F + R] mask."""
    jax, jnp = require()
    ct = jnp.dtype(cfg.precision.compute_dtype)
    e = jax.tree_util.tree_map(lambda t: t.astype(ct), params["embed"])
    v = params["value_encoder"]
    vals = batch["values"].astype(ct)
    venc = jax.nn.silu(vals @ v["w1"].astype(ct) + v["b1"].astype(ct)) \
        @ v["w2"].astype(ct) + v["b2"].astype(ct)
    valid = (batch["validity"] == tokens.VALIDITY.index("VALID"))
    venc = venc * valid[..., None].astype(ct)
    x = (e["feature"][batch["feature_ids"]]
         + e["context"][batch["context_ids"]]
         + e["validity"][batch["validity"]]
         + batch["dim_exponents"].astype(ct) @ e["dim_proj"]
         + e["dim_class"][batch["dim_class"]]
         + venc)
    b = x.shape[0]
    readout = jnp.broadcast_to(e["readout"][None],
                               (b,) + e["readout"].shape)
    x = jnp.concatenate([x, readout], axis=1)
    mask = batch.get("token_mask")
    if mask is None:
        mask = jnp.ones(batch["feature_ids"].shape, jnp.bool_)
    mask = jnp.concatenate([mask.astype(jnp.bool_),
                            jnp.ones((b, cfg.num_readout_tokens),
                                     jnp.bool_)], axis=1)
    return x, mask


def attention(p, x, mask, cfg: ModelConfig):
    jax, jnp = require()
    a = cfg.attention
    b, s, _ = x.shape
    ct = x.dtype
    q = (x @ p["q"].astype(ct)).reshape(b, s, a.num_heads, a.head_dim)
    k = (x @ p["k"].astype(ct)).reshape(b, s, a.num_kv_heads, a.head_dim)
    v = (x @ p["v"].astype(ct)).reshape(b, s, a.num_kv_heads, a.head_dim)
    group = a.num_heads // a.num_kv_heads
    if group > 1:
        k = jnp.repeat(k, group, axis=2)
        v = jnp.repeat(v, group, axis=2)
    scores = jnp.einsum("bqhd,bkhd->bhqk", q.astype(jnp.float32),
                        k.astype(jnp.float32)) / math.sqrt(a.head_dim)
    scores = jnp.where(mask[:, None, None, :], scores, NEG_INF)
    w = jax.nn.softmax(scores, axis=-1)
    out = jnp.einsum("bhqk,bkhd->bqhd", w, v.astype(jnp.float32))
    out = out.reshape(b, s, a.num_heads * a.head_dim).astype(ct)
    return out @ p["o"].astype(ct)


def capacity(cfg: ModelConfig, n_tokens: int) -> int:
    m = cfg.moe_block
    if m.capacity_factor is None:
        return n_tokens
    return max(1, math.ceil(m.capacity_factor * n_tokens * m.top_k
                            / m.num_experts))


def route(p_router, h, token_mask, cfg: ModelConfig, top_k: int | None = None):
    """Router decision for flat tokens h [T, d]. Returns a dict of arrays."""
    jax, jnp = require()
    m = cfg.moe_block
    k = m.top_k if top_k is None else top_k
    logits = (h.astype(jnp.float32) @ p_router.astype(jnp.float32)) \
        / m.router_temperature
    finite = jnp.all(jnp.isfinite(logits))
    probs = jax.nn.softmax(logits, axis=-1)
    top_p, top_i = jax.lax.top_k(probs, k)
    gates = top_p / jnp.sum(top_p, axis=-1, keepdims=True) \
        if m.normalize_top_k else top_p
    live = token_mask.astype(jnp.float32)
    ent = -jnp.sum(probs * jnp.log(jnp.clip(probs, 1e-30)), axis=-1)
    n_live = jnp.maximum(jnp.sum(live), 1.0)
    return {"logits": logits, "probs": probs, "selected": top_i,
            "gates": gates, "finite": finite,
            "entropy": jnp.sum(ent * live) / n_live, "live": live,
            "n_live": n_live, "top_k": k}


def dispatch_positions(selected, live, n_experts: int, cap: int):
    """Slot of each (token, choice) in its expert's buffer, GShard order,
    and whether it was kept. Padding tokens are never kept."""
    jax, jnp = require()
    t, k = selected.shape
    onehot = jax.nn.one_hot(selected, n_experts, dtype=jnp.int32)
    onehot = onehot * live.astype(jnp.int32)[:, None, None]
    order = jnp.transpose(onehot, (1, 0, 2)).reshape(k * t, n_experts)
    pos = jnp.cumsum(order, axis=0) - 1
    pos = jnp.transpose(pos.reshape(k, t, n_experts), (1, 0, 2))
    slot = jnp.sum(pos * onehot, axis=-1)
    assigned = jnp.sum(onehot, axis=-1) > 0
    kept = assigned & (slot < cap)
    return slot, kept, assigned


def moe(p, h, token_mask, cfg: ModelConfig, top_k: int | None = None):
    """Mixture of experts over flat tokens h [T, d]; returns (y, stats)."""
    jax, jnp = require()
    m = cfg.moe_block
    t, d = h.shape
    e = m.num_experts
    r = route(p["router"], h, token_mask, cfg, top_k)
    k = r["top_k"]
    cap = capacity(cfg, t)
    slot, kept, assigned = dispatch_positions(r["selected"], r["live"], e,
                                              cap)
    ct = h.dtype
    flat_e = r["selected"].reshape(-1)
    flat_s = jnp.where(kept, slot, cap).reshape(-1)    # cap = out of range
    src = jnp.repeat(h, k, axis=0)
    buf = jnp.zeros((e, cap, d), ct).at[flat_e, flat_s].add(src,
                                                            mode="drop")
    ex = p["experts"]
    hid = jax.nn.silu(jnp.einsum("ecd,edf->ecf", buf,
                                 ex["gate"].astype(ct))) \
        * jnp.einsum("ecd,edf->ecf", buf, ex["up"].astype(ct))
    out = jnp.einsum("ecf,efd->ecd", hid, ex["down"].astype(ct))
    gathered = out.at[flat_e, flat_s].get(mode="fill", fill_value=0)
    w = (r["gates"] * kept.astype(jnp.float32)).reshape(-1, 1).astype(ct)
    y = jnp.sum((gathered * w).reshape(t, k, d), axis=1)
    if "shared" in p:
        s = p["shared"]
        sh = jax.nn.silu(jnp.einsum("td,sdf->tsf", h, s["gate"].astype(ct))) \
            * jnp.einsum("td,sdf->tsf", h, s["up"].astype(ct))
        y = y + jnp.einsum("tsf,sfd->td", sh, s["down"].astype(ct))
    y = y * token_mask.astype(ct)[:, None]
    assigned_n = jnp.sum(assigned.astype(jnp.float32))
    counts = jnp.sum(jax.nn.one_hot(r["selected"], e, dtype=jnp.float32)
                     * kept[..., None].astype(jnp.float32), axis=(0, 1))
    frac = jnp.sum(jax.nn.one_hot(r["selected"], e, dtype=jnp.float32)
                   * assigned[..., None].astype(jnp.float32), axis=(0, 1)) \
        / jnp.maximum(assigned_n, 1.0)
    mean_p = jnp.sum(r["probs"] * r["live"][:, None], axis=0) / r["n_live"]
    stats = {
        "selected": r["selected"], "gates": r["gates"], "kept": kept,
        "expert_counts": counts,
        "dropped": assigned_n - jnp.sum(kept.astype(jnp.float32)),
        "assigned": assigned_n,
        "entropy": r["entropy"],
        "aux_loss": e * jnp.sum(frac * mean_p),
        "finite": r["finite"],
        "max_load_fraction": jnp.max(counts)
        / jnp.maximum(jnp.sum(counts), 1.0),
        "capacity": cap, "top_k": k,
    }
    return y, stats


def apply(params, buffers, batch, cfg: ModelConfig, *,
          top_k: int | None = None, remat: bool = False):
    """Forward pass. Returns predictions in STANDARDISED target space:
    ``mean`` and ``var`` [B, n_heads], the pooled readout ``pooled``
    [B, d], the final token states ``tokens`` and per-MoE-layer router
    statistics ``router``."""
    jax, jnp = require()
    x, mask = embed(params, batch, cfg)
    b, s, d = x.shape
    router = []

    def block(lp, x, i):
        h = _rmsnorm(x, lp["attn_norm"])
        x = x + attention(lp["attn"], h, mask, cfg)
        h = _rmsnorm(x, lp["ffn_norm"])
        if cfg.is_moe_layer(i):
            y, st = moe(lp["moe"], h.reshape(b * s, d), mask.reshape(-1),
                        cfg, top_k)
            return x + y.reshape(b, s, d), st
        f = lp["ffn"]
        ct = h.dtype
        y = _swiglu(h, f["gate"].astype(ct), f["up"].astype(ct),
                    f["down"].astype(ct))
        return x + y * mask[..., None].astype(ct), None

    for i, lp in enumerate(params["layers"]):
        fn = jax.checkpoint(block, static_argnums=(2,)) if remat else block
        x, st = fn(lp, x, i)
        if st is not None:
            router.append(st)
    x = _rmsnorm(x, params["final_norm"])
    f = batch["feature_ids"].shape[1]
    pooled = jnp.mean(x[:, f:, :].astype(jnp.float32), axis=1)
    hd = params["heads"]
    raw = pooled @ hd["mean_w"].astype(jnp.float32) \
        + hd["mean_b"].astype(jnp.float32)
    mean = _bounded(raw, buffers["target_norm"], cfg)
    var = jax.nn.softplus(pooled @ hd["var_w"].astype(jnp.float32)
                          + hd["var_b"].astype(jnp.float32)) + VAR_FLOOR
    return {"mean": mean, "var": var, "pooled": pooled, "tokens": x,
            "router": router}


def _bounded(raw, target_norm, cfg: ModelConfig):
    """A bounded head's mean lies in its bounds by construction: the
    sigmoid maps onto [lower, upper] expressed in standardised units."""
    jax, jnp = require()
    cols = []
    for j, h in enumerate(cfg.heads):
        if h.kind == "bounded_gaussian":
            loc, sc = target_norm[j, 0], target_norm[j, 1]
            lo = (h.lower - loc) / sc
            hi = (h.upper - loc) / sc
            cols.append(lo + (hi - lo) * jax.nn.sigmoid(raw[:, j]))
        else:
            cols.append(raw[:, j])
    return jnp.stack(cols, axis=1)


def reconstruct(params, token_states, feature_ids, cfg: ModelConfig):
    """The auxiliary masked-feature reconstruction: a standardised value
    per feature token. ``tied``: the decoder vector of feature i IS row i
    of the feature-identity table."""
    _, jnp = require()
    if cfg.reconstruction_head == "none":
        raise NetworkError("this configuration has no reconstruction head")
    rec = params["recon"]
    dec = params["embed"]["feature"] if cfg.reconstruction_head == "tied" \
        else rec["decoder"]
    f = feature_ids.shape[1]
    xs = token_states[:, :f, :].astype(jnp.float32)
    return jnp.einsum("bfd,bfd->bf", xs,
                      dec[feature_ids].astype(jnp.float32)) \
        / math.sqrt(cfg.hidden_size) + rec["bias"][0].astype(jnp.float32)
