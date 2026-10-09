"""Independent check of Langmuir capture: the rate law integrated, the flux
taken in its other form.

The producer (``scientific.models.surface_adsorption``) computes the
impingement flux as n vbar / 4 from the number density, and the inventory from
the exact constant-flux solution, one closed-form step per window. This check
shares neither computation:

* the flux in its pressure form, F = p / sqrt(2 pi m k_B T) -- algebraically
  n vbar / 4 with n = p / (k_B T), evaluated by a different expression, so an
  error in either form's constants shows as a disagreement;
* the captured inventory C = N - N0 by integrating the same law,
  dC/dt = s F ((N_cap - N0) - C) / N_cap, with classical fourth-order
  Runge-Kutta over the whole exposure, n_windows x window_s, in one pass --
  not window by window, and without the exponential. Integrating C rather
  than N keeps a small capture onto a well-covered surface from cancelling
  against N0.

WHAT IT ESTABLISHES, AND WHAT IT DOES NOT

Agreement between two solutions of ONE law. It shares the law itself and the
values of the constants with the producer, and says so; an error in the law
(a missing desorption term, a wrong order in the vacancies) is invisible to
it. It is not a comparison with measurement, and a PASS is not experimental
validation of anything.

The criterion. RK4 is run at k h <= 0.01, k = s F / N_cap, where its relative
error in C on this linear equation is of order (k h)^4 / 120, about 1e-10;
the bound 1e-8 leaves two orders of margin over that and resolves an error of
ten parts per billion in the flux or in C -- tight enough that a lower-order
integrator fails it (the same four stages with uniform weights are second
order, per-step error (k h)^3 / 48, and do), so the bound is a statement
about this integrator and not a tolerance any would meet. The producer's C
is a difference, N - N0, so it is only as good as the resolution the
producer states for N: the relative disagreement is taken against C or
against that resolution / 1e-8, whichever is larger, so a difference inside
the producer's own stated resolution never fails and one outside it always
can. Past k t = 40 the remaining vacancy exp(-40) ~ 4e-18 is below the
precision of N_cap, so the integration stops there: continuing would change
no representable digit.
"""
from __future__ import annotations

from typing import Any

import math

from ..identity import implementation_digest
from ..quantity import Quantity, ResolutionClass as RC
from ..result import OutputStatus, ResultBundle
from ..verification import (
    CheckType, Establishes, Independence, Status, VerificationResult,
)
from . import quantity

CHECK_ID = "surface_adsorption.rk4_pressure_form"
#: The model this check was written for -- stated, not imported, so that the
#: check's code identity covers none of the producer's code. A new model
#: version is refused until this check has been reviewed against it.
MODEL_ID = "surface.langmuir_capture"
MODEL_VERSION = "1.0.0"
IMPLEMENTATION_MODULES = ("scientific.checks.langmuir_rk4",)
REL_MAX = 1e-8
KH = 0.01                    # k h, the RK4 step in units of 1 / k
KT_SATURATED = 40.0          # exp(-40) is below double precision of N_cap
MIN_STEPS = 200

#: the same published values the producer uses, written down again
BOLTZMANN_J_K = 1.380649e-23
ATOMIC_MASS_KG = 1.66053906660e-27

SHARED = ("the Langmuir capture law dN/dt = s F (1 - N / N_cap)",
          "the values of k_B and the atomic mass constant")
LIMITS = ("agreement of two solutions of one law, not of the law with "
          "measurement",
          "an error in the law itself is invisible to this check")


def check_digest() -> str:
    return implementation_digest(IMPLEMENTATION_MODULES)


def pressure_form_flux(p_Pa: float, T_K: float, mass_amu: float) -> float:
    return p_Pa / math.sqrt(2.0 * math.pi * mass_amu * ATOMIC_MASS_KG
                            * BOLTZMANN_J_K * T_K)


def captured(N0: float, flux: float, sticking: float, capacity: float,
             t_total: float) -> float:
    """C(t_total) = N - N0 by classical RK4 on dC/dt = s F (V0 - C) / N_cap,
    V0 = N_cap - N0 the initial vacancy."""
    k = sticking * flux / capacity
    vacancy = capacity - N0
    if k <= 0.0 or t_total <= 0.0 or vacancy <= 0.0:
        return 0.0
    span = min(t_total, KT_SATURATED / k)
    steps = max(MIN_STEPS, math.ceil(k * span / KH))
    h = span / steps

    def rate(C):
        return sticking * flux * (vacancy - C) / capacity

    C = 0.0
    for _ in range(steps):
        k1 = rate(C)
        k2 = rate(C + 0.5 * h * k1)
        k3 = rate(C + 0.5 * h * k2)
        k4 = rate(C + h * k3)
        C += h * (k1 + 2.0 * k2 + 2.0 * k3 + k4) / 6.0
    return C


def _rel(a: float, b: float) -> float:
    if a == b:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b))


def run_check(bundle: ResultBundle, *, verifier_id: str) -> VerificationResult:
    if (bundle.model_id, bundle.model_version) != (MODEL_ID, MODEL_VERSION):
        raise ValueError(f"this check is for {MODEL_ID}@{MODEL_VERSION}, "
                         f"not {bundle.model_id}@{bundle.model_version}")
    common: dict[str, Any] = dict(
        check_id=CHECK_ID, check_type=CheckType.INDEPENDENT_IMPLEMENTATION,
        subject_digest=bundle.digest(),
        subject_model=f"{bundle.model_id}@{bundle.model_version}",
        criterion=f"max(|dF| / F, |dC| / max(C, r_N / {REL_MAX})) <= "
                  f"{REL_MAX}; C = N - N0, r_N the producer's stated "
                  "resolution of N",
        criterion_derivation="RK4 at k h <= 0.01 on this linear equation "
                             "errs about 1e-10 relative in C; 1e-8 keeps "
                             "two orders of margin, and a lower-order "
                             "integrator fails it",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_DISCRETIZATION,
        establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
        shared_components=SHARED, limitations=LIMITS)
    flux_out = bundle.output("impingement_flux")
    inv_out = bundle.output("final_inventory")
    if flux_out.status is not OutputStatus.OK or \
            inv_out.status is not OutputStatus.OK:
        return VerificationResult(
            **{**common, "limitations": LIMITS + (
                "the producer's outputs are not OK; nothing to compare",)},
            status=Status.NOT_RUN, measured=None, threshold=None)

    prm = bundle.parameters
    cap = prm["capacity_per_m2"]
    N0 = prm["initial_coverage"] * cap
    flux = pressure_form_flux(prm["pressure_Pa"], prm["T_gas_K"],
                              prm["mass_amu"])
    C = captured(N0, flux, prm["sticking"], cap,
                 prm["window_s"] * prm["n_windows"])
    C_producer = quantity(inv_out).value - N0
    floor = (quantity(inv_out).resolution or 0.0) / REL_MAX
    rel = max(_rel(flux, quantity(flux_out).value),
              abs(C - C_producer) / max(abs(C), floor)
              if C != C_producer else 0.0)
    return VerificationResult(
        **common,
        status=Status.PASS if rel <= REL_MAX else Status.FAIL,
        measured=Quantity(rel, "1", resolution=1e-10,
                          resolution_class=RC.DISCRETIZATION_ESTIMATE,
                          resolution_basis="RK4 relative error in C at "
                                           "k h <= 0.01 on a linear equation",
                          reporting_digits=4),
        threshold=Quantity(REL_MAX, "1"))
