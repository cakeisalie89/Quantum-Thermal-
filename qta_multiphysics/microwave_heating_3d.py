"""3D microwave/RF heating budget hook from the canonical delivery line model.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Reuses ``microwave_heating_1d.microwave_path_1d`` (per-stage dissipation along
the coax/CPW line) verbatim; no new electromagnetic physics. A 3D field /
SAR map of the microwave delivery in the chamber is NOT_IMPLEMENTED and
reported as such. Deterministic.
"""
from __future__ import annotations

from .microwave_heating_1d import microwave_path_1d

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def budget_rows():
    diss, metrics = microwave_path_1d()
    rows = [{"stage": stage, "dissipated_W": f"{w:.9e}",
             "resolution": "LINE_MODEL_1D (canonical)",
             "three_d_field_map": "NOT_IMPLEMENTED", "label": LABEL}
            for stage, w in diss.items()]
    rows.append({"stage": "NV_region_total",
                 "dissipated_W": f"{metrics['dissipated_power_NV_region_W']:.9e}",
                 "resolution": "LINE_MODEL_1D (canonical)",
                 "three_d_field_map": "NOT_IMPLEMENTED", "label": LABEL})
    return rows, metrics
