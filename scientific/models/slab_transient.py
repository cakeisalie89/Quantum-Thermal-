"""Transient conduction in a plane slab: a generic model with checks that can
disagree with it.

    rho c dT/dt = k d2T/dx2 + q        0 < x < L
    -k dT/dx = 0                       at x = 0  (insulated)
    -k dT/dx = h (T - T_inf)           at x = L  (convective)
    T(x, 0) = T0

Constant properties, uniform volumetric heating q. Nothing in it belongs to
an apparatus: it is the textbook slab, chosen because three implementations
of it that share no code are possible -- this finite-volume solver, the
closed-form eigenfunction series (``scientific.checks.slab_series``) and a
finite-element solve in FEniCSx (``scientific.checks.fenicsx_slab``) -- so a
wrong answer from any one of them has two chances to be caught.

THE METHOD. Cell-centred finite volume on n uniform cells, Crank-Nicolson in
time with m equal steps: second order in both, unconditionally stable. The
convective face is closed with the half-cell conduction resistance in series
with the film resistance, so the scheme is exactly conservative: the heat
that entered, minus the heat that left (trapezoid-integrated, consistent
with Crank-Nicolson), equals the change of the stored heat to rounding.

ITS OWN RESOLUTION. Every output carries a DISCRETIZATION_ESTIMATE: the run
is repeated on n/2 cells and m/2 steps, and for a second-order method the
error of the fine run is estimated as |T_h - T_2h| / 3 (Richardson). That
estimate, not a tolerance chosen after looking at a discrepancy, is what a
check compares against.

OUTPUTS are point temperatures at x = 0, L/4, L/2, 3L/4 and L, read from the
cell-centred field by linear interpolation (second order, consistent with the
method), with the insulated face closed by symmetry and the convective face
by its face temperature; and the energy terms per unit area.

MODEL-ONLY. Every parameter is declared by the caller; nothing here is
measured.
"""
from __future__ import annotations

import math
import sys

from ..backend_probe import run_environment
from ..identity import digest, digest_bytes
from ..model import (
    Applicability, CheckSpec, InvariantSpec, ModelBase, Parameter,
    ParameterSchema, run_identity_for,
)
from ..quantity import Quantity, ResolutionClass as RC
from ..result import (ArtifactRef, InvariantResult, Output, OutputStatus,
                      ResultBundle)
from ..run_identity import environment_digest

MODEL_ID = "thermal.slab_transient"
MODEL_VERSION = "1.0.0"

#: the five fixed sample positions, as fractions of L
SAMPLE_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)
SAMPLE_NAMES = ("T_at_0", "T_at_quarter", "T_at_half", "T_at_three_quarter",
                "T_at_L")
#: Crank-Nicolson with a trapezoid boundary flux is conservative to rounding;
#: n_steps x n_cells accumulated roundings of order eps stay far below this.
ENERGY_RESIDUAL_MAX = 1e-9
_EPS = sys.float_info.epsilon


FIELD_ARTIFACT = "temperature_field"


def _field_bytes(x, T) -> bytes:
    import json

    import numpy as np
    xs = np.ascontiguousarray(x, dtype="<f8")
    ts = np.ascontiguousarray(T, dtype="<f8")
    head = json.dumps({"n": int(xs.size), "columns": ["x", "T"],
                       "units": {"x": "m", "T": "K"}, "dtype": "<f8",
                       "location": "cell centres"}, sort_keys=True)
    return b"QTSL1\n" + head.encode() + b"\n" + xs.tobytes() + ts.tobytes()


