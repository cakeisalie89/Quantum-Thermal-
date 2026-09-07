"""The scheduler, under the conditions a queue actually meets.

Every test here corresponds to something that goes wrong in a real system:
a worker that dies holding a lease, a dependency that fails while its child
waits, a retry that races the original completion, a second submission of the
same work under the same key. A scheduler that only handles the happy path is
a for-loop with extra vocabulary.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest  # noqa: E402
from qta_agent.capability import (  # noqa: E402
    Action, CapabilitySet, issue,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.policy import (  # noqa: E402
    Effect, PolicyDenied, PolicyStore, document, rule,
)
from qta_agent.scheduler import (  # noqa: E402
    AGING_INTERVAL, BACKOFF_BASE_SEQS, CapacityError, DuplicateJob,
    FailureClass, Job, JobState, JobTransitionError, MAX_PRIORITY, RETRYABLE,
    MAX_LEASE_RENEWALS, Scheduler, SchedulerError, TERMINAL, UnknownJob,
    backoff_for, default_policy, job_from_record,
)

WORK = digest({"work": 1})
OTHER_WORK = digest({"work": 2})


@pytest.fixture()
def sched(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    pol.publish(default_policy(), actor="owner")
    s = Scheduler(log, policy=pol, policy_id="scheduler.default",
                  capacity={"slots": 2})
    return s.load()


def _enqueue(sched, job_id="j1", **kw):
    kw.setdefault("work_digest", WORK)
    kw.setdefault("submitter", "owner")
    return sched.enqueue(job_id=job_id, **kw)


def _dispatch(sched, job_id="j1", worker="w1", lease_seqs=50, **kw):
    return sched.dispatch(job_id=job_id, worker=worker,
                          lease_id=f"lease-{job_id}", lease_seqs=lease_seqs,
                          **kw)


def _ready_and_dispatch(sched, job_id="j1", **kw):
    sched.reconcile()
    return _dispatch(sched, job_id, **kw)


# ---- identity and durability -------------------------------------------
def test_a_job_survives_a_restart_with_its_identity_intact(sched, tmp_path):
    _enqueue(sched, "j1", priority=3)
    _ready_and_dispatch(sched, "j1", worker="w1")

    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    revived = Scheduler(log, policy=pol, policy_id="scheduler.default",
                        capacity={"slots": 2}).load()
    before, after = sched.get("j1"), revived.get("j1")
    assert after == before, (
        "a queue that is not recovered byte-for-byte after a restart has not "
        "recovered; it has started a similar one")
    assert after.state is JobState.DISPATCHED
    assert after.lease_holder == "w1"
    assert revived.snapshot_digest() == sched.snapshot_digest()


def test_the_projection_refuses_an_event_it_does_not_understand(sched):
    ev = sched.log.append(actor="x", action="task.create", target="t",
                          payload={})
    assert sched.apply(ev) is False


def test_unknown_job_is_an_error_not_a_default(sched):
    with pytest.raises(UnknownJob):
        sched.get("nope")


# ---- enqueue validation -------------------------------------------------
def test_enqueue_refuses_a_dependency_that_does_not_exist(sched):
    with pytest.raises(SchedulerError, match="does not exist"):
        _enqueue(sched, "j1", depends_on=("ghost",))


def test_enqueue_refuses_self_dependency(sched):
    with pytest.raises(SchedulerError, match="cannot depend on itself"):
        _enqueue(sched, "j1", depends_on=("j1",))


def test_enqueue_refuses_a_non_digest_work_identity(sched):
    with pytest.raises(SchedulerError, match="sha256 digest"):
        sched.enqueue(job_id="j1", work_digest="work", submitter="owner")


@pytest.mark.parametrize("priority", [-1, 10, True, 1.5, "0"])
def test_priority_is_bounded_and_typed(sched, priority):
    with pytest.raises(SchedulerError, match="priority"):
        _enqueue(sched, "j1", priority=priority)


def test_enqueue_refuses_work_the_executor_can_never_run(sched):
    """Hanging forever is not a failure mode; it is an absence of one."""
    with pytest.raises(CapacityError, match="never satisfy"):
        _enqueue(sched, "j1", resources={"slots": 99})


def test_enqueue_refuses_a_malformed_resource_declaration(sched):
    with pytest.raises(SchedulerError, match="non-negative int"):
        _enqueue(sched, "j1", resources={"slots": -1})
    with pytest.raises(SchedulerError, match="non-negative int"):
        _enqueue(sched, "j1", resources={"slots": True})


# ---- duplicate suppression ----------------------------------------------
def test_same_key_same_work_is_the_same_job(sched):
    a = _enqueue(sched, "j1", idempotency_key="k")
    b = _enqueue(sched, "j2", idempotency_key="k")
    assert b.job_id == a.job_id
    assert len(sched.all_jobs()) == 1


def test_same_key_different_work_is_refused(sched):
    """The dangerous direction: a real request suppressed as if it were a
    retry."""
    _enqueue(sched, "j1", idempotency_key="k")
    with pytest.raises(DuplicateJob, match="different work"):
        _enqueue(sched, "j2", idempotency_key="k", work_digest=OTHER_WORK)


def test_the_same_job_id_twice_is_refused(sched):
    _enqueue(sched, "j1")
    with pytest.raises(DuplicateJob, match="already exists"):
        _enqueue(sched, "j1")


def test_a_replayed_duplicate_enqueue_event_is_refused(sched, tmp_path):
    """A forged second enqueue would reset attempts and the lease."""
    job = _enqueue(sched, "j1")
    sched.log.append(actor="owner", action="scheduler.enqueue", target="j1",
                     payload={"job": job.to_record()})
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    with pytest.raises(DuplicateJob, match="enqueued twice"):
        Scheduler(log, policy=pol,
                  policy_id="scheduler.default").load()


# ---- readiness ----------------------------------------------------------
def test_a_job_waits_for_its_dependency(sched):
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    sched.reconcile()
    assert sched.get("a").state is JobState.READY
    assert sched.get("b").state is JobState.WAITING
    r = sched.readiness(sched.get("b"), at_seq=sched.at_seq())
    assert r.blocked_by == ("a",) and "waiting on a" in r.reason


def test_a_failed_dependency_blocks_rather_than_stranding(sched):
    """The requirement: a dependent must never sit in WAITING forever."""
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    _ready_and_dispatch(sched, "a", worker="w1")
    sched.report(job_id="a", worker="w1", failure=FailureClass.PERMANENT,
                 detail="deterministic")
    sched.reconcile()
    b = sched.get("b")
    assert b.state is JobState.BLOCKED
    assert "a" in b.reason


def test_a_cancelled_dependency_blocks_its_dependent(sched):
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    sched.cancel(job_id="a", actor="owner", reason="no longer wanted",
                 cascade=False)
    sched.reconcile()
    assert sched.get("b").state is JobState.BLOCKED


def test_a_ready_job_returns_to_waiting_when_a_precondition_lapses(sched):
    """Readiness is re-evaluated, not latched."""
    dg = digest({"evidence": 1})
    held = {dg}
    _enqueue(sched, "j1", requires_evidence=(dg,))
    sched.reconcile(resolve=lambda d: d in held)
    assert sched.get("j1").state is JobState.READY
    held.clear()
    sched.reconcile(resolve=lambda d: d in held)
    assert sched.get("j1").state is JobState.WAITING


def test_evidence_without_a_resolver_is_not_treated_as_satisfied(sched):
    dg = digest({"evidence": 1})
    _enqueue(sched, "j1", requires_evidence=(dg,))
    r = sched.readiness(sched.get("j1"), at_seq=sched.at_seq())
    assert r.ready is False
    assert "no resolver" in r.reason


def test_a_revoked_capability_blocks_fatally(sched):
    cap = issue(capability_id="c1", subject="w1", action=Action.WRITE_PATHS,
                task_id="t1", scope=("verification/stage10",), issued_seq=0)
    _enqueue(sched, "j1", requires_capability="c1")
    live = CapabilitySet(issued={"c1": cap}, at_seq=sched.at_seq())
    assert sched.readiness(sched.get("j1"), at_seq=sched.at_seq(),
                           capabilities=live).ready is True
    dead = CapabilitySet(issued={"c1": cap}, revoked=frozenset({"c1"}),
                         at_seq=sched.at_seq())
    r = sched.readiness(sched.get("j1"), at_seq=sched.at_seq(),
                        capabilities=dead)
    assert r.ready is False and r.fatal is True and "revoked" in r.reason


def test_an_expired_capability_blocks_fatally(sched):
    cap = issue(capability_id="c1", subject="w1", action=Action.WRITE_PATHS,
                task_id="t1", scope=("verification/stage10",), issued_seq=0,
                expires_after_seq=1)
    _enqueue(sched, "j1", requires_capability="c1")
    caps = CapabilitySet(issued={"c1": cap}, at_seq=99)
    r = sched.readiness(sched.get("j1"), at_seq=99, capabilities=caps)
    assert r.ready is False and r.fatal is True and "expired" in r.reason


def test_an_unissued_capability_blocks_fatally(sched):
    _enqueue(sched, "j1", requires_capability="never-issued")
    caps = CapabilitySet(issued={}, at_seq=sched.at_seq())
    r = sched.readiness(sched.get("j1"), at_seq=sched.at_seq(),
                        capabilities=caps)
    assert r.fatal is True and "never issued" in r.reason


def test_a_missing_capability_set_is_not_a_pass(sched):
    _enqueue(sched, "j1", requires_capability="c1")
    r = sched.readiness(sched.get("j1"), at_seq=sched.at_seq())
    assert r.ready is False and "no capability set" in r.reason


def test_readiness_consults_the_policy(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    pol.publish(document(
        policy_id="strict", version=1,
        rules=(rule(rule_id="enqueue-ok", effect=Effect.ALLOW,
                    actions=("scheduler.enqueue", "scheduler.cancel"),
                    subjects=("*",), roles=("*",), resources=("*",)),
               rule(rule_id="no-dispatch", effect=Effect.DENY,
                    actions=("scheduler.dispatch",), subjects=("*",),
                    roles=("*",), resources=("*",),
                    reason="this queue is drained, not run"))),
        actor="owner")
    s = Scheduler(log, policy=pol, policy_id="strict").load()
    _enqueue(s, "j1")
    r = s.readiness(s.get("j1"), at_seq=s.at_seq())
    assert r.ready is False and r.fatal is True
    assert "drained" in r.reason


def test_enqueue_is_refused_when_the_policy_says_so(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    pol.publish(document(
        policy_id="strict", version=1,
        rules=(rule(rule_id="no-mallory", effect=Effect.DENY,
                    actions=("*",), subjects=("mallory",), roles=("*",),
                    resources=("*",)),
               rule(rule_id="ok", effect=Effect.ALLOW, actions=("*",),
                    subjects=("*",), roles=("*",), resources=("*",)))),
        actor="owner")
    s = Scheduler(log, policy=pol, policy_id="strict").load()
    _enqueue(s, "ok-job", submitter="owner")
    with pytest.raises(PolicyDenied):
        _enqueue(s, "bad-job", submitter="mallory")


# ---- ordering -----------------------------------------------------------
def test_the_ready_queue_is_totally_ordered(sched):
    _enqueue(sched, "b", priority=5)
    _enqueue(sched, "a", priority=5)
    _enqueue(sched, "urgent", priority=1)
    sched.reconcile()
    order = [j.job_id for j in sched.ready_queue()]
    assert order[0] == "urgent"
    assert order[1:] == ["b", "a"], (
        "ties must break on enqueue order and then on id; dictionary order "
        "is stable within a process and not across a restart")


def test_waiting_improves_effective_priority_but_never_past_zero(sched):
    job = _enqueue(sched, "j1", priority=3)
    at = job.enqueued_seq
    assert job.effective_priority(at) == 3
    assert job.effective_priority(at + AGING_INTERVAL) == 2
    assert job.effective_priority(at + 10 * AGING_INTERVAL) == 0


def test_aging_lets_a_starved_job_overtake(sched):
    old = _enqueue(sched, "old", priority=9)
    _enqueue(sched, "new", priority=8)
    sched.reconcile()
    late = old.enqueued_seq + 2 * AGING_INTERVAL
    order = [j.job_id for j in sched.ready_queue(at_seq=late)]
    assert order[0] == "old"


# ---- priority escalation ------------------------------------------------
def test_a_worker_may_not_escalate_priority(sched):
    _enqueue(sched, "j1", priority=5)
    with pytest.raises(PolicyDenied):
        sched.set_priority(job_id="j1", priority=0, actor="w1", role="WORKER",
                           reason="mine is important")
    assert sched.get("j1").priority == 5


def test_lowering_priority_needs_no_escalation_grant(sched):
    _enqueue(sched, "j1", priority=5)
    out = sched.set_priority(job_id="j1", priority=7, actor="w1",
                             role="WORKER", reason="deprioritised")
    assert out.priority == 7


def test_the_scheduler_may_escalate_and_must_say_why(sched):
    _enqueue(sched, "j1", priority=5)
    out = sched.set_priority(job_id="j1", priority=1, actor="scheduler",
                             role="SCHEDULER", reason="blocking the release")
    assert out.priority == 1 and "release" in out.reason
    with pytest.raises(SchedulerError, match="requires a reason"):
        sched.set_priority(job_id="j1", priority=2, actor="scheduler",
                           role="SCHEDULER", reason="")


# ---- dispatch, leases, capacity ----------------------------------------
def test_only_a_ready_job_may_be_dispatched(sched):
    _enqueue(sched, "j1")
    with pytest.raises(JobTransitionError, match="only a READY job"):
        _dispatch(sched, "j1")


def test_dispatch_rechecks_readiness_against_the_current_log(sched):
    """A caller may have taken the queue and come back later."""
    dg = digest({"e": 1})
    held = {dg}
    _enqueue(sched, "j1", requires_evidence=(dg,))
    sched.reconcile(resolve=lambda d: d in held)
    held.clear()
    with pytest.raises(JobTransitionError, match="does not resolve"):
        _dispatch(sched, "j1", resolve=lambda d: d in held)


def test_capacity_bounds_concurrent_dispatch(sched):
    for i in range(3):
        _enqueue(sched, f"j{i}", resources={"slots": 1})
    sched.reconcile()
    _dispatch(sched, "j0", worker="w0")
    _dispatch(sched, "j1", worker="w1")
    with pytest.raises(CapacityError, match="above capacity"):
        _dispatch(sched, "j2", worker="w2")
    assert sched.in_flight_resources() == {"slots": 2}


def test_two_workers_cannot_both_take_one_job(sched):
    _enqueue(sched, "j1")
    sched.reconcile()
    _dispatch(sched, "j1", worker="w1")
    with pytest.raises(JobTransitionError, match="only a READY job"):
        _dispatch(sched, "j1", worker="w2")
    assert sched.get("j1").lease_holder == "w1"


def test_a_worker_that_does_not_hold_the_lease_may_not_report(sched):
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    with pytest.raises(JobTransitionError, match="leased to 'w1'"):
        sched.report(job_id="j1", worker="w2")
    assert sched.get("j1").state is JobState.DISPATCHED


def test_a_stale_lease_may_not_report_success(sched):
    """The worker back from the dead."""
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1", lease_seqs=1)
    for i in range(4):
        sched.log.append(actor="noise", action="scheduler.priority",
                         target="j1",
                         payload={"job_id": "j1", "priority": MAX_PRIORITY,
                                  "reason": f"tick {i}"})
        sched.apply(sched.log.read()[-1])
    with pytest.raises(JobTransitionError, match="lapsed"):
        sched.report(job_id="j1", worker="w1")


def test_a_lapsed_lease_returns_the_work_to_the_queue(sched):
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1", lease_seqs=1)
    for i in range(4):
        sched.set_priority(job_id="j1", priority=MAX_PRIORITY,
                           actor="scheduler", role="SCHEDULER",
                           reason=f"tick {i}")
    assert sched.expired_leases()[0].job_id == "j1"
    sched.reconcile()
    j = sched.get("j1")
    assert j.state is JobState.READY
    assert j.lease_id is None and j.lease_holder is None, (
        "a requeued job that keeps its owner can be completed by a worker "
        "that no longer owns it")


def test_reconcile_is_idempotent(sched):
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    first = sched.reconcile()
    assert first
    assert sched.reconcile() == (), (
        "a reconcile that moves something every time it runs fills the log "
        "with events that record nothing")


# ---- retry --------------------------------------------------------------
@pytest.mark.parametrize("cls", sorted(RETRYABLE, key=lambda c: c.value))
def test_retryable_failures_back_off_and_return(sched, cls):
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    j = sched.report(job_id="j1", worker="w1", failure=cls, detail="blip")
    assert j.state is JobState.RETRY_WAIT
    assert j.attempts == 1
    assert j.backoff_until_seq > j.updated_seq
    assert cls.value in j.last_failure


@pytest.mark.parametrize("cls", [
    FailureClass.PERMANENT, FailureClass.POLICY_DENIED,
    FailureClass.EVIDENCE_FAILED, FailureClass.VERIFICATION_FAILED,
    FailureClass.CANCELLED,
])
def test_non_retryable_failures_are_final(sched, cls):
    """Retrying a refusal makes it slower, not different.

    VERIFICATION_FAILED is the one that matters most: a verification that may
    be retried until it passes is not a verification.
    """
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    j = sched.report(job_id="j1", worker="w1", failure=cls)
    assert j.state is JobState.FAILED


def test_the_retry_budget_is_finite(sched):
    _enqueue(sched, "j1", max_attempts=2)
    for expected in (JobState.RETRY_WAIT, JobState.FAILED):
        sched.reconcile()
        job = sched.get("j1")
        if job.state is JobState.RETRY_WAIT:
            # step the log past the backoff window
            for i in range(backoff_for(job.attempts) + 1):
                sched.set_priority(job_id="j1", priority=MAX_PRIORITY,
                                   actor="scheduler", role="SCHEDULER",
                                   reason=f"tick {i}")
            sched.reconcile()
        _dispatch(sched, "j1", worker="w1")
        out = sched.report(job_id="j1", worker="w1",
                           failure=FailureClass.TRANSIENT)
        assert out.state is expected


def test_backoff_grows_and_is_capped():
    seqs = [backoff_for(n) for n in range(1, 20)]
    assert seqs[0] == BACKOFF_BASE_SEQS
    assert seqs == sorted(seqs)
    assert seqs[-1] == seqs[-2], "backoff must reach a ceiling"
    assert backoff_for(0) == 0


def test_a_retrying_job_is_not_dispatchable_until_the_backoff_elapses(sched):
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    j = sched.report(job_id="j1", worker="w1", failure=FailureClass.TIMEOUT)
    assert sched.readiness(j, at_seq=sched.at_seq()).ready is False
    assert [x.job_id for x in sched.ready_queue()] == []
    late = j.backoff_until_seq + 1
    assert sched.readiness(j, at_seq=late).ready is True


def test_a_retry_produces_a_distinct_attempt_and_keeps_the_first(sched):
    """The failed attempt must remain visible; history is not rewritten."""
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    sched.report(job_id="j1", worker="w1", failure=FailureClass.TRANSIENT,
                 detail="socket reset")
    job = sched.get("j1")
    for i in range(backoff_for(job.attempts) + 1):
        sched.set_priority(job_id="j1", priority=MAX_PRIORITY,
                           actor="scheduler", role="SCHEDULER",
                           reason=f"tick {i}")
    sched.reconcile()
    _dispatch(sched, "j1", worker="w2")
    done = sched.report(job_id="j1", worker="w2")
    assert done.state is JobState.SUCCEEDED
    assert done.attempts == 2

    transitions = [ev.payload for ev in sched.log.read()
                   if ev.action == "scheduler.transition"]
    assert any("socket reset" in (p.get("reason") or "")
               for p in transitions), (
        "the failed attempt must still be in the history after the retry "
        "succeeds")


# ---- cancellation -------------------------------------------------------
@pytest.mark.parametrize("stage", ["waiting", "ready", "dispatched",
                                   "retrying"])
def test_cancellation_reaches_every_pre_terminal_stage(sched, stage):
    _enqueue(sched, "j1")
    if stage in ("ready", "dispatched", "retrying"):
        sched.reconcile()
    if stage in ("dispatched", "retrying"):
        _dispatch(sched, "j1", worker="w1")
    if stage == "retrying":
        sched.report(job_id="j1", worker="w1",
                     failure=FailureClass.TRANSIENT)
    (out,) = sched.cancel(job_id="j1", actor="owner", reason="stop",
                          cascade=False)
    assert out.state is JobState.CANCELLED


def test_cancellation_cascades_to_transitive_dependents(sched):
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    _enqueue(sched, "c", depends_on=("b",))
    out = sched.cancel(job_id="a", actor="owner", reason="scrapped")
    assert {j.job_id for j in out} == {"a", "b", "c"}
    assert all(j.state is JobState.CANCELLED for j in out)


def test_a_cancelled_job_cannot_be_completed_by_a_late_report(sched):
    """The record of the request decides, not the race."""
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    sched.cancel(job_id="j1", actor="owner", reason="stop")
    with pytest.raises(JobTransitionError, match="only a DISPATCHED job"):
        sched.report(job_id="j1", worker="w1")
    assert sched.get("j1").state is JobState.CANCELLED


def test_a_cancelled_job_never_becomes_ready(sched):
    _enqueue(sched, "j1")
    sched.cancel(job_id="j1", actor="owner", reason="stop")
    sched.reconcile()
    assert sched.get("j1").state is JobState.CANCELLED
    assert [j.job_id for j in sched.ready_queue()] == []


def test_cancelling_a_finished_job_is_refused(sched):
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    sched.report(job_id="j1", worker="w1")
    with pytest.raises(JobTransitionError, match="already SUCCEEDED"):
        sched.cancel(job_id="j1", actor="owner", reason="too late")


# ---- invalidation -------------------------------------------------------
def test_invalidating_a_succeeded_job_reaches_downstream(sched):
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    _enqueue(sched, "c", depends_on=("b",))
    sched.reconcile()
    _dispatch(sched, "a", worker="w1")
    sched.report(job_id="a", worker="w1")
    sched.reconcile()
    _dispatch(sched, "b", worker="w1")
    sched.report(job_id="b", worker="w1")

    moved = sched.invalidate(job_id="a", actor="system",
                             reason="input parameter changed")
    states = {j.job_id: j.state for j in moved}
    assert states["a"] is JobState.INVALIDATED
    assert states["b"] is JobState.INVALIDATED
    assert states["c"] is JobState.BLOCKED


def test_a_running_dependent_is_blocked_when_its_input_is_invalidated(sched):
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    sched.reconcile()
    _dispatch(sched, "a", worker="w1")
    sched.report(job_id="a", worker="w1")
    sched.reconcile()
    _dispatch(sched, "b", worker="w2")
    sched.invalidate(job_id="a", actor="system", reason="source changed")
    assert sched.get("b").state is JobState.BLOCKED


def test_only_a_succeeded_job_can_be_invalidated(sched):
    _enqueue(sched, "j1")
    with pytest.raises(JobTransitionError, match="only a SUCCEEDED job"):
        sched.invalidate(job_id="j1", actor="system", reason="x")


def test_invalidation_does_not_rewrite_the_success(sched):
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    sched.report(job_id="j1", worker="w1")
    sched.invalidate(job_id="j1", actor="system", reason="input moved")
    dsts = [ev.payload["dst"] for ev in sched.log.read()
            if ev.action == "scheduler.transition"]
    assert "SUCCEEDED" in dsts and dsts[-1] == "INVALIDATED", (
        "the history must keep saying it succeeded; invalidation adds a fact "
        "rather than deleting one")


# ---- the state machine itself -------------------------------------------
def test_no_finished_job_can_resume_work(sched):
    """Terminal means the work is over. It does not mean sealed.

    SUCCEEDED keeps one outgoing edge -- to INVALIDATED -- because an input
    changing is a fact recorded ABOUT finished work, not a resumption of it.
    What must be impossible is a finished job re-entering the queue.
    """
    from qta_agent.scheduler import PENDING, allowed_targets
    for state in sorted(TERMINAL, key=lambda s: s.value):
        assert not (allowed_targets(state) & (PENDING | {
            JobState.DISPATCHED})), (
            f"{state.value} can re-enter the queue; a finished job that can "
            "be redone in place leaves no trail of having been redone")


def test_every_declared_edge_is_reachable():
    """A dead edge is a rule nobody enforces and everybody reads.

    The task machine declared VERIFIED -> INVALIDATED and its guard refused
    it, so invalidation of a verified task was impossible while the table and
    the docstring both said otherwise. Nothing caught it, because an
    unreachable edge looks exactly like a reachable one until something asks.
    """
    from qta_agent.scheduler import EDGES, SEALED, check_edge
    for edge in EDGES:
        assert edge.src not in SEALED
        assert check_edge(edge.src, edge.dst, "j") is edge, (
            f"{edge.src.value} -> {edge.dst.value} is declared and refused")


def test_the_same_invariant_holds_for_the_task_machine():
    from qta_agent.tasks import (
        EDGES as T_EDGES, SEALED as T_SEALED, Task, TaskState,
        TaskTransition, check,
    )
    for edge in T_EDGES:
        assert edge.src not in T_SEALED
        task = Task(task_id="t", tool_id="x", submitter="o",
                    inputs_digest="0" * 64, state=edge.src,
                    executed_by="worker",
                    result_digest="1" * 64)
        if edge.requires_lease:
            from qta_agent.tasks import Lease
            task = __import__("dataclasses").replace(
                task, lease=Lease("L", "worker", 0, 10 ** 6))
        role = sorted(edge.roles, key=lambda r: r.value)[0]
        actor = "verifier" if edge.requires_distinct_actor else "worker"
        req = TaskTransition(
            task_id="t", src=edge.src, dst=edge.dst, actor=actor, role=role,
            at_seq=1, lease_id="L", result_digest="1" * 64)
        assert check(req, task) is edge, (
            f"{edge.src.value} -> {edge.dst.value} is declared and refused")
    assert TaskState.VERIFIED not in T_SEALED


def test_optimistic_concurrency_refuses_a_stale_writer(sched):
    _enqueue(sched, "j1")
    stale = sched.get("j1").revision
    sched.set_priority(job_id="j1", priority=4, actor="scheduler",
                       role="SCHEDULER", reason="bump")
    with pytest.raises(SchedulerError, match="changed since it was read"):
        sched.transition(job_id="j1", dst=JobState.READY, actor="scheduler",
                         expected_revision=stale)


def test_a_forged_transition_from_the_wrong_state_is_refused(sched, tmp_path):
    _enqueue(sched, "j1")
    sched.log.append(
        actor="mallory", action="scheduler.transition", target="j1",
        payload={"job_id": "j1", "src": JobState.DISPATCHED.value,
                 "dst": JobState.SUCCEEDED.value, "reason": "trust me"})
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    with pytest.raises(JobTransitionError, match="is WAITING"):
        Scheduler(log, policy=pol, policy_id="scheduler.default").load()


def test_a_forged_edge_that_does_not_exist_is_refused(sched, tmp_path):
    _enqueue(sched, "j1")
    sched.log.append(
        actor="mallory", action="scheduler.transition", target="j1",
        payload={"job_id": "j1", "src": JobState.WAITING.value,
                 "dst": JobState.SUCCEEDED.value, "reason": "shortcut"})
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    with pytest.raises(JobTransitionError, match="no edge WAITING"):
        Scheduler(log, policy=pol, policy_id="scheduler.default").load()


def test_a_job_record_with_unknown_fields_is_refused():
    rec = Job(job_id="j", work_digest=WORK, submitter="o").to_record()
    rec["run_as_root"] = True
    with pytest.raises(SchedulerError, match="unknown fields"):
        job_from_record(rec)


@pytest.mark.parametrize("bad", [None, [], "job", {"job_id": "j"}])
def test_malformed_job_records_fail_closed(bad):
    with pytest.raises(SchedulerError):
        job_from_record(bad)


# ---- lease release on every path out of DISPATCHED ----------------------
@pytest.mark.parametrize("how", ["cancel", "invalidate", "report_failure",
                                 "report_success", "lease_lapse"])
def test_leaving_dispatched_always_drops_the_lease(sched, how):
    """Every exit, not only the ones that remember to clear the fields.

    ``reconcile`` and ``report`` pass the clearing fields explicitly, so a
    test that only exercised those would pass with the release logic deleted.
    Cancellation and invalidation do not pass them -- and a cancelled job that
    keeps its owner is a job a returning worker can still report on.
    """
    _enqueue(sched, "a")
    _enqueue(sched, "b", depends_on=("a",))
    sched.reconcile()
    _dispatch(sched, "a", worker="w1", lease_seqs=1 if how == "lease_lapse"
              else 50)

    if how == "cancel":
        sched.cancel(job_id="a", actor="owner", reason="stop")
    elif how == "invalidate":
        sched.report(job_id="a", worker="w1")
        sched.reconcile()
        _dispatch(sched, "b", worker="w2")
        sched.invalidate(job_id="a", actor="system", reason="input moved")
        j = sched.get("b")
        assert j.state is JobState.BLOCKED
        assert j.lease_id is None and j.lease_holder is None
        assert j.lease_expires_after_seq == -1
        return
    elif how == "report_failure":
        sched.report(job_id="a", worker="w1",
                     failure=FailureClass.PERMANENT)
    elif how == "report_success":
        sched.report(job_id="a", worker="w1")
    else:
        for i in range(4):
            sched.set_priority(job_id="a", priority=MAX_PRIORITY,
                               actor="scheduler", role="SCHEDULER",
                               reason=f"tick {i}")
        sched.reconcile()

    j = sched.get("a")
    assert j.lease_id is None, f"{how} left the lease in place"
    assert j.lease_holder is None
    assert j.lease_expires_after_seq == -1


def test_a_cancelled_job_does_not_keep_an_owner_who_could_still_report(sched):
    """The composite failure the release logic exists to prevent."""
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    sched.cancel(job_id="j1", actor="owner", reason="stop")
    j = sched.get("j1")
    assert j.lease_holder is None
    assert not j.lease_is_live(sched.at_seq())


# ---- ordering that survives a differently-ordered projection ------------
def test_ready_order_does_not_depend_on_projection_insertion_order(sched):
    """The tiebreak is what makes the queue reproducible, not the dict.

    Rebuilding from the log gives insertion order; rebuilding from a snapshot
    gives id order, because a snapshot is sorted so it can be digested. Those
    two orders differ, and a queue whose dispatch order depends on which
    restore path was used has not recovered -- it has started a different
    queue that happens to hold the same work.
    """
    for jid in ("zeta", "alpha", "mid"):
        _enqueue(sched, jid, priority=5)
    sched.reconcile()
    log_order = [j.job_id for j in sched.ready_queue()]

    snapshot_order = dict(sorted(sched.all_jobs().items()))
    assert list(snapshot_order) != list(sched.all_jobs()), (
        "this test is only meaningful when the two orders differ")
    sched._jobs = snapshot_order
    assert [j.job_id for j in sched.ready_queue()] == log_order


def test_equal_priority_work_dispatches_oldest_first(sched):
    _enqueue(sched, "zzz-first", priority=5)
    for i in range(3):
        sched.set_priority(job_id="zzz-first", priority=5, actor="scheduler",
                           role="SCHEDULER", reason=f"tick {i}")
    _enqueue(sched, "aaa-second", priority=5)
    sched.reconcile()
    at = sched.get("zzz-first").enqueued_seq
    assert [j.job_id for j in sched.ready_queue(at_seq=at)] == [
        "zzz-first", "aaa-second"], (
        "id order must not overtake enqueue order; only exact ties fall "
        "through to the id")


# ---- the refusal must say what to do instead ---------------------------
def test_a_finished_job_is_refused_with_the_reason_an_operator_needs(sched):
    """'No edge' is true and useless. 'Terminal, make a new job' is actionable.

    The terminal branch is reached only when the edge table has no edge, so
    what it protects is the diagnosis rather than the refusal. That is still
    worth protecting: an operator who reads 'no edge SUCCEEDED -> READY' will
    go looking for the missing edge.
    """
    _enqueue(sched, "j1")
    _ready_and_dispatch(sched, "j1", worker="w1")
    sched.report(job_id="j1", worker="w1")
    with pytest.raises(JobTransitionError, match="is terminal.*NEW job"):
        sched.transition(job_id="j1", dst=JobState.READY, actor="scheduler")
    with pytest.raises(JobTransitionError, match="is terminal"):
        sched.transition(job_id="j1", dst=JobState.DISPATCHED,
                         actor="scheduler")


# --- a dependency that can never succeed ------------------------------------

def test_enqueueing_onto_a_cancelled_dependency_is_refused(sched):
    """FOUND BY THE STATEFUL PROPERTY TEST, in three steps.

    enqueue already refuses work that would wait forever twice over -- for
    capacity it can never have, and for a dependency nothing recorded. A
    dependency that is already CANCELLED is the same condition with the same
    consequence: CANCELLED is SEALED, no edge leads from it to SUCCEEDED, so
    the new job could never become ready.

    reconcile() would eventually move it to BLOCKED. That is a worse answer
    than this one: it arrives later, to nobody in particular, and by then the
    submitter has gone.
    """
    _enqueue(sched, "j1")
    sched.cancel(job_id="j1", actor="owner", reason="not needed")
    with pytest.raises(SchedulerError, match="wait forever"):
        _enqueue(sched, "j2", depends_on=("j1",))


@pytest.mark.parametrize("state", ["FAILED", "BLOCKED", "INVALIDATED"])
def test_no_sealed_dependency_state_is_acceptable(sched, state):
    """Every state from which SUCCEEDED is unreachable, not just the one the
    property test happened to find."""
    from qta_agent.scheduler import CAN_STILL_SUCCEED, JobState

    assert JobState(state) not in CAN_STILL_SUCCEED
    _enqueue(sched, "dep")
    sched.transition(job_id="dep", dst=JobState.READY, actor="scheduler")
    sched.dispatch(job_id="dep", worker="w", lease_id="L1", lease_seqs=50)
    if state == "FAILED":
        sched.report(job_id="dep", worker="w",
                     failure=FailureClass.PERMANENT)
    elif state == "INVALIDATED":
        # Reachable only from SUCCEEDED: an input changed after acceptance.
        sched.report(job_id="dep", worker="w")
        sched.invalidate(job_id="dep", actor="owner", reason="input changed")
    else:
        sched.transition(job_id="dep", dst=JobState(state), actor="scheduler")
    with pytest.raises(SchedulerError, match="wait forever"):
        _enqueue(sched, "later", depends_on=("dep",))


def test_a_dependency_still_in_flight_is_accepted(sched):
    """The refusal must name a real impossibility, not any unfinished parent.

    A job depending on one that is still WAITING, READY, DISPATCHED or
    RETRY_WAIT is the ordinary case, and refusing it would make dependencies
    useless.
    """
    from qta_agent.scheduler import JobState

    _enqueue(sched, "dep")
    for i, dst in enumerate((None, JobState.READY)):
        if dst is not None:
            sched.transition(job_id="dep", dst=dst, actor="scheduler")
        _enqueue(sched, f"child{i}", depends_on=("dep",))
    sched.dispatch(job_id="dep", worker="w", lease_id="L1", lease_seqs=50)
    _enqueue(sched, "child2", depends_on=("dep",))
    assert sched.get("child2").state is JobState.WAITING


def test_the_reachability_set_is_derived_from_the_edge_table(sched):
    """A hand-written list beside the table is a second place to forget."""
    from qta_agent.scheduler import (
        CAN_STILL_SUCCEED, EDGES, SEALED, JobState,
    )

    # Every state outside the set is one no edge leads out of toward success.
    for st in JobState:
        if st in CAN_STILL_SUCCEED:
            continue
        assert st in SEALED or not any(
            e.src is st and e.dst in CAN_STILL_SUCCEED for e in EDGES), (
            f"{st.value} is excluded but has an edge toward success")
    assert JobState.SUCCEEDED in CAN_STILL_SUCCEED


def test_a_terminal_failure_blocks_everything_downstream(sched):
    """FOUND BY THE STATEFUL PROPERTY TEST, as an asymmetry.

    cancel() cascades and says why: "the alternative is a dependent that
    waits on work nobody will ever do". invalidate() cascades for the same
    reason. A terminal FAILURE has exactly that consequence and did not --
    the dependent stayed WAITING until somebody happened to run reconcile().

    That is not a timing detail. A job WAITING on a dead parent is
    indistinguishable from one waiting on a slow parent, so nothing alerts
    and nobody looks; and a process that dies before the next tick leaves a
    queue on disk describing work as pending when it is not.
    """
    from qta_agent.scheduler import JobState

    _enqueue(sched, "parent")
    _enqueue(sched, "child", depends_on=("parent",))
    _enqueue(sched, "grandchild", depends_on=("child",))
    sched.transition(job_id="parent", dst=JobState.READY, actor="scheduler")
    sched.dispatch(job_id="parent", worker="w", lease_id="L1", lease_seqs=50)
    sched.report(job_id="parent", worker="w",
                 failure=FailureClass.PERMANENT)

    assert sched.get("parent").state is JobState.FAILED
    # Transitive, not just the immediate child -- the classic shortcut bug.
    assert sched.get("child").state is JobState.BLOCKED
    assert sched.get("grandchild").state is JobState.BLOCKED
    # blocked_by lives on the transition record, not on the Job: it is why
    # the move happened, and the projection carries current state.
    rec = next(ev.payload for ev in sched.log.read()
               if ev.action == "scheduler.transition"
               and ev.payload["job_id"] == "child"
               and ev.payload["dst"] == JobState.BLOCKED.value)
    assert rec["blocked_by"] == ["parent"]


def test_a_retryable_failure_does_not_block_dependents(sched):
    """The cascade must name a real impossibility.

    A RETRY_WAIT job still has attempts left, so SUCCEEDED is reachable and
    a dependent that waits is waiting for something that may yet arrive.
    Blocking it here would turn a transient outage into a dead queue.
    """
    from qta_agent.scheduler import JobState

    _enqueue(sched, "parent", max_attempts=3)
    _enqueue(sched, "child", depends_on=("parent",))
    sched.transition(job_id="parent", dst=JobState.READY, actor="scheduler")
    sched.dispatch(job_id="parent", worker="w", lease_id="L1", lease_seqs=50)
    sched.report(job_id="parent", worker="w",
                 failure=FailureClass.TRANSIENT)

    assert sched.get("parent").state is JobState.RETRY_WAIT
    assert sched.get("child").state is JobState.WAITING


def test_the_failure_cascade_does_not_rewrite_a_finished_dependent(sched):
    """A dependent that already finished keeps how it finished."""
    from qta_agent.scheduler import JobState

    _enqueue(sched, "parent")
    _enqueue(sched, "child", depends_on=("parent",))
    sched.cancel(job_id="child", actor="owner", reason="not wanted")
    sched.transition(job_id="parent", dst=JobState.READY, actor="scheduler")
    sched.dispatch(job_id="parent", worker="w", lease_id="L1", lease_seqs=50)
    sched.report(job_id="parent", worker="w",
                 failure=FailureClass.PERMANENT)

    assert sched.get("child").state is JobState.CANCELLED


def test_the_cascade_is_recorded_so_an_operator_can_see_it(sched):
    """Every move the scheduler makes on its own is an event.

    A state change nobody can attribute is a state change an operator cannot
    reason about after the fact.
    """
    from qta_agent.scheduler import ACT_JOB_TRANSITION, JobState

    _enqueue(sched, "parent")
    _enqueue(sched, "child", depends_on=("parent",))
    sched.transition(job_id="parent", dst=JobState.READY, actor="scheduler")
    sched.dispatch(job_id="parent", worker="w", lease_id="L1", lease_seqs=50)
    sched.report(job_id="parent", worker="w",
                 failure=FailureClass.PERMANENT)

    blocked = [ev for ev in sched.log.read()
               if ev.action == ACT_JOB_TRANSITION
               and ev.payload["job_id"] == "child"
               and ev.payload["dst"] == JobState.BLOCKED.value]
    assert len(blocked) == 1
    assert "can no longer succeed" in blocked[0].payload["reason"]


# --- ownership, re-checked on replay ----------------------------------------
#
# report() refuses an outcome from anyone but the lease holder, and refuses a
# report from a holder whose lease has lapsed. Those refusals lived only on
# the write path, so they were advice: mallory's report was rejected, and
# mallory appended the identical record to the log instead. The next process
# to load the queue folded it in and called the job SUCCEEDED.
#
# Each test below appends the record the write path had just refused.

def _dispatched(sched, job_id="j1", worker="worker-a", lease_seqs=100):
    _enqueue(sched, job_id)
    sched.transition(job_id=job_id, dst=JobState.READY, actor="scheduler")
    return sched.dispatch(job_id=job_id, worker=worker, lease_id="L1",
                          lease_seqs=lease_seqs)


def _reload(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    return Scheduler(log, policy=pol, policy_id="scheduler.default").load()


def test_the_write_path_refuses_an_outcome_from_a_non_holder(sched):
    """The refusal this pair of tests is about. Stated first, so the one
    below is visibly the SAME record arriving by another route."""
    _dispatched(sched)
    with pytest.raises(JobTransitionError, match="leased to 'worker-a'"):
        sched.report(job_id="j1", worker="mallory")


def test_and_replay_refuses_it_too_when_it_arrives_as_a_record(sched,
                                                               tmp_path):
    _dispatched(sched)
    sched.log.append(
        actor="mallory", action="scheduler.transition", target="j1",
        payload={"job_id": "j1", "src": JobState.DISPATCHED.value,
                 "dst": JobState.SUCCEEDED.value, "reason": "verified",
                 "lease_id": "", "lease_holder": "",
                 "lease_expires_after_seq": -1})
    with pytest.raises(JobTransitionError, match="is reporting its outcome"):
        _reload(tmp_path)


def test_replay_refuses_an_outcome_reported_after_the_lease_lapsed(sched,
                                                                   tmp_path):
    """A worker back from the dead does not get to decide the outcome.

    The work may already have been redone by someone else, and differently.
    """
    _dispatched(sched, lease_seqs=1)
    for i in range(4):                       # push the log past the expiry
        _enqueue(sched, f"filler{i}")
    sched.log.append(
        actor="worker-a", action="scheduler.transition", target="j1",
        payload={"job_id": "j1", "src": JobState.DISPATCHED.value,
                 "dst": JobState.SUCCEEDED.value, "reason": "verified",
                 "lease_id": "", "lease_holder": "",
                 "lease_expires_after_seq": -1})
    with pytest.raises(JobTransitionError, match="lapsed"):
        _reload(tmp_path)


def test_replay_refuses_reclaiming_a_lease_that_is_still_live(sched,
                                                              tmp_path):
    """The requeue edge cannot be guarded by actor -- it is the edge somebody
    ELSE is meant to take -- so it is guarded by the fact it claims.

    Without this, "the lease lapsed" is a sentence anyone can write, and the
    same work goes to a second worker while the first is still running it.
    """
    _dispatched(sched, lease_seqs=500)
    sched.log.append(
        actor="mallory", action="scheduler.transition", target="j1",
        payload={"job_id": "j1", "src": JobState.DISPATCHED.value,
                 "dst": JobState.READY.value, "reason": "lease lapsed",
                 "lease_id": "", "lease_holder": "",
                 "lease_expires_after_seq": -1})
    with pytest.raises(JobTransitionError, match="Reclaiming a live lease"):
        _reload(tmp_path)


def test_an_honest_reconcile_of_a_lapsed_lease_still_replays(sched, tmp_path):
    """The guard above must refuse the claim, not the operation.

    A requeue that is TRUE is the scheduler's ordinary recovery path, and a
    check that broke it would be removed within a week.
    """
    _dispatched(sched, lease_seqs=1)
    for i in range(4):
        _enqueue(sched, f"filler{i}")
    moved = sched.reconcile()
    assert any(j.job_id == "j1" and j.state is JobState.READY for j in moved)
    assert _reload(tmp_path).get("j1").state is JobState.READY


def test_replay_refuses_a_record_that_rewrites_the_attempt_count(sched,
                                                                 tmp_path):
    """max_attempts bounds the count, so whoever may set the count may retry
    forever -- and a retry loop is the cheapest denial of service there is."""
    _dispatched(sched)
    sched.log.append(
        actor="worker-a", action="scheduler.transition", target="j1",
        payload={"job_id": "j1", "src": JobState.DISPATCHED.value,
                 "dst": JobState.RETRY_WAIT.value, "reason": "again",
                 "attempts": 0, "lease_id": "", "lease_holder": "",
                 "lease_expires_after_seq": -1, "backoff_until_seq": 0})
    with pytest.raises(JobTransitionError, match="attempts"):
        _reload(tmp_path)


def test_replay_refuses_a_record_that_grants_itself_a_lease(sched, tmp_path):
    """Ownership is taken at dispatch. A record that hands one out anywhere
    else is naming its own owner, which is the whole of the defence."""
    _enqueue(sched, "j1")
    sched.log.append(
        actor="mallory", action="scheduler.transition", target="j1",
        payload={"job_id": "j1", "src": JobState.WAITING.value,
                 "dst": JobState.READY.value, "reason": "mine now",
                 "lease_id": "L9", "lease_holder": "mallory",
                 "lease_expires_after_seq": 99999})
    with pytest.raises(JobTransitionError, match="carries a lease"):
        _reload(tmp_path)


def test_a_governed_report_is_written_under_the_holders_identity(sched):
    """The verifier judges; the lease holder is who the record is FROM.

    Both facts are durable: replay re-checks ownership from the actor, and
    ``closed_by`` keeps the verifier's separate role visible to an auditor.
    """
    from qta_agent.scheduler import ACT_JOB_TRANSITION

    _dispatched(sched)
    sched.report(job_id="j1", worker="worker-a", actor="verifier-1")
    ev = [e for e in sched.log.read()
          if e.action == ACT_JOB_TRANSITION
          and e.payload["dst"] == JobState.SUCCEEDED.value][-1]
    assert ev.actor == "worker-a"
    assert ev.payload["closed_by"] == "verifier-1"


def test_the_replayed_lease_deadline_is_the_one_the_write_path_used(sched,
                                                                    tmp_path):
    """An off-by-one here refuses reports that report() had just accepted.

    ``report`` decides at ``at_seq()`` and the record it writes lands one seq
    later, so replay evaluates liveness at ``seq - 1``. This pins that
    exactly: the report below is made at the LAST position the lease is live,
    which is the only position where the two readings differ.
    """
    _dispatched(sched, lease_seqs=1)
    job = sched.get("j1")
    _enqueue(sched, "filler")                # advance to the boundary seq
    assert sched.at_seq() == job.lease_expires_after_seq, (
        "this test is only meaningful at the last live seq")

    sched.report(job_id="j1", worker="worker-a")
    assert sched.get("j1").state is JobState.SUCCEEDED
    assert _reload(tmp_path).get("j1").state is JobState.SUCCEEDED


# --- isolating the write path from the replay it now shares rules with ------
#
# The two ownership checks above made N6 and N7 -- the SAME two checks in
# report() -- survive, because transition() calls apply() on the event it
# just appended, so a deleted write-path check is caught by the replay one.
#
# They are not redundant, and the difference is the whole reason the write
# path exists: it refuses BEFORE the append. With the write-path check gone,
# report() still raises, but only after the record is a permanent,
# hash-chained fact -- and the queue can no longer be loaded at all, because
# every future replay refuses that record forever. A refusal that leaves the
# log unloadable is not the same refusal.
#
# So each test below provokes one check with the other unable to fire, by
# asserting on the LOG rather than on the exception.

def _events(sched):
    return sched.log.verify().count


def test_a_refused_report_from_a_non_holder_appends_nothing(sched):
    _dispatched(sched)
    before = _events(sched)
    with pytest.raises(JobTransitionError, match="leased to 'worker-a'"):
        sched.report(job_id="j1", worker="mallory")
    assert _events(sched) == before, (
        "the report was refused, but the record reached the log; every "
        "future load of this queue now refuses that record forever")
    # And the queue still loads, which is what the append would have cost.
    assert sched.load().get("j1").state is JobState.DISPATCHED


def test_a_refused_report_from_a_lapsed_lease_appends_nothing(sched):
    _dispatched(sched, lease_seqs=1)
    for i in range(4):                        # push past the expiry
        _enqueue(sched, f"filler{i}")
    before = _events(sched)
    with pytest.raises(JobTransitionError, match="lapsed"):
        sched.report(job_id="j1", worker="worker-a")
    assert _events(sched) == before, (
        "a late report was refused and still became a permanent record")
    assert sched.load().get("j1").state is JobState.DISPATCHED


# --- the priority gate, re-run on replay ------------------------------------
#
# set_priority calls the policy for a RAISE -- "raising it is a policy
# decision, not a setter" -- and that call lived on the write path alone. A
# job could be lifted to the front of the queue by appending the record
# set_priority had just refused. Starvation is not a state-machine violation,
# so nothing else here would have noticed.

def _deny_raises(log):
    """A policy under which raising priority is refused, everything else is
    allowed. Published so the gate has something real to consult."""
    from qta_agent.policy import ANY, Effect, document, rule

    pol = PolicyStore(log).load()
    pol.publish(document(policy_id="scheduler.default", version=1, rules=(
        rule(rule_id="no-raise", effect=Effect.DENY,
             actions=("scheduler.raise_priority",), subjects=(ANY,),
             roles=(ANY,), resources=(ANY,),
             reason="urgency is an operator decision"),
        rule(rule_id="allow-rest", effect=Effect.ALLOW, actions=(ANY,),
             subjects=(ANY,), roles=(ANY,), resources=(ANY,)),
    )), actor="owner")
    return pol


def _strict(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    pol = _deny_raises(log)
    s = Scheduler(log, policy=pol, policy_id="scheduler.default").load()
    s.enqueue(job_id="j1", work_digest=WORK, submitter="alice",
              priority=MAX_PRIORITY)
    return log, s


def test_the_write_path_refuses_a_raise_the_policy_denies(tmp_path):
    """Stated first, so the test below is visibly the same record."""
    _, s = _strict(tmp_path)
    with pytest.raises(PolicyDenied, match="raise_priority"):
        s.set_priority(job_id="j1", priority=0, actor="mallory",
                       role="WORKER", reason="mine is urgent")


def test_and_replay_refuses_it_when_it_arrives_as_a_record(tmp_path):
    log, s = _strict(tmp_path)
    log.append(actor="mallory", action="scheduler.priority", target="j1",
               payload={"job_id": "j1", "priority": 0, "role": "WORKER",
                        "reason": "mine is urgent"})
    log2 = EventLog(tmp_path / "log.jsonl")
    pol2 = PolicyStore(log2).load()
    with pytest.raises(SchedulerError, match="does not become the queue"):
        Scheduler(log2, policy=pol2,
                  policy_id="scheduler.default").load()


def test_a_raise_with_no_reason_is_refused_on_replay(tmp_path):
    """The write path demands one; so does the replay."""
    log, s = _strict(tmp_path)
    log.append(actor="mallory", action="scheduler.priority", target="j1",
               payload={"job_id": "j1", "priority": 0, "role": "WORKER"})
    log2 = EventLog(tmp_path / "log.jsonl")
    pol2 = PolicyStore(log2).load()
    with pytest.raises(SchedulerError, match="no reason"):
        Scheduler(log2, policy=pol2, policy_id="scheduler.default").load()


def test_lowering_urgency_needs_no_gate_and_still_replays(tmp_path):
    """The guard must refuse a RAISE, not every priority change.

    Deprioritising your own work is not an escalation, and a check that
    refused it would be removed rather than fixed.
    """
    log, s = _strict(tmp_path)
    s.set_priority(job_id="j1", priority=MAX_PRIORITY - 0, actor="alice",
                   role="SUBMITTER", reason="can wait")
    s.enqueue(job_id="j2", work_digest=OTHER_WORK, submitter="alice",
              priority=3)
    s.set_priority(job_id="j2", priority=7, actor="alice", role="SUBMITTER",
                   reason="less urgent than I thought")
    log2 = EventLog(tmp_path / "log.jsonl")
    pol2 = PolicyStore(log2).load()
    reloaded = Scheduler(log2, policy=pol2,
                         policy_id="scheduler.default").load()
    assert reloaded.get("j2").priority == 7


def test_a_permitted_raise_replays(sched):
    """And under the default policy, which permits it, a raise survives a
    reload -- so the gate is refusing the DECISION, not the operation."""
    _enqueue(sched, "j1", priority=MAX_PRIORITY)
    sched.set_priority(job_id="j1", priority=1, actor="scheduler",
                       role="SCHEDULER", reason="a real deadline")
    assert sched.get("j1").priority == 1


# --- an enqueue introduces work; it does not assert a lifecycle -------------
#
# job_from_record validates the SHAPE of a job and nothing else, so a forged
# enqueue could name any field of one. enqueue() never lets a caller pick
# them: it constructs the Job itself, at the initial state, with no lease and
# no attempts.

def _forged_enqueue(sched, **over):
    rec = {"job_id": "forged", "work_digest": WORK, "submitter": "mallory",
           "priority": 0}
    rec.update(over)
    sched.log.append(actor="mallory", action="scheduler.enqueue",
                     target=rec["job_id"], payload={"job": rec})


def test_a_job_cannot_be_enqueued_already_finished(sched, tmp_path):
    """A job born SUCCEEDED never ran, never held a lease, was never
    verified -- and anything downstream reading SUCCEEDED as 'this work was
    done' is reading a fabrication."""
    _forged_enqueue(sched, state=JobState.SUCCEEDED.value)
    with pytest.raises(SchedulerError, match="enqueued already in SUCCEEDED"):
        _reload(tmp_path)


