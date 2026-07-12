"""Falsification / block conditions for the coupled 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Falsification is a useful result and is never hidden. Every condition uses an
EXISTING canonical bound (or is honestly NOT_IMPLEMENTED when no canonical
bound exists); "falsified_in_model = false" means only that the deterministic
model run stayed inside the stated bound -- it is NOT validation, NOT a PASS,
and NOT a hardware claim. Deterministic.
"""
from __future__ import annotations

from .energy_accounting_3d import ENERGY_CLOSURE_TOL
from .heat_switch_3d import switch_leak_forecast_W, A1_LEAK_BOUND_W
from .state_machine_3d import device_state

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def _cond(name, bound, provenance, value, units, falsified, status, note=""):
    return {"condition": name, "bound": bound, "bound_provenance": provenance,
            "model_value": value, "units": units,
            "falsified_in_model": ("" if falsified is None
                                   else str(bool(falsified)).lower()),
            "status": status, "note": note, "label": LABEL}


def falsification_report(cfg, seq, vrep, species_rows) -> dict:
    thr = float(seq.threshold_K)
    tsC = seq.tC.probe_timeseries_K()
    probe_end = float(tsC[-1])
    conds = []

    # thermal recovery before Mode D
    conds.append(_cond(
        "thermal_recovery_before_mode_D", f"NV probe <= {thr:.3e}",
        "SolverConfig.mode_d_temp_threshold_K (canonical)",
        f"{probe_end:.9e}", "K", not seq.mode_c_ready,
        "DERIVED_CHECK" if seq.mode_c_ready else "CONDITIONAL",
        seq.status))

    # per-phase energy closure
    for tag, res in (("MODE_B", seq.tB), ("MODE_C", seq.tC)):
        rel = abs(res.energy_residual())
        conds.append(_cond(
            f"energy_residual_{tag}", f"|rel| <= {ENERGY_CLOSURE_TOL:.2e}",
            "shared ENERGY_CLOSURE_TOL (energy_accounting_3d)",
            f"{rel:.3e}", "-", rel > ENERGY_CLOSURE_TOL,
            "DERIVED_CHECK" if rel <= ENERGY_CLOSURE_TOL else "CONDITIONAL"))

    # reduction mismatches (from the verification report)
    for c in vrep["checks"]:
        if c["check"].startswith("reduction_"):
            rel = abs(float(c["rel_error"]))
            tol = float(c["tolerance"])
            conds.append(_cond(
                f"{c['check']}_mismatch", f"|rel| <= {tol:.2e}",
                "declared reduction tolerance (reduction_checks_3d)",
                f"{rel:.4e}", "-", rel > tol, c["status"]))

    # forbidden species in Mode D (structural software enforcement)
    conds.append(_cond(
        "forbidden_species_in_mode_D", "no C13_CH4 live; H2 never active",
        "canonical species policy (mode_sequence_3d validator)",
        "enforced (any violation raises ValueError)", "-", False,
        "DERIVED_CHECK", "software enforcement; hardware interlocks IL-01/02/14"))

    # residual C-13 coverage before Mode D (bound NOT_IMPLEMENTED)
    res_rows = [r for r in species_rows
                if r["phase"] == "MODE_C" and r["species"] == "C13_CH4"]
    resC13 = res_rows[0]["coverage_end"] if res_rows else ""
    conds.append(_cond(
        "residual_C13_coverage_before_mode_D", "NOT_IMPLEMENTED",
        "no canonical residual-coverage bound is parameterized",
        resC13, "theta (fraction)", None, "NOT_IMPLEMENTED",
        "worst-case purge forecast reported; bound requires a canonical spec"))

    # heat-switch leak in the OPEN (protection) states, gate A1 bound
    for mode in ("MODE_B", "MODE_D"):
        st = device_state(mode)
        leak = switch_leak_forecast_W(st.heat_switch_state,
                                      cfg.fridge.T_fridge_K)
        conds.append(_cond(
            f"heat_switch_leak_{mode}",
            f"P_leak < {A1_LEAK_BOUND_W:.1e} (1% of P_cool_MC)",
            "gate A1 threshold; DESIGN_SPECIFIED conductances",
            f"{leak['leak_forecast_W']:.3e}", "W",
            not leak["within_A1_bound_in_model"],
            "DERIVED_CHECK" if leak["within_A1_bound_in_model"]
            else "CONDITIONAL",
            f"switch state {leak['state']} (design values, not measured)"))

    # hotspot damage bound (no canonical threshold exists)
    hot = float(seq.tB.T_xyz(-1).max())
    conds.append(_cond(
        "laser_hotspot_damage_bound", "NOT_IMPLEMENTED",
        "no canonical hotspot damage/graphitization threshold is "
        "parameterized in the repository",
        f"{hot:.9e}", "K", None, "NOT_IMPLEMENTED",
        "peak Mode-B temperature reported; bound requires a canonical spec"))

    # solver stability
    unstable = [t for t, r in (("MODE_B", seq.tB), ("MODE_C", seq.tC))
                if r.solver_status != "ok"]
    conds.append(_cond(
        "solver_stability", "solver_status == ok for all phases",
        "BDF integrator status", ";".join(unstable) or "ok,ok", "-",
        bool(unstable), "DERIVED_CHECK" if not unstable else "CONDITIONAL"))

    n_fals = sum(1 for c in conds if c["falsified_in_model"] == "true")
    return {
        "conditions": conds,
        "n_conditions": len(conds),
        "n_falsified_in_model": n_fals,
        "n_not_implemented_bounds": sum(1 for c in conds
                                        if c["status"] == "NOT_IMPLEMENTED"),
        "meaning": "falsified_in_model=false means the deterministic model "
                   "run stayed inside the stated canonical bound; this is a "
                   "numerical forecast statement only -- never validation, "
                   "never PASS, never a hardware claim",
        "label": LABEL,
    }
