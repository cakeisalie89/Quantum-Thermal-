"""Radiation leakage via a staged view-factor cascade:
300 K -> 77 K -> 4 K -> 1 K -> 100 mK -> 10 mK -> sample.

  q_ij = sigma * epsilon_eff * F_ij * A_ij * (T_i^4 - T_j^4)

attenuated by shutter / labyrinth-baffle / aperture-stop / optical-filter /
line-of-sight factors.

MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
from .units import SIGMA_SB, require_positive, require_fraction

# Stage chain (name, temperature_K). DESIGN_SPECIFIED.
STAGE_CHAIN = [
    ("300K", 300.0), ("77K", 77.0), ("4K", 4.0),
    ("1K", 1.0), ("100mK", 0.1), ("10mK", 0.010), ("sample", 0.010),
]

# Per-hop attenuation factors (product applied to each path). ASSUMED/DESIGN.
DEFAULT_FACTORS = {
    "shutter_closed_factor": 1.0e-3,
    "labyrinth_baffle_factor": 1.0e-2,
    "aperture_stop_factor": 1.0e-1,
    "optical_filter_factor": 5.0e-1,
    "line_of_sight_multiplier": 1.0,
}


def radiation_paths(epsilon_eff=0.05, view_factor=0.1, area_m2=1.0e-4,
                    factors=None, shutter_state="closed", baffle_state="engaged"):
    """Compute per-hop radiative heat loads down the stage chain."""
    require_fraction("epsilon_eff", epsilon_eff)
    require_fraction("view_factor", view_factor)
    require_positive("area_m2", area_m2)
    f = dict(DEFAULT_FACTORS)
    if factors:
        f.update(factors)
    atten = (f["labyrinth_baffle_factor"] if baffle_state == "engaged" else 1.0)
    atten *= (f["shutter_closed_factor"] if shutter_state == "closed" else 1.0)
    atten *= f["aperture_stop_factor"] * f["optical_filter_factor"] * f["line_of_sight_multiplier"]

    paths = []
    reaches_10mK = 0.0
    for (sname, Ts), (kname, Tk) in zip(STAGE_CHAIN[:-1], STAGE_CHAIN[1:]):
        q_raw = SIGMA_SB * epsilon_eff * view_factor * area_m2 * (Ts**4 - Tk**4)
        q = q_raw * atten
        reaches = kname in ("10mK", "sample")
        if reaches:
            reaches_10mK += max(q, 0.0)
        paths.append({
            "path_name": f"{sname}->{kname}",
            "source_stage": sname, "sink_stage": kname,
            "heat_load_W": q, "attenuation_factor": atten,
            "shutter_state": shutter_state, "baffle_state": baffle_state,
            "reaches_10mK_region": reaches,
        })
    metrics = {
        "total_heat_load_to_10mK_W": reaches_10mK,
        "attenuation_factor": atten,
        "shutter_state": shutter_state, "baffle_state": baffle_state,
        "finite": all(__import__("math").isfinite(p["heat_load_W"]) for p in paths),
    }
    return paths, metrics
