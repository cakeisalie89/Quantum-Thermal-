"""NF-1T s.28-30, s.33-34, s.58: the development pipeline end to end.

The samples here are SYNTHETIC test fixtures (the Langmuir closed form
evaluated in NumPy, labelled as such): the committed development dataset is
generated through the governed model, and ``test_neural_evidence.py``
checks it. This module checks the PIPELINE, at a size a test can afford.
"""
from __future__ import annotations

import math
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

from scientific.identity import digest  # noqa: E402
from scientific_ai.neural import datasets as D  # noqa: E402
from scientific_ai.neural import family, manifests  # noqa: E402
from scientific_ai.neural import source_surface_adsorption as SRC  # noqa
from scientific_ai.neural.model import checkpoint, meta, pipeline  # noqa
from scientific_ai.neural.model import train  # noqa: E402

KB = 1.380649e-23
AMU = 1.66053906660e-27


def langmuir(x: dict) -> dict:
    """Closed form of surface.langmuir_capture -- a TEST FIXTURE."""
    m = x["mass_amu"] * AMU
    flux = x["pressure_Pa"] / math.sqrt(2 * math.pi * m * KB * x["T_gas_K"])
    dt = x["window_s"] * x["n_windows"]
    cap = x["capacity_per_m2"]
    n0 = x["initial_coverage"] * cap
    n = cap - (cap - n0) * math.exp(-x["sticking"] * flux * dt / cap)
    return {"admitted_fluence": flux * dt, "final_coverage": n / cap,
            "final_inventory": n, "impingement_flux": flux}


def synthetic(n_in=96, n_ood=16):
    out = []
    for region, n, seed in (("in_distribution", n_in, 1), ("ood", n_ood, 2)):
        for i, x in enumerate(SRC.design(region, n, seed)):
            y = langmuir(x)
            prov = {"input_digest": digest(x), "target_digest": digest(y),
                    "scientific_configuration_digest": digest(x),
                    "validation_status": "NOT_ASSESSED",
                    "generator_version": {"model_id": "SYNTHETIC-FIXTURE",
                                          "model_version": "0",
                                          "implementation_digest": "0" * 64},
                    "note": "synthetic test fixture, not evidence"}
            split = "ood" if region == "ood" else D.assign_split(
                prov["input_digest"], 5)
            out.append(D.Sample(f"{region}/{i:04d}", split, x, y, prov))
    return out


@pytest.fixture(scope="module")
def run():
    samples = synthetic()
    dman, norm = pipeline.dataset_manifest(
        SRC.SCHEMA, samples, [], dataset_id="fixture",
        generation={"note": "synthetic"}, storage={"path": "memory"},
        ood_rule=SRC.in_ood_region)
    tc = train.TrainConfig(steps=40, batch_size=8, warmup_steps=4,
                           checkpoint_interval=20, log_interval=10)
    out = pipeline.run(family.development(), tc, SRC.SCHEMA, samples, dman,
                       norm, source_commit=manifests.unavailable("test"),
                       run_prefix="t", stored_at="memory")
    return samples, dman, norm, tc, out


def test_the_dataset_manifest_is_well_formed_and_leak_free(run):
    _, dman, *_ = run
    assert manifests.problems(dman) == []
    assert dman["leakage"]["passed"]
    assert dman["normalization_statistics"]["fitted_on_split_digest"] == \
        dman["split_digests"]["train"]


def test_every_document_is_well_formed_and_the_chain_holds(run):
    *_, out = run
    d = out["documents"]
    for name, doc in d.items():
        assert manifests.problems(doc) == [], name
    import hashlib
    assert manifests.check_chain(
        dataset=run[1], training=d["training"], checkpoint=d["checkpoint"],
        evaluation=d["evaluation"],
        checkpoint_bytes_digest=hashlib.sha256(
            out["checkpoint_bytes"]).hexdigest()) == []


def test_fresh_runs_and_an_exact_resume_are_bitwise_reproducible(run):
    *_, out = run
    rep = out["documents"]["training"]["reproducibility"]
    ev = rep["evidence"]
    assert ev["fresh_run_tensor_digests_equal"] is True
    assert ev["exact_resume_bitwise_equal_to_uninterrupted"] is True
    assert {"BITWISE_REPRODUCIBLE", "CHECKPOINT_REPRODUCIBLE"} <= set(
        rep["claimed"])
    assert "multi-device or GPU determinism" in rep["not_claimed"]


