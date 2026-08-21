"""3D radiation-path budget hook: per-stage rows from the canonical chain.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Reuses ``radiation_paths.radiation_paths`` (the 300 K -> 77 K -> 4 K -> 1 K ->
100 mK -> 10 mK chain with shutter/baffle attenuation) verbatim; no new
radiative physics. 3D view-factor resolution of the chamber geometry is
NOT_IMPLEMENTED and reported as such. Deterministic.
"""
from __future__ import annotations

from .radiation_paths import (RADIATION_PATH_FIELDS, radiation_paths,
                              validate_radiation_paths)

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

# Columns this module adds on top of the canonical path contract.
BUDGET_ANNOTATION_FIELDS = ("row_kind", "resolution", "three_d_view_factors",
                            "label")
BUDGET_FIELDS = RADIATION_PATH_FIELDS + BUDGET_ANNOTATION_FIELDS

# The aggregate row is not a stage hop. It previously emitted its own field
# names ("path", "load_W"), which the canonical path rows do not use; the CSV
# header was taken from the first row, so the total radiative load reaching the
# 10 mK region was written as an empty cell. The aggregate now reports through
# the same declared contract, tagged by ``row_kind`` so a consumer can exclude
# it from a per-hop sum instead of inferring intent from a name.
ROW_KIND_PATH = "PATH"
ROW_KIND_TOTAL = "TOTAL"


def _fmt(v):
    return f"{v:.9e}" if isinstance(v, float) else v


def budget_rows(shutter_state: str = "closed", baffle_state: str = "engaged"):
    paths, metrics = radiation_paths(shutter_state=shutter_state,
                                     baffle_state=baffle_state)
    validate_radiation_paths(paths, metrics)
    rows = []
    for p in paths:
        r = {k: _fmt(p[k]) for k in RADIATION_PATH_FIELDS}
        r["row_kind"] = ROW_KIND_PATH
        r["resolution"] = "STAGE_CHAIN_1D (canonical)"
        r["three_d_view_factors"] = "NOT_IMPLEMENTED"
        r["label"] = LABEL
        rows.append(r)
    rows.append({
        "path_name": "TOTAL_reaching_10mK",
        "source_stage": "ALL_STAGES",
        "sink_stage": "10mK_region",
        "heat_load_W": _fmt(float(metrics["total_heat_load_to_10mK_W"])),
        "attenuation_factor": _fmt(float(metrics["attenuation_factor"])),
        "shutter_state": metrics["shutter_state"],
        "baffle_state": metrics["baffle_state"],
        "reaches_10mK_region": True,
        "row_kind": ROW_KIND_TOTAL,
        "resolution": "STAGE_CHAIN_1D (canonical)",
        "three_d_view_factors": "NOT_IMPLEMENTED",
        "label": LABEL,
    })
    for i, r in enumerate(rows):
        if tuple(r) != BUDGET_FIELDS:
            raise ValueError(
                f"radiation budget row {i} does not match the declared "
                f"contract {BUDGET_FIELDS}; got {tuple(r)}")
    return rows, metrics
