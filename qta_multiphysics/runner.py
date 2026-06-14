"""run_all: orchestrate the QTA non-lumped multiphysics layer.

Executes every implemented module, writes all declared CSV/JSON outputs to the
given directory, and returns (summary_dict, gate_specs). The host sim converts
gate_specs into Gate objects. 3D is FUTURE_WORK and excluded from gates.

MODEL-ONLY / FORECAST-ONLY / ZERO-PASS.
"""
from __future__ import annotations
import os
import math
import numpy as np

from .config import default_config
from .thermal_1d import solve_thermal_1d
from .thermal_2d_axisymmetric import solve_thermal_2d
from .optical_absorption import optical_absorption_1d, optical_absorption_2d
from .gas_transport_1d import solve_gas_transport_1d, default_species
from .gas_transport_2d import gas_exposure_map_2d
from .surface_coverage import surface_coverage_1d, default_coverage_specs, kinetic_flux
from .microwave_heating_1d import microwave_path_1d
from .radiation_paths import radiation_paths
from .vibration_transfer import vibration_transfer
from .coupled_mode_solver import run_coupled
from .verification import run_verification
from .uncertainty import run_monte_carlo
from . import future_3d
from .metrics import build_gate_specs
from .exports import write_rows_csv, write_profile_csv, write_json

EVID = "FORECAST_ONLY;MODEL_ONLY;NOT_MEASURED_IN_THIS_SYSTEM"


