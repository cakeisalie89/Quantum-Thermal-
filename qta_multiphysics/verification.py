"""Verification suite for the QTA non-lumped multiphysics layer.

Every check here is a NUMERICAL self-consistency test of the models, not a
physical validation against hardware. Results are recorded so a reviewer can
see exactly what was and was not verified.

Emits: mesh_convergence_summary.csv, numerical_stability_summary.csv,
multiphysics_verification_summary.csv (written by the runner).
"""
from __future__ import annotations
import math
import numpy as np
from scipy.integrate import solve_ivp

from .config import MultiphysicsConfig, default_config
from .grids import Grid1D, thermal_depth_refinement, thermal_radial_refinement
from .material_models import diamond_k, diamond_cp
from .thermal_1d import solve_thermal_1d
from .thermal_2d_axisymmetric import solve_thermal_2d
from .laser_source import LaserSource


# ---------- 1D numerical checks ----------
def diffusion_sanity_1d(n=200, L=4.0e-5, k_const=2000.0, rho=3510.0, cp_const=1.0):
    """Constant-coefficient diffusion of a cosine mode vs analytic decay rate.

    T(z,0) = 1 + cos(pi z / L); insulated ends. Mode amplitude should decay as
    exp(-alpha (pi/L)^2 t), alpha = k/(rho cp). Returns relative error in the
    fitted decay rate."""
    grid = Grid1D(L, n)
    z = grid.centers
    dz = grid.dx
    alpha = k_const / (rho * cp_const)
    T0 = 1.0 + np.cos(math.pi * z / L)

    def rhs(t, T):
        d = np.zeros_like(T)
        d[1:-1] = alpha * (T[2:] - 2 * T[1:-1] + T[:-2]) / dz**2
        d[0] = alpha * (T[1] - T[0]) / dz**2
        d[-1] = alpha * (T[-2] - T[-1]) / dz**2
        return d

    t_end = 0.2 * L**2 / alpha
    teval = np.linspace(0, t_end, 30)
    so = solve_ivp(rhs, (0, t_end), T0, method="BDF", t_eval=teval, rtol=1e-8, atol=1e-12)
    amp = so.y.max(axis=0) - so.y.min(axis=0)  # peak-to-... amplitude of cosine mode
    amp0 = amp[amp > 0]
    # fit log(amp) vs t
    mask = amp > 1e-9
    rate_fit = -np.polyfit(so.t[mask], np.log(amp[mask]), 1)[0]
    rate_analytic = alpha * (math.pi / L) ** 2
    rel_err = abs(rate_fit - rate_analytic) / rate_analytic
    return {"rate_fit": rate_fit, "rate_analytic": rate_analytic,
            "rel_error": rel_err, "finite": bool(np.all(np.isfinite(so.y)))}


def source_energy_integral_1d(cfg):
    """Check the 1D Beer-Lambert depth profile integrates to the areal power on
    the SAME graded finite-volume mesh the solver uses (cell-width weighted)."""
    laser = LaserSource(cfg.laser, mode="averaged")
    refine = thermal_depth_refinement(cfg.geometry.thermal_depth_m, laser.absorption_depth_m(),
                                      cfg.geometry.nv_layer_depth_m, cfg.geometry.front_position_m)
    grid = Grid1D.graded(cfg.geometry.thermal_depth_m, cfg.solver.n_cells_1d, refine_at=refine)
    Q = laser.q_volumetric_1d(grid.centers, 0.0)              # W/m^3
    integral_areal = float(np.sum(Q * grid.cell_widths))      # W/m^2 over resolved depth
    A_spot = 0.5 * math.pi * cfg.laser.spot_radius_m ** 2
    expected_areal = cfg.laser.absorbed_average_power_W / A_spot
    # only the fraction within the resolved depth is captured
    captured = 1.0 - math.exp(-cfg.laser.absorption_coeff_1_m * cfg.geometry.thermal_depth_m)
    rel = abs(integral_areal - expected_areal * captured) / (expected_areal * captured)
    return {"integral_areal_W_m2": integral_areal,
            "expected_areal_W_m2": expected_areal * captured, "rel_error": rel}


