"""Numerical infrastructure: finite-volume diffusion operators (1D and 2D
axisymmetric), stability/finite checks, and small helpers.

Finite-volume is used so that fluxes are conservative. Transient PDEs are
integrated by method-of-lines with scipy.integrate.solve_ivp (stiff: BDF).

MODEL-ONLY infrastructure.
"""
from __future__ import annotations
import numpy as np
import scipy.sparse as sp


#: A solve that did not converge carries no scientific authority. Any consumer
#: -- Mode-C readiness, Mode-D start, eligibility forecasts, reduction checks,
#: derived metrics -- must deny authority rather than read the numbers anyway.
SOLVER_OK = "ok"


class SolverFailure(RuntimeError):
    """A numerical solve did not converge; downstream authority is denied."""


def require_converged(result, what: str):
    """Fail closed on a non-converged solve.

    solver_status used to be reported alongside the metrics as a passive
    string while ready_terms was computed from the same result regardless, so
    a failed BDF integration could still produce FORECAST_READY_IF_MEASURED.
    Readiness is now unreachable without convergence.

    WHY THIS LIVES HERE AND NOT BESIDE THE 1D COUPLED SOLVER, WHERE IT WAS
    WRITTEN. Because it was written where the defect was found and applied
    only there. For a long time it had exactly two call sites, both in
    ``run_coupled``, while ``run_mode_sequence_3d`` -- whose own docstring
    says it runs the canonical mode order "exactly mirroring the 1D/2D
    coupled_mode_solver" -- mirrored everything about it except this. A rule
    that lives inside one of the things it governs is a rule the next sibling
    does not inherit, so it lives in the numerics layer that every solver
    already depends on.
    """
    status = getattr(result, "solver_status", None)
    if status != SOLVER_OK:
        raise SolverFailure(
            f"{what}: solver_status={status!r} (expected {SOLVER_OK!r}); "
            "readiness, eligibility and derived metrics are denied")
    return result


def require_integrated(sol_obj, what: str):
    """Fail closed on a raw ``solve_ivp`` result that did not finish.

    The counterpart of :func:`require_converged` for call sites that hold a
    scipy ``OdeResult`` rather than one of this repository's result objects.
    Both say the same thing in the vocabulary of the layer they sit in.

    WHY IT IS NEEDED SEPARATELY. A failed integration does not return
    garbage; it returns a SHORTER trajectory. ``so.y`` holds the points
    reached, every one of them finite, so ``assert_finite`` passes and the
    values look ordinary. Callers that pair ``so.y`` with the ``t_eval`` they
    asked for then carry two arrays of different lengths, and the one that
    describes time is the one that is still full length.
    """
    if not getattr(sol_obj, "success", False):
        raise SolverFailure(
            f"{what}: the integration did not finish "
            f"(status={getattr(sol_obj, 'status', None)!r}, "
            f"{getattr(sol_obj, 'message', '')!r}); a truncated trajectory is "
            "shorter, not wrong-looking, so nothing downstream would notice")
    return sol_obj


def assert_finite(arr, name="array"):
    a = np.asarray(arr, dtype=float)
    if not np.all(np.isfinite(a)):
        bad = int(np.sum(~np.isfinite(a)))
        raise FloatingPointError(f"{name} contains {bad} non-finite values")
    return a


def explicit_diffusion_cfl_dt(alpha_max, dx):
    """Max stable explicit time step for 1D diffusion: dt <= dx^2/(2 alpha)."""
    return dx * dx / (2.0 * max(alpha_max, 1e-300))


def harmonic_face_k(k_cells):
    """Harmonic mean of conductivity at interior faces (series conduction).

    k_cells: array (n,). Returns face conductivities (n-1,)."""
    k = np.asarray(k_cells, dtype=float)
    kl, kr = k[:-1], k[1:]
    return 2.0 * kl * kr / (kl + kr + 1e-300)


def laplacian_1d_flux(T, k_cells, dx):
    """Conservative 1D FV divergence of k*grad(T) at cell centers, interior only.

    Returns d/dz[k dT/dz] approximation [W/m^3 per (k units)] with zero at the
    two boundary cells (boundaries handled separately by the caller). Units:
    if k is W/m/K and T is K, result is W/m^3 (per unit cross-section)."""
    T = np.asarray(T, dtype=float)
    kf = harmonic_face_k(k_cells)            # (n-1,)
    flux = kf * (T[1:] - T[:-1]) / dx        # (n-1,) face fluxes [W/m^2]
    div = np.zeros_like(T)
    div[1:-1] = (flux[1:] - flux[:-1]) / dx  # interior divergence [W/m^3]
    return div, flux


def build_axisym_diffusion_operator(grid, k_const):
    """Build a sparse linear operator for the *constant-k* axisymmetric
    Laplacian (1/r) d/dr(r k dT/dr) + d/dz(k dT/dz) with homogeneous Neumann
    (insulated) boundaries on all sides. Used for verification / preconditioning
    and as the diffusion backbone; nonlinear k(T) corrections are applied
    explicitly by the solver. Returns a (N x N) CSR matrix with N = nr*nz.

    Indexing: flat index = i*nz + j for cell (i, j).
    """
    nr, nz = grid.nr, grid.nz
    dr, dz = grid.dr, grid.dz
    rc = grid.r_centers
    rf = grid.r_faces
    N = nr * nz
    rows, cols, vals = [], [], []

    def idx(i, j):
        return i * nz + j

    for i in range(nr):
        for j in range(nz):
            p = idx(i, j)
            diag = 0.0
            # radial faces: face at i-1/2 (area ~ r_{i-1/2}) and i+1/2
            # inner face (between i-1 and i)
            if i > 0:
                r_face = rf[i]
                coef = k_const * r_face / (rc[i] * dr * dr)
                rows.append(p); cols.append(idx(i - 1, j)); vals.append(coef)
                diag -= coef
            if i < nr - 1:
                r_face = rf[i + 1]
                coef = k_const * r_face / (rc[i] * dr * dr)
                rows.append(p); cols.append(idx(i + 1, j)); vals.append(coef)
                diag -= coef
            # axial faces
            if j > 0:
                coef = k_const / (dz * dz)
                rows.append(p); cols.append(idx(i, j - 1)); vals.append(coef)
                diag -= coef
            if j < nz - 1:
                coef = k_const / (dz * dz)
                rows.append(p); cols.append(idx(i, j + 1)); vals.append(coef)
                diag -= coef
            rows.append(p); cols.append(p); vals.append(diag)
    return sp.csr_matrix((vals, (rows, cols)), shape=(N, N))


def face_series_resistance(k_left, k_right, dist_left, dist_right):
    """Per-unit-area thermal resistance of a finite-volume face.

    For a face shared by two cells with conductivities ``k_left``/``k_right``
    whose centres lie ``dist_left``/``dist_right`` from the face, series
    conduction gives ``R = dist_left/k_left + dist_right/k_right`` and the
    face heat flux is ``(T_left - T_right) / R`` [W/m^2]. This is the
    physically correct (distance-weighted harmonic) face interpolation on a
    NONUNIFORM mesh; for equal half-widths and constant k it reduces to the
    usual harmonic mean dx/k. Arrays broadcast elementwise.
    """
    kl = np.maximum(np.asarray(k_left, dtype=float), 1e-300)
    kr = np.maximum(np.asarray(k_right, dtype=float), 1e-300)
    return np.asarray(dist_left, dtype=float) / kl + np.asarray(dist_right, dtype=float) / kr
