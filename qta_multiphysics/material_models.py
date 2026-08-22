"""Temperature-dependent material property models (reduced-order).

These are ASSUMED / literature-anchored reduced models, NOT measured in this
system. They are documented so a reviewer can see every constant.

Diamond is the relevant substrate (NV host). Over the temperature range of
interest here (~10 mK up to a few hundred K transiently), two facts dominate:

  * Specific heat: diamond's Debye temperature is ~2220 K, so for all T here
    (T << Theta_D) the Debye low-temperature limit Cp ~ (12/5) pi^4 R (T/Theta_D)^3
    is an accurate, genuinely first-principles form (per mole, divided by molar
    mass to get J/kg/K). A small floor is added for numerical safety.

  * Thermal conductivity: at low T, phonon conduction in a finite crystal is
    boundary/Casimir limited and scales as k ~ T^3; near ~70-100 K it peaks and
    then decreases. A single global fit is out of scope for a reduced model, so
    we use a boundary-limited k0*(T/T_ref)^3 form valid in the cryogenic regime,
    capped at a plateau k_plateau to avoid unphysical divergence at higher
    transient temperatures. This is explicitly a REDUCED model; the cap and
    exponent are ASSUMED and registered as such.
"""
from __future__ import annotations
import numpy as np
from .units import (K_B, N_A, DEBYE_TEMP_DIAMOND_K, DIAMOND_DENSITY_KG_M3,
                    DIAMOND_MOLAR_MASS_KG_MOL, require_temperature)

R_GAS = K_B * N_A  # universal gas constant [J/mol/K]

# ---------------------------------------------------------------------------
# Numerical floors, and where they stop being numerical.
#
# Both floors were documented as "small ... for numerical safety", preventing a
# divide-by-zero in the heat equation at ultra-low T. Measured against the raw
# models they guard, that description holds only above a crossover temperature
# that is far ABOVE this machine's operating point:
#
#   Cp floor 1e-6 J/kg/K  exceeds the Debye model below      T = 0.407 K
#   k  floor 1e-3 W/m/K   exceeds the boundary-limited model below  T = 0.794 K
#
# The canonical stages sit at 10 mK (Mode A baseline, Mode D sensing) and the
# Mode-C readiness threshold is 50 mK. At 10 mK the floors exceed the physical
# models by 6.8e4x (Cp) and 5.0e5x (k). Across the whole sub-kelvin regime the
# floors ARE the material model; they are not regularization there.
#
# Consequence for diffusivity, which is what the recovery forecast depends on:
# both raw models scale as T^3, so raw alpha = k/(rho Cp) is temperature
# independent at 0.0385 m^2/s. The floors bite at DIFFERENT temperatures, so
# the floored ratio is 0.2846 m^2/s -- a 7.4x overestimate of diffusivity
# throughout the floor-dominated regime, including at the 50 mK Mode-C
# readiness threshold.
#
# This repository has no authority for better cryogenic diamond properties, so
# no replacement values are invented. The floors are UNCHANGED. What changes is
# that they are declared: floor_report() states the crossovers and ratios, and
# any consumer of a sub-crossover prediction can see that it rests on an
# effective material-property assumption rather than on the Debye and
# boundary-limited models the docstrings cite.
CP_FLOOR_J_KG_K = 1.0e-6
K_FLOOR_W_M_K = 1.0e-3

FLOOR_CLASS_REGULARIZATION = "NUMERICAL_REGULARIZATION"
FLOOR_CLASS_EFFECTIVE_PROPERTY = "EFFECTIVE_MATERIAL_PROPERTY_ASSUMPTION"

#: Temperatures spanned by the canonical modes, for the ratio table below.
#: 10 mK is the Mode-A/D stage, 50 mK the Mode-C readiness threshold, and
#: ~29 K the Mode-B peak the thermal model forecasts.
MODE_TEMPERATURE_PROBES_K = (0.010, 0.050, 0.100, 0.407, 0.794, 1.0, 4.0, 29.4)


def _cp_raw(T):
    """Debye low-T limit with NO floor applied [J/kg/K]."""
    T = np.asarray(T, dtype=float)
    return ((12.0 / 5.0) * np.pi**4 * R_GAS
            * (T / DEBYE_TEMP_DIAMOND_K) ** 3 / DIAMOND_MOLAR_MASS_KG_MOL)


def _k_raw(T, k_ref=2000.0, T_ref=100.0, exponent=3.0, k_plateau=3000.0):
    """Boundary-limited k(T) with NO floor applied [W/m/K]."""
    T = np.asarray(T, dtype=float)
    return np.minimum(k_ref * (np.maximum(T, 0.0) / T_ref) ** exponent, k_plateau)


def cp_floor_crossover_K() -> float:
    """T at which the Cp floor equals the raw Debye model."""
    return float(DEBYE_TEMP_DIAMOND_K * (
        CP_FLOOR_J_KG_K * DIAMOND_MOLAR_MASS_KG_MOL
        / ((12.0 / 5.0) * np.pi**4 * R_GAS)) ** (1.0 / 3.0))