def test_a_job_cannot_be_enqueued_already_holding_a_lease(sched, tmp_path):
    """THE ONE THAT REOPENED AN EARLIER FIX.

    The outcome edges check that the reporter holds the lease. A forged
    enqueue that arrives already DISPATCHED to mallory supplies that
    precondition, so mallory can then report on it legitimately. A guard
    that can be handed its own precondition is not a guard, and this is why
    the enqueue path had to be closed and not only the report path.
    """
    _forged_enqueue(sched, state=JobState.DISPATCHED.value,
                    lease_id="L-forged", lease_holder="mallory",
                    lease_expires_after_seq=10 ** 9)
    with pytest.raises(SchedulerError, match="enqueued already in DISPATCHED"):
        _reload(tmp_path)


def test_a_forged_enqueue_cannot_rebind_a_live_idempotency_key(sched,
                                                                tmp_path):
    """enqueue() refuses this because it 'would suppress a real request as
    if it were a retry'. The replay OVERWROTE the binding, so the real
    request is the one that gets refused afterwards."""
    _enqueue(sched, "real", idempotency_key="k1")
    _forged_enqueue(sched, work_digest=OTHER_WORK, idempotency_key="k1")
    with pytest.raises(SchedulerError, match="suppress a real request"):
        _reload(tmp_path)


