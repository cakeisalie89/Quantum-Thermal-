"""Langmuir capture of an impinging gas onto a surface, as a ScientificModel.

EXTRACTED in tranche 4 (directive 22) from
``qta_multiphysics.cryopanel_dynamics_3d``, which forecast the adsorbed
inventory of the QTA apparatus's cryopanels. What was general in it is here,
and nothing else:

    n  = p / (k_B T)                               ideal-gas number density
    F  = n vbar / 4,  vbar = sqrt(8 k_B T / (pi m))  impingement flux
    dN/dt = s F (1 - N / N_cap)                    Langmuir capture
    N(t0 + dt) = N_cap - (N_cap - N(t0)) exp(-s F dt / N_cap)

the last being the exact solution for a constant flux, used directly: no
integrator, so nothing to converge. What was the apparatus's is not here --
which gas reaches the surface in which machine phase, the panels' names, the
sticking coefficients read from its memory table, its phase windows. Those
stay in the legacy module, which now calls these functions for its
arithmetic, so the legacy campaign outputs are regenerated through them
byte for byte: the regression equivalence the directive asks for, held by the
byte gate and by ``tests/test_surface_adsorption.py``.

WHAT IS INVARIANT, AND WHY EACH IS A REAL CHECK

* ``finite`` -- the flux and every inventory are finite; counted.
* ``bounded`` -- 0 <= N <= N_cap after every window. The exact solution is
  bounded by construction; an exponent of the wrong sign or scale is not.
* ``capture_only`` -- no window lowers the inventory. The law has no
  desorption term; a window that loses inventory is not this law.
* ``within_incidence`` -- what was captured, N - N0, is at most s times the
  fluence admitted, because dN/dt = s F (1 - N/N_cap) <= s F. A capture law
  that forgets the capacity in its exponent saturates at once and breaks it.
* ``composition`` -- n windows of dt land where one window of n dt does. The
  exact solution composes; a stepping formula that is not the solution of
  this law does not.

NOT modelled, and so outside the applicability: desorption (thermal or
stimulated) -- the source model was capture-only and never had a desorption
term; coverage dependence of sticking beyond the (1 - N/N_cap) factor;
multilayer growth; pressure or temperature varying inside a run.

The independent check (``scientific.checks.langmuir_rk4``) takes the flux in
its pressure form and integrates the rate law numerically.
"""
from __future__ import annotations

import math
import sys

from ..identity import digest
from ..model import (
    Applicability, CheckSpec, InvariantSpec, ModelBase, Parameter,
    ParameterSchema, run_identity_for,
)
from ..quantity import Quantity, ResolutionClass as RC
from ..result import InvariantResult, Output, OutputStatus, ResultBundle
from ..run_identity import environment_digest, environment_record

MODEL_ID = "surface.langmuir_capture"
MODEL_VERSION = "1.0.0"

K_B = 1.380649e-23           # Boltzmann constant [J/K], exact (SI 2019)
AMU = 1.66053906660e-27      # atomic mass constant [kg], CODATA 2018

COMPOSITION_MAX = 1e-9       # |N_stepped - N_closed| / N_cap
INCIDENCE_RTOL = 1e-12       # rounding allowance on N - N0 <= s * fluence
_EPS = sys.float_info.epsilon


def number_density(pressure_Pa: float, T_K: float) -> float:
    """Ideal-gas number density [1/m^3]."""
    return pressure_Pa / (K_B * T_K)


def impingement_flux(n_m3: float, T_K: float, mass_amu: float) -> float:
    """Molecules striking unit area per unit time [1/m^2/s]: n vbar / 4,
    with vbar the Maxwell-Boltzmann mean speed."""
    vbar = math.sqrt(8.0 * K_B * T_K / (math.pi * mass_amu * AMU))
    return 0.25 * n_m3 * vbar


def langmuir_capture(N: float, flux: float, sticking: float,
                     capacity: float, dt: float) -> float:
    """The inventory after one window of constant ``flux``, from ``N``: the
    exact solution of dN/dt = s F (1 - N / N_cap). Per unit area."""
    if dt < 0:
        raise ValueError("dt_s must be >= 0")
    if sticking <= 0.0 or flux <= 0.0 or dt == 0.0:
        return N
    N = capacity - (capacity - N) * math.exp(-sticking * flux * dt / capacity)
    # the exact solution is bounded by construction; rounding is not
    return min(N, capacity)


