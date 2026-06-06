"""3D multiphysics: FUTURE_WORK / NOT_IMPLEMENTED.

This module is deliberately NOT a working solver. It exists to (a) document the
intended 3D architecture and (b) make the deferral explicit and machine-checkable.
Calling the solver raises NotImplementedError. The THREE_D_FUTURE_WORK_CHECK gate
reads STATUS from here; it is a DERIVED_CHECK, never a PASS, and 3D is excluded
from all physical gates in this pass.
"""
from __future__ import annotations

STATUS = "FUTURE_WORK"          # one of the allowed evidence labels
IMPLEMENTED = False

PLANNED_ARCHITECTURE = {
    "thermal_3d": "T(x,y,z,t) full anisotropic conduction; FV on a 3D grid; "
                  "method-of-lines + sparse stiff integrator. NOT_IMPLEMENTED.",
    "gas_transport_3d": "3D advection-diffusion in the process volume. NOT_IMPLEMENTED.",
    "surface_coverage_3d": "Coverage on a 2D surface manifold with 3D flux. NOT_IMPLEMENTED.",
    "grid_3d": "StructuredGrid3D (x,y,z) with face areas and cell volumes. NOT_IMPLEMENTED.",
    "verification_3d": "3D mesh convergence, symmetry, 3D->2D reduction. NOT_IMPLEMENTED.",
}


def thermal_3d(*args, **kwargs):
    raise NotImplementedError(
        "3D thermal solver is FUTURE_WORK / NOT_IMPLEMENTED. The 1D (canonical) "
        "and 2D axisymmetric backends are the implemented non-lumped models in "
        "this pass. 3D will be added only with real code, verification, and "
        "outputs.")


def status_report():
    return {
        "module": "future_3d",
        "status": STATUS,
        "implemented": IMPLEMENTED,
        "planned_architecture": PLANNED_ARCHITECTURE,
        "note": "3D is not claimed and is excluded from all physical gates.",
    }
