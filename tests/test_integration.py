"""Integration tests for qta_multiphysics.integrated_layers."""
from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from qta_multiphysics.integrated_layers import run_integrated_layers, _ALLOWED_STATES
from qta_multiphysics.nv_spin.runner import ModeReadinessError

_EXPECTED = [
    # design
    "design_component_registry.json", "design_interface_graph.json",
    "design_decision_ledger.json", "design_validation_report.csv",
    # nv spin
    "nv_coherence_curves.csv", "nv_sequence_contrast.csv", "nv_odmr_spectrum.csv",
    "nv_spin_dynamics_summary.json", "nv_spin_parameter_provenance.json",
    # expdesign
    "experimental_design_candidates.csv", "expected_information_gain.csv",
    "validation_experiment_ranking.csv", "adaptive_experiment_policy.json",
    "bayesian_design_summary.json", "deep_surrogate_readiness.json",
    "experiment_falsification_map.csv",
    # integrated
    "nv_spin_gate_records.csv", "integrated_layers_summary.json",
]


def test_integration_completes_with_all_outputs():
    out = Path("/tmp/integ1")
    r = run_integrated_layers(profile="ci", output_dir=out, seed=42)
    for f in _EXPECTED:
        assert (out / f).exists(), f"missing output {f}"
    assert r["summary"]["no_pass_gate_states"] is True


def test_new_gate_records_never_pass():
    out = Path("/tmp/integ1")
    run_integrated_layers(profile="ci", output_dir=out, seed=42)
    with open(out / "nv_spin_gate_records.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "no gate records written"
    for row in rows:
        assert row["status"] in _ALLOWED_STATES, row["status"]
        assert row["status"] != "PASS"
        assert row["can_PASS_now"] == "NO"
        assert row["measured_in_this_system"] == "false"
        assert "FORECAST_ONLY" in row["notes"]


def test_mode_c_readiness_blocks_mode_d():
    raised = False
    try:
        run_integrated_layers(profile="ci", mode_c_ready=False, output_dir=Path("/tmp/integ_block"))
    except ModeReadinessError:
        raised = True
    assert raised, "Mode D ran before Mode C readiness in the integrated layer"


def test_integration_two_run_byte_identical():
    def run_to(d):
        run_integrated_layers(profile="ci", output_dir=Path(d), seed=42)
        return {f: hashlib.sha256((Path(d) / f).read_bytes()).hexdigest()
                for f in _EXPECTED}
    h1 = run_to("/tmp/integ_a")
    h2 = run_to("/tmp/integ_b")
    diffs = [f for f in _EXPECTED if h1[f] != h2[f]]
    assert not diffs, f"non-deterministic outputs: {diffs}"


ALL_TESTS = [
    test_integration_completes_with_all_outputs,
    test_new_gate_records_never_pass,
    test_mode_c_readiness_blocks_mode_d,
    test_integration_two_run_byte_identical,
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
