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

import sys
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


class Horizon:
    """A long-running system, rebuildable from its log at any moment."""

    def __init__(self, root: Path):
        self.root = root
        self.evidence = EvidenceStore(root / "evidence")
        self.checkpoints = CheckpointStore(root / "checkpoints")
        self.restarts = 0
        self.reload()

    def reload(self) -> "Horizon":
        self.restarts += 1
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
    }

    for cycle in range(CYCLES):
        kind = cycle % 6
        jid = f"job-{cycle:04d}"
        rid = f"rec-{cycle:04d}"
        dg = h.evidence.put(f"artifact for cycle {cycle}".encode())

        h.sched.enqueue(job_id=jid, work_digest=digest({"cycle": cycle}),
                        submitter="p1", requires_evidence=(dg,))
        h.sched.reconcile(resolve=h.evidence.contains)

        if kind == 0:                                   # plain success
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.sched.report(job_id=jid, worker="w1")
            expectations["succeeded"].add(jid)
        elif kind == 1:                                 # permanent failure
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.sched.report(job_id=jid, worker="w1",
                           failure=FailureClass.PERMANENT, detail="bad input")
            expectations["failed"].add(jid)
        elif kind == 2:                                 # transient, retried
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.sched.report(job_id=jid, worker="w1",
                           failure=FailureClass.TRANSIENT, detail="socket")
            h.step_past(backoff_for(1) + 1, jid)
            h.sched.reconcile(resolve=h.evidence.contains)
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}b",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.sched.report(job_id=jid, worker="w1")
            expectations["succeeded"].add(jid)
            expectations["retried"].add(jid)
        elif kind == 3:                                 # cancelled mid-flight
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.sched.cancel(job_id=jid, actor="p1", reason="withdrawn")
            expectations["cancelled"].add(jid)
        elif kind == 4:                                 # succeeded then void
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.sched.report(job_id=jid, worker="w1")
            h.sched.invalidate(job_id=jid, actor="system",
                               reason="input moved")
            expectations["invalidated"].add(jid)
        else:                                           # lease lapses
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}",
                             lease_seqs=1, resolve=h.evidence.contains)
            h.step_past(3, jid)
            h.sched.reconcile(resolve=h.evidence.contains)
            h.sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{cycle}c",
                             lease_seqs=200, resolve=h.evidence.contains)
            h.sched.report(job_id=jid, worker="w1")
            expectations["succeeded"].add(jid)

        # An authority record alongside, so both machines share the history.
        h.store.create(record_id=rid, kind="claim", proposer="p1",
                       evidence={"verification_report": dg})
        h.store.transition(record_id=rid, dst=State.UNDER_REVIEW, actor="v1",
                           role=Role.VERIFIER)
        if cycle % 3 == 0:
            h.store.transition(record_id=rid, dst=State.VERIFIED, actor="v1",
                               role=Role.VERIFIER,
                               evidence={"verification_report": dg})
            h.store.transition(record_id=rid, dst=State.PROMOTED,
                               actor="owner", role=Role.PROMOTER,
                               policy_id="scheduler.default@1",
                               evidence={"policy_id": "scheduler.default@1"})
            expectations["promoted"].add(rid)

        # Memory, with a source that is sometimes later withdrawn.
        h.memory.remember(memory_id=f"mem-{cycle:04d}",
                          text=f"cycle {cycle} looked ordinary",
                          author="p1", derived_from=(dg,))
        if cycle % 7 == 0:
            h.memory.invalidate_source(dg, actor="system",
                                       reason="re-measured")

        # A policy version every so often, so historical decisions have
        # something to be non-retroactive against.
        if cycle % 40 == 39:
            version = h.policy.in_force("scheduler.default").version + 1
            h.policy.publish(document(
                policy_id="scheduler.default", version=version,
                rules=(rule(rule_id=f"v{version}", effect=Effect.ALLOW,
                            actions=("*",), subjects=("*",), roles=("*",),
                            resources=("*",)),)), actor="owner")

        if cycle % RESTART_EVERY == RESTART_EVERY - 1:
            h.store.checkpoint(h.checkpoints)
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
    witness = horizon.log.head()
    events = horizon.log.read()
    assert witness.seq == events[-1].seq
    assert witness.head_hash == events[-1].hash


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