def k_floor_crossover_K(k_ref=2000.0, T_ref=100.0, exponent=3.0) -> float:
    """T at which the k floor equals the raw boundary-limited model."""
    return float(T_ref * (K_FLOOR_W_M_K / k_ref) ** (1.0 / exponent))


#: Diagnostic multipliers for the floor sensitivity sweep. These are NOT
#: alternative material properties and are never used by any canonical run --
#: they exist only to answer "how much does the forecast depend on a value the
#: repository has no authority for?". A decade either side brackets the
#: plausible ignorance without asserting any of it is right.
FLOOR_SENSITIVITY_FACTORS = (0.1, 1.0, 10.0)


def floor_sensitivity(T_probe: float = 0.050,
                      factors=FLOOR_SENSITIVITY_FACTORS) -> dict:
    """DIAGNOSTIC ONLY: response of derived quantities to the floor values.

    Distinguishes three things the single word "floor" was hiding:

      * numerical stability mechanism -- what stops a divide-by-zero;
      * constitutive assumption -- what the material is taken to be below the
        crossover, because the floor exceeds the model it guards there;
      * output sensitivity -- how much a governed forecast moves if that
        assumption moves.

    Nothing here is written to a canonical artifact and no floor is changed.
    ``T_probe`` defaults to the Mode-C readiness threshold, the governed
    temperature most exposed to the floors.
    """
    rows = []
    for fc in factors:
        for fk in factors:
            cp = CP_FLOOR_J_KG_K * fc
            kk = K_FLOOR_W_M_K * fk
            cp_eff = max(float(_cp_raw(T_probe)), cp)
            k_eff = max(min(float(_k_raw(T_probe)), 3000.0), kk)
            t_cp = float(DEBYE_TEMP_DIAMOND_K * (
                cp * DIAMOND_MOLAR_MASS_KG_MOL
                / ((12.0 / 5.0) * np.pi**4 * R_GAS)) ** (1.0 / 3.0))
            t_k = float(100.0 * (kk / 2000.0) ** (1.0 / 3.0))
            rows.append({
                "cp_floor_factor": fc, "k_floor_factor": fk,
                "cp_floor_J_kg_K": cp, "k_floor_W_m_K": kk,
                "cp_crossover_K": t_cp, "k_crossover_K": t_k,
                "alpha_at_probe_m2_s": k_eff / (DIAMOND_DENSITY_KG_M3 * cp_eff),
            })
    base = next(r for r in rows
                if r["cp_floor_factor"] == 1.0 and r["k_floor_factor"] == 1.0)
    alphas = [r["alpha_at_probe_m2_s"] for r in rows]
    return {
        "meaning": "DIAGNOSTIC, NON-AUTHORITATIVE sensitivity of the derived "
                   "thermal diffusivity to the numerical floors; no floor is "
                   "changed and no canonical output is produced from this",
        "probe_T_K": T_probe,
        "probe_rationale": "Mode-C readiness threshold "
                           "(config.SolverConfig.mode_d_temp_threshold_K)",
        "baseline_alpha_m2_s": base["alpha_at_probe_m2_s"],
        "alpha_min_m2_s": min(alphas), "alpha_max_m2_s": max(alphas),
        "alpha_spread_factor": max(alphas) / min(alphas),
        "raw_physical_alpha_m2_s": float(_k_raw(T_probe)) / (
            DIAMOND_DENSITY_KG_M3 * float(_cp_raw(T_probe))),
        "interpretation": "alpha at the probe scales as k_floor / cp_floor "
                          "while both floors dominate, so a decade of "
                          "uncertainty in either moves the diffusivity by a "
                          "decade. This is a CONSTITUTIVE sensitivity, not a "
                          "numerical one: the floors are the material model in "
                          "this regime.",
        "authority_status": "NO_AUTHORITATIVE_REPLACEMENT_IN_REPOSITORY",
        "table": rows,
        "label": "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM "
                 "DIAGNOSTIC_NON_AUTHORITATIVE",
    }


