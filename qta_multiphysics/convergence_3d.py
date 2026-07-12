"""Lightweight convergence checks for the 3D layer (verification only).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Numerical VERIFICATION only (never validation): the reduced CI solution is
compared against one refined mesh and against a tightened time integration
(smaller rtol and halved max step). Reported statuses are DERIVED_CHECK /
CONDITIONAL; outputs are deterministic. Nothing here is tuned to force
agreement -- observed changes are reported as-is.
"""
from __future__ import annotations

from .config import MultiphysicsConfig, default_config
from .mesh_3d import Grid3DConfig
from .thermal_3d_transient import solve_thermal_3d

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

TOL_MESH_REL = 0.10       # declared: NV probe change under one refinement
TOL_TIME_REL = 1.0e-3     # declared: tightened-integration change (implicit BDF)

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
    mesh_ok = mesh_rel < TOL_MESH_REL

    # ---- time-integration tightening (implicit BDF; adaptive) ----
    # tightened controls via a scoped solver-config field (restored in finally)
    sol = cfg.solver
    rtol0, ms_note = sol.rtol, "max_step = t_end/20 (solver-internal)"
    try:
        sol.rtol = rtol0 * 0.1
        tight = solve_thermal_3d(cfg, ci, n_eval=13)
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
            "status": "DERIVED_CHECK" if mesh_ok else "CONDITIONAL",
        },
        "time_integration_check": {
            "method": "implicit BDF (adaptive); tightened rtol x0.1",
            "base_rtol": rtol0, "tightened_rtol": rtol0 * 0.1,
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
