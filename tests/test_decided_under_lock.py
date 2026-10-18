"""A decision is taken under the writer lock: another writer waits for it.

D-2026-156. The race tests beside this one load a stale view and decide
after the other writer has FINISHED, so a writer that re-read the log and
then appended WITHOUT the lock would pass every one of them. These open the
window itself. Writer A pauses after its last read and before its write;
writer B, on another thread, takes the competing decision. Under the lock B
waits for A, then decides against A's record and is refused. With the
decision moved outside the lock B writes in the gap, and the history carries
two records of one decision -- which is what each mutation in
tools/mutations/t2_closure.json that moves a decision out of the lock does.

Each case has a control in which B's write does not compete with A's, so
the pause alone is shown to refuse nothing.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import test_agent_compensation as C  # noqa: E402
import test_t2_closure as T2  # noqa: E402
from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate.capability import (ACT_REVOKE, Action,  # noqa: E402
                                        CapabilityLedger, CapabilityRevoked,
                                        issue)
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.governed_tasks import (  # noqa: E402
    ACT_COMPENSATION_AUTHORIZE, CompensationAlreadyAuthorized,
)
from agent_substrate.tasks import TaskRole, TaskState  # noqa: E402
from test_t2_governed import _actions, _answered, _second  # noqa: E402
from test_t2_governed import gov  # noqa: E402,F401 - the fixture

#: How long A holds its decision open. Under the lock B cannot finish in
#: this time; without it B finishes in milliseconds.
HOLD_S = 1.0


class _Gate:
    """Pause the first call that comes from ``only_from``, after it returns."""

    def __init__(self, only_from: str):
        self.only_from = only_from
        self.entered = threading.Event()
        self.release = threading.Event()

    def wrap(self, real):
        def held(*args, **kwargs):
            out = real(*args, **kwargs)
            if sys._getframe(1).f_code.co_name == self.only_from and \
                    not self.entered.is_set():
                self.entered.set()
                self.release.wait(HOLD_S)
            return out
        return held


def _race(gate: _Gate, first, second) -> dict:
    errors: dict = {}

    def run(name, fn):
        try:
            fn()
        except BaseException as exc:          # noqa: BLE001 - reported
            errors[name] = exc

    a = threading.Thread(target=run, args=("first", first))
    a.start()
    assert gate.entered.wait(30), "the first writer never reached its decision"
    b = threading.Thread(target=run, args=("second", second))
    b.start()
    b.join(HOLD_S / 2)            # under the lock, B is still waiting here
    gate.release.set()
    a.join(60)
    b.join(60)
    assert not a.is_alive() and not b.is_alive()
    return errors


# ---- the decided-writer mixin: capability, egress, secrets ------------------

def _two_ledgers(tmp_path):
    p = tmp_path / "log.jsonl"
    T2._caps(p).issue(issue(capability_id="c2", subject=T2.ACTOR,
                            action=Action.EXECUTE_TOOL, task_id=T2.TASK,
                            tool_id=T2.TOOL, scope=("out",), issued_seq=0),
                      actor="root")
    a = CapabilityLedger(EventLog(p)).load()
    b = CapabilityLedger(EventLog(p)).load()
    gate = _Gate("under_lock")
    a._fold_new = gate.wrap(a._fold_new)
    return p, a, b, gate


def test_a_second_revocation_waits_for_the_first_and_is_refused(tmp_path):
    p, a, b, gate = _two_ledgers(tmp_path)
    errors = _race(gate,
                   lambda: a.revoke("c1", actor="root", reason="first"),
                   lambda: b.revoke("c1", actor="root", reason="second"))
    assert "first" not in errors, errors
    assert isinstance(errors.get("second"), CapabilityRevoked), errors
    _, events = EventLog(p).read_verified()
    assert [e.target for e in events if e.action == ACT_REVOKE] == ["c1"]


def test_control_a_revocation_of_another_grant_is_written_after_it(tmp_path):
    p, a, b, gate = _two_ledgers(tmp_path)
    errors = _race(gate,
                   lambda: a.revoke("c1", actor="root", reason="first"),
                   lambda: b.revoke("c2", actor="root", reason="second"))
    assert errors == {}
    _, events = EventLog(p).read_verified()
    assert sorted(e.target for e in events if e.action == ACT_REVOKE) == [
        "c1", "c2"]


# ---- the execution record ----------------------------------------------------

def _recover_meanwhile(gov, gate, task_ids):
    def recover():
        other = _second(gov)
        task = other.projection().get(task_ids[0])
        other._move(task, TaskState.QUEUED, "system", TaskRole.SYSTEM,
                    note="its worker looked dead from here")
    return recover


def test_a_recovery_waits_for_the_execution_record_it_would_contradict(
        gov, monkeypatch):
    gate = _Gate("decide_execution")
    monkeypatch.setattr(gov, "_projection_at_head",
                        gate.wrap(gov._projection_at_head))
    task_ids: list = []

    def first():
        gov.run(tool_id="stage10.emit_artifact", inputs=C._inputs(gov))

    def second():
        (create,) = _actions(gov, "task.create")
        task_ids.append(create.payload["task_id"])
        _recover_meanwhile(gov, gate, task_ids)()

    _race(gate, first, second)
    # Whatever each writer was told, the history replays for everybody:
    # the recovery decided against the execution record, not before it.
    _second(gov).projection()
    assert len(_actions(gov, "task.execution")) == 1


def test_control_an_execution_with_no_competing_move_is_verified(
        gov, monkeypatch):
    gate = _Gate("decide_execution")
    monkeypatch.setattr(gov, "_projection_at_head",
                        gate.wrap(gov._projection_at_head))
    out: dict = {}
    errors = _race(gate,
                   lambda: out.update(run=gov.run(
                       tool_id="stage10.emit_artifact",
                       inputs=C._inputs(gov))),
                   lambda: _second(gov).projection())
    assert errors == {}
    assert out["run"].state is TaskState.VERIFIED, out["run"].reason


# ---- the compensation --------------------------------------------------------

def test_a_second_supervisor_waits_for_the_authorization_and_is_refused(
        gov, monkeypatch):
    run = _answered(gov, monkeypatch)
    other = _second(gov, undo=True)
    gate = _Gate("prepare")
    monkeypatch.setattr(gov, "projection", gate.wrap(gov.projection))
    errors = _race(
        gate,
        lambda: gov.compensate(task_id=run.task_id,
                               escalation_id=run.escalation_id),
        lambda: other.compensate(task_id=run.task_id,
                                 escalation_id=run.escalation_id))
    assert "first" not in errors, errors
    assert isinstance(errors.get("second"), CompensationAlreadyAuthorized), \
        errors
    assert len(_actions(gov, ACT_COMPENSATION_AUTHORIZE)) == 1
    _second(gov).projection()


# ---- the governed read -------------------------------------------------------

def test_a_revocation_waits_for_the_read_it_would_contradict(
        gov, monkeypatch):
    import agent_substrate.readpath as RP

    gate = _Gate("decide")
    real = RP.GovernedReader._grants

    # Patched on the class -- the reader is built inside the verifier -- so
    # the gate is spelled out rather than wrapped around a bound method.
    def held(self, at_seq):
        out = real(self, at_seq)
        if sys._getframe(1).f_code.co_name == "decide" and \
                not gate.entered.is_set():
            gate.entered.set()
            gate.release.wait(HOLD_S)
        return out

    monkeypatch.setattr(RP.GovernedReader, "_grants", held)

    def revoke():
        led = CapabilityLedger(EventLog(gov.log.path)).load()
        (cid,) = [c for c in led.issued_ids() if c.startswith("cap-read-")]
        led.revoke(cid, actor="scheduler", reason="withdrawn while it read")

    _race(gate,
          lambda: gov.run(tool_id="stage10.emit_artifact",
                          inputs=C._inputs(gov)),
          revoke)
    found = [a for a in rc.reconstruct_subsystems(gov.log).anomalies
             if "allowed read" in a]
    assert not found, found
