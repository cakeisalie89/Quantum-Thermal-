"""Microwave / RF 1D path model: path-distributed attenuation and per-stage
dissipation along the coax/CPW line feeding the NV/sample region.

  P_remaining(x+dx) = P_remaining(x) exp(-att_per_m dx)
  P_dissipated_segment = P_in_segment (1 - exp(-att_per_m dx))

Line segments are assigned to thermal stages (300 K ... 10 mK, NV region).
B1/Rabi are reported only when a valid coupling is supplied.

MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import math
import numpy as np
from .units import require_positive, require_nonnegative

# Default staged line: (stage_name, length_m, attenuation_dB) — DESIGN_SPECIFIED.
DEFAULT_STAGES = [
    ("300K", 0.30, 0.0),
    ("77K", 0.20, 3.0),
    ("4K", 0.20, 20.0),
    ("1K", 0.10, 0.0),
    ("100mK", 0.10, 3.0),
    ("10mK", 0.05, 0.0),
    ("NV_region", 0.01, 0.0),  # termination/CPW at sample
]


def microwave_path_1d(input_power_W=1.0e-6, stages=None, termination_power_fraction=1.0e-3,
                      b1_per_sqrtW=None):
    """Distribute input power along staged line. Attenuation dissipates power at
    each stage (anchored at that stage's temperature). A small residual reaches
    the NV region (termination_power_fraction of the power arriving there)."""
    require_positive("input_power_W", input_power_W)
    stages = stages or DEFAULT_STAGES
    P = input_power_W
    diss = {}
    total_dB = 0.0
    for name, length_m, att_dB in stages:
        require_nonnegative(f"att_dB[{name}]", att_dB)
        frac_through = 10.0 ** (-att_dB / 10.0)
        P_in = P
        P_after = P_in * frac_through
        diss[name] = P_in - P_after
        total_dB += att_dB
        P = P_after
    # power arriving at NV region; termination dissipates a fraction there
    P_nv_region = P * termination_power_fraction
    diss["NV_region"] = diss.get("NV_region", 0.0) + P_nv_region

    metrics = {
        "total_input_power_W": input_power_W,
        "dissipated_power_10mK_W": diss.get("10mK", 0.0),
        "dissipated_power_NV_region_W": diss.get("NV_region", 0.0),
        "line_attenuation_total_dB": total_dB,
        "power_reaching_termination_W": P,
    }
    if b1_per_sqrtW is not None and P > 0:
        # Optional: B1 ~ coupling * sqrt(P_at_NV); Rabi = gamma_e * B1
        require_positive("b1_per_sqrtW", b1_per_sqrtW)
        B1 = b1_per_sqrtW * math.sqrt(max(P_nv_region, 0.0))
        gamma_e = 28.024e9  # Hz/T (electron gyromagnetic ratio /2pi)
        metrics["estimated_B1_T"] = B1
        metrics["estimated_Rabi_Hz"] = gamma_e * B1
    return diss, metrics