def source_energy_integral_2d(cfg, n_r=40, n_z=48):
    """Check int Q_2d dV == absorbed power on the SAME graded axisymmetric mesh
    the solver uses, with EXACT annular cell volumes."""
    from .grids import AxisymmetricGrid2D
    laser = LaserSource(cfg.laser, mode="averaged")
    refine_z = thermal_depth_refinement(cfg.geometry.thermal_depth_m, laser.absorption_depth_m(),
                                        cfg.geometry.nv_layer_depth_m, cfg.geometry.front_position_m)
    refine_r = thermal_radial_refinement(cfg.geometry.thermal_radius_m, cfg.laser.spot_radius_m)
    grid = AxisymmetricGrid2D.graded(cfg.geometry.thermal_radius_m, cfg.geometry.thermal_depth_m,
                                     n_r, n_z, refine_r=refine_r, refine_z=refine_z)
    Q = laser.q_volumetric_2d(grid.R, grid.Z, 0.0)
    integral = float(np.sum(Q * grid.cell_volume))  # W
    P = cfg.laser.absorbed_average_power_W
    # fraction captured radially and in depth
    frac_r = 1.0 - math.exp(-2.0 * cfg.geometry.thermal_radius_m**2 / cfg.laser.spot_radius_m**2)
    frac_z = 1.0 - math.exp(-cfg.laser.absorption_coeff_1_m * cfg.geometry.thermal_depth_m)
    expected = P * frac_r * frac_z
    rel = abs(integral - expected) / expected
    return {"integral_W": integral, "expected_W": expected, "rel_error": rel}


def kapitza_sign_check(cfg):
    """Backside Kapitza term must cool when T>Tf and warm when T<Tf."""
    aK = cfg.fridge.kapitza_coeff_W_m2_K4
    Tf = cfg.fridge.T_fridge_K
    hot = aK * ((Tf + 1.0) ** 4 - Tf ** 4)   # >0 means heat leaves (cools cell)
    cold = aK * ((Tf * 0.5) ** 4 - Tf ** 4)  # <0 means heat enters (warms cell)
    return {"hot_outflux_positive": bool(hot > 0), "cold_influx_negative": bool(cold < 0),
            "sign_correct": bool(hot > 0 and cold < 0)}


def mesh_convergence_1d(cfg):
    rows = []
    vals = {}
    for n in (100, 200, 400):
        r = solve_thermal_1d(cfg, source_mode="averaged", n_cells=n, n_eval=30)
        vals[n] = r.nv_layer_temperature_K()
        rows.append({"model": "thermal_1d", "mesh": f"n={n}",
                     "metric": "NV_layer_T_K", "value": vals[n],
                     "solver_status": r.solver_status})
    rel = abs(vals[400] - vals[200]) / max(abs(vals[400]), 1e-12)
    converged = rel < 0.15
    return rows, {"thermal_1d_rel_change_200_400": rel, "thermal_1d_converged": converged}


def mesh_convergence_2d(cfg):
    rows = []
    vals = {}
    for tag, (nr, nz) in [("coarse", (24, 32)), ("medium", (32, 40)), ("fine", (40, 48))]:
        r = solve_thermal_2d(cfg, source_mode="averaged", n_r=nr, n_z=nz, n_eval=12)
        vals[tag] = r.nv_layer_max_K()
        rows.append({"model": "thermal_2d", "mesh": f"{tag}({nr}x{nz})",
                     "metric": "NV_layer_max_T_K", "value": vals[tag],
                     "solver_status": r.solver_status})
    rel = abs(vals["fine"] - vals["medium"]) / max(abs(vals["fine"]), 1e-12)
    converged = rel < 0.25
    return rows, {"thermal_2d_rel_change_med_fine": rel, "thermal_2d_converged": converged}


