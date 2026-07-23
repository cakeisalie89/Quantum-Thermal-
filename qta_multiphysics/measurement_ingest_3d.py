"""Read-only synthetic-measurement ingestion and forecast comparison.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Stage 4 boundary (architectural, not procedural):
- data_class accepts exactly one value: "SYNTHETIC". "HARDWARE" (or any
  other value) is refused record-by-record with the recorded reason that
  hardware ingestion requires the validation-plan governance step and is
  not implemented. No code path exists from a measurement to a gate, a
  parameter, machine state, or `measured_in_this_system`.
- The comparator is read-only end-to-end: it imports no gate machinery,
  emits no status vocabulary, mutates nothing, and its outputs state on
  every row: comparison context only, not validation, never a gate input.

Active-species policy vs. diagnostics (approved correction #1):
- PROCESS quantities are phase-locked via the registry (e.g. Mode-D SNR
  only aligns to MODE_D) and off-policy alignment is rejected.
- DIAGNOSTIC quantities (residual-gas/RGA partial pressures of CH4, H2,
  He; vibration; timing) may be aligned to ANY mode: measuring a residual
  gas is not activating a species. Where no canonical prediction exists
  for the aligned mode (e.g. CH4 partial pressure outside Mode B), the
  record is accepted as a contamination/diagnostic observation with
  prediction=None and residual=None, stated.

Residual semantics (approved correction #2):
- residual = measured - predicted (only when a prediction exists).
- normalized_residual = residual / stddev ONLY when the record supplies a
  stddev-type uncertainty; otherwise null with normalization
  "NONE_AVAILABLE".
- For quantities with a Stage-3 p05-p95 forecast band: band_position =
  (measured - p05) / (p95 - p05); inside_band = 0 <= band_position <= 1;
  band_scaled_residual = (measured - p50) / (p95 - p05), explicitly
  labeled band-relative -- the band width is NEVER represented as a sigma.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
SCHEMA_VERSION = "1.0"
INTERPRETATION = ("comparison context only; not validation; never a gate "
                  "input")
HARDWARE_REFUSAL = ("hardware ingestion requires the validation-plan "
                    "governance step; not implemented in this stage")

MODES = ("MODE_A", "MODE_B", "MODE_C", "MODE_D")
_ALL = MODES

REQUIRED_FIELDS = ("measurement_id", "quantity", "value", "units",
                   "timestamp", "alignment", "data_class", "instrument",
                   "provenance")


def _canon_json(name):
    return json.loads(Path(name).read_text())


def _canon_csv(name):
    return list(csv.DictReader(open(name)))


def _band_theta(cycle):
    cu = _canon_json("campaign_uncertainty_3d.json")
    q = cu["cycles"][cycle - 1]["theta_c13_residual_before_D"]
    return {k: float(q[k]) for k in ("p05", "p50", "p95")}


def _band_panel(cycle):
    cu = _canon_json("campaign_uncertainty_3d.json")
    q = cu["cycles"][cycle - 1]["panel_CH4_ML"]
    return {k: float(q[k]) for k in ("p05", "p50", "p95")}


def _pred_T_recovery(_mode, _cycle):
    cs = _canon_json("campaign_state_3d.json")
    return float(cs["cycles"][0]["T_probe_recovery_end_K"])


def _pred_nv_T(_mode, _cycle):
    return float(_canon_json("multiphysics_summary.json")
                 ["thermal_metrics"]["NV_layer_T_1d_K"])


def _pred_peak(_mode, _cycle):
    return float(_canon_json("multiphysics_summary.json")
                 ["thermal_metrics"]["pulse_peak_NV_1d_K"])


def _pred_vib(_mode, _cycle):
    rows = _canon_csv("vibration_paths_3d_budget.csv")
    nv = [r for r in rows if r["stage"] == "NV_region"][0]
    return float(nv["output_amplitude_m"])


def _pred_settle(_mode, _cycle):
    rows = _canon_csv("vibration_paths_3d_budget.csv")
    nv = [r for r in rows if r["stage"] == "NV_region"][0]
    return float(nv["settling_time_s"])


def _pred_T2star(_mode, _cycle):
    rows = _canon_csv("nv_sequence_contrast.csv")
    r = [x for x in rows if x["sequence"] == "ramsey"
         and x["observable"] == "T2star_s"][0]
    return float(r["one_over_e_time_s"])


def _pred_P_CH4(mode, _cycle):
    return 1.0e-4 if mode == "MODE_B" else None      # canonical work pressure


def _pred_P_He(mode, _cycle):
    return 1.0e-6 if mode == "MODE_D" else None      # canonical dose pressure


def _pred_theta(_mode, cycle):
    cs = _canon_json("campaign_state_3d.json")
    return float(cs["cycles"][cycle - 1]["theta_c13_residual_before_D"])


def _pred_panel(_mode, cycle):
    cs = _canon_json("campaign_state_3d.json")
    return float(cs["cycles"][cycle - 1]["panel_CH4_monolayer_fraction"])


#: quantity -> (kind, permitted modes, units, cycle_indexed,
#:              predictor(mode, cycle)->float|None, band(cycle)->dict|None,
#:              source note)
QUANTITY_REGISTRY = {
    "T_probe_recovery_end_K": ("process", ("MODE_C",), "K", False,
        _pred_T_recovery, None,
        "campaign_state_3d.json cycles[].T_probe_recovery_end_K"),
    "NV_layer_T_K": ("process", ("MODE_B",), "K", False, _pred_nv_T, None,
        "multiphysics_summary.json thermal_metrics.NV_layer_T_1d_K"),
    "pulse_peak_NV_K": ("process", ("MODE_B",), "K", False, _pred_peak,
        None, "multiphysics_summary.json thermal_metrics.pulse_peak_NV_1d_K"),
    "P_H2_Pa": ("diagnostic", _ALL, "Pa", False,
        lambda m, c: 1.0e-12, None,
        "canonical post-bakeout residual target (contamination diagnostic)"),
    "P_CH4_partial_Pa": ("diagnostic", _ALL, "Pa", False, _pred_P_CH4, None,
        "canonical Mode-B work pressure; no canonical prediction outside "
        "Mode B (contamination diagnostic)"),
    "P_He_partial_Pa": ("diagnostic", _ALL, "Pa", False, _pred_P_He, None,
        "canonical Mode-D dose pressure; no canonical prediction outside "
        "Mode D (contamination diagnostic)"),
    "theta_c13_residual": ("diagnostic", _ALL, "monolayer_fraction", True,
        _pred_theta, _band_theta,
        "campaign residual before Mode D; Stage-3 band"),
    "panel_CH4_ML": ("process", _ALL, "monolayer_fraction", True,
        _pred_panel, _band_panel,
        "cryopanel loading forecast; Stage-3 band"),
    "nv_vibration_amplitude_m": ("diagnostic", _ALL, "m", False, _pred_vib,
        None, "vibration_paths_3d_budget.csv NV_region output amplitude"),
    "settling_time_s": ("diagnostic", _ALL, "s", False, _pred_settle, None,
        "vibration_paths_3d_budget.csv NV_region settling time"),
    "T2_star_s": ("process", ("MODE_D",), "s", False, _pred_T2star, None,
        "nv_sequence_contrast.csv ramsey/T2star row (conditional on that "
        "row's tau_c input; tau_c gate remains UNKNOWN; no gate effect)"),
    "mode_D_SNR": ("process", ("MODE_D",), "dimensionless", False,
        lambda m, c: 5.00, None,
        "canonical Mode-D SNR definition value (SNR=5.00 at canonical "
        "tau_c); conditional-on-canonical; no gate effect"),
    "purge_window_s": ("diagnostic", _ALL, "s", False, lambda m, c: 2.0,
        None, "canonical Mode-C purge window"),
}


def validate_record(rec, n_cycles=3):
    """Return (accepted: bool, reasons: list). Fail-closed field checks."""
    reasons = []
    if not isinstance(rec, dict):
        return False, ["record is not an object"]
    for f in REQUIRED_FIELDS:
        if f not in rec:
            reasons.append(f"missing required field '{f}'")
    if reasons:
        return False, reasons
    if rec["data_class"] != "SYNTHETIC":
        if rec["data_class"] == "HARDWARE":
            return False, [f"data_class HARDWARE refused: {HARDWARE_REFUSAL}"]
        return False, [f"data_class '{rec['data_class']}' not accepted "
                       "(SYNTHETIC only in this stage)"]
    q = rec["quantity"]
    if q not in QUANTITY_REGISTRY:
        return False, [f"unknown quantity '{q}' (not in registry)"]
    kind, modes, units, cyc_idx, _pred, _band, _src = QUANTITY_REGISTRY[q]
    if rec["units"] != units:
        reasons.append(f"unit mismatch: got '{rec['units']}', registry "
                       f"requires '{units}' (no conversion; fail-closed)")
    try:
        datetime.fromisoformat(str(rec["timestamp"]).replace("Z", "+00:00"))
    except Exception:
        reasons.append(f"timestamp '{rec['timestamp']}' not ISO-8601")
    al = rec["alignment"]
    if not isinstance(al, dict) or "mode" not in al:
        reasons.append("alignment must be an object with 'mode'")
    else:
        mode = al["mode"]
        if mode not in MODES:
            reasons.append(f"alignment mode '{mode}' not in {MODES}")
        elif kind == "process" and mode not in modes:
            reasons.append(f"process quantity '{q}' not permitted in "
                           f"{mode} (permitted: {modes}); measuring is "
                           "allowed only where the process is defined -- "
                           "diagnostic quantities are the any-mode class")
        if cyc_idx:
            c = al.get("cycle")
            if not isinstance(c, int) or not (1 <= c <= n_cycles):
                reasons.append(f"quantity '{q}' requires integer alignment "
                               f"cycle in 1..{n_cycles}")
    try:
        float(rec["value"])
    except Exception:
        reasons.append(f"value '{rec['value']}' is not numeric")
    unc = rec.get("uncertainty")
    if unc is not None:
        if not isinstance(unc, dict) or unc.get("type") not in (
                "stddev", "interval", "none"):
            reasons.append("uncertainty.type must be stddev|interval|none")
        elif unc["type"] == "stddev":
            try:
                if float(unc["value"]) <= 0:
                    reasons.append("stddev must be > 0")
            except Exception:
                reasons.append("stddev value not numeric")
    return (len(reasons) == 0), reasons


def compare_record(rec):
    """Accepted record -> comparison row (read-only; corrected semantics)."""
    q = rec["quantity"]
    kind, _modes, units, cyc_idx, pred_fn, band_fn, src = \
        QUANTITY_REGISTRY[q]
    mode = rec["alignment"]["mode"]
    cycle = rec["alignment"].get("cycle") if cyc_idx else None
    measured = float(rec["value"])
    predicted = pred_fn(mode, cycle)
    row = {"measurement_id": rec["measurement_id"], "quantity": q,
           "kind": kind, "mode": mode,
           "cycle": str(cycle) if cycle else "-",
           "measured": f"{measured:.9e}", "units": units,
           "predicted": (f"{predicted:.9e}" if predicted is not None
                         else "NONE (no canonical prediction for this "
                              "mode; contamination/diagnostic record)"),
           "data_class": "SYNTHETIC", "synthetic_only": "true",
           "measured_in_this_system": "false", "can_PASS_now": "NO",
           "interpretation": INTERPRETATION, "source": src,
           "label": LABEL}
    if predicted is not None:
        residual = measured - predicted
        row["residual"] = f"{residual:.9e}"
        unc = rec.get("uncertainty") or {}
        if unc.get("type") == "stddev":
            row["normalized_residual"] = \
                f"{residual / float(unc['value']):.9e}"
            row["normalization"] = "stddev"
        else:
            row["normalized_residual"] = "null"
            row["normalization"] = "NONE_AVAILABLE"
    else:
        row["residual"] = "null"
        row["normalized_residual"] = "null"
        row["normalization"] = "NONE_AVAILABLE"
    if band_fn is not None and cycle is not None:
        b = band_fn(cycle)
        width = b["p95"] - b["p05"]
        pos = (measured - b["p05"]) / width if width > 0 else float("nan")
        row["band_p05"], row["band_p50"], row["band_p95"] = (
            f"{b['p05']:.9e}", f"{b['p50']:.9e}", f"{b['p95']:.9e}")
        row["band_position"] = f"{pos:.6e}"
        row["inside_band"] = "true" if 0.0 <= pos <= 1.0 else "false"
        row["band_scaled_residual"] = \
            f"{(measured - b['p50']) / width:.6e}"
        row["band_note"] = ("band-relative quantities; the p05-p95 width "
                            "is NOT a sigma and is never represented as one")
    else:
        row["inside_band"] = "no_band"
    return row


def ingest_and_compare(path="synthetic_measurements_example.json",
                       n_cycles=3) -> dict:
    """Fail-closed ingestion + read-only comparison report."""
    p = Path(path)
    base = {"schema_version": SCHEMA_VERSION, "synthetic_only": True,
            "hardware_refused_by_design": HARDWARE_REFUSAL,
            "measured_in_this_system": False, "can_PASS_now": "NO",
            "interpretation": INTERPRETATION, "label": LABEL}
    if not p.exists():
        return {**base, "ingestion_status": "NO_MEASUREMENTS",
                "n_accepted": 0, "n_rejected": 0, "accepted": [],
                "rejected": [], "coverage": {"n_banded": 0, "n_inside": 0,
                                             "fraction": "null"}}
    try:
        doc = json.loads(p.read_text())
        assert doc.get("schema_version") == SCHEMA_VERSION
        records = doc["measurements"]
        assert isinstance(records, list)
    except Exception as e:            # noqa: BLE001
        return {**base, "ingestion_status": "REJECTED_FILE",
                "reason": f"malformed measurement file: {e!r}",
                "n_accepted": 0, "n_rejected": 0, "accepted": [],
                "rejected": [], "coverage": {"n_banded": 0, "n_inside": 0,
                                             "fraction": "null"}}
    accepted, rejected = [], []
    for rec in records:
        ok, reasons = validate_record(rec, n_cycles)
        if ok:
            accepted.append(compare_record(rec))
        else:
            rejected.append({"measurement_id":
                             rec.get("measurement_id", "(missing)")
                             if isinstance(rec, dict) else "(not an object)",
                             "reasons": reasons,
                             "disposition": "REJECTED (fail-closed)"})
    banded = [r for r in accepted if r["inside_band"] in ("true", "false")]
    inside = [r for r in banded if r["inside_band"] == "true"]
    return {**base, "ingestion_status": "OK",
            "source_file": str(p),
            "n_accepted": len(accepted), "n_rejected": len(rejected),
            "accepted": accepted, "rejected": rejected,
            "coverage": {"n_banded": len(banded), "n_inside": len(inside),
                         "fraction": (f"{len(inside)/len(banded):.6f}"
                                      if banded else "null")}}


def comparison_rows(rep: dict) -> list:
    keys = ("measurement_id", "quantity", "kind", "mode", "cycle",
            "measured", "units", "predicted", "residual",
            "normalized_residual", "normalization", "inside_band",
            "band_position", "band_scaled_residual", "data_class",
            "interpretation", "label")
    rows = []
    for a in rep["accepted"]:
        rows.append({k: a.get(k, "-") for k in keys})
    return rows