def test_an_enqueue_may_not_name_a_submitter_it_did_not_have(sched, tmp_path):
    _forged_enqueue(sched, submitter="alice")
    with pytest.raises(SchedulerError, match="was appended by"):
        _reload(tmp_path)


@pytest.mark.parametrize("over,match", [
    ({"attempts": 5}, "attempts and revision"),
    ({"revision": 7}, "attempts and revision"),
    ({"max_attempts": 0}, "retry budget"),
    ({"priority": 99}, "outside"),
    ({"work_digest": "not-a-digest"}, "no work digest"),
    ({"depends_on": ["forged"]}, "cannot depend on itself"),
    ({"depends_on": ["nonexistent"]}, "does not exist"),
])
def test_replay_refuses_an_enqueue_the_write_path_would_have(sched, tmp_path,
                                                             over, match):
    """Each rule enqueue() enforces, provoked one at a time on the replay."""
    _forged_enqueue(sched, **over)
    with pytest.raises(SchedulerError, match=match):
        _reload(tmp_path)


def test_replay_refuses_an_enqueue_onto_a_dependency_that_cannot_succeed(
        sched, tmp_path):
    """The liveness rule Hypothesis found, enforced on the replay too.

    Waiting forever looks exactly like slow, so a job admitted onto a dead
    dependency is a defect nothing else reports.
    """
    _enqueue(sched, "parent")
    sched.cancel(job_id="parent", actor="owner", reason="not wanted")
    _forged_enqueue(sched, depends_on=["parent"])
    with pytest.raises(SchedulerError, match="unreachable"):
        _reload(tmp_path)


