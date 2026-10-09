"""Independent check of the slab: the closed-form eigenfunction series.

The producer (``scientific.models.slab_transient``) integrates the slab by
cell-centred finite volume with Crank-Nicolson. This check shares none of
that: it writes the exact solution as the steady profile plus a decaying
eigenfunction expansion,

    T(x, t) = T_s(x) + sum_n c_n exp(-alpha lambda_n^2 t) cos(lambda_n x)
    T_s(x)  = T_inf + q L / h + q (L^2 - x^2) / (2 k)
    lambda_n tan(lambda_n L) = h / k          (one root in each branch)
    c_n     = <T0 - T_s, cos(lambda_n x)> / <cos, cos>

with the eigenvalues found by bracketed root-finding and the projections by
64-point Gauss-Legendre quadrature (exact for these integrands to rounding).
Terms are summed until the next one's decay factor underflows or 400 terms
are used; the tail beyond that is below exp(-alpha lambda_400^2 t), which
for any admitted parameters is far below the producer's discretization.

THE CRITERION. The producer states, per output, a Richardson estimate r of
its own discretization error (second order, fine against half resolution).
Roache's Grid Convergence Index for a two-level study multiplies such an
estimate by a factor of safety Fs = 3 (Roache 1998, "Verification and
Validation in Computational Science and Engineering", ch. 5); a disagreement
larger than GCI = 3 r means the producer is wrong by more than its own method
can explain. The series' own error is added (64 eps |T|); nothing else.

WHAT IT ESTABLISHES, AND WHAT IT DOES NOT. That the finite-volume solver
solves the stated slab equations to the accuracy it claims. It shares the
equations themselves and their boundary conditions with the producer, and
says so: a wrong model of the physics is invisible to it. It is not a
comparison with measurement.
"""
from __future__ import annotations

from typing import Any

import math
import sys

from ..identity import implementation_digest
from ..quantity import Quantity, ResolutionClass as RC
from ..result import OutputStatus, ResultBundle
from ..verification import (
    CheckType, Establishes, Independence, Status, VerificationResult,
)
from . import quantity, resolution

CHECK_ID = "thermal.slab_series"
#: the model this check was written for -- stated, not imported, so this
#: check's code identity covers none of the producer's code
MODEL_ID = "thermal.slab_transient"
MODEL_VERSION = "1.0.0"
IMPLEMENTATION_MODULES = ("scientific.checks.slab_series",)
SAMPLE_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)
SAMPLE_NAMES = ("T_at_0", "T_at_quarter", "T_at_half", "T_at_three_quarter",
                "T_at_L")
GCI_SAFETY = 3.0
MAX_TERMS = 400
_EPS = sys.float_info.epsilon

SHARED = ("the slab equations: rho c T_t = k T_xx + q, insulated at x = 0, "
          "convective at x = L",)
LIMITS = ("agreement of two solutions of one model, not of the model with "
          "measurement",
          "a wrong model of the physics is invisible to this check")


def check_digest() -> str:
    return implementation_digest(IMPLEMENTATION_MODULES)


def _bisect(f, a: float, b: float) -> float:
    """Bisection to the last representable digit: slow, unconditional, and
    a different root-finder from anything the producer uses."""
    fa = f(a)
    for _ in range(200):
        m = 0.5 * (a + b)
        if m in (a, b):
            break
        fm = f(m)
        if (fm < 0.0) == (fa < 0.0):
            a, fa = m, fm
        else:
            b = m
    return 0.5 * (a + b)


def series_temperature(p: dict, x_positions) -> list:
    L, k, rc = p["L_m"], p["k_W_m_K"], p["rho_c_J_m3_K"]
    q, h, Tinf = p["q_W_m3"], p["h_W_m2_K"], p["T_inf_K"]
    T0, t = p["T0_K"], p["t_end_s"]
    alpha = k / rc
    bi = h / k
    nodes, weights = _gauss_legendre(64)
    xq = [0.5 * L * (z + 1.0) for z in nodes]
    wq = [0.5 * L * w for w in weights]

    def steady(x):
        return Tinf + q * L / h + q * (L * L - x * x) / (2.0 * k)

    theta0 = [T0 - steady(x) for x in xq]
    out = [steady(x) for x in x_positions]
    for n in range(1, MAX_TERMS + 1):
        lo = (n - 1) * math.pi / L
        hi = (n - 0.5) * math.pi / L
        span = hi - lo
        lam = _bisect(lambda z: z * math.tan(z * L) - bi,
                     lo + 1e-12 * span, hi - 1e-12 * span)
        decay = math.exp(-alpha * lam * lam * t)
        if decay == 0.0:
            break
        phi = [math.cos(lam * x) for x in xq]
        num = sum(w * th * ph for w, th, ph in zip(wq, theta0, phi))
        den = sum(w * ph * ph for w, ph in zip(wq, phi))
        c = num / den
        for i, x in enumerate(x_positions):
            out[i] += c * decay * math.cos(lam * x)
    return out


def _gauss_legendre(n: int):
    """Nodes and weights on [-1, 1] by Newton on P_n -- written here rather
    than imported, so this check's identity is its own code."""
    nodes, weights = [], []
    for i in range(1, n + 1):
        z = math.cos(math.pi * (i - 0.25) / (n + 0.5))
        for _ in range(100):
            p1, p2 = 1.0, 0.0
            for j in range(1, n + 1):
                p1, p2 = ((2 * j - 1) * z * p1 - (j - 1) * p2) / j, p1
            dp = n * (z * p1 - p2) / (z * z - 1.0)
            dz = p1 / dp
            z -= dz
            if abs(dz) < 1e-16:
                break
        nodes.append(z)
        weights.append(2.0 / ((1.0 - z * z) * dp * dp))
    return nodes, weights


def run_check(bundle: ResultBundle, *, verifier_id: str) -> VerificationResult:
    if (bundle.model_id, bundle.model_version) != (MODEL_ID, MODEL_VERSION):
        raise ValueError(f"this check is for {MODEL_ID}@{MODEL_VERSION}, "
                         f"not {bundle.model_id}@{bundle.model_version}")
    common: dict[str, Any] = dict(
        check_id=CHECK_ID, check_type=CheckType.ANALYTIC_REFERENCE,
        subject_digest=bundle.digest(),
        subject_model=f"{bundle.model_id}@{bundle.model_version}",
        criterion=f"max over the five sample points of |T_producer - "
                  f"T_series| / (GCI + 64 eps |T|) <= 1, GCI = "
                  f"{GCI_SAFETY:g} x the producer's stated Richardson "
                  "estimate",
        criterion_derivation="Roache's two-level Grid Convergence Index "
                             "(factor of safety 3) on the producer's own "
                             "second-order estimate; the series is exact "
                             "to rounding",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_IMPLEMENTATION,
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
    xs = [f * p["L_m"] for f in SAMPLE_FRACTIONS]
    ref = series_temperature(p, xs)
    ratio = 0.0
    for o, r in zip(outs, ref):
        q = quantity(o)
        tol = GCI_SAFETY * resolution(q) + 64 * _EPS * abs(r)
        ratio = max(ratio, abs(q.value - r) / tol)
    status = Status.PASS if ratio <= 1.0 else Status.FAIL
    return VerificationResult(
        **common, status=status,
        measured=Quantity(ratio if math.isfinite(ratio) else 1e300, "1",
                          resolution=1e-6,
                          resolution_class=RC.SINGLE_EVALUATION,
                          resolution_basis="a ratio of two stated values",
                          reporting_digits=6),
        threshold=Quantity(1.0, "1", exact=True))
