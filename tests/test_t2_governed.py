"""Governed-task writers decide against the history they append onto.

D-2026-156, the governed half. Two writers of the task runner were not
decided under the writer lock:

* THE EXECUTION RECORD. The tool runs outside any lock, for as long as it
  takes, and the task can move meanwhile: a supervisor that took the worker
  for dead returns it to the queue, a submitter cancels it. The record was
  appended unchecked when the tool returned. Measured at b9b2187: the stale
  worker's record was written, its own COMPLETED move then failed, and the
  history no longer replayed for anybody ("an execution record for ...
  while it is QUEUED").
* THE COMPENSATION. A person answering COMPENSATE authorizes one undo of an
  external effect. Nothing consumed the answer. Measured at b9b2187:
  compensate() called twice with one answer ran the external undo twice,
  and two supervisors each holding the answer would both have run it.

The execution record is now decided by the replay's own rule against the
head it is written onto. A compensation is now PREPARED -- an authorization
record decided under the lock, which consumes the answer -- then RUN outside
any lock, then COMMITTED by a record that cites the authorization. A crash
between the two leaves an authorization with no outcome: whether the undo ran
is not known, and it is not run again without a new decision.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import test_agent_compensation as C  # noqa: E402
from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate.agents import AgentError  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_tasks import (  # noqa: E402
    ACT_COMPENSATION, ACT_COMPENSATION_AUTHORIZE, GovernedTasks,
)
from agent_substrate.tasks import (Lease, TaskRole, TaskState,  # noqa: E402
                                   TaskTransitionError)

WS = "verification/stage10/_pytest_t2_governed"


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedTasks(root=ROOT, log=EventLog(base / "log.jsonl"),
                      evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _second(gov, *, undo: bool = False) -> GovernedTasks:
    """Another supervisor on the same log, holding its own view of it."""
    other = GovernedTasks(root=ROOT, log=EventLog(gov.log.path),
                          evidence=gov.evidence)
    other.out_rel = gov.out_rel
    if undo:
        C._external_with_undo(other)
    return other


def _actions(gov, action):
    report, events = gov.log.read_verified()
    report.raise_if_bad()
    return [e for e in events if e.action == action]


def _task_id(gov) -> str:
    (create,) = _actions(gov, "task.create")
    return create.payload["task_id"]


# ---- the execution record ---------------------------------------------------

def _moved_while_running(gov, monkeypatch, move):
    real_run = type(gov.executor).run

    def run_then_move(self, **kw):
        result = real_run(self, **kw)
        other = _second(gov)
        move(other, other.projection().get(kw["task_id"]), kw["actor"])
        return result

    monkeypatch.setattr(type(gov.executor), "run", run_then_move)


def _recover(other, task, worker):
    other._move(task, TaskState.QUEUED, "system", TaskRole.SYSTEM,
                note="its worker looked dead from here")


def test_an_execution_record_for_a_task_recovered_meanwhile_is_refused(
        gov, monkeypatch):
    _moved_while_running(gov, monkeypatch, _recover)
    with pytest.raises(TaskTransitionError, match="moved while its tool ran"):
        gov.run(tool_id="stage10.emit_artifact", inputs=C._inputs(gov))
    monkeypatch.undo()
    # The history replays, the task is where the recovery put it, and no
    # record names the stale worker as its executor.
    assert _second(gov).projection().get(_task_id(gov)).state \
        is TaskState.QUEUED
    assert not _actions(gov, "task.execution")
    assert not rc.reconstruct_tasks(gov.log, reauthorize=False).anomalies


def test_an_execution_record_under_a_lease_since_replaced_is_refused(
        gov, monkeypatch):
    """The same worker identity, holding the task again under a NEW lease.

    The replay's rule asks only that the record's actor holds the lease, so
    it would accept this record -- and attribute the first attempt's result
    to the second. The writer asks which lease it ran under."""
    def requeue_and_relet(other, task, worker):
        _recover(other, task, worker)
        task = other.projection().get(task.task_id)
        head = other.log.verify().head_seq
        lease = Lease(lease_id="lease-relet", holder=worker,
                      granted_seq=head, expires_after_seq=head + 500)
        task = other._move(task, TaskState.LEASED, worker, TaskRole.WORKER,
                           lease=lease)
        other._move(task, TaskState.EXECUTING, worker, TaskRole.WORKER,
                    lease_id=lease.lease_id)

    _moved_while_running(gov, monkeypatch, requeue_and_relet)
    with pytest.raises(TaskTransitionError, match="moved while its tool ran"):
        gov.run(tool_id="stage10.emit_artifact", inputs=C._inputs(gov))
    monkeypatch.undo()
    assert not _actions(gov, "task.execution")
    task = _second(gov).projection().get(_task_id(gov))
    assert (task.state, task.lease.lease_id) == (TaskState.EXECUTING,
                                                 "lease-relet")


def test_an_execution_record_for_a_task_finished_under_its_lease_is_refused(
        gov, monkeypatch):
    """The lease alone does not say the task is still running.

    A non-terminal outcome keeps the lease: another process holding this
    worker's identity reports the task FAILED under the same lease while
    the tool runs. The lease matches; the replay's own rule -- an execution
    record only in EXECUTING -- is what refuses the record."""
    def reported_failed(other, task, worker):
        other._move(task, TaskState.FAILED, worker, TaskRole.WORKER,
                    lease_id=task.lease.lease_id,
                    note="another process holding this identity reported it")

    _moved_while_running(gov, monkeypatch, reported_failed)
    with pytest.raises(TaskTransitionError, match="moved while its tool ran"):
        gov.run(tool_id="stage10.emit_artifact", inputs=C._inputs(gov))
    monkeypatch.undo()
    assert not _actions(gov, "task.execution")
    assert _second(gov).projection().get(_task_id(gov)).state \
        is TaskState.FAILED


def test_control_an_execution_nobody_moved_meanwhile_is_recorded(gov):
    run = gov.run(tool_id="stage10.emit_artifact", inputs=C._inputs(gov))
    assert run.state is TaskState.VERIFIED, run.reason
    (ex,) = _actions(gov, "task.execution")
    assert ex.payload["task_id"] == run.task_id
    assert not rc.reconstruct_tasks(gov.log, reauthorize=False).anomalies


# ---- the compensation -------------------------------------------------------

def _answered(gov, monkeypatch, *, eid=None):
    run = C._timed_out_run(gov, monkeypatch)
    who = C._human(gov)
    gov.agents.answer(escalation_id=run.escalation_id, answered_by=who,
                      answer="COMPENSATE", reason="the record was published")
    return run


def _counting_undos(monkeypatch, gov):
    ran = []
    real_run = type(gov.executor).run

    def counted(self, **kw):
        ran.append(kw["tool_id"])
        return real_run(self, **kw)

    monkeypatch.setattr(type(gov.executor), "run", counted)
    return ran


def test_one_answer_runs_one_undo(gov, monkeypatch):
    run = _answered(gov, monkeypatch)
    ran = _counting_undos(monkeypatch, gov)
    gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    with pytest.raises(AgentError, match="already authorized"):
        gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    assert ran == ["stage10.revoke_artifact"]
    (auth,) = _actions(gov, ACT_COMPENSATION_AUTHORIZE)
    (done,) = _actions(gov, ACT_COMPENSATION)
    assert done.payload["authorization_seq"] == auth.seq


def test_a_second_supervisor_holding_the_answer_does_not_run_it_again(
        gov, monkeypatch):
    run = _answered(gov, monkeypatch)
    other = _second(gov, undo=True)
    assert other.agents.escalation(run.escalation_id).answer == "COMPENSATE"
    ran = _counting_undos(monkeypatch, gov)
    gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    with pytest.raises(AgentError, match="already authorized"):
        other.compensate(task_id=run.task_id,
                         escalation_id=run.escalation_id)
    assert ran == ["stage10.revoke_artifact"]
    _second(gov).projection()


def test_an_answer_given_after_this_view_was_loaded_authorizes(
        gov, monkeypatch):
    """Decided against the history, not the view: a supervisor that loaded
    before the person answered acts on the answer rather than refusing it
    as unanswered."""
    run = C._timed_out_run(gov, monkeypatch)
    other = _second(gov, undo=True)
    who = C._human(gov)
    gov.agents.answer(escalation_id=run.escalation_id, answered_by=who,
                      answer="COMPENSATE", reason="the record was published")
    out = other.compensate(task_id=run.task_id,
                           escalation_id=run.escalation_id)
    assert out["answered_by"] == who


def test_an_undo_interrupted_after_its_authorization_is_not_run_again(
        gov, monkeypatch):
    run = _answered(gov, monkeypatch)

    def died(self, **kw):
        raise SystemExit("the supervisor died while the undo ran")

    with monkeypatch.context() as m:
        m.setattr(type(gov.executor), "run", died)
        with pytest.raises(SystemExit):
            gov.compensate(task_id=run.task_id,
                           escalation_id=run.escalation_id)
    with pytest.raises(AgentError, match="already authorized"):
        _second(gov, undo=True).compensate(task_id=run.task_id,
                                           escalation_id=run.escalation_id)
    (auth,) = _actions(gov, ACT_COMPENSATION_AUTHORIZE)
    assert not _actions(gov, ACT_COMPENSATION)
    sub = rc.reconstruct_subsystems(gov.log)
    assert sub.compensation_authorizations[run.escalation_id][
        "ran_seq"] is None, "an authorization with no outcome is UNCERTAIN"
    assert not sub.anomalies


def test_control_a_new_decision_authorizes_a_new_undo(gov, monkeypatch):
    run = _answered(gov, monkeypatch)
    gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    raised_by = gov.agents.escalation(run.escalation_id).raised_by
    gov.agents.escalate(escalation_id="esc-again", task_id=run.task_id,
                        question="the undo reported success and the record "
                                 "is still public. Run it again?",
                        raised_by=raised_by,
                        options=("COMPENSATE", "ACCEPT_AS_DONE"))
    gov.agents.answer(escalation_id="esc-again", answered_by="operator-1",
                      answer="COMPENSATE", reason="still public")
    gov.compensate(task_id=run.task_id, escalation_id="esc-again")
    assert len(_actions(gov, ACT_COMPENSATION)) == 2
    _second(gov).projection()
    assert not rc.reconstruct_subsystems(gov.log).anomalies


def test_an_authorization_of_an_answer_to_leave_it_is_refused_by_both(
        gov, monkeypatch):
    """The answer is the authority. A person who answered ACCEPT_AS_DONE
    authorized no undo, and a record claiming otherwise -- written outside
    compensate(), which refuses it -- is refused on replay by the primary
    and reported by the second reader."""
    run = C._timed_out_run(gov, monkeypatch)
    who = C._human(gov)
    gov.agents.answer(escalation_id=run.escalation_id, answered_by=who,
                      answer="ACCEPT_AS_DONE", reason="it is fine as it is")
    with pytest.raises(AgentError, match="not 'COMPENSATE'"):
        gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    gov.log.append(
        actor="system", action=ACT_COMPENSATION_AUTHORIZE, target=run.task_id,
        payload={"task_id": run.task_id,
                 "authorized_by_escalation": run.escalation_id,
                 "answered_by": who,
                 "compensated_tool": "stage10.emit_artifact",
                 "compensating_tool": "stage10.revoke_artifact"})
    with pytest.raises(TaskTransitionError, match="COMPENSATE"):
        _second(gov, undo=True).projection()
    assert any("answered COMPENSATE" in a
               for a in rc.reconstruct_subsystems(gov.log).anomalies)


def test_a_second_authorization_of_one_answer_is_refused_on_replay(
        gov, monkeypatch):
    run = _answered(gov, monkeypatch)
    gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    (auth,) = _actions(gov, ACT_COMPENSATION_AUTHORIZE)
    gov.log.append(actor=auth.actor, action=ACT_COMPENSATION_AUTHORIZE,
                   target=auth.target, payload=auth.payload)
    with pytest.raises(TaskTransitionError, match="already authorized"):
        _second(gov).projection()
    assert any("already authorized" in a
               for a in rc.reconstruct_subsystems(gov.log).anomalies)


def test_an_undo_recorded_without_its_authorization_is_refused_on_replay(
        gov, monkeypatch):
    run = _answered(gov, monkeypatch)
    gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    (done,) = _actions(gov, ACT_COMPENSATION)
    forged = {k: v for k, v in done.payload.items()
              if k != "authorization_seq"}
    gov.log.append(actor=done.actor, action=ACT_COMPENSATION,
                   target=done.target, payload=forged)
    with pytest.raises(TaskTransitionError, match="authorization"):
        _second(gov).projection()
    assert any("authorization" in a
               for a in rc.reconstruct_subsystems(gov.log).anomalies)


def test_a_second_outcome_for_one_authorization_is_refused_on_replay(
        gov, monkeypatch):
    run = _answered(gov, monkeypatch)
    gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    (done,) = _actions(gov, ACT_COMPENSATION)
    gov.log.append(actor=done.actor, action=ACT_COMPENSATION,
                   target=done.target, payload=done.payload)
    with pytest.raises(TaskTransitionError, match="already recorded"):
        _second(gov).projection()
    assert any("already recorded" in a
               for a in rc.reconstruct_subsystems(gov.log).anomalies)


# ---- the governed read ------------------------------------------------------

def test_a_read_whose_grant_is_revoked_while_it_reads_is_refused(
        gov, monkeypatch):
    """Measured at b9b2187: the verifier's read was recorded ALLOWED under a
    grant another process revoked while the bytes were being read ("an
    allowed read cites capability ..., which was revoked at seq 20"). The
    run was rejected only afterwards, by the separate-process check reading
    that record back; the history itself states an allowed read that was
    not. Now the record is decided against the head: refused, and no
    reader finds anything to flag."""
    import agent_substrate.readpath as RP
    from agent_substrate.capability import CapabilityLedger

    real = RP.ReadRoot.read
    revoked: list = []

    def revoked_meanwhile(self, resource, **kw):
        if not revoked:
            led = CapabilityLedger(EventLog(gov.log.path)).load()
            (cid,) = [c for c in led.issued_ids()
                      if c.startswith("cap-read-")]
            led.revoke(cid, actor="scheduler",
                       reason="withdrawn while it read")
            revoked.append(cid)
        return real(self, resource, **kw)

    monkeypatch.setattr(RP.ReadRoot, "read", revoked_meanwhile)
    run = gov.run(tool_id="stage10.emit_artifact", inputs=C._inputs(gov))
    monkeypatch.undo()
    assert run.state is TaskState.REJECTED, run.reason
    assert "withdrawn while the read ran" in run.reason
    reads = [e for e in _actions(gov, "file.read")
             if e.payload["capability_id"] == revoked[0]]
    assert reads and not any(r.payload["allowed"] for r in reads)
    assert not rc.reconstruct_subsystems(gov.log).anomalies


def test_control_a_read_nobody_revoked_is_recorded_allowed(gov):
    run = gov.run(tool_id="stage10.emit_artifact", inputs=C._inputs(gov))
    assert run.state is TaskState.VERIFIED, run.reason
    reads = _actions(gov, "file.read")
    assert reads and all(r.payload["allowed"] for r in reads)
    assert not rc.reconstruct_subsystems(gov.log).anomalies
