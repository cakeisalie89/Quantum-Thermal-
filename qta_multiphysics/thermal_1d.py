"""Thermal 1D non-lumped solver: conservative finite-volume heat diffusion on a
genuinely meshed (graded, nonuniform) Grid1D.

  rho Cp(T) dT/dt = d/dz[ k(T) dT/dz ] + Q_laser(z,t) + Q_mw(z,t) + Q_bg

Discretisation (cell-centred finite volume on nonuniform cells):
  * Each cell i has width w_i and a control volume w_i (per unit cross-section).
  * Interior face flux uses the distance-weighted harmonic (series) face
    resistance R_f = d_right_i/k_i + d_left_{i+1}/k_{i+1}, so the scheme is
    exactly conservative and physically correct on a nonuniform mesh and on a
    temperature-dependent k(T):  F_f = (T_i - T_{i+1}) / R_f   [W/m^2].
  * Heat capacity uses the temperature-dependent Cp(T): cap = rho Cp(T).
  * Sources are evaluated as mesh fields: a depth-resolved Beer-Lambert laser
    field, an (optionally depth-resolved) microwave field, and a background
    volumetric term.

Boundaries:
  z = 0 (surface / process-front side): insulated (the laser deposits
        volumetrically; no conductive flux leaves the front face).
  z = L (cold thermal-contact side): Kapitza-radiative sink,
        q_sink = alpha_K (T_L^4 - T_fridge^4)  [W/m^2].

Moving front: s(t) = s0 + v_front t (structured for a later Stefan condition;
first pass uses v_front = 0).

Method-of-lines + solve_ivp (stiff BDF). Energy accounting (integrated source,
internal-energy change, boundary loss, residual) is computed from the actual
solution arrays as a DERIVED numerical self-consistency check.

SCOPE / WHAT THIS IS NOT. This solver models the laser *thermal loading* into
the process zone (Beer-Lambert volumetric HEAT [W/m^3]) and reports a
temperature field in KELVIN. Reported temperatures (e.g. NV_layer_T_1d_K) are
thermal-hotspot temperatures / rises, NOT a deposition or growth rate. It does
NOT model methane dissociation, sticking, carbon incorporation, yield per
pulse, or growth velocity, and does NOT validate any C13 deposition rate;
deposition yield remains UNKNOWN / BLOCKED pending measurement or a defensible
LCVD surface-chemistry model.

MODEL-ONLY / FORECAST-ONLY. No measured data; not validated against hardware.
"""
from __future__ import annotations
import numpy as np
import scipy.sparse as sp
from scipy.integrate import solve_ivp

from .grids import Grid1D, thermal_depth_refinement
from .fields import Field1D
from .config import MultiphysicsConfig
from .material_models import diamond_cp, diamond_k, internal_energy_density
from .numerics import face_series_resistance, assert_finite
from .laser_source import LaserSource