def test_an_ordinary_enqueue_still_replays(sched, tmp_path):
    """The guard must refuse forgeries, not work.

    Every rule above is enforced on the honest path by enqueue() already, so
    a check that also refused a real enqueue would make the queue unloadable
    and be deleted rather than fixed.
    """
    _enqueue(sched, "j1", idempotency_key="k1")
    _enqueue(sched, "j2", depends_on=("j1",))
    reloaded = _reload(tmp_path)
    assert reloaded.get("j1").state is JobState.WAITING
    assert reloaded.get("j2").depends_on == ("j1",)
    assert reloaded._keys["k1"][0] == "j1"


# --- the four survivors, each isolated from the guard that masked it --------

def test_a_legal_state_that_still_arrives_holding_a_lease_is_refused(
        sched, tmp_path):
    """ISOLATES the lease check from the state check above it.

    The first version of this test used state=DISPATCHED, so the STATE guard
    fired and the lease guard never ran -- and a mutation deleting the lease
    guard survived every test in this file. WAITING is a legal state to be
    enqueued in, so only the lease is wrong here.
    """
    _forged_enqueue(sched, state=JobState.WAITING.value, lease_id="L",
                    lease_holder="mallory", lease_expires_after_seq=10 ** 9)
    with pytest.raises(SchedulerError, match="enqueued already leased"):
        _reload(tmp_path)


