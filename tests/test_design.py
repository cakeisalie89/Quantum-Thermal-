"""Tests for qta_multiphysics.design.

Runnable standalone or via pytest. Verifies the validator does NOT false-flag
the (consistent) canonical design, yet DOES fire on injected violations.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from qta_multiphysics.design import (
    load_registry, validate_design, build_interface_graph, run_design_layer,
    DesignComponent, DesignInterface,
)


def _component(cid, **kw):
    base = dict(
        component_id=cid, name="x", component_type="x", subsystem="x",
        allowed_modes=[], status="DESIGN_SPECIFIED", current_or_forecast="forecast",
        provenance_source="src", part_number_or_spec="", parameters={},
        parent=None, warm_side_anchor="", cold_side_anchor="",
        thermal_intercepts=[], interfaces=[], associated_gates=[],
        contamination_path="", can_PASS_now="NO", validation_state="UNVALIDATED",
        source_record="test",
    )
    base.update(kw)
    return DesignComponent(**base)


def _errors(findings, rule=None):
    return [f for f in findings if f["severity"] == "ERROR" and (rule is None or f["rule"] == rule)]


def test_registry_counts():
    reg = load_registry()
    assert len(reg.components) == 121, len(reg.components)
    assert len(reg.interfaces) == 75, len(reg.interfaces)
    assert len(reg.interlocks) == 14, len(reg.interlocks)


def test_canonical_design_has_no_error_findings():
    reg = load_registry()
    F = validate_design(reg)
    errs = _errors(F)
    assert errs == [], f"canonical design unexpectedly flagged: {errs}"
    # but the validator is still live: the real D12 vs D12_G23 naming mismatch
    # must surface as a WARN.
    warns = [f for f in F if f["severity"] == "WARN" and f["rule"] == "gate_referenced_but_absent"]
    assert any(w["subject"] == "D12" for w in warns), "expected D12 naming-mismatch WARN"


def test_rule_c13_methane_outside_mode_B_fires():
    reg = load_registry()
    reg.components["ZZ1"] = _component(
        "ZZ1", name="C-13 methane delivery line", component_type="deliver precursor",
        subsystem="Gas delivery", allowed_modes=["B", "D"])
    F = validate_design(reg)
    assert _errors(F, "c13_methane_outside_mode_B"), "c13-outside-B rule did not fire"


def test_rule_he_outside_mode_D_fires():
    reg = load_registry()
    reg.components["ZZ2"] = _component(
        "ZZ2", name="He-3 surface dosing line", component_type="dose helium-3 to NV surface",
        subsystem="Gas delivery", allowed_modes=["B", "D"])
    F = validate_design(reg)
    assert _errors(F, "he_outside_mode_D"), "he-outside-D rule did not fire"


def test_rule_stage_ordering_fires():
    reg = load_registry()
    reg.components["ZZ3"] = _component("ZZ3", thermal_intercepts=["10mK", "4K"])
    F = validate_design(reg)
    assert _errors(F, "invalid_stage_ordering"), "stage-ordering rule did not fire"


def test_rule_thermal_connection_fires():
    reg = load_registry()
    reg.components["ZZ4"] = _component(
        "ZZ4", warm_side_anchor="10 mK flange", cold_side_anchor="4K stage")
    F = validate_design(reg)
    assert _errors(F, "invalid_thermal_connection"), "thermal-connection rule did not fire"


def test_rule_sensing_outside_mode_D_fires():
    reg = load_registry()
    reg.interfaces["ZI1"] = DesignInterface(
        interface_id="ZI1", name="NV readout laser", subsystems="optics / NV",
        function="ODMR/Ramsey readout", allowed_modes=["B", "D"], status="DESIGN_SPECIFIED",
        associated_gates=[], possible_mismatch="", measurement_required="",
        can_PASS_now="NO", source_record="test")
    F = validate_design(reg)
    assert _errors(F, "sensing_before_mode_C"), "sensing-outside-D rule did not fire"


def test_operational_accessors():
    reg = load_registry()
    fss = reg.forbidden_simultaneous_states()
    assert len(fss) >= 1 and all("signals" in x for x in fss)
    assert reg.components_for_gate("D5"), "no components found for gate D5"
    assert reg.components_in_mode("D"), "no components in Mode D"


def test_interface_graph():
    reg = load_registry()
    g = build_interface_graph(reg)
    assert len(g["nodes"]) > 0 and len(g["edges"]) > 0
    # deterministic ordering
    assert g["nodes"] == sorted(g["nodes"])
    assert [e["edge_id"] for e in g["edges"]] == sorted(e["edge_id"] for e in g["edges"])


def test_outputs_deterministic():
    files = ["design_component_registry.json", "design_interface_graph.json",
             "design_decision_ledger.json", "design_validation_report.csv"]

    def run_to(d):
        run_design_layer(output_dir=Path(d))
        return {f: hashlib.sha256((Path(d) / f).read_bytes()).hexdigest() for f in files}

    h1 = run_to("/tmp/designrun1")
    h2 = run_to("/tmp/designrun2")
    for f in files:
        assert h1[f] == h2[f], f"{f} not byte-identical across runs"


ALL_TESTS = [
    test_registry_counts,
    test_canonical_design_has_no_error_findings,
    test_rule_c13_methane_outside_mode_B_fires,
    test_rule_he_outside_mode_D_fires,
    test_rule_stage_ordering_fires,
    test_rule_thermal_connection_fires,
    test_rule_sensing_outside_mode_D_fires,
    test_operational_accessors,
    test_interface_graph,
    test_outputs_deterministic,
]


def main() -> int:
    passed = failed = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(ALL_TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
