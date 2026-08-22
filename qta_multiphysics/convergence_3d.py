"""Lightweight convergence checks for the 3D layer (verification only).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Numerical VERIFICATION only (never validation): the reduced CI solution is
compared against one refined mesh and against a tightened time integration.

The time-integration tightening now really does both things this docstring
claimed: rtol is scaled by 0.1 AND the solver's maximum step is halved. Only
rtol was actually tightened before, because max_step was hard-coded as
t_end/20 inside solve_thermal_3d and was not reachable from a SolverConfig
field; solve_thermal_3d now takes max_step_divisor, so the halving is real.

The probe and the hotspot get SEPARATE convergence predicates. Previously the
mesh status was decided by the NV-probe change alone while the hotspot change
was computed, reported and then ignored, so a converged probe could hide a
non-converged hotspot. Both must now pass for the mesh check to read
DERIVED_CHECK.

Reported statuses are DERIVED_CHECK / CONDITIONAL; outputs are deterministic.
Nothing here is tuned to force agreement -- observed changes are reported
as-is. Numerical verification is never experimental validation.
"""
from __future__ import annotations

from .config import MultiphysicsConfig, default_config
from .mesh_3d import Grid3DConfig
from .thermal_3d_transient import solve_thermal_3d

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

TOL_MESH_REL = 0.10       # declared: NV probe change under one refinement
TOL_HOTSPOT_REL = 0.10    # declared: peak-temperature change under one refinement
TOL_TIME_REL = 1.0e-3     # declared: tightened-integration change (implicit BDF)

#: Time-integration tightening actually applied (both factors, see docstring).
RTOL_TIGHTEN_FACTOR = 0.1
MAX_STEP_DIVISOR_BASE = 20.0        # solve_thermal_3d default
MAX_STEP_DIVISOR_TIGHT = 40.0       # halved max step

CI = Grid3DConfig(nx=10, ny=10, nz=12)
REFINED = Grid3DConfig(nx=14, ny=14, nz=18)


def _probe(res):
    return float(res.probe_timeseries_K()[-1])


def convergence_report(cfg: MultiphysicsConfig | None = None,
                       ci: Grid3DConfig | None = None,
                       refined: Grid3DConfig | None = None) -> dict:
    cfg = cfg or default_config()
    ci = ci or CI
    refined = refined or REFINED
    base = solve_thermal_3d(cfg, ci, n_eval=13)
    p0, hot0 = _probe(base), float(base.T_xyz(-1).max())

    # ---- mesh refinement ----
    ref = solve_thermal_3d(cfg, refined, n_eval=13)
    p1, hot1 = _probe(ref), float(ref.T_xyz(-1).max())
    mesh_rel = abs(p1 - p0) / max(abs(p1), 1e-30)
    hot_rel = abs(hot1 - hot0) / max(abs(hot1), 1e-30)
    # Two independent predicates. A converged probe must not certify a
    # non-converged hotspot: hot_rel used to be reported and never tested.
    probe_ok = mesh_rel < TOL_MESH_REL
    hotspot_ok = hot_rel < TOL_HOTSPOT_REL
    mesh_ok = probe_ok and hotspot_ok

    # ---- time-integration tightening (implicit BDF; adaptive) ----
    # tightened controls via a scoped solver-config field (restored in finally)
    sol = cfg.solver
    rtol0 = sol.rtol
    ms_note = (f"max_step t_end/{MAX_STEP_DIVISOR_BASE:g} -> "
               f"t_end/{MAX_STEP_DIVISOR_TIGHT:g} (halved)")
    try:
        sol.rtol = rtol0 * RTOL_TIGHTEN_FACTOR
        tight = solve_thermal_3d(cfg, ci, n_eval=13,
                                 max_step_divisor=MAX_STEP_DIVISOR_TIGHT)
    finally:
        sol.rtol = rtol0
    p2 = _probe(tight)
    time_rel = abs(p2 - p0) / max(abs(p2), 1e-30)
    time_ok = time_rel < TOL_TIME_REL

    return {
        "meaning": "numerical verification only (solution-change under mesh "
                   "refinement and tightened time integration); never "
                   "validation, never a hardware statement",
        "mesh_check": {
            "meshes": [f"{ci.nx}x{ci.ny}x{ci.nz}",
                       f"{refined.nx}x{refined.ny}x{refined.nz}"],
            "target": "NV-layer probe temperature at t_end (K); peak T (K)",
            "probe_CI_K": p0, "probe_refined_K": p1,
            "probe_rel_change": mesh_rel,
            "hotspot_CI_K": hot0, "hotspot_refined_K": hot1,
            "hotspot_rel_change": hot_rel,
            "tolerance": TOL_MESH_REL,
            "probe_tolerance": TOL_MESH_REL,
            "hotspot_tolerance": TOL_HOTSPOT_REL,
            "probe_within_tolerance": bool(probe_ok),
            "hotspot_within_tolerance": bool(hotspot_ok),
            "predicate": "probe AND hotspot must both converge",
            "status": "DERIVED_CHECK" if mesh_ok else "CONDITIONAL",
        },
        "time_integration_check": {
            "method": ("implicit BDF (adaptive); tightened rtol x0.1 AND "
                       "max_step halved"),
            "base_rtol": rtol0, "tightened_rtol": rtol0 * RTOL_TIGHTEN_FACTOR,
            "base_max_step_divisor": MAX_STEP_DIVISOR_BASE,
            "tightened_max_step_divisor": MAX_STEP_DIVISOR_TIGHT,
            "max_step_note": ms_note,
            "target": "NV-layer probe temperature at t_end (K)",
            "probe_base_K": p0, "probe_tightened_K": p2,
            "rel_change": time_rel,
            "tolerance": TOL_TIME_REL,
            "status": "DERIVED_CHECK" if time_ok else "CONDITIONAL",
            "stability_note": "implicit BDF; no CFL-limited explicit stepping",
        },
        "statuses_allowed": ["DERIVED_CHECK", "CONDITIONAL"],
        "label": LABEL,
    }
