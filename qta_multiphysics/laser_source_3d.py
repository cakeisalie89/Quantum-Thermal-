"""Volumetric femtosecond laser absorption source on the 3D grid.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Reuses the shared ``LaserSource`` (temporal envelope: 'averaged' CW-average or
'pulse'-resolved Gaussian/tophat; Beer-Lambert depth density) and adds the
transverse Gaussian beam profile:

    Q(x, y, z, t) = P_abs(t) * g2(x, y) * b(z)          [W/m^3]

with g2 the normalized transverse density (integral over the plane = 1),
g2(r) = (2 / (pi w0^2)) exp(-2 r^2 / w0^2) for 1/e^2 radius w0
(= ``LaserConfig.spot_radius_m``), and b(z) the normalized Beer-Lambert depth
density from ``LaserSource.depth_density_1m``. Two uniform transverse modes exist solely
for reduction checks: 'uniform_xy' (g2 = 1/box area; conserves total power over
the box) and 'uniform_peak' (g2 = 2/(pi w0^2) everywhere, i.e. every column sees
exactly the 1D backend's areal intensity P/(pi w0^2/2), making the 3D->1D
comparison like-for-like).
All absorbed powers/fractions are the same ASSUMED values used by the 1D/2D
backends; nothing here is measured.
"""
from __future__ import annotations

import numpy as np

from .config import LaserConfig
from .laser_source import LaserSource


class LaserSource3D:
    def __init__(self, cfg: LaserConfig, mode: str = "averaged",
                 transverse: str = "gaussian", beam_center_xy=(0.0, 0.0),
                 box_area_m2: float | None = None):
        if transverse not in ("gaussian", "uniform_xy", "uniform_peak"):
            raise ValueError("transverse must be 'gaussian', 'uniform_xy' or 'uniform_peak'")
        if transverse == "uniform_xy" and not (box_area_m2 and box_area_m2 > 0):
            raise ValueError("uniform_xy requires a positive box_area_m2")
        self.base = LaserSource(cfg, mode=mode)
        self.cfg = self.base.cfg
        self.mode = mode
        self.transverse = transverse
        self.beam_center_xy = (float(beam_center_xy[0]), float(beam_center_xy[1]))
        self.box_area_m2 = box_area_m2

    # ---- normalized transverse density [1/m^2] ----
    def transverse_density_1m2(self, X, Y):
        if self.transverse == "uniform_xy":
            return np.full(np.broadcast(X, Y).shape, 1.0 / self.box_area_m2)
        if self.transverse == "uniform_peak":
            w0 = self.cfg.spot_radius_m
            return np.full(np.broadcast(X, Y).shape, 2.0 / (np.pi * w0 ** 2))
        w0 = self.cfg.spot_radius_m
        x0, y0 = self.beam_center_xy
        r2 = (np.asarray(X, dtype=float) - x0) ** 2 + (np.asarray(Y, dtype=float) - y0) ** 2
        return (2.0 / (np.pi * w0 ** 2)) * np.exp(-2.0 * r2 / w0 ** 2)

    # ---- full volumetric source [W/m^3] ----
    def q_volumetric_3d(self, X, Y, Z, t, front_position_m: float = 0.0):
        P = float(np.asarray(self.base.temporal_power_W(t)))
        g2 = self.transverse_density_1m2(X, Y)
        bz = self.base.depth_density_1m(Z, front_position_m=front_position_m)
        return P * g2 * bz

    @property
    def pulse_center_s(self):
        return self.base.pulse_center_s

    def absorption_depth_m(self):
        return self.base.absorption_depth_m()

    def captured_power_W(self, grid, t: float = 0.0,
                         front_position_m: float = 0.0) -> float:
        """Analytic power captured inside the grid box at time t [W].

        P(t) * F_xy * F_z with F_xy the transverse-density integral over the
        lateral box and F_z the Beer-Lambert fraction inside [z_min, z_max].
        Used by the source-integral verification check; deterministic.
        """
        P = float(np.asarray(self.base.temporal_power_W(t)))
        return P * float(self.cell_fractions(grid, front_position_m).sum())

    def cell_fractions(self, grid, front_position_m: float = 0.0):
        """Exact per-cell deposited POWER FRACTION array (nx, ny, nz).

        Analytic integrals of the separable source over every finite-volume
        cell (error-function integrals across x/y cell faces; exponential
        Beer-Lambert differences across z faces), so the discrete deposition
        is exactly conservative: ``sum(cell_fractions)`` equals the analytic
        captured fraction of the box to machine precision. Deterministic.
        """
        from scipy.special import erf
        xf, yf, zf = grid.x_faces, grid.y_faces, grid.z_faces
        if self.transverse == "uniform_xy":
            fx = np.diff(xf) / (xf[-1] - xf[0])
            fy = np.diff(yf) / (yf[-1] - yf[0])
        elif self.transverse == "uniform_peak":
            dens = 2.0 / (np.pi * self.cfg.spot_radius_m ** 2)
            fx = np.diff(xf) * np.sqrt(dens)
            fy = np.diff(yf) * np.sqrt(dens)
        else:
            w0 = self.cfg.spot_radius_m
            x0, y0 = self.beam_center_xy
            ex = 0.5 * erf(np.sqrt(2.0) * (xf - x0) / w0)
            ey = 0.5 * erf(np.sqrt(2.0) * (yf - y0) / w0)
            fx, fy = np.diff(ex), np.diff(ey)
        delta = self.absorption_depth_m()
        zz = np.maximum(zf - front_position_m, 0.0)
        fz = np.exp(-zz[:-1] / delta) - np.exp(-zz[1:] / delta)
        return fx[:, None, None] * fy[None, :, None] * fz[None, None, :]
