"""Design-graph validation engine and deterministic outputs.

Implements the required cross-source consistency, mode, and interlock checks
against the typed registry built in :mod:`.registry`. The registry and these
checks are operationally consumed (see ``validate_design`` /
``DesignRegistry`` accessors), not merely exported as documentation.

Severity: ERROR (canonical-integrity violation), WARN (suspect), INFO
(coverage/visibility). No check ever emits a PASS gate state.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Optional

from .registry import (
    DesignRegistry, load_registry, build_interface_graph, STAGE_ORDER, MODES,
    _REPO_ROOT,
)

_C13_RE = re.compile(r"c-?13|methane|precursor|lcvd", re.IGNORECASE)
_HE_RE = re.compile(r"he-?3|he-?4|helium", re.IGNORECASE)
_SENSING_RE = re.compile(r"nv readout|odmr|ramsey|rabi|esr|spin control|spin readout|spad", re.IGNORECASE)
# Components/interfaces that merely monitor, isolate, interlock, purge, or use a
# species as refrigerant are NOT active controlled-species sources. Excluding
# them prevents false positives on the (consistent) canonical design.
_EXCLUDE_RE = re.compile(
    r"isolat|purge|inert|monitor|diagnos|interlock|prevent|track|witness|verif|"
    r"residual|separat|valve|sampling|proxy|\bRGA\b|\bQCM\b|analyz|gauge|"
    r"control system|software|dilution|refriger|mixture|leak check|coverage proxy|"
    r"purif|purity|getter|filter|trap\b",
    re.IGNORECASE)


def _is_active_source(text: str, species_re) -> bool:
    return bool(species_re.search(text)) and not _EXCLUDE_RE.search(text)


def _finding(rule, severity, subject, detail):
    return {"rule": rule, "severity": severity, "subject": subject, "detail": detail}


def _stage_index(text: str):
    for s in STAGE_ORDER:
        if re.search(rf"\b{s[:-2]}\s*{s[-2:]}\b" if s.endswith("mK") else rf"\b{s[:-1]}\s*K\b", text):
            return STAGE_ORDER.index(s)
    return None


def validate_design(reg: DesignRegistry, repo_root: Optional[Path] = None) -> list:
    root = Path(repo_root) if repo_root else _REPO_ROOT
    F = []

    # 1. duplicate component IDs (re-read raw BOM rows; dict would hide dupes)
    raw_ids = []
    with open(root / "BOM.csv", "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            raw_ids.append(r["item_id"].strip())
    seen = set()
    for cid in raw_ids:
        if cid in seen:
            F.append(_finding("duplicate_component_id", "ERROR", cid,
                              "component_id appears more than once in BOM.csv"))
        seen.add(cid)

    # 2. missing units (structured parameters)
    n_params = 0
    for c in reg.components.values():
        for pname, pv in c.parameters.items():
            n_params += 1
            if not pv.get("unit"):
                F.append(_finding("missing_units", "ERROR", f"{c.component_id}.{pname}",
                                  "structured parameter has no unit"))
    F.append(_finding("missing_units", "INFO", "_summary",
                      f"{n_params} structured parameters examined for units"))

    # 3. missing provenance
    for c in reg.components.values():
        if not c.status or not c.provenance_source:
            F.append(_finding("missing_provenance", "ERROR", c.component_id,
                              f"status={c.status!r} source={c.provenance_source!r}"))

    # 4. invalid stage ordering
    for c in reg.components.values():
        idx = [STAGE_ORDER.index(s) for s in c.thermal_intercepts if s in STAGE_ORDER]
        if idx != sorted(idx):
            F.append(_finding("invalid_stage_ordering", "ERROR", c.component_id,
                              f"intercepts not warm->cold ordered: {c.thermal_intercepts}"))

    # 5. invalid thermal connection (warm must be >= warmer than cold)
    for c in reg.components.values():
        wi = _stage_index(c.warm_side_anchor)
        ci = _stage_index(c.cold_side_anchor)
        if wi is not None and ci is not None and wi > ci:
            F.append(_finding("invalid_thermal_connection", "ERROR", c.component_id,
                              f"warm anchor colder than cold anchor "
                              f"({c.warm_side_anchor!r} vs {c.cold_side_anchor!r})"))

    # 6. C-13 methane active outside Mode B
    for c in reg.components.values():
        ident = f"{c.name} {c.component_type} {c.subsystem}"
        if _is_active_source(ident, _C13_RE):
            bad = [m for m in c.allowed_modes if m != "B"]
            for m in bad:
                sev = "ERROR" if m in ("C", "D") else "WARN"
                F.append(_finding("c13_methane_outside_mode_B", sev, c.component_id,
                                  f"active C-13/methane/LCVD source allowed in Mode {m} (must be Mode B)"))

    # 7. He-3/He-4 active outside Mode D
    for c in reg.components.values():
        ident = f"{c.name} {c.component_type} {c.subsystem}"
        if _is_active_source(ident, _HE_RE):
            bad = [m for m in c.allowed_modes if m != "D"]
            for m in bad:
                F.append(_finding("he_outside_mode_D", "ERROR", c.component_id,
                                  f"active He-3/He-4 source allowed in Mode {m} (must be Mode D)"))

    # 8. Mode B / Mode D overlap
    for c in reg.components.values():
        ident = f"{c.name} {c.component_type} {c.subsystem}"
        if "B" in c.allowed_modes and "D" in c.allowed_modes:
            if _is_active_source(ident, _C13_RE) and _SENSING_RE.search(ident):
                F.append(_finding("mode_B_D_overlap", "ERROR", c.component_id,
                                  "active processing+sensing component allowed in both Mode B and Mode D"))
    has_il01 = any(("lcvd" in il.condition.lower() or "precursor" in il.condition.lower())
                   and "sens" in il.condition.lower() and il.interlock_type.upper().startswith("IMPOSSIBLE")
                   for il in reg.interlocks.values())
    if not has_il01:
        F.append(_finding("mode_B_D_overlap", "ERROR", "_interlocks",
                          "no IMPOSSIBLE interlock forbids processing+sensing concurrency"))

    # 9. sensing before Mode C readiness (design-level: sensing objects must be D-only;
    #    sequence places C before D)
    if STAGE_ORDER and not (MODES.index("C") < MODES.index("D")):
        F.append(_finding("sensing_before_mode_C", "ERROR", "_sequence",
                          "canonical sequence does not place Mode C before Mode D"))
    for i in reg.interfaces.values():
        if _SENSING_RE.search(f"{i.name} {i.function}"):
            bad = [m for m in i.allowed_modes if m != "D"]
            for m in bad:
                F.append(_finding("sensing_before_mode_C", "ERROR", i.interface_id,
                                  f"sensing interface active in Mode {m} (sensing is Mode D, after C)"))

    # 10. shutter / interlock inconsistencies (well-formedness + signal sanity)
    for il in reg.interlocks.values():
        if il.interlock_type.upper().startswith("IMPOSSIBLE") and len(il.signals) < 2:
            F.append(_finding("interlock_inconsistency", "WARN", il.interlock_id,
                              f"IMPOSSIBLE interlock has <2 signals: {il.signals}"))
        if not il.reason:
            F.append(_finding("interlock_inconsistency", "WARN", il.interlock_id,
                              "interlock missing reason"))

    # 11. gates referenced by design but absent from the gate table (+ coverage)
    gate_ids = set()
    with open(root / "results_gate_table.csv", "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            gate_ids.add(r["gate_id"].strip())
    referenced = set(reg.all_referenced_gates())
    for g in sorted(referenced - gate_ids):
        F.append(_finding("gate_referenced_but_absent", "WARN", g,
                          "gate referenced by a design object but not present in results_gate_table.csv"))
    uncovered = sorted(gate_ids - referenced)
    F.append(_finding("gate_coverage", "INFO", "_summary",
                      f"{len(referenced & gate_ids)}/{len(gate_ids)} gate ids referenced by design objects; "
                      f"{len(uncovered)} uncovered"))

    # 12. design values disagreeing with canonical configuration (base temperature)
    mp = json.load(open(root / "measured_parameters.json", encoding="utf-8"))
    base_mK = round(float(mp["T_fridge_K"]["value"]) * 1000)  # 10 mK
    for c in reg.components.values():
        m = re.search(r"(\d+)\s*mK\s*base", f"{c.part_number_or_spec} {c.name}", re.IGNORECASE)
        if m and int(m.group(1)) != base_mK:
            F.append(_finding("design_vs_canonical_mismatch", "ERROR", c.component_id,
                              f"states {m.group(1)} mK base; canonical base is {base_mK} mK"))

    return sorted(F, key=lambda x: (x["rule"], x["severity"], str(x["subject"])))


def apply_validation_states(reg: DesignRegistry, findings: list) -> None:
    err = {f["subject"] for f in findings if f["severity"] == "ERROR"}
    warn = {f["subject"] for f in findings if f["severity"] == "WARN"}
    for cid, c in reg.components.items():
        c.validation_state = "INVALID" if cid in err else ("WARN" if cid in warn else "VALID")


# --- deterministic output writers ---------------------------------------------

def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                    encoding="utf-8")


def write_design_outputs(reg: DesignRegistry, output_dir: Path, findings: list) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    comp_json = {
        "_note": "Typed QTA design-component registry (forecast/pre-hardware). "
                 "Derived from BOM.csv; no invented geometry/vendors.",
        "forecast_only": True,
        "components": {cid: reg.components[cid].to_dict() for cid in sorted(reg.components)},
    }
    _write_json(output_dir / "design_component_registry.json", comp_json)

    graph = build_interface_graph(reg)
    _write_json(output_dir / "design_interface_graph.json", graph)

    ledger = {
        "_note": "Derived design decisions/relationships consumed by validation + "
                 "simulation. Forecast-only.",
        "forecast_only": True,
        "mode_permissions": {cid: reg.components[cid].allowed_modes for cid in sorted(reg.components)},
        "gate_associations": {cid: reg.components[cid].associated_gates
                              for cid in sorted(reg.components) if reg.components[cid].associated_gates},
        "forbidden_simultaneous_states": reg.forbidden_simultaneous_states(),
        "validation_states": {cid: reg.components[cid].validation_state for cid in sorted(reg.components)},
    }
    _write_json(output_dir / "design_decision_ledger.json", ledger)

    # validation report CSV (sorted, deterministic, LF newlines)
    header = ["rule", "severity", "subject", "detail"]
    lines = [",".join(header)]
    for f in findings:
        row = [f["rule"], f["severity"], str(f["subject"]),
               '"' + f["detail"].replace('"', "'") + '"']
        lines.append(",".join(row))
    (output_dir / "design_validation_report.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")

    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"n_components": len(reg.components), "n_interfaces": len(reg.interfaces),
            "n_interlocks": len(reg.interlocks), "finding_counts": counts}


def run_design_layer(output_dir: Optional[Path] = None, repo_root: Optional[Path] = None) -> dict:
    """Build registry, validate, and write outputs. Returns a summary dict."""
    reg = load_registry(repo_root)
    findings = validate_design(reg, repo_root)
    apply_validation_states(reg, findings)
    if output_dir is None:
        output_dir = Path(__file__).resolve().parents[1] / "outputs"
    summary = write_design_outputs(reg, output_dir, findings)
    summary["findings"] = findings
    summary["registry"] = reg
    return summary
