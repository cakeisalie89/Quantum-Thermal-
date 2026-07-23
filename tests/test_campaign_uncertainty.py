"""Campaign-uncertainty tests (Stage 3). MODEL-ONLY / FORECAST-ONLY.

Required coverage: fixed-seed two-run byte determinism; parameter
persistence across all phases/cycles (no per-cycle resampling); quantile
ordering and physical bounds; analytic linear-regime benchmark; inventory
growth correlation; excluded-parameter immutability; distribution and
provenance records match authoritative sources; zero-PASS / claim scans;
runtime bound; Stage-2 + single-cycle regression validity.
"""
import json
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np                                                # noqa: E402

from qta_multiphysics.config import default_config                # noqa: E402
from qta_multiphysics.campaign_uncertainty_3d import (            # noqa: E402
    propagate, draw_members, quantile_rows, DISTRIBUTIONS, EXCLUSIONS,
    CAMPAIGN_UNC_SEED, N_MEMBERS, S_CH4_PANEL_LO, S_CH4_PANEL_HI,
    STICK_COV_CH4_CENTER, PURGE_CENTER_1_S)
from qta_multiphysics.cryopanel_dynamics_3d import (              # noqa: E402
    STICKING, N_ML_CAP, phase_fluxes_per_m2_s, phase_windows_s,
    SITES_PER_M2)
from qta_multiphysics.surface_coverage import default_coverage_specs  # noqa: E402

CFG = default_config()
REP = propagate(CFG)


def test_fixed_seed_two_run_byte_determinism():
    a = json.dumps(propagate(CFG), sort_keys=True)
    b = json.dumps(propagate(CFG), sort_keys=True)
    assert a == b
    assert REP["seed"] == CAMPAIGN_UNC_SEED and REP["n_members"] == N_MEMBERS


def test_parameter_persistence_no_percycle_resampling():
    # the witness ratio equals n_cycles up to the exact-solution curvature
    w = REP["persistence_witness"]["per_member_cycle3_over_cycle1_ML"]
    F = phase_fluxes_per_m2_s("MODE_B")["C13_CH4"]
    dt = phase_windows_s(CFG)["MODE_B"]
    curv_bound = 3 * S_CH4_PANEL_HI * F * dt / (N_ML_CAP * SITES_PER_M2)
    for q in ("p05", "p50", "p95"):
        assert abs(float(w[q]) - 3.0) <= 3 * curv_bound, (q, w[q])
    # draws are one dict per member reused verbatim (structure witness)
    m = draw_members(7)
    assert len(m) == 7 and all(len(x) == 3 for x in m)


def test_quantile_ordering_and_bounds():
    for c in REP["cycles"]:
        for qty in ("panel_CH4_ML", "theta_c13_residual_before_D"):
            q = c[qty]
            v = [float(q["p05"]), float(q["p50"]), float(q["p95"])]
            assert v[0] <= v[1] <= v[2]
            assert all(0.0 <= x <= 1.0 for x in v)


def test_linear_regime_analytic_benchmark():
    # ensemble median ML at cycle 1 ~ median(s) * F * dt / Ncap
    F = phase_fluxes_per_m2_s("MODE_B")["C13_CH4"]
    dt = phase_windows_s(CFG)["MODE_B"]
    med_s = float(np.median([m["s_CH4_panel"] for m in draw_members()]))
    analytic = med_s * F * dt / (N_ML_CAP * SITES_PER_M2)
    got = float(REP["cycles"][0]["panel_CH4_ML"]["p50"])
    assert abs(got - analytic) / analytic < 1e-5, (got, analytic)


def test_inventory_growth_correlation_across_cycles():
    c = [float(REP["cycles"][i]["panel_CH4_ML"]["p50"]) for i in range(3)]
    assert c[0] < c[1] < c[2]
    assert abs(c[1] / c[0] - 2.0) < 1e-5 and abs(c[2] / c[0] - 3.0) < 1e-5


def test_excluded_parameters_remain_unchanged():
    assert STICKING["H2"][0] == 0.01 and STICKING["He"][0] == 0.0
    assert N_ML_CAP == 1.0
    names = {e["parameter"] for e in EXCLUSIONS}
    assert {"s_H2_panel", "s_He_panel", "N_ML_cap", "tau_c", "C_contr",
            "vibration_transfer_factors",
            "sensitivity_STEP_10pct"} <= names
    blob = json.dumps(REP)
    assert "tau_c_distribution" not in blob


def test_distribution_provenance_matches_sources():
    d = {x["parameter"]: x for x in DISTRIBUTIONS}
    assert d["s_CH4_panel"]["params"] == {"low": 0.3, "high": 0.8}
    assert "cryopanel_memory_model.csv" in d["s_CH4_panel"]["center_source"]
    spec = [s for s in default_coverage_specs() if s.name == "CH4"][0]
    assert spec.sticking == STICK_COV_CH4_CENTER == \
        d["sticking_coverage_CH4"]["params"]["center"]
    assert "uncertainty.py" in d["sticking_coverage_CH4"]["spread_source"]
    assert d["purge_1_s"]["params"]["center"] == PURGE_CENTER_1_S
    assert "uncertainty.py" in d["purge_1_s"]["spread_source"]
    assert (S_CH4_PANEL_LO, S_CH4_PANEL_HI) == (0.3, 0.8)


def test_marginal_not_joint_labeling():
    mc = REP["marginal_composition"]["thermal"]
    assert "MARGINAL" in mc and "not joint" in mc
    assert "12345" in mc                      # cites the canonical family


def test_zero_pass_and_claim_scan():
    blob = json.dumps([REP, quantile_rows(REP)])
    assert '"PASS"' not in blob
    assert REP["can_PASS_now"] == "NO"
    assert REP["measured_in_this_system"] is False
    assert "never PASS" in REP["cycles"][0]["clearance_context"]


def test_runtime_bound_and_regression_surface():
    t0 = time.time()
    propagate(CFG)
    assert time.time() - t0 < 30.0
    # regression surface: this module imports nothing from the solvers and
    # mutates nothing -- canonical constants still at their values
    from qta_multiphysics.species_accounting_3d import P_C13_WORK_PA
    assert P_C13_WORK_PA == 1.0e-4


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:            # noqa: BLE001
            print(f"  FAIL  {t.__name__}: {e!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)