class Thermal1DResult:
    def __init__(self, grid, t, T, cfg, source_mode, solver_status, message,
                 T_init=None, q_mw_field=None, q_bg=0.0, laser=None,
                 front_position_m=0.0, energy=None):
        self.grid = grid
        self.t = t                  # (Nt,)
        self.T = T                  # (n, Nt)
        self.cfg = cfg
        self.source_mode = source_mode
        self.solver_status = solver_status
        self.message = message
        self._T_init = (np.asarray(T_init, dtype=float).copy()
                        if T_init is not None else T[:, 0].copy())
        self._q_mw_field = (np.asarray(q_mw_field, dtype=float)
                            if q_mw_field is not None else np.zeros(grid.n))
        self._q_bg = float(q_bg)
        self._laser = laser
        self._front = float(front_position_m)
        self.energy = energy or {}

    # ---- index helpers ----
    def _nv_index(self):
        zc = self.grid.centers
        return int(np.argmin(np.abs(zc - self.cfg.geometry.nv_layer_depth_m)))

    # ---- canonical derived metrics (interfaces preserved) ----
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

    # ---- mesh / field outputs (generated from the actual computed arrays) ----
    @property
    def z_faces(self):
        return self.grid.faces

    @property
    def z_centers(self):
        return self.grid.centers

    @property
    def cell_widths(self):
        return self.grid.cell_widths

    @property
    def T_initial(self):
        return self._T_init

    @property
    def T_final(self):
        return self.T[:, -1]

    @property
    def T_peak(self):
        """Per-cell maximum temperature over the time window."""
        return self.T.max(axis=1)

    @property
    def Q_mw(self):
        return self._q_mw_field

    @property
    def Q_bg(self):
        return np.full(self.grid.n, self._q_bg)

    def Q_laser(self, t=None):
        """Laser volumetric heat field [W/m^3] at time t (default: time of peak
        deposition for the run's source mode)."""
        if self._laser is None:
            return np.zeros(self.grid.n)
        if t is None:
            t = (self._laser.pulse_center_s if self.source_mode == "pulse"
                 else float(self.t[-1]))
        s = self._front + self.cfg.geometry.front_velocity_m_s * t
        return self._laser.q_volumetric_1d(self.grid.centers, t, front_position_m=s)

    def gradient(self):
        """dT/dz of the final profile [K/m]."""
        return np.gradient(self.T[:, -1], self.grid.centers)

    def heat_flux(self):
        """Conductive heat flux q = -k(T) dT/dz of the final profile [W/m^2]."""
        Tf = self.T[:, -1]
        k = diamond_k(Tf, **self.cfg.material.k_kwargs())
        return -k * np.gradient(Tf, self.grid.centers)

    def nv_layer_samples(self):
        """Sampled NV-layer values: index, depth, peak/final/initial T, and the
        NV temperature time series."""
        i = self._nv_index()
        return {
            "nv_index": i,
            "nv_depth_m": float(self.grid.centers[i]),
            "nv_T_initial_K": float(self._T_init[i]),
            "nv_T_peak_K": float(self.T[i, :].max()),
            "nv_T_final_K": float(self.T[i, -1]),
            "nv_T_series_K": self.T[i, :],
        }

    def energy_residual(self):
        """Relative energy-balance residual of the finite-volume solution."""
        return float(self.energy.get("rel_residual", float("nan")))


def _front_position(cfg, t):
    g = cfg.geometry
    return g.front_position_m + g.front_velocity_m_s * t


def _as_mw_field(q_mw_volumetric, z_centers, t0):
    """Coerce the microwave-heating argument into a mesh field (n,) [W/m^3].

    Accepts a scalar (uniform field), an array of length n, or a callable
    q_mw(z, t). This realises the Q_mw(z,t) term as an actual mesh field while
    preserving the historical scalar interface (default 0.0)."""
    n = len(z_centers)
    if callable(q_mw_volumetric):
        return np.asarray(q_mw_volumetric(z_centers, t0), dtype=float) * np.ones(n)
    arr = np.asarray(q_mw_volumetric, dtype=float)
    if arr.ndim == 0:
        return np.full(n, float(arr))
    if arr.shape != (n,):
        raise ValueError(f"q_mw_volumetric array shape {arr.shape} != ({n},)")
    return arr.copy()