def _solve(p: dict, n: int, m: int):
    """(x_centres, T_final, (E_in, E_out, dU)) per unit area. Pure Python
    over NumPy arrays; the tridiagonal system is solved by the Thomas
    algorithm, written out, so the method is the code in this file."""
    import numpy as np
    L, k, rc = p["L_m"], p["k_W_m_K"], p["rho_c_J_m3_K"]
    q, h, Tinf = p["q_W_m3"], p["h_W_m2_K"], p["T_inf_K"]
    T0, t_end = p["T0_K"], p["t_end_s"]
    dx = L / n
    dt = t_end / m
    G = k / dx
    Gb = 1.0 / (0.5 * dx / k + 1.0 / h)
    cap = rc * dx
    # tridiagonal operator A (net conductive power into each cell)
    diag = np.full(n, -2.0 * G)
    diag[0] = -G
    diag[-1] = -G - Gb
    off = np.full(n - 1, G)
    a_lo = -0.5 * dt / cap * off          # implicit side, (I - dt/2C A)
    a_di = 1.0 - 0.5 * dt / cap * diag
    a_up = a_lo.copy()
    src = np.full(n, q * dx)
    src[-1] += Gb * Tinf
    # Thomas factorization once: the matrix does not change
    c_star = np.empty(n - 1)
    d_den = np.empty(n)
    d_den[0] = a_di[0]
    c_star[0] = a_up[0] / d_den[0]
    for i in range(1, n):
        d_den[i] = a_di[i] - a_lo[i - 1] * c_star[i - 1]
        if i < n - 1:
            c_star[i] = a_up[i] / d_den[i]
    T = np.full(n, float(T0))
    E_out = 0.0
    for _ in range(m):
        AT = diag * T
        AT[1:] += off * T[:-1]
        AT[:-1] += off * T[1:]
        rhs = T + 0.5 * dt / cap * AT + dt / cap * src
        y = np.empty(n)
        y[0] = rhs[0] / d_den[0]
        for i in range(1, n):
            y[i] = (rhs[i] - a_lo[i - 1] * y[i - 1]) / d_den[i]
        for i in range(n - 2, -1, -1):
            y[i] -= c_star[i] * y[i + 1]
        E_out += 0.5 * dt * Gb * ((T[-1] - Tinf) + (y[-1] - Tinf))
        T = y
    x = (np.arange(n) + 0.5) * dx
    E_in = q * L * t_end
    dU = float(np.sum(cap * (T - T0)))
    return x, T, (E_in, E_out, dU)


def _samples(p: dict, x, T) -> list:
    """T at the fixed positions, by linear interpolation on the centres with
    the insulated face closed by symmetry and the convective face by its
    face temperature."""
    import numpy as np
    L, k, h, Tinf = p["L_m"], p["k_W_m_K"], p["h_W_m2_K"], p["T_inf_K"]
    dx = x[1] - x[0]
    g = k / (0.5 * dx)
    T_face = (g * T[-1] + h * Tinf) / (g + h)
    xa = np.concatenate([[-x[0]], x, [L]])
    Ta = np.concatenate([[T[0]], T, [T_face]])
    return [float(np.interp(f * L, xa, Ta)) for f in SAMPLE_FRACTIONS]


