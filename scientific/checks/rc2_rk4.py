"""Independent check of the two-node RC network: classical Runge-Kutta,
against the producer's closed form.

The producer (``scientific.models.thermal_rc2``) solves the linear system
exactly: the eigen-decomposition of its 2 x 2 matrix, the steady state, the
exponentials. This check integrates the same two equations numerically, by
classical fourth-order Runge-Kutta at a fixed step, and shares nothing of
the producer's method: no eigenvalue, no eigenvector, no steady state. Even
the step is chosen from a Gershgorin bound on the matrix's rows, not from
its eigenvalues.

    C1 dT1/dt = Q - G12 (T1 - T2)
    C2 dT2/dt = G12 (T1 - T2) - G2a (T2 - Tamb)

ITS OWN RESOLUTION. The march is made with N and with 2N steps; the 2N
result is the check's answer. Its error is stated as the sum of two terms:

* truncation, |y_N - y_2N| / 15 -- Richardson, for a fourth-order method;
  the estimate belongs to the finer of the two marches;
* rounding, 2 x 2N x eps x the largest |T| either node reaches in the fine
  march. Each step rounds each node once in its update and its increment
  carries a few roundings more, and the network does not amplify what a
  step leaves: its matrix has non-negative off-diagonal entries and no
  positive row sum, so the exact propagator is non-negative with row sums
  at most one, and RK4's, at the step chosen below, differs from it by
  less than 3e-9 per step (the remainder (h ||A||)^5 / 120). Halving the
  step does not shrink rounding -- it doubles the steps -- so the
  Richardson difference alone would understate it once truncation is
  negligible.

N puts at least STEPS_PER_TAU steps in the network's fastest time constant:
h ||A|| <= 1 / STEPS_PER_TAU, with ||A|| the largest absolute row sum of the
matrix, which bounds every eigenvalue (Gershgorin). At least MIN_STEPS in
all; a network that would need more than MAX_STEPS is NOT_RUN, never a
pass.

THE CRITERION. For T1 and T2 at t_end: |T_producer - T_check| <=
3 (r_producer + r_check) + 64 eps |T|: Roache's factor of safety on the sum
of both stated estimates.

WHAT IT ESTABLISHES, AND WHAT IT DOES NOT. Independent numerical agreement
of two solutions of one model. A wrong model of the physics -- a third node
that matters, a temperature-dependent conductance -- is invisible to it; it
is not a comparison with measurement, and a PASS is not experimental
validation of anything.
"""
from __future__ import annotations

import math
import sys
from typing import Any

from ..identity import implementation_digest
from ..quantity import Quantity, ResolutionClass as RC
from ..result import OutputStatus, ResultBundle
from ..verification import (
    CheckType, Establishes, Independence, Status, VerificationResult,
)
from . import quantity, resolution

CHECK_ID = "thermal.rc2_rk4"
#: the model this check was written for -- stated, not imported, so this
#: check's code identity covers none of the producer's code. A new model
#: version is refused until this check has been reviewed against it.
MODEL_ID = "thermal.rc2_network"
MODEL_VERSION = "1.0.0"
IMPLEMENTATION_MODULES = ("scientific.checks.rc2_rk4",)
OUTPUT_NAMES = ("T1", "T2")
GCI_SAFETY = 3.0
#: RK4 steps in the fastest time constant, at the least
STEPS_PER_TAU = 20
MIN_STEPS = 400
MAX_STEPS = 2_000_000
_EPS = sys.float_info.epsilon

SHARED = ("the network equations: C1 T1' = Q - G12 (T1 - T2), "
          "C2 T2' = G12 (T1 - T2) - G2a (T2 - Tamb)",)
LIMITS = ("agreement of two solutions of one model, not of the model with "
          "measurement",
          "a wrong model of the physics is invisible to this check")


def check_digest() -> str:
    return implementation_digest(IMPLEMENTATION_MODULES)


def steps_for(p: dict) -> int | None:
    """N for a march over ``t_end_s``: STEPS_PER_TAU steps in the fastest
    time constant, bounded by the Gershgorin discs of the system's matrix
    (each eigenvalue's magnitude is at most a row's absolute sum). None
    when that is more than MAX_STEPS."""
    rate = max(2.0 * p["G12_W_K"] / p["C1_J_K"],
               (2.0 * p["G12_W_K"] + p["G2a_W_K"]) / p["C2_J_K"])
    n = max(MIN_STEPS, math.ceil(p["t_end_s"] * rate * STEPS_PER_TAU))
    return n if n <= MAX_STEPS else None


