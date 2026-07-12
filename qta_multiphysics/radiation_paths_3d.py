"""3D radiation-path budget hook: per-stage rows from the canonical chain.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Reuses ``radiation_paths.radiation_paths`` (the 300 K -> 77 K -> 4 K -> 1 K ->
100 mK -> 10 mK chain with shutter/baffle attenuation) verbatim; no new
radiative physics. 3D view-factor resolution of the chamber geometry is
NOT_IMPLEMENTED and reported as such. Deterministic.
"""
from __future__ import annotations

from .radiation_paths import radiation_paths

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def budget_rows(shutter_state: str = "closed", baffle_state: str = "engaged"):
    paths, metrics = radiation_paths(shutter_state=shutter_state,
                                     baffle_state=baffle_state)
    rows = []
    for p in paths:
        r = {k: (f"{v:.9e}" if isinstance(v, float) else v) for k, v in p.items()}
        r["resolution"] = "STAGE_CHAIN_1D (canonical)"
        r["three_d_view_factors"] = "NOT_IMPLEMENTED"
        r["label"] = LABEL
        rows.append(r)
    rows.append({"path": "TOTAL_reaching_10mK",
                 "load_W": f"{metrics['total_heat_load_to_10mK_W']:.9e}",
                 "resolution": "STAGE_CHAIN_1D (canonical)",
                 "three_d_view_factors": "NOT_IMPLEMENTED", "label": LABEL})
    return rows, metrics
