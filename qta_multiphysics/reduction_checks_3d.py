"""3D -> 2D and 3D -> 1D reduction checks (DERIVED numerical checks).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

These compare like-for-like reduced geometries only:

* 3D -> 1D: the 3D solver is run with the 'uniform_peak' transverse mode, in
  which every column receives exactly the 1D backend's areal intensity
  P/(pi w0^2 / 2). Lateral gradients then vanish and every 3D column solves the
  same problem as the canonical 1D backend (identical depth-attractor family,
  reduced resolution), so the NV-probe temperatures must agree within the
  declared numerical tolerance.
* 3D -> 2D: the Gaussian-beam 3D solve is compared against the 2D axisymmetric
  backend at matched resolved depth/radius and time window. The Cartesian box
  and the axisymmetric disc are different reduced geometries (the box carries
  extra corner volume outside the inscribed disc), so the declared tolerance
  allows for that geometric difference; disagreement beyond tolerance is reported honestly as CONDITIONAL,
  never hidden and never forced into agreement.

A failed tolerance is a numerical-consistency statement about reduced models,
not a physical result; statuses are DERIVED_CHECK / CONDITIONAL only.
"""
from __future__ import annotations

from .config import MultiphysicsConfig
from .thermal_1d import solve_thermal_1d
from .thermal_2d_axisymmetric import solve_thermal_2d
from .thermal_3d_transient import solve_thermal_3d
from .mesh_3d import Grid3DConfig

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

# Declared numerical tolerances (reduced-resolution CI meshes).
TOL_3D_TO_1D = 0.10
TOL_3D_TO_2D = 0.10


def _status(rel, tol):
    return "DERIVED_CHECK" if abs(rel) < tol else "CONDITIONAL"


def reduction_3d_to_1d(cfg: MultiphysicsConfig, g3: Grid3DConfig | None = None,
                       t_end: float | None = None, n_eval: int = 13) -> dict:
    cfg.validate()
    t_end = float(t_end if t_end is not None else cfg.solver.pulse_window_s)
    r3 = solve_thermal_3d(cfg, g3=g3, transverse="uniform_peak",
                          t_end=t_end, n_eval=n_eval)
    r1 = solve_thermal_1d(cfg, source_mode="averaged",
                          n_cells=cfg.solver.n_cells_1d, n_eval=n_eval,
                          t_end=t_end)
    T3 = r3.nv_layer_temperature_K()
    T1 = r1.nv_layer_temperature_K()
    rel = (T3 - T1) / max(abs(T1), 1e-30)
    return {
        "check": "reduction_3d_to_1d",
        "method": "uniform_peak transverse mode (every 3D column sees the 1D "
                  "areal intensity); lateral gradients vanish by construction",
        "T_nv_3d_K": float(T3), "T_nv_1d_K": float(T1),
        "rel_error": float(rel), "tolerance": TOL_3D_TO_1D,
        "within_tolerance": bool(abs(rel) < TOL_3D_TO_1D),
        "status": _status(rel, TOL_3D_TO_1D),
        "solver_status_3d": r3.solver_status, "solver_status_1d": r1.solver_status,
        "label": LABEL,
    }


def reduction_3d_to_2d(cfg: MultiphysicsConfig, g3: Grid3DConfig | None = None,
                       t_end: float | None = None, n_eval: int = 13) -> dict:
    cfg.validate()
    t_end = float(t_end if t_end is not None else cfg.solver.pulse_window_s)
    r3 = solve_thermal_3d(cfg, g3=g3, transverse="gaussian",
                          t_end=t_end, n_eval=n_eval)
    r2 = solve_thermal_2d(cfg, source_mode="averaged", t_end=t_end,
                          n_r=24, n_z=32, n_eval=n_eval)
    T3 = r3.nv_layer_temperature_K()
    # 2D probe: NV-layer maximum = the on-axis (r=0) value for a centred beam,
    # the like-for-like counterpart of the 3D beam-axis probe.
    T2 = float(r2.nv_layer_max_K())
    rel = (T3 - T2) / max(abs(T2), 1e-30)
    return {
        "check": "reduction_3d_to_2d",
        "method": "Gaussian-beam 3D (Cartesian box, adiabatic lateral) vs 2D "
                  "axisymmetric disc at matched depth/half-extent and window; "
                  "geometries differ by the box corner volume, hence the geometric-difference "
                  "allowance in the declared tolerance",
        "T_nv_3d_K": float(T3), "T_nv_2d_K": T2,
        "rel_error": float(rel), "tolerance": TOL_3D_TO_2D,
        "within_tolerance": bool(abs(rel) < TOL_3D_TO_2D),
        "status": _status(rel, TOL_3D_TO_2D),
        "solver_status_3d": r3.solver_status, "solver_status_2d": r2.solver_status,
        "label": LABEL,
    }


def run_reduction_checks(cfg: MultiphysicsConfig,
                         g3: Grid3DConfig | None = None) -> dict:
    c1 = reduction_3d_to_1d(cfg, g3=g3)
    c2 = reduction_3d_to_2d(cfg, g3=g3)
    overall = "DERIVED_CHECK" if (c1["within_tolerance"] and c2["within_tolerance"]) \
        else "CONDITIONAL"
    return {
        "label": LABEL,
        "overall_status": overall,
        "note": "Numerical consistency between reduced models; NOT physical "
                "validation; never PASS.",
        "reduction_3d_to_1d": c1,
        "reduction_3d_to_2d": c2,
    }
