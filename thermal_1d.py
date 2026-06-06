"""Thermal 1D non-lumped solver: T(z,t) in a diamond slab with a moving process
front, depth-resolved laser absorption, optional microwave/background volumetric
heating, and a Kapitza-type radiative sink at the backside.

  rho Cp(T) dT/dt = d/dz[k(T) dT/dz] + Q_laser + Q_mw + Q_bg
  front:  s(t) = s0 + v_front * t   (structured so a Stefan condition can replace it)
  laser:  Q_laser(z,t) = areal_power(t) * alpha * exp[-alpha (z - s(t))], z >= s(t)
  sink BC at z=L: -k dT/dz|L = alpha_K (T(L)^4 - T_fridge^4)

Method-of-lines + solve_ivp (BDF). MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import numpy as np
from scipy.integrate import solve_ivp

from .grids import Grid1D
from .fields import Field1D
from .config import MultiphysicsConfig
from .material_models import diamond_cp, diamond_k
from .numerics import harmonic_face_k, assert_finite
from .laser_source import LaserSource


class Thermal1DResult:
    def __init__(self, grid, t, T, cfg, source_mode, solver_status, message):
        self.grid = grid
        self.t = t                  # (Nt,)
        self.T = T                  # (n, Nt)
        self.cfg = cfg
        self.source_mode = source_mode
        self.solver_status = solver_status
        self.message = message

    # ---- derived metrics ----
    def _nv_index(self):
        zc = self.grid.centers
        return int(np.argmin(np.abs(zc - self.cfg.geometry.nv_layer_depth_m)))

    def nv_layer_temperature_K(self):
        return float(self.T[self._nv_index(), :].max())

    def nv_layer_temperature_final_K(self):
        return float(self.T[self._nv_index(), -1])

    def hotspot_temperature_K(self):
        return float(self.T.max())

    def hotspot_depth_m(self):
        i, _ = np.unravel_index(int(np.argmax(self.T)), self.T.shape)
        return float(self.grid.centers[i])

    def max_gradient_K_per_m(self):
        g = np.gradient(self.T, self.grid.centers, axis=0)
        return float(np.max(np.abs(g)))

    def post_pulse_drift_K(self):
        """NV-layer temperature change over the second half of the window."""
        nv = self.T[self._nv_index(), :]
        half = len(nv) // 2
        return float(abs(nv[-1] - nv[half]))

    def recool_time_s(self, threshold_K):
        """First time the NV-layer temperature falls below threshold_K after its
        peak; np.inf if never within the window."""
        nv = self.T[self._nv_index(), :]
        ipk = int(np.argmax(nv))
        for k in range(ipk, len(nv)):
            if nv[k] <= threshold_K:
                return float(self.t[k])
        return float("inf")

    def final_profile(self):
        return Field1D(self.grid, self.T[:, -1], name="T", unit="K")


def _front_position(cfg, t):
    g = cfg.geometry
    return g.front_position_m + g.front_velocity_m_s * t


def solve_thermal_1d(cfg: MultiphysicsConfig, source_mode="averaged",
                     t_end=None, q_mw_volumetric=0.0, n_cells=None,
                     n_eval=120, T_init=None):
    """Solve the 1D heat equation. Returns Thermal1DResult.

    source_mode: 'averaged' (continuous absorbed power) or 'pulse' (single fs
    pulse, short window). q_mw_volumetric: extra uniform volumetric heating
    [W/m^3] (microwave/background surrogate)."""
    cfg.validate()
    geo, mat, fr, sol = cfg.geometry, cfg.material, cfg.fridge, cfg.solver
    n = int(n_cells or sol.n_cells_1d)
    grid = Grid1D(geo.thermal_depth_m, n, axis="z")
    dz = grid.dx
    rho = mat.rho_kg_m3
    kkw = mat.k_kwargs()
    laser = LaserSource(cfg.laser, mode=source_mode)

    if t_end is None:
        t_end = sol.pulse_window_s if source_mode == "pulse" else sol.recovery_window_s

    if T_init is None:
        T0 = np.full(n, fr.T_fridge_K)
        if source_mode == "averaged":
            # warm start near front to reduce stiffness transient (still physical:
            # the averaged source has been on; we let the solver relax).
            T0 = np.full(n, fr.T_fridge_K)
    else:
        T0 = np.asarray(T_init, dtype=float).copy()

    alpha_K = fr.kapitza_coeff_W_m2_K4
    Tf = fr.T_fridge_K
    q_bg = fr.background_flux_W_m3

    def rhs(t, T):
        T = np.clip(T, 1e-6, None)
        k_cells = diamond_k(T, **kkw)
        cp = diamond_cp(T)
        cap = rho * cp                      # [J/m^3/K]
        # interior conservative diffusion
        kf = harmonic_face_k(k_cells)
        flux = kf * (T[1:] - T[:-1]) / dz   # face fluxes [W/m^2], length n-1
        div = np.zeros_like(T)
        div[1:-1] = (flux[1:] - flux[:-1]) / dz
        # source terms
        s = _front_position(cfg, t)
        Ql = laser.q_volumetric_1d(grid.centers, t, front_position_m=s)
        Q = Ql + q_mw_volumetric + q_bg
        dTdt = np.zeros_like(T)
        dTdt[1:-1] = (div[1:-1] + Q[1:-1]) / cap[1:-1]
        # front-side boundary (z=0): insulated (no heat escapes the front face in
        # this reduced model; the laser deposits volumetrically). One-sided flux.
        flux0 = kf[0] * (T[1] - T[0]) / dz
        dTdt[0] = (flux0 / dz + Q[0]) / cap[0]
        # backside Kapitza sink (z=L): -k dT/dz|L = alpha_K (T[-1]^4 - Tf^4)
        flux_in = kf[-1] * (T[-2] - T[-1]) / dz        # conduction into last cell [W/m^2]
        q_sink = alpha_K * (T[-1] ** 4 - Tf ** 4)      # leaves through backside [W/m^2]
        dTdt[-1] = (flux_in / dz - q_sink / dz + Q[-1]) / cap[-1]
        return dTdt

    t_eval = np.linspace(0.0, t_end, n_eval)
    sol_obj = solve_ivp(rhs, (0.0, t_end), T0, method=sol.method,
                        t_eval=t_eval, rtol=sol.rtol, atol=sol.atol, max_step=t_end / 20.0)
    T = sol_obj.y
    assert_finite(T, "thermal_1d.T")
    return Thermal1DResult(grid, sol_obj.t, T, cfg, source_mode,
                           "ok" if sol_obj.success else "failed", sol_obj.message)
