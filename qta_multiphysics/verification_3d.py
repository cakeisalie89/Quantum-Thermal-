"""Numerical verification checks for the 3D transient layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Every check here is a NUMERICAL self-consistency test of the 3D model at
reduced CI resolution (mirroring ``verification.py`` for the 1D/2D layers),
never a physical validation: zero-source stability, bitwise determinism,
source-field symmetry and solution symmetry, discrete source-integral
consistency, energy-accounting closure, and the 3D->1D / 3D->2D reduction
checks. Statuses are DERIVED_CHECK (within declared tolerance) or CONDITIONAL
(outside tolerance, reported honestly) only. Deterministic.
"""
from __future__ import annotations

import numpy as np

from .config import MultiphysicsConfig, default_config
from .mesh_3d import Grid3DConfig
from .thermal_3d_transient import solve_thermal_3d
from .energy_accounting_3d import ENERGY_CLOSURE_TOL
from .reduction_checks_3d import reduction_3d_to_1d, reduction_3d_to_2d

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

TOL_ZERO_SOURCE_K = 1.0e-9
TOL_SYMMETRY_REL = 1.0e-6
TOL_SOURCE_INTEGRAL_REL = 1.0e-9   # cell-integrated deposition is conservation-exact
TOL_ENERGY_REL = ENERGY_CLOSURE_TOL  # single shared closure tolerance


def _entry(name, ok_flag, detail):
    return {"check": name,
            "status": "DERIVED_CHECK" if ok_flag else "CONDITIONAL",
            "within_declared_tolerance": bool(ok_flag), **detail}


def run_verification_3d(cfg: MultiphysicsConfig | None = None,
                        g3: Grid3DConfig | None = None) -> dict:
    cfg = cfg or default_config()
    g3 = g3 or Grid3DConfig()      # default reduced CI mesh (10x10x12), as in tests
    checks = []

    # zero-source stability: field stays at the fridge temperature
    r0 = solve_thermal_3d(cfg, g3, source_scale=0.0, n_eval=7)
    drift = float(np.max(np.abs(r0.T - cfg.fridge.T_fridge_K)))
    checks.append(_entry("zero_source_stability", drift < TOL_ZERO_SOURCE_K,
                         {"max_drift_K": drift, "tolerance_K": TOL_ZERO_SOURCE_K}))

    # bitwise determinism
    ra = solve_thermal_3d(cfg, g3, n_eval=7)
    rb = solve_thermal_3d(cfg, g3, n_eval=7)
    same = bool(np.array_equal(ra.T, rb.T))
    checks.append(_entry("deterministic_repeat", same,
                         {"bitwise_identical": same}))

    # symmetry: centred beam on the symmetric mesh -> mirror-symmetric T
    Tf = ra.T_xyz(-1)
    asym_x = float(np.max(np.abs(Tf - Tf[::-1, :, :])))
    asym_y = float(np.max(np.abs(Tf - Tf[:, ::-1, :])))
    scale = float(np.max(Tf) - cfg.fridge.T_fridge_K) or 1.0
    rel = max(asym_x, asym_y) / scale
    checks.append(_entry("symmetry_preservation", rel < TOL_SYMMETRY_REL,
                         {"rel_asymmetry": rel, "tolerance": TOL_SYMMETRY_REL}))

    # discrete deposition vs captured analytic power (conservation-exact by
    # construction of the cell-integrated source; verified here)
    t_mid = 0.5 * ra.t[-1]
    frac = ra.laser.cell_fractions(ra.grid,
                                   front_position_m=cfg.geometry.front_position_m)
    P_t = float(np.asarray(ra.laser.base.temporal_power_W(t_mid)))
    P_disc = float(np.sum(frac)) * P_t
    P_cap = float(ra.laser.captured_power_W(
        ra.grid, t_mid, front_position_m=cfg.geometry.front_position_m))
    rel_src = abs(P_disc - P_cap) / max(P_cap, 1e-30)
    checks.append(_entry("source_integral_consistency",
                         rel_src < TOL_SOURCE_INTEGRAL_REL,
                         {"discrete_W": P_disc, "captured_analytic_W": P_cap,
                          "rel_error": rel_src,
                          "tolerance": TOL_SOURCE_INTEGRAL_REL}))

    # energy accounting closure
    rel_e = abs(ra.energy_residual())
    checks.append(_entry("energy_accounting_closure", rel_e < TOL_ENERGY_REL,
                         {"rel_residual": rel_e, "tolerance": TOL_ENERGY_REL,
                          **{k: v for k, v in ra.energy.items() if k != "label"}}))

    # reductions (each carries its own declared tolerance and honest status)
    red1 = reduction_3d_to_1d(cfg, g3)
    red2 = reduction_3d_to_2d(cfg, g3)
    checks.append(red1)
    checks.append(red2)

    all_ok = all(c.get("within_declared_tolerance",
                       c.get("status") == "DERIVED_CHECK") for c in checks)
    return {
        "layer": "thermal_3d_transient (reduced CI mesh)",
        "mesh": {"nx": g3.nx, "ny": g3.ny, "nz": g3.nz},
        "checks": checks,
        "all_within_declared_tolerances": bool(all_ok),
        "meaning": "numerical self-consistency of the reduced 3D model only; "
                   "NOT physical validation, NOT hardware, NOT measured",
        "statuses_allowed": ["DERIVED_CHECK", "CONDITIONAL"],
        "label": LABEL,
    }
