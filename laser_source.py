"""LaserSource: shared optical-deposition model for the thermal solvers.

Supports the package's femtosecond process-laser architecture. Two operating
modes (both genuinely implemented):

  * pulse-resolved ("pulse"): a single pulse's energy is deposited with a
    Gaussian (or top-hat) temporal envelope of width pulse_duration_s. Used to
    estimate the per-pulse thermal spike and immediate relaxation. Femtosecond
    pulses are far shorter than thermal diffusion times, so this is a
    reduced-order surrogate: the absorbed pulse energy is correct; the temporal
    envelope is a numerically tractable stand-in (documented, not hidden).

  * averaged ("averaged"): the time-averaged absorbed power
    (absorbed_fraction * pulse_energy * repetition_rate) is applied as a
    continuous source. Used for long-window recool/recovery forecasts.

Spatial deposition is Beer-Lambert in depth (alpha) and Gaussian in radius
(1/e^2 radius = spot_radius). All outputs are volumetric heat rates [W/m^3].

MODEL-ONLY. No measured data.
"""
from __future__ import annotations
import math
import numpy as np
from .config import LaserConfig
from .units import require_positive


class LaserSource:
    def __init__(self, cfg: LaserConfig, mode: str = "averaged"):
        self.cfg = cfg.validate()
        if mode not in ("averaged", "pulse"):
            raise ValueError("LaserSource mode must be 'averaged' or 'pulse'")
        self.mode = mode

    # ---- temporal envelope ----
    def temporal_power_W(self, t):
        """Total absorbed optical power entering the sample at time t [W]."""
        c = self.cfg
        if self.mode == "averaged":
            return np.full_like(np.asarray(t, dtype=float), c.absorbed_average_power_W)
        # pulse-resolved: deposit one pulse's absorbed energy centered at t0.
        E_abs = c.absorbed_fraction * c.pulse_energy_J
        t = np.asarray(t, dtype=float)
        if c.temporal_profile == "tophat":
            tau = c.pulse_duration_s
            P0 = E_abs / tau
            return np.where((t >= 0.0) & (t <= tau), P0, 0.0)
        # gaussian: sigma so FWHM ~ pulse_duration; normalize integral to E_abs.
        sigma = c.pulse_duration_s / 2.3548
        t0 = 5.0 * sigma
        norm = E_abs / (sigma * math.sqrt(2.0 * math.pi))
        return norm * np.exp(-0.5 * ((t - t0) / sigma) ** 2)

    @property
    def pulse_center_s(self):
        sigma = self.cfg.pulse_duration_s / 2.3548
        return 5.0 * sigma

    # ---- spatial profiles ----
    def depth_density_1m(self, z, front_position_m=0.0):
        """Beer-Lambert absorbed-power density per unit depth, normalized so its
        integral over z (from the front) equals 1. Units [1/m]."""
        c = self.cfg
        z = np.asarray(z, dtype=float)
        zz = z - front_position_m
        prof = np.where(zz >= 0.0, c.absorption_coeff_1_m * np.exp(-c.absorption_coeff_1_m * zz), 0.0)
        return prof

    def radial_density_1m2(self, r):
        """Gaussian radial absorbed-power areal density [1/m^2], normalized so
        the cylindrical integral int 2*pi*r * f(r) dr = 1.

        For I(r) ~ exp(-2 r^2 / w0^2), the normalizing constant is 2/(pi w0^2)."""
        c = self.cfg
        r = np.asarray(r, dtype=float)
        w0 = c.spot_radius_m
        return (2.0 / (math.pi * w0 ** 2)) * np.exp(-2.0 * r ** 2 / w0 ** 2)

    # ---- volumetric heat for 1D (depth only; per unit area) ----
    def q_volumetric_1d(self, z, t, front_position_m=0.0):
        """Q_laser(z,t) [W/m^3] for the 1D slab (per unit cross-sectional area).

        P(t) [W] distributed over depth by Beer-Lambert. Because the 1D model is
        per unit area, we divide the areal power P(t)/A by depth-profile; but for
        a slab we treat the *areal* absorbed power as P(t)/A_spot, then multiply
        by the depth density [1/m]. We work per unit area of the spot, so the
        areal power is P(t)/A_spot with A_spot = pi*w0^2/2 (Gaussian effective
        area). Result is [W/m^3]."""
        c = self.cfg
        A_spot = 0.5 * math.pi * c.spot_radius_m ** 2  # effective Gaussian area
        P = self.temporal_power_W(t)
        areal = P / A_spot  # [W/m^2]
        return areal * self.depth_density_1m(z, front_position_m)

    # ---- volumetric heat for 2D axisymmetric ----
    def q_volumetric_2d(self, R, Z, t, front_position_m=0.0):
        """Q_laser(r,z,t) [W/m^3] on a 2D (r,z) mesh.

        Q = P(t) * radial_density(r) [1/m^2] * depth_density(z) [1/m]
        so that the cylindrical volume integral int Q * 2*pi*r dr dz = P(t)."""
        P = self.temporal_power_W(t)
        rad = self.radial_density_1m2(R)            # [1/m^2]
        dep = self.depth_density_1m(Z, front_position_m)  # [1/m]
        return P * rad * dep

    # ---- diagnostics ----
    def absorption_depth_m(self):
        return 1.0 / self.cfg.absorption_coeff_1_m

    def peak_fluence_J_m2(self):
        """Per-pulse peak areal fluence at beam center [J/m^2]."""
        c = self.cfg
        E_abs = c.absorbed_fraction * c.pulse_energy_J
        return E_abs * (2.0 / (math.pi * c.spot_radius_m ** 2))