class SlabTransientModel(ModelBase):
    model_id = MODEL_ID
    model_version = MODEL_VERSION
    implementation_modules = ("scientific.models.slab_transient",)
    parameter_schema = ParameterSchema((
        Parameter("L_m", "float", unit="m", minimum=1e-6, maximum=10.0,
                  description="slab thickness"),
        Parameter("k_W_m_K", "float", unit="W m^-1 K^-1", minimum=1e-3,
                  maximum=1e4, description="thermal conductivity"),
        Parameter("rho_c_J_m3_K", "float", unit="J m^-3 K^-1", minimum=1.0,
                  maximum=1e9, description="volumetric heat capacity"),
        Parameter("q_W_m3", "float", unit="W m^-3", minimum=0.0,
                  maximum=1e12, description="uniform volumetric heating"),
        Parameter("h_W_m2_K", "float", unit="W m^-2 K^-1", minimum=1e-6,
                  maximum=1e8,
                  description="film coefficient at the convective face"),
        Parameter("T_inf_K", "float", unit="K", minimum=1e-3, maximum=1e5,
                  description="ambient temperature at the convective face"),
        Parameter("T0_K", "float", unit="K", minimum=1e-3, maximum=1e5,
                  description="uniform initial temperature"),
        Parameter("t_end_s", "float", unit="s", minimum=1e-9, maximum=1e12,
                  description="simulated time"),
        Parameter("n_cells", "int", unit="1", minimum=8, maximum=20000,
                  default=80, description="finite-volume cells (even)"),
        Parameter("n_steps", "int", unit="1", minimum=8, maximum=10000000,
                  default=240, description="Crank-Nicolson steps (even)"),
    ))
    invariants = (
        InvariantSpec("finite", "every temperature is finite",
                      "non-finite count == 0"),
        InvariantSpec("energy_balance",
                      "heat in - heat out = change of stored heat",
                      f"|residual| / max(|E_in|, |dU|) <= "
                      f"{ENERGY_RESIDUAL_MAX}"),
        InvariantSpec("minimum_principle",
                      "with q >= 0 no temperature falls below "
                      "min(T0, T_inf)",
                      "largest undershoot <= 64 eps max|T|"),
    )
    independent_checks = (
        CheckSpec("thermal.slab_series",
                  "the closed-form eigenfunction series of the same "
                  "problem, against this solver's outputs at its own "
                  "discretization estimate",
                  "DIFFERENT_IMPLEMENTATION",
                  shared_components=("the slab equations and their "
                                     "boundary conditions",),
                  limitations=("agreement of two solutions of one model, "
                               "not of the model with measurement",)),
        CheckSpec("thermal.slab_fenicsx",
                  "a finite-element solve in FEniCSx (dolfinx) on a 2D "
                  "domain whose lateral sides are insulated, run in its "
                  "own environment",
                  "DIFFERENT_DISCRETIZATION",
                  shared_components=("the slab equations and their "
                                     "boundary conditions",),
                  limitations=("agreement of two discretizations of one "
                               "model, not of the model with "
                               "measurement",)),
    )
    applicability = Applicability(
        "one-dimensional transient conduction in a homogeneous slab with "
        "constant properties, uniform volumetric heating, one insulated and "
        "one convective face",
        bounds={"L_m": [1e-6, 10.0]},
        excludes=("temperature-dependent properties", "radiation",
                  "phase change", "spatially varying heating",
                  "any measured temperature"))

    def validate(self, inputs: dict) -> dict:
        p = super().validate(inputs)
        for name in ("n_cells", "n_steps"):
            if p[name] % 2:
                raise ValueError(f"{name} must be even: the discretization "
                                 "estimate repeats the run at half "
                                 "resolution")
        return p

    def run(self, inputs: dict) -> ResultBundle:
        return self.run_with_artifacts(inputs)[0]

    def run_with_artifacts(self, inputs: dict) -> tuple:
        """The bundle, and the final temperature field as an artefact: the
        bundle cites it by digest and size and never carries it inline."""
        p = self.validate(inputs)
        n, m = p["n_cells"], p["n_steps"]
        x, T, (E_in, E_out, dU) = _solve(p, n, m)
        xc, Tc, _ = _solve(p, n // 2, m // 2)
        fine = _samples(p, x, T)
        coarse = _samples(p, xc, Tc)
        import numpy as np
        nonfinite = int(np.count_nonzero(~np.isfinite(T)))
        finite = nonfinite == 0
        tmax = float(np.max(np.abs(T))) if finite else math.inf
        floor = min(p["T0_K"], p["T_inf_K"])
        under = max(0.0, floor - float(np.min(T))) if finite else math.inf
        resid = float((E_in - E_out - dU)
                      / max(abs(E_in), abs(dU), 1e-300)) \
            if finite else math.inf
        rounding = 64 * _EPS * tmax if finite else math.inf

        def clip(v):
            return v if math.isfinite(v) else 1e300

        invariants = (
            InvariantResult("finite", finite,
                            Quantity(float(nonfinite), "1", exact=True),
                            "non-finite count == 0",
                            Quantity(0.0, "1", exact=True),
                            "counted over the final field"),
            InvariantResult("energy_balance",
                            bool(abs(resid) <= ENERGY_RESIDUAL_MAX),
                            Quantity(clip(abs(resid)), "1"),
                            f"|residual| <= {ENERGY_RESIDUAL_MAX}",
                            Quantity(ENERGY_RESIDUAL_MAX, "1"),
                            "Crank-Nicolson with the trapezoid boundary "
                            "flux conserves heat exactly; the bound is "
                            "accumulated rounding with margin"),
            InvariantResult("minimum_principle",
                            bool(p["q_W_m3"] < 0.0 or under <= rounding),
                            Quantity(clip(under), "K"),
                            "largest undershoot below min(T0, T_inf) <= "
                            "64 eps max|T|",
                            Quantity(clip(rounding), "K"),
                            "q >= 0 and both the initial field and the "
                            "ambient are >= the floor"),
        )
        outputs = []
        for name, f_val, c_val in zip(SAMPLE_NAMES, fine, coarse):
            if not finite:
                outputs.append(Output(name, OutputStatus.FAILED,
                                      reason=f"{nonfinite} non-finite cells"))
                continue
            est = max(abs(f_val - c_val) / 3.0, 64 * _EPS * abs(f_val))
            outputs.append(Output(name, OutputStatus.OK, Quantity(
                f_val, "K", resolution=est,
                resolution_class=RC.DISCRETIZATION_ESTIMATE,
                resolution_basis=f"Richardson, second order: |T(n={n}, "
                                 f"m={m}) - T(n={n // 2}, m={m // 2})| / 3",
                reporting_digits=10)))
        if finite:
            scale = max(abs(E_in), abs(dU))
            for name, v in (("energy_in", E_in), ("energy_out", E_out),
                            ("stored_energy_change", dU)):
                outputs.append(Output(name, OutputStatus.OK, Quantity(
                    float(v), "J m^-2", resolution=4 * (n + m) * _EPS
                    * max(scale, abs(v)),
                    resolution_class=RC.ACCUMULATED_BOUND,
                    resolution_basis="n_cells + n_steps accumulated "
                                     "roundings", reporting_digits=10)))
            outputs.append(Output("energy_relative_residual", OutputStatus.OK,
                                  Quantity(float(resid), "1",
                                           resolution=ENERGY_RESIDUAL_MAX,
                                           resolution_class=RC.ACCUMULATED_BOUND,
                                           resolution_basis="the balance's "
                                                            "rounding bound",
                                           reporting_digits=6)))
        field = _field_bytes(x, T)
        art = ArtifactRef(FIELD_ARTIFACT, digest_bytes(field),
                          "application/octet-stream", len(field),
                          "magic QTSL1, a JSON header line, then x [m] and "
                          "T [K] at the cell centres as little-endian "
                          "float64")
        env = run_environment()
        return ResultBundle(
            model_id=self.model_id, model_version=self.model_version,
            implementation_digest=self.implementation_digest(),
            parameter_digest=digest(p),
            environment_digest=environment_digest(env),
            parameters=dict(p), outputs=tuple(outputs),
            invariants=invariants,
            convergence={"method": "cell-centred finite volume, "
                                   "Crank-Nicolson",
                         "n_cells": n, "n_steps": m,
                         "estimate": "Richardson against n/2, m/2"},
            solver_config={"tridiagonal": "Thomas algorithm, in this file"},
            warnings=("MODEL-ONLY: a forecast from declared inputs, not a "
                      "measured temperature",),
            artifacts=(art,),
            provenance={"solver": "scientific.models.slab_transient._solve",
                        "environment": env,
                        "run_identity":
                            run_identity_for(self, p).to_record()}), \
            {FIELD_ARTIFACT: field}
