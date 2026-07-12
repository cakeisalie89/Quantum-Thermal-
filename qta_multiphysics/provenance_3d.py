"""Provenance and assumption records for the coupled 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Machine-readable ledgers: every model in the coupled layer with its honesty
status, parameters, limitations, outputs, and covering tests; and every
parameter with its value, units, and provenance class (literature assumption,
design assumption, derived value, placeholder, or NOT_IMPLEMENTED). Assumed
parameters are never presented as measurements. Deterministic.
"""
from __future__ import annotations

from .heat_switch_3d import HeatSwitchSpec, T_4K_STAGE_K, A1_LEAK_BOUND_W
from .species_accounting_3d import (P_HE_DOSE_PA, P_C13_WORK_PA,
                                    PURGE_WINDOW_S, PURGE_RATE_1_S,
                                    DOSE_WINDOW_S, THETA0_WORST_CASE)
from .convergence_3d import TOL_MESH_REL, TOL_TIME_REL

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def assumptions_record(cfg) -> dict:
    sw = HeatSwitchSpec()

    def p(value, units, provenance, note=""):
        return {"value": value, "units": units, "provenance": provenance,
                "note": note}

    return {
        "parameters": {
            "laser.absorbed_fraction": p(cfg.laser.absorbed_fraction, "-",
                                         "ASSUMED"),
            "laser.spot_radius_m": p(cfg.laser.spot_radius_m, "m", "DESIGN"),
            "laser.absorption_coeff_1_m": p(cfg.laser.absorption_coeff_1_m,
                                            "1/m", "ASSUMED (Beer-Lambert)"),
            "fridge.T_fridge_K": p(cfg.fridge.T_fridge_K, "K", "DESIGN"),
            "fridge.kapitza_coeff_W_m2_K4": p(cfg.fridge.kapitza_coeff_W_m2_K4,
                                              "W/m^2/K^4",
                                              "ASSUMED (radiative Kapitza form)"),
            "heat_switch.G_open_W_K": p(sw.G_open_W_K, "W/K",
                                        "DESIGN_SPECIFIED (gate A1)",
                                        "design target; not measured"),
            "heat_switch.G_closed_W_K": p(sw.G_closed_W_K, "W/K",
                                          "DESIGN_SPECIFIED (validation plan)"),
            "heat_switch.T_4K_stage_K": p(T_4K_STAGE_K, "K",
                                          "DESIGN (canonical stage chain)"),
            "heat_switch.A1_leak_bound_W": p(A1_LEAK_BOUND_W, "W",
                                             "DERIVED (1% of P_cool_MC)"),
            "species.P_he_dose_Pa": p(P_HE_DOSE_PA, "Pa",
                                      "DESIGN (canonical dose, D9)"),
            "species.P_c13_work_Pa": p(P_C13_WORK_PA, "Pa",
                                       "DESIGN (Mode-B working pressure)"),
            "species.purge_window_s": p(PURGE_WINDOW_S, "s",
                                        "DESIGN (canonical 1D-layer window)"),
            "species.purge_rate_1_s": p(PURGE_RATE_1_S, "1/s",
                                        "ASSUMED (canonical 1D-layer value)"),
            "species.dose_window_s": p(DOSE_WINDOW_S, "s",
                                       "DESIGN (canonical 1D-layer window)"),
            "species.theta0_worst_case": p(THETA0_WORST_CASE, "-",
                                           "PLACEHOLDER (stated worst case)"),
            "convergence.tol_mesh_rel": p(TOL_MESH_REL, "-",
                                          "DECLARED numerical tolerance"),
            "convergence.tol_time_rel": p(TOL_TIME_REL, "-",
                                          "DECLARED numerical tolerance"),
            "support_conductance": p("NOT_IMPLEMENTED", "W/K",
                                     "NOT_IMPLEMENTED",
                                     "no canonical numbers; none invented"),
            "optical_collection_load_W": p("NOT_IMPLEMENTED", "W",
                                           "NOT_IMPLEMENTED"),
        },
        "rule": "assumed parameters are model inputs, never measurements",
        "label": LABEL,
    }


