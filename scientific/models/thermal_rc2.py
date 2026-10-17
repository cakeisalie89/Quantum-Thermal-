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

    x(t) = x_s + V diag(exp(lambda_i t)) V^-1 (x0 - x_s),   x_s = -A^-1 b,

and the heat that left through G2a is G2a times the closed-form integral of
T2 - Tamb, written with expm1 so a short window does not lose its digits to
cancellation. Every output is a few dozen roundings of one closed-form
evaluation; the stated resolution is 256 eps times the scale of the
quantity, generous for the condition of a 2 x 2 eigenproblem with these
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
    """T1, T2, E_in, E_out, E_stored at time t, exactly (to rounding)."""
    C1, C2, G12, G2a = p["C1_J_K"], p["C2_J_K"], p["G12_W_K"], p["G2a_W_K"]
    Q, Ta = p["Q_W"], p["Tamb_K"]
    T10, T20 = p["T1_0_K"], p["T2_0_K"]
    a11, a12 = -G12 / C1, G12 / C1
    a21, a22 = G12 / C2, -(G12 + G2a) / C2
    # steady state: heat Q flows through G12 then G2a
    s2 = Ta + Q / G2a
    s1 = s2 + Q / G12
    tr, det = a11 + a22, a11 * a22 - a12 * a21
    disc = math.sqrt(tr * tr / 4.0 - det)
    lam = (tr / 2.0 + disc, tr / 2.0 - disc) if tr < 0 else \
        (tr / 2.0 - disc, tr / 2.0 + disc)
    # eigenvectors (a12, lambda - a11)
    vecs = [(a12, l_ - a11) for l_ in lam]
    d0 = (T10 - s1, T20 - s2)
    # solve V c = d0
    det_v = vecs[0][0] * vecs[1][1] - vecs[1][0] * vecs[0][1]
    c0 = (d0[0] * vecs[1][1] - vecs[1][0] * d0[1]) / det_v
    c1 = (vecs[0][0] * d0[1] - d0[0] * vecs[0][1]) / det_v
    cs = (c0, c1)
    T1 = s1 + sum(c * v[0] * math.exp(l_ * t)
                  for c, v, l_ in zip(cs, vecs, lam))
    T2 = s2 + sum(c * v[1] * math.exp(l_ * t)
                  for c, v, l_ in zip(cs, vecs, lam))
    integral_T2 = (s2 - Ta) * t + sum(c * v[1] * math.expm1(l_ * t) / l_
                                      for c, v, l_ in zip(cs, vecs, lam))
    E_in = Q * t
    E_out = G2a * integral_T2
    E_stored = C1 * (T1 - T10) + C2 * (T2 - T20)
    return {"T1": T1, "T2": T2, "E_in": E_in, "E_out": E_out,
            "E_stored": E_stored, "eigenvalues": lam}


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
        scale = max(abs(r["E_in"]), abs(r["E_out"]), abs(r["E_stored"]),
                    1e-300) if finite else 1.0
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
                            "bound is rounding of the exponentials"),
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
            sc = max(abs(v), abs(p["T1_0_K"]) if UNITS[name] == "K"
                     else scale)
            outputs.append(Output(name, OutputStatus.OK, Quantity(
                float(v), UNITS[name], resolution=256 * _EPS * sc,
                resolution_class=RC.SINGLE_EVALUATION,
                resolution_basis="a few dozen roundings of one closed-form "
                                 "evaluation of a 2 x 2 linear system",
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
