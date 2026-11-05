"""NF-1T s.19-22, s.55 (instantiated), s.58: the network itself."""
from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neural_support import batch, require_jax  # noqa: E402

jax = require_jax()
jnp = jax.numpy

from scientific_ai.neural import accounting, family  # noqa: E402
from scientific_ai.neural.config import (AttentionConfig,  # noqa: E402
                                         MoEConfig, PrecisionProfile)
from scientific_ai.neural.model import checkpoint, meta, network  # noqa


def dev():
    return family.development()


VARIANTS = {
    "development": dev(),
    "gqa": replace(dev(), attention=AttentionConfig(4, 16, 2)),
    "shared_expert": replace(dev(), moe=replace(
        dev().moe, num_shared_experts=1, shared_hidden=32)),
    "dense_then_moe": replace(dev(), num_layers=3, moe_layer_start=1,
                              dense_ffn_hidden=96),
    "every_other_layer": replace(dev(), num_layers=4, moe_layer_interval=2,
                                 dense_ffn_hidden=96),
    "tied_reconstruction": replace(dev(), reconstruction_head="tied"),
    "untied_reconstruction": replace(dev(), reconstruction_head="untied"),
    "readout_4": replace(dev(), num_readout_tokens=4,
                         feature_vocab_size=16),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_instantiated_tensors_count_exactly_what_accounting_says(name):
    c = VARIANTS[name]
    p, b = meta.materialize(c, jax.random.key(0))
    pc = accounting.count(c)
    assert network.count_by_category(p) == pc.by_category
    assert sum(int(np.prod(x.shape)) for x in jax.tree_util.tree_leaves(b)) \
        == pc.non_trainable_parameters


@settings(max_examples=40, deadline=None, derandomize=True)
@given(heads=st.sampled_from([1, 2, 4]), hd=st.sampled_from([4, 8]),
       layers=st.integers(1, 4), e=st.integers(2, 6), f=st.integers(1, 40),
       vocab=st.integers(1, 12), hv=st.integers(1, 9),
       readout=st.integers(1, 3))
def test_abstract_tensors_count_what_accounting_says(heads, hd, layers, e, f,
                                                     vocab, hv, readout):
    c = replace(dev(), hidden_size=heads * hd, num_layers=layers,
                attention=AttentionConfig(heads, hd, heads),
                moe=MoEConfig(num_experts=e, top_k=1, expert_hidden=f),
                feature_vocab_size=vocab, value_encoder_hidden=hv,
                num_readout_tokens=readout)
    rep = meta.validate(c, batch=2, features=3)
    assert rep["counts_equal"] and rep["result"] == "PASS"


def test_every_parameter_path_has_exactly_one_category():
    for c in VARIANTS.values():
        p, _ = meta.materialize(c, jax.random.key(0))
        for path, _leaf in network.leaves_with_paths(p):
            assert network.category_of(path) in accounting.CATEGORIES
    with pytest.raises(network.NetworkError):
        network.category_of(("layers", 0, "padding"))
    with pytest.raises(network.NetworkError):
        network.category_of(("dummy",))


def test_the_tied_decoder_is_the_feature_table_and_has_no_copy():
    c = VARIANTS["tied_reconstruction"]
    p, _ = meta.materialize(c, jax.random.key(0))
    assert set(p["recon"]) == {"bias"}
    x = batch(c)
    out = network.apply(p, _identity_buffers(c), x, c)
    g = jax.grad(lambda pp: jnp.sum(network.reconstruct(
        pp, network.apply(pp, _identity_buffers(c), x, c)["tokens"],
        x["feature_ids"], c)))(p)
    assert float(jnp.sum(jnp.abs(g["embed"]["feature"]))) > 0
    assert out["mean"].shape == (3, 4)


def _identity_buffers(c):
    _, b = meta.materialize(c, jax.random.key(0))
    return b


def test_forward_shapes_and_finite_outputs():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    out = network.apply(p, b, batch(c, b=5), c)
    assert out["mean"].shape == (5, 4) and out["var"].shape == (5, 4)
    assert out["pooled"].shape == (5, 64)
    assert out["tokens"].shape == (5, 8 + 1, 64)
    assert len(out["router"]) == 2
    for k in ("mean", "var", "pooled"):
        assert bool(jnp.all(jnp.isfinite(out[k])))
    assert bool(jnp.all(out["var"] > 0))


def test_a_bounded_head_stays_inside_its_bounds_for_any_raw_value():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    b = {**b, "target_norm": jnp.asarray([[0.0, 1.0], [0.4, 0.2],
                                          [0.0, 1.0], [0.0, 1.0]],
                                         jnp.float32)}
    raw = jnp.asarray([[0.0, -1e4, 0.0, 0.0], [0.0, 1e4, 0.0, 0.0],
                       [0.0, 3.0, 0.0, 0.0]])
    mean = network._bounded(raw, b["target_norm"], c)
    phys = np.asarray(mean[:, 1]) * 0.2 + 0.4
    assert np.all(phys >= 0.0) and np.all(phys <= 1.0)


def test_backward_reaches_every_trainable_tensor_the_forward_uses():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    x = batch(c)
    y = jnp.ones((3, 4))
    from scientific_ai.neural.model.train import loss_terms
    g = jax.grad(lambda pp: loss_terms(pp, b, x, y, c)[0])(p)
    for path, leaf in network.leaves_with_paths(g):
        assert bool(jnp.all(jnp.isfinite(leaf))), path
    nonzero = {network.category_of(path) for path, leaf in
               network.leaves_with_paths(g) if float(jnp.sum(jnp.abs(
                   leaf))) > 0}
    assert {"attention", "router", "expert", "output_head",
            "uncertainty_head", "feature_embedding", "readout_embedding",
            "numerical_encoder", "dimensional_encoder",
            "normalization"} <= nonzero


def test_unused_rows_and_missing_values_receive_no_gradient():
    c = replace(dev(), feature_vocab_size=16)
    p, b = meta.materialize(c, jax.random.key(0))
    x = batch(c, f=8)       # identities 0..7 only
    missing = x["validity"].at[:, 2].set(1)
    x2 = {**x, "validity": missing}
    from scientific_ai.neural.model.train import loss_terms
    g = jax.grad(lambda pp: loss_terms(pp, b, x2, jnp.ones((3, 4)), c)[0])(p)
    assert float(jnp.sum(jnp.abs(g["embed"]["feature"][8:]))) == 0.0
    # a MISSING token's value channels cannot move the output
    o1 = network.apply(p, b, x2, c)["mean"]
    x3 = {**x2, "values": x2["values"].at[:, 2, :].set(123.0)}
    o2 = network.apply(p, b, x3, c)["mean"]
    assert np.array_equal(np.asarray(o1), np.asarray(o2))


def test_seeded_initialisation_is_reproducible_and_order_independent():
    c = dev()
    a, _ = meta.materialize(c, jax.random.key(7))
    b, _ = meta.materialize(c, jax.random.key(7))
    d, _ = meta.materialize(c, jax.random.key(8))
    la, lb, ld = (jax.tree_util.tree_leaves(t) for t in (a, b, d))
    assert all(np.array_equal(np.asarray(x), np.asarray(y))
               for x, y in zip(la, lb))
    assert not all(np.array_equal(np.asarray(x), np.asarray(y))
                   for x, y in zip(la, ld) if x.size > 1)
    k1 = network._key_for(jax.random.key(7), "layers/1/attn/q")
    k2 = network._key_for(jax.random.key(7), "layers/1/attn/q")
    assert np.array_equal(jax.random.key_data(k1), jax.random.key_data(k2))


def test_save_and_reload_reproduce_the_outputs_exactly():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    x = batch(c)
    raw = checkpoint.save(c, p, b)
    p2, b2, _, _ = checkpoint.load(
        c, raw, expected_digest=hashlib.sha256(raw).hexdigest())
    o1 = network.apply(p, b, x, c)
    o2 = network.apply(p2, b2, x, c)
    for k in ("mean", "var", "pooled"):
        assert np.array_equal(np.asarray(o1[k]), np.asarray(o2[k]))


def test_the_input_is_a_set_token_order_does_not_change_the_prediction():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    x = batch(c)
    perm = np.array([3, 0, 7, 1, 5, 2, 6, 4])
    xp = {k: v[:, perm] for k, v in x.items()}
    o1 = network.apply(p, b, x, c)
    o2 = network.apply(p, b, xp, c)
    assert np.allclose(np.asarray(o1["mean"]), np.asarray(o2["mean"]),
                       rtol=1e-5, atol=1e-6)


def test_activation_checkpointing_changes_nothing_computed():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    x = batch(c)
    o1 = network.apply(p, b, x, c)
    o2 = network.apply(p, b, x, c, remat=True)
    assert np.array_equal(np.asarray(o1["mean"]), np.asarray(o2["mean"]))


def test_bfloat16_members_build_and_run_finite():
    c = replace(dev(), precision=PrecisionProfile("bfloat16", "bfloat16"))
    p, b = meta.materialize(c, jax.random.key(0))
    assert p["layers"][0]["moe"]["experts"]["gate"].dtype == jnp.bfloat16
    out = network.apply(p, b, batch(c), c)
    assert bool(jnp.all(jnp.isfinite(out["mean"])))
    assert out["router"][0]["selected"].dtype == jnp.int32


def test_the_same_values_in_another_dimension_are_another_prediction():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    x = batch(c)
    k = {**x, "dim_exponents": x["dim_exponents"].at[:, 0, :].set(
        jnp.asarray([0, 0, 0, 0, 1, 0, 0], jnp.float32))}
    pa = {**x, "dim_exponents": x["dim_exponents"].at[:, 0, :].set(
        jnp.asarray([-1, 1, -2, 0, 0, 0, 0], jnp.float32))}
    o1 = network.apply(p, b, k, c)["mean"]
    o2 = network.apply(p, b, pa, c)["mean"]
    assert not np.allclose(np.asarray(o1), np.asarray(o2))


def test_a_padding_token_cannot_move_the_prediction():
    c = dev()
    p, b = meta.materialize(c, jax.random.key(0))
    x = batch(c)
    mask = jnp.asarray([[True] * 7 + [False]] * 3)
    xm = {**x, "token_mask": mask}
    o1 = network.apply(p, b, xm, c)["mean"]
    x2 = {**xm, "values": xm["values"].at[:, 7, :].set(55.0),
          "dim_exponents": xm["dim_exponents"].at[:, 7, :].set(3.0)}
    o2 = network.apply(p, b, x2, c)["mean"]
    assert np.array_equal(np.asarray(o1), np.asarray(o2))


def test_every_tensor_draws_its_own_values():
    c = dev()
    p, _ = meta.materialize(c, jax.random.key(0))
    a = p["layers"][0]["attn"]
    assert not np.array_equal(np.asarray(a["q"]), np.asarray(a["k"]))
    assert not np.array_equal(np.asarray(p["layers"][0]["attn"]["q"]),
                              np.asarray(p["layers"][1]["attn"]["q"]))