def test_replay_re_runs_the_enqueue_policy_gate(tmp_path):
    """enqueue() consults the policy; so does the replay.

    Otherwise a submitter the policy refuses gets its work into the queue by
    writing the record itself, which is the whole shape of this class.
    """
    from qta_agent.policy import ANY, Effect, document, rule

    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    pol.publish(document(policy_id="scheduler.default", version=1, rules=(
        rule(rule_id="no-mallory", effect=Effect.DENY,
             actions=("scheduler.enqueue",), subjects=("mallory",),
             roles=(ANY,), resources=(ANY,),
             reason="mallory may not submit work"),
        rule(rule_id="allow-rest", effect=Effect.ALLOW, actions=(ANY,),
             subjects=(ANY,), roles=(ANY,), resources=(ANY,)),
    )), actor="owner")
    s = Scheduler(log, policy=pol, policy_id="scheduler.default").load()
    with pytest.raises(PolicyDenied):
        s.enqueue(job_id="j1", work_digest=WORK, submitter="mallory")

    log.append(actor="mallory", action="scheduler.enqueue", target="j1",
               payload={"job": {"job_id": "j1", "work_digest": WORK,
                                "submitter": "mallory", "priority": 5}})
    log2 = EventLog(tmp_path / "log.jsonl")
    pol2 = PolicyStore(log2).load()
    with pytest.raises(SchedulerError, match="refuses to enqueue"):
        Scheduler(log2, policy=pol2, policy_id="scheduler.default").load()