def floor_report(probes=None) -> dict:
    """Declare each floor's crossover, dominance ratio and true class.

    A floor that exceeds the model it guards is not a guard. This makes that
    visible per temperature instead of leaving it implicit in a docstring.
    """
    probes = tuple(probes or MODE_TEMPERATURE_PROBES_K)
    t_cp, t_k = cp_floor_crossover_K(), k_floor_crossover_K()
    rows = []
    for T in probes:
        cp_r, k_r = float(_cp_raw(T)), float(_k_raw(T))
        rows.append({
            "T_K": float(T),
            "cp_raw_J_kg_K": cp_r,
            "cp_floor_over_raw": CP_FLOOR_J_KG_K / cp_r if cp_r > 0 else float("inf"),
            "cp_floor_dominates": bool(CP_FLOOR_J_KG_K > cp_r),
            "k_raw_W_m_K": k_r,
            "k_floor_over_raw": K_FLOOR_W_M_K / k_r if k_r > 0 else float("inf"),
            "k_floor_dominates": bool(K_FLOOR_W_M_K > k_r),
            "alpha_raw_m2_s": k_r / (DIAMOND_DENSITY_KG_M3 * cp_r) if cp_r > 0 else float("inf"),
            "alpha_floored_m2_s": float(
                diamond_k(T) / (DIAMOND_DENSITY_KG_M3 * diamond_cp(T))),
        })
    return {
        "meaning": "declaration of where each numerical floor stops being "
                   "numerical; no floor value is changed by this report and "
                   "no replacement cryogenic property is invented",
        "floors": {
            "diamond_cp": {
                "value": CP_FLOOR_J_KG_K, "unit": "J/kg/K",
                "guards": "division by rho*Cp in the heat equation",
                "crossover_K": t_cp,
                "class_below_crossover": FLOOR_CLASS_EFFECTIVE_PROPERTY,
                "class_above_crossover": FLOOR_CLASS_REGULARIZATION,
            },
            "diamond_k": {
                "value": K_FLOOR_W_M_K, "unit": "W/m/K",
                "guards": "vanishing face conductance at ultra-low T",
                "crossover_K": t_k,
                "class_below_crossover": FLOOR_CLASS_EFFECTIVE_PROPERTY,
                "class_above_crossover": FLOOR_CLASS_REGULARIZATION,
            },
        },
        "dominant_in_canonical_regime": True,
        "affected_predictions": [
            "Mode-C recool time and the 50 mK readiness threshold: the "
            "threshold sits ~8x below the k crossover and ~8x below the Cp "
            "crossover, so recovery near it is governed by the floors",
            "thermal diffusivity at base temperature: raw alpha is 0.0385 "
            "m^2/s (k and Cp both scale as T^3, so the ratio is flat); the "
            "floors bite at different temperatures and give 0.2846 m^2/s, a "
            "7.4x overestimate",
            "Mode-A/Mode-D 10 mK stage temperatures",
        ],
        "authority_status": "NO_AUTHORITATIVE_REPLACEMENT_IN_REPOSITORY -- "
                            "measured or literature-bound cryogenic diamond "
                            "Cp/k below ~1 K are not registered here; the "
                            "floors stand and the affected predictions remain "
                            "MODEL_ONLY / FORECAST_ONLY",
        "table": rows,
        "label": "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM",
    }


def diamond_cp(T):
    """Specific heat of diamond [J/kg/K], Debye low-T limit (T << Theta_D).

    Cp_molar = (12/5) pi^4 R (T/Theta_D)^3   [J/mol/K]
    Cp_mass  = Cp_molar / M                  [J/kg/K]
    A small floor (1e-6 J/kg/K) prevents division-by-zero in the heat equation
    at ultra-low T; it does not affect transient peaks.
    """
    T = np.asarray(T, dtype=float)
    cp_molar = (12.0 / 5.0) * np.pi**4 * R_GAS * (T / DEBYE_TEMP_DIAMOND_K) ** 3
    cp_mass = cp_molar / DIAMOND_MOLAR_MASS_KG_MOL
    return np.maximum(cp_mass, CP_FLOOR_J_KG_K)


def diamond_k(T, k_ref=2000.0, T_ref=100.0, exponent=3.0, k_plateau=3000.0,
              k_floor=K_FLOOR_W_M_K):
    """Thermal conductivity of diamond [W/m/K], reduced boundary-limited model.

    k(T) = min( k_ref * (T/T_ref)^exponent , k_plateau ), floored at k_floor.

    Defaults are ASSUMED reduced-model parameters (registered in the parameter
    registries). They give cryogenic k ~ T^3 behaviour and avoid unphysical
    divergence at higher transient T. This is a forecast model, not measured.
    """
    T = np.asarray(T, dtype=float)
    k = k_ref * (np.maximum(T, 0.0) / T_ref) ** exponent
    k = np.minimum(k, k_plateau)
    return np.maximum(k, k_floor)


def thermal_diffusivity(T, rho=DIAMOND_DENSITY_KG_M3, **kkw):
    """alpha = k / (rho Cp) [m^2/s]."""
    return diamond_k(T, **kkw) / (rho * diamond_cp(T))


def internal_energy_density(T, rho=DIAMOND_DENSITY_KG_M3, n_table=6000):
    """Volumetric internal energy u(T) = rho * \\int_0^T Cp(T') dT'  [J/m^3].

    Integrates the SAME diamond_cp model the solvers use (Debye low-T form with
    its small numerical floor), so the finite-volume energy accounting is
    self-consistent with the heat capacity in the PDE. Vectorised via a
    cumulative reference table. This supports a DERIVED numerical energy-balance
    check; it is MODEL-ONLY and not a measured quantity.
    """
    T = np.asarray(T, dtype=float)
    Tmax = float(max(T.max(), 1.0)) if T.size else 1.0
    Tref = np.linspace(0.0, Tmax, int(n_table))
    cp = diamond_cp(Tref)                                   # [J/kg/K]
    u_ref = rho * np.concatenate(
        ([0.0], np.cumsum(0.5 * (cp[1:] + cp[:-1]) * np.diff(Tref))))
    return np.interp(np.clip(T, 0.0, Tmax), Tref, u_ref)
