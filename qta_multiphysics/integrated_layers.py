"""Integrated QTA forecast layers: design + NV spin + Bayesian design.

Orchestrates the three additions in the canonical order and feeds the gate
system with NEW forecast gate records that are never PASS. This module is the
single entry point the top-level runner calls for the new layers; it does not
modify the existing multiphysics outputs.

Order implemented here (steps of the full execution path that concern the new
layers):
  1. Validate the computable design graph.
  3. Confirm Mode C recovery readiness before any Mode D spin calculation.
  4. Run the QuTiP spin layer only when the configured sequence permits it.
  5. Update relevant forecast observables (from the spin layer).
  6. Run the Bayesian experiment ranking.
  7. Update gate records WITHOUT creating PASS states.
  8. Write deterministic outputs.
(Steps 2, 9, 10 - existing multiphysics, output-sync report, manifest - are
owned by the top-level runner and generate_manifest.py.)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .design import run_design_layer
from .nv_spin import run_nv_spin_layer, ci_config as nv_ci, standard_config as nv_std
from .nv_spin.runner import ModeReadinessError
from .expdesign import run_expdesign_layer

# canonical gate schema (must match results_gate_table.csv exactly)
_GATE_HEADER = ["gate_id", "mode", "name", "equation", "computed", "threshold",
                "unit", "status", "reason", "fix", "measured_in_this_system",
                "source_directness", "can_PASS_now", "required_measurement",
                "blocked_by", "notes"]
_FORECAST_NOTE = "FORECAST_ONLY; can_PASS_now=NO"
_ALLOWED_STATES = {"BLOCKED", "CONDITIONAL", "UNKNOWN", "DERIVED_CHECK", "NOT_IMPLEMENTED"}


def _csv_cell(v) -> str:
    s = "" if v is None else str(v)
    if any(ch in s for ch in (",", '"', "\n")):
        return '"' + s.replace('"', '""') + '"'
    return s


class GateTableRefusal(Exception):
    """A row was offered to the canonical gate table that may not be in it."""


def _write_gate_csv(path: Path, rows: list) -> None:
    """Write the gate table, refusing any row that breaks the zero-PASS rule.

    THESE WERE `assert` STATEMENTS, AND `python -O` DELETES THOSE.

    `PASS = 0`, `can_PASS_now = NO` and `measured_in_this_system = false` are
    the three claims this package exists to hold, and in the function that
    EMITS them they were enforced by statements the interpreter removes when
    given a flag. Reproduced (D-2026-45):

        normal:  refused -- illegal gate state PASS, no file written
        -O:      FORGED,,,,,,,PASS,,,true,,YES,,,   written, no error

    A refusal that an optimisation flag can switch off is not a refusal. It
    also fails OPEN, which is the wrong direction for a gate table: the
    invariant is that no PASS row exists, so losing the check produces
    exactly the artifact the invariant forbids.

    Raising is not a style preference here. `assert` is a DEBUGGING
    construct whose contract is that it may vanish; an enforcement point has
    the opposite contract.
    """
    lines = [",".join(_GATE_HEADER)]
    for r in rows:
        if r["status"] not in _ALLOWED_STATES:
            raise GateTableRefusal(
                f"gate {r.get('gate_id', '(unnamed)')!r} offers status "
                f"{r['status']!r}, which is not in {sorted(_ALLOWED_STATES)}. "
                "The canonical gate table carries no PASS row by "
                "construction, and this is where that is decided")
        if r["can_PASS_now"] != "NO":
            raise GateTableRefusal(
                f"gate {r.get('gate_id', '(unnamed)')!r} claims "
                f"can_PASS_now={r['can_PASS_now']!r}; no gate in this "
                "package can PASS now, and saying so in the table would "
                "make the artifact disagree with the claim it carries")
        if r["measured_in_this_system"] != "false":
            raise GateTableRefusal(
                f"gate {r.get('gate_id', '(unnamed)')!r} claims "
                f"measured_in_this_system={r['measured_in_this_system']!r}. "
                "Nothing in this package is measured; MODEL_ONLY / "
                "FORECAST_ONLY is not a label, it is the state")
        lines.append(",".join(_csv_cell(r.get(k, "")) for k in _GATE_HEADER))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _spin_gate_records(spin_summary: dict, design_summary: dict, expd: dict) -> list:
    obs = spin_summary["observables"]
    dq = spin_summary["distinct_quantities"]

    def g(gate_id, mode, name, equation, computed, threshold, unit, status, reason,
          required, blocked_by=""):
        return {
            "gate_id": gate_id, "mode": mode, "name": name, "equation": equation,
            "computed": computed, "threshold": threshold, "unit": unit,
            "status": status, "reason": reason, "fix": required,
            "measured_in_this_system": "false",
            "source_directness": "ASSUMED_OR_DESIGN_SPECIFIED", "can_PASS_now": "NO",
            "required_measurement": required, "blocked_by": blocked_by,
            "notes": _FORECAST_NOTE,
        }

    rows = [
        g("NV-TAUC-UNKNOWN", "D", "NV surface-noise correlation time tau_c",
          "tau_c vs 292 us canonical threshold", "", 2.92e-4, "s", "UNKNOWN",
          "tau_c is UNKNOWN (forecast OU bath correlation time); not measured on "
          "F-diamond. Distinct from T1/T2/T2*.",
          "In-system tau_c at 10 mK with controlled 3He coverage (paired 3He vs 4He)"),
        g("NV-T2STAR-FCAST", "D", "NV Ramsey T2* (forecast)",
          "T2* from OU colored-noise model", obs["T2star_s"]["value"], "", "s",
          "CONDITIONAL",
          "Forecast Ramsey decay from the spin model under ASSUMED tau_c/sigma. "
          "Model-only; not a measured coherence time.",
          "Ramsey T2* on the actual sample at 10 mK"),
        g("NV-T2ECHO-FCAST", "D", "NV Hahn-echo T2 (forecast)",
          "T2 from OU colored-noise model + echo", obs["T2_echo_s"]["value"], "", "s",
          "CONDITIONAL",
          "Forecast Hahn-echo coherence; model-only.",
          "Hahn-echo T2 at 10 mK on the actual sample"),
        g("NV-ODMR-VIS", "D", "NV ODMR visibility / contrast at 10 mK",
          "C_contr at 10 mK", "", "", "dimensionless", "BLOCKED",
          "ODMR contrast C_contr at 10 mK is UNKNOWN (co-equal bottleneck with "
          "tau_c). Spin model gives a transition spectrum only, not a measured contrast.",
          "ODMR contrast at 10 mK with the proposed laser/MW/collection geometry"),
        g("DESIGN-GRAPH-VALID", "ALL", "Design-graph cross-source validation",
          "ERROR findings == 0", design_summary["finding_counts"].get("ERROR", 0), 0,
          "count", "DERIVED_CHECK",
          "Typed design registry validated against BOM/interfaces/interlocks. "
          "Derived check over forecast design records; not a hardware test.",
          "N/A (design-graph consistency check)"),
        g("EXPDESIGN-RANKING", "ALL", "Bayesian experiment ranking available",
          "EIG-based ranking computed", expd["ranking"][0]["experiment_id"], "", "rank",
          "DERIVED_CHECK",
          "Direct Monte-Carlo EIG ranking of validation experiments (forecast). "
          "Identifies the next most-informative measurement; does not perform it.",
          "Perform the top-ranked validation experiment"),
    ]
    return rows


def run_integrated_layers(*, profile: str = "standard", mode_c_ready: bool = True,
                          output_dir: Optional[Path] = None,
                          repo_root: Optional[Path] = None, seed: int = 42) -> dict:
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "outputs"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. design graph validation
    design = run_design_layer(output_dir=output_dir, repo_root=repo_root)

    # 3 + 4. Mode C readiness gate, then QuTiP spin layer (Mode D)
    nv_cfg = nv_ci() if profile == "ci" else nv_std()
    try:
        spin = run_nv_spin_layer(mode_c_ready=mode_c_ready, cfg=nv_cfg,
                                 output_dir=output_dir, write_outputs=True)
    except ModeReadinessError:
        # honest hard stop: sensing cannot precede recovery readiness
        raise

    # 6. Bayesian ranking
    if profile == "ci":
        expd = run_expdesign_layer(n_outer=200, n_inner=200, n_batches=4, seed=seed,
                                   profile="ci", output_dir=output_dir)
    else:
        expd = run_expdesign_layer(seed=seed, profile="standard", output_dir=output_dir)

    # 5 + 7. forecast observables -> NEW gate records (never PASS)
    gate_rows = _spin_gate_records(spin["summary"], design, expd)
    _write_gate_csv(output_dir / "nv_spin_gate_records.csv", gate_rows)

    # 8. combined deterministic summary
    combined = {
        "_note": "Integrated forecast-layer summary (design + NV spin + Bayesian "
                 "design). Forecast-only; no PASS gate states; no measured results.",
        "forecast_only": True,
        "profile": profile,
        "mode_c_ready": mode_c_ready,
        "design": {"n_components": design["n_components"],
                   "n_interfaces": design["n_interfaces"],
                   "finding_counts": design["finding_counts"]},
        "nv_spin_observables": spin["summary"]["observables"],
        "nv_spin_distinct_quantities": spin["summary"]["distinct_quantities"],
        "bayesian_top_experiment": expd["ranking"][0]["experiment_id"],
        "bayesian_estimator_source": expd["estimator_source"],
        "new_gate_records": [r["gate_id"] for r in gate_rows],
        "new_gate_states": sorted({r["status"] for r in gate_rows}),
        "no_pass_gate_states": all(r["status"] != "PASS" for r in gate_rows),
    }
    (output_dir / "integrated_layers_summary.json").write_text(
        json.dumps(combined, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8")

    return {"design": design, "spin": spin, "expd": expd, "gate_rows": gate_rows,
            "summary": combined, "output_dir": output_dir}