def test_the_resume_lineage_is_exact_resume_of_an_interrupted_run(run):
    *_, out = run
    a = out["documents"]["training_resume_a"]
    b = out["documents"]["training_resume_b"]
    assert a["completion_state"] == "INTERRUPTED"
    assert a["resume_semantics"] == "FRESH_TRAINING"
    assert b["resume_semantics"] == "EXACT_RESUME"
    assert b["parent_run"] == a["run_id"]
    assert b["parent_checkpoint"] == a["checkpoints"][0]["checkpoint_digest"]
    ca = out["documents"]["checkpoint_resume_a"]
    assert ca["optimizer_state_presence"] is True
    assert ca["data_position"]["step"] == 20


def test_the_checkpoint_reloads_to_identical_outputs(run):
    *_, out = run
    ev = out["documents"]["evaluation"]
    assert ev["reload"] == {**ev["reload"], "digest_verified": True,
                            "outputs_equal": True}


def test_the_evaluation_reports_every_split_target_and_its_semantics(run):
    *_, out = run
    ev = out["documents"]["evaluation"]
    assert set(ev["per_target"]) == {"train", "validation", "test", "ood"}
    for split, per in ev["per_target"].items():
        assert set(per) == set(SRC.SCHEMA.target_names)
        for t, m in per.items():
            assert set(m["interval_coverage"]) == {"0.5", "0.8", "0.9",
                                                   "0.95"}
            assert math.isfinite(m["rmse"]) and m["n"] > 0
    assert ev["semantics"] == ["LEARNED_PREDICTION", "NON_AUTHORITATIVE",
                               "REQUIRES_EXTERNAL_VERIFICATION"]
    assert ev["calibration"]["epistemic"].startswith("NOT_ASSESSED")
    assert set(ev["ood"]["rmse_ratio_ood_over_test"]) == set(
        SRC.SCHEMA.target_names)
    assert ev["ood"]["input_classes"]["ood"].get("EXTRAPOLATION", 0) == 16
    assert set(ev["constraints"]) == {"test", "ood"}
    for layer in ev["router"]:
        assert layer["indices_in_range"] and layer["indices_unique_per_token"]
        assert layer["selected_per_token"] == 2 and layer["router_finite"]


def test_the_training_manifest_records_what_the_directive_lists(run):
    *_, out = run
    tr = out["documents"]["training"]
    assert tr["model_configuration_digest"] == family.development().digest()
    assert tr["parameter_count"] == 431_784
    assert tr["initialization_seed"] == 0 and tr["data_order_seed"] == 1
    assert tr["world_size"] == 1 and tr["gradient_accumulation"] == 1
    assert manifests.is_unavailable(tr["source_commit"])
    assert "Gaussian negative" in tr["loss_definition"]
    assert tr["framework_versions"]["jax"] == jax.__version__
    assert "host" not in tr["hardware_identity"]["cpu"]
    assert tr["loss_history"] and tr["router_health"]["moe"]


def test_a_non_finite_step_stops_the_run_and_is_never_completed():
    samples = synthetic(48, 0)
    norm = D.fit_normalization(SRC.SCHEMA, samples)
    tr = [s for s in samples if s.split == "train"]
    from scientific_ai.neural.features import encode_targets, tokenize
    tok = tokenize(SRC.SCHEMA, norm, [s.inputs for s in tr])
    y = encode_targets(SRC.SCHEMA, norm, [s.targets for s in tr])
    cfg = family.development()
    p, b = meta.materialize(cfg, jax.random.key(0))
    p = jax.tree_util.tree_map(lambda x: x, p)
    p["layers"][0]["moe"]["router"] = p["layers"][0]["moe"]["router"].at[
        0, 0].set(jnp.nan)
    tc = train.TrainConfig(steps=5, batch_size=8, warmup_steps=1)
    with pytest.raises(train.TrainingAborted, match="step 0"):
        train.run(cfg, tc, tok, y, params=p, buffers=b)


