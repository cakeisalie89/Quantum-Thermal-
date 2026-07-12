"""3D vibration-path budget hook: per-path rows from the canonical chain.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Reuses ``vibration_transfer.vibration_transfer`` (floor/cryocooler -> frame ->
cryo stages -> sample mount -> NV region attenuation chain) verbatim; no new
vibro-mechanical physics. 3D modal analysis of the support structure is
NOT_IMPLEMENTED and reported as such. Deterministic.
"""
from __future__ import annotations

from .vibration_transfer import vibration_transfer

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def budget_rows():
    profile, metrics = vibration_transfer()
    rows = []
    for p in profile:
        r = {k: (f"{v:.9e}" if isinstance(v, float) else v) for k, v in p.items()}
        r["resolution"] = "TRANSFER_CHAIN_1D (canonical)"
        r["three_d_modal_analysis"] = "NOT_IMPLEMENTED"
        r["label"] = LABEL
        rows.append(r)
    return rows, metrics
