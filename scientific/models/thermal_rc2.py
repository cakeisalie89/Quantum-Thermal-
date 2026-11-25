"""A two-node thermal RC network, solved in closed form.

    C1 dT1/dt = Q - G12 (T1 - T2)
    C2 dT2/dt = G12 (T1 - T2) - G2a (T2 - Tamb)

with Q and Tamb constant. A generic lumped model -- two heat capacities, a
conductance between them, one to ambient -- and the reference against which
the FMI 3.0 export of the same network (``integrations/fmi/thermal_rc2``, C,
classical RK4) is verified. The two share the equations and nothing else:
this file solves the linear system exactly, by the eigen-decomposition of
its 2 x 2 matrix, the FMU integrates it numerically.

THE SOLUTION. x = (T1, T2), dx/dt = A x + b, A has two real, distinct,
negative eigenvalues for any admitted (positive) parameters, so

    x(t) = x0 + V diag(expm1(lambda_i t)) V^-1 (x0 - x_s),   x_s = -A^-1 b,

the departure from the initial state, written with expm1 so a short window
does not lose its digits; the heat that left through G2a is Q t plus G2a
times the closed-form integral of the decaying modes of T2.

WITHOUT CANCELLATION (AUD-024). Until this was found, the slow eigenvalue
was taken as tr/2 + disc and each eigenvector as (a12, lambda - a11): both
differences of nearly equal numbers whenever the two time constants are far
apart. With capacities a thousand to one apart, runs whose every invariant
held stated T1 and T2 a thousand times closer to the exact value than they
were; and 870 of 5000 networks drawn across the admitted bounds divided by
an eigenvalue that had cancelled to zero. Now the discriminant is a sum of
non-negative terms, the fast root a sum of negative ones, the slow root
comes from the product of the roots, det A = (G12 / C1)(G2a / C2), and for
each root whichever of lambda - a11 and lambda - a22 is a sum of like-signed
terms is computed, the other from their product, a12 a21.

THE STATED RESOLUTION is 256 eps times the scale of the terms the
computation passes through, not of the output alone: the initial and
steady states, the modal amplitudes V^-1 (x0 - x_s) magnified by how
nearly parallel the eigenvectors are, and, for the energies, the heat
capacities times the temperature change those amplitudes carry. A steady
state 1e16 K away moves a temperature of 300 K by eps 1e16; a stored energy
over a microsecond is the difference of nothing a temperature can resolve
-- and the resolution says so. ``tests/test_rc_counterfactuals.py`` holds
every output to it against a 90-digit evaluation, across the admitted
bounds.

MODEL-ONLY. Every parameter is declared by the caller; nothing is measured.
"""
from __future__ import annotations

import math
import sys

from ..backend_probe import run_environment
from ..identity import digest
from ..model import (
    Applicability, CheckSpec, InvariantSpec, ModelBase, Parameter,
    ParameterSchema, run_identity_for,
)
from ..quantity import Quantity, ResolutionClass as RC
from ..result import InvariantResult, Output, OutputStatus, ResultBundle
from ..run_identity import environment_digest

MODEL_ID = "thermal.rc2_network"
MODEL_VERSION = "1.0.0"
OUTPUTS = ("T1", "T2", "E_in", "E_out", "E_stored")
UNITS = {"T1": "K", "T2": "K", "E_in": "J", "E_out": "J", "E_stored": "J"}
#: the exported variables, by FMI name: (causality, variability, unit). The
#: FMU's modelDescription is checked against THIS table (FMI-P5).
FMI_VARIABLES = {
    "C1": ("parameter", "fixed", "J/K"),
    "C2": ("parameter", "fixed", "J/K"),
    "G12": ("parameter", "fixed", "W/K"),
    "G2a": ("parameter", "fixed", "W/K"),
    "T1_0": ("parameter", "fixed", "K"),
    "T2_0": ("parameter", "fixed", "K"),
    "Q": ("input", "continuous", "W"),
    "Tamb": ("input", "continuous", "K"),
    "T1": ("output", "continuous", "K"),
    "T2": ("output", "continuous", "K"),
    "E_in": ("output", "continuous", "J"),
    "E_out": ("output", "continuous", "J"),
    "E_stored": ("output", "continuous", "J"),
}
BALANCE_MAX = 1e-10
_EPS = sys.float_info.epsilon