def provenance_record() -> dict:
    def m(status, params, limits, outputs, tests):
        return {"status": status, "parameters": params, "limitations": limits,
                "outputs": outputs, "tests": tests}

    return {
        "models": {
            "thermal_3d_transient": m(
                "IMPLEMENTED",
                "graded FV mesh; diamond k(T), cp(T); BDF; Kapitza back BC; "
                "bounded auxiliary channels (guarded kwargs)",
                "homogeneous diamond; reduced CI resolution; front/lateral "
                "insulated except the bounded radiative front channel",
                ["thermal_3d_probe_timeseries.csv", "thermal_3d_hotspots.csv",
                 "thermal_3d_energy_accounting.csv"],
                ["tests/test_thermal_3d.py", "tests/test_coupled_3d.py"]),
            "laser_source_3d": m(
                "IMPLEMENTED",
                "exactly-conservative cell-integrated Gaussian x Beer-Lambert",
                "time-averaged deposition approximation in averaged mode; no "
                "electron-phonon nonequilibrium model",
                ["laser_3d_deposition_summary.json"],
                ["tests/test_thermal_3d.py"]),
            "state_machine_3d": m(
                "IMPLEMENTED",
                "mode -> shutter/baffle/laser/MW/switch/species states",
                "DESIGN_SPECIFIED state logic; no timing dynamics",
                ["coupling_ledger_3d.json"], ["tests/test_coupled_3d.py"]),
            "sources_3d.microwave": m(
                "REDUCED_ORDER",
                "canonical line-model NV-region dissipation, uniform over "
                "NV-layer cells",
                "3D field/SAR map NOT_IMPLEMENTED",
                ["coupling_ledger_3d.json"], ["tests/test_coupled_3d.py"]),
            "sources_3d.radiative_front": m(
                "REDUCED_ORDER",
                "canonical stage-chain load at the mode's shutter/baffle "
                "state, uniform front-face flux",
                "3D view factors NOT_IMPLEMENTED",
                ["coupling_ledger_3d.json"], ["tests/test_coupled_3d.py"]),
            "sources_3d.vibration": m(
                "BOUNDED_FORECAST",
                "canonical transfer-chain dissipated power (ledger scalar)",
                "no spatial map; 3D modal analysis NOT_IMPLEMENTED",
                ["coupling_ledger_3d.json"], ["tests/test_coupled_3d.py"]),
            "heat_switch_3d": m(
                "REDUCED_ORDER",
                "DESIGN_SPECIFIED G_open/G_closed; lumped MC<->4K path",
                "stage-level only (not resolved in the 3D field); gas-gap "
                "switch NOT_IMPLEMENTED",
                ["falsification_report_3d.json"], ["tests/test_coupled_3d.py"]),
            "species_accounting_3d": m(
                "REDUCED_ORDER",
                "canonical coverage ODE, purge and dose windows; worst-case "
                "theta0",
                "C-13 arrival footprint NOT_IMPLEMENTED; residual bound "
                "NOT_IMPLEMENTED",
                ["species_accounting_3d.csv"], ["tests/test_coupled_3d.py"]),
            "falsification_3d": m(
                "IMPLEMENTED",
                "existing canonical bounds only",
                "damage and residual-coverage bounds NOT_IMPLEMENTED",
                ["falsification_report_3d.json"], ["tests/test_coupled_3d.py"]),
            "convergence_3d": m(
                "IMPLEMENTED",
                "one mesh refinement + tightened rtol",
                "lightweight verification only",
                ["convergence_report_3d.json"], ["tests/test_coupled_3d.py"]),
            "sensitivity_3d": m(
                "IMPLEMENTED",
                "one-at-a-time +10% on four uncertain inputs (CI mesh)",
                "model sensitivity only; local, one-sided",
                ["sensitivity_ranking_3d.csv"], ["tests/test_coupled_3d.py"]),
            "nv_eligibility_3d": m(
                "IMPLEMENTED",
                "existing canonical thresholds; state machine",
                "forecast eligibility only; never measured NV performance",
                ["nv_eligibility_3d.json"], ["tests/test_coupled_3d.py"]),
            "support_conduction": m(
                "SCAFFOLD", "registry entries only",
                "no canonical conductance numbers; none invented",
                ["cryo_stack_3d_budget.csv"], []),
            "optical_collection_load": m(
                "NOT_IMPLEMENTED", "-",
                "no repository parameterization", [], []),
        },
        "status_vocabulary": ["IMPLEMENTED", "REDUCED_ORDER",
                              "BOUNDED_FORECAST", "SCAFFOLD",
                              "NOT_IMPLEMENTED"],
        "label": LABEL,
    }
