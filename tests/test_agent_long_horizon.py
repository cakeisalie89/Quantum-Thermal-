"""Long-horizon: the defects that only appear after the thousandth operation.

WHY LENGTH IS ITS OWN TEST DIMENSION

Two defects in this package were invisible to every short test and obvious
after a few hundred operations: checkpointing was O(n^2), and so was the event
log's append, because each one verified the whole history before doing its
work. Neither is a logic error that a targeted test would find. Both are
properties of a trajectory.

So this suite runs a mixed workload long enough for that class to show, drives
it through repeated restarts, and then checks the invariants that must hold
over the WHOLE history rather than over any single operation.

WHAT COUNTS AS AN INVARIANT HERE

Not "the code did not crash". The list at the end is the one that matters: no
duplicate sequence number, no accepted stale lease, no cancelled job promoted,
no orphan evidence treated as authoritative, no authority escalation, and the
primary and independent reconstructions agreeing field for field. Each is a
thing that would be true of a healthy history and false of a subtly broken
one.
"""
from __future__ import annotations

import collections
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
from qta_agent.checkpoint import CheckpointStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.memory import MemoryStatus, MemoryStore  # noqa: E402
from qta_agent.policy import (  # noqa: E402
    Effect, PolicyStore, document, rule,
)
from qta_agent.reconstruct import compare, reconstruct  # noqa: E402
from qta_agent.scheduler import (  # noqa: E402
    FailureClass, JobState, Scheduler, TERMINAL, backoff_for,
    default_policy,
)
from qta_agent.store import AuthorityStore  # noqa: E402

#: Cycles of the mixed workload. Each cycle writes roughly twenty events, so
#: this is a few thousand -- past the point where the two known quadratic
#: defects became obvious, and still inside a CI budget.
CYCLES = 260

#: Restart every this many cycles. State must not drift across any of them.
RESTART_EVERY = 40

#: Crash every this many cycles, mid-operation. Coprime-ish with
#: RESTART_EVERY so crashes and orderly restarts do not always coincide --
#: a crash that only ever happens immediately after a checkpoint is the
#: easiest possible crash, and would prove the least.
CRASH_EVERY = 27


#: WHAT COUNTS AS ONE GOVERNED OPERATION.
#:
#: Not a loop iteration, not a log append, not a sleep, not a counter bump.
#: An operation here is a call that moves DURABLE GOVERNED STATE through a
#: gate: work is admitted, owned, run, judged, withdrawn, remembered, or the
#: system is restarted and rebuilt from what survived.
#:
#: The distinction matters because "2,000 events" and "1,000 governed
#: operations" are different claims, and only the second says anything about
#: how much lifecycle the system actually survived. One enqueue writes one
#: event; one retry cycle writes six and is four operations.
#:
#: DELIBERATELY EXCLUDED: the priority ticks used to advance sequence-numbered
#: expiry. They are a loop whose purpose is to move the clock, and counting
#: them would let the headline number grow by spinning. They are counted
#: separately as ``ticks`` so the exclusion is visible rather than silent.
GOVERNED_OPS = (
    "evidence.put", "job.enqueue", "job.reconcile", "job.dispatch",
    "job.report.success", "job.report.failure", "job.cancel",
    "job.invalidate", "record.create", "record.transition",
    "memory.remember", "memory.invalidate_source", "policy.publish",
    "checkpoint", "restart", "crash.recover",
)


class Horizon:
    """A long-running system, rebuildable from its log at any moment."""

    def __init__(self, root: Path):
        self.root = root
        self.evidence = EvidenceStore(root / "evidence")
        self.checkpoints = CheckpointStore(root / "checkpoints")
        self.restarts = 0
        self.crashes = 0
        self.ops: "collections.Counter[str]" = collections.Counter()
        self.ticks = 0
        self.reload()

    def op(self, kind: str, n: int = 1):
        """Record one meaningful governed operation. See GOVERNED_OPS."""
        assert kind in GOVERNED_OPS, f"undeclared operation kind {kind!r}"
        self.ops[kind] += n

    @property
    def total_ops(self) -> int:
        return sum(self.ops.values())

    def reload(self, *, after_crash: bool = False) -> "Horizon":
        self.restarts += 1
        if hasattr(self, "ops"):
            self.op("crash.recover" if after_crash else "restart")
        if after_crash:
            self.crashes += 1
        self.log = EventLog(self.root / "log.jsonl")
        self.policy = PolicyStore(self.log).load()
        self.sched = Scheduler(self.log, policy=self.policy,
                               policy_id="scheduler.default",
                               capacity={"slots": 8}).load()
        self.store = AuthorityStore(self.log, evidence=self.evidence).load()
        self.memory = MemoryStore(self.log, evidence=self.evidence).load()
        self.agents = AgentDirectory(self.log).load()
        return self

    def step_past(self, seqs: int, tag: str) -> None:
        """Advance the log so sequence-numbered expiries can elapse."""
        for i in range(seqs):
            self.sched.set_priority(
                job_id=tag, priority=9, actor="scheduler", role="SCHEDULER",
                reason=f"tick {i}")
        # Counted, and deliberately NOT counted as governed operations.
        self.ticks += seqs


