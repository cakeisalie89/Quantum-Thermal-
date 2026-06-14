"""Vibration transfer through the cryostat chain (banded model):
floor/cryocooler -> frame -> cryo stages -> sample mount -> NV region.

Per stage and per frequency band: A_out(f) = A_in(f) * H_stage(f).
Dissipated heating: P_stage = c_eff_stage * sum_bands A_out(f)^2.
Settling time per stage from an effective damping/quality factor.

MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import math
from .units import require_positive, require_nonnegative

# Frequency bands [Hz] (representative center frequencies). DESIGN_SPECIFIED.
BANDS = {"low_freq": 1.0, "cryocooler": 1.4, "acoustic": 500.0}

# Input amplitudes [m] per band at the floor/cryocooler. ASSUMED.
DEFAULT_INPUT_AMP_M = {"low_freq": 1.0e-7, "cryocooler": 5.0e-7, "acoustic": 1.0e-8}

# Stage transfer (per-band attenuation H<1) and effective coupling. DESIGN/ASSUMED.
STAGE_TRANSFER = [
    ("frame",        {"low_freq": 0.5, "cryocooler": 0.3, "acoustic": 0.2}, 1.0e3),
    ("cryo_stages",  {"low_freq": 0.5, "cryocooler": 0.2, "acoustic": 0.1}, 1.0e3),
    ("sample_mount", {"low_freq": 0.3, "cryocooler": 0.1, "acoustic": 0.05}, 1.0e2),
    ("NV_region",    {"low_freq": 0.5, "cryocooler": 0.3, "acoustic": 0.1}, 1.0e1),
]


def vibration_transfer(input_amp_m=None, settling_quality_factor=20.0,
                       mode_d_amp_threshold_m=1.0e-10):
    """Propagate banded vibration amplitudes through the chain."""
    require_positive("settling_quality_factor", settling_quality_factor)
    amp = dict(DEFAULT_INPUT_AMP_M)
    if input_amp_m:
        amp.update(input_amp_m)
    profile = []
    cur = dict(amp)
    for name, H, c_eff in STAGE_TRANSFER:
        out = {b: cur[b] * H[b] for b in BANDS}
        # dissipated heating surrogate: c_eff * sum A_out^2 (units folded into c_eff)
        P = c_eff * sum(out[b] ** 2 for b in BANDS)
        atten = (sum(out.values()) / max(sum(cur.values()), 1e-30))
        # settling time ~ Q / (2 pi f_dominant)
        f_dom = max(BANDS, key=lambda b: out[b])
        settling = settling_quality_factor / (2.0 * math.pi * BANDS[f_dom])
        profile.append({
            "stage": name,
            "input_amplitude_m": sum(cur.values()),
            "output_amplitude_m": sum(out.values()),
            "attenuation_factor": atten,
            "dissipated_power_W": P,
            "settling_time_s": settling,
        })
        cur = out
    nv_amp = profile[-1]["output_amplitude_m"]
    metrics = {
        "nv_output_amplitude_m": nv_amp,
        "mode_d_amp_threshold_m": mode_d_amp_threshold_m,
        "Mode_D_vibration_ready_if_measured": bool(nv_amp <= mode_d_amp_threshold_m),
        "total_dissipated_power_W": sum(p["dissipated_power_W"] for p in profile),
        "max_settling_time_s": max(p["settling_time_s"] for p in profile),
        "finite": all(math.isfinite(p["dissipated_power_W"]) for p in profile),
    }
    return profile, metrics
