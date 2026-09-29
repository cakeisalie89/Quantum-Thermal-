"""Independent check of thermal 1D: the 2D axisymmetric solver, reduced.

With radial transport disabled, every radial column of the 2D axisymmetric
problem is an independent 1D depth problem, and the axis column's areal
power is exactly the 1D slab's. So the 2D solver -- a different
discretization in a different module, ``thermal_2d_axisymmetric`` -- must
reproduce the 1D NV-layer peak temperature. This is the check
``qta_multiphysics.verification.reduction_2d_to_1d`` performs inside the
QTA verification suite; here it is run against a ResultBundle, by a
verifier, with its own implementation digest, and reported as a
VerificationResult.

WHAT IT ESTABLISHES, AND WHAT IT DOES NOT

Agreement between two discretizations of ONE model. It shares the material
models (k(T), Cp(T)), the laser source term and the parameter-to-
configuration mapping with the producer, and says so; an error in any of
those is invisible to it. It is not a comparison with measurement, and a
PASS here is not experimental validation of anything.

The criterion, rel <= 0.15, is the one ``reduction_2d_to_1d`` applies,
unchanged. The meshes differ (the producer's ``n_cells`` in 1D, the
configuration's ``n_z_2d`` in 2D), which the criterion absorbs and the
limitations state. Only the ``averaged`` source mode is covered: for a pulse
run the check reports NOT_RUN rather than comparing unlike problems.
"""
from __future__ import annotations

from ..identity import implementation_digest
from ..models.thermal_1d import MODEL_ID, MODEL_VERSION, configure
from ..quantity import Quantity, ResolutionClass as RC
from ..result import OutputStatus, ResultBundle
from ..verification import (
    CheckType, Establishes, Independence, Status, VerificationResult,
)

CHECK_ID = "thermal_1d.reduction_2d_radial_disabled"
IMPLEMENTATION_MODULES = ("scientific.checks.reduction_2d",
                          "qta_multiphysics.thermal_2d_axisymmetric")
REL_MAX = 0.15
N_R = 24
N_EVAL = 12

SHARED = ("material models k(T), Cp(T)", "laser source term",
          "parameter-to-configuration mapping")
LIMITS = ("agreement of two discretizations of one model, not of the model "
          "with measurement",
          "the 1D and 2D depth meshes differ (producer n_cells vs "
          "configuration n_z_2d); the 0.15 criterion absorbs that",
          "averaged source mode only")


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
        criterion=f"|T2D_axis - T1D| / |T1D| <= {REL_MAX}",
        criterion_derivation="the criterion qta_multiphysics.verification."
                             "reduction_2d_to_1d applies (reduces_to_1d = "
                             "rel < 0.15), unchanged",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_DISCRETIZATION,
        establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
        shared_components=SHARED, limitations=LIMITS)
    out = bundle.output("nv_layer_peak_T")
    if bundle.parameters.get("source_mode") != "averaged" or \
            out.status is not OutputStatus.OK:
        why = ("pulse mode is outside this check" if out.status is
               OutputStatus.OK else f"the producer's output is "
               f"{out.status.value}")
        return VerificationResult(
            **{**common, "limitations": LIMITS + (why,)},
            status=Status.NOT_RUN, measured=None, threshold=None)

    from qta_multiphysics.numerics import require_converged
    from qta_multiphysics.thermal_2d_axisymmetric import solve_thermal_2d

    cfg = configure(bundle.parameters)
    r2 = require_converged(
        solve_thermal_2d(cfg, source_mode="averaged", n_r=N_R,
                         n_z=cfg.solver.n_z_2d, n_eval=N_EVAL,
                         disable_radial=True),
        "reduction_2d: 2D radial-disabled solve")
    nv1 = out.quantity.value
    nv2 = r2.nv_layer_max_K()
    rel = abs(nv2 - nv1) / max(abs(nv1), 1e-12)
    return VerificationResult(
        **common,
        status=Status.PASS if rel <= REL_MAX else Status.FAIL,
        measured=Quantity(rel, "1", resolution=cfg.solver.rtol,
                          resolution_class=RC.SOLVER_TOLERANCE,
                          resolution_basis="solve_ivp rtol of both solves",
                          reporting_digits=4),
        threshold=Quantity(REL_MAX, "1"))
