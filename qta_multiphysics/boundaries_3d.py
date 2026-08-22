"""Boundary treatment for the 3D transient thermal layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

The 3D layer shares the front and back boundary physics of the 1D/2D backends.
It does NOT share the lateral treatment, and this file used to claim it did.

* front face (z = 0): insulated (the deposited laser power enters volumetrically
  through the Beer-Lambert source, as in 1D/2D) -- same as 1D/2D;
* back face (z = depth): Kapitza-radiative fridge sink,
      q_sink = alpha_K * (T_face^4 - T_fridge^4)   [W/m^2],
  with the same ASSUMED ``kapitza_coeff_W_m2_K4`` and MANUFACTURER_SPEC
  ``T_fridge_K`` used everywhere else -- same as 1D/2D;
* lateral faces (|x| = R, |y| = R): adiabatic / zero-flux. This is NOT the
  Cartesian counterpart of the 2D outer-radius treatment, which this file
  previously asserted. thermal_2d_axisymmetric applies a COLD RADIAL CONTACT at
  r = R -- a half-cell conduction path to surrounding bulk at T_fridge, i.e. a
  heat sink. Adiabatic and cold-contact are opposite in the sign of boundary
  heat flow: one exports energy, the other reflects it. Calling them
  counterparts asserted an equivalence that does not exist.

WHAT THE ADIABATIC LATERAL FACE IS AND IS NOT. It is not a far-field
approximation. Over the pulse window (5e-6 s) the thermal diffusion length
sqrt(alpha*t) is ~4.4e-4 m at the modelled diffusivity, about 11x the 4e-5 m box
half-extent; over the recovery window (0.2 s) it is ~0.088 m, about 2200x. Heat
reaches the lateral boundary essentially immediately on both timescales, so the
adiabatic wall acts as a mirror that confines energy in the resolved box rather
than as a distant boundary whose flux is negligible. The consequence is
directional and should be read that way: 3D peak temperatures and recovery
times are biased HIGH relative to a model that lets heat leave sideways.

Whether the production 3D lateral face SHOULD instead carry a cold contact to
surrounding bulk (as 2D does) is a modelling decision that would change every
3D output; it is not taken here and is recorded as requiring owner authority.
BoundarySpec3D.validate() still enforces adiabatic, so the current behaviour is
unchanged -- only the claim about it is corrected.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import FridgeConfig


#: Measured evidence for the note above: diffusion length over each window
#: divided by the box half-extent. Recomputed by tests, not asserted here.
LATERAL_CONFINEMENT_NOTE = (
    "adiabatic lateral faces sit far inside the thermal diffusion length "
    "(~11x the half-extent over the pulse window, ~2200x over the recovery "
    "window); this is a confining boundary, not a negligible-flux one")

#: The 2D backend's lateral treatment, named so a reader cannot assume parity.
LATERAL_2D_TREATMENT = "cold radial contact to bulk at T_fridge (heat sink)"
LATERAL_3D_TREATMENT = "adiabatic / zero-flux (no lateral heat export)"


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
