"""Gas transport 2D: molecular-beam exposure / capture map on the sample surface.

This is a genuine reduced model (not a PDE): the process beam from the inlet
illuminates the sample with a Gaussian footprint; line-of-sight to the sample is
attenuated by a baffle aperture (transmission + cosine obliquity). The result is
a radial dose profile on the sample surface and a sample-region contamination
map for shutter-open vs shutter-closed.

Output is a (config x radius) map. MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import math
import numpy as np
from .units import require_positive, require_fraction


def gas_exposure_map_2d(footprint_radius_m=8.0e-4, sample_radius_m=1.5e-3,
                        aperture_transmission=0.2, incidence_deg=15.0,
                        beam_flux_m2_s=1.0e18, n_r=60, shutter_states=("open", "closed"),
                        shutter_closed_attenuation=1.0e-6):
    """Return (r, {state: dose_flux[r]}, metrics).

    dose_flux(r) = beam_flux * exp(-2 r^2 / footprint^2) * aperture_transmission
                   * cos(incidence) * shutter_factor."""
    require_positive("footprint_radius_m", footprint_radius_m)
    require_positive("sample_radius_m", sample_radius_m)
    require_fraction("aperture_transmission", aperture_transmission)
    r = np.linspace(0.0, sample_radius_m, n_r)
    cosfac = math.cos(math.radians(incidence_deg))
    gauss = np.exp(-2.0 * r**2 / footprint_radius_m**2)
    maps = {}
    for st in shutter_states:
        sf = 1.0 if st == "open" else shutter_closed_attenuation
        maps[st] = beam_flux_m2_s * gauss * aperture_transmission * cosfac * sf
    metrics = {
        "footprint_radius_m": footprint_radius_m,
        "aperture_transmission": aperture_transmission,
        "incidence_deg": incidence_deg,
        "peak_dose_flux_open_m2_s": float(maps["open"].max()),
        "peak_dose_flux_closed_m2_s": float(maps.get("closed", np.array([0.0])).max()),
        "sample_edge_dose_fraction": float(gauss[-1]),
        "finite": bool(np.all([np.all(np.isfinite(v)) for v in maps.values()])),
    }
    return r, maps, metrics
