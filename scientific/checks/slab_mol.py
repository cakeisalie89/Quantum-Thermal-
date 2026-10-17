"""Independent check of the slab: a method-of-lines solve by another
discretization and another integrator.

The producer (``scientific.models.slab_transient``) is CELL-centred finite
volume with a fixed-step Crank-Nicolson march and its own Thomas solver.
This check is VERTEX-centred finite differences on N + 1 nodes, the two
boundary conditions closed by ghost nodes,

    insulated, x = 0:   T_{-1} = T_1
    convective, x = L:  k (T_{N+1} - T_{N-1}) / (2 dx) = -h (T_N - T_inf)

integrated in time by SciPy's adaptive, variable-order BDF with a banded
Jacobian at rtol 1e-10. It shares the slab EQUATIONS with the producer and
nothing of the method: not the grid, not the boundary closure, not the time
integrator, not the linear solver.

ITS OWN RESOLUTION. The solve is done on N and on 2N intervals and the
2N solution is the check's answer; for a second-order spatial scheme its
error is estimated as |T_N - T_2N| / 3 (Richardson: the estimate belongs to
the FINER of the two, and is four times too small for the coarser). The five
samples sit exactly on nodes (N is a multiple of 4: no interpolation).

THE CRITERION. At each sample, |T_producer - T_check| <= 3 (r_producer +
r_check) + 64 eps |T|: Roache's factor of safety on the sum of both stated
discretization estimates. A disagreement larger than that is more than
either method can explain.

WHAT IT ESTABLISHES, AND WHAT IT DOES NOT. Independent numerical agreement
of two discretizations of one model. A wrong model of the physics is
invisible to it; it is not a comparison with measurement.
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

CHECK_ID = "thermal.slab_method_of_lines"
#: the model this check was written for -- stated, not imported, so this
#: check's code identity covers none of the producer's code
MODEL_ID = "thermal.slab_transient"
MODEL_VERSION = "1.0.0"
IMPLEMENTATION_MODULES = ("scientific.checks.slab_mol",)
SAMPLE_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)
SAMPLE_NAMES = ("T_at_0", "T_at_quarter", "T_at_half", "T_at_three_quarter",
                "T_at_L")
GCI_SAFETY = 3.0
NODES = 160          # intervals; a multiple of 4, so samples are nodes
RTOL = 1e-10
_EPS = sys.float_info.epsilon

SHARED = ("the slab equations: rho c T_t = k T_xx + q, insulated at x = 0, "
          "convective at x = L",)
LIMITS = ("agreement of two discretizations of one model, not of the model "
          "with measurement",
          "a wrong model of the physics is invisible to this check")


def check_digest() -> str:
    return implementation_digest(IMPLEMENTATION_MODULES)


def mol_temperature(p: dict, intervals: int) -> list:
    """T at the five sample fractions after ``t_end_s``, from N + 1 nodes."""
    import numpy as np
    from scipy.integrate import solve_ivp
    from scipy.sparse import diags

    if intervals % 4:
        raise ValueError("the node count must put every sample on a node")
    L, k, rc = p["L_m"], p["k_W_m_K"], p["rho_c_J_m3_K"]
    q, h, Tinf = p["q_W_m3"], p["h_W_m2_K"], p["T_inf_K"]
    n = intervals
    dx = L / n
    a = k / (rc * dx * dx)
    main = np.full(n + 1, -2.0 * a)
    upper = np.full(n, a)
    lower = np.full(n, a)
    upper[0] = 2.0 * a                     # ghost T_{-1} = T_1
    lower[-1] = 2.0 * a                    # ghost from the convective face
    main[-1] = -2.0 * a - 2.0 * a * dx * h / k
    A = diags([lower, main, upper], [-1, 0, 1], format="csr")
    b = np.full(n + 1, q / rc)
    b[-1] += 2.0 * a * dx * h / k * Tinf

    def rhs(_t, T):
        return A @ T + b

    sol = solve_ivp(rhs, (0.0, p["t_end_s"]),
                    np.full(n + 1, float(p["T0_K"])), method="BDF",
                    jac=A, rtol=RTOL, atol=RTOL * abs(p["T0_K"]),
                    t_eval=[p["t_end_s"]])
    if not sol.success:
        raise RuntimeError(f"BDF did not reach t_end: {sol.message}")
    T = sol.y[:, -1]
    return [float(T[round(f * n)]) for f in SAMPLE_FRACTIONS]


def run_check(bundle: ResultBundle, *, verifier_id: str) -> VerificationResult:
    if (bundle.model_id, bundle.model_version) != (MODEL_ID, MODEL_VERSION):
        raise ValueError(f"this check is for {MODEL_ID}@{MODEL_VERSION}, "
                         f"not {bundle.model_id}@{bundle.model_version}")
    common: dict[str, Any] = dict(
        check_id=CHECK_ID, check_type=CheckType.INDEPENDENT_IMPLEMENTATION,
        subject_digest=bundle.digest(),
        subject_model=f"{bundle.model_id}@{bundle.model_version}",
        criterion=f"max over the five sample points of |T_producer - "
                  f"T_check| / ({GCI_SAFETY:g} (r_producer + r_check) + "
                  "64 eps |T|) <= 1",
        criterion_derivation="Roache's factor of safety 3 on the sum of the "
                             "two stated second-order Richardson estimates",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_DISCRETIZATION,
        establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
        shared_components=SHARED, limitations=LIMITS)
    outs = [bundle.output(n) for n in SAMPLE_NAMES]
    if any(o.status is not OutputStatus.OK for o in outs) or any(
            quantity(o).resolution_class is not RC.DISCRETIZATION_ESTIMATE
            for o in outs):
        return VerificationResult(
            **{**common, "limitations": LIMITS + (
                "the producer's sample outputs are not OK or state no "
                "discretization estimate; nothing to compare",)},
            status=Status.NOT_RUN, measured=None, threshold=None)
    p = bundle.parameters
    coarse = mol_temperature(p, NODES)
    fine = mol_temperature(p, 2 * NODES)
    ratio = 0.0
    for o, tc, tf in zip(outs, coarse, fine):
        q = quantity(o)
        r_check = abs(tc - tf) / 3.0       # the error of tf, not of tc
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