def test_the_priority_gate_matches_on_the_actor_not_a_payload_field(
        tmp_path):
    """The subject a policy rule matches on is the event's actor.

    A record that could supply its own subject would pick which rule judges
    it, which is the same defect as naming your own executor.
    """
    from qta_agent.policy import ANY, Effect, document, rule

    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    pol.publish(document(policy_id="scheduler.default", version=1, rules=(
        rule(rule_id="no-mallory", effect=Effect.DENY,
             actions=("scheduler.raise_priority",), subjects=("mallory",),
             roles=(ANY,), resources=(ANY,), reason="not mallory"),
        rule(rule_id="allow-rest", effect=Effect.ALLOW, actions=(ANY,),
             subjects=(ANY,), roles=(ANY,), resources=(ANY,)),
    )), actor="owner")
    s = Scheduler(log, policy=pol, policy_id="scheduler.default").load()
    s.enqueue(job_id="j1", work_digest=WORK, submitter="alice",
              priority=MAX_PRIORITY)
    # mallory raises it, but names a subject the policy permits.
    log.append(actor="mallory", action="scheduler.priority", target="j1",
               payload={"job_id": "j1", "priority": 0, "role": "SUBMITTER",
                        "subject": "alice", "reason": "urgent"})
    log2 = EventLog(tmp_path / "log.jsonl")
    pol2 = PolicyStore(log2).load()
    with pytest.raises(SchedulerError, match="does not become the queue"):
        Scheduler(log2, policy=pol2, policy_id="scheduler.default").load()


