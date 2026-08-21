"""Coupled Mode B -> Mode C -> Mode D solver.

Chains the distributed sub-models with real state hand-off:

  Mode B (process): laser/source ON (averaged power), gas source ON, surface
    coverage builds on a warm surface. -> peak temperatures, contamination flux.
  Mode C (recovery): sources OFF (thermal initialized from Mode B final field),
    gas purge from Mode B residual, surface decay on a cold surface.
    -> recool time, gas cleanup, coverage decay.
  Mode D (sense): read the final Mode C distributed state and evaluate readiness
    (NV-layer T, post-pulse drift, residual gas, residual coverage, and the
    microwave/radiation/vibration readiness surrogates).

MODEL-ONLY / FORECAST-ONLY. Mode B and Mode D never run simultaneously.
"""
from __future__ import annotations
import dataclasses

import numpy as np

from .config import MultiphysicsConfig
from .thermal_1d import solve_thermal_1d
from .gas_transport_1d import solve_gas_transport_1d, default_species
from .surface_coverage import surface_coverage_1d, default_coverage_specs
from .microwave_heating_1d import microwave_path_1d
from .radiation_paths import radiation_paths
from .vibration_transfer import vibration_transfer


#: A solve that did not converge carries no scientific authority. Any consumer
#: below -- Mode-C readiness, Mode-D start, eligibility forecasts, derived
#: metrics -- must deny authority rather than read the numbers anyway.
SOLVER_OK = "ok"


class SolverFailure(RuntimeError):
    """A numerical solve did not converge; downstream authority is denied."""


def require_converged(result, what: str):
    """Fail closed on a non-converged solve.

    solver_status used to be reported alongside the metrics as a passive
    string while ready_terms was computed from the same result regardless, so
    a failed BDF integration could still produce FORECAST_READY_IF_MEASURED.
    Readiness is now unreachable without convergence.
    """
    status = getattr(result, "solver_status", None)
    if status != SOLVER_OK:
        raise SolverFailure(
            f"{what}: solver_status={status!r} (expected {SOLVER_OK!r}); "
            "readiness, eligibility and derived metrics are denied")
    return result


