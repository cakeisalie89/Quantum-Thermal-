"""NV sensing eligibility forecast for the coupled 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Combines the 3D mode-sequence recovery state with the existing canonical
bounds (Mode-D temperature threshold; vibration Mode-D amplitude threshold
from the canonical transfer chain; species policy; microwave/laser device
states) into an eligibility FORECAST whose statuses are only
CONDITIONAL / BLOCKED / UNKNOWN -- never PASS, never measured performance.
Deterministic.
"""
from __future__ import annotations

from .vibration_transfer import vibration_transfer
from .state_machine_3d import device_state

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def nv_eligibility_forecast(cfg, seq, species_rows) -> dict:
    checks = []
    reasons_block = []

    def chk(name, holds, value, bound, provenance, note=""):
        checks.append({"check": name,
                       "holds_in_model": str(bool(holds)).lower(),
                       "model_value": value, "bound": bound,
                       "provenance": provenance, "note": note})
        if not holds:
            reasons_block.append(name)

    thr = float(seq.threshold_K)
    probe = float(seq.tC.probe_timeseries_K()[-1])
    chk("thermal_recovery", seq.mode_c_ready, f"{probe:.9e}",
        f"<= {thr:.3e} K", "SolverConfig.mode_d_temp_threshold_K",
        seq.status)

    _, vm = vibration_transfer()
    amp = float(vm["nv_output_amplitude_m"])
    vthr = float(vm["mode_d_amp_threshold_m"])
    chk("vibration_amplitude", amp < vthr, f"{amp:.9e}",
        f"< {vthr:.3e} m", "canonical vibration transfer chain",
        "forecast amplitude at the NV region; consistent with the "
        "canonical VIBRATION_TRANSFER_CHECK gate (CONDITIONAL, transfer "
        "factors ASSUMED)")

    dstate = device_state("MODE_D")
    chk("laser_off_in_mode_D", not dstate.laser_active,
        str(dstate.laser_active).lower(), "false",
        "mode state machine (canonical mode map)")
    chk("microwave_drive_allowed", dstate.microwave_active, "true",
        "true (Mode D ODMR drive)", "mode state machine")

    c13 = [r for r in species_rows
           if r["phase"] == "MODE_D" and r["species"] == "C13_CH4"]
    chk("c13_methane_not_live", bool(c13) and c13[0]["allowed_active"] == "false",
        "structurally forbidden", "no live C-13 methane in Mode D",
        "canonical species policy (validator raises)",
        "residual-coverage bound NOT_IMPLEMENTED (value reported in species "
        "accounting)")

    eligible = not reasons_block
    return {
        "eligibility_forecast": "CONDITIONAL" if eligible else "BLOCKED",
        "eligible_in_model": str(bool(eligible)).lower(),
        "blocking_reasons": reasons_block,
        "checks": checks,
        "meaning": "forecast eligibility of the MODEL state for Mode-D "
                   "sensing under the existing canonical bounds; CONDITIONAL "
                   "means no modeled block was found -- it is not PASS, not "
                   "measured NV performance, and requires in-system "
                   "measurement to resolve",
        "measured_in_this_system": False,
        "can_PASS_now": "NO",
        "label": LABEL,
    }