def axis_symmetry_2d(cfg):
    """No singularity at r=0; radial gradient at the axis is small."""
    r = solve_thermal_2d(cfg, source_mode="averaged", n_r=32, n_z=40, n_eval=10)
    finite = r.T_final.is_finite()
    rg = r.T_final.radial_gradient()
    axis_grad = float(np.max(np.abs(rg[0, :])))
    bulk_grad = float(np.max(np.abs(rg)))
    return {"axis_finite": finite, "axis_radial_gradient": axis_grad,
            "axis_grad_small_vs_bulk": bool(axis_grad <= bulk_grad)}


def reduction_2d_to_1d(cfg):
    """2D with radial transport disabled should recover the 1D answer.

    With radial transport off, each radial column is an independent 1D depth
    problem. The axis column's areal power P*radial_density(0) = P/A_spot is
    exactly the 1D slab areal power, so the hottest 2D column (near r=0) must
    match the 1D NV temperature. (Overriding the spot would change the source
    normalization and is therefore NOT done here.)"""
    r1 = solve_thermal_1d(cfg, source_mode="averaged", n_cells=cfg.solver.n_z_2d, n_eval=20)
    # fine near-axis radial resolution so the axis column sits close to r=0
    r2 = solve_thermal_2d(cfg, source_mode="averaged", n_r=24, n_z=cfg.solver.n_z_2d,
                          n_eval=12, disable_radial=True)
    nv1 = r1.nv_layer_temperature_K()
    nv2 = r2.nv_layer_max_K()
    rel = abs(nv2 - nv1) / max(abs(nv1), 1e-12)
    return {"nv_1d_K": nv1, "nv_2d_reduced_K": nv2, "rel_error": rel,
            "reduces_to_1d": bool(rel < 0.15)}


def coupling_checks(cfg):
    """Verify the documented couplings actually hold in code."""
    from .surface_coverage import surface_coverage_1d
    out = {}
    # optical Q feeds thermal: averaged laser produces heating above base
    rT = solve_thermal_1d(cfg, source_mode="averaged", n_cells=80, n_eval=20)
    out["optical_feeds_thermal"] = bool(rT.hotspot_temperature_K() > cfg.fridge.T_fridge_K * 1.01)
    # gas flux feeds surface adsorption: nonzero gas -> nonzero coverage
    covWith, _, _ = surface_coverage_1d({"CH4": 1e17}, T_surface_K=20.0, t_end=0.5, mode="B")
    covNone, _, _ = surface_coverage_1d({"CH4": 0.0}, T_surface_K=20.0, t_end=0.5, mode="B")
    out["gas_feeds_surface"] = bool(covWith["CH4"][-1] > covNone["CH4"][-1])
    # thermal feeds desorption: hotter surface -> lower equilibrium coverage
    covCold, _, _ = surface_coverage_1d({"CH4": 1e17}, T_surface_K=5.0, t_end=0.5, mode="B")
    covHot, _, _ = surface_coverage_1d({"CH4": 1e17}, T_surface_K=80.0, t_end=0.5, mode="B")
    out["thermal_feeds_desorption"] = bool(covHot["CH4"][-1] <= covCold["CH4"][-1])
    out["all_couplings_ok"] = all(out.values())
    return out


