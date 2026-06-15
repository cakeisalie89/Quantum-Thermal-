"""Optical absorption profiles (1D depth and 2D axisymmetric), built on the
shared LaserSource. These produce the absorbed-power density that the thermal
solvers consume, plus standalone diagnostic profiles/maps.

MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import numpy as np
from .config import MultiphysicsConfig
from .grids import Grid1D, AxisymmetricGrid2D
from .fields import Field1D, Field2D
from .laser_source import LaserSource


def optical_absorption_1d(cfg: MultiphysicsConfig, n=None):
    """Depth profile of absorbed-power density [W/m^3] at peak (pulse) deposition."""
    cfg.validate()
    n = int(n or cfg.solver.n_cells_1d)
    grid = Grid1D(cfg.geometry.thermal_depth_m, n, axis="z")
    laser = LaserSource(cfg.laser, mode="pulse")
    tpk = laser.pulse_center_s
    Q = laser.q_volumetric_1d(grid.centers, tpk, front_position_m=cfg.geometry.front_position_m)
    field = Field1D(grid, Q, name="Q_laser", unit="W_m3")
    metrics = {
        "absorption_depth_m": laser.absorption_depth_m(),
        "peak_volumetric_W_m3": field.max(),
        "peak_fluence_J_m2": laser.peak_fluence_J2() if hasattr(laser, "peak_fluence_J2") else laser.peak_fluence_J_m2(),
        "absorbed_average_power_W": cfg.laser.absorbed_average_power_W,
        "finite": field.is_finite(),
    }
    return grid, field, metrics


def optical_absorption_2d(cfg: MultiphysicsConfig, n_r=None, n_z=None):
    """2D (r,z) absorbed-power density map [W/m^3] and radial fluence."""
    cfg.validate()
    nr = int(n_r or cfg.solver.n_r_2d)
    nz = int(n_z or cfg.solver.n_z_2d)
    grid = AxisymmetricGrid2D(cfg.geometry.thermal_radius_m, cfg.geometry.thermal_depth_m, nr, nz)
    laser = LaserSource(cfg.laser, mode="pulse")
    tpk = laser.pulse_center_s
    Q = laser.q_volumetric_2d(grid.R, grid.Z, tpk, front_position_m=cfg.geometry.front_position_m)
    field = Field2D(grid, Q, name="Q_laser", unit="W_m3")
    # radial fluence at the surface (z=0 row), areal energy density per pulse
    E_abs = cfg.laser.absorbed_fraction * cfg.laser.pulse_energy_J
    radial_fluence = E_abs * laser.radial_density_1m2(grid.r_centers)  # [J/m^2]
    metrics = {
        "absorption_depth_m": laser.absorption_depth_m(),
        "peak_volumetric_W_m3": field.max(),
        "peak_fluence_J_m2": laser.peak_fluence_J_m2(),
        "spot_radius_m": cfg.laser.spot_radius_m,
        "finite": field.is_finite(),
    }
    return grid, field, radial_fluence, metrics
