"""An execution record is accepted only where the lifecycle has a place for it.

The record is what establishes WHO RAN a task, and the executor is what
verification must differ from. Every reader folded it wherever it appeared:
appended after the verdict it renamed the executor of work already judged,
and the production projection and the independent reconstruction agreed with
each other about the renamed one -- a shared defect, which a differential
comparison cannot see. The auditor had it too, in its separation-of-duties
check.

The governed runner writes the record at exactly one point: in EXECUTING, by
the worker holding the lease, between the tool's run and its outcome. So the
rule each reader states is that, and nothing wider: the production projection
refuses (``tasks.check_execution``), the reconstruction restates it and
records the refusal, the auditor reports it as a gap and judges the executor
as of the verdict.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.audit import AuditIndex  # noqa: E402
from qta_agent.canonical import digest_bytes  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_EXECUTION, ACT_TASK_TRANSITION, SUBMITTER_ID, VERIFIER_ID, WORKER_ID,
    GovernedStage10,
)
from qta_agent.reconstruct import (  # noqa: E402
    compare_tasks, reconstruct_tasks,
)
from qta_agent.tasks import TaskTransitionError  # noqa: E402

WS = "verification/stage10/_pytest_execution_phase"


@pytest.fixture
def gov(request):
    name = request.node.name.replace("/", "_").replace("[", "_")[:60]
    name = name.replace("]", "")
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)
    if (ROOT / WS).exists() and not any((ROOT / WS).iterdir()):
        (ROOT / WS).rmdir()


def _run(gov):
    return gov.run(tool_id="stage10.emit_artifact",
                   inputs={"out_dir": gov.out_rel, "name": "a.json",
                           "payload": {"v": 1}},
                   submitter=SUBMITTER_ID, worker=WORKER_ID,
                   verifier=VERIFIER_ID)


def _execution(gov, task_id: str):
    (ev,) = [e for e in gov.log.read_verified()[1]
             if e.action == ACT_EXECUTION and e.payload["task_id"] == task_id]
    return ev


# ---- the control: where the governed runner writes it ----------------------

def test_the_governed_runner_writes_it_in_executing_by_the_lease_holder(gov):
    run = _run(gov)
    events = gov.log.read_verified()[1]
    ex = _execution(gov, run.task_id)
    moves = [e for e in events if e.action == ACT_TASK_TRANSITION
             and e.payload["task_id"] == run.task_id]
    before = [e for e in moves if e.seq < ex.seq][-1]
    after = [e for e in moves if e.seq > ex.seq][0]
    assert before.payload["dst"] == "EXECUTING"
    assert after.payload["src"] == "EXECUTING"
    assert ex.actor == WORKER_ID == before.actor
    recon = reconstruct_tasks(gov.log)
    assert not recon.unauthorized and not recon.anomalies
    assert compare_tasks(gov.projection(), recon) == ()
    gaps = AuditIndex.from_log(gov.log).explain_task(run.task_id).gaps
    assert not any("execution record by" in g for g in gaps), gaps


# ---- after the verdict ------------------------------------------------------

@pytest.mark.parametrize("actor", [VERIFIER_ID, "mallory", WORKER_ID])
def test_an_execution_record_after_the_verdict_is_refused(gov, actor):
    """The forgery the rule exists for. By the verifier it would make the
    verdict look self-verified after the fact; by a stranger it names an
    executor nobody leased the work to; by the real worker it is a second
    run of work already judged. Refused by every reader, and the executor
    stays who ran it."""
    run = _run(gov)
    genuine = _execution(gov, run.task_id)
    gov.log.append(actor=actor, action=ACT_EXECUTION, target=run.task_id,
                   payload=dict(genuine.payload))

    with pytest.raises(TaskTransitionError, match="while it is VERIFIED"):
        gov.projection()

    recon = reconstruct_tasks(gov.log)
    assert any("execution record while VERIFIED" in u
               for u in recon.unauthorized), recon.unauthorized
    task = recon.tasks[run.task_id]
    assert task["executed_by"] == WORKER_ID
    assert task["executions"] == [(genuine.seq, WORKER_ID)]
    assert task["state"] == "VERIFIED"

    gaps = AuditIndex.from_log(gov.log).explain_task(run.task_id).gaps
    assert any(f"execution record by {actor!r}" in g and "'VERIFIED'" in g
               for g in gaps), gaps


# ---- hand-built lifecycles: the other phases, and the holder ---------------

def _lifecycle(tmp_path, *, execution_at: str, executor: str = "w",
               verifier: str = "v", late: str | None = None):
    """A task walked through the governed lifecycle by hand, with the
    execution record placed at ``execution_at`` (the state the task is in
    when it is appended) and written by ``executor``; ``late`` appends a
    second one, after the verdict, by that actor."""
    log = EventLog(tmp_path / "log.jsonl")
    tid, dg = "t-phase", digest_bytes(b"r")

    def tr(src, dst, role, actor, **extra):
        payload = {"task_id": tid, "src": src, "dst": dst, "role": role}
        payload.update(extra)
        log.append(actor=actor, action=ACT_TASK_TRANSITION, target=tid,
                   payload=payload)

    def execution(actor):
        log.append(actor=actor, action=ACT_EXECUTION, target=tid,
                   payload={"task_id": tid, "result_digest": dg,
                            "outcome": "COMPLETED", "tool_id": "probe"})

    log.append(actor="alice", action="task.create", target=tid,
               payload={"task_id": tid, "tool_id": "probe",
                        "submitter": "alice", "inputs_digest": dg})
    tr("CREATED", "VALIDATED", "SUBMITTER", "alice")
    tr("VALIDATED", "QUEUED", "SCHEDULER", "sched")
    if execution_at == "QUEUED":
        execution(executor)
    tr("QUEUED", "LEASED", "WORKER", "w",
       lease={"lease_id": "L1", "holder": "w", "granted_seq": 3,
              "expires_after_seq": 9999})
    if execution_at == "LEASED":
        execution(executor)
    tr("LEASED", "EXECUTING", "WORKER", "w", lease_id="L1")
    if execution_at == "EXECUTING":
        execution(executor)
    tr("EXECUTING", "COMPLETED", "WORKER", "w", lease_id="L1",
       result_digest=dg)
    tr("COMPLETED", "VERIFIED", "VERIFIER", verifier)
    if late is not None:
        execution(late)
    return log, tid


def _projection(tmp_path, log):
    return GovernedStage10(root=ROOT, log=log,
                           evidence=EvidenceStore(tmp_path / "evidence")
                           ).projection()


def test_by_the_holder_in_executing_both_readers_accept(tmp_path):
    """The hand-built control: the same builder the refusals use, with the
    record where the runner puts it."""
    log, tid = _lifecycle(tmp_path, execution_at="EXECUTING")
    live = _projection(tmp_path, log)
    recon = reconstruct_tasks(log)
    assert live.get(tid).executed_by == "w"
    assert live.get(tid).state.value == "VERIFIED"
    assert not recon.unauthorized and not recon.anomalies
    assert compare_tasks(live, recon) == ()


@pytest.mark.parametrize("phase", ["QUEUED", "LEASED"])
def test_before_executing_both_readers_refuse(tmp_path, phase):
    """Before the task is running there is nothing to have run. Recorded
    by the eventual holder, even: a record ahead of EXECUTING would name the
    executor of work that has not started."""
    log, tid = _lifecycle(tmp_path, execution_at=phase)
    with pytest.raises(TaskTransitionError, match=f"while it is {phase}"):
        _projection(tmp_path, log)
    recon = reconstruct_tasks(log)
    assert any(f"execution record while {phase}" in u
               for u in recon.unauthorized), recon.unauthorized
    # Refused, so nothing records who ran it, and the verdict that needed an
    # executor to differ from is refused for that honest reason.
    assert recon.tasks[tid]["executed_by"] is None
    assert recon.verified_ids() == ()


def test_by_anyone_but_the_holder_both_readers_refuse(tmp_path):
    """In EXECUTING, but not by the worker holding the lease: the record
    names its writer as the executor of work another actor was given."""
    log, tid = _lifecycle(tmp_path, execution_at="EXECUTING",
                          executor="mallory")
    with pytest.raises(TaskTransitionError, match="lease is held by 'w'"):
        _projection(tmp_path, log)
    recon = reconstruct_tasks(log)
    assert any("the lease is held by 'w'" in u for u in recon.unauthorized)
    assert recon.tasks[tid]["executed_by"] is None


def test_the_auditor_judges_the_executor_as_of_the_verdict(tmp_path):
    """The third reader. A history where one actor ran and verified its own
    work is a finding; a record appended afterwards naming somebody else
    used to silence it, because the auditor took the LAST execution record
    as the executor."""
    log, tid = _lifecycle(tmp_path, execution_at="EXECUTING", verifier="w",
                          late="someone-else")
    gaps = AuditIndex.from_log(log).explain_task(tid).gaps
    assert any("executor and verifier are both 'w'" in g for g in gaps), gaps
    assert any("execution record by 'someone-else'" in g for g in gaps), gaps


def test_the_auditor_does_not_invent_self_verification_either(tmp_path):
    """The other direction: an honest verdict, then a record by the
    verifier. Taking the last record as the executor would report the
    verifier as having verified its own work."""
    log, tid = _lifecycle(tmp_path, execution_at="EXECUTING", late="v")
    gaps = AuditIndex.from_log(log).explain_task(tid).gaps
    assert not any("executor and verifier" in g for g in gaps), gaps
    assert any("execution record by 'v'" in g for g in gaps), gaps