def closed_form(p: dict, t: float) -> dict:
    """T1, T2, E_in, E_out, E_stored at time t, exactly (to rounding), the
    eigenvalues, and the scales the rounding is relative to: ``scale_K``
    for the temperatures and ``scale_J`` for the energies."""
    C1, C2, G12, G2a = p["C1_J_K"], p["C2_J_K"], p["G12_W_K"], p["G2a_W_K"]
    Q, Ta = p["Q_W"], p["Tamb_K"]
    T10, T20 = p["T1_0_K"], p["T2_0_K"]
    a11, a12 = -G12 / C1, G12 / C1
    a21, a22 = G12 / C2, -(G12 + G2a) / C2
    # steady state: heat Q flows through G12 then G2a
    s2 = Ta + Q / G2a
    s1 = s2 + Q / G12
    # the eigenvalues, (slow, fast), with no difference of near-equals
    h, q = 0.5 * (a11 - a22), a12 * a21
    disc = math.sqrt(h * h + q)
    fast = 0.5 * (a11 + a22) - disc
    lam = ((G12 / C1) * (G2a / C2) / fast, fast)
    # eigenvectors (a12, lambda - a11), from (lambda - a11)(lambda - a22)
    # = a12 a21 where lambda - a11 itself would cancel
    mus = (q / (h + disc), -h - disc) if h >= 0 else \
        (disc - h, q / (h - disc))
    vecs = [(a12, m) for m in mus]
    d0 = (T10 - s1, T20 - s2)
    # solve V c = d0; det V = a12 (fast - slow) = -2 a12 disc
    det_v = -2.0 * a12 * disc
    cs = ((d0[0] * mus[1] - a12 * d0[1]) / det_v,
          (a12 * d0[1] - d0[0] * mus[0]) / det_v)
    xs = [math.expm1(l_ * t) for l_ in lam]
    ints = [x / l_ for x, l_ in zip(xs, lam)]   # integral_0^t exp(lambda u)
    dT1 = sum(c * v[0] * x for c, v, x in zip(cs, vecs, xs))
    dT2 = sum(c * v[1] * x for c, v, x in zip(cs, vecs, xs))
    T1, T2 = T10 + dT1, T20 + dT2
    E_in = Q * t
    E_out = Q * t + G2a * sum(c * v[1] * i for c, v, i in zip(cs, vecs, ints))
    E_stored = C1 * dT1 + C2 * dT2
    # the size of what was rounded: the states, and the modal amplitudes
    # magnified by how nearly parallel the eigenvectors are
    modal = ((max(abs(d0[0]), abs(d0[1])) + max(abs(s1), abs(s2)))
             * (1.0 + (a12 + a21) / (2.0 * disc)))
    moved = modal * max(abs(xs[0]), abs(xs[1]))
    return {"T1": T1, "T2": T2, "E_in": E_in, "E_out": E_out,
            "E_stored": E_stored, "eigenvalues": lam,
            "scale_K": max(abs(T1), abs(T2), abs(T10), abs(T20), moved),
            "scale_J": max(abs(E_in), abs(E_out), abs(E_stored),
                           (C1 + C2) * moved,
                           G2a * modal * max(abs(ints[0]), abs(ints[1])),
                           1e-300)}