def test_a_raise_is_judged_by_the_policy_of_its_own_day(tmp_path):
    """in_force_at, not in_force -- and here that difference is REAL.

    The identical mutation in policy.py is equivalent, because that reducer
    folds its own events in order so only the prefix is loaded when a
    decision is re-checked. The scheduler's policy store is a SEPARATE
    projection, already loaded to the end of the log before the queue
    replays, so in_force yields the newest version and in_force_at yields
    the one that governed the change. Same edit, opposite verdicts, for a
    structural reason worth writing down.

    Without this, publishing a stricter policy would make every queue that
    ever raised a priority unloadable.
    """
    from qta_agent.policy import ANY, Effect, document, rule

    def doc(version, extra=()):
        return document(policy_id="scheduler.default", version=version,
                        rules=tuple(extra) + (
                            rule(rule_id="allow", effect=Effect.ALLOW,
                                 actions=(ANY,), subjects=(ANY,),
                                 roles=(ANY,), resources=(ANY,)),))

    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    pol.publish(doc(1), actor="owner")
    s = Scheduler(log, policy=pol, policy_id="scheduler.default").load()
    s.enqueue(job_id="j1", work_digest=WORK, submitter="alice",
              priority=MAX_PRIORITY)
    s.set_priority(job_id="j1", priority=1, actor="alice", role="SUBMITTER",
                   reason="a real deadline")
    # A later, stricter edition. It must not re-judge what came before it.
    pol.publish(doc(2, (rule(rule_id="no-raise", effect=Effect.DENY,
                             actions=("scheduler.raise_priority",),
                             subjects=(ANY,), roles=(ANY,), resources=(ANY,),
                             reason="v2 forbids raises"),)), actor="owner")

    log2 = EventLog(tmp_path / "log.jsonl")
    pol2 = PolicyStore(log2).load()
    reloaded = Scheduler(log2, policy=pol2,
                         policy_id="scheduler.default").load()
    assert reloaded.get("j1").priority == 1
    assert reloaded.policy.in_force("scheduler.default").version == 2


