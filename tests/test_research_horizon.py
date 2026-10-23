"""Gate G: the long-horizon campaign's counters, kept apart and held to
each other.

The full staged campaign (to ten thousand scripted decisions) is
tools/research_horizon.py; its measurements are in
docs/assurance/research_horizon_results.json. Here one short stage runs,
with a crash and a resumption in it, and the invariants that must hold over
any length are checked:

* every scripted decision is one recorded inference call, none lost and
  none counted twice, across the crash;
* every governed run is one run and one independent check, and each
  verified record is one of them;
* nothing was executed, received or answered twice;
* every objective reached a terminal state; none needed a second rescue;
* the second reader agrees, the log verifies, and a checkpointed load
  reaches the same state as a full one and says it is weaker.

The agent is a script; its decisions are scripted decisions.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import research_horizon as H  # noqa: E402


@pytest.fixture(scope="module")
def stage():
    c = H.Campaign("pytest-horizon")
    try:
        out = c.run(50)
    finally:
        c.clean()
    return out["stages"][-1], out


def test_the_counters_are_kept_apart_and_agree(stage):
    s, _ = stage
    n = s["counters"]
    assert n["scripted_decisions"] >= 50
    # one recorded call per decision, across the crash and its resumption
    assert n["inference_calls"] == n["scripted_decisions"]
    # a governed run is the run and its independent check
    assert n["governed_operations"] == 2 * n["verified_records"]
    assert n["conclusions"] == s["objective_states"].get("CONCLUDED", 0)
    assert n["restarts"] >= 1
    assert n["resets"] == n["objectives"] + n["restarts"]


def test_nothing_happened_twice_and_nothing_was_lost(stage):
    s, _ = stage
    assert s["duplicates"] == {"executions": 0, "receipts": 0,
                               "inference_results": 0}
    assert s["recovery_failures"] == 0
    assert set(s["objective_states"]) <= {"CONCLUDED", "ABSTAINED"}


def test_the_history_holds_up(stage):
    s, _ = stage
    assert s["log_verifies"] and s["second_reader_anomalies"] == 0
    assert s["checkpoint"] == {"equal_state": True,
                               "prefix_verified": False}


def test_the_decisions_are_said_to_be_scripted(stage):
    _, out = stage
    assert out["agent"].startswith("SCRIPTED")


def _ev(action, target, **payload):
    return SimpleNamespace(action=action, target=target, payload=payload)


def test_a_duplicate_is_seen_where_there_is_one():
    """The duplicate counter's positive control: two completed executions
    of one task, two receipts of one proposal, two results for one attempt
    -- and the attempts a retry is allowed are not counted as duplicates."""
    events = [_ev("task.execution", "t1", phase="complete"),
              _ev("task.execution", "t1", phase="complete"),
              _ev("task.execution", "t2", phase="started"),
              _ev("task.execution", "t2", phase="complete"),
              _ev("proposal.receive", "p1"), _ev("proposal.receive", "p1"),
              _ev("inference.result", "r1", attempt=1),
              _ev("inference.result", "r1", attempt=1),
              _ev("inference.result", "r2", attempt=1),
              _ev("inference.result", "r2", attempt=2)]
    assert H._duplicates(events) == {"executions": 1, "receipts": 1,
                                     "inference_results": 1}
