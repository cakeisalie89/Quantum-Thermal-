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
  backend at matched resolved depth/radius and time window. This check used to
  run the 2D backend with its DEFAULT lateral boundary -- a cold radial contact
  to bulk at T_fridge -- against the 3D box's ADIABATIC lateral faces, and
  attributed the residual difference solely to "the box corner volume". That
  attribution was wrong: the two runs were different boundary-value problems,
  and the 2D lateral heat sink dominated the comparison.

  A first attempt to match them used ``disable_radial=True``, which was wrong:
  that flag removes radial transport THROUGHOUT the 2D domain, turning it into
  a stack of independent 1D columns (it reproduces the 1D solver to 0.7%). It
  is a 1D-reduction fixture, not an adiabatic wall, and comparing a full 3D
  solve against it produced a spurious 53% "disagreement" that was recorded as
  a falsified model condition. That was an artifact of the fixture.

  The 2D backend now has a real ``lateral_adiabatic`` option that keeps
  interior radial conduction active and applies zero normal flux at r=R only.
  With it, the two backends are asked the same question and they agree:

      2D cold radial contact (production BC):  rel vs 3D = +1.18e-03
        -- small, but a comparison between DIFFERENT boundary-value problems
      2D lateral_adiabatic (matched to 3D):    rel vs 3D = -1.6e-02 at CI
        meshes, -8.0e-03 at refined meshes

  The matched residual HALVES under refinement, which is the signature of
  agreement limited by discretization rather than a model inconsistency. The
  mismatched comparison is still reported alongside, explicitly labelled as not
  an equivalence statement; it must not be presented as the stronger evidence
  merely because its number is smaller.

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
    T3 = r3.nv_layer_temperature_K()
    # 2D probe: NV-layer maximum = the on-axis (r=0) value for a centred beam,
    # the like-for-like counterpart of the 3D beam-axis probe.

    # PRIMARY: same boundary-value problem on both sides. The 3D box has
    # adiabatic lateral faces, so the 2D disc uses lateral_adiabatic -- zero
    # normal flux at r=R with interior radial conduction retained. NOT
    # disable_radial, which would remove radial transport everywhere.
    r2m = solve_thermal_2d(cfg, source_mode="averaged", t_end=t_end,
                           n_r=24, n_z=32, n_eval=n_eval, lateral_adiabatic=True)
    T2m = float(r2m.nv_layer_max_K())
    rel_m = (T3 - T2m) / max(abs(T2m), 1e-30)

    # REFERENCE ONLY: the 2D production boundary (cold radial contact). Kept
    # visible because it is what this check used to report, and because the
    # contrast is the evidence that the old number measured boundary physics
    # rather than dimensional consistency. It is NOT an equivalence statement.
    r2p = solve_thermal_2d(cfg, source_mode="averaged", t_end=t_end,
                           n_r=24, n_z=32, n_eval=n_eval, disable_radial=False)
    T2p = float(r2p.nv_layer_max_K())
    rel_p = (T3 - T2p) / max(abs(T2p), 1e-30)

    return {
        "check": "reduction_3d_to_2d",
        "method": "Gaussian-beam 3D (Cartesian box) vs 2D axisymmetric disc at "
                  "matched depth/half-extent and window, with the SAME lateral "
                  "boundary condition on both sides (adiabatic). The 2D "
                  "production boundary is a cold radial contact; comparing "
                  "against it compares different boundary-value problems and "
                  "is reported separately below, never as equivalence.",
        "boundary_conditions_matched": True,
        "lateral_bc_3d": "adiabatic (BoundarySpec3D, enforced)",
        "lateral_bc_2d_in_this_check": "adiabatic at r=R (lateral_adiabatic="
                                       "True; interior radial conduction "
                                       "retained; reduction fixture only)",
        "T_nv_3d_K": float(T3), "T_nv_2d_K": T2m,
        "rel_error": float(rel_m), "tolerance": TOL_3D_TO_2D,
        "within_tolerance": bool(abs(rel_m) < TOL_3D_TO_2D),
        "status": _status(rel_m, TOL_3D_TO_2D),
        "mismatched_boundary_reference": {
            "meaning": "NOT a reduction result: 3D adiabatic lateral vs 2D "
                       "cold radial contact. Reported because this is the "
                       "comparison the check previously made, and its small "
                       "rel_error came from the 2D lateral heat sink, not "
                       "from dimensional consistency.",
            "lateral_bc_2d": "cold radial contact to bulk at T_fridge",
            "T_nv_2d_K": T2p,
            "rel_error": float(rel_p),
            "solver_status_2d": r2p.solver_status,
        },
        "geometry_note": "box/disc volume ratio is 4/pi; with Cp ~ T^3 the "
                         "internal energy goes as T^4, so full lateral "
                         "equilibration would give ~6% in temperature. The "
                         "beam is well inside the domain (R/w0 = 8), so the "
                         "peak stays beam-local and the observed matched "
                         "residual is far smaller than that bound.",
        "solver_status_3d": r3.solver_status, "solver_status_2d": r2m.solver_status,
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
