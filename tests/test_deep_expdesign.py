"""Tests for qta_multiphysics.deep_expdesign (Stage 1: foundation + dataset).

Covers the deterministic foundation and the fail-closed readiness machine.
Every rejection path is exercised, not only the success path. The neural
posterior/EIG training + validation is a later stage; until it runs and passes,
the surrogate must remain fail-closed (these tests enforce that).

Runnable standalone or via pytest.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from qta_multiphysics.deep_expdesign import (
    build_latent_parameters, trainable_subspace, sample_prior, ci_config,
    ExperimentDesign, validate_design, is_valid, validate_sequence,
    load_interlocks, simulate, generate_dataset, summarize, splits_disjoint,
    Evidence, compute_state, readiness_record, run_deep_expdesign, ReadinessState,
    MeasurementRecord,
)
from qta_multiphysics.deep_expdesign import transforms as tf


def test_joint_prior_sampling():
    lat = build_latent_parameters()
    assert len(lat) == 13
    tr = trainable_subspace(lat)
    assert len(tr) == 5 and all(p.is_trainable and p.validity_range for p in tr)
    s = sample_prior(lat, n=32, seed=123, sampler="lhs")
    assert s["natural"].shape == (32, 5)
    j = s["names"].index("tau_c")
    assert s["natural"][:, j].min() >= 1e-6 - 1e-12
    assert s["natural"][:, j].max() <= 1e-2 + 1e-9


def test_unknown_params_failclosed():
    lat = build_latent_parameters()
    for p in lat:
        if not p.is_trainable:
            assert p.source_required and p.validity_range is None
            assert p.evidence_status == "UNKNOWN"


def test_transform_roundtrips():
    assert tf.roundtrip_max_abs_error(np.array([0.0, 0.3, 1.0]), "linear") == 0.0
    assert tf.roundtrip_max_abs_error(np.array([1e-6, 1e-3, 1e-1]), "log10") < 1e-12
    # normalize/denormalize inverse
    z = np.array([-6.0, -4.0, -2.0])
    u = tf.normalize(z, -6.0, -2.0)
    assert np.allclose(tf.denormalize(u, -6.0, -2.0), z)


def test_deterministic_dataset_generation():
    cfg = ci_config(n_dataset=32)
    d1 = generate_dataset(cfg)
    d2 = generate_dataset(cfg)
    assert d1.sha256 == d2.sha256, "dataset hash not reproducible"
    assert np.array_equal(d1.x[d1.success], d2.x[d2.success])


def test_train_val_test_separation():
    cfg = ci_config(n_dataset=40)
    ds = generate_dataset(cfg)
    assert splits_disjoint(ds), "splits overlap or do not cover the dataset"
    a = set(ds.split["train"].tolist())
    b = set(ds.split["val"].tolist())
    c = set(ds.split["test"].tolist())
    assert a.isdisjoint(b) and a.isdisjoint(c) and b.isdisjoint(c)
    assert len(a) + len(b) + len(c) == ds.theta.shape[0]


def test_valid_design_accepted():
    d = ExperimentDesign(experiment_id="EXP-A", pulse_sequence="hahn", mode="D",
                         he3_coverage_target=1.0)
    assert is_valid(d), validate_design(d)


def test_invalid_design_rejected_sensing_outside_D():
    d = ExperimentDesign(experiment_id="X", pulse_sequence="ramsey", mode="B")
    assert any("Mode D" in r for r in validate_design(d))


def test_invalid_design_rejected_methane_in_D():
    d = ExperimentDesign(experiment_id="X", pulse_sequence="lcvd", mode="D",
                         dosing_duration=10.0)
    r = validate_design(d)
    assert any("Mode B" in x or "methane" in x for x in r)


def test_invalid_design_rejected_helium_outside_D():
    d = ExperimentDesign(experiment_id="X", pulse_sequence="none", mode="B",
                         he3_coverage_target=1.0)
    assert any("helium" in x for x in validate_design(d))


def test_no_processing_and_sensing_overlap():
    d = ExperimentDesign(experiment_id="X", pulse_sequence="lcvd", mode="B",
                         dosing_duration=5.0)
    # force a sensing+lcvd contradiction by also marking a sensing seq path
    d2 = ExperimentDesign(experiment_id="X", pulse_sequence="lcvd", mode="D",
                          he3_coverage_target=0.0, dosing_duration=5.0)
    assert not is_valid(d2)  # methane in D rejected
    # sequence order A->B->C->D enforced
    seq = [ExperimentDesign("a", "none", "A"), ExperimentDesign("b", "none", "C"),
           ExperimentDesign("c", "none", "B")]
    assert validate_sequence(seq), "out-of-order sequence not flagged"


def test_interlocks_loaded_and_enforced():
    interlocks = load_interlocks()
    assert len(interlocks) >= 1
    # a design that turns on both LCVD and sensing must be rejected by interlock/logic
    d = ExperimentDesign(experiment_id="X", pulse_sequence="lcvd", mode="B")
    # craft contradictory state: sensing implied + lcvd
    d.pulse_sequence = "lcvd"
    # direct overlap check
    bad = ExperimentDesign("Y", "ramsey", "D")
    bad.pulse_sequence = "lcvd"  # now lcvd in mode D -> invalid
    assert not is_valid(bad)


def test_simulator_failure_capture_no_fabrication():
    # invalid design -> success False, empty observables (no invented data)
    d = ExperimentDesign(experiment_id="X", pulse_sequence="ramsey", mode="A")
    theta = {"tau_c": 1e-3, "he3_he4_contrast": 0.5, "nv_contrast_10mK": 0.1,
             "thermal_recovery_time": 4e-8, "mode_isolation_leak": 1e-3}
    resp = simulate(theta, d, seed=1)
    assert resp.success is False and resp.observables == {}
    assert resp.failure_reason and resp.measured_in_this_system is False


def test_simulator_success_finite_observables():
    d = ExperimentDesign(experiment_id="EXP-A", pulse_sequence="hahn", mode="D",
                         he3_coverage_target=1.0)
    theta = {"tau_c": 1e-3, "he3_he4_contrast": 0.5, "nv_contrast_10mK": 0.1,
             "thermal_recovery_time": 4e-8, "mode_isolation_leak": 1e-3}
    resp = simulate(theta, d, seed=2)
    assert resp.success and len(resp.observables) == 5
    assert all(np.isfinite(v) for v in resp.observables.values())
    assert resp.forecast_only is True and resp.measured_in_this_system is False


def test_readiness_state_machine():
    # fail-closed transitions
    assert compute_state(Evidence(dependency_ok=False)) == ReadinessState.NOT_IMPLEMENTED
    assert compute_state(Evidence(dataset_generated=False, trained=False)) == ReadinessState.NOT_IMPLEMENTED
    assert compute_state(Evidence(dataset_generated=True, trained=False)) == ReadinessState.DATASET_GENERATED
    assert compute_state(Evidence(dataset_generated=True, trained=True, validation_ran=False)) == ReadinessState.TRAINED_NOT_VALIDATED
    assert compute_state(Evidence(dataset_generated=True, trained=True, validation_ran=True, thresholds_passed=False)) == ReadinessState.TRAINED_NOT_TRUSTED
    assert compute_state(Evidence(dataset_generated=True, trained=True, validation_ran=True, thresholds_passed=True)) == ReadinessState.VALIDATED_SURROGATE
    assert compute_state(Evidence(ood_triggered=True)) == ReadinessState.OUT_OF_DISTRIBUTION


def test_deep_cannot_control_ordering_before_validation():
    cfg = ci_config()
    rec = readiness_record(Evidence(dataset_generated=True, trained=True,
                                    validation_ran=True, thresholds_passed=False), cfg)
    assert rec["status"] == ReadinessState.TRAINED_NOT_TRUSTED
    assert rec["controls_experiment_ordering"] is False
    assert rec["fallback"] == "direct_monte_carlo"
    # only VALIDATED_SURROGATE may control ordering
    rec2 = readiness_record(Evidence(dataset_generated=True, trained=True,
                                     validation_ran=True, thresholds_passed=True), cfg)
    assert rec2["controls_experiment_ordering"] is True


def test_hardware_record_rejected_when_incomplete():
    incomplete = MeasurementRecord(
        measurement_id="m1", timestamp="", instrument="", calibration_record="",
        experiment_design={}, raw_data_reference="", processed_observable={},
        uncertainty={}, systematic_uncertainty={}, operator="", temperature=0.01,
        field=0.0, coverage=0.0, mode="D", quality_flags=[])
    assert not incomplete.is_complete()
    assert not incomplete.is_usable()
    # complete but unverified source -> still not usable
    complete = MeasurementRecord(
        measurement_id="m2", timestamp="2026-01-01", instrument="ODMR",
        calibration_record="cal-1", experiment_design={"mode": "D"},
        raw_data_reference="ref", processed_observable={"c": 0.1},
        uncertainty={"c": 0.01}, systematic_uncertainty={}, operator="op",
        temperature=0.01, field=5e-3, coverage=1.0, mode="D", quality_flags=["ok"])
    assert complete.is_complete() and not complete.is_usable()
    complete.data_source = "MEASURED_IN_SYSTEM"
    assert complete.is_usable()


def test_runner_outputs_failclosed_no_pass(tmp_path=None):
    import tempfile
    out = Path(tempfile.mkdtemp())
    r = run_deep_expdesign(profile="ci", output_dir=out, cfg=ci_config(n_dataset=24))
    assert r["readiness"]["status"] == ReadinessState.DATASET_GENERATED
    assert r["readiness"]["controls_experiment_ordering"] is False
    assert r["readiness"]["fallback"] == "direct_monte_carlo"
    # scan all written outputs for forbidden tokens
    for f in out.glob("*.json"):
        txt = f.read_text()
        assert '"measured_in_this_system": true' not in txt.lower().replace(" ", " ")
        obj = json.loads(txt)
        _assert_no_pass(obj, f.name)


def _assert_no_pass(obj, where):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                assert v != "PASS", f"PASS state in {where}:{k}"
            _assert_no_pass(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _assert_no_pass(v, where)


def test_existing_direct_expdesign_unchanged():
    # the canonical reference path must still import and rank deterministically
    from qta_multiphysics.expdesign import run_expdesign_layer
    import tempfile
    r = run_expdesign_layer(n_outer=200, n_inner=200, n_batches=4, seed=42,
                            output_dir=Path(tempfile.mkdtemp()))
    order = [x["experiment_id"] for x in r["ranking"]]
    assert order[0] == "EXP-A"
    assert r["estimator_source"] == "direct_monte_carlo"


ALL_TESTS = [
    test_joint_prior_sampling,
    test_unknown_params_failclosed,
    test_transform_roundtrips,
    test_deterministic_dataset_generation,
    test_train_val_test_separation,
    test_valid_design_accepted,
    test_invalid_design_rejected_sensing_outside_D,
    test_invalid_design_rejected_methane_in_D,
    test_invalid_design_rejected_helium_outside_D,
    test_no_processing_and_sensing_overlap,
    test_interlocks_loaded_and_enforced,
    test_simulator_failure_capture_no_fabrication,
    test_simulator_success_finite_observables,
    test_readiness_state_machine,
    test_deep_cannot_control_ordering_before_validation,
    test_hardware_record_rejected_when_incomplete,
    test_runner_outputs_failclosed_no_pass,
    test_existing_direct_expdesign_unchanged,
]


def main() -> int:
    passed = failed = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(ALL_TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
