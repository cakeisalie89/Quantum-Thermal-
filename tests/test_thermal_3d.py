"""Unit tests for the additive 3D transient layer (core physics invariants).

MODEL-ONLY / FORECAST-ONLY. These are numerical self-consistency tests of the
reduced 3D model; they are not physical validation and introduce no PASS gates.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.config import default_config

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


def test_reduction_3d_to_2d():
    """Gaussian-beam 3D agrees with the 2D axisymmetric backend on-axis."""
    from qta_multiphysics.reduction_checks_3d import reduction_3d_to_2d, TOL_3D_TO_2D
    c = reduction_3d_to_2d(CFG)
    assert c["solver_status_3d"] == "ok" and c["solver_status_2d"] == "ok"
    assert c["within_tolerance"], (f"3D->2D rel={c['rel_error']:+.4f} "
                                   f"tol={TOL_3D_TO_2D}")


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
    test_reduction_3d_to_2d,
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