class ThermalRC2Model(ModelBase):
    model_id = MODEL_ID
    model_version = MODEL_VERSION
    implementation_modules = ("scientific.models.thermal_rc2",)
    parameter_schema = ParameterSchema((
        Parameter("C1_J_K", "float", unit="J K^-1", minimum=1e-6,
                  maximum=1e12, default=500.0,
                  description="heat capacity of node 1"),
        Parameter("C2_J_K", "float", unit="J K^-1", minimum=1e-6,
                  maximum=1e12, default=2000.0,
                  description="heat capacity of node 2"),
        Parameter("G12_W_K", "float", unit="W K^-1", minimum=1e-9,
                  maximum=1e9, default=5.0,
                  description="conductance between the nodes"),
        Parameter("G2a_W_K", "float", unit="W K^-1", minimum=1e-9,
                  maximum=1e9, default=2.0,
                  description="conductance from node 2 to ambient"),
        Parameter("T1_0_K", "float", unit="K", minimum=1e-3, maximum=1e5,
                  default=300.0, description="initial temperature, node 1"),
        Parameter("T2_0_K", "float", unit="K", minimum=1e-3, maximum=1e5,
                  default=300.0, description="initial temperature, node 2"),
        Parameter("Q_W", "float", unit="W", minimum=-1e9, maximum=1e9,
                  default=10.0, description="constant heat input, node 1"),
        Parameter("Tamb_K", "float", unit="K", minimum=1e-3, maximum=1e5,
                  default=300.0, description="ambient temperature"),
        Parameter("t_end_s", "float", unit="s", minimum=0.0, maximum=1e12,
                  default=3600.0, description="simulated time"),
    ))
    invariants = (
        InvariantSpec("finite", "every output is finite",
                      "non-finite count == 0"),
        InvariantSpec("energy_balance", "E_in - E_out = E_stored",
                      f"|E_in - E_out - E_stored| / scale <= {BALANCE_MAX}"),
        InvariantSpec("stable", "both eigenvalues are negative",
                      "max eigenvalue < 0"),
    )
    independent_checks = (
        CheckSpec("thermal.rc2_fmu",
                  "the FMI 3.0 export of the same network (C, classical "
                  "RK4), loaded and run by fmpy, an independent FMI "
                  "runtime, against this closed form",
                  "DIFFERENT_IMPLEMENTATION",
                  shared_components=("the network equations",),
                  limitations=("agreement of two solutions of one model, "
                               "not of the model with measurement",)),
        CheckSpec("thermal.rc2_rk4",
                  "the same two equations integrated by classical "
                  "fourth-order Runge-Kutta in pure Python, at a step "
                  "chosen from a Gershgorin bound, with its own Richardson "
                  "and rounding estimate: no eigenvalue, eigenvector or "
                  "steady state of this closed form",
                  "DIFFERENT_IMPLEMENTATION",
                  shared_components=("the network equations",),
                  limitations=("agreement of two solutions of one model, "
                               "not of the model with measurement",)),
    )
    applicability = Applicability(
        "two lumped heat capacities in series between a heat input and an "
        "ambient, constant input and ambient temperature, constant "
        "conductances",
        bounds={"t_end_s": [0.0, 1e12]},
        excludes=("temperature-dependent properties", "radiation",
                  "time-varying inputs within one run",
                  "any measured temperature"))

    def evaluate_outputs(self, inputs: dict) -> tuple:
        """``(parameters, outputs, invariants)`` exactly as :meth:`run`
        publishes them, without the run's identity -- for an analysis that
        evaluates the response many times (scientific/sensitivity.py).
        ``run`` is this plus the environment and run identity."""
        p = self.validate(inputs)
        r = closed_form(p, p["t_end_s"])
        vals = [r[k] for k in OUTPUTS]
        nonfinite = sum(1 for v in vals if not math.isfinite(v))
        finite = nonfinite == 0
        # the energies' rounding scale (closed_form's ``scale_J``), not
        # their size: a balance over a microsecond is the difference of
        # nothing the temperatures can resolve (AUD-024)
        scale = r["scale_J"] if finite and math.isfinite(r["scale_J"]) \
            else 1.0
        gap = abs(r["E_in"] - r["E_out"] - r["E_stored"]) / scale \
            if finite else math.inf
        lam_max = max(r["eigenvalues"])
        invariants = (
            InvariantResult("finite", finite,
                            Quantity(float(nonfinite), "1", exact=True),
                            "non-finite count == 0",
                            Quantity(0.0, "1", exact=True),
                            "counted over the five outputs"),
            InvariantResult("energy_balance", bool(gap <= BALANCE_MAX),
                            Quantity(gap if math.isfinite(gap) else 1e300,
                                     "1"),
                            f"|E_in - E_out - E_stored| / scale <= "
                            f"{BALANCE_MAX}",
                            Quantity(BALANCE_MAX, "1"),
                            "the closed form conserves heat exactly; the "
                            "bound is rounding, relative to the scale of "
                            "the terms the energies are computed from"),
            InvariantResult("stable", bool(lam_max < 0.0),
                            Quantity(lam_max, "s^-1"),
                            "max eigenvalue < 0",
                            Quantity(0.0, "s^-1", exact=True),
                            "positive capacities and conductances make A "
                            "negative definite"),
        )
        outputs = []
        for name in OUTPUTS:
            v = r[name]
            if not finite:
                outputs.append(Output(name, OutputStatus.FAILED,
                                      reason="non-finite closed form"))
                continue
            sc = r["scale_K"] if UNITS[name] == "K" else scale
            outputs.append(Output(name, OutputStatus.OK, Quantity(
                float(v), UNITS[name], resolution=256 * _EPS * sc,
                resolution_class=RC.SINGLE_EVALUATION,
                resolution_basis="a few dozen roundings of one closed-form "
                                 "evaluation of a 2 x 2 linear system, "
                                 "relative to the largest term it passes "
                                 "through: the states, and the modal "
                                 "amplitudes as the eigenvectors' "
                                 "conditioning magnifies them",
                reporting_digits=12)))
        return p, tuple(outputs), invariants

    def run(self, inputs: dict) -> ResultBundle:
        p, outputs, invariants = self.evaluate_outputs(inputs)
        env = run_environment()
        return ResultBundle(
            model_id=self.model_id, model_version=self.model_version,
            implementation_digest=self.implementation_digest(),
            parameter_digest=digest(p),
            environment_digest=environment_digest(env),
            parameters=dict(p), outputs=outputs,
            invariants=invariants,
            convergence={"method": "closed form (2 x 2 eigen-decomposition)"},
            solver_config={},
            warnings=("MODEL-ONLY: a forecast from declared inputs, not a "
                      "measured temperature",),
            artifacts=(),
            provenance={"solver": "scientific.models.thermal_rc2.closed_form",
                        "environment": env,
                        "run_identity":
                            run_identity_for(self, p).to_record()})
