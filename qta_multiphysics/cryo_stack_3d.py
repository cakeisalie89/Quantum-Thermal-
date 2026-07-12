"""Cryogenic stack registry / budget for the 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Rows describe the canonical QTA cryogenic architecture on two honesty tiers:

* ACTIVE_MODELED -- elements the repository actively parameterizes today.
  Their numeric loads are REUSED from the existing canonical modules
  (``radiation_paths`` per-stage radiative loads; ``FridgeConfig`` for the
  dilution/base stage; the SC heat switch and 4 K shutter as design elements
  referenced by gates A1 / A3). No new physics, no new numbers.
* NOT_IMPLEMENTED -- stages that are part of the canonical architecture but are
  NOT actively modeled in this repository (LN2 pre-cooling as a distinct
  stage; a standalone Joule-Thomson stage beyond the combined RTB/JT-class
  cooling-plant gate; nuclear demagnetization; gas-gap heat switch). They are
  registered here so the architecture is not erased, with **no invented
  performance numbers** (numeric fields left empty).

Deterministic: no randomness anywhere in this module.
"""
from __future__ import annotations

from .config import default_config
from .radiation_paths import radiation_paths

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def budget_rows(shutter_state: str = "closed", baffle_state: str = "engaged"):
    cfg = default_config()
    paths, rmet = radiation_paths(shutter_state=shutter_state,
                                  baffle_state=baffle_state)
    rows = []

    def add(element, stage, status, source, load_W="", note=""):
        rows.append({"element": element, "stage": stage, "status": status,
                     "source": source, "forecast_load_W": load_W,
                     "note": note, "label": LABEL})

    # -- ACTIVE_MODELED: intercept chain radiative loads (reused verbatim) --
    for p in paths:
        name = p.get("path", p.get("hop", "stage_hop"))
        load = p.get("load_W", p.get("P_W", ""))
        add(f"radiative_intercept:{name}", str(name), "ACTIVE_MODELED",
            "radiation_paths (canonical chain, shutter/baffle attenuation)",
            f"{load:.9e}" if isinstance(load, float) else str(load))
    p10 = rmet["total_heat_load_to_10mK_W"]  # fail loud on schema drift
    add("radiative_total_to_10mK", "10mK", "ACTIVE_MODELED", "radiation_paths",
        f"{p10:.9e}" if isinstance(p10, float) else "")

    # -- ACTIVE_MODELED / DESIGN_SPECIFIED elements (numbers only where owned) --
    add("dilution_refrigeration_base_stage", "10mK", "ACTIVE_MODELED",
        f"FridgeConfig (T_fridge_K={cfg.fridge.T_fridge_K}, Kapitza-radiative sink)",
        note="base-temperature sink used by the 1D/2D/3D thermal backends")
    add("RTB_JT_class_upstream_cooling_plant", "4K/shield", "ACTIVE_MODELED",
        "qta_full_sim gate RTB_JT_OPTIONAL_COOLING_PLANT (optional plant, forecast-only)",
        note="combined RTB/JT-class gate; no standalone JT stage is modeled")
    add("superconducting_heat_switch", "MC/sample", "DESIGN_SPECIFIED",
        "qta_full_sim gate A1 (MC protected, SC switch open in Mode B)",
        note="state logic modeled via gates; no new numbers here")
    add("radiation_shutter_4K", "4K", "DESIGN_SPECIFIED",
        "qta_full_sim gate A3 + radiation_paths shutter attenuation factor")
    add("cryobaffles", "4K/1K", "DESIGN_SPECIFIED",
        "radiation_paths baffle attenuation factor; gas_transport cryobaffle sink")
    add("kevlar49_g10cr_support_suspension", "stage intercepts", "DESIGN_SPECIFIED",
        "committed support architecture (manuscript Sec. 4); conduction not "
        "separately parameterized here", note="no invented conduction numbers")
    add("ofhc_thermal_shielding_paths", "all stages", "DESIGN_SPECIFIED",
        "committed architecture; material paths, no separate numeric model")

    # -- canonical stages NOT actively modeled: registered, never faked --
    for element, note in (
        ("LN2_precooling_stage",
         "canonical upstream pre-cool; not a distinct modeled stage in this repo"),
        ("standalone_joule_thomson_stage",
         "only the combined RTB/JT-class plant gate exists; standalone JT not modeled"),
        ("nuclear_demagnetization_stage",
         "canonical ULT option; not modeled; no parameters exist"),
        ("gas_gap_heat_switch",
         "canonical switch option; only the superconducting switch is represented"),
    ):
        add(element, "", "NOT_IMPLEMENTED", "architecture registry only",
            load_W="", note=note + " -- no performance numbers invented")
    return rows, rmet
