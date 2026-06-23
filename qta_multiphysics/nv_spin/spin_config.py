"""Typed configuration for the NV spin-dynamics layer.

Mirrors the validate-on-construction pattern used elsewhere in
``qta_multiphysics.config``. Every default is a forecast / ASSUMED value, not a
measurement. In particular ``tau_c_s`` defaults to the pre-existing QTA forecast
value 4e-3 s, but is tagged UNKNOWN/ASSUMED and is intended to be swept; it must
never be read as measured evidence or used to assert a PASS gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass(frozen=True)
class NVSpinConfig:
    # --- Mode D physical inputs (forecast / design-specified) -----------------
    T_K: float = 0.01                 # Mode D sample temperature [K] (10 mK base)
    B_T: float = 5.0e-3               # axial-ish bias field magnitude [T]
    theta_B_rad: float = 0.0          # field polar angle from NV axis [rad]
    E_strain_rad_s: float = 2.0 * math.pi * 1.0e5   # transverse strain split [rad/s] (ASSUMED)

    # --- decoherence inputs: tau_c (UNKNOWN), T1, T2 pure-dephasing ------------
    tau_c_s: float = 4.0e-3           # OU bath correlation time [s] (UNKNOWN forecast)
    tau_c_status: str = "UNKNOWN"     # provenance tag; never 'MEASURED'
    sigma_rad_s: float = 2.0 * math.pi * 5.0e3      # OU noise amplitude [rad/s] (ASSUMED)
    T1_s: float = 1.0e-2              # longitudinal relaxation [s] (ASSUMED forecast)
    T2_pure_s: float = 5.0e-4         # Markovian pure-dephasing time [s] (ASSUMED)

    # --- surface / isotope condition (parameterised forecast hypothesis) ------
    surface_condition: str = "bare_diamond_forecast"   # e.g. He3/He4 dosing label
    he_coverage_monolayers: float = 0.0                # forecast coverage (0 = none)

    # --- sequence sampling ----------------------------------------------------
    ramsey_max_s: float = 2.0e-3
    hahn_max_s: float = 8.0e-3
    xy8_max_s: float = 2.0e-2
    n_time_points: int = 24           # points along each coherence curve
    n_traj: int = 400                 # OU trajectories per coherence point
    n_time_inner: int = 200           # time-grid resolution within one point
    seed: int = 42                    # fixed seed (reproducibility)

    # --- ODMR -----------------------------------------------------------------
    odmr_span_Hz: float = 2.0e8       # +/- window around each line [Hz]
    odmr_points: int = 401
    odmr_linewidth_Hz: float = 1.0e6  # forecast linewidth [Hz]

    profile: str = "standard"         # 'standard' or 'ci'

    def validate(self) -> "NVSpinConfig":
        if not (self.T_K > 0):
            raise ValueError("T_K must be > 0")
        if not (self.B_T >= 0):
            raise ValueError("B_T must be >= 0")
        if not (self.tau_c_s > 0):
            raise ValueError("tau_c_s must be > 0")
        if self.tau_c_status.upper() in {"MEASURED", "VERIFIED"}:
            raise ValueError("tau_c must not be tagged MEASURED/VERIFIED (it is UNKNOWN)")
        if not (self.sigma_rad_s >= 0):
            raise ValueError("sigma_rad_s must be >= 0")
        if not (self.T1_s > 0):
            raise ValueError("T1_s must be > 0")
        if not (self.T2_pure_s > 0):
            raise ValueError("T2_pure_s must be > 0")
        if self.he_coverage_monolayers < 0:
            raise ValueError("he_coverage_monolayers must be >= 0")
        for nm in ("n_time_points", "n_traj", "n_time_inner", "odmr_points"):
            if getattr(self, nm) < 2:
                raise ValueError(f"{nm} must be >= 2")
        if self.profile not in {"standard", "ci"}:
            raise ValueError("profile must be 'standard' or 'ci'")
        return self

    def as_dict(self) -> dict:
        return asdict(self)


def ci_config(**overrides) -> NVSpinConfig:
    """Small, fast, deterministic configuration for CI."""
    base = dict(
        n_time_points=8, n_traj=80, n_time_inner=64, odmr_points=101, profile="ci",
    )
    base.update(overrides)
    return NVSpinConfig(**base).validate()


def standard_config(**overrides) -> NVSpinConfig:
    """Fuller configuration for normal runs."""
    return NVSpinConfig(profile="standard", **overrides).validate()
