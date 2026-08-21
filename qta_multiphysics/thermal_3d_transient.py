"""Transient 3D heat conduction on a structured graded finite-volume mesh.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

    rho * Cp(T) dT/dt = div( k(T) grad T ) + Q_laser(x, y, z, t)

Finite-volume method-of-lines on ``StructuredGrid3D`` (harmonic/series face
conductances on the nonuniform mesh, exactly the 1D pattern), integrated with
the repository's stiff BDF ``solve_ivp`` configuration (same rtol/atol/method
from ``SolverConfig``) and a 7-point Jacobian sparsity pattern so the stiff
integrator stays CPU-practical at reduced CI resolution. Boundaries: insulated
front, adiabatic lateral, Kapitza-radiative back (``boundaries_3d``). Material
model, laser parameters, and fridge parameters are the SAME ASSUMED /
DESIGN_SPECIFIED values used by the canonical 1D/2D backends; the 3D layer is
an additive numerical validation layer, not a hardware validation and not a
replacement for the 1D (canonical) or 2D axisymmetric backends.

Deterministic: no randomness anywhere in this module.
"""
from __future__ import annotations

import numpy as np
import scipy.sparse as sp
from scipy.integrate import solve_ivp

from .config import MultiphysicsConfig
from .material_models import diamond_cp, diamond_k, internal_energy_density
from .numerics import face_series_resistance, assert_finite
from .mesh_3d import StructuredGrid3D, Grid3DConfig
from .laser_source_3d import LaserSource3D
from .boundaries_3d import BoundarySpec3D, kapitza_sink_flux_W_m2

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


class Thermal3DResult:
    def __init__(self, grid: StructuredGrid3D, t, T_flat, cfg, source_mode,
                 transverse, solver_status, message, T_init, energy, laser):
        self.grid = grid
        self.t = np.asarray(t, dtype=float)
        self.T = np.asarray(T_flat, dtype=float)          # (n_cells, nt)
        self.cfg = cfg
        self.source_mode = source_mode
        self.transverse = transverse
        self.solver_status = solver_status
        self.message = message
        self.T_init = np.asarray(T_init, dtype=float)
        self.energy = energy or {}
        self.laser = laser
        g = grid
        self._shape = (g.nx, g.ny, g.nz)
        ix, iy, iz = g.index_nearest(laser.beam_center_xy[0],
                                     laser.beam_center_xy[1],
                                     cfg.geometry.nv_layer_depth_m)
        self.probe_index = (ix, iy, iz)

    # ---- field access ----
    def T_xyz(self, k_time: int = -1):
        return self.T[:, k_time].reshape(self._shape)

    def probe_timeseries_K(self):
        flat = np.ravel_multi_index(self.probe_index, self._shape)
        return self.T[flat, :]

    def nv_layer_temperature_K(self):
        return float(self.probe_timeseries_K()[-1])

    def peak_temperature_K(self):
        return float(self.T.max())

    def energy_residual(self):
        return float(self.energy.get("rel_residual", float("nan")))

    # ---- output rows (consumed by exports.write_rows_csv) ----
    def probe_timeseries_rows(self):
        g = self.grid
        ix, iy, iz = self.probe_index
        ts = self.probe_timeseries_K()
        surf = self.T.reshape(self._shape + (-1,))[:, :, 0, :]
        return [{"t_s": f"{tk:.9e}",
                 "T_nv_probe_K": f"{ts[k]:.9e}",
                 "T_front_surface_max_K": f"{surf[:, :, k].max():.9e}",
                 "probe_x_m": f"{g.xc[ix]:.6e}", "probe_y_m": f"{g.yc[iy]:.6e}",
                 "probe_z_m": f"{g.zc[iz]:.6e}", "label": LABEL}
                for k, tk in enumerate(self.t)]

    def hotspot_rows(self, top_n: int = 10):
        g = self.grid
        Tpk = self.T.max(axis=1)                       # (n_cells,) peak over time
        kpk = self.T.argmax(axis=1)
        order = np.argsort(Tpk)[::-1][:top_n]
        rows = []
        for rank, flat in enumerate(order, start=1):
            ix, iy, iz = np.unravel_index(flat, self._shape)
            rows.append({"rank": rank,
                         "x_m": f"{g.xc[ix]:.6e}", "y_m": f"{g.yc[iy]:.6e}",
                         "z_m": f"{g.zc[iz]:.6e}",
                         "T_peak_K": f"{Tpk[flat]:.9e}",
                         "t_at_peak_s": f"{self.t[kpk[flat]]:.9e}",
                         "T_final_K": f"{self.T[flat, -1]:.9e}",
                         "label": LABEL})
        return rows


