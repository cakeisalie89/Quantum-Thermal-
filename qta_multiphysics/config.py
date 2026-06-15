"""Validated SI configuration objects for the QTA multiphysics layer.

Every default here is a forecast / DESIGN_SPECIFIED / ASSUMED value, NOT measured
in this system. Defaults are chosen to be consistent with the rest of the QTA
package (10 mK dilution-fridge base temperature; femtosecond process laser
architecture; diamond NV host). All values are validated on construction.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .units import (require_positive, require_nonnegative, require_temperature,
                    require_fraction, DIAMOND_DENSITY_KG_M3)


@dataclass
class GeometryConfig:
    sample_depth_m: float = 2.0e-3      # full diamond thickness along z [m]
    sample_radius_m: float = 1.5e-3     # full radial half-extent [m]
    nv_layer_depth_m: float = 1.0e-8    # NV layer depth below front surface [m] (~10 nm)
    front_position_m: float = 0.0       # s0, initial process front [m]
    front_velocity_m_s: float = 0.0     # v_front [m/s] (0 = static front, first pass)
    # Near-field THERMAL domain. The fs spot (~5 um) and absorption depth (~1 um)
    # are microscopic relative to the mm-scale sample, so the thermal spike is a
    # near-surface, near-axis phenomenon. The thermal solvers resolve a near-field
    # micro-domain (a few spot radii / absorption depths) embedded in cold bulk;
    # the outer radial and backside boundaries represent the surrounding cold
    # material / path to the fridge. This is an explicit reduced-order choice.
    thermal_radius_m: float = 4.0e-5    # ~8 x spot radius [m]
    thermal_depth_m: float = 4.0e-5     # near-surface depth resolved [m]

    def validate(self):
        require_positive("sample_depth_m", self.sample_depth_m)
        require_positive("sample_radius_m", self.sample_radius_m)
        require_nonnegative("nv_layer_depth_m", self.nv_layer_depth_m)
        require_nonnegative("front_position_m", self.front_position_m)
        require_nonnegative("front_velocity_m_s", self.front_velocity_m_s)
        require_positive("thermal_radius_m", self.thermal_radius_m)
        require_positive("thermal_depth_m", self.thermal_depth_m)
        if self.nv_layer_depth_m >= self.thermal_depth_m:
            raise ValueError("nv_layer_depth_m must be < thermal_depth_m")
        if self.thermal_depth_m > self.sample_depth_m:
            raise ValueError("thermal_depth_m must be <= sample_depth_m")
        return self


@dataclass
class MaterialConfig:
    rho_kg_m3: float = DIAMOND_DENSITY_KG_M3
    # Reduced k(T) model parameters (ASSUMED).
    k_ref_W_mK: float = 2000.0
    k_T_ref_K: float = 100.0
    k_exponent: float = 3.0
    k_plateau_W_mK: float = 3000.0

    def validate(self):
        require_positive("rho_kg_m3", self.rho_kg_m3)
        require_positive("k_ref_W_mK", self.k_ref_W_mK)
        require_positive("k_T_ref_K", self.k_T_ref_K)
        require_positive("k_exponent", self.k_exponent)
        require_positive("k_plateau_W_mK", self.k_plateau_W_mK)
        return self

    def k_kwargs(self):
        return dict(k_ref=self.k_ref_W_mK, T_ref=self.k_T_ref_K,
                    exponent=self.k_exponent, k_plateau=self.k_plateau_W_mK)


@dataclass
class LaserConfig:
    # Femtosecond process-laser architecture (Mode B). DESIGN_SPECIFIED / forecast.
    pulse_energy_J: float = 1.0e-9        # 1 nJ per pulse (design target)
    pulse_duration_s: float = 250.0e-15   # 250 fs
    repetition_rate_Hz: float = 1.0e6     # 1 MHz
    spot_radius_m: float = 5.0e-6         # w0 = 5 um (1/e^2 radius)
    absorbed_fraction: float = 0.30       # absorbed_fraction (ASSUMED)
    absorption_coeff_1_m: float = 1.0e6   # alpha [1/m] (Beer-Lambert; ASSUMED)
    wavelength_nm: float = 1030.0         # 1030 nm class
    temporal_profile: str = "gaussian"    # "gaussian" or "tophat"

    def validate(self):
        require_positive("pulse_energy_J", self.pulse_energy_J)
        require_positive("pulse_duration_s", self.pulse_duration_s)
        require_positive("repetition_rate_Hz", self.repetition_rate_Hz)
        require_positive("spot_radius_m", self.spot_radius_m)
        require_fraction("absorbed_fraction", self.absorbed_fraction)
        require_positive("absorption_coeff_1_m", self.absorption_coeff_1_m)
        require_positive("wavelength_nm", self.wavelength_nm)
        if self.temporal_profile not in ("gaussian", "tophat"):
            raise ValueError("temporal_profile must be 'gaussian' or 'tophat'")
        # duty-cycle sanity: pulse must fit inside its period
        if self.pulse_duration_s >= 1.0 / self.repetition_rate_Hz:
            raise ValueError("pulse_duration_s must be < 1/repetition_rate_Hz")
        return self

    @property
    def average_power_W(self) -> float:
        return self.pulse_energy_J * self.repetition_rate_Hz

    @property
    def absorbed_average_power_W(self) -> float:
        return self.absorbed_fraction * self.average_power_W


@dataclass
class FridgeConfig:
    T_fridge_K: float = 0.010             # 10 mK base (Oxford Triton spec; MANUFACTURER_SPEC)
    kapitza_coeff_W_m2_K4: float = 50.0   # alpha_K [W/m^2/K^4] (ASSUMED Kapitza-radiative form)
    T_background_K: float = 0.010
    background_flux_W_m3: float = 0.0     # volumetric background heating [W/m^3]

    def validate(self):
        require_temperature("T_fridge_K", self.T_fridge_K)
        require_positive("kapitza_coeff_W_m2_K4", self.kapitza_coeff_W_m2_K4)
        require_temperature("T_background_K", self.T_background_K)
        require_nonnegative("background_flux_W_m3", self.background_flux_W_m3)
        return self


@dataclass
class SolverConfig:
    n_cells_1d: int = 200
    n_r_2d: int = 48
    n_z_2d: int = 64
    rtol: float = 1.0e-6
    atol: float = 1.0e-9
    method: str = "BDF"                    # stiff integrator
    pulse_window_s: float = 5.0e-6        # short window for pulse-resolved runs
    recovery_window_s: float = 2.0e-1     # long window for recool/recovery runs
    mode_d_temp_threshold_K: float = 0.050  # NV-layer must recool below this for Mode D readiness

    def validate(self):
        if self.n_cells_1d < 10:
            raise ValueError("n_cells_1d too small")
        if self.n_r_2d < 8 or self.n_z_2d < 8:
            raise ValueError("2D mesh too small")
        require_positive("rtol", self.rtol)
        require_positive("atol", self.atol)
        require_positive("pulse_window_s", self.pulse_window_s)
        require_positive("recovery_window_s", self.recovery_window_s)
        require_temperature("mode_d_temp_threshold_K", self.mode_d_temp_threshold_K)
        if self.method not in ("BDF", "Radau", "LSODA"):
            raise ValueError("method must be a stiff integrator (BDF/Radau/LSODA)")
        return self


@dataclass
class MultiphysicsConfig:
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    material: MaterialConfig = field(default_factory=MaterialConfig)
    laser: LaserConfig = field(default_factory=LaserConfig)
    fridge: FridgeConfig = field(default_factory=FridgeConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)

    def validate(self):
        self.geometry.validate()
        self.material.validate()
        self.laser.validate()
        self.fridge.validate()
        self.solver.validate()
        return self


def default_config() -> MultiphysicsConfig:
    return MultiphysicsConfig().validate()
