"""Crash injection at every boundary a transaction can be interrupted at.

WHAT "CRASH" MEANS HERE

Every in-memory object is abandoned and the state is rebuilt from the log
alone. That is the strongest form of the test available without killing a
process, because it asserts the property that actually matters: nothing the
system knows may live only in a projection. Where a real process is needed --
a worker that dies holding a lease -- a subprocess is used and killed.

THE PROPERTY UNDER TEST, AT EVERY BOUNDARY

After the crash, the recovered state must be exactly what the durable record
implies, and must never be MORE advanced than it. A task that was executing
when the machine died must not come back verified; a job whose lease lapsed
must not come back owned; a verification that had not been recorded must not
come back recorded. The direction matters: losing progress is a cost, and
gaining it is a failure.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.agents import (  # noqa: E402
    AgentDirectory, AgentRole, PrincipalKind, identity,
)
from qta_agent.audit import AuditIndex  # noqa: E402
from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.capability import (  # noqa: E402
    Action, CapabilitySet, issue as issue_cap,
)
from qta_agent.checkpoint import CheckpointStore  # noqa: E402
from qta_agent.events import ChainBroken, EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore, UnknownEvidence  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.reconstruct import compare, reconstruct  # noqa: E402
from qta_agent.scheduler import (  # noqa: E402
    FailureClass, JobState, Scheduler, default_policy,
)
from qta_agent.store import AuthorityStore, StoreError  # noqa: E402

WORK = digest({"work": "stage10"})


class World:
    """Everything durable, rebuildable from the log by construction."""

    def __init__(self, root: Path):
        self.root = root
        self.log = EventLog(root / "log.jsonl")
        self.evidence = EvidenceStore(root / "evidence")
        self.checkpoints = CheckpointStore(root / "checkpoints")
        self.reload()

    def reload(self) -> "World":
        """THE crash. Every projection is discarded and rebuilt from the log."""
        self.log = EventLog(self.root / "log.jsonl")
        self.policy = PolicyStore(self.log).load()
        self.sched = Scheduler(self.log, policy=self.policy,
                               policy_id="scheduler.default",
                               capacity={"slots": 4})
        self.sched.load()
        self.store = AuthorityStore(self.log, evidence=self.evidence).load()
        self.agents = AgentDirectory(self.log).load()
        return self


@pytest.fixture()
def world(tmp_path):
    w = World(tmp_path)
    w.policy.publish(default_policy(), actor="owner")
    w.agents.register(identity(agent_id="proposer", instance_id="p1",
                               kind=PrincipalKind.AGENT,
                               roles={AgentRole.PROPOSER}), by="system")
    w.agents.register(identity(agent_id="worker", instance_id="w1",
                               kind=PrincipalKind.AGENT,
                               roles={AgentRole.EXECUTOR}), by="system")
    w.agents.register(identity(agent_id="verifier", instance_id="v1",
                               kind=PrincipalKind.AGENT,
                               roles={AgentRole.VERIFIER}), by="system")
    return w.reload()


# ---- 1. proposal created -> crash ---------------------------------------
def test_a_proposal_survives_a_crash_and_is_no_further_along(world):
    world.store.create(record_id="r1", kind="claim", proposer="p1")
    world.reload()
    rec = world.store.get("r1")
    assert rec.state is State.PROPOSED, (
        "recovery must not advance a record past what the log records")
    assert rec.proposer == "p1"


def test_a_crash_before_the_append_leaves_no_trace(world):
    """The append is the commit point. Before it, nothing happened."""
    before = world.log.verify().head_seq
    with pytest.raises(StoreError):
        world.store.create(record_id="r1", kind="claim", proposer="p1",
                           evidence={"report": "not-a-digest"})
    world.reload()
    assert world.log.verify().head_seq == before
    with pytest.raises(Exception):
        world.store.get("r1")


# ---- 2. queued -> crash --------------------------------------------------
def test_a_queued_job_comes_back_queued(world):
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.sched.reconcile()
    assert world.sched.get("j1").state is JobState.READY
    world.reload()
    assert world.sched.get("j1").state is JobState.READY
    assert world.sched.get("j1").attempts == 0


# ---- 3. lease granted -> the worker dies ---------------------------------
def test_a_dead_worker_s_lease_lapses_and_the_work_returns(world):
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.sched.reconcile()
    world.sched.dispatch(job_id="j1", worker="w1", lease_id="L1",
                         lease_seqs=2)
    world.reload()
    assert world.sched.get("j1").state is JobState.DISPATCHED, (
        "the lease is durable; a crash does not release it early")
    assert world.sched.get("j1").lease_holder == "w1"

    for i in range(4):
        world.sched.set_priority(job_id="j1", priority=9, actor="scheduler",
                                 role="SCHEDULER", reason=f"tick {i}")
    world.reload()
    world.sched.reconcile()
    job = world.sched.get("j1")
    assert job.state is JobState.READY
    assert job.lease_holder is None


def test_a_worker_process_killed_mid_execution_leaves_a_recoverable_log(
        tmp_path):
    """A real process, really killed, holding a real lease.

    The in-process version of this test cannot show that the log survives a
    SIGKILL between the fsync and the next statement; this one can.
    """
    log_path = tmp_path / "log.jsonl"
    script = tmp_path / "worker.py"
    script.write_text(f'''
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from qta_agent.events import EventLog
log = EventLog({str(log_path)!r})
for i in range(1000):
    log.append(actor="w1", action="probe", target="t", payload={{"i": i}})
    sys.stdout.write("x")
    sys.stdout.flush()
''', encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)],
                            stdout=subprocess.PIPE, start_new_session=True)
    try:
        assert proc.stdout.read(1) == b"x"
        time.sleep(0.05)
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=10)

    report = EventLog(log_path).verify()
    assert report.ok, report.problems[:3]
    assert report.count >= 1, "at least the first append must have survived"


# ---- 4. capability granted -> revoked ------------------------------------
def test_a_revoked_capability_blocks_a_job_that_was_already_ready(world):
    cap = issue_cap(capability_id="c1", subject="w1",
                    action=Action.EXECUTE_TOOL, task_id="t1",
                    tool_id="stage10.emit_artifact",
                    scope=("verification/stage10",), issued_seq=0)
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1",
                        requires_capability="c1", task_id="t1")
    live = CapabilitySet(issued={"c1": cap}, at_seq=world.sched.at_seq())
    world.sched.reconcile(capabilities=live)
    assert world.sched.get("j1").state is JobState.READY

    world.reload()
    revoked = CapabilitySet(issued={"c1": cap}, revoked=frozenset({"c1"}),
                            at_seq=world.sched.at_seq())
    world.sched.reconcile(capabilities=revoked)
    assert world.sched.get("j1").state is JobState.BLOCKED
    assert "revoked" in world.sched.get("j1").reason


# ---- 5-6. execution times out / dies before the completion record --------
def test_a_timed_out_attempt_is_retryable_and_a_crash_preserves_that(world):
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.sched.reconcile()
    world.sched.dispatch(job_id="j1", worker="w1", lease_id="L1",
                         lease_seqs=50)
    world.sched.report(job_id="j1", worker="w1",
                       failure=FailureClass.TIMEOUT, detail="wall bound")
    world.reload()
    job = world.sched.get("j1")
    assert job.state is JobState.RETRY_WAIT
    assert job.attempts == 1
    assert "TIMEOUT" in job.last_failure


def test_output_that_exists_without_a_completion_record_is_not_completed(
        world):
    """The dangerous asymmetry: bytes on disk are not a recorded outcome."""
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.sched.reconcile()
    world.sched.dispatch(job_id="j1", worker="w1", lease_id="L1",
                         lease_seqs=50)
    produced = world.evidence.put(b"the artifact the tool wrote")
    world.reload()
    assert world.sched.get("j1").state is JobState.DISPATCHED, (
        "an artifact on disk is not a completion; only the record is")
    assert world.evidence.contains(produced), (
        "and the artifact must still be there, so the retry can compare")


# ---- 7. evidence stored -> corrupted -------------------------------------
def test_corrupted_evidence_is_detected_on_read_not_trusted(world):
    dg = world.evidence.put(b"a measurement record")
    blob = next(p for p in (world.root / "evidence").rglob("*")
                if p.is_file() and dg[8:] in p.name or p.name.endswith(dg[-8:]))
    original = blob.read_bytes()
    blob.write_bytes(original + b" tampered")
    world.reload()
    with pytest.raises(Exception):
        world.evidence.get(dg)
    assert not world.evidence.verify_store().ok
    blob.write_bytes(original)


def test_a_record_citing_evidence_that_vanished_cannot_be_promoted(world):
    dg = world.evidence.put(b"a verification report")
    world.store.create(record_id="r1", kind="claim", proposer="p1",
                       evidence={"verification_report": dg})
    world.store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                           actor="v1", role=Role.VERIFIER)
    world.store.transition(record_id="r1", dst=State.VERIFIED, actor="v1",
                           role=Role.VERIFIER,
                           evidence={"verification_report": dg})

    for p in sorted((world.root / "evidence").rglob("*")):
        if p.is_file():
            p.unlink()
    world.reload()
    with pytest.raises(Exception):
        world.store.transition(record_id="r1", dst=State.PROMOTED,
                               actor="owner", role=Role.PROMOTER,
                               policy_id="p1",
                               evidence={"policy_id": "p1"})


# ---- 8. verification begins -> a dependency is invalidated ---------------
def test_a_dependency_invalidated_mid_verification_blocks_the_dependent(
        world):
    world.sched.enqueue(job_id="upstream", work_digest=WORK, submitter="p1")
    world.sched.enqueue(job_id="downstream", work_digest=digest({"w": 2}),
                        submitter="p1", depends_on=("upstream",))
    world.sched.reconcile()
    world.sched.dispatch(job_id="upstream", worker="w1", lease_id="L1",
                         lease_seqs=50)
    world.sched.report(job_id="upstream", worker="w1")
    world.sched.reconcile()
    world.sched.dispatch(job_id="downstream", worker="w1", lease_id="L2",
                         lease_seqs=50)

    world.sched.invalidate(job_id="upstream", actor="system",
                           reason="the input changed")
    world.reload()
    assert world.sched.get("downstream").state is JobState.BLOCKED


# ---- 9. verification succeeds -> crash before the transition -------------
def test_a_verification_that_was_not_recorded_did_not_happen(world):
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.sched.reconcile()
    world.sched.dispatch(job_id="j1", worker="w1", lease_id="L1",
                         lease_seqs=50)
    # The verifier finishes its work and the process dies here.
    world.reload()
    assert world.sched.get("j1").state is JobState.DISPATCHED, (
        "recovery must never be MORE advanced than the log; a verification "
        "nobody recorded is a verification nobody can check")


# ---- 10-11. crash before checkpoint; corrupt checkpoint ------------------
def test_a_missing_checkpoint_costs_time_and_never_authority(world):
    for i in range(5):
        world.store.create(record_id=f"r{i}", kind="claim", proposer="p1")
    world.reload()
    assert len(world.store.all_records()) == 5
    assert world.store.loaded_prefix_verified is True


def test_a_corrupt_checkpoint_is_detected_rather_than_restored(world):
    for i in range(3):
        world.store.create(record_id=f"r{i}", kind="claim", proposer="p1")
    cp = world.store.checkpoint(world.checkpoints)
    files = sorted(p for p in (world.root / "checkpoints").rglob("*")
                   if p.is_file())
    assert files, "the checkpoint must be on disk to be corrupted"
    raw = json.loads(files[-1].read_text(encoding="utf-8"))
    raw["seq"] = raw["seq"] + 100
    files[-1].write_text(json.dumps(raw), encoding="utf-8")

    log = EventLog(world.root / "log.jsonl")
    store = AuthorityStore(log, evidence=world.evidence)
    with pytest.raises(Exception):
        AuthorityStore.load_from(log, CheckpointStore(
            world.root / "checkpoints"), blobs=world.evidence,
            evidence=world.evidence, require_checkpoint=True)
    assert cp.seq >= 0
    assert store.load().all_records()


# ---- 12-13. restart -> reconstruction -> audit ---------------------------
def test_primary_and_independent_reconstruction_agree_after_a_crash(world):
    dg = world.evidence.put(b"a report")
    world.store.create(record_id="r1", kind="claim", proposer="p1",
                       evidence={"verification_report": dg})
    world.store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                           actor="v1", role=Role.VERIFIER)
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.reload()

    independent = reconstruct(world.log)
    assert independent.states()["r1"] == world.store.get("r1").state.value, (
        "a second implementation reading the same log must reach the same "
        "state; a difference is a bug in one of them and the log decides")
    assert compare(world.store, independent) == (), (
        "the live projection and the independent reconstruction must agree "
        "field for field after a crash")
    assert not independent.unauthorized, independent.unauthorized


def test_the_audit_chain_is_answerable_after_a_crash(world):
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.reload()
    index = AuditIndex.from_log(world.log)
    timeline = index.timeline()
    assert timeline, "the history must be readable after recovery"
    assert any(getattr(e, "action", None) == "scheduler.enqueue"
               for e in timeline)


def test_full_verification_still_catches_a_tampered_prefix(world):
    """Incremental verification is an optimization, not a replacement."""
    for i in range(6):
        world.store.create(record_id=f"r{i}", kind="claim", proposer="p1")
    path = world.root / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[2])
    rec["actor"] = "mallory"
    lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = EventLog(path).verify()
    assert not report.ok
    assert any("altered" in p or "does not link" in p
               for p in report.problems), report.problems[:3]


def test_a_truncated_log_is_detected_by_the_separately_held_witness(world):
    for i in range(4):
        world.store.create(record_id=f"r{i}", kind="claim", proposer="p1")
    path = world.root / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")

    report = EventLog(path).verify()
    assert not report.ok
    assert any("TRUNCATED" in p for p in report.problems), report.problems


def test_a_partial_trailing_line_is_treated_as_a_truncation_boundary(world):
    world.store.create(record_id="r1", kind="claim", proposer="p1")
    path = world.root / "log.jsonl"
    intact = [e.seq for e in EventLog(path).read()]
    with path.open("ab") as fh:
        fh.write(b'{"seq": 99, "actor": "half-writ')

    with pytest.raises(Exception):
        EventLog(path).read(strict=True)
    salvaged = EventLog(path).read(strict=False)
    assert [e.seq for e in salvaged] == intact, (
        "a crash mid-append must not cost the records before it")


# ---- the composite: nothing unsafe becomes true --------------------------
@pytest.mark.parametrize("boundary", [
    "after_enqueue", "after_dispatch", "after_failure", "after_success",
    "after_invalidation",
])
def test_recovery_is_never_more_advanced_than_the_log(world, boundary):
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    expected = JobState.WAITING
    if boundary != "after_enqueue":
        world.sched.reconcile()
        world.sched.dispatch(job_id="j1", worker="w1", lease_id="L1",
                             lease_seqs=50)
        expected = JobState.DISPATCHED
    if boundary == "after_failure":
        world.sched.report(job_id="j1", worker="w1",
                           failure=FailureClass.PERMANENT)
        expected = JobState.FAILED
    if boundary in ("after_success", "after_invalidation"):
        world.sched.report(job_id="j1", worker="w1")
        expected = JobState.SUCCEEDED
    if boundary == "after_invalidation":
        world.sched.invalidate(job_id="j1", actor="system", reason="changed")
        expected = JobState.INVALIDATED

    world.reload()
    assert world.sched.get("j1").state is expected


def test_the_log_refuses_to_grow_onto_damage(world):
    world.store.create(record_id="r1", kind="claim", proposer="p1")
    path = world.root / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    rec["actor"] = "mallory"
    lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBroken, match="refusing to append"):
        EventLog(path).append(actor="a", action="probe", target="t",
                              payload={})


def test_evidence_that_never_existed_cannot_be_recovered_into_existence(
        world):
    with pytest.raises(UnknownEvidence):
        world.evidence.get("0" * 64)


def test_the_reconstruction_reports_a_log_it_could_not_fully_interpret(world):
    """FOREIGN is skipped in silence; UNKNOWN must never be.

    The independent reconstruction rebuilds authority records only, so
    another subsystem's events are correctly not its business. An action
    NOTHING in this package writes is a different thing entirely: whatever it
    recorded is missing from the reconstruction, and a reader who is not told
    that will treat an incomplete answer as a complete one.
    """
    world.store.create(record_id="r1", kind="claim", proposer="p1")
    clean = reconstruct(world.log)
    assert clean.anomalies == [], clean.anomalies
    assert clean.foreign_events > 0, (
        "this log carries policy and agent events; they must be counted as "
        "foreign rather than passed over invisibly")

    world.log.append(actor="mallory", action="record.invented.by.a.future",
                     target="r1", payload={"record_id": "r1"})
    dirty = reconstruct(world.log)
    assert any("unknown action" in a for a in dirty.anomalies), (
        dirty.anomalies)
    assert any("missing whatever it recorded" in a for a in dirty.anomalies)


def test_several_subsystems_share_one_log(world):
    """The property the action registry exists for.

    Before it, the authority store raised on 'policy.publish' and no two
    subsystems could use one log -- which made the package's central premise,
    that one hash-chained log is the authority history, unimplementable.
    """
    world.store.create(record_id="r1", kind="claim", proposer="p1")
    world.sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
    world.agents.escalate(escalation_id="e1", task_id="t1",
                          question="proceed?", raised_by="p1",
                          options=("yes", "no"))
    world.reload()

    assert world.store.get("r1").state is State.PROPOSED
    assert world.sched.get("j1").state is JobState.WAITING
    assert world.agents.is_blocked("t1") is True
    assert world.policy.in_force("scheduler.default").version == 1
    actions_seen = {ev.action for ev in world.log.read()}
    assert {"record.create", "scheduler.enqueue", "agent.escalation",
            "policy.publish"} <= actions_seen