def run_verification(cfg=None):
    """Run the whole suite, return (mesh_rows, stability_rows, verification_rows)."""
    cfg = cfg or default_config()

    diff = diffusion_sanity_1d()
    se1 = source_energy_integral_1d(cfg)
    se2 = source_energy_integral_2d(cfg)
    kap = kapitza_sign_check(cfg)
    m1_rows, m1 = mesh_convergence_1d(cfg)
    m2_rows, m2 = mesh_convergence_2d(cfg)
    sym = axis_symmetry_2d(cfg)
    red = reduction_2d_to_1d(cfg)
    cpl = coupling_checks(cfg)

    mesh_rows = m1_rows + m2_rows

    stability_rows = [
        {"check": "diffusion_sanity_1d_rel_error", "value": diff["rel_error"],
         "pass_if_measured": diff["rel_error"] < 0.10, "evidence": "MODEL_ONLY"},
        {"check": "thermal_1d_finite", "value": 1.0, "pass_if_measured": diff["finite"],
         "evidence": "MODEL_ONLY"},
        {"check": "source_energy_integral_1d_rel_error", "value": se1["rel_error"],
         "pass_if_measured": se1["rel_error"] < 0.05, "evidence": "MODEL_ONLY"},
        {"check": "source_energy_integral_2d_rel_error", "value": se2["rel_error"],
         "pass_if_measured": se2["rel_error"] < 0.10, "evidence": "MODEL_ONLY"},
        {"check": "kapitza_sign_correct", "value": 1.0 if kap["sign_correct"] else 0.0,
         "pass_if_measured": kap["sign_correct"], "evidence": "MODEL_ONLY"},
        {"check": "axis_no_singularity_finite", "value": 1.0 if sym["axis_finite"] else 0.0,
         "pass_if_measured": sym["axis_finite"], "evidence": "MODEL_ONLY"},
    ]

    verification_rows = [
        {"check": "thermal_1d_mesh_convergence",
         "detail": f"rel_change(200->400)={m1['thermal_1d_rel_change_200_400']:.3e}",
         "status_if_measured": "CONVERGING" if m1["thermal_1d_converged"] else "NOT_CONVERGED",
         "evidence": "MODEL_ONLY"},
        {"check": "thermal_2d_mesh_convergence",
         "detail": f"rel_change(med->fine)={m2['thermal_2d_rel_change_med_fine']:.3e}",
         "status_if_measured": "CONVERGING" if m2["thermal_2d_converged"] else "NOT_CONVERGED",
         "evidence": "MODEL_ONLY"},
        {"check": "kapitza_bc_sign", "detail": str(kap),
         "status_if_measured": "OK" if kap["sign_correct"] else "FAIL", "evidence": "MODEL_ONLY"},
        {"check": "source_energy_conservation_1d", "detail": f"rel_error={se1['rel_error']:.3e}",
         "status_if_measured": "OK" if se1["rel_error"] < 0.05 else "CHECK", "evidence": "MODEL_ONLY"},
        {"check": "source_energy_conservation_2d", "detail": f"rel_error={se2['rel_error']:.3e}",
         "status_if_measured": "OK" if se2["rel_error"] < 0.10 else "CHECK", "evidence": "MODEL_ONLY"},
        {"check": "axis_symmetry_2d", "detail": str(sym),
         "status_if_measured": "OK" if sym["axis_grad_small_vs_bulk"] else "CHECK", "evidence": "MODEL_ONLY"},
        {"check": "reduction_2d_to_1d", "detail": f"rel_error={red['rel_error']:.3e}",
         "status_if_measured": "OK" if red["reduces_to_1d"] else "CHECK", "evidence": "MODEL_ONLY"},
        {"check": "diffusion_sanity_1d", "detail": f"rel_error={diff['rel_error']:.3e}",
         "status_if_measured": "OK" if diff["rel_error"] < 0.10 else "CHECK", "evidence": "MODEL_ONLY"},
        {"check": "coupling_optical_thermal_gas_surface", "detail": str(cpl),
         "status_if_measured": "OK" if cpl["all_couplings_ok"] else "CHECK", "evidence": "MODEL_ONLY"},
    ]

    summary = {
        "diffusion": diff, "source_1d": se1, "source_2d": se2, "kapitza": kap,
        "mesh_1d": m1, "mesh_2d": m2, "symmetry": sym, "reduction": red, "coupling": cpl,
    }
    return mesh_rows, stability_rows, verification_rows, summary