def run_all(outdir, mc_samples=60, verbose=True):
    os.makedirs(outdir, exist_ok=True)
    cfg = default_config()

    def out(name):
        return os.path.join(outdir, name)

    def log(*a):
        if verbose:
            print("[multiphysics]", *a)

    # ---------- optical absorption ----------
    g1, qf1, om1 = optical_absorption_1d(cfg)
    g2, qf2, rad_fluence, om2 = optical_absorption_2d(cfg, n_r=40, n_z=48)
    write_profile_csv(out("optical_absorption_profile.csv"),
                      ["z_m", "Q_laser_W_m3"], [list(g1.centers), list(qf1.values)])
    qf2.to_slice_csv(out("optical_absorption_2d_slices.csv"))
    write_rows_csv(out("optical_absorption_metrics.csv"),
                   [{"metric": k, "value": v, "evidence_class": EVID,
                     "measured_in_this_system": "false"} for k, v in {**om1, **{f"2d_{k}": v for k, v in om2.items()}}.items()])
    log("optical done")

    # ---------- distributed thermal (1D + 2D) ----------
    t1 = solve_thermal_1d(cfg, source_mode="averaged", n_eval=80)
    t1p = solve_thermal_1d(cfg, source_mode="pulse", n_eval=80)
    t2 = solve_thermal_2d(cfg, source_mode="averaged", n_r=40, n_z=48, n_eval=20)
    t2.T_peak.to_slice_csv(out("distributed_thermal_2d_slices.csv"))
    # Per-cell 1D mesh + field outputs, all generated from the actual finite-
    # volume arrays the solver integrated (genuinely nonuniform cells).
    nv1 = t1.nv_layer_samples()
    write_profile_csv(
        out("distributed_thermal_profile.csv"),
        ["z_m", "z_left_face_m", "z_right_face_m", "cell_width_m",
         "T_initial_K", "T_final_K", "T_peak_K",
         "Q_laser_W_m3", "Q_mw_W_m3", "gradient_K_per_m", "heat_flux_W_m2"],
        [list(t1.z_centers), list(t1.grid.faces[:-1]), list(t1.grid.faces[1:]),
         list(t1.cell_widths), list(t1.T_initial), list(t1.T_final), list(t1.T_peak),
         list(t1.Q_laser()), list(t1.Q_mw), list(t1.gradient()), list(t1.heat_flux())])
    hz_r, hz_z = t2.hotspot_rz()
    thermal_metrics = {
        "NV_layer_T_1d_K": t1.nv_layer_temperature_K(),
        "max_T_1d_K": t1.hotspot_temperature_K(),
        "max_gradient_1d_K_per_m": t1.max_gradient_K_per_m(),
        "pulse_peak_NV_1d_K": t1p.nv_layer_temperature_K(),
        "post_pulse_drift_1d_K": t1p.post_pulse_drift_K(),
        "NV_layer_mean_2d_K": t2.nv_layer_mean_K(),
        "NV_layer_max_2d_K": t2.nv_layer_max_K(),
        "hotspot_r_m": hz_r, "hotspot_z_m": hz_z, "max_T_2d_K": t2.max_T_K(),
        "max_radial_gradient_2d_K_per_m": t2.max_radial_gradient_K_per_m(),
        "max_depth_gradient_2d_K_per_m": t2.max_depth_gradient_K_per_m(),
        "thermal_1d_solver_status": t1.solver_status,
        "thermal_2d_solver_status": t2.solver_status,
        # finite-volume energy-balance residual (DERIVED numerical check)
        "energy_balance_residual_1d_rel": t1.energy_residual(),
        "energy_balance_residual_2d_rel": t2.energy_residual(),
        # NV-layer sampled values (1D), from the actual mesh
        "nv_depth_1d_m": nv1["nv_depth_m"],
        "nv_T_initial_1d_K": nv1["nv_T_initial_K"],
        "nv_T_final_1d_K": nv1["nv_T_final_K"],
        # mesh descriptors (evidence the meshes are genuinely nonuniform)
        "mesh_1d_min_cell_m": t1.grid.min_width,
        "mesh_1d_max_cell_m": t1.grid.max_width,
        "mesh_2d_min_cell_volume_m3": float(t2.cell_volume.min()),
        "mesh_2d_max_cell_volume_m3": float(t2.cell_volume.max()),
        # --- interpretation guards (so values are not misread) ---
        "value_units": "temperatures_in_Kelvin",
        "nv_temperature_interpretation": (
            "near-surface thermal hotspot temperature/rise above fridge base; "
            "NOT a deposition or growth rate"),
        "deposition_yield_status": (
            "UNKNOWN/BLOCKED: no LCVD methane-dissociation/sticking/"
            "carbon-incorporation/yield model; thermal field does not validate "
            "C13 deposition rate"),
    }
    write_rows_csv(out("distributed_thermal_metrics.csv"),
                   [{"metric": k, "value": v, "evidence_class": EVID,
                     "measured_in_this_system": "false"} for k, v in thermal_metrics.items()])
    log("thermal done")

    # ---------- gas transport (1D B/C + 2D map) ----------
    species = default_species()
    gB = solve_gas_transport_1d(mode="B", t_end=2.0)
    n_init = {s.name: gB.profile_final(s.name) for s in species}
    gC = solve_gas_transport_1d(mode="C", t_end=2.0, n_init=n_init)
    # profile CSV: final density of each species along x (Mode C)
    cols = [list(gB.grid.centers)] + [list(gC.profile_final(s.name)) for s in species]
    write_profile_csv(out("gas_transport_profile.csv"),
                      ["x_m"] + [f"n_{s.name}_modeC_1m3" for s in species], cols)
    rmap, maps, gm2 = gas_exposure_map_2d()
    map_rows = []
    for i, rr in enumerate(rmap):
        row = {"r_m": f"{rr:.6e}"}
        for st, arr in maps.items():
            row[f"dose_flux_{st}_m2_s"] = f"{arr[i]:.6e}"
        map_rows.append(row)
    write_rows_csv(out("gas_transport_2d_map.csv"), map_rows)
    gas_metrics = []
    pumped = gB  # for pump-removed fraction proxy
    for s in species:
        gas_metrics.append({
            "species": s.name,
            "max_density_m3": gB.max_density(s.name),
            "sample_region_density_modeB_m3": gB.sample_region_density(s.name),
            "residual_mode_D_density_m3": gC.sample_region_density(s.name),
            "cryobaffle_capture": s.cryobaffle_capture,
            "gas_transport_stability_status": "STABLE_MODEL_ONLY",
            "evidence_class": EVID, "measured_in_this_system": "false"})
    write_rows_csv(out("gas_transport_metrics.csv"), gas_metrics)
    log("gas done")

    # ---------- surface coverage (1D B/C + 2D radial map) ----------
    gasB_sample = {s.name: gB.sample_region_density(s.name) for s in species}
    covB, _, cspecs = surface_coverage_1d(gasB_sample, T_surface_K=max(t1.nv_layer_temperature_K(), 1.0),
                                          t_end=1.0, mode="B")
    thetaB = {k: float(v[-1]) for k, v in covB.items()}
    covC, tcov, _ = surface_coverage_1d({}, T_surface_K=cfg.fridge.T_fridge_K,
                                        t_end=2.0, mode="C", theta0=thetaB, purge_1_s=5.0)
    # profile CSV: coverage vs time during Mode C decay
    scov_cols = [list(tcov)] + [list(covC[s.name]) for s in cspecs]
    write_profile_csv(out("surface_coverage_profile.csv"),
                      ["t_s"] + [f"theta_{s.name}_modeC" for s in cspecs], scov_cols)
    # 2D radial coverage map using the 2D thermal surface temperature + radial gas flux
    from .surface_coverage import surface_coverage_2d
    T_surf_radial = t2.T_peak.values[:, 0]  # surface row over r (peak during B)
    gas_radial_flux = {s.name: kinetic_flux(gasB_sample.get(s.name, 0.0), 300.0, s.mass_amu)
                       * np.exp(-2.0 * (t2.grid.r_centers ** 2) / (cfg.laser.spot_radius_m * 4) ** 2)
                       for s in cspecs}
    cov2d, _ = surface_coverage_2d(gas_radial_flux, np.maximum(T_surf_radial, 1.0),
                                   t_end=0.5, n_r=len(T_surf_radial))
    s2d_rows = []
    for i, rr in enumerate(t2.grid.r_centers):
        row = {"r_m": f"{rr:.6e}"}
        for s in cspecs:
            row[f"theta_{s.name}"] = f"{cov2d[s.name][i]:.6e}"
        s2d_rows.append(row)
    write_rows_csv(out("surface_coverage_2d_map.csv"), s2d_rows)
    surf_metrics = []
    for s in cspecs:
        surf_metrics.append({
            "species": s.name, "max_theta_modeB": thetaB.get(s.name, 0.0),
            "residual_theta_mode_D": float(covC[s.name][-1]),
            "contamination_risk_status": "FORECAST_ONLY",
            "evidence_class": EVID, "measured_in_this_system": "false"})
    write_rows_csv(out("surface_coverage_metrics.csv"), surf_metrics)
    log("surface done")

    # ---------- microwave / radiation / vibration ----------
    mw_diss, mw_m = microwave_path_1d(input_power_W=1.0e-6, b1_per_sqrtW=1.0e-4)
    write_rows_csv(out("microwave_heating_profile.csv"),
                   [{"stage": k, "dissipated_power_W": f"{v:.6e}"} for k, v in mw_diss.items()])
    write_rows_csv(out("microwave_heating_metrics.csv"),
                   [{"metric": k, "value": v, "evidence_class": EVID,
                     "measured_in_this_system": "false"} for k, v in mw_m.items()])
    rad_paths_list, rad_m = radiation_paths()
    for p in rad_paths_list:
        p["evidence_class"] = EVID; p["measured_in_this_system"] = "false"
    write_rows_csv(out("radiation_leakage_paths.csv"), rad_paths_list)
    write_rows_csv(out("radiation_leakage_metrics.csv"),
                   [{"metric": k, "value": v, "evidence_class": EVID,
                     "measured_in_this_system": "false"} for k, v in rad_m.items()])
    vib_prof, vib_m = vibration_transfer()
    for p in vib_prof:
        p["evidence_class"] = EVID; p["measured_in_this_system"] = "false"
    write_rows_csv(out("vibration_transfer_profile.csv"), vib_prof)
    write_rows_csv(out("vibration_transfer_metrics.csv"),
                   [{"metric": k, "value": v, "evidence_class": EVID,
                     "measured_in_this_system": "false"} for k, v in vib_m.items()])
    log("paths done")

    # ---------- coupled B->C->D ----------
    cm, cstate, _ = run_coupled(cfg)
    write_rows_csv(out("coupled_mode_recovery_metrics.csv"),
                   [{"metric": k, "value": v, "evidence_class": EVID,
                     "measured_in_this_system": "false"} for k, v in cm.items()])
    write_json(out("coupled_mode_state_summary.json"),
               {"metrics": cm, "state": cstate, "evidence_class": EVID,
                "measured_in_this_system": False})
    log("coupled done")

    # ---------- verification ----------
    mesh_rows, stab_rows, ver_rows, vsumm = run_verification(cfg)
    write_rows_csv(out("mesh_convergence_summary.csv"), mesh_rows)
    for r in stab_rows:
        r["measured_in_this_system"] = "false"
    write_rows_csv(out("numerical_stability_summary.csv"), stab_rows)
    for r in ver_rows:
        r["measured_in_this_system"] = "false"
    write_rows_csv(out("multiphysics_verification_summary.csv"), ver_rows)
    log("verification done")

    # ---------- Monte Carlo ----------
    mc_dists, mc_summary = run_monte_carlo(n_samples=mc_samples)
    log("monte carlo done")

    # ---------- lumped vs non-lumped comparator ----------
    # Legacy lumped estimate: single-node steady-state from absorbed power and a
    # lumped conductance (comparator-only; NOT a gate authority).
    P_abs = cfg.laser.absorbed_average_power_W
    G_lumped = 1.0e-5  # W/K lumped conductance (legacy assumption)
    T_lumped_rise = P_abs / G_lumped
    lumped_rows = [
        {"quantity": "Mode_B_peak_T_K", "lumped_model": f"{cfg.fridge.T_fridge_K + T_lumped_rise:.3e}",
         "nonlumped_1d_model": f"{thermal_metrics['max_T_1d_K']:.3e}",
         "nonlumped_2d_model": f"{thermal_metrics['max_T_2d_K']:.3e}",
         "role": "lumped=comparator_only;nonlumped=gate_authority", "evidence_class": EVID},
        {"quantity": "NV_layer_T_K", "lumped_model": f"{cfg.fridge.T_fridge_K + T_lumped_rise:.3e}",
         "nonlumped_1d_model": f"{thermal_metrics['NV_layer_T_1d_K']:.3e}",
         "nonlumped_2d_model": f"{thermal_metrics['NV_layer_max_2d_K']:.3e}",
         "role": "lumped=comparator_only;nonlumped=gate_authority", "evidence_class": EVID},
    ]
    write_rows_csv(out("lumped_vs_nonlumped_comparison.csv"), lumped_rows)
    # fidelity comparison: 1D vs 2D
    fidelity_rows = [
        {"metric": "NV_layer_T_K", "model_1d": f"{thermal_metrics['NV_layer_T_1d_K']:.3e}",
         "model_2d": f"{thermal_metrics['NV_layer_max_2d_K']:.3e}",
         "note": "2D adds radial spreading -> generally lower peak than 1D slab",
         "evidence_class": EVID},
        {"metric": "max_T_K", "model_1d": f"{thermal_metrics['max_T_1d_K']:.3e}",
         "model_2d": f"{thermal_metrics['max_T_2d_K']:.3e}",
         "note": "fidelity_comparison MODEL_ONLY", "evidence_class": EVID},
    ]
    write_rows_csv(out("fidelity_comparison.csv"), fidelity_rows)
    log("comparators done")

    # ---------- top-level summary JSON ----------
    summary = {
        "layer": "qta_multiphysics",
        "status": "MODEL_ONLY;FORECAST_ONLY;ZERO_PASS;PRE_EXPERIMENTAL",
        "implemented_backends": {"thermal_1d": "canonical/default non-lumped",
                                 "thermal_2d_axisymmetric": "serious spatial refinement",
                                 "3d": future_3d.STATUS},
        "thermal_metrics": thermal_metrics,
        "coupled_metrics": cm,
        "verification": {"mesh_1d": vsumm["mesh_1d"], "mesh_2d": vsumm["mesh_2d"],
                         "kapitza": vsumm["kapitza"], "source_1d": vsumm["source_1d"],
                         "source_2d": vsumm["source_2d"], "reduction": vsumm["reduction"],
                         "coupling": vsumm["coupling"]},
        "monte_carlo": {"distributions": mc_dists, "summary": mc_summary},
        "future_3d": future_3d.status_report(),
        "measured_in_this_system": False,
    }
    write_json(out("multiphysics_summary.json"), summary)
    log("summary written")

    gate_specs = build_gate_specs(cm, vsumm, mc_summary, future_3d.STATUS)
    return summary, gate_specs