# ==========================================================================
# LEASE RENEWAL
#
# THE GAP THIS CLOSES, stated as it was found:
#
#     "no lease RENEWAL: a worker holding long work cannot extend, so a
#      lease must be sized for the worst case rather than the common one"
#
# Sizing for the worst case is what made lapse useless: a worker with an
# hour of work took an hour-long lease, and a worker that died in the first
# minute held the job for fifty-nine more. Renewal is also the one change
# that can make a lease stop meaning anything, so every test below is an
# attempt to use it that way.
# ==========================================================================

def _leased(sched, *, lease_seqs=6, worker="w1", lease_id="L1"):
    _enqueue(sched)
    sched.reconcile()
    return sched.dispatch(job_id="j1", worker=worker, lease_id=lease_id,
                          lease_seqs=lease_seqs)


def test_the_holder_can_extend_a_live_lease(sched):
    job = _leased(sched)
    before = job.lease_expires_after_seq

    job = sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                            lease_seqs=50)

    assert job.lease_expires_after_seq > before
    assert job.lease_renewals == 1
    assert job.state is JobState.DISPATCHED


def test_a_renewal_replays_to_the_same_state(sched, tmp_path):
    """The end is computed from the replay's position, so a second reader of
    the same log has to reach the same number."""
    job = _leased(sched)
    job = sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                            lease_seqs=50)

    revived = Scheduler(EventLog(tmp_path / "log.jsonl"),
                        policy=PolicyStore(EventLog(tmp_path / "log.jsonl")
                                           ).load(),
                        policy_id="scheduler.default",
                        capacity={"slots": 2}).load()
    assert revived.get("j1").to_record() == job.to_record()


def test_a_lapsed_lease_cannot_be_renewed(sched):
    """THE rule renewal must not break.

    By the time a lease has lapsed, reconcile may already have returned the
    work to READY and given it to somebody else. Renewal extends possession;
    it must never re-acquire it.
    """
    _leased(sched, lease_seqs=1)
    for i in range(4):                       # move the log past the lease
        sched.enqueue(job_id=f"filler-{i}", work_digest=OTHER_WORK,
                      submitter="sub")

    with pytest.raises(JobTransitionError) as exc:
        sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                          lease_seqs=50)
    assert "lapsed" in str(exc.value)
    assert "not renewed, it is lost" in str(exc.value)


def test_a_renewal_from_someone_else_is_refused(sched):
    _leased(sched)
    with pytest.raises(JobTransitionError) as exc:
        sched.renew_lease(job_id="j1", worker="mallory", lease_id="L1",
                          lease_seqs=50)
    assert "held by" in str(exc.value)


def test_a_renewal_citing_a_stale_lease_id_is_refused(sched):
    """A worker back from the dead carries the old id.

    It is the same NAME as the live holder -- that is what makes it
    dangerous -- and the lease id is the only thing that tells the two
    apart.
    """
    _leased(sched, lease_seqs=1)
    for i in range(4):
        sched.enqueue(job_id=f"filler-{i}", work_digest=OTHER_WORK,
                      submitter="sub")
    sched.reconcile()                        # j1 goes back to READY
    sched.dispatch(job_id="j1", worker="w1", lease_id="L2", lease_seqs=20)

    with pytest.raises(JobTransitionError) as exc:
        sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                          lease_seqs=50)
    assert "old id" in str(exc.value)


def test_renewal_is_bounded(sched):
    """Without a bound, a worker holds the job forever by asking often
    enough, which is the same as having no lease."""
    _leased(sched, lease_seqs=200)
    for i in range(MAX_LEASE_RENEWALS):
        sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                          lease_seqs=200)
    assert sched.get("j1").lease_renewals == MAX_LEASE_RENEWALS

    with pytest.raises(JobTransitionError) as exc:
        sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                          lease_seqs=200)
    assert "bound" in str(exc.value)


def test_a_renewal_that_would_shorten_is_refused(sched):
    _leased(sched, lease_seqs=500)
    with pytest.raises(SchedulerError) as exc:
        sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                          lease_seqs=1)
    assert "would not extend" in str(exc.value)


def test_the_record_does_not_get_to_name_its_own_expiry(sched):
    """A forged renewal naming a huge expiry must not become one.

    The payload carries a REQUEST. The replay decides what that means from
    where it is standing, which is the same rule that stops a capability
    naming its own issued_seq.
    """
    from qta_agent.scheduler import ACT_LEASE_RENEW
    _leased(sched)
    sched.log.append(actor="w1", action=ACT_LEASE_RENEW, target="j1",
                     payload={"job_id": "j1", "lease_id": "L1",
                              "lease_seqs": 40,
                              "lease_expires_after_seq": 10 ** 9})
    revived = sched.load()
    assert revived.get("j1").lease_expires_after_seq < 10 ** 6


def test_a_forged_renewal_by_a_non_holder_fails_on_replay(sched, tmp_path):
    """The write path refuses it, so an attacker appends it directly."""
    from qta_agent.scheduler import ACT_LEASE_RENEW
    _leased(sched)
    sched.log.append(actor="mallory", action=ACT_LEASE_RENEW, target="j1",
                     payload={"job_id": "j1", "lease_id": "L1",
                              "lease_seqs": 500})
    with pytest.raises(JobTransitionError) as exc:
        sched.load()
    assert "mallory" in str(exc.value)


def test_a_renewal_of_a_job_that_is_not_dispatched_is_refused(sched):
    from qta_agent.scheduler import ACT_LEASE_RENEW
    _enqueue(sched)
    sched.reconcile()
    sched.log.append(actor="w1", action=ACT_LEASE_RENEW, target="j1",
                     payload={"job_id": "j1", "lease_id": "L1",
                              "lease_seqs": 5})
    with pytest.raises(JobTransitionError) as exc:
        sched.load()
    assert "only a DISPATCHED job" in str(exc.value)


def test_a_requeued_job_starts_its_renewal_budget_again(sched):
    """A count carried across dispatches would make one worker's extensions
    limit the next worker's, for reasons belonging to a lease that no longer
    exists."""
    _leased(sched, lease_seqs=200)
    sched.renew_lease(job_id="j1", worker="w1", lease_id="L1", lease_seqs=300)
    assert sched.get("j1").lease_renewals == 1

    sched.report(job_id="j1", worker="w1", failure=FailureClass.TIMEOUT,
                 detail="slow")
    job = sched.get("j1")
    assert job.state is not JobState.DISPATCHED
    assert job.lease_renewals == 0, (
        "the renewal count survived the lease it belonged to")
