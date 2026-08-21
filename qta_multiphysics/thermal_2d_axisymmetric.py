"""Thermal 2D axisymmetric non-lumped solver: conservative finite-volume
T(r,z,t) on a genuinely meshed (graded, nonuniform) AxisymmetricGrid2D.

  rho Cp(T) dT/dt = (1/r) d/dr[ r k(T) dT/dr ] + d/dz[ k(T) dT/dz ] + Q(r,z,t)

Cell-centred cylindrical finite volume with EXACT geometry from the face radii:
  * cell volume V_ij      = pi (r_{i+1}^2 - r_i^2) * dz_j        (annular ring)
  * radial face area      = 2 pi r_face * dz_j                  (lateral)
  * axial  face area      = pi (r_{i+1}^2 - r_i^2)              (annulus)
  * face conductances use the distance-weighted harmonic (series) resistance,
    correct for nonuniform spacing and temperature-dependent k(T).
The inner radial face of the axis cell has area 0 (r_face[0]=0), so the r=0
symmetry boundary is enforced exactly with no special-casing of 1/r.

Boundaries:
  r=0   : symmetry (zero-area axis face -> no radial flux)
  r=R   : cold radial contact to surrounding bulk at T_fridge (half-cell
          resistance through the outer face); zero normal flux instead when
          lateral_adiabatic=True (reduction fixture; interior radial
          conduction is retained either way)
  z=0   : insulated (volumetric laser deposition; surface/source side)
  z=L   : Kapitza-radiative sink, q = alpha_K (T^4 - T_fridge^4) per annulus

Method-of-lines, solve_ivp BDF with a precomputed 5-point Jacobian sparsity
pattern. Energy accounting (integrated source, Kapitza and radial boundary
losses, internal-energy change, residual) is computed from the solution arrays
as a DERIVED numerical check.

SCOPE / WHAT THIS IS NOT. This solver models the *laser thermal loading* into
the C13-methane / LCVD process zone as a transient Gaussian (radial) x
Beer-Lambert (depth) volumetric HEAT field [W/m^3], and reports the resulting
temperature field T(r,z,t) in KELVIN. Outputs such as the NV-layer temperature
(e.g. NV_layer_mean_2d_K ~ a few K) are thermal-hotspot temperatures / rises
above the fridge base, NOT a deposition or growth rate. This solver does NOT
model methane dissociation, surface sticking, carbon incorporation, yield per
pulse, or growth velocity, and it does NOT validate any C13 deposition rate.
Deposition yield remains UNKNOWN / BLOCKED until measured or supported by a
defensible LCVD surface-chemistry model. The temperature-dependent k(T)/Cp(T)
are reduced ASSUMED models, not measured data for this diamond; convergence and
energy-balance checks are numerical self-consistency, not hardware validation
and not a COMSOL-class multiphysics qualification.

MODEL-ONLY / FORECAST-ONLY. No measured data; not validated against hardware.
"""
from __future__ import annotations
import numpy as np
import scipy.sparse as sp
from scipy.integrate import solve_ivp

from .grids import AxisymmetricGrid2D, thermal_depth_refinement, thermal_radial_refinement
from .fields import Field2D
from .config import MultiphysicsConfig
from .material_models import diamond_cp, diamond_k, internal_energy_density
from .numerics import face_series_resistance, assert_finite
from .laser_source import LaserSource


