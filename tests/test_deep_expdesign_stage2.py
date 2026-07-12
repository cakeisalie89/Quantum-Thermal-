"""Stage-2 tests for the deep layer: training, posterior, calibration, OOD,
direct-MC validation, and fail-closed trust gating.

Uses tiny deterministic configs so the suite is fast on CPU. These tests check
structure, determinism, and the fail-closed contract -- NOT surrogate accuracy
(on tiny CI data the honest outcome is TRAINED_NOT_TRUSTED, which is asserted).
Runnable standalone or via pytest.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from qta_multiphysics.deep_expdesign import (
    DeepConfig, TrustThresholds, train_posterior, posterior_query, run_sbc,
    fit_ood, ood_report, direct_mc_eig, learned_eig, validate_against_direct_mc,
    benchmark_designs, rank_candidates, Candidate, Evidence, readiness_record,
    compute_state, ReadinessState, run_deep_expdesign_full, likelihood_model as lm,
)


def _tiny_cfg(**ov):
    base = dict(profile="ci", n_dataset=48, train_steps=120, n_mixture=2,
                hidden_units=16, eig_ref_outer=40, eig_ref_inner=40,
                eig_ref_batches=2, n_benchmark_designs=4, n_repeat_seeds=2)
    base.update(ov)
    return DeepConfig(**base).validate()


_TP_CACHE = {}
def _trained(cfg=None):
    cfg = cfg or _tiny_cfg()
    key = (cfg.n_dataset, cfg.train_steps, cfg.n_mixture, cfg.hidden_units)
    if key not in _TP_CACHE:
        _TP_CACHE[key] = train_posterior(cfg)
    return _TP_CACHE[key]


def test_training_runs_and_reduces_nll():
    tp = _trained()
    assert tp.history["nll"][-1] < tp.history["nll"][0]
    assert np.isfinite(tp.history["nll"][-1])


def test_training_deterministic():
    cfg = _tiny_cfg()
    a = train_posterior(cfg); b = train_posterior(cfg)
    assert np.allclose(a.history["nll"], b.history["nll"])


def test_posterior_query_shapes_finite():
    tp = _trained()
    bench = benchmark_designs(4)
    names, transf = tp.dataset.names, tp.dataset.transforms
    lo, hi = tp.prior_lo, tp.prior_hi
    x = lm.forward_matrix((lo + 0.5 * (hi - lo))[None, :], bench[0], names, transf)[0]
    post = posterior_query(tp, x, bench[0], n_samples=300, seed=0)
    assert len(post["posterior_mean_natural"]) == len(names)
    assert all(np.isfinite(post["posterior_mean_natural"]))
    assert post["measured_in_this_system"] is False


def test_calibration_coverage_in_range():
    tp = _trained()
    cal = run_sbc(tp, seed=0)
    for lv in cal.levels:
        assert 0.0 <= cal.empirical_coverage[lv] <= 1.0
    assert np.isfinite(cal.max_coverage_error)


def test_ood_detects_far_points():
    tp = _trained()
    om = fit_ood(tp.context_train, quantile=0.99, maha_factor=1.5)
    far = np.tile(om.hi * 5 + 10, (8, 1))
    s_far = om.score(far)
    s_train = om.score(tp.context_train)
    assert s_far["is_ood"].all(), "far points must be flagged OOD"
    assert s_train["is_ood"].mean() < 0.5, "most training points should be in-distribution"


def test_direct_mc_eig_finite_positive():
    tp = _trained()
    names, transf = tp.dataset.names, tp.dataset.transforms
    lo, hi = tp.prior_lo, tp.prior_hi
    bench = benchmark_designs(4)
    m, se = direct_mc_eig(bench[0], names, transf, lo, hi, n_outer=60, n_inner=60,
                          n_batches=2, seed=1)
    assert np.isfinite(m) and np.isfinite(se) and m > 0.0


def test_learned_eig_finite():
    tp = _trained()
    e = learned_eig(tp, benchmark_designs(4)[0], n_samples=60, seed=1)
    assert np.isfinite(e)


def test_validation_runs_and_gates():
    tp = _trained()
    vr = validate_against_direct_mc(tp, benchmark_designs(4), _tiny_cfg(), seed=0)
    assert isinstance(vr.thresholds_passed, bool)
    assert set(vr.per_threshold.keys()) >= {"spearman", "top1_agreement",
                                            "mean_abs_eig_error_nats"}
    for k, (val, thr, ok) in vr.per_threshold.items():
        assert isinstance(ok, (bool, np.bool_))


def test_validation_repeat_seed_deterministic():
    tp = _trained()
    bench = benchmark_designs(4)
    a = validate_against_direct_mc(tp, bench, _tiny_cfg(), seed=0)
    b = validate_against_direct_mc(tp, bench, _tiny_cfg(), seed=0)
    assert np.allclose(a.eig_deep, b.eig_deep)
    assert np.allclose(a.eig_direct, b.eig_direct)


def test_trust_gating_failclosed():
    cfg = _tiny_cfg()
    # failing validation -> TRAINED_NOT_TRUSTED, cannot control ordering
    ev = Evidence(dataset_generated=True, trained=True, validation_ran=True,
                  thresholds_passed=False)
    rec = readiness_record(ev, cfg)
    assert rec["status"] == ReadinessState.TRAINED_NOT_TRUSTED
    assert rec["controls_experiment_ordering"] is False
    assert rec["fallback"] == "direct_monte_carlo"
    # only a fully-passing model may control ordering
    ev2 = Evidence(dataset_generated=True, trained=True, validation_ran=True,
                   thresholds_passed=True)
    assert readiness_record(ev2, cfg)["controls_experiment_ordering"] is True


def test_policy_uses_direct_mc_when_untrusted():
    cands = [Candidate(f"C{i}", eig=float(i)) for i in range(4)]
    pol = rank_candidates(cands, eig_source="direct_monte_carlo", controls_ordering=False)
    assert pol["eig_source"] == "direct_monte_carlo"
    assert pol["controls_experiment_ordering"] is False
    assert "explanation" in pol and pol["selected"] in [c.experiment_id for c in cands]


def test_full_runner_failclosed_no_pass():
    out = Path(tempfile.mkdtemp())
    r = run_deep_expdesign_full(profile="ci", output_dir=out, cfg=_tiny_cfg())
    rd = r["readiness"]
    assert rd["status"] in (ReadinessState.TRAINED_NOT_TRUSTED,
                            ReadinessState.VALIDATED_SURROGATE)
    # on tiny CI data, must NOT be a trusted surrogate
    assert rd["status"] == ReadinessState.TRAINED_NOT_TRUSTED
    assert rd["controls_experiment_ordering"] is False
    # required outputs present
    for nm in ["deep_training_history.csv", "deep_model_manifest.json",
               "deep_posterior_summary.json", "deep_posterior_samples.csv",
               "deep_posterior_calibration.csv", "deep_eig_validation.csv",
               "deep_ranking_comparison.csv", "deep_ood_report.json",
               "deep_adaptive_policy.json", "deep_surrogate_readiness.json",
               "deep_expdesign_summary.json"]:
        assert (out / nm).exists(), f"missing output {nm}"
    # no PASS / no measured-in-system across JSON outputs
    for f in out.glob("*.json"):
        obj = json.loads(f.read_text())
        _no_pass(obj, f.name)
        _no_measured(obj, f.name)


def _no_pass(o, where):
    if isinstance(o, dict):
        for k, v in o.items():
            assert v != "PASS", f"PASS in {where}:{k}"
            _no_pass(v, where)
    elif isinstance(o, list):
        for v in o:
            _no_pass(v, where)


def _no_measured(o, where):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "measured_in_this_system":
                assert v is False, f"measured_in_this_system true in {where}"
            _no_measured(v, where)
    elif isinstance(o, list):
        for v in o:
            _no_measured(v, where)


ALL_TESTS = [
    test_training_runs_and_reduces_nll,
    test_training_deterministic,
    test_posterior_query_shapes_finite,
    test_calibration_coverage_in_range,
    test_ood_detects_far_points,
    test_direct_mc_eig_finite_positive,
    test_learned_eig_finite,
    test_validation_runs_and_gates,
    test_validation_repeat_seed_deterministic,
    test_trust_gating_failclosed,
    test_policy_uses_direct_mc_when_untrusted,
    test_full_runner_failclosed_no_pass,
]


def main() -> int:
    passed = failed = 0
    for t in ALL_TESTS:
        try:
            t(); print(f"  PASS  {t.__name__}"); passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(ALL_TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