def march(p: dict, n: int) -> tuple:
    """(T1, T2, the largest |T| reached) at ``t_end_s`` after ``n``
    classical RK4 steps."""
    C1, C2, G12, G2a = p["C1_J_K"], p["C2_J_K"], p["G12_W_K"], p["G2a_W_K"]
    Q, Ta = p["Q_W"], p["Tamb_K"]

    def rate(t1, t2):
        flow = G12 * (t1 - t2)
        return (Q - flow) / C1, (flow - G2a * (t2 - Ta)) / C2

    h = p["t_end_s"] / n
    t1, t2 = float(p["T1_0_K"]), float(p["T2_0_K"])
    peak = max(abs(t1), abs(t2))
    for _ in range(n):
        a1, a2 = rate(t1, t2)
        b1, b2 = rate(t1 + 0.5 * h * a1, t2 + 0.5 * h * a2)
        c1, c2 = rate(t1 + 0.5 * h * b1, t2 + 0.5 * h * b2)
        d1, d2 = rate(t1 + h * c1, t2 + h * c2)
        t1 += h * (a1 + 2.0 * b1 + 2.0 * c1 + d1) / 6.0
        t2 += h * (a2 + 2.0 * b2 + 2.0 * c2 + d2) / 6.0
        peak = max(peak, abs(t1), abs(t2))
    return t1, t2, peak


def solve(p: dict) -> tuple | None:
    """The check's own answer, apart from its judgment: ((T1, r1), (T2, r2))
    at ``t_end_s`` -- the 2N march, and the error the check states for it,
    Richardson's estimate plus the rounding bound. None when the network
    needs more than MAX_STEPS."""
    n = steps_for(p)
    if n is None:
        return None
    coarse, fine = march(p, n), march(p, 2 * n)
    rounding = 2.0 * (2 * n) * _EPS * fine[2]
    return tuple((tf, abs(tc - tf) / 15.0 + rounding)
                 for tc, tf in zip(coarse[:2], fine[:2]))


def run_check(bundle: ResultBundle, *, verifier_id: str) -> VerificationResult:
    if (bundle.model_id, bundle.model_version) != (MODEL_ID, MODEL_VERSION):
        raise ValueError(f"this check is for {MODEL_ID}@{MODEL_VERSION}, "
                         f"not {bundle.model_id}@{bundle.model_version}")
    common: dict[str, Any] = dict(
        check_id=CHECK_ID, check_type=CheckType.INDEPENDENT_IMPLEMENTATION,
        subject_digest=bundle.digest(),
        subject_model=f"{bundle.model_id}@{bundle.model_version}",
        criterion=f"max over T1 and T2 of |T_producer - T_check| / "
                  f"({GCI_SAFETY:g} (r_producer + r_check) + 64 eps |T|) "
                  "<= 1",
        criterion_derivation="Roache's factor of safety 3 on the sum of the "
                             "producer's stated resolution and the check's "
                             "own: its fourth-order Richardson estimate and "
                             "a bound on the rounding its steps accumulate",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_IMPLEMENTATION,
        establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
        shared_components=SHARED, limitations=LIMITS)
    outs = [bundle.output(n) for n in OUTPUT_NAMES]
    own = None
    if all(o.status is OutputStatus.OK for o in outs):
        own = solve(bundle.parameters)
    if own is None:
        why = ("the producer's T1 or T2 is not OK; nothing to compare"
               if any(o.status is not OutputStatus.OK for o in outs) else
               f"the network is too stiff for {MAX_STEPS} steps")
        return VerificationResult(
            **{**common, "limitations": LIMITS + (why,)},
            status=Status.NOT_RUN, measured=None, threshold=None)
    ratio = 0.0
    for o, (tf, r_check) in zip(outs, own):
        q = quantity(o)
        tol = GCI_SAFETY * (resolution(q) + r_check) + 64 * _EPS * abs(tf)
        ratio = max(ratio, abs(q.value - tf) / tol)
    return VerificationResult(
        **common, status=Status.PASS if ratio <= 1.0 else Status.FAIL,
        measured=Quantity(ratio if math.isfinite(ratio) else 1e300, "1",
                          resolution=1e-6,
                          resolution_class=RC.SINGLE_EVALUATION,
                          resolution_basis="a ratio of two stated values",
                          reporting_digits=6),
        threshold=Quantity(1.0, "1", exact=True))
