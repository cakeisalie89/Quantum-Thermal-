"""Monte Carlo uncertainty propagation for the non-lumped multiphysics layer.

Each sample perturbs the registered parameters and evaluates:
  * the 1D thermal PDE (peak NV temperature, max T, recool time) -- the stiff,
    physically central model is solved per sample;
  * reduced algebraic surrogates for gas residual and surface-coverage residual,
    parameterised by the sampled transport/sticking/desorption coefficients
    (these sub-models are reduced-order in the first place, so an algebraic
    steady-state form is used for tractability and is documented as such).

Reports distributions plus a PDE-stability failure count and a mesh-convergence
failure count. MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import math
import dataclasses
import numpy as np

from .config import default_config
from .thermal_1d import solve_thermal_1d
from .units import K_B


def _pct(a, p):
    return float(np.percentile(a, p)) if len(a) else float("nan")


def _dist(name, arr):
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    return {"metric": name, "n": int(a.size),
            "mean": float(np.mean(a)) if a.size else float("nan"),
            "std": float(np.std(a)) if a.size else float("nan"),
            "p05": _pct(a, 5), "p50": _pct(a, 50), "p95": _pct(a, 95)}


def run_monte_carlo(n_samples=120, seed=12345, mesh_check_fraction=0.1):
    rng = np.random.default_rng(seed)
    base = default_config()

    peak_NV, max_T, recool, gas_res_CH4, gas_res_H2, surf_res = [], [], [], [], [], []
    cleanup_t, readiness_flag = [], []
    pde_fail = 0
    mesh_fail = 0
    n_mesh_checked = 0

    th = base.solver.mode_d_temp_threshold_K
    Tf = base.fridge.T_fridge_K

    for i in range(n_samples):
        def lf(center, frac):  # lognormal-ish multiplicative jitter
            return center * float(np.exp(rng.normal(0.0, frac)))

        laser = dataclasses.replace(
            base.laser,
            pulse_energy_J=lf(base.laser.pulse_energy_J, 0.25),
            pulse_duration_s=lf(base.laser.pulse_duration_s, 0.20),
            repetition_rate_Hz=lf(base.laser.repetition_rate_Hz, 0.15),
            spot_radius_m=lf(base.laser.spot_radius_m, 0.15),
            absorbed_fraction=float(np.clip(lf(base.laser.absorbed_fraction, 0.30), 1e-3, 1.0)),
            absorption_coeff_1_m=lf(base.laser.absorption_coeff_1_m, 0.30),
        )
        material = dataclasses.replace(
            base.material,
            k_ref_W_mK=lf(base.material.k_ref_W_mK, 0.30),
            k_exponent=float(np.clip(base.material.k_exponent + rng.normal(0, 0.2), 1.5, 3.5)),
        )
        fridge = dataclasses.replace(
            base.fridge,
            kapitza_coeff_W_m2_K4=lf(base.fridge.kapitza_coeff_W_m2_K4, 0.40),
        )
        geometry = dataclasses.replace(
            base.geometry,
            front_velocity_m_s=max(0.0, base.geometry.front_velocity_m_s),
        )
        solver = dataclasses.replace(base.solver, n_cells_1d=80)
        try:
            cfg = dataclasses.replace(base, laser=laser, material=material,
                                      fridge=fridge, geometry=geometry, solver=solver).validate()
        except Exception:
            pde_fail += 1
            continue

        try:
            rB = solve_thermal_1d(cfg, source_mode="averaged", n_cells=80, n_eval=25)
            if rB.solver_status != "ok" or not rB.final_profile().is_finite():
                pde_fail += 1
                continue
            peak_NV.append(rB.nv_layer_temperature_K())
            max_T.append(rB.hotspot_temperature_K())
            # recool: source off
            cfg_off = dataclasses.replace(cfg, laser=dataclasses.replace(laser, absorbed_fraction=0.0))
            rC = solve_thermal_1d(cfg_off, source_mode="averaged", n_cells=80, n_eval=40,
                                  T_init=rB.T[:, -1], t_end=base.solver.recovery_window_s)
            rc_t = rC.recool_time_s(th)
            recool.append(rc_t)
        except Exception:
            pde_fail += 1
            continue

        # ---- algebraic gas residual surrogate (sampled coefficients) ----
        # Mode B steady state n_B ~ source / S_B ; Mode C purge n_C = n_B * exp(-S_C * t)
        src_CH4 = lf(1.0e18, 0.3); S_CH4 = max(lf(2.0, 0.3), 1e-3)
        purge = max(lf(20.0, 0.3), 1.0)
        n_B_CH4 = src_CH4 / S_CH4
        t_purge = 2.0
        n_C_CH4 = n_B_CH4 * math.exp(-S_CH4 * purge * t_purge)
        gas_res_CH4.append(n_C_CH4)
        src_H2 = lf(2.0e17, 0.3); S_H2 = max(lf(0.2, 0.3), 1e-3)
        n_C_H2 = (src_H2 / S_H2) * math.exp(-S_H2 * purge * t_purge)
        gas_res_H2.append(n_C_H2)
        cleanup_t.append(math.log(max(n_B_CH4, 1.0)) / (S_CH4 * purge))

        # ---- algebraic surface residual surrogate ----
        stick = float(np.clip(lf(0.5, 0.2), 1e-3, 1.0))
        E_des = lf(0.16 * 1.602e-19, 0.15)
        nu = 1e13
        # equilibrium coverage in Mode B at warm surface, then cold-purge decay
        flux_B = 0.25 * n_B_CH4 * 400.0  # rough thermal flux
        ads = stick * (flux_B / 1e19)
        des_B = nu * math.exp(-E_des / (K_B * max(rB.nv_layer_temperature_K(), 1.0)))
        theta_B = ads / (ads + des_B + 5.0)
        cryotrap = max(lf(5.0, 0.3), 0.1)
        theta_C = theta_B * math.exp(-(cryotrap + purge) * 2.0)
        surf_res.append(theta_C)

        ready = (rC.nv_layer_temperature_final_K() <= th and n_C_CH4 < 1e12 and theta_C < 1e-3)
        readiness_flag.append(1.0 if ready else 0.0)

        # ---- occasional mesh-convergence check ----
        if rng.random() < mesh_check_fraction:
            n_mesh_checked += 1
            try:
                a = solve_thermal_1d(cfg, source_mode="averaged", n_cells=100, n_eval=12).nv_layer_temperature_K()
                b = solve_thermal_1d(cfg, source_mode="averaged", n_cells=200, n_eval=12).nv_layer_temperature_K()
                if abs(b - a) / max(abs(b), 1e-12) > 0.20:
                    mesh_fail += 1
            except Exception:
                mesh_fail += 1

    distributions = [
        _dist("Mode_B_peak_NV_layer_T_K", peak_NV),
        _dist("Mode_B_max_T_K", max_T),
        _dist("Mode_C_recool_time_s", recool),
        _dist("Mode_C_cleanup_time_s", cleanup_t),
        _dist("Mode_D_residual_CH4_density_m3", gas_res_CH4),
        _dist("Mode_D_residual_H2_density_m3", gas_res_H2),
        _dist("Mode_D_residual_surface_theta", surf_res),
    ]
    readiness_forecast_fraction = float(np.mean(readiness_flag)) if readiness_flag else 0.0
    summary = {
        "n_samples": n_samples,
        "n_evaluated": len(peak_NV),
        "pde_stability_failure_count": pde_fail,
        "mesh_convergence_checked": n_mesh_checked,
        "mesh_convergence_failure_count": mesh_fail,
        "Mode_D_forecast_ready_fraction": readiness_forecast_fraction,
        "note": "FORECAST_ONLY; ready_fraction is a model forecast, NOT a validated pass rate.",
    }
    return distributions, summary
