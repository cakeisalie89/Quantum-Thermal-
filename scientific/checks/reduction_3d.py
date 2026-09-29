"""Independent check of thermal 2D: the 3D Cartesian solver, adiabatic sides.

A Gaussian beam on the 3D transient solver -- a different module
(``thermal_3d_transient``), a different discretization (a structured
Cartesian box, 7-point stencil) and a different geometry (a box, not a disc)
-- with adiabatic lateral faces solves the problem an ``adiabatic`` thermal
2D run solves. Its beam-axis NV-layer peak must match the bundle's. This is
``qta_multiphysics.reduction_checks_3d.reduction_3d_to_2d``'s primary,
boundary-matched comparison, run against a ResultBundle by a verifier with
its own implementation digest and reported as a VerificationResult.

WHAT IT ESTABLISHES, AND WHAT IT DOES NOT

Agreement between two discretizations of ONE model. It shares the material
models, the laser parameters and the parameter-to-configuration mapping with
the producer, and says so. The box holds 4/pi of the disc's volume, which
the criterion absorbs for a beam well inside the domain and the limitations
state. It is not a comparison with measurement.

It speaks ONLY to an ``adiabatic``, ``averaged`` run: the 3D box has no cold
radial contact to compare a production-boundary run against, and no pulse
comparison has an established criterion. Anything else is NOT_RUN, never a
comparison of unlike problems.

The criterion, rel < 0.10, is ``reduction_checks_3d.TOL_3D_TO_2D``, unchanged
(measured about 1.7e-2 at the default meshes).
"""
from __future__ import annotations

from ..identity import implementation_digest
from ..models.thermal_2d import (
    MODEL_ID, MODEL_VERSION, configure, window_s,
)
from ..quantity import Quantity, ResolutionClass as RC
from ..result import OutputStatus, ResultBundle
from ..verification import (
    CheckType, Establishes, Independence, Status, VerificationResult,
)

CHECK_ID = "thermal_2d.reduction_3d_adiabatic_lateral"
IMPLEMENTATION_MODULES = ("scientific.checks.reduction_3d",
                          "qta_multiphysics.thermal_3d_transient")
REL_MAX = 0.10
N_EVAL = 13

SHARED = ("material models k(T), Cp(T)", "laser source parameters",
          "parameter-to-configuration mapping")
LIMITS = ("agreement of two discretizations of one model, not of the model "
          "with measurement",
          "box versus disc: the box holds 4/pi of the disc's volume; the "
          "0.10 criterion absorbs that for a beam well inside the domain",
          "the 3D mesh is the solver's default Grid3DConfig and its time "
          "sampling is 13 points, not the producer's",
          "adiabatic lateral boundary and averaged source only")


def check_digest() -> str:
    return implementation_digest(IMPLEMENTATION_MODULES)


def run_check(bundle: ResultBundle, *, verifier_id: str) -> VerificationResult:
    if (bundle.model_id, bundle.model_version) != (MODEL_ID, MODEL_VERSION):
        raise ValueError(f"this check is for {MODEL_ID}@{MODEL_VERSION}, "
                         f"not {bundle.model_id}@{bundle.model_version}")
    common = dict(
        check_id=CHECK_ID, check_type=CheckType.INDEPENDENT_IMPLEMENTATION,
        subject_digest=bundle.digest(),
        subject_model=f"{bundle.model_id}@{bundle.model_version}",
        criterion=f"|T3D_axis - T2D_axis| / |T2D_axis| < {REL_MAX}",
        criterion_derivation="the tolerance qta_multiphysics."
                             "reduction_checks_3d.reduction_3d_to_2d "
                             "applies (TOL_3D_TO_2D = 0.10), unchanged",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_DISCRETIZATION,
        establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
        shared_components=SHARED, limitations=LIMITS)
    out = bundle.output("nv_layer_peak_T")
    params = bundle.parameters
    why = None
    if out.status is not OutputStatus.OK:
        why = f"the producer's output is {out.status.value}"
    elif params.get("lateral_boundary") != "adiabatic":
        why = ("a cold_contact run has no counterpart in the 3D box, whose "
               "lateral faces are adiabatic")
    elif params.get("source_mode") != "averaged":
        why = "pulse mode is outside this check"
    if why is not None:
        return VerificationResult(
            **{**common, "limitations": LIMITS + (why,)},
            status=Status.NOT_RUN, measured=None, threshold=None)

    from qta_multiphysics.numerics import require_converged
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d

    cfg = configure(params)
    r3 = require_converged(
        solve_thermal_3d(cfg, source_mode="averaged", transverse="gaussian",
                         t_end=window_s(cfg, "averaged"), n_eval=N_EVAL),
        "reduction_3d: 3D Gaussian solve")
    nv2 = out.quantity.value
    nv3 = r3.nv_layer_temperature_K()
    rel = abs(nv3 - nv2) / max(abs(nv2), 1e-12)
    return VerificationResult(
        **common,
        status=Status.PASS if rel < REL_MAX else Status.FAIL,
        measured=Quantity(rel, "1", resolution=cfg.solver.rtol,
                          resolution_class=RC.SOLVER_TOLERANCE,
                          resolution_basis="solve_ivp rtol of both solves",
                          reporting_digits=4),
        threshold=Quantity(REL_MAX, "1"))
