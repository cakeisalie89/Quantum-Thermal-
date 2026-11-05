"""NF-1T s.12-13, s.57: the router and the mixture-of-experts layer."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neural_support import require_jax  # noqa: E402

jax = require_jax()
jnp = jax.numpy

from scientific_ai.neural import family  # noqa: E402
from scientific_ai.neural.model import meta, network  # noqa: E402


def cfg(**moe):
    c = family.development()
    return replace(c, moe=replace(c.moe, **moe)) if moe else c


def layer(c, seed=0):
    p, _ = meta.materialize(c, jax.random.key(seed))
    return p["layers"][0]["moe"]


def tokens_(t=48, d=64, seed=1):
    return jax.random.normal(jax.random.key(seed), (t, d))


def test_router_shapes_and_top_k_are_the_largest_probabilities():
    c = cfg()
    r = network.route(layer(c)["router"], tokens_(), jnp.ones(48, bool), c)
    assert r["selected"].shape == (48, 2) and r["gates"].shape == (48, 2)
    assert r["probs"].shape == (48, 8)
    probs = np.asarray(r["probs"])
    want = np.argsort(-probs, axis=1, kind="stable")[:, :2]
    assert np.array_equal(np.sort(np.asarray(r["selected"]), axis=1),
                          np.sort(want, axis=1))


def test_indices_are_in_range_unique_and_exactly_k_even_on_ties():
    c = cfg(top_k=3)
    zero = jnp.zeros((64, 8))
    r = network.route(zero, tokens_(), jnp.ones(48, bool), c)
    sel = np.asarray(r["selected"])
    assert sel.shape == (48, 3)
    assert np.all((sel >= 0) & (sel < 8))
    assert all(len(set(row)) == 3 for row in sel.tolist())


def test_gates_are_normalised_over_the_selected_experts():
    c = cfg()
    r = network.route(layer(c)["router"], tokens_(), jnp.ones(48, bool), c)
    assert np.allclose(np.asarray(r["gates"]).sum(axis=1), 1.0, atol=1e-6)
    c2 = cfg(normalize_top_k=False)
    r2 = network.route(layer(c2)["router"], tokens_(), jnp.ones(48, bool),
                       c2)
    probs = np.asarray(r2["probs"])
    sel = np.asarray(r2["selected"])
    assert np.allclose(np.asarray(r2["gates"]),
                       np.take_along_axis(probs, sel, axis=1))


def test_routing_is_deterministic_jitted_or_not():
    c = cfg()
    p = layer(c)
    h = tokens_()
    m = jnp.ones(48, bool)
    a = network.moe(p, h, m, c)
    b = network.moe(p, h, m, c)
    j = jax.jit(lambda x: network.moe(p, x, m, c))(h)
    assert np.array_equal(np.asarray(a[1]["selected"]),
                          np.asarray(b[1]["selected"]))
    assert np.array_equal(np.asarray(a[1]["selected"]),
                          np.asarray(j[1]["selected"]))
    assert np.array_equal(np.asarray(a[0]), np.asarray(b[0]))


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_router_score_is_detected(bad):
    c = cfg()
    h = tokens_().at[3, 5].set(bad)
    _, st = network.moe(layer(c), h, jnp.ones(48, bool), c)
    assert bool(st["finite"]) is False


def test_expert_load_is_counted_and_sums_to_the_kept_choices():
    c = cfg()
    _, st = network.moe(layer(c), tokens_(), jnp.ones(48, bool), c)
    counts = np.asarray(st["expert_counts"])
    assert counts.sum() == 48 * 2 == float(st["assigned"])
    assert float(st["dropped"]) == 0.0
    assert np.isclose(float(st["max_load_fraction"]), counts.max() / 96)


def _gshard_kept(sel: np.ndarray, e: int, cap: int) -> np.ndarray:
    """Independent restatement: choice rank first, then token order."""
    t, k = sel.shape
    used = np.zeros(e, int)
    kept = np.zeros((t, k), bool)
    for r in range(k):
        for i in range(t):
            ex = sel[i, r]
            if used[ex] < cap:
                kept[i, r] = True
                used[ex] += 1
    return kept


def test_capacity_drops_are_exactly_the_gshard_overflow_and_counted():
    c = cfg(capacity_factor=0.5)
    p = layer(c)
    h = tokens_()
    y, st = network.moe(p, h, jnp.ones(48, bool), c)
    cap = network.capacity(c, 48)
    assert cap == int(np.ceil(0.5 * 48 * 2 / 8))
    want = _gshard_kept(np.asarray(st["selected"]), 8, cap)
    assert np.array_equal(np.asarray(st["kept"]), want)
    assert float(st["dropped"]) == float((~want).sum()) > 0
    assert np.asarray(st["expert_counts"]).max() <= cap


def test_a_dropped_choice_contributes_nothing():
    c = cfg(capacity_factor=0.5)
    p = layer(c)
    h = tokens_()
    y, st = network.moe(p, h, jnp.ones(48, bool), c)
    sel = np.asarray(st["selected"])
    gates = np.asarray(st["gates"])
    kept = np.asarray(st["kept"])
    ex = p["experts"]
    hn = np.asarray(h)
    want = np.zeros_like(hn)
    for i in range(48):
        for r in range(2):
            if not kept[i, r]:
                continue
            e = sel[i, r]
            g = hn[i] @ np.asarray(ex["gate"][e])
            u = hn[i] @ np.asarray(ex["up"][e])
            hid = g / (1 + np.exp(-g)) * u
            want[i] += gates[i, r] * (hid @ np.asarray(ex["down"][e]))
    assert np.allclose(np.asarray(y), want, rtol=1e-4, atol=1e-5)


def test_padding_tokens_are_never_routed():
    c = cfg()
    m = jnp.asarray([True] * 40 + [False] * 8)
    y, st = network.moe(layer(c), tokens_(), m, c)
    assert not np.asarray(st["kept"])[40:].any()
    assert np.all(np.asarray(y)[40:] == 0)
    assert float(st["assigned"]) == 40 * 2


def test_the_auxiliary_loss_is_the_switch_formula():
    c = cfg()
    p = layer(c)
    h = tokens_()
    _, st = network.moe(p, h, jnp.ones(48, bool), c)
    r = network.route(p["router"], h, jnp.ones(48, bool), c)
    probs = np.asarray(r["probs"])
    sel = np.asarray(r["selected"])
    f = np.bincount(sel.ravel(), minlength=8) / sel.size
    want = 8 * np.sum(f * probs.mean(axis=0))
    assert np.isclose(float(st["aux_loss"]), want, rtol=1e-5)
    # perfectly uniform routing scores 1
    flat = network.route(jnp.zeros((64, 8)), h, jnp.ones(48, bool), c)
    assert np.allclose(np.asarray(flat["probs"]), 1 / 8)


def test_only_selected_experts_receive_gradient():
    c = cfg()
    p = layer(c)
    h = tokens_(t=6)
    m = jnp.ones(6, bool)

    def loss(px):
        y, _ = network.moe(px, h, m, c)
        return jnp.sum(y ** 2)

    g = jax.grad(loss)(p)
    _, st = network.moe(p, h, m, c)
    used = set(np.asarray(st["selected"]).ravel().tolist())
    for e in range(8):
        norm = sum(float(jnp.sum(jnp.abs(g["experts"][w][e])))
                   for w in ("gate", "up", "down"))
        assert (norm > 0) == (e in used), e
    assert float(jnp.sum(jnp.abs(g["router"]))) > 0


def test_a_routing_profile_changes_how_many_experts_compute():
    c = cfg()
    p = layer(c)
    for k in (1, 2, 4):
        _, st = network.moe(p, tokens_(), jnp.ones(48, bool), c, top_k=k)
        assert np.asarray(st["selected"]).shape == (48, k)
        assert np.asarray(st["expert_counts"]).sum() == 48 * k


def test_shared_experts_apply_to_every_token():
    c = cfg(num_shared_experts=1, shared_hidden=32)
    p = layer(c)
    h = tokens_()
    y, _ = network.moe(p, h, jnp.ones(48, bool), c)
    c0 = cfg()
    y0, _ = network.moe({k: v for k, v in p.items() if k != "shared"}, h,
                        jnp.ones(48, bool), c0)
    s = p["shared"]
    hn = np.asarray(h)
    g = hn @ np.asarray(s["gate"][0])
    shared = (g / (1 + np.exp(-g)) * (hn @ np.asarray(s["up"][0]))) \
        @ np.asarray(s["down"][0])
    assert np.allclose(np.asarray(y) - np.asarray(y0), shared, rtol=1e-4,
                       atol=1e-5)
