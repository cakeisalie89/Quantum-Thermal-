"""§6 regression: the 2D lateral boundary hooks are distinct and correct.

A 3D->2D reduction check must compare the SAME boundary-value problem. An
earlier attempt matched the 3D layer's adiabatic lateral walls by passing
``disable_radial=True`` to the 2D backend. That flag removes radial transport
THROUGHOUT the domain -- it is a 1D-reduction fixture that reproduces the 1D
solver -- so the comparison was 3D-with-conduction against a stack of 1D
columns, and it manufactured a ~53% "disagreement" that was recorded as a
falsified model condition.

``lateral_adiabatic=True`` is the real thing: interior radial conduction stays
active and zero normal flux is applied at r=R only.

These tests assert PHYSICAL properties, never the specific temperatures the
diagnostics happened to produce, so they stay meaningful if the mesh, window or
material model changes.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.config import default_config                # noqa: E402
from qta_multiphysics.thermal_1d import solve_thermal_1d          # noqa: E402
from qta_multiphysics.thermal_2d_axisymmetric import (            # noqa: E402
    solve_thermal_2d)
from qta_multiphysics.thermal_3d_transient import solve_thermal_3d  # noqa: E402
from qta_multiphysics.mesh_3d import Grid3DConfig                 # noqa: E402

CFG = default_config()
T_END = CFG.solver.pulse_window_s


def _run2d(**kw):
    return solve_thermal_2d(CFG, source_mode="averaged", t_end=T_END,
                            n_r=24, n_z=32, n_eval=13, **kw)


# ------------------------------- interior radial conduction stays active ----

def test_lateral_adiabatic_keeps_interior_radial_conduction():
    """Heat must still spread radially; only the outer face is sealed.

    Under disable_radial each column is independent, so the outermost column
    never receives heat from the beam. Under lateral_adiabatic it must.
    """
    adia = _run2d(lateral_adiabatic=True)
    none_ = _run2d(disable_radial=True)
    T_fridge = CFG.fridge.T_fridge_K
    rim_adia = float(adia.T_final.values[-1, :].max())
    rim_none = float(none_.T_final.values[-1, :].max())
    assert rim_adia > rim_none, (
        f"outer column no hotter with conduction on ({rim_adia}) than with "
        f"radial transport removed ({rim_none})")
    assert rim_adia > T_fridge, (
        "interior radial conduction is not delivering heat to the outer column")


def test_disable_radial_leaves_the_outer_column_unheated():
    """Confirms the two flags are not variants of one another."""
    none_ = _run2d(disable_radial=True)
    rim = float(none_.T_final.values[-1, :].max())
    axis = float(none_.T_final.values[0, :].max())
    assert axis > 10 * rim, (
        "with radial transport removed the outer column should stay near its "
        f"initial state; axis={axis} rim={rim}")


# ------------------------------------------- zero flux at the outer face ----

def test_lateral_adiabatic_exports_no_energy_through_the_outer_face():
    r = _run2d(lateral_adiabatic=True)
    assert r.energy["radial_boundary_energy_J"] == 0.0, (
        "an adiabatic outer face exported energy: "
        f"{r.energy['radial_boundary_energy_J']}")


def test_production_boundary_does_export_energy():
    """Control: without the flag the cold contact is a real sink."""
    r = _run2d()
    assert r.energy["radial_boundary_energy_J"] > 0.0, (
        "the production cold radial contact exported no energy; the control "
        "for the previous test is not meaningful")


def test_energy_still_balances_with_an_adiabatic_outer_face():
    r = _run2d(lateral_adiabatic=True)
    assert r.solver_status == "ok"
    assert abs(r.energy["rel_residual"]) < 0.05, r.energy


# ------------------------------------- the two hooks are distinguishable ----

def test_the_three_lateral_configurations_are_distinct():
    prod = float(_run2d().nv_layer_max_K())
    adia = float(_run2d(lateral_adiabatic=True).nv_layer_max_K())
    none_ = float(_run2d(disable_radial=True).nv_layer_max_K())
    assert prod != adia != none_
    assert abs(none_ - adia) / adia > 0.5, (
        "disable_radial and lateral_adiabatic are supposed to be very "
        f"different physics; got {none_} vs {adia}")


def test_disable_radial_reproduces_the_1d_solver():
    """Pins what disable_radial actually means, so it cannot be misread again."""
    t1 = solve_thermal_1d(CFG, source_mode="averaged", t_end=T_END, n_eval=13)
    t2 = float(_run2d(disable_radial=True).nv_layer_max_K())
    rel = abs(t2 - t1.nv_layer_temperature_K()) / t1.nv_layer_temperature_K()
    assert rel < 0.05, (
        f"disable_radial no longer reduces to the 1D solve (rel={rel:.3e}); "
        "its docstring and every consumer assume it does")


def test_disable_radial_takes_precedence_when_both_are_set():
    both = float(_run2d(disable_radial=True, lateral_adiabatic=True).nv_layer_max_K())
    none_ = float(_run2d(disable_radial=True).nv_layer_max_K())
    assert both == none_


# ------------------------------------ matched comparison and convergence ----

def _matched_rel(nr, nz, n3):
    r3 = solve_thermal_3d(CFG, g3=Grid3DConfig(nx=n3[0], ny=n3[1], nz=n3[2]),
                          transverse="gaussian", t_end=T_END, n_eval=13)
    r2 = solve_thermal_2d(CFG, source_mode="averaged", t_end=T_END,
                          n_r=nr, n_z=nz, n_eval=13, lateral_adiabatic=True)
    T3 = float(r3.nv_layer_temperature_K())
    T2 = float(r2.nv_layer_max_K())
    return (T3 - T2) / T2


def test_matched_comparison_converges_under_refinement():
    """The residual must SHRINK with refinement.

    That is the property that distinguishes 'agreement limited by
    discretization' from 'the models disagree'. No absolute value is asserted.
    """
    coarse = abs(_matched_rel(24, 32, (10, 10, 12)))
    fine = abs(_matched_rel(48, 64, (18, 18, 22)))
    assert fine < coarse, (
        f"matched-BC residual grew under refinement: {coarse:.4e} -> {fine:.4e}")


def test_reduction_check_uses_the_matched_fixture_not_the_1d_one():
    from qta_multiphysics.reduction_checks_3d import reduction_3d_to_2d
    c = reduction_3d_to_2d(CFG)
    assert c["boundary_conditions_matched"] is True
    assert "lateral_adiabatic" in c["lateral_bc_2d_in_this_check"]
    assert "disable_radial" not in c["lateral_bc_2d_in_this_check"]
    assert c["solver_status_2d"] == "ok"


if __name__ == "__main__":
    ns = dict(globals())
    fails = 0
    for name, fn in sorted(ns.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:                                # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if fails else 0)