class Thermal2DResult:
    def __init__(self, grid, t, T_final, T_peak_field, cfg, solver_status, message,
                 T_init=None, laser=None, energy=None, Q_laser_field=None):
        self.grid = grid
        self.t = t
        self.T_final = T_final          # Field2D at t_end
        self.T_peak = T_peak_field      # Field2D of per-cell max over time
        self.cfg = cfg
        self.solver_status = solver_status
        self.message = message
        self._T_init = (np.asarray(T_init, dtype=float).copy()
                        if T_init is not None else None)
        self._laser = laser
        self.energy = energy or {}
        self._Q_laser_field = Q_laser_field

    # ---- canonical derived metrics (interfaces preserved) ----
    def _nv_j(self):
        return int(np.argmin(np.abs(self.grid.z_centers - self.cfg.geometry.nv_layer_depth_m)))

    def nv_layer_mean_K(self):
        j = self._nv_j()
        # exact-volume-weighted mean over the NV-depth annular ring
        w = self.grid.cell_volume[:, j]
        return float(np.sum(self.T_peak.values[:, j] * w) / np.sum(w))

    def nv_layer_max_K(self):
        j = self._nv_j()
        return float(self.T_peak.values[:, j].max())

    def hotspot_rz(self):
        return self.T_peak.argmax_rz()

    def max_T_K(self):
        return self.T_peak.max()

    def max_radial_gradient_K_per_m(self):
        return self.T_peak.max_abs_radial_gradient()

    def max_depth_gradient_K_per_m(self):
        return self.T_peak.max_abs_depth_gradient()

    # ---- mesh / field outputs (generated from the actual computed arrays) ----
    @property
    def r_faces(self):
        return self.grid.r_faces

    @property
    def z_faces(self):
        return self.grid.z_faces

    @property
    def cell_volume(self):
        return self.grid.cell_volume

    @property
    def Q_laser(self):
        return self._Q_laser_field

    def energy_residual(self):
        return float(self.energy.get("rel_residual", float("nan")))


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
                     disable_radial=False, spot_radius_override=None, T_init=None,
                     max_ratio=6.0, lateral_adiabatic=False):
    """Solve the 2D axisymmetric heat equation by conservative finite volume on
    a graded r-z mesh. Returns Thermal2DResult.

    The radial mesh is refined near the beam axis, the beam waist, and the cold
    radial contact; the depth mesh is refined identically to the 1D solver
    (surface, absorption depth, NV layer, front, cold contact).

    Two DISTINCT radial verification hooks exist; they are not interchangeable:

    * ``disable_radial=True`` removes radial transport THROUGHOUT the domain, so
      each column becomes an independent 1D depth problem and the hottest column
      (near r=0) reproduces the 1D NV result. This is a 1D-reduction fixture. It
      is NOT an adiabatic lateral boundary, and using it as one compares a 3D
      solve against a stack of 1D columns.
    * ``lateral_adiabatic=True`` keeps interior radial conduction fully active
      and applies zero normal heat flux at the OUTER radial face only (r=R). The
      r=0 symmetry face is untouched in both cases (its area is exactly zero).
      This is the like-for-like counterpart of the 3D layer's adiabatic lateral
      walls (``boundaries_3d.BoundarySpec3D.lateral``), and is what a 3D->2D
      reduction check must use.

    ``disable_radial`` takes precedence if both are set, since there is then no
    radial flux for a boundary condition to apply to.

    Production default is neither: r=R is a cold radial contact to surrounding
    bulk at T_fridge."""
    cfg.validate()
    geo, mat, fr, sol = cfg.geometry, cfg.material, cfg.fridge, cfg.solver
    nr = int(n_r or sol.n_r_2d)
    nz = int(n_z or sol.n_z_2d)

    lcfg = cfg.laser
    if spot_radius_override is not None:
        import dataclasses
        lcfg = dataclasses.replace(lcfg, spot_radius_m=spot_radius_override)
    laser = LaserSource(lcfg, mode=source_mode)

    # Graded mesh: depth refinement shared with the 1D solver (so the 2D->1D
    # reduction is exact in z); radial refinement near axis/waist/contact.
    refine_z = thermal_depth_refinement(geo.thermal_depth_m, laser.absorption_depth_m(),
                                        geo.nv_layer_depth_m, geo.front_position_m)
    refine_r = thermal_radial_refinement(geo.thermal_radius_m, lcfg.spot_radius_m)
    grid = AxisymmetricGrid2D.graded(geo.thermal_radius_m, geo.thermal_depth_m, nr, nz,
                                     refine_r=refine_r, refine_z=refine_z, max_ratio=max_ratio)
    rho = mat.rho_kg_m3
    kkw = mat.k_kwargs()

    if t_end is None:
        t_end = sol.pulse_window_s if source_mode == "pulse" else sol.recovery_window_s

    Tf = fr.T_fridge_K
    alpha_K = fr.kapitza_coeff_W_m2_K4
    q_bg = fr.background_flux_W_m3
    shape = (nr, nz)

    # precomputed mesh metrics
    V = grid.cell_volume                          # (nr, nz)
    ring = grid.ring_area                         # (nr,)
    Acr_int = grid.r_face_area[1:nr, :]           # (nr-1, nz) interior radial faces
    Acr_out = grid.r_face_area[nr, :]             # (nz,) outer face area
    dor = grid.dist_to_outer_rface                # (nr,)
    dir_ = grid.dist_to_inner_rface               # (nr,)
    dhz = grid.dist_to_high_zface                 # (nz,)
    dlz = grid.dist_to_low_zface                  # (nz,)
    q_mw_field = np.asarray(q_mw_volumetric, dtype=float)  # scalar or (nr,nz)

    if T_init is None:
        T0 = np.full(shape, Tf)
    else:
        T0 = np.asarray(T_init, dtype=float).reshape(shape).copy()

    def laser_field(t):
        return laser.q_volumetric_2d(grid.R, grid.Z, t,
                                     front_position_m=geo.front_position_m)

    def rhs(t, Tflat):
        T = np.clip(Tflat.reshape(shape), 1e-6, None)
        k = diamond_k(T, **kkw)
        cap = rho * diamond_cp(T)

        # ---- radial conservative flux ----
        if not disable_radial:
            Rr = face_series_resistance(k[:-1, :], k[1:, :],
                                        dor[:-1, None], dir_[1:, None])     # (nr-1, nz)
            cross_r = Acr_int * (T[:-1, :] - T[1:, :]) / Rr                  # power i-1 -> i [W]
            Pr = np.zeros_like(T)
            Pr[1:, :] += cross_r
            Pr[:-1, :] -= cross_r
            # Outer radial face. Production: cold contact to surrounding bulk at
            # T_fridge through the half-cell resistance. lateral_adiabatic
            # applies zero normal flux here INSTEAD, leaving the interior
            # conductances above untouched.
            if not lateral_adiabatic:
                Rout = dor[-1] / k[-1, :]
                Pr[-1, :] += Acr_out * (Tf - T[-1, :]) / Rout
        else:
            Pr = np.zeros_like(T)

        # ---- axial conservative flux ----
        Rz = face_series_resistance(k[:, :-1], k[:, 1:],
                                    dhz[None, :-1], dlz[None, 1:])           # (nr, nz-1)
        cross_z = ring[:, None] * (T[:, :-1] - T[:, 1:]) / Rz                # power j-1 -> j [W]
        Pz = np.zeros_like(T)
        Pz[:, 1:] += cross_z
        Pz[:, :-1] -= cross_z
        # z=L Kapitza sink (z=0 is insulated -> no term there)
        q_sink = alpha_K * (T[:, -1] ** 4 - Tf ** 4)                        # (nr,) [W/m^2]
        Pz[:, -1] -= ring * q_sink

        Q = laser_field(t) + q_mw_field + q_bg                              # (nr,nz) [W/m^3]
        dTdt = (Pr + Pz + Q * V) / (cap * V)
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

    # ---- energy accounting (DERIVED numerical check, MODEL-ONLY) ----
    tt = sol_obj.t
    P_dep = np.empty_like(tt)        # total deposited power in domain [W]
    P_sink = np.empty_like(tt)       # Kapitza loss [W]
    P_rout = np.empty_like(tt)       # radial outer-boundary loss [W]
    for kk, tk in enumerate(tt):
        Tk = Y[:, kk].reshape(shape)
        Qk = laser_field(tk) + q_mw_field + q_bg
        P_dep[kk] = float(np.sum(Qk * V))
        P_sink[kk] = float(np.sum(ring * alpha_K * (Tk[:, -1] ** 4 - Tf ** 4)))
        if not disable_radial and not lateral_adiabatic:
            k_out = diamond_k(np.clip(Tk[-1, :], 1e-6, None), **kkw)
            Rout = dor[-1] / k_out
            P_rout[kk] = float(np.sum(Acr_out * (Tk[-1, :] - Tf) / Rout))
        else:
            P_rout[kk] = 0.0
    E_src = float(np.trapezoid(P_dep, tt)) if tt.size > 1 else 0.0
    E_sink = float(np.trapezoid(P_sink, tt)) if tt.size > 1 else 0.0
    E_rout = float(np.trapezoid(P_rout, tt)) if tt.size > 1 else 0.0
    u_final = internal_energy_density(T_final, rho=rho)
    u_init = internal_energy_density(T0, rho=rho)
    dU = float(np.sum((u_final - u_init) * V))
    residual = E_src - E_sink - E_rout - dU
    denom = max(abs(E_src), abs(dU), 1e-30)
    energy = {
        "integrated_source_energy_J": E_src,
        "kapitza_sink_energy_J": E_sink,
        "radial_boundary_energy_J": E_rout,
        "internal_energy_change_J": dU,
        "residual_J": residual,
        "rel_residual": residual / denom,
    }

    Q_laser_field = laser_field(laser.pulse_center_s if source_mode == "pulse"
                                else float(tt[-1]))
    return Thermal2DResult(
        grid, sol_obj.t,
        Field2D(grid, T_final, name="T", unit="K"),
        Field2D(grid, T_peak, name="Tpeak", unit="K"),
        cfg, "ok" if sol_obj.success else "failed", sol_obj.message,
        T_init=T0, laser=laser, energy=energy, Q_laser_field=Q_laser_field)
