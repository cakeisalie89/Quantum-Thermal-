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
    return np.maximum(cp_mass, 1.0e-6)


def diamond_k(T, k_ref=2000.0, T_ref=100.0, exponent=3.0, k_plateau=3000.0, k_floor=1.0e-3):
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