def run_coupled(cfg: MultiphysicsConfig):
    cfg.validate()
    th = cfg.solver.mode_d_temp_threshold_K

    # ---- Mode B: process (source ON) ----
    tB = require_converged(
        solve_thermal_1d(cfg, source_mode="averaged", n_eval=60),
        "Mode B thermal solve")
    B_peak_T = tB.hotspot_temperature_K()
    B_peak_NV = tB.nv_layer_temperature_K()
    B_surf_T = float(tB.T[0, :].max())  # front-surface peak

    gasB = solve_gas_transport_1d(mode="B", t_end=2.0)
    species = default_species()
    gasB_sample = {s.name: gasB.sample_region_density(s.name) for s in species}
    B_contam_flux = max(gasB_sample.get("CH4", 0.0), gasB_sample.get("H2", 0.0))

    covB, _, _ = surface_coverage_1d(gasB_sample, T_surface_K=max(B_surf_T, 1.0),
                                     t_end=1.0, mode="B")
    thetaB = {k: float(v[-1]) for k, v in covB.items()}

    # ---- Mode C: recovery (source OFF, init from Mode B) ----
    # Mode C is the isolation/recovery mode: the processing source is OFF by
    # definition, which is expressed by zeroing the absorbed laser fraction on a
    # config clone. A first solve with the laser still absorbing used to run
    # here and have its result immediately overwritten; it was dead (it mutated
    # neither cfg nor T_init and consumed no RNG) but it also computed a Mode C
    # that violates the mode definition, which is not a state this path should
    # ever construct. Removed.
    cfg_off = dataclasses.replace(
        cfg, laser=dataclasses.replace(cfg.laser, absorbed_fraction=0.0))
    assert cfg_off.laser.absorbed_fraction == 0.0, \
        "Mode C must run with the processing source OFF"
    tC = require_converged(
        solve_thermal_1d(cfg_off, source_mode="averaged", n_eval=120,
                         T_init=tB.T[:, -1], t_end=cfg.solver.recovery_window_s),
        "Mode C recovery solve")
    C_recool = tC.recool_time_s(th)
    C_drift = tC.post_pulse_drift_K()

    n_init = {s.name: gasB.profile_final(s.name) for s in species}
    gasC = solve_gas_transport_1d(mode="C", t_end=2.0, n_init=n_init)
    gasC_sample = {s.name: gasC.sample_region_density(s.name) for s in species}

    covC, tcov, _ = surface_coverage_1d({}, T_surface_K=cfg.fridge.T_fridge_K,
                                        t_end=2.0, mode="C", theta0=thetaB, purge_1_s=5.0)
    thetaC = {k: float(v[-1]) for k, v in covC.items()}
    # surface decay time: first time total coverage falls below 1e-6
    tot = np.sum([covC[k] for k in covC], axis=0)
    decay_idx = np.argmax(tot <= 1e-6) if np.any(tot <= 1e-6) else -1
    C_surf_decay = float(tcov[decay_idx]) if decay_idx >= 0 else float("inf")

    # ---- Mode D readiness surrogates ----
    mw_diss, mw_m = microwave_path_1d(input_power_W=1.0e-6)
    rad_paths, rad_m = radiation_paths()
    vib_prof, vib_m = vibration_transfer()

    D_T_NV = tC.nv_layer_temperature_final_K()
    D_res_CH4 = gasC_sample.get("CH4", 0.0)
    D_res_H2 = gasC_sample.get("H2", 0.0)
    D_res_theta = max(thetaC.values()) if thetaC else 0.0

    # readiness is a model-only forecast; never a PASS. Both thermal solves
    # are known converged here -- require_converged() raised otherwise -- so
    # readiness cannot be derived from a failed integration.
    ready_terms = {
        "nv_temperature_ok": D_T_NV <= th,
        "drift_ok": C_drift <= 0.5 * th,
        "gas_residual_ok": (D_res_CH4 < 1e12 and D_res_H2 < 1e12),
        "coverage_ok": D_res_theta < 1e-3,
        "vibration_ok": vib_m["Mode_D_vibration_ready_if_measured"],
    }
    readiness = "FORECAST_READY_IF_MEASURED" if all(ready_terms.values()) else "FORECAST_NOT_READY"

    # limiting recovery process
    limiters = {"thermal_recool_s": C_recool if np.isfinite(C_recool) else 1e9,
                "surface_decay_s": C_surf_decay if np.isfinite(C_surf_decay) else 1e9}
    limiting = max(limiters, key=limiters.get)

    metrics = {
        "Mode_B_peak_T_K": B_peak_T,
        "Mode_B_peak_surface_T_K": B_surf_T,
        "Mode_B_peak_NV_layer_T_K": B_peak_NV,
        "Mode_B_peak_contamination_flux_proxy_m3": B_contam_flux,
        "Mode_C_recool_time_s": C_recool,
        "Mode_C_cleanup_residual_CH4_m3": gasC_sample.get("CH4", 0.0),
        "Mode_C_surface_decay_time_s": C_surf_decay,
        "Mode_D_T_NV_layer_K": D_T_NV,
        "Mode_D_residual_CH4_density_m3": D_res_CH4,
        "Mode_D_residual_H2_density_m3": D_res_H2,
        "Mode_D_residual_surface_theta": D_res_theta,
        "Mode_D_readiness_status": readiness,
        "limiting_recovery_process": limiting,
        "thermal_solver_status_B": tB.solver_status,
        "thermal_solver_status_C": tC.solver_status,
    }
    state = {
        "thetaB": thetaB, "thetaC": thetaC,
        "gasB_sample": gasB_sample, "gasC_sample": gasC_sample,
        "ready_terms": ready_terms,
        "microwave": mw_m, "radiation": rad_m, "vibration": vib_m,
    }
    return metrics, state, dict(thermalB=tB, thermalC=tC, gasB=gasB, gasC=gasC)
