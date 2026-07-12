"""Energy accounting for the 3D transient layer (DERIVED numerical check).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Closure is a NUMERICAL self-consistency statement about the finite-volume
solution (integrated source vs internal-energy change vs Kapitza boundary
outflow); it is not physical validation and never produces a PASS.
"""
from __future__ import annotations

from .thermal_3d_transient import Thermal3DResult, LABEL

# Declared numerical tolerance for 3D energy closure (matches the 1D/2D
# verification tolerances' order; DERIVED_CHECK only).
ENERGY_CLOSURE_TOL = 0.05


def energy_accounting_rows(res: Thermal3DResult):
    e = res.energy
    rel = float(e.get("rel_residual", float("nan")))
    status = "DERIVED_CHECK" if abs(rel) < ENERGY_CLOSURE_TOL else "CONDITIONAL"
    rows = [
        {"component": "integrated_source_energy_J", "value": f"{e['integrated_source_energy_J']:.9e}",
         "note": "time-integrated volumetric laser deposition"},
        {"component": "boundary_sink_energy_J", "value": f"{e['boundary_sink_energy_J']:.9e}",
         "note": "time-integrated Kapitza-radiative back-face outflow"},
        {"component": "internal_energy_change_J", "value": f"{e['internal_energy_change_J']:.9e}",
         "note": "sum over cells of rho*int(Cp dT)*V"},
        {"component": "residual_J", "value": f"{e['residual_J']:.9e}",
         "note": "source - sink - stored"},
        {"component": "rel_residual", "value": f"{rel:.6e}",
         "note": f"closure tolerance {ENERGY_CLOSURE_TOL} (numerical self-consistency, "
                 f"not physical validation)"},
        {"component": "closure_status", "value": status,
         "note": "DERIVED_CHECK if |rel_residual| < tol else CONDITIONAL (never PASS)"},
    ]
    for r in rows:
        r["label"] = LABEL
    return rows


def closure_ok(res: Thermal3DResult, tol: float = ENERGY_CLOSURE_TOL) -> bool:
    return abs(res.energy_residual()) < tol
