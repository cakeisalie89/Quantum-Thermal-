"""Status module for the 3D transient layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

The 3D transient layer is implemented only as an additive forecast-only /
benchmark-numerical validation layer (``mesh_3d``, ``thermal_3d_transient``,
``laser_source_3d``, ``boundaries_3d``, ``energy_accounting_3d``,
``reduction_checks_3d``, ``verification_3d``, ``mode_sequence_3d`` and the
forecast hooks). It is not hardware-validated, not COMSOL, not measured
in-system, and introduces no PASS gates. The canonical 1D and 2D axisymmetric
backends remain the gate authority; the 3D layer is reduction-checked against
them at reduced CI resolution. The THREE_D_LAYER_STATUS_CHECK gate records this
status as a DERIVED_CHECK (never PASS).
"""
from __future__ import annotations

STATUS = "FORECAST_ONLY_IMPLEMENTED"
IMPLEMENTED = True

CLAIM_BOUNDARY = (
    "3D transient layer is implemented only as an additive forecast-only / "
    "benchmark-numerical validation layer. It is not hardware-validated, not "
    "COMSOL, not measured in-system, and introduces no PASS gates.")

IMPLEMENTED_COMPONENTS = (
    "structured graded 3D finite-volume mesh (mesh_3d)",
    "transient 3D heat equation, BDF method-of-lines (thermal_3d_transient)",
    "exactly-conservative volumetric fs-laser source (laser_source_3d)",
    "insulated-front / adiabatic-lateral / Kapitza-radiative-back boundaries (boundaries_3d)",
    "energy-accounting closure (energy_accounting_3d)",
    "3D->1D and 3D->2D reduction checks (reduction_checks_3d)",
    "numerical verification suite (verification_3d)",
    "Mode B->C->D sequencing with species safety (mode_sequence_3d)",
    "forecast-only hooks: cryo stack registry, species transport, surface "
    "coverage, radiation/vibration/microwave budgets",
)

NOT_IMPLEMENTED_COMPONENTS = (
    "per-region 3D material property sets",
    "C-13 methane 3D deposition footprint (no inlet geometry parameterized)",
    "3D radiative view factors",
    "3D modal analysis",
    "3D microwave field/SAR map",
    "LN2 precooling / standalone Joule-Thomson / nuclear demagnetization / "
    "gas-gap heat switch (architecture-registry entries only)",
)


def thermal_3d(*args, **kwargs):
    """Delegates to the implemented reduced 3D transient solver.

    Forecast-only / benchmark-numerical; never a hardware claim.
    """
    from .thermal_3d_transient import solve_thermal_3d
    from .config import default_config
    if not args and "cfg" not in kwargs:
        args = (default_config(),)
    return solve_thermal_3d(*args, **kwargs)


def status_report() -> dict:
    return {
        "status": STATUS,
        "implemented": IMPLEMENTED,
        "claim_boundary": CLAIM_BOUNDARY,
        "implemented_components": list(IMPLEMENTED_COMPONENTS),
        "not_implemented_components": list(NOT_IMPLEMENTED_COMPONENTS),
        "measured_in_this_system": False,
        "can_PASS_now": "NO",
        "hardware_validated": False,
        "external_solver": "none (NumPy/SciPy only; no COMSOL)",
    }