@pytest.fixture(scope="module")
def horizon(tmp_path_factory):
    """Built once and shared: the trajectory IS the fixture."""
    root = tmp_path_factory.mktemp("horizon")
    h = Horizon(root)
    h.policy.publish(default_policy(), actor="owner")
    for iid, role in (("p1", AgentRole.PROPOSER), ("w1", AgentRole.EXECUTOR),
                      ("v1", AgentRole.VERIFIER)):
        h.agents.register(identity(agent_id=iid, instance_id=iid,
                                   kind=PrincipalKind.AGENT, roles={role}),
                          by="system")
    h.reload()

    expectations = {
        "succeeded": set(), "failed": set(), "cancelled": set(),
        "blocked": set(), "invalidated": set(), "promoted": set(),
        "rejected_promotions": 0, "retried": set(),
        "crash_recovered": set(),
    }

    for cycle in range(CYCLES):
        kind = cycle % 6
        jid = f"job-{cycle:04d}"
        rid = f"rec-{cycle:04d}"
        dg = h.evidence.put(f"artifact for cycle {cycle}".encode())
        h.op("evidence.put")

        h.sched.enqueue(job_id=jid, work_digest=digest({"cycle": cycle}),
                        submitter="p1", requires_evidence=(dg,))
        h.op("job.enqueue")
        h.sched.reconcile(resolve=h.evidence.contains)
        h.op("job.reconcile")

        if kind == 0:                                   # plain success
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op('job.report.success')
            h.sched.report(job_id=jid, worker="w1")
            expectations["succeeded"].add(jid)
        elif kind == 1:                                 # permanent failure
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op('job.report.failure')
            h.sched.report(job_id=jid, worker="w1",
                           failure=FailureClass.PERMANENT, detail="bad input")
            expectations["failed"].add(jid)
        elif kind == 2:                                 # transient, retried
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op('job.report.failure')
            h.sched.report(job_id=jid, worker="w1",
                           failure=FailureClass.TRANSIENT, detail="socket")
            h.step_past(backoff_for(1) + 1, jid)
            h.sched.reconcile(resolve=h.evidence.contains)
            h.op('job.reconcile')
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}b",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op('job.report.success')
            h.sched.report(job_id=jid, worker="w1")
            expectations["succeeded"].add(jid)
            expectations["retried"].add(jid)
        elif kind == 3:                                 # cancelled mid-flight
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op('job.cancel')
            h.sched.cancel(job_id=jid, actor="p1", reason="withdrawn")
            expectations["cancelled"].add(jid)
        elif kind == 4:                                 # succeeded then void
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op('job.report.success')
            h.sched.report(job_id=jid, worker="w1")
            h.op('job.invalidate')
            h.sched.invalidate(job_id=jid, actor="system",
                               reason="input moved")
            expectations["invalidated"].add(jid)
        else:                                           # lease lapses
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=1, resolve=h.evidence.contains)
            h.step_past(3, jid)
            h.sched.reconcile(resolve=h.evidence.contains)
            h.op('job.reconcile')
            h.op('job.dispatch')
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}c",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op('job.report.success')
            h.sched.report(job_id=jid, worker="w1")
            expectations["succeeded"].add(jid)

        # An authority record alongside, so both machines share the history.
        h.store.create(record_id=rid, kind="claim", proposer="p1",
                       evidence={"verification_report": dg})
        h.op("record.create")
        h.store.transition(record_id=rid, dst=State.UNDER_REVIEW, actor="v1",
                           role=Role.VERIFIER)
        h.op("record.transition")
        if cycle % 3 == 0:
            h.store.transition(record_id=rid, dst=State.VERIFIED, actor="v1",
                               role=Role.VERIFIER,
                               evidence={"verification_report": dg})
            h.store.transition(record_id=rid, dst=State.PROMOTED,
                               actor="owner", role=Role.PROMOTER,
                               policy_id="scheduler.default@1",
                               evidence={"policy_id": "scheduler.default@1"})
            h.op("record.transition", 2)
            expectations["promoted"].add(rid)

        # Memory, with a source that is sometimes later withdrawn.
        h.memory.remember(memory_id=f"mem-{cycle:04d}",
                          text=f"cycle {cycle} looked ordinary",
                          author="p1", derived_from=(dg,))
        h.op("memory.remember")
        if cycle % 7 == 0:
            h.memory.invalidate_source(dg, actor="system",
                                       reason="re-measured")
            h.op("memory.invalidate_source")

        # A policy version every so often, so historical decisions have
        # something to be non-retroactive against.
        if cycle % 40 == 39:
            version = h.policy.in_force("scheduler.default").version + 1
            h.policy.publish(document(
                policy_id="scheduler.default", version=version,
                rules=(rule(rule_id=f"v{version}", effect=Effect.ALLOW,
                            actions=("*",), subjects=("*",), roles=("*",),
                            resources=("*",)),)), actor="owner")
            h.op("policy.publish")

        # A CRASH IS NOT A RESTART.
        #
        # reload() above is an orderly restart: the checkpoint is written
        # first and every operation of the cycle has completed. This is the
        # other thing -- the process disappears BETWEEN the two events of one
        # logical operation, with no checkpoint and nothing flushed by the
        # caller. Recovery must reach a state the system can continue from,
        # not merely one it can parse.
        #
        # The job is enqueued and dispatched, and the report never happens.
        # What must survive: the lease is real and owned, the work is not
        # lost, and nothing about the half-finished attempt reads as success.
        if cycle % CRASH_EVERY == CRASH_EVERY - 1:
            cid = f"crash-{cycle:04d}"
            h.sched.enqueue(job_id=cid, work_digest=digest({"crash": cycle}),
                            submitter="p1", requires_evidence=(dg,))
            h.op("job.enqueue")
            h.sched.reconcile(resolve=h.evidence.contains)
            h.op("job.reconcile")
            h.sched.dispatch(job_id=cid, worker="w1", lease_id=f"LC{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.op("job.dispatch")
            h.reload(after_crash=True)          # no report, no checkpoint
            recovered = h.sched.get(cid)
            assert recovered.state is JobState.DISPATCHED, recovered.state
            assert recovered.lease_holder == "w1"
            h.sched.report(job_id=cid, worker="w1")
            h.op("job.report.success")
            expectations["succeeded"].add(cid)
            expectations["crash_recovered"].add(cid)

        if cycle % RESTART_EVERY == RESTART_EVERY - 1:
            h.store.checkpoint(h.checkpoints)
            h.op("checkpoint")
            h.reload()

    # A REAL PROCESS, REALLY KILLED, WRITING TO THIS LOG.
    #
    # Every other restart in this suite discards projections inside one
    # interpreter. That proves state is derived from the log rather than
    # from memory, which is worth proving -- but it cannot show what
    # happens when the OS takes the process away between an fsync and the
    # next statement, because there is no next statement to reach.
    #
    # So one segment of the trajectory is written by a child that is sent
    # SIGKILL while it is appending. The log it was writing is the campaign's
    # own, and the campaign continues over it afterwards.
    script = h.root / "worker.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
from qta_agent.events import EventLog
log = EventLog({str(h.root / "log.jsonl")!r})
for i in range(4000):
    log.append(actor="w1", action="memory.write",
               target=f"killed-{{i}}",
               payload={{"entry": {{"memory_id": f"killed-{{i}}",
                                  "text": "written mid-flight",
                                  "author": "w1", "derived_from": [],
                                  "status": "ACTIVE", "status_reason": "",
                                  "created_seq": -1, "updated_seq": -1}}}})
    sys.stdout.write("x")
    sys.stdout.flush()
''', encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)],
                            stdout=subprocess.PIPE, start_new_session=True)
    try:
        assert proc.stdout.read(1) == b"x", "the child never appended"
        time.sleep(0.15)
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=10)
    h.killed_pid = proc.pid
    h.op("crash.recover")
    h.crashes += 1
    h.reload()
    h.expectations = expectations
    return h


# ---- the trajectory itself ----------------------------------------------
def test_the_run_was_long_enough_to_mean_something(horizon):
    report = horizon.log.verify()
    assert report.count > 2000, (
        f"only {report.count} events; the quadratic defects this suite exists "
        "for did not become obvious until several hundred")
    assert horizon.restarts >= CYCLES // RESTART_EVERY


def test_the_campaign_performed_a_thousand_governed_operations(horizon):
    """THE CLAIM, AND THE METRIC BEHIND IT.

    "2,000 events" and "1,000 governed operations" are different statements,
    and only the second says anything about how much lifecycle the system
    survived: one enqueue is one event and one operation, while one retry
    cycle is six events and four operations.

    So the count here is of calls that move durable governed state through a
    gate -- see GOVERNED_OPS -- and it deliberately EXCLUDES the priority
    ticks used to advance sequence-numbered expiry. Those are a loop, and
    counting them would let this number grow by spinning. They are asserted
    separately so the exclusion is visible rather than silent.
    """
    total = horizon.total_ops
    assert total >= 1000, (
        f"only {total} governed operations: {dict(horizon.ops)}")
    assert horizon.ticks > 0, "the tick loop never ran"
    assert "tick" not in horizon.ops, (
        "ticks leaked into the governed-operation count, which is exactly "
        "the inflation this metric is defined to exclude")


def test_the_thousand_operations_were_not_all_the_same_one(horizon):
    """A thousand enqueues would satisfy a count and prove almost nothing.

    The mixture is the point: work that succeeds, fails, is retried,
    cancelled, invalidated, promoted, remembered, withdrawn, checkpointed
    and recovered from a crash.
    """
    kinds = {k for k, n in horizon.ops.items() if n > 0}
    missing = {
        "job.enqueue", "job.dispatch", "job.report.success",
        "job.report.failure", "job.cancel", "job.invalidate",
        "record.create", "record.transition", "memory.remember",
        "memory.invalidate_source", "policy.publish", "checkpoint",
        "restart", "crash.recover", "evidence.put",
    } - kinds
    assert not missing, f"the trajectory never exercised: {sorted(missing)}"
    # No single kind may be most of the campaign.
    top = max(horizon.ops.values())
    assert top < horizon.total_ops * 0.5, (
        f"one operation kind is {top}/{horizon.total_ops} of the run")


def test_the_campaign_survived_crashes_mid_operation(horizon):
    """Restarts are orderly; crashes are not.

    Every restart in this suite happens after a checkpoint and after every
    operation of its cycle finished. A crash happens BETWEEN the two events
    of one logical operation, with nothing flushed and no checkpoint -- the
    case where a half-finished attempt could read as success.
    """
    assert horizon.crashes >= 5, f"only {horizon.crashes} crashes"
    recovered = horizon.expectations["crash_recovered"]
    assert recovered, "no job was carried across a crash"
    for jid in sorted(recovered):
        job = horizon.sched.get(jid)
        assert job.state is JobState.SUCCEEDED, (jid, job.state)
        # And the attempt that spanned the crash is still one attempt.
        assert job.attempts == 1, (jid, job.attempts)


def test_the_metric_is_documented_where_the_claim_is_made(horizon):
    """The number is only meaningful with its definition attached.

    Pinned so a later edit cannot raise the headline by widening what
    counts, which is the easiest way to make this suite dishonest.
    """
    import qta_agent  # noqa: F401  - anchors ROOT for the read below

    src = Path(__file__).read_text(encoding="utf-8")
    assert "WHAT COUNTS AS ONE GOVERNED OPERATION" in src
    assert "DELIBERATELY EXCLUDED" in src
    for kind in GOVERNED_OPS:
        assert f'"{kind}"' in src or f"'{kind}'" in src, kind


def test_the_chain_verifies_over_the_whole_history(horizon):
    """Full verification, not incremental. It must stay practical to run."""
    report = horizon.log.verify()
    assert report.ok, report.problems[:3]
    assert report.prefix_verified is True, (
        "this must be the strong check; an incremental one would not have "
        "looked at the prefix at all")


def test_sequence_numbers_are_unique_and_contiguous(horizon):
    seqs = [ev.seq for ev in horizon.log.read()]
    assert seqs == list(range(len(seqs)))
    assert len(set(seqs)) == len(seqs)


def test_the_witness_still_agrees_with_the_log(horizon):
    """The witness is a LOWER BOUND on the history, not a mirror of it.

    This asserted exact equality until a real SIGKILL landed between an
    append and the witness update, leaving the witness one event behind.
    The library was already right about that: appending first and
    witnessing second is the safe order, and _check_witness records a
    lagging witness as a NOTE while a witness AHEAD of the log is
    TRUNCATED and a mismatch at the same seq is FORKED.

    So the invariant is directional. A witness that lags cannot hide a
    truncation below its own position, which is what it exists to catch.
    """
    witness = horizon.log.head()
    events = horizon.log.read()
    assert witness.seq <= events[-1].seq, (
        "the witness is AHEAD of the log: records are missing")
    at = next(e for e in events if e.seq == witness.seq)
    assert witness.head_hash == at.hash, (
        "the witness and the log disagree at the witness's own position")
    report = horizon.log.verify()
    assert report.ok, report.problems[:3]
    if witness.seq < events[-1].seq:
        assert any("witness is behind" in n for n in report.notes), (
            "a lagging witness must be reported as a note, not passed over")


# ---- end-of-run state invariants ----------------------------------------
def test_every_job_reached_the_state_the_workload_intended(horizon):
    e = horizon.expectations
    for jid in sorted(e["succeeded"]):
        assert horizon.sched.get(jid).state is JobState.SUCCEEDED, jid
    for jid in sorted(e["failed"]):
        assert horizon.sched.get(jid).state is JobState.FAILED, jid
    for jid in sorted(e["cancelled"]):
        assert horizon.sched.get(jid).state is JobState.CANCELLED, jid
    for jid in sorted(e["invalidated"]):
        assert horizon.sched.get(jid).state is JobState.INVALIDATED, jid


def test_no_cancelled_job_was_promoted_to_success(horizon):
    for jid in sorted(horizon.expectations["cancelled"]):
        job = horizon.sched.get(jid)
        assert job.state is JobState.CANCELLED
        assert job.lease_holder is None, (
            "a cancelled job that keeps its owner can still be reported on")


def test_no_stale_lease_survived_anywhere_in_the_history(horizon):
    at = horizon.sched.at_seq()
    for job in horizon.sched.all_jobs().values():
        if job.state in TERMINAL:
            assert job.lease_id is None, (
                f"{job.job_id} finished holding a lease")
        elif job.state is JobState.DISPATCHED:
            assert job.lease_is_live(at), (
                f"{job.job_id} is dispatched under a lapsed lease")


def test_retried_jobs_kept_their_failed_attempt_in_the_history(horizon):
    reasons = [ev.payload.get("reason", "") for ev in horizon.log.read()
               if ev.action == "scheduler.transition"]
    assert any("TRANSIENT" in r for r in reasons), (
        "the failed attempts must still be readable after the retries "
        "succeeded; history is added to, not rewritten")
    for jid in sorted(horizon.expectations["retried"]):
        assert horizon.sched.get(jid).attempts >= 2


def test_promotions_all_have_a_resolvable_verification_report(horizon):
    """No orphan evidence was ever treated as authoritative."""
    for rid in sorted(horizon.expectations["promoted"]):
        rec = horizon.store.get(rid)
        assert rec.state is State.PROMOTED
        dg = rec.evidence["verification_report"]
        assert horizon.evidence.contains(dg, verify=True), (
            f"{rid} is canonical and cites evidence that does not resolve")


def test_no_record_reached_promoted_without_being_verified_first(horizon):
    """I1 over the whole history, from the log rather than from the state."""
    history: dict = {}
    for ev in horizon.log.read():
        if ev.action == "record.transition":
            rid = ev.payload["record_id"]
            history.setdefault(rid, []).append(
                (ev.payload["src"], ev.payload["dst"]))
    for rid, moves in history.items():
        for src, dst in moves:
            if dst == State.PROMOTED.value:
                assert src == State.VERIFIED.value, (
                    f"{rid} reached PROMOTED from {src}")


def test_memory_derived_from_a_withdrawn_source_is_not_current(horizon):
    stale = [e for e in horizon.memory.all_entries()
             if e.status is MemoryStatus.STALE]
    assert stale, "the workload withdrew sources; something must be stale"
    for entry in stale:
        assert entry not in horizon.memory.current()
    for entry in horizon.memory.current():
        assert entry.status is MemoryStatus.ACTIVE


def test_policy_versions_are_gap_free_after_the_whole_run(horizon):
    versions = [v for _, v, _ in horizon.policy.versions("scheduler.default")]
    assert versions == list(range(1, len(versions) + 1))


def test_a_historical_decision_is_still_judged_by_its_own_policy(horizon):
    """I5 at length: the first policy still governs the first decisions."""
    first_seq = horizon.policy.versions("scheduler.default")[0][0]
    assert horizon.policy.in_force_at(
        "scheduler.default", first_seq).version == 1
    assert horizon.policy.in_force("scheduler.default").version > 1


# ---- reconstruction -----------------------------------------------------
def test_primary_and_independent_reconstruction_agree_over_the_history(
        horizon):
    independent = reconstruct(horizon.log)
    assert compare(horizon.store, independent) == (), (
        "two implementations reading one log must agree field for field")
    assert not independent.unauthorized, independent.unauthorized[:3]
    assert not independent.anomalies, independent.anomalies[:3]
    assert independent.foreign_events > 0


def test_replaying_from_scratch_reproduces_the_live_projection(horizon):
    fresh = AuthorityStore(EventLog(horizon.root / "log.jsonl"),
                           evidence=horizon.evidence).load()
    assert fresh.state_digest() == horizon.store.state_digest(), (
        "state drifted across the restarts; a projection that cannot be "
        "rebuilt byte-for-byte is not derived from the log")
    assert fresh._loaded_through >= horizon.store._loaded_through, (
        "a fresh load reads to the head; the live store stopped at its own "
        "last write, which is why snapshot_digest is the wrong comparison "
        "here and state_digest is the right one")


def test_a_checkpointed_load_reaches_the_same_state_and_says_it_is_weaker(
        horizon):
    cached = AuthorityStore.load_from(
        EventLog(horizon.root / "log.jsonl"), horizon.checkpoints,
        blobs=horizon.evidence, evidence=horizon.evidence,
        require_checkpoint=True)
    assert cached.state_digest() == horizon.store.state_digest()
    assert cached.loaded_prefix_verified is False, (
        "a checkpointed load did not read the prefix; a report that does not "
        "say so is claiming more than it checked")


# ---- the audit trail ----------------------------------------------------
def test_the_whole_history_is_still_answerable(horizon):
    index = AuditIndex.from_log(horizon.log)
    timeline = index.timeline()
    assert len(timeline) == horizon.log.verify().count
    actors = set(index.actors())
    assert {"p1", "v1", "owner", "scheduler"} <= actors


def test_no_authority_escalation_happened_anywhere(horizon):
    """Every promotion was performed by a PROMOTER who was not the proposer."""
    proposers = {}
    for ev in horizon.log.read():
        if ev.action == "record.create":
            proposers[ev.payload["record_id"]] = ev.payload["proposer"]
        elif (ev.action == "record.transition"
                and ev.payload["dst"] == State.PROMOTED.value):
            rid = ev.payload["record_id"]
            assert ev.payload["role"] == Role.PROMOTER.value
            assert ev.actor != proposers[rid], (
                f"{rid} was promoted by its own proposer")


def test_a_real_process_was_killed_while_writing_this_log(horizon):
    """THE RESTART THAT IS NOT SIMULATED.

    Every other restart here discards projections inside one interpreter,
    which proves state is derived from the log rather than held in memory.
    It cannot prove anything about the OS removing the process between an
    fsync and the next statement, because there is no next statement.

    This one was a child process sent SIGKILL mid-append, writing to the
    campaign's own log. What must hold afterwards: the chain still verifies
    end to end, the partial write did not corrupt it, and the trajectory
    continued over the top.
    """
    assert getattr(horizon, "killed_pid", None), "no child was killed"
    report = horizon.log.verify()
    assert report.ok, report.problems[:3]
    # The child's appends are in the history and are ordinary events.
    killed = [ev for ev in horizon.log.read()
              if str(ev.target).startswith("killed-")]
    assert killed, "the child died before its first append reached the log"
    first = killed[0].payload["entry"]["memory_id"]
    assert horizon.memory.get(first) is not None


def test_the_chain_survives_a_partial_write_at_the_kill_point(horizon):
    """A SIGKILL lands wherever it lands, including mid-line.

    The log's own reader is what has to cope: either the last record is
    complete and verifies, or it is not there at all. A half-written line
    that still parses would be the dangerous outcome, and the chain check
    is what rules it out.
    """
    report = horizon.log.verify()
    assert report.ok and not report.problems
    seqs = [ev.seq for ev in horizon.log.read()]
    assert seqs == list(range(len(seqs))), "a gap or duplicate at the tear"
