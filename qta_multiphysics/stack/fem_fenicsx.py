"""FEniCSx (dolfinx) adapter — STAGED, with its acceptance contract executable.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

FEniCSx is the intended route to an unstructured, higher-fidelity FEM thermal
solve. It is **not adopted**: dolfinx is not a wheel-installable dependency,
it is not in the container, and no result from it is canonical. What this
module provides now is the part that must exist *before* adoption is even
discussable — a written, executable acceptance contract:

1. **Manufactured-solution convergence.** The solver must reproduce a known
   exact solution of the transient heat equation at its nominal order (2 for
   P1 elements in L2) on a refinement sequence.
2. **Reduction to the canonical backend.** On the 1D configuration it must
   agree with ``thermal_1d`` within the tolerance the project already applies
   to its 2D->1D reduction check.
3. **Energy conservation.** Its relative energy residual must meet the same
   bound the existing 3D layer is held to.
4. **Determinism.** Repeated runs on identical input must agree to round-off.

Until every criterion passes on a real dolfinx build, this module reports
``status = STAGED`` and ``availability = UNAVAILABLE``, and the finite-volume
backends remain the authority. The harness is deliberately solver-agnostic:
:func:`run_acceptance` takes any solver callable, so it is exercised and
tested today against an analytic reference and needs no edit when dolfinx
arrives.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Callable

import numpy as np
import numpy.typing as npt

from . import AUTOMATIC_GATE_EFFECT, LABEL
from .workspace import StrPath, guard_output_dir, write_json_deterministic

ADOPTION_STATUS = "STAGED"
EXACT_RECOVERY_FLOOR = 1e-13   # below this an "error" is round-off, not discretisation

# Numeric thresholds are kept in their own mapping so a threshold is always
# a number: mixing the explanatory note in here once made `>=` a type error.
ACCEPTANCE_CRITERIA: dict[str, float] = {
    "mms_observed_order_min": 1.8,
    "mms_observed_order_target": 2.0,
    "reduction_vs_thermal_1d_rel_tol": 0.02,
    "energy_residual_rel_max": 1e-3,
    "determinism_rel_tol": 1e-12,
}
ACCEPTANCE_NOTE = ("all four criteria must pass on a real dolfinx build "
                   "before any FEM result may be compared with, let alone "
                   "replace, a canonical finite-volume output")


def dolfinx_available() -> bool:
    try:
        import dolfinx  # noqa: F401
        return True
    except Exception:
        return False


class ManufacturedSolution:
    """Exact solution of ``rho*cp*dT/dt = k*d2T/dz2 + Q`` on ``[0, L]``.

    ``T(z, t) = T0 + A e^{-lambda t} cos(pi z / L)`` has zero gradient at both
    ends, so it satisfies homogeneous Neumann boundaries exactly and the
    convergence study measures interior discretisation error rather than
    boundary treatment. The source term is the residual the PDE needs for
    this ``T`` to be exact, so any correct solver must converge to it.
    """

    def __init__(self, L: float = 1.0, k: float = 1.0, rho_cp: float = 1.0,
                 amplitude: float = 1.0, decay: float = 1.0,
                 T0: float = 0.0):
        self.L, self.k, self.rho_cp = float(L), float(k), float(rho_cp)
        self.A, self.lam, self.T0 = float(amplitude), float(decay), float(T0)

    def T(self, z: npt.ArrayLike, t: float) -> np.ndarray:
        z = np.asarray(z, dtype=float)
        return self.T0 + self.A * math.exp(-self.lam * t) * np.cos(
            math.pi * z / self.L)

    def source(self, z: npt.ArrayLike, t: float) -> np.ndarray:
        """``Q(z, t)`` making ``T`` an exact solution (units: W/m^3)."""
        z = np.asarray(z, dtype=float)
        spatial = np.cos(math.pi * z / self.L)
        coeff = (-self.rho_cp * self.lam
                 + self.k * (math.pi / self.L) ** 2)
        return self.A * math.exp(-self.lam * t) * spatial * coeff

    def describe(self) -> dict:
        return {"form": "T0 + A*exp(-lambda*t)*cos(pi*z/L)",
                "L": self.L, "k": self.k, "rho_cp": self.rho_cp,
                "amplitude": self.A, "decay": self.lam, "T0": self.T0,
                "boundary_conditions": "homogeneous Neumann at z=0 and z=L",
                "expected_order_L2": 2.0}


def l2_error(numeric: npt.ArrayLike, exact: npt.ArrayLike,
             dz: npt.ArrayLike) -> float:
    """Cell-volume-weighted discrete L2 error (same weighting as the FV code)."""
    num = np.asarray(numeric, dtype=float)
    ex = np.asarray(exact, dtype=float)
    w = np.asarray(dz, dtype=float)
    if num.shape != ex.shape or num.shape != w.shape:
        raise ValueError("numeric, exact and dz must share a shape")
    return float(math.sqrt(np.sum(w * (num - ex) ** 2) / np.sum(w)))


def observed_order(errors: npt.ArrayLike,
                   h_values: npt.ArrayLike) -> float:
    """Least-squares slope of ``log(error)`` vs ``log(h)`` over a refinement set."""
    e = np.asarray(errors, dtype=float)
    h = np.asarray(h_values, dtype=float)
    if e.size < 2 or np.any(e <= 0) or np.any(h <= 0):
        raise ValueError("need at least two positive errors and mesh sizes")
    slope, _ = np.polyfit(np.log(h), np.log(e), 1)
    return float(slope)


def run_acceptance(solver_fn: Callable[..., npt.ArrayLike],
                   n_cells_sequence: Sequence[int] = (20, 40, 80, 160),
                   t_final: float = 0.1,
                   mms: ManufacturedSolution | None = None) -> dict:
    """Run the manufactured-solution criterion against any solver callable.

    ``solver_fn(z_centres, dz, source_fn, t_final, mms) -> T`` returns the
    cell-centred temperature field at ``t_final``. The harness owns the mesh,
    the exact solution, the error norm, and the verdict, so swapping the
    solver cannot move the goalposts.
    """
    mms = mms or ManufacturedSolution()
    errors: list[float] = []
    h_values: list[float] = []
    rows: list[dict] = []
    for n in n_cells_sequence:
        faces = np.linspace(0.0, mms.L, int(n) + 1)
        dz = np.diff(faces)
        zc = 0.5 * (faces[:-1] + faces[1:])
        T_num = np.asarray(solver_fn(zc, dz, mms.source, t_final, mms),
                           dtype=float)
        err = l2_error(T_num, mms.T(zc, t_final), dz)
        errors.append(err)
        h_values.append(float(mms.L / n))
        rows.append({"n_cells": int(n), "h": float(mms.L / n),
                     "l2_error": err})
    if max(errors) <= EXACT_RECOVERY_FLOOR:
        # A solver that returns the exact field has no order to measure. This
        # is the harness self-check: it proves the error norm reads zero for a
        # known-exact answer, so a non-zero reading later is discretisation
        # error and not a bug in the norm.
        return {"criterion": "manufactured_solution_convergence",
                "manufactured_solution": mms.describe(),
                "refinement": rows,
                "observed_order_L2": None,
                "max_l2_error": max(errors),
                "required_order_min": ACCEPTANCE_CRITERIA[
                    "mms_observed_order_min"],
                "verdict": "EXACT_RECOVERED",
                "note": "solver reproduced the manufactured solution to "
                        "round-off; convergence order is undefined and this "
                        "result validates the harness, not a discretisation",
                "label": LABEL,
                "automatic_gate_effect": AUTOMATIC_GATE_EFFECT}
    order = observed_order(errors, h_values)
    for i in range(1, len(rows)):
        rows[i]["pairwise_order"] = float(
            math.log(rows[i - 1]["l2_error"] / rows[i]["l2_error"])
            / math.log(rows[i - 1]["h"] / rows[i]["h"]))
    passed = order >= ACCEPTANCE_CRITERIA["mms_observed_order_min"]
    return {"criterion": "manufactured_solution_convergence",
            "manufactured_solution": mms.describe(),
            "refinement": rows,
            "observed_order_L2": order,
            "required_order_min": ACCEPTANCE_CRITERIA[
                "mms_observed_order_min"],
            "verdict": "PASS" if passed else "FAIL",
            "label": LABEL,
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT}


def analytic_reference_solver(z_centres: npt.ArrayLike, dz: npt.ArrayLike,
                              source_fn: Callable, t_final: float,
                              mms: ManufacturedSolution) -> np.ndarray:
    """Reference 'solver' returning the exact field — harness self-check only.

    Used to prove the harness measures zero error for an exact answer. It is
    not a discretisation and can never stand in for one.
    """
    return mms.T(z_centres, t_final)


def fv_reference_solver(z_centres: npt.ArrayLike, dz: npt.ArrayLike,
                        source_fn: Callable, t_final: float,
                        mms: ManufacturedSolution) -> np.ndarray:
    """Second-order finite-volume method-of-lines solve of the MMS problem.

    A genuine discretisation, integrated with a stiff BDF integrator at tight
    tolerances so the measured error is spatial. Its role is to prove the
    harness can *detect* order — a harness that only ever sees an exact
    solver has never been tested. It is a verification fixture, not a project
    backend: the canonical solvers stay in ``qta_multiphysics``.
    """
    from scipy.integrate import solve_ivp

    z = np.asarray(z_centres, dtype=float)
    h = np.asarray(dz, dtype=float)
    n = z.size
    # interior face conductances k/Δz_centre-to-centre; homogeneous Neumann
    # ends contribute no flux, exactly as the manufactured solution requires
    d_centre = np.diff(z)
    G = mms.k / d_centre                                   # (n-1,)

    def rhs(t: float, T: np.ndarray) -> np.ndarray:
        flux = G * (T[1:] - T[:-1])                        # face fluxes
        div = np.zeros(n)
        div[:-1] += flux
        div[1:] -= flux
        return (div / h + source_fn(z, t)) / mms.rho_cp

    sol = solve_ivp(rhs, (0.0, float(t_final)), mms.T(z, 0.0), method="BDF",
                    rtol=1e-11, atol=1e-13, t_eval=[float(t_final)])
    if not sol.success:
        raise RuntimeError(f"MMS reference solve failed: {sol.message}")
    return sol.y[:, -1]


def status_report(out_dir: StrPath | None = None) -> dict:
    """Staged-adoption status: what is required, and what is true right now."""
    available = dolfinx_available()
    report = {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.fem_fenicsx",
        "component": "FEniCSx / dolfinx thermal FEM backend",
        "adoption_status": ADOPTION_STATUS,
        "availability": "AVAILABLE" if available else "UNAVAILABLE",
        "install_route": "environment-provided (conda-forge, spack, or the "
                         "dolfinx container); intentionally not a wheel "
                         "dependency and not in the QTA container",
        "authority_in_force": "qta_multiphysics.thermal_1d / "
                              "thermal_2d_axisymmetric / "
                              "thermal_3d_transient (finite volume)",
        "acceptance_criteria": ACCEPTANCE_CRITERIA,
        "acceptance_note": ACCEPTANCE_NOTE,
        "harness_status": "EXECUTABLE — run_acceptance() is solver-agnostic "
                          "and is exercised in CI against the analytic "
                          "reference; no edit is needed when dolfinx arrives",
        "blocking": [] if available else [
            "dolfinx not importable in this environment"],
        "note": "no FEM result is canonical, comparable, or gate-relevant "
                "until every acceptance criterion passes on a real build",
    }
    if out_dir is not None:
        out = guard_output_dir(out_dir)
        write_json_deterministic(out / "fenicsx_status.json", report)
    return report
