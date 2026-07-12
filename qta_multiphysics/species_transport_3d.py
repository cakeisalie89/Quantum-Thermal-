"""Molecular-flow / Knudsen transport summary for the 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Deterministic gas-kinetic regime forecast per canonical species, using exactly
the D9 derived-check formula and conventions already canonical in this
repository (``lambda = k_B T / (sqrt(2) pi d^2 P)`` evaluated at the
sensing-stage temperature ``T_fridge``; chamber characteristic length 10 mm;
He kinetic diameter 2.6e-10 m per source_map SM053, TEXTBOOK/LITERATURE_BOUND).
The C-13 methane and H2 kinetic diameters are the same class of textbook
constants (Bird 1994 / Roth 1990), labelled LITERATURE_BOUND; the pressures are
the repository's existing canonical values (Mode-B working pressure 1e-4 Pa;
He dose pressure 1e-6 Pa; post-bakeout H2 residual target 1e-10 Pa).

The 3D deposition FOOTPRINT of the C-13 methane beam on the sample plane is
NOT_IMPLEMENTED: the repository does not parameterize the inlet/nozzle
geometry, and no numbers are invented. Deterministic.
"""
from __future__ import annotations

import math

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

K_B = 1.380649e-23
L_CHAR_M = 0.010          # canonical chamber characteristic length (D9)
T_EVAL_K = 0.010          # sensing-stage temperature (implementation convention, D9)

#: species table: (canonical label, role, kinetic diameter [m], provenance,
#:                 pressure [Pa], pressure provenance)
SPECIES = (
    ("C13_CH4", "MODE_B_precursor", 3.8e-10,
     "LITERATURE_BOUND (Bird 1994 class textbook value)",
     1.0e-4, "Mode-B working pressure (existing canonical value)"),
    ("H2", "residual_background_only", 2.9e-10,
     "LITERATURE_BOUND (Bird 1994 class textbook value)",
     1.0e-10, "post-bakeout residual target (existing canonical value)"),
    ("He3", "MODE_D_sensing", 2.6e-10,
     "LITERATURE_BOUND (source_map SM053, Bird1994;Roth1990)",
     1.0e-6, "He dose pressure (existing canonical value, D9)"),
    ("He4", "MODE_D_sensing", 2.6e-10,
     "LITERATURE_BOUND (source_map SM053, Bird1994;Roth1990)",
     1.0e-6, "He dose pressure (existing canonical value, D9)"),
)


def mean_free_path_m(T_K: float, d_m: float, P_Pa: float) -> float:
    return K_B * T_K / (math.sqrt(2.0) * math.pi * d_m * d_m * P_Pa)


def regime(kn: float) -> str:
    if kn > 10.0:
        return "MOLECULAR_FLOW"
    if kn > 0.1:
        return "TRANSITIONAL"
    return "CONTINUUM"


def summary() -> dict:
    per_species = []
    for name, role, d, dprov, P, pprov in SPECIES:
        lam = mean_free_path_m(T_EVAL_K, d, P)
        kn = lam / L_CHAR_M
        per_species.append({
            "species": name, "role": role,
            "kinetic_diameter_m": d, "diameter_provenance": dprov,
            "pressure_Pa": P, "pressure_provenance": pprov,
            "T_eval_K": T_EVAL_K,
            "mean_free_path_m": lam, "Kn": kn, "regime": regime(kn),
        })
    return {
        "formula": "lambda = k_B*T/(sqrt(2)*pi*d^2*P); Kn = lambda/L_char",
        "L_char_m": L_CHAR_M,
        "convention": "evaluated at the sensing-stage temperature T_fridge "
                      "= 10 mK, exactly as the canonical D9 derived check",
        "per_species": per_species,
        "c13_methane_footprint_3d": {
            "status": "NOT_IMPLEMENTED",
            "reason": "inlet/nozzle geometry is not parameterized in this "
                      "repository; no deposition-footprint numbers are invented",
        },
        "note": "He-3/He-4 (dose) and residual H2 are deep in the "
                "molecular-flow regime at the canonical conditions "
                "(quasi-ballistic line-of-sight transport). The C-13 methane "
                "precursor at the Mode-B working pressure is TRANSITIONAL "
                "(Kn~0.2), consistent with the repository's advective-"
                "diffusive Mode-B gas-transport treatment. Forecast, DERIVED "
                "numerical only.",
        "label": LABEL,
    }