def test_a_resume_needs_its_optimizer_state_and_its_step():
    cfg = family.development()
    p, b = meta.materialize(cfg, jax.random.key(0))
    tc = train.TrainConfig(steps=5, batch_size=8, warmup_steps=1)
    tok = {"values": np.zeros((8, 8, 4), np.float32)}
    with pytest.raises(ValueError, match="optimizer state"):
        train.run(cfg, tc, tok, np.zeros((8, 4)), params=p, buffers=b,
                  start_step=2)
    opt = train.init_optimizer(p)
    with pytest.raises(ValueError, match="not the start step"):
        train.run(cfg, tc, tok, np.zeros((8, 4)), params=p, buffers=b,
                  opt=opt, start_step=2)


def test_the_data_order_is_a_function_of_seed_and_epoch():
    tc = train.TrainConfig(steps=10, batch_size=4, warmup_steps=1)
    a = train.batch_order(40, tc, 3)
    assert np.array_equal(a, train.batch_order(40, tc, 3))
    assert not np.array_equal(a, train.batch_order(40, tc, 4))
    pos = train.position(25, 40, tc)
    assert pos == {"step": 25, "epoch": 2, "batch_in_epoch": 5,
                   "batches_per_epoch": 10}


@pytest.mark.parametrize("kw", [{"steps": 0}, {"batch_size": 10,
                                               "gradient_accumulation": 3},
                                {"warmup_steps": 10, "steps": 10},
                                {"learning_rate": 0.0},
                                {"beta1": 1.0}])
def test_an_invalid_training_configuration_is_refused(kw):
    with pytest.raises(ValueError):
        replace(train.TrainConfig(), **kw).validate()


def test_the_first_adam_step_is_bias_corrected():
    cfg = family.development()
    p, b = meta.materialize(cfg, jax.random.key(0))
    samples = synthetic(48, 0)
    norm = D.fit_normalization(SRC.SCHEMA, samples)
    from scientific_ai.neural.features import encode_targets, tokenize
    tr = [s for s in samples if s.split == "train"][:8]
    tok = {k: jnp.asarray(v) for k, v in tokenize(
        SRC.SCHEMA, norm, [s.inputs for s in tr]).items()}
    y = jnp.asarray(encode_targets(SRC.SCHEMA, norm,
                                   [s.targets for s in tr]), jnp.float32)
    tc = train.TrainConfig(steps=10, batch_size=8, warmup_steps=1,
                           learning_rate=1e-3, clip_norm=1e9)
    p1, opt, m = train.make_step(cfg, tc)(p, train.init_optimizer(p), b,
                                          tok, y)
    # after one bias-corrected step every update is lr * g/(|g|+eps):
    # the largest moved by ~lr, none by more
    moves = [float(jnp.max(jnp.abs(a.astype(jnp.float32)
                                   - c.astype(jnp.float32))))
             for a, c in zip(jax.tree_util.tree_leaves(p1),
                             jax.tree_util.tree_leaves(p))]
    assert max(moves) <= 1e-3 * 1.0001 and max(moves) > 0.9e-3
    assert opt["step"] == 1


def test_accumulated_gradients_equal_the_full_batch_for_a_mean_loss():
    cfg = family.development()
    cfg0 = replace(cfg, moe=replace(cfg.moe, aux_loss_coef=0.0))
    p, b = meta.materialize(cfg0, jax.random.key(0))
    samples = synthetic(48, 0)
    norm = D.fit_normalization(SRC.SCHEMA, samples)
    from scientific_ai.neural.features import encode_targets, tokenize
    tr = [s for s in samples if s.split == "train"][:16]
    tok = {k: jnp.asarray(v) for k, v in tokenize(
        SRC.SCHEMA, norm, [s.inputs for s in tr]).items()}
    y = jnp.asarray(encode_targets(SRC.SCHEMA, norm,
                                   [s.targets for s in tr]), jnp.float32)
    g1 = train.accumulated_gradient(cfg0, p, b, tok, y, 1)[4]
    g4 = train.accumulated_gradient(cfg0, p, b, tok, y, 4)[4]
    for a, c in zip(jax.tree_util.tree_leaves(g1),
                    jax.tree_util.tree_leaves(g4)):
        assert np.allclose(np.asarray(a), np.asarray(c), rtol=1e-4,
                           atol=1e-7)
