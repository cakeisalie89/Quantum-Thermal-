"""Thermal 2D axisymmetric non-lumped solver: T(r,z,t).

  rho Cp(T) dT/dt = (1/r) d/dr[r k(T) dT/dr] + d/dz[k(T) dT/dz] + Q(r,z,t)

Boundaries:
  r=0   : symmetry (no radial flux; enforced by 2*pi*r face weighting, area->0)
  r=R   : insulated by default (configurable)
  z=0   : insulated (volumetric laser deposition; source side)
  z=L   : Kapitza sink, q = alpha_K (T^4 - T_fridge^4) per annulus

Finite-volume divergence, method-of-lines, solve_ivp BDF with a precomputed
5-point Jacobian sparsity pattern (keeps BDF tractable on ~3000 unknowns).

MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import numpy as np
import scipy.sparse as sp
from scipy.integrate import solve_ivp

from .grids import AxisymmetricGrid2D
from .fields import Field2D
from .config import MultiphysicsConfig
from .material_models import diamond_cp, diamond_k
from .numerics import assert_finite
from .laser_source import LaserSource


class Thermal2DResult:
    def __init__(self, grid, t, T_final, T_peak_field, cfg, solver_status, message):
        self.grid = grid
        self.t = t
        self.T_final = T_final          # Field2D at t_end
        self.T_peak = T_peak_field      # Field2D of per-cell max over time
        self.cfg = cfg
        self.solver_status = solver_status
        self.message = message

    def nv_layer_mean_K(self):
        j = int(np.argmin(np.abs(self.grid.z_centers - self.cfg.geometry.nv_layer_depth_m)))
        # area-weighted mean over the NV-depth annular ring
        w = self.grid.cell_volume[:, j]
        return float(np.sum(self.T_peak.values[:, j] * w) / np.sum(w))

    def nv_layer_max_K(self):
        j = int(np.argmin(np.abs(self.grid.z_centers - self.cfg.geometry.nv_layer_depth_m)))
        return float(self.T_peak.values[:, j].max())

    def hotspot_rz(self):
        return self.T_peak.argmax_rz()

    def max_T_K(self):
        return self.T_peak.max()

    def max_radial_gradient_K_per_m(self):
        return self.T_peak.max_abs_radial_gradient()

    def max_depth_gradient_K_per_m(self):
        return self.T_peak.max_abs_depth_gradient()


def _jac_sparsity(nr, nz):
    N = nr * nz
    rows, cols = [], []
    def idx(i, j): return i * nz + j
    for i in range(nr):
        for j in range(nz):
            p = idx(i, j)
            rows.append(p); cols.append(p)
            for (di, dj) in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ii, jj = i + di, j + dj
                if 0 <= ii < nr and 0 <= jj < nz:
                    rows.append(p); cols.append(idx(ii, jj))
    return sp.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(N, N))


def solve_thermal_2d(cfg: MultiphysicsConfig, source_mode="averaged", t_end=None,
                     q_mw_volumetric=0.0, n_r=None, n_z=None, n_eval=40,
                     disable_radial=False, spot_radius_override=None, T_init=None):
    """Solve the 2D axisymmetric heat equation. Returns Thermal2DResult.

    disable_radial / spot_radius_override are verification hooks to demonstrate
    that the 2D solver reduces to the 1D result when radial gradients vanish."""
    cfg.validate()
    geo, mat, fr, sol = cfg.geometry, cfg.material, cfg.fridge, cfg.solver
    nr = int(n_r or sol.n_r_2d)
    nz = int(n_z or sol.n_z_2d)
    grid = AxisymmetricGrid2D(geo.thermal_radius_m, geo.thermal_depth_m, nr, nz)
    dr, dz = grid.dr, grid.dz
    rho = mat.rho_kg_m3
    kkw = mat.k_kwargs()

    lcfg = cfg.laser
    if spot_radius_override is not None:
        import dataclasses
        lcfg = dataclasses.replace(lcfg, spot_radius_m=spot_radius_override)
    laser = LaserSource(lcfg, mode=source_mode)

    if t_end is None:
        t_end = sol.pulse_window_s if source_mode == "pulse" else sol.recovery_window_s

    Tf = fr.T_fridge_K
    alpha_K = fr.kapitza_coeff_W_m2_K4
    q_bg = fr.background_flux_W_m3

    rc = grid.r_centers
    rf = grid.r_faces
    shape = (nr, nz)

    if T_init is None:
        T0 = np.full(shape, Tf)
    else:
        T0 = np.asarray(T_init, dtype=float).reshape(shape).copy()

    def rhs(t, Tflat):
        T = np.clip(Tflat.reshape(shape), 1e-6, None)
        k = diamond_k(T, **kkw)
        cap = rho * diamond_cp(T)
        dTdt = np.zeros_like(T)

        # ---- radial divergence (1/r) d/dr(r k dT/dr) ----
        if not disable_radial:
            # face k by harmonic mean between adjacent radial cells
            kf_r = 2.0 * k[:-1, :] * k[1:, :] / (k[:-1, :] + k[1:, :] + 1e-300)  # (nr-1, nz)
            dTdr = (T[1:, :] - T[:-1, :]) / dr
            Fr = kf_r * dTdr                       # radial face flux (nr-1, nz) [W/m^2]
            rface_in = rf[1:nr][:, None]           # radii of interior faces (nr-1,1)
            rF = rface_in * Fr                     # r * F at interior faces
            # divergence: (r_{i+1/2}F_{i+1/2} - r_{i-1/2}F_{i-1/2}) / (r_i dr)
            radial_div = np.zeros_like(T)
            radial_div[1:-1, :] = (rF[1:, :] - rF[:-1, :]) / (rc[1:-1][:, None] * dr)
            # axis cell i=0: inner face area ~0; only outer face contributes
            radial_div[0, :] = (rF[0, :]) / (rc[0] * dr)
            # outer cell i=nr-1: cold contact to surrounding bulk (Dirichlet T_fridge)
            # implemented as a sink flux through the outer face (area ~ r_face[nr]).
            k_out = k[-1, :]
            F_outer = k_out * (Tf - T[-1, :]) / (0.5 * dr)     # [W/m^2] into cell if T<Tf
            r_out = rf[nr]
            radial_div[-1, :] = (r_out * F_outer - rF[-1, :]) / (rc[-1] * dr)
        else:
            radial_div = np.zeros_like(T)

        # ---- axial divergence d/dz(k dT/dz) ----
        kf_z = 2.0 * k[:, :-1] * k[:, 1:] / (k[:, :-1] + k[:, 1:] + 1e-300)  # (nr, nz-1)
        Fz = kf_z * (T[:, 1:] - T[:, :-1]) / dz                              # (nr, nz-1) [W/m^2]
        axial_div = np.zeros_like(T)
        axial_div[:, 1:-1] = (Fz[:, 1:] - Fz[:, :-1]) / dz
        # z=0 source side: insulated -> only +z face
        axial_div[:, 0] = (Fz[:, 0]) / dz
        # z=L sink side: conduction in from -z face minus Kapitza sink
        q_sink = alpha_K * (T[:, -1] ** 4 - Tf ** 4)                         # [W/m^2]
        axial_div[:, -1] = (-Fz[:, -1] - q_sink) / dz

        # ---- source ----
        Q = laser.q_volumetric_2d(grid.R, grid.Z, t) + q_mw_volumetric + q_bg

        dTdt = (radial_div + axial_div + Q) / cap
        return dTdt.reshape(-1)

    t_eval = np.linspace(0.0, t_end, n_eval)
    jac_sp = _jac_sparsity(nr, nz)
    sol_obj = solve_ivp(rhs, (0.0, t_end), T0.reshape(-1), method=sol.method,
                        t_eval=t_eval, rtol=max(sol.rtol, 1e-5), atol=max(sol.atol, 1e-9),
                        jac_sparsity=jac_sp, max_step=t_end / 10.0)
    Y = sol_obj.y  # (N, Nt)
    assert_finite(Y, "thermal_2d.T")
    T_final = Y[:, -1].reshape(shape)
    T_peak = Y.max(axis=1).reshape(shape)
    return Thermal2DResult(
        grid, sol_obj.t,
        Field2D(grid, T_final, name="T", unit="K"),
        Field2D(grid, T_peak, name="Tpeak", unit="K"),
        cfg, "ok" if sol_obj.success else "failed", sol_obj.message)
