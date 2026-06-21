"""Tests for qta_multiphysics.expdesign (direct Bayesian experimental design)."""
from __future__ import annotations

import hashlib
import math
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from qta_multiphysics.expdesign import (
    load_candidates, eig_monte_carlo, compute_all_eig, rank_experiments,
    adaptive_policy, DeepEIGSurrogate, select_estimator_source, run_expdesign_layer,
)
from qta_multiphysics.expdesign.model import ParameterModel


def test_candidates_parsed():
    cands = load_candidates()
    ids = {c.experiment_id for c in cands}
    assert ids == {"EXP-A", "EXP-B", "EXP-C", "EXP-D", "EXP-E", "EXP-F"}, ids
    a = next(c for c in cands if c.experiment_id == "EXP-A")
    assert "D13" in a.gates_affected and "D12_G23" in a.gates_affected, a.gates_affected
    # tau_c experiment carries a falsification threshold (kill if too low)
    c_exp = next(c for c in cands if c.experiment_id == "EXP-C")
    assert c_exp.parameter.name == "tau_c"
    assert c_exp.parameter.falsify_below_transformed is not None


def test_eig_positive_with_uncertainty():
    cands = load_candidates()
    eigs = compute_all_eig(cands, n_outer=400, n_inner=400, seed=42, n_batches=6)
    for eid, e in eigs.items():
        assert e["eig_nats"] > 0, f"{eid} EIG not positive"
        assert e["eig_se"] >= 0 and not math.isnan(e["eig_se"]), f"{eid} SE missing"


def test_eig_matches_analytic_limit():
    # Uniform[0,W] prior, Gaussian sigma, W/sigma large:
    #   EIG -> log(W/sigma) - 0.5*log(2*pi*e)
    pm = ParameterModel("ref", "linear", 0.0, 10.0, 0.1, "u", "UNKNOWN")
    e = eig_monte_carlo(pm, n_outer=4000, n_inner=4000, seed=7, n_batches=8)
    analytic = math.log(10.0 / 0.1) - 0.5 * math.log(2 * math.pi * math.e)
    assert abs(e["eig_nats"] - analytic) < 0.05, (e["eig_nats"], analytic)


def test_eig_monotonic_in_precision():
    # smaller measurement sigma -> more information
    coarse = eig_monte_carlo(ParameterModel("p", "linear", 0, 1, 0.20, "u", "UNKNOWN"),
                             n_outer=2000, n_inner=2000, seed=3, n_batches=6)
    fine = eig_monte_carlo(ParameterModel("p", "linear", 0, 1, 0.05, "u", "UNKNOWN"),
                           n_outer=2000, n_inner=2000, seed=3, n_batches=6)
    assert fine["eig_nats"] > coarse["eig_nats"]


def test_eig_reproducible():
    pm = ParameterModel("p", "log10", -6, -2, 0.15, "s", "UNKNOWN")
    a = eig_monte_carlo(pm, n_outer=500, n_inner=500, seed=11, n_batches=4)
    b = eig_monte_carlo(pm, n_outer=500, n_inner=500, seed=11, n_batches=4)
    assert a["eig_nats"] == b["eig_nats"], "EIG not reproducible for fixed seed"


def test_ranking_is_blended_and_deterministic():
    cands = load_candidates()
    eigs = compute_all_eig(cands, n_outer=400, n_inner=400, seed=42, n_batches=6)
    r1 = rank_experiments(cands, eigs)
    r2 = rank_experiments(cands, eigs)
    assert [r["experiment_id"] for r in r1] == [r["experiment_id"] for r in r2]
    # every score is benefit/penalty with all components present
    for r in r1:
        assert {"eig_component", "criticality_component", "falsification_component",
                "dependency_component", "cost_penalty", "score"} <= set(r)
    # ranking favours a high-criticality experiment at the top (not pure gate count)
    by_id = {c.experiment_id: c for c in cands}
    crits = sorted(c.criticality for c in cands)
    top = by_id[r1[0]["experiment_id"]]
    assert top.criticality >= crits[len(crits) // 2], "top experiment not criticality-driven"


def test_adaptive_policy_outcome_dependent():
    cands = load_candidates()
    eigs = compute_all_eig(cands, n_outer=400, n_inner=400, seed=42, n_batches=6)
    ranking = rank_experiments(cands, eigs)
    pol = adaptive_policy(cands, eigs, ranking)
    assert "advance" in pol["outcomes"]
    # the top experiment has a falsify threshold -> a 'kill' branch exists, and the
    # recommended next experiment is allowed to change between outcomes
    if "kill" in pol["outcomes"]:
        nxts = {o["recommended_next_experiment"] for o in pol["outcomes"].values()}
        assert pol["next_changes_with_outcome"] == (len(nxts) > 1)


def test_deep_surrogate_not_implemented_and_fallback():
    s = DeepEIGSurrogate()
    assert s.status == "NOT_IMPLEMENTED"
    assert s.is_trustworthy() is False
    assert select_estimator_source(s) == "direct_monte_carlo"
    assert select_estimator_source(None) == "direct_monte_carlo"
    for meth in (s.train, s.predict_eig):
        raised = False
        try:
            meth()
        except NotImplementedError:
            raised = True
        assert raised
    rd = s.readiness()
    assert rd["status"] == "NOT_IMPLEMENTED" and rd["controls_experiment_ordering"] is False


def test_full_run_reproducible_and_outputs_deterministic():
    r1 = run_expdesign_layer(n_outer=300, n_inner=300, n_batches=4, seed=42,
                             write_outputs=False)
    r2 = run_expdesign_layer(n_outer=300, n_inner=300, n_batches=4, seed=42,
                             write_outputs=False)
    assert ([x["experiment_id"] for x in r1["ranking"]]
            == [x["experiment_id"] for x in r2["ranking"]])
    for eid in r1["eigs"]:
        assert r1["eigs"][eid]["eig_nats"] == r2["eigs"][eid]["eig_nats"]

    files = ["experimental_design_candidates.csv", "expected_information_gain.csv",
             "validation_experiment_ranking.csv", "adaptive_experiment_policy.json",
             "bayesian_design_summary.json", "deep_surrogate_readiness.json",
             "experiment_falsification_map.csv"]

    def run_to(d):
        run_expdesign_layer(n_outer=300, n_inner=300, n_batches=4, seed=42,
                            output_dir=Path(d))
        return {f: hashlib.sha256((Path(d) / f).read_bytes()).hexdigest() for f in files}

    h1 = run_to("/tmp/exprun1")
    h2 = run_to("/tmp/exprun2")
    for f in files:
        assert h1[f] == h2[f], f"{f} not byte-identical across runs"


ALL_TESTS = [
    test_candidates_parsed,
    test_eig_positive_with_uncertainty,
    test_eig_matches_analytic_limit,
    test_eig_monotonic_in_precision,
    test_eig_reproducible,
    test_ranking_is_blended_and_deterministic,
    test_adaptive_policy_outcome_dependent,
    test_deep_surrogate_not_implemented_and_fallback,
    test_full_run_reproducible_and_outputs_deterministic,
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
