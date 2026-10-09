"""R49's two kinds of measurement, kept apart by the tool that publishes
them: deterministic work counters gate at an inclusive invariant on any
host; timing shapes gate only at a gross bound and are published as
telemetry; nothing unclassed, nothing vacuous, nothing appended to a
retired series."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import performance_baseline as PB  # noqa: E402

D, T = "DETERMINISTIC_WORK", "ENVIRONMENT_SENSITIVE_TIMING"


def _m(value, ceiling, kind, guard="g"):
    return {"guard": guard, "value": value, "ceiling": ceiling, "kind": kind}


def test_a_deterministic_ceiling_is_an_inclusive_invariant():
    assert not PB.over(_m(0, 0, D))
    assert PB.over(_m(1, 0, D))
    assert not PB.over(_m(11, 11, D))


def test_a_timing_ceiling_is_an_exclusive_gross_bound():
    assert not PB.over(_m(1.44, 1.45, T))
    assert PB.over(_m(1.45, 1.45, T))


def test_telemetry_classes_every_measurement():
    tel = PB.telemetry([_m(0, 0, D), _m(1.0, 1.45, T)], [])
    assert tel["counts"] == {D: 1, T: 1}
    assert tel["unclassified"] == []
    assert tel["host"]["proc_available"] in (True, False)
    tel = PB.telemetry([{"guard": "x", "value": 1, "ceiling": 2}], [])
    assert tel["unclassified"] == ["x"]


def test_the_committed_history_holds_only_timing_and_retires_with_reason():
    doc = PB._load()
    assert "governed_operation_vs_history" in doc["retired"]
    assert "governed_operation_vs_history" not in doc["guards"]
    for g in doc["guards"].values():
        assert g["observations"]


@pytest.mark.parametrize("value, ceiling, kind, flagged", [
    (0, 0, D, False), (2, 1, D, True), (1.0, 3.0, T, False),
    (3.0, 3.0, T, True)])
def test_report_flags_by_kind(value, ceiling, kind, flagged, capsys):
    probs = PB.report({"guards": {}}, [_m(value, ceiling, kind)])
    assert bool(probs) is flagged