class SurfaceAdsorptionModel(ModelBase):
    model_id = MODEL_ID
    model_version = MODEL_VERSION
    implementation_modules = ("scientific.models.surface_adsorption",)
    parameter_schema = ParameterSchema((
        Parameter("pressure_Pa", "float", unit="Pa", minimum=0.0,
                  maximum=1e5, description="gas pressure at the surface"),
        Parameter("T_gas_K", "float", unit="K", minimum=1.0, maximum=1e4,
                  description="temperature of the impinging gas"),
        Parameter("mass_amu", "float", unit="u", minimum=1.0, maximum=1e3,
                  description="molecular mass"),
        Parameter("sticking", "float", unit="1", minimum=0.0, maximum=1.0,
                  description="sticking coefficient on the bare surface"),
        Parameter("capacity_per_m2", "float", unit="m^-2", minimum=1e12,
                  maximum=1e21,
                  description="saturation inventory per unit area"),
        Parameter("initial_coverage", "float", unit="1", minimum=0.0,
                  maximum=1.0, default=0.0,
                  description="N0 / N_cap"),
        Parameter("window_s", "float", unit="s", minimum=0.0, maximum=1e9,
                  description="duration of one exposure window"),
        Parameter("n_windows", "int", unit="1", minimum=1, maximum=10000,
                  default=1, description="consecutive windows"),
    ))
    invariants = (
        InvariantSpec("finite", "the flux and every inventory are finite",
                      "non-finite count == 0"),
        InvariantSpec("bounded", "0 <= N <= N_cap after every window",
                      "largest excursion outside [0, N_cap] == 0"),
        InvariantSpec("capture_only", "no window lowers the inventory",
                      "largest decrease == 0"),
        InvariantSpec("within_incidence",
                      "captured <= sticking x admitted fluence",
                      f"relative excess <= {INCIDENCE_RTOL}"),
        InvariantSpec("composition",
                      "n windows of dt equal one window of n dt",
                      f"|N_stepped - N_closed| / N_cap <= "
                      f"{COMPOSITION_MAX}"),
    )
    independent_checks = (
        CheckSpec("surface_adsorption.rk4_pressure_form",
                  "the flux in its pressure form p / sqrt(2 pi m k_B T) and "
                  "the rate law integrated by classical Runge-Kutta, "
                  "against the exact solution",
                  "DIFFERENT_DISCRETIZATION",
                  shared_components=("the Langmuir capture law",
                                     "the values of k_B and the atomic "
                                     "mass constant"),
                  limitations=("agreement of two solutions of one law, not "
                               "of the law with measurement",)),
    )
    applicability = Applicability(
        "Langmuir (single-site, first-order in vacancies) capture of one "
        "species from a gas at constant pressure and temperature onto a "
        "surface of fixed capacity, per unit area",
        bounds={"pressure_Pa": [0.0, 1e5], "T_gas_K": [1.0, 1e4]},
        excludes=("desorption", "coverage-dependent sticking beyond the "
                  "(1 - N/N_cap) factor", "multilayer growth",
                  "pressure or temperature varying within a run",
                  "any measured in-system inventory"))

    def run(self, inputs: dict) -> ResultBundle:
        p, T, m = inputs["pressure_Pa"], inputs["T_gas_K"], inputs["mass_amu"]
        s, cap = inputs["sticking"], inputs["capacity_per_m2"]
        dt, n_win = inputs["window_s"], inputs["n_windows"]

        flux = impingement_flux(number_density(p, T), T, m)
        N0 = inputs["initial_coverage"] * cap
        trajectory = [N0]
        admitted = 0.0
        for _ in range(n_win):
            trajectory.append(langmuir_capture(trajectory[-1], flux, s, cap,
                                               dt))
            admitted += flux * dt
        N = trajectory[-1]
        closed = langmuir_capture(N0, flux, s, cap, dt * n_win)

        values = trajectory + [flux, admitted, closed]
        nonfinite = sum(1 for v in values if not math.isfinite(v))
        finite = nonfinite == 0
        excursion = max(max(0.0 - v, v - cap, 0.0) for v in trajectory) \
            if finite else math.inf
        decrease = max([a - b for a, b in zip(trajectory, trajectory[1:])]
                       + [0.0]) if finite else math.inf
        allowed = s * admitted
        excess = (N - N0) - allowed
        if not finite:
            rel_excess = math.inf
        elif excess <= 0.0:
            rel_excess = 0.0
        else:
            rel_excess = excess / allowed if allowed > 0.0 else math.inf
        gap = abs(N - closed) / cap if finite else math.inf

        def ratio(v):
            return Quantity(v if math.isfinite(v) else 1e300, "1")

        invariants = (
            InvariantResult("finite", finite,
                            Quantity(float(nonfinite), "1", exact=True),
                            "non-finite count == 0",
                            Quantity(0.0, "1", exact=True),
                            "counted over the flux, the fluence and every "
                            "window's inventory"),
            InvariantResult("bounded", excursion == 0.0,
                            Quantity(excursion if math.isfinite(excursion)
                                     else 1e300, "m^-2"),
                            "largest excursion outside [0, N_cap] == 0",
                            Quantity(0.0, "m^-2", exact=True),
                            "the exact solution never leaves [0, N_cap]"),
            InvariantResult("capture_only", decrease == 0.0,
                            Quantity(decrease if math.isfinite(decrease)
                                     else 1e300, "m^-2"),
                            "largest decrease == 0",
                            Quantity(0.0, "m^-2", exact=True),
                            "the law has no desorption term"),
            InvariantResult("within_incidence",
                            rel_excess <= INCIDENCE_RTOL,
                            ratio(rel_excess),
                            f"((N - N0) - s x fluence) / (s x fluence) <= "
                            f"{INCIDENCE_RTOL}",
                            Quantity(INCIDENCE_RTOL, "1"),
                            "dN/dt = s F (1 - N/N_cap) <= s F; the "
                            "allowance is rounding"),
            InvariantResult("composition", gap <= COMPOSITION_MAX,
                            ratio(gap),
                            f"|N_stepped - N_closed| / N_cap <= "
                            f"{COMPOSITION_MAX}",
                            Quantity(COMPOSITION_MAX, "1"),
                            "the exact solution composes; n windows "
                            "accumulate at most ~n eps N_cap of rounding"),
        )

        def single(v, unit):
            return Quantity(float(v), unit, resolution=4 * _EPS * abs(v),
                            resolution_class=RC.SINGLE_EVALUATION,
                            resolution_basis="a few roundings of one "
                                             "closed-form evaluation",
                            reporting_digits=10)

        def accumulated(v, unit, scale):
            return Quantity(float(v), unit,
                            resolution=4 * (n_win + 1) * _EPS * scale,
                            resolution_class=RC.ACCUMULATED_BOUND,
                            resolution_basis="n_windows + 1 closed-form "
                                             "steps, each within a few eps",
                            reporting_digits=10)

        names = ("impingement_flux", "final_inventory", "final_coverage",
                 "admitted_fluence")
        if finite:
            outputs = (
                Output(names[0], OutputStatus.OK,
                       single(flux, "m^-2 s^-1")),
                Output(names[1], OutputStatus.OK,
                       accumulated(N, "m^-2", cap)),
                Output(names[2], OutputStatus.OK,
                       accumulated(N / cap, "1", 1.0)),
                Output(names[3], OutputStatus.OK,
                       accumulated(admitted, "m^-2", abs(admitted))),
            )
        else:
            outputs = tuple(Output(n, OutputStatus.FAILED,
                                   reason=f"{nonfinite} non-finite values")
                            for n in names)

        env = environment_record()
        return ResultBundle(
            model_id=self.model_id, model_version=self.model_version,
            implementation_digest=self.implementation_digest(),
            parameter_digest=digest(inputs),
            environment_digest=environment_digest(env),
            parameters=dict(inputs),
            outputs=outputs, invariants=invariants,
            convergence={"method": "exact constant-flux solution",
                         "n_windows": n_win},
            solver_config={},
            warnings=("MODEL-ONLY: a forecast from declared inputs, not a "
                      "measured inventory",),
            artifacts=(),
            provenance={"solver": "scientific.models.surface_adsorption."
                                  "langmuir_capture",
                        "environment": env,
                        "run_identity":
                            run_identity_for(self, inputs).to_record()})
