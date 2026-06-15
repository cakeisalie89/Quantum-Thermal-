"""SI units, physical constants, and parameter validation for the QTA
non-lumped multiphysics layer.

Everything in this package is in SI internally. No hidden constants: every
physical constant used anywhere in the layer is defined here and imported.

This module is MODEL-ONLY infrastructure. It does not represent measured data.
"""
from __future__ import annotations
import math

# ---- Physical constants (SI, CODATA-style values) ----
K_B = 1.380649e-23          # Boltzmann constant [J/K]
SIGMA_SB = 5.670374419e-8   # Stefan-Boltzmann constant [W/m^2/K^4]
N_A = 6.02214076e23         # Avogadro [1/mol]
H_PLANCK = 6.62607015e-34   # Planck [J*s]
HBAR = H_PLANCK / (2.0 * math.pi)
AMU = 1.66053906660e-27     # atomic mass unit [kg]

# Diamond reference properties (reduced-model anchors; ASSUMED/literature, not
# measured in this system). Debye temperature of diamond ~ 2220 K.
DEBYE_TEMP_DIAMOND_K = 2220.0
DIAMOND_DENSITY_KG_M3 = 3510.0
DIAMOND_MOLAR_MASS_KG_MOL = 12.011e-3   # carbon


class ParameterError(ValueError):
    """Raised when a parameter is physically impossible or out of range."""


def require_positive(name: str, value: float) -> float:
    if value is None or not math.isfinite(value) or value <= 0.0:
        raise ParameterError(f"{name} must be a finite positive number, got {value!r}")
    return float(value)


def require_nonnegative(name: str, value: float) -> float:
    if value is None or not math.isfinite(value) or value < 0.0:
        raise ParameterError(f"{name} must be a finite non-negative number, got {value!r}")
    return float(value)


def require_temperature(name: str, value: float) -> float:
    """Absolute temperature: finite, > 0 K, and below a sanity ceiling."""
    v = require_positive(name, value)
    if v > 1.0e5:
        raise ParameterError(f"{name}={v} K exceeds sanity ceiling (1e5 K)")
    return v


def require_fraction(name: str, value: float) -> float:
    v = require_nonnegative(name, value)
    if v > 1.0:
        raise ParameterError(f"{name} must be in [0,1], got {v}")
    return v


def require_range(name: str, value: float, lo: float, hi: float) -> float:
    if value is None or not math.isfinite(value):
        raise ParameterError(f"{name} must be finite, got {value!r}")
    if not (lo <= value <= hi):
        raise ParameterError(f"{name}={value} outside allowed range [{lo}, {hi}]")
    return float(value)