def solve_thermal_1d(cfg: MultiphysicsConfig, source_mode="averaged",
                     t_end=None, q_mw_volumetric=0.0, n_cells=None,
                     n_eval=120, T_init=None, max_ratio=6.0):
    """Solve the 1D heat equation by conservative finite volume on a graded
    mesh. Returns Thermal1DResult.

    source_mode: 'averaged' (continuous absorbed power) or 'pulse' (single fs
    pulse, short window). q_mw_volumetric: extra volumetric heating
    [W/m^3] -- scalar, array(n,), or callable q_mw(z,t) (microwave/background
    surrogate). The mesh is refined near the surface, the absorption depth, the
    NV layer, the process front, and the cold contact."""
    cfg.validate()
    geo, mat, fr, sol = cfg.geometry, cfg.material, cfg.fridge, cfg.solver
    n = int(n_cells or sol.n_cells_1d)
    laser = LaserSource(cfg.laser, mode=source_mode)

    # Graded, genuinely nonuniform finite-volume mesh.
    refine = thermal_depth_refinement(geo.thermal_depth_m, laser.absorption_depth_m(),
                                      geo.nv_layer_depth_m, geo.front_position_m)
    grid = Grid1D.graded(geo.thermal_depth_m, n, refine_at=refine, axis="z", max_ratio=max_ratio)

    w = grid.cell_widths                 # (n,)  control volumes (unit area)
    dR = grid.dist_to_right_face         # (n,)  centre -> right face
    dL = grid.dist_to_left_face          # (n,)  centre -> left face
    rho = mat.rho_kg_m3
    kkw = mat.k_kwargs()

    if t_end is None:
        t_end = sol.pulse_window_s if source_mode == "pulse" else sol.recovery_window_s

    if T_init is None:
        T0 = np.full(n, fr.T_fridge_K)
    else:
        T0 = np.asarray(T_init, dtype=float).copy()
        if T0.shape != (n,):
            raise ValueError(f"T_init shape {T0.shape} != ({n},)")

    alpha_K = fr.kapitza_coeff_W_m2_K4
    Tf = fr.T_fridge_K
    q_bg = fr.background_flux_W_m3
    q_mw_field = _as_mw_field(q_mw_volumetric, grid.centers, 0.0)

    def source_field(t):
        s = _front_position(cfg, t)
        Ql = laser.q_volumetric_1d(grid.centers, t, front_position_m=s)
        return Ql + q_mw_field + q_bg, Ql

    def rhs(t, T):
        T = np.clip(T, 1e-6, None)
        k = diamond_k(T, **kkw)              # (n,)  k(T)
        cap = rho * diamond_cp(T)            # (n,)  rho Cp(T)
        # interior face fluxes, +z direction (cell j -> j+1), conservative FV
        Rf = face_series_resistance(k[:-1], k[1:], dR[:-1], dL[1:])  # (n-1,)
        F = (T[:-1] - T[1:]) / Rf            # (n-1,) [W/m^2]
        Q, _ = source_field(t)
        # net conductive power into each cell (per unit area)
        net = np.zeros_like(T)
        net[1:-1] = F[:-1] - F[1:]
        net[0] = -F[0]                       # z=0 insulated: no left-face flux
        q_sink = alpha_K * (T[-1] ** 4 - Tf ** 4)   # Kapitza sink at z=L [W/m^2]
        net[-1] = F[-1] - q_sink
        return (net + Q * w) / (cap * w)

    t_eval = np.linspace(0.0, t_end, n_eval)
    # The 1D finite-volume operator is tridiagonal (each cell couples only to its
    # two face-neighbours), so supply that Jacobian sparsity pattern: BDF then
    # builds the Jacobian by banded finite differences instead of a dense one,
    # which keeps the refined (stiffer) mesh efficient. Deterministic.
    jac_sp = sp.diags([np.ones(n - 1), np.ones(n), np.ones(n - 1)], [-1, 0, 1],
                      format="csr")
    sol_obj = solve_ivp(rhs, (0.0, t_end), T0, method=sol.method, t_eval=t_eval,
                        rtol=sol.rtol, atol=sol.atol, max_step=t_end / 20.0,
                        jac_sparsity=jac_sp)
    T = sol_obj.y
    assert_finite(T, "thermal_1d.T")

    # ---- energy accounting (DERIVED numerical check, MODEL-ONLY) ----
    tt = sol_obj.t
    P_dep = np.empty_like(tt)                # total deposited power per unit area [W/m^2]
    for kk, tk in enumerate(tt):
        Qk, _ = source_field(tk)
        P_dep[kk] = float(np.sum(Qk * w))
    q_sink_t = alpha_K * (T[-1, :] ** 4 - Tf ** 4)      # [W/m^2] over time
    E_src = float(np.trapezoid(P_dep, tt)) if tt.size > 1 else 0.0
    E_sink = float(np.trapezoid(q_sink_t, tt)) if tt.size > 1 else 0.0
    u_final = internal_energy_density(T[:, -1], rho=rho)
    u_init = internal_energy_density(T0, rho=rho)
    dU = float(np.sum((u_final - u_init) * w))
    residual = E_src - E_sink - dU
    denom = max(abs(E_src), abs(dU), 1e-30)
    energy = {
        "integrated_source_energy_J_m2": E_src,
        "boundary_sink_energy_J_m2": E_sink,
        "boundary_low_energy_J_m2": 0.0,     # z=0 insulated
        "internal_energy_change_J_m2": dU,
        "residual_J_m2": residual,
        "rel_residual": residual / denom,
    }

    return Thermal1DResult(grid, sol_obj.t, T, cfg, source_mode,
                           "ok" if sol_obj.success else "failed", sol_obj.message,
                           T_init=T0, q_mw_field=q_mw_field, q_bg=q_bg, laser=laser,
                           front_position_m=geo.front_position_m, energy=energy)
