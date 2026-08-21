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

# ---------------------------------------------------------------------------
# Per-mode / per-species gas-temperature semantics.
#
# Every species used to be evaluated at T_EVAL_K = 10 mK, the Mode-D
# sensing-stage temperature. For the dosed helium that is the right number --
# it is thermalised to the stage being sensed. For the C-13 methane PRECURSOR
# at the Mode-B working pressure it is not: Mode B is material processing, the
# methane is a working gas delivered to a surface that the thermal model takes
# to ~29 K peak, and methane has no meaningful vapour pressure at 10 mK, so a
# 10 mK methane population is not a state the rest of the repository models.
#
# lambda scales linearly with T, so the assumption is not a detail: at 10 mK
# the methane classifies TRANSITIONAL (Kn ~ 0.2); at any of the repository's
# other declared stage temperatures it is MOLECULAR_FLOW (Kn ~ 1e3). The
# regime classification is decided by the temperature assumption alone.
#
# The repository registers NO authoritative Mode-B gas temperature. Rather than
# choose one, the affected species are marked UNRESOLVED and their regime is
# reported as PARAMETERIZED across the temperatures the repository does
# declare -- the radiation_paths STAGE_CHAIN stages, which are cited values,
# not invented ones.
RESOLVED = "RESOLVED"
UNRESOLVED = "UNRESOLVED_REQUIRES_OWNER_AUTHORITY"

#: Declared stage temperatures already canonical in this repository
#: (radiation_paths.STAGE_CHAIN). Used only as a parameterization span for
#: species whose gas temperature is unresolved; none of these is asserted to BE
#: the Mode-B gas temperature.
PARAMETERIZATION_SPAN_K = (0.010, 0.1, 1.0, 4.0, 77.0, 300.0)

#: species -> (mode, temperature basis, status, temperature or None)
GAS_TEMPERATURE_SEMANTICS: dict[str, tuple[str, str, str, float | None]] = {
    "He3": ("MODE_D", "dosed into and thermalised to the 10 mK sensing stage "
            "(config.FridgeConfig.T_fridge_K); this is the stage being sensed",
            RESOLVED, T_EVAL_K),
    "He4": ("MODE_D", "dosed into and thermalised to the 10 mK sensing stage "
            "(config.FridgeConfig.T_fridge_K); this is the stage being sensed",
            RESOLVED, T_EVAL_K),
    "C13_CH4": ("MODE_B", "process precursor delivered at the Mode-B working "
                "pressure to a surface the thermal model takes to ~29 K peak; "
                "no inlet, line or reservoir temperature is registered "
                "anywhere in this repository",
                UNRESOLVED, None),
    "H2": ("ALL_MODES", "residual background, never a live species; it is "
           "outgassed from surfaces spanning 300 K feedthroughs to the 10 mK "
           "stage and no single population temperature is registered",
           UNRESOLVED, None),
}

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
        mode, basis, status, T = GAS_TEMPERATURE_SEMANTICS[name]
        row = {
            "species": name, "role": role,
            "kinetic_diameter_m": d, "diameter_provenance": dprov,
            "pressure_Pa": P, "pressure_provenance": pprov,
            "gas_temperature_mode": mode,
            "gas_temperature_basis": basis,
            "gas_temperature_status": status,
        }
        if status == RESOLVED:
            # RESOLVED entries always carry a temperature; the tuple element is
            # Optional because UNRESOLVED entries deliberately carry None.
            assert T is not None, f"{name}: RESOLVED status with no temperature"
            lam = mean_free_path_m(T, d, P)
            kn = lam / L_CHAR_M
            row.update({"T_eval_K": T, "mean_free_path_m": lam, "Kn": kn,
                        "regime": regime(kn)})
        else:
            # No authoritative temperature: report the span, never a value.
            span: list[dict] = []
            regime_names: set[str] = set()
            for Tk in PARAMETERIZATION_SPAN_K:
                lam = mean_free_path_m(Tk, d, P)
                kn = lam / L_CHAR_M
                reg = regime(kn)
                regime_names.add(reg)
                span.append({"T_K": Tk, "mean_free_path_m": lam, "Kn": kn,
                             "regime": reg})
            regimes = sorted(regime_names)
            row.update({
                "T_eval_K": None,
                "mean_free_path_m": None,
                "Kn": None,
                "regime": ("PARAMETERIZED_UNRESOLVED" if len(regimes) > 1
                           else regimes[0]),
                "regime_parameterized_over": span,
                "regime_is_temperature_sensitive": len(regimes) > 1,
                "regimes_spanned": regimes,
            })
        per_species.append(row)
    return {
        "formula": "lambda = k_B*T/(sqrt(2)*pi*d^2*P); Kn = lambda/L_char",
        "L_char_m": L_CHAR_M,
        "convention": "per-species gas temperature, not one global value. "
                      "Mode-D helium is evaluated at the sensing-stage "
                      "temperature T_fridge = 10 mK (the canonical D9 derived "
                      "check). Species whose gas temperature is not registered "
                      "anywhere in this repository are NOT assigned one: their "
                      "regime is reported parameterized over the declared "
                      "stage temperatures and marked "
                      "UNRESOLVED_REQUIRES_OWNER_AUTHORITY.",
        "gas_temperature_semantics": {
            k: {"mode": v[0], "basis": v[1], "status": v[2], "T_K": v[3]}
            for k, v in GAS_TEMPERATURE_SEMANTICS.items()},
        "parameterization_span_K": list(PARAMETERIZATION_SPAN_K),
        "unresolved_gas_temperatures": sorted(
            k for k, v in GAS_TEMPERATURE_SEMANTICS.items()
            if v[2] == UNRESOLVED),
        "per_species": per_species,
        "c13_methane_footprint_3d": {
            "status": "NOT_IMPLEMENTED",
            "reason": "inlet/nozzle geometry is not parameterized in this "
                      "repository; no deposition-footprint numbers are invented",
        },
        "note": "He-3/He-4 (dose) are deep in the molecular-flow regime at "
                "the sensing-stage temperature (quasi-ballistic "
                "line-of-sight transport); that classification is resolved. "
                "Residual H2 classifies MOLECULAR_FLOW at every temperature "
                "in the declared span, so its classification is robust even "
                "though its population temperature is unresolved. The C-13 "
                "methane precursor is NOT robust: it classifies TRANSITIONAL "
                "at 10 mK and MOLECULAR_FLOW at every other declared stage "
                "temperature, so the regime follows entirely from the "
                "temperature assumption. This previously read as a settled "
                "TRANSITIONAL (Kn~0.2) result because the Mode-D sensing-stage "
                "temperature was applied to a Mode-B process gas. No Mode-B "
                "gas temperature is registered in this repository and none is "
                "invented here. Forecast, DERIVED numerical only.",
        "label": LABEL,
    }
