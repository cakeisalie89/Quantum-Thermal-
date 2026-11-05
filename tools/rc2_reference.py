#!/usr/bin/env python3
"""The two-node RC network's temperatures by another closed form: the
analytic reference the evaluation's RC counterfactual cases are judged by.

    python tools/rc2_reference.py '{"C1_J_K": 500.0, ...}'   # T1, T2 as JSON

    C1 dT1/dt = Q - G12 (T1 - T2)
    C2 dT2/dt = G12 (T1 - T2) - G2a (T2 - Tamb)

THE COMPUTATION, AND NOT THE JUDGMENT. This file says what the network's
temperatures are at ``t_end_s``. Whether a world's answer is clear enough to
judge the research loop by, and what that answer is, is decided in
``tools/adversarial_eval.py`` (``rc_oracle``), from what this file computes.

ANOTHER CLOSED FORM. The model the loop runs
(``scientific/models/thermal_rc2.py``) solves the system by the
eigen-decomposition of its matrix: two eigenvectors, the steady state, and
the departure from it decaying along each. This file uses none of those.
With x = (T1, T2), dx/dt = A x + b and r0 = A x0 + b the initial rates of
change,

    x(t) = x0 + Phi(t) r0,       Phi(t) = integral_0^t exp(A u) du,

and Phi(t) = G0(t) I + G1(t) A by Sylvester's formula for a 2 x 2 matrix
with two distinct eigenvalues, whose coefficients are divided differences
of E(lam) = (exp(lam t) - 1) / lam. No eigenvector, no steady state and no
inverse of A is formed; the temperatures are reached as increments from
the initial state, so no two large terms cancel. The eigenvalues are
computed apart from the model's too: the discriminant as a sum of
non-negative terms, the slower root from the product of the two.

STANDALONE. The standard library only, and nothing of this repository: a
defect in the model's code cannot reach the reference that judges it.
Agreement with the model and with the independent RK4 check
(``scientific/checks/rc2_rk4.py``) is held to by
``tests/test_rc_counterfactuals.py``.

WHAT IS REFUSED. A network that is no network: a capacity or conductance
that is not positive, an absolute temperature that is not positive, a
negative time, a value that is not a finite real number, and a parameter
set with a name missing or one too many. Each is a ``ValueError``: no
answer is computed for a world that does not exist.
"""
from __future__ import annotations

import json
import math
import sys

PARAMETERS = ("C1_J_K", "C2_J_K", "G12_W_K", "G2a_W_K", "T1_0_K", "T2_0_K",
              "Q_W", "Tamb_K", "t_end_s")
#: the multiple of eps, times the size of the terms summed, that bounds this
#: evaluation's rounding (``error_K``); tests/test_rc_counterfactuals.py
#: holds it against a 90-digit evaluation across the admitted bounds
ROUNDING_FACTOR = 4.0
#: must be greater than zero: capacities, conductances, absolute temperatures
POSITIVE = ("C1_J_K", "C2_J_K", "G12_W_K", "G2a_W_K", "T1_0_K", "T2_0_K",
            "Tamb_K")


