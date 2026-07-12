"""Boundary treatment for the 3D transient thermal layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

The 3D layer uses exactly the boundary physics of the existing 1D/2D backends,
with no new claims:

* front face (z = 0): insulated (the deposited laser power enters volumetrically
  through the Beer-Lambert source, as in 1D/2D);
* lateral faces (|x| = R, |y| = R): adiabatic / zero-flux, the Cartesian
  counterpart of the 2D outer-radius treatment on the resolved near-surface box;
* back face (z = depth): Kapitza-radiative fridge sink,
      q_sink = alpha_K * (T_face^4 - T_fridge^4)   [W/m^2],
  with the same ASSUMED ``kapitza_coeff_W_m2_K4`` and MANUFACTURER_SPEC
  ``T_fridge_K`` used everywhere else in the repository.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import FridgeConfig


@dataclass
class BoundarySpec3D:
    front: str = "insulated"
    lateral: str = "adiabatic"
    back: str = "kapitza_radiative"

    def validate(self):
        if self.front != "insulated":
            raise ValueError("front boundary must be 'insulated' (volumetric deposition)")
        if self.lateral != "adiabatic":
            raise ValueError("lateral boundaries must be 'adiabatic' (zero-flux)")
        if self.back != "kapitza_radiative":
            raise ValueError("back boundary must be 'kapitza_radiative'")
        return self


def kapitza_sink_flux_W_m2(T_face, fridge: FridgeConfig):
    """Kapitza-radiative sink flux out of the back face [W/m^2] (ASSUMED form)."""
    alpha_K = fridge.kapitza_coeff_W_m2_K4
    Tf = fridge.T_fridge_K
    T_face = np.asarray(T_face, dtype=float)
    return alpha_K * (T_face ** 4 - Tf ** 4)