def jacobian_sparsity_7pt(nx: int, ny: int, nz: int) -> sp.csr_matrix:
    """7-point (self + 6 face neighbours) sparsity pattern for BDF."""
    def tri(n):
        return sp.diags([np.ones(n - 1), np.ones(n), np.ones(n - 1)],
                        [-1, 0, 1], format="csr")
    Ix, Iy, Iz = (sp.identity(nx, format="csr"), sp.identity(ny, format="csr"),
                  sp.identity(nz, format="csr"))
    S = (sp.kron(sp.kron(tri(nx), Iy), Iz)
         + sp.kron(sp.kron(Ix, tri(ny)), Iz)
         + sp.kron(sp.kron(Ix, Iy), tri(nz)))
    S.data[:] = 1.0
    return S.tocsr()


def solve_thermal_3d(cfg: MultiphysicsConfig, g3: Grid3DConfig | None = None,
                     source_mode: str = "averaged", transverse: str = "gaussian",
                     t_end: float | None = None, n_eval: int = 13,
                     T_init=None, beam_center_xy=(0.0, 0.0),
                     source_scale: float = 1.0,
                     extra_volumetric_W=None,
                     extra_front_flux_W_m2: float = 0.0,
                     max_step_divisor: float = 20.0) -> Thermal3DResult:
    """Solve the reduced 3D transient heat equation (deterministic, CPU-only).

    ``source_scale=0.0`` gives the zero-source stability case. ``transverse``
    'uniform_xy' exists solely for the like-for-like 3D->1D reduction check.

    Bounded auxiliary channels (coupled layer; defaults leave the solve
    bit-identical to the laser-only path):
    ``extra_volumetric_W`` -- optional per-cell power array [W], (nx,ny,nz)
    (e.g. the REDUCED_ORDER microwave NV-region map). Constant in time.
    ``extra_front_flux_W_m2`` -- optional uniform inbound front-face flux
    [W/m^2] (e.g. the REDUCED_ORDER stage-chain radiative load). Both enter
    the energy accounting so closure covers every implemented source.
    """
    cfg.validate()
    grid = StructuredGrid3D(cfg, g3)
    bc = BoundarySpec3D().validate()
    assert bc.back == "kapitza_radiative"
    mat, fr, sol = cfg.material, cfg.fridge, cfg.solver
    rho = mat.rho_kg_m3
    kkw = mat.k_kwargs()
    t_end = float(t_end if t_end is not None else sol.pulse_window_s)

    laser = LaserSource3D(cfg.laser, mode=source_mode, transverse=transverse,
                          beam_center_xy=beam_center_xy,
                          box_area_m2=(2 * grid.half_extent_xy_m) ** 2)
    front = cfg.geometry.front_position_m
    # exactly-conservative discrete deposition: per-cell POWER fractions from
    # analytic integrals over every finite-volume cell (erf across x/y faces,
    # Beer-Lambert exponential differences across z faces). sum(frac) equals
    # the analytic captured fraction to machine precision, so the discrete
    # source integral and the energy accounting are conservation-exact.
    frac = source_scale * laser.cell_fractions(grid, front_position_m=front)

    # bounded auxiliary channels (coupled layer): validated, constant in time
    _xv = None
    if extra_volumetric_W is not None:
        _xv = np.asarray(extra_volumetric_W, dtype=float)
        if _xv.shape != (grid.nx, grid.ny, grid.nz):
            raise ValueError("extra_volumetric_W must have shape (nx, ny, nz)")
        assert_finite(_xv, "thermal_3d.extra_volumetric_W")
    _xf = float(extra_front_flux_W_m2)
    if _xf < 0.0:
        raise ValueError("extra_front_flux_W_m2 must be >= 0")

    def P_deposited_W(t):
        P = frac * float(np.asarray(laser.base.temporal_power_W(t)))
        if _xv is not None:
            P = P + _xv
        return P

    shape = (grid.nx, grid.ny, grid.nz)
    n = grid.n_cells
    if T_init is None:
        T0 = np.full(n, fr.T_fridge_K)
    else:
        T0 = np.asarray(T_init, dtype=float).reshape(n).copy()

    Ax = grid.area_x[None, :, :]     # (1, ny, nz) -> x-normal faces
    Ay = grid.area_y[:, None, :]     # (nx, 1, nz) -> y-normal faces
    Az = grid.area_z                  # (nx, ny)    -> z-normal faces
    V = grid.volumes
    dRx = grid.dRx[:, None, None]; dLx = grid.dLx[:, None, None]
    dRy = grid.dRy[None, :, None]; dLy = grid.dLy[None, :, None]
    dRz = grid.dRz[None, None, :]; dLz = grid.dLz[None, None, :]

    def rhs(t, Tf):
        T = np.clip(Tf, 1e-6, None).reshape(shape)
        k = diamond_k(T, **kkw)
        cap = rho * diamond_cp(T)
        net = np.zeros(shape)
        # x faces (flux from i -> i+1, per unit area, positive when T_i > T_{i+1})
        Fx = (T[:-1, :, :] - T[1:, :, :]) / face_series_resistance(
            k[:-1, :, :], k[1:, :, :], dRx, dLx)
        Px = Fx * Ax
        net[:-1, :, :] -= Px
        net[1:, :, :] += Px
        # y faces
        Fy = (T[:, :-1, :] - T[:, 1:, :]) / face_series_resistance(
            k[:, :-1, :], k[:, 1:, :], dRy, dLy)
        Py = Fy * Ay
        net[:, :-1, :] -= Py
        net[:, 1:, :] += Py
        # z faces (front z=0 insulated; lateral zero-flux by omission)
        Fz = (T[:, :, :-1] - T[:, :, 1:]) / face_series_resistance(
            k[:, :, :-1], k[:, :, 1:], dRz, dLz)
        Pz = Fz * Az[:, :, None]
        net[:, :, :-1] -= Pz
        net[:, :, 1:] += Pz
        # back face Kapitza-radiative sink
        q_sink = kapitza_sink_flux_W_m2(T[:, :, -1], fr)
        net[:, :, -1] -= q_sink * Az
        # optional bounded inbound front-face flux (coupled layer)
        if _xf > 0.0:
            net[:, :, 0] += _xf * Az
        # volumetric source (exactly-conservative per-cell deposition [W])
        net += P_deposited_W(t)
        return (net / (cap * V)).reshape(n)

    t_eval = np.linspace(0.0, t_end, int(n_eval))
    sol_obj = solve_ivp(rhs, (0.0, t_end), T0, method=sol.method, t_eval=t_eval,
                        rtol=sol.rtol, atol=sol.atol,
                        max_step=t_end / float(max_step_divisor),
                        jac_sparsity=jacobian_sparsity_7pt(*shape),
                        dense_output=True)
    T = sol_obj.y
    assert_finite(T, "thermal_3d.T")

    # ---- energy accounting (DERIVED numerical check, MODEL-ONLY) [J] ----
    # Quadrature on a dense internal grid from the solver's continuous
    # interpolant. A composite geometric+linear grid resolves the fast early
    # transient (microsecond-scale in both the heating and the recovery/decay
    # phases) so closure is independent of the sparse n_eval sampling.
    tt = sol_obj.t
    _tg = np.geomspace(max(t_end * 1e-7, 1e-12), t_end, 161)
    tq = np.unique(np.concatenate(([0.0], _tg, np.linspace(0.0, t_end, 81))))
    Tq = sol_obj.sol(tq)
    P_dep = np.array([float(np.sum(P_deposited_W(tk))) for tk in tq])
    Tback_q = Tq.reshape(shape + (-1,))[:, :, -1, :]
    q_sink_t = np.array([float(np.sum(kapitza_sink_flux_W_m2(Tback_q[:, :, k], fr) * Az))
                         for k in range(tq.size)])
    E_src = float(np.trapezoid(P_dep, tq))
    _P_front = _xf * float(np.sum(Az))
    E_front = _P_front * t_end
    E_src_total = E_src + E_front
    E_sink = float(np.trapezoid(q_sink_t, tq))
    u_final = internal_energy_density(T[:, -1].reshape(shape), rho=rho)
    u_init = internal_energy_density(T0.reshape(shape), rho=rho)
    dU = float(np.sum((u_final - u_init) * V))
    residual = E_src_total - E_sink - dU
    denom = max(abs(E_src_total), abs(dU), abs(E_sink), 1e-30)
    energy = {
        "integrated_source_energy_J": E_src_total,
        "laser_and_volumetric_channels_J": E_src,
        "front_flux_channel_J": E_front,
        "boundary_sink_energy_J": E_sink,
        "internal_energy_change_J": dU,
        "residual_J": residual,
        "rel_residual": residual / denom,
        "label": LABEL,
    }
    return Thermal3DResult(grid, tt, T, cfg, source_mode, transverse,
                           "ok" if sol_obj.success else "failed",
                           sol_obj.message, T0, energy, laser)
