"""Unit tests for the additive 3D transient layer (core physics invariants).

MODEL-ONLY / FORECAST-ONLY. These are numerical self-consistency tests of the
reduced 3D model; they are not physical validation and introduce no PASS gates.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.config import default_config

ROOT = pathlib.Path(__file__).resolve().parent.parent

CFG = default_config()
CI_RUNTIME_LIMIT_S = 60.0


def test_3d_imports_do_not_break_existing_package():
    """New 3D modules import cleanly and existing modules stay importable."""
    import qta_multiphysics.mesh_3d as _m3
    import qta_multiphysics.laser_source_3d as _l3
    import qta_multiphysics.boundaries_3d as _b3
    import qta_multiphysics.thermal_3d_transient as _t3
    import qta_multiphysics.energy_accounting_3d as _e3
    import qta_multiphysics.reduction_checks_3d as _r3
    # existing canonical backends untouched and importable
    from qta_multiphysics.thermal_1d import solve_thermal_1d
    from qta_multiphysics.thermal_2d_axisymmetric import solve_thermal_2d
    from qta_multiphysics.coupled_mode_solver import run_coupled
    for obj in (_m3, _l3, _b3, _t3, _e3, _r3,
                solve_thermal_1d, solve_thermal_2d, run_coupled):
        assert obj is not None


def test_3d_zero_source_stability():
    """With no source, the field stays exactly at the fridge temperature."""
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d
    r = solve_thermal_3d(CFG, source_scale=0.0, n_eval=7)
    assert r.solver_status == "ok", r.message
    dev = float(np.max(np.abs(r.T - CFG.fridge.T_fridge_K)))
    assert np.isfinite(r.T).all(), "non-finite values in zero-source run"
    assert dev < 1e-9, f"zero-source drift {dev:.3e} K"


def test_3d_deterministic_repeat():
    """Two identical reduced solves are bitwise identical."""
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d
    r1 = solve_thermal_3d(CFG, n_eval=7)
    r2 = solve_thermal_3d(CFG, n_eval=7)
    assert np.array_equal(r1.T, r2.T), "3D solve not bitwise deterministic"
    assert r1.nv_layer_temperature_K() == r2.nv_layer_temperature_K()


def test_3d_symmetry_preserved():
    """A centred Gaussian source on the mirror-symmetric mesh yields a field
    symmetric under x- and y-reflection (to interpolation/solver tolerance)."""
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d
    r = solve_thermal_3d(CFG, n_eval=7)
    T = r.T_xyz(-1)
    relx = np.max(np.abs(T - T[::-1, :, :])) / np.max(T)
    rely = np.max(np.abs(T - T[:, ::-1, :])) / np.max(T)
    assert relx < 1e-6, f"x-mirror asymmetry {relx:.2e}"
    assert rely < 1e-6, f"y-mirror asymmetry {rely:.2e}"


def test_3d_energy_accounting_closes():
    """Source - Kapitza outflow - stored energy closes within the declared tol."""
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d
    from qta_multiphysics.energy_accounting_3d import closure_ok, ENERGY_CLOSURE_TOL
    r = solve_thermal_3d(CFG)
    assert r.solver_status == "ok", r.message
    assert closure_ok(r), (f"energy closure |rel|={abs(r.energy_residual()):.3e} "
                           f">= {ENERGY_CLOSURE_TOL}")


def test_3d_reduced_run_is_ci_safe():
    """The reduced default 3D solve completes well inside the CI budget."""
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d
    t0 = time.time()
    r = solve_thermal_3d(CFG)
    dt = time.time() - t0
    assert r.solver_status == "ok", r.message
    assert dt < CI_RUNTIME_LIMIT_S, f"reduced 3D solve took {dt:.1f}s"


def test_reduction_3d_to_1d():
    """uniform_peak 3D columns reproduce the canonical 1D NV temperature."""
    from qta_multiphysics.reduction_checks_3d import reduction_3d_to_1d, TOL_3D_TO_1D
    c = reduction_3d_to_1d(CFG)
    assert c["solver_status_3d"] == "ok" and c["solver_status_1d"] == "ok"
    assert c["within_tolerance"], (f"3D->1D rel={c['rel_error']:+.4f} "
                                   f"tol={TOL_3D_TO_1D}")


def test_reduction_3d_to_2d_is_boundary_matched_and_reported_honestly():
    """The 3D->2D reduction check must compare the SAME boundary-value problem.

    This test used to assert the two backends agree. That assertion held only
    because the check ran the 2D backend with its production lateral boundary
    -- a cold radial contact to bulk at T_fridge -- against the 3D box's
    adiabatic lateral faces. The 2D lateral heat sink dominated the comparison
    and produced rel = +1.18e-03, which read as excellent agreement between
    two different boundary-value problems.

    Asked the same question (both sides adiabatic), the backends disagree by
    53%. So this test no longer asserts agreement OR disagreement: it asserts
    that the comparison is matched and that whatever comes out is reported
    correctly. It will pass unchanged if the underlying model inconsistency is
    later resolved.
    """
    from qta_multiphysics.reduction_checks_3d import reduction_3d_to_2d, TOL_3D_TO_2D
    c = reduction_3d_to_2d(CFG)
    assert c["solver_status_3d"] == "ok" and c["solver_status_2d"] == "ok"

    # the fixture condition is actually applied on both sides
    assert c["boundary_conditions_matched"] is True
    assert "adiabatic" in c["lateral_bc_3d"]
    assert "adiabatic" in c["lateral_bc_2d_in_this_check"]

    # status follows the measurement, in whichever direction
    within = abs(c["rel_error"]) < TOL_3D_TO_2D
    assert c["within_tolerance"] is within
    assert c["status"] == ("DERIVED_CHECK" if within else "CONDITIONAL")

    # the mismatched comparison is retained for contrast but never as status
    ref = c["mismatched_boundary_reference"]
    assert "NOT a reduction result" in ref["meaning"]
    assert "cold radial contact" in ref["lateral_bc_2d"]
    assert c["status"] != "PASS"


def test_reduction_3d_to_2d_disagreement_is_a_declared_open_item():
    """Pin the currently measured inconsistency so it cannot be forgotten.

    The matched-boundary comparison disagrees by ~53%, and the repository's own
    falsification report flags it. This is a model-consistency finding that
    requires owner authority to resolve; it must not quietly disappear, and it
    must never be resolved by reverting to the mismatched comparison.
    """
    import json
    from qta_multiphysics.reduction_checks_3d import reduction_3d_to_2d
    c = reduction_3d_to_2d(CFG)
    if abs(c["rel_error"]) < 0.10:
        return          # resolved upstream; nothing left to pin
    assert c["status"] == "CONDITIONAL"
    assert abs(c["mismatched_boundary_reference"]["rel_error"]) < 0.10, (
        "the mismatched comparison should still look like agreement -- that "
        "contrast is the evidence that the old number measured boundary "
        "physics rather than dimensional consistency")
    report = json.loads((ROOT / "falsification_report_3d.json").read_text())
    cond = next(x for x in report["conditions"]
                if x["condition"] == "reduction_3d_to_2d_mismatch")
    # the report serializes this flag as a JSON string in some writers
    flagged = cond["falsified_in_model"]
    assert flagged in (True, "true"), (
        f"the falsification report must surface the disagreement; got {flagged!r}")
    assert cond["status"] == "CONDITIONAL"


def test_3d_outputs_carry_forecast_labels_and_no_pass():
    """3D result rows carry the forecast-only label and never a PASS status."""
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d, LABEL
    from qta_multiphysics.energy_accounting_3d import energy_accounting_rows
    r = solve_thermal_3d(CFG, n_eval=7)
    rows = (r.probe_timeseries_rows() + r.hotspot_rows(5)
            + energy_accounting_rows(r))
    for row in rows:
        assert row.get("label") == LABEL or "label" in row, row
        for v in row.values():
            assert str(v) != "PASS", f"PASS status leaked into 3D rows: {row}"


ALL_TESTS = [
    test_3d_imports_do_not_break_existing_package,
    test_3d_zero_source_stability,
    test_3d_deterministic_repeat,
    test_3d_symmetry_preserved,
    test_3d_energy_accounting_closes,
    test_3d_reduced_run_is_ci_safe,
    test_reduction_3d_to_1d,
    test_reduction_3d_to_2d_is_boundary_matched_and_reported_honestly,
    test_reduction_3d_to_2d_disagreement_is_a_declared_open_item,
    test_3d_outputs_carry_forecast_labels_and_no_pass,
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