def _finite(v) -> bool:
    """A number a float holds, and holds finitely: a JSON integer too
    large for a float is refused, where ``math.isfinite`` would raise."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def checked(p: dict) -> dict:
    """``p``, refused unless it is a network: exactly :data:`PARAMETERS`,
    each a finite real number, positive where :data:`POSITIVE` says, and a
    time that is not negative."""
    if not isinstance(p, dict):
        raise ValueError("a network is an object of named parameters")
    missing = sorted(set(PARAMETERS) - set(p))
    extra = sorted(set(p) - set(PARAMETERS))
    if missing or extra:
        raise ValueError(f"no such network: missing {missing}, "
                         f"unknown {extra}")
    for k in PARAMETERS:
        v = p[k]
        if not _finite(v):
            raise ValueError(f"no such network: {k} = {v!r} is not a "
                             "finite real number")
    for k in POSITIVE:
        if not p[k] > 0:
            raise ValueError(f"no such network: {k} = {p[k]!r} must be "
                             "greater than zero")
    if p["t_end_s"] < 0:
        raise ValueError(f"no such network: t_end_s = {p['t_end_s']!r} is "
                         "before the start")
    return {k: float(p[k]) for k in PARAMETERS}


def _e(lam: float, t: float) -> float:
    """E(lam) = integral_0^t exp(lam u) du, for lam < 0."""
    return math.expm1(lam * t) / lam


def temperatures(parameters: dict) -> dict:
    """{"T1": ..., "T2": ...} in K at ``t_end_s``, and ``error_K``, a bound
    on the rounding either carries."""
    p = checked(parameters)
    C1, C2, G12, G2a = p["C1_J_K"], p["C2_J_K"], p["G12_W_K"], p["G2a_W_K"]
    Q, Ta, t = p["Q_W"], p["Tamb_K"], p["t_end_s"]
    x1, x2 = p["T1_0_K"], p["T2_0_K"]
    # the matrix, by its entries
    a11, a12 = -G12 / C1, G12 / C1
    a21, a22 = G12 / C2, -(G12 + G2a) / C2
    # its eigenvalues: half the trace, less the square root of a
    # discriminant written as a sum of non-negative terms (a12 a21 > 0);
    # the slower one from the product of the two, det A = (G12 G2a) /
    # (C1 C2) -- from the parameters, not as a difference of products
    fast = 0.5 * (a11 + a22) - math.sqrt((0.5 * (a11 - a22)) ** 2
                                         + a12 * a21)
    slow = (G12 * G2a) / (C1 * C2) / fast
    gap = slow - fast                                      # > 0
    # Phi(t) = G0 I + G1 A, Sylvester's coefficients integrated
    e_slow, e_fast = _e(slow, t), _e(fast, t)
    g1 = (e_slow - e_fast) / gap
    g0 = e_slow - slow * g1
    # the initial rates of change, r0 = A x0 + b, and A r0
    r1 = (Q - G12 * (x1 - x2)) / C1
    r2 = (G12 * (x1 - x2) - G2a * (x2 - Ta)) / C2
    s1 = a11 * r1 + a12 * r2
    s2 = a21 * r1 + a22 * r2
    # What the rounding of this evaluation can come to: ROUNDING_FACTOR eps
    # times the size of every term it adds, with what their own rounding
    # brings -- the rates' parts, magnified by Phi and A, and the divided
    # difference's parts, magnified by 1 / gap. A stiff network makes the
    # initial rates large and their cancellation the whole answer, and the
    # bound grows with it.
    # (the inputs are exact: x1 - x2 and x2 - Ta are each rounded once)
    rates = max((abs(Q) + G12 * abs(x1 - x2)) / C1,
                (G12 * abs(x1 - x2) + G2a * abs(x2 - Ta)) / C2)
    norm_a = max(abs(a11) + a12, a21 + abs(a22))
    d_g1 = (abs(e_slow) + abs(e_fast)) / gap
    d_g0 = abs(e_slow) + abs(slow) * d_g1
    out = {}
    size = 0.0
    for name, x, r, s in (("T1", x1, r1, s1), ("T2", x2, r2, s2)):
        out[name] = x + g0 * r + g1 * s
        size = max(size, abs(x) + abs(g0 * r) + abs(g1 * s)
                   + (abs(g0) + abs(g1) * norm_a) * rates
                   + d_g0 * abs(r) + d_g1 * abs(s))
    out["error_K"] = ROUNDING_FACTOR * sys.float_info.epsilon * size
    return out


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: rc2_reference.py '<parameters as a JSON object>'",
              file=sys.stderr)
        return 2
    try:
        out = temperatures(json.loads(args[0]))
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
