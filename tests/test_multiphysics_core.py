"""Unit tests for the core non-lumped multiphysics layer.

These tests wrap the package's own numerical verification checks
(qta_multiphysics.verification) as fast, individually-failing unit tests, so the
scientifically central PDE backends are covered by the test suite directly and
not only via the full pipeline run. Every threshold asserted here is the SAME
threshold the verification suite itself declares; no new acceptance criteria are
introduced.

All checks are numerical self-consistency tests of the models (MODEL_ONLY).
They are not physical validation against hardware.

The expensive 2D mesh-convergence sweep is intentionally not duplicated here; it
remains exercised by the full pipeline (qta_full_sim.py / run_verification).
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.config import default_config, SolverConfig
from qta_multiphysics.thermal_1d import solve_thermal_1d
from qta_multiphysics import verification as V


CFG = default_config()


# ---------------------------------------------------------------------------
# numerical self-consistency (thresholds identical to verification.py)
# ---------------------------------------------------------------------------
def test_diffusion_sanity_1d_analytic_decay():
    """Constant-coefficient cosine-mode decay matches the analytic rate (<10%)."""
    r = V.diffusion_sanity_1d()
    assert r["finite"], "1D diffusion produced non-finite values"
    assert r["rel_error"] < 0.10, f"decay-rate rel_error={r['rel_error']:.3e} >= 0.10"


def test_kapitza_boundary_sign():
    """Backside Kapitza term cools when T>T_fridge and warms when T<T_fridge."""
    r = V.kapitza_sign_check(CFG)
    assert r["hot_outflux_positive"], "Kapitza term does not cool a hot backside"
    assert r["cold_influx_negative"], "Kapitza term does not warm a cold backside"


def test_source_energy_conservation_1d():
    """Beer-Lambert depth profile integrates to the areal power (<5%) on the
    solver's own graded finite-volume mesh."""
    r = V.source_energy_integral_1d(CFG)
    assert r["rel_error"] < 0.05, f"1D source integral rel_error={r['rel_error']:.3e} >= 0.05"


def test_source_energy_conservation_2d():
    """Volumetric 2D source integrates to the absorbed power (<10%) with exact
    annular cell volumes on the solver's own graded axisymmetric mesh."""
    r = V.source_energy_integral_2d(CFG)
    assert r["rel_error"] < 0.10, f"2D source integral rel_error={r['rel_error']:.3e} >= 0.10"


def test_mesh_convergence_1d():
    """NV-layer temperature converges under 1D mesh refinement (n=100/200/400)."""
    rows, summary = V.mesh_convergence_1d(CFG)
    assert all(row["solver_status"] == "ok" for row in rows), \
        f"solver_status not ok in {rows}"
    assert summary["thermal_1d_converged"], \
        f"rel_change(200->400)={summary['thermal_1d_rel_change_200_400']:.3e} >= 0.15"


def test_axis_symmetry_2d():
    """2D axisymmetric solution is finite at r=0 with no axis singularity."""
    r = V.axis_symmetry_2d(CFG)
    assert r["axis_finite"], "non-finite values at the symmetry axis"
    assert r["axis_grad_small_vs_bulk"], "axis radial gradient exceeds bulk gradient"


def test_reduction_2d_to_1d():
    """With radial transport disabled, the hottest 2D column recovers the 1D
    NV temperature (<15%)."""
    r = V.reduction_2d_to_1d(CFG)
    assert r["reduces_to_1d"], f"2D->1D reduction rel_error={r['rel_error']:.3e} >= 0.15"


def test_documented_couplings_hold():
    """Optical->thermal, gas->surface, and thermal->desorption couplings hold."""
    r = V.coupling_checks(CFG)
    assert r["optical_feeds_thermal"], "laser source does not heat above base T"
    assert r["gas_feeds_surface"], "gas flux does not produce surface coverage"
    assert r["thermal_feeds_desorption"], "hotter surface does not reduce coverage"


# ---------------------------------------------------------------------------
# determinism and input validation
# ---------------------------------------------------------------------------
def test_thermal_1d_deterministic():
    """Two identical 1D solves produce bitwise-identical fields and metrics."""
    r1 = solve_thermal_1d(CFG, source_mode="averaged", n_cells=80, n_eval=15)
    r2 = solve_thermal_1d(CFG, source_mode="averaged", n_cells=80, n_eval=15)
    assert np.array_equal(np.asarray(r1.T_final.data), np.asarray(r2.T_final.data)), \
        "1D thermal field differs between identical runs"
    assert r1.nv_layer_temperature_K() == r2.nv_layer_temperature_K(), \
        "NV-layer temperature differs between identical runs"


def test_config_validation_rejects_bad_inputs():
    """SolverConfig.validate() rejects an unusably coarse mesh (error handling).

    Validation in this package is by explicit .validate() calls (the project
    convention), not construction-time checks; this exercises that contract."""
    try:
        SolverConfig(n_cells_1d=5).validate()
    except ValueError:
        return
    raise AssertionError("SolverConfig(n_cells_1d=5).validate() did not raise ValueError")


ALL_TESTS = [
    test_diffusion_sanity_1d_analytic_decay,
    test_kapitza_boundary_sign,
    test_source_energy_conservation_1d,
    test_source_energy_conservation_2d,
    test_mesh_convergence_1d,
    test_axis_symmetry_2d,
    test_reduction_2d_to_1d,
    test_documented_couplings_hold,
    test_thermal_1d_deterministic,
    test_config_validation_rejects_bad_inputs,
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
