"""Property and stateful tests for the TASK and JOB state machines.

The authority record machine already has this treatment in
``test_agent_substrate_properties.py``. These two did not, and the gap was the
wrong way round: the record machine has six named invariants and a small
table, while the task machine has leases and separation of duties and the job
machine has readiness, retry budgets, cancellation cascades and aging. Those
are the ones whose reachable states nobody can enumerate by hand.

Example-based tests prove a rule holds for the cases someone thought of. A
stateful test drives the real objects through arbitrary interleavings and
checks the invariants after every single step, so a violation arrives as the
shortest sequence that produces it rather than as a bug report months later.

WHAT THESE DO NOT PROVE

Nothing here is a scientific claim, and a passing property is not a proof:
Hypothesis explores, it does not exhaust. A property that holds over ten
thousand generated histories is evidence that the invariant is not trivially
violable, which is exactly as much as it sounds like.
"""
from __future__ import annotations

import os
import sys

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle, RuleBasedStateMachine, invariant, rule,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qta_agent import scheduler as sch  # noqa: E402
from qta_agent import tasks as tk  # noqa: E402
from qta_agent.capability import (  # noqa: E402
    Action, CapabilityDenied, CapabilityError, CapabilityLedger,
    CapabilityExpired, CapabilityRevoked, Request, issue,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.scheduler import (  # noqa: E402
    FailureClass, JobState, Scheduler, backoff_for, default_policy,
)
from qta_agent.tasks import (  # noqa: E402
    Lease, Task, TaskRole, TaskState, TaskTransition, TaskTransitionError,
    LeaseError,
)

DIG = "c" * 64

task_states = st.sampled_from(list(TaskState))
task_roles = st.sampled_from(list(TaskRole))
job_states = st.sampled_from(list(JobState))
#: Sampled directly rather than filtered with assume(): sealed states are a
#: small minority of both machines, and filtering for them throws away most
#: generated inputs, which Hypothesis rightly reports as distorting.
sealed_task_states = st.sampled_from(sorted(tk.SEALED, key=lambda s: s.value))
sealed_job_states = st.sampled_from(sorted(sch.SEALED, key=lambda s: s.value))
identities = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=10)


def _task(state, **kw):
    base = dict(task_id="t1", tool_id="probe", submitter="sub",
                inputs_digest=DIG, state=state)
    base.update(kw)
    return Task(**base)


# ---------------------------------------------------------------------------
# the task machine, as a table
# ---------------------------------------------------------------------------

@given(src=sealed_task_states, dst=task_states, role=task_roles)
@settings(max_examples=400, deadline=None)
def test_no_role_can_leave_a_sealed_task_state(src, dst, role):
    """A sealed state has no outgoing edge, for anyone, ever.

    SEALED is derived from the table rather than written beside it, so this
    is a check that the derivation and the guard agree for every pair.
    """
    with pytest.raises(TaskTransitionError):
        tk.check(TaskTransition(task_id="t1", src=src, dst=dst, actor="a",
                                role=role, at_seq=1, lease_id="L",
                                executed_by="w", result_digest=DIG),
                 _task(src, executed_by="w"))


@given(src=task_states, role=task_roles)
@settings(max_examples=200, deadline=None)
def test_verified_is_reachable_only_from_completed(src, role):
    """The task machine's I1. One edge in, and it needs a verifier."""
    assume(not (src is TaskState.COMPLETED and role is TaskRole.VERIFIER))
    with pytest.raises(TaskTransitionError):
        tk.check(TaskTransition(task_id="t1", src=src,
                                dst=TaskState.VERIFIED, actor="v", role=role,
                                at_seq=1, executed_by="w"),
                 _task(src, executed_by="w"))


@given(actor=identities)
@settings(max_examples=100, deadline=None)
def test_self_verification_is_refused_for_any_identity(actor):
    """An agent that verifies its own work has not verified anything."""
    with pytest.raises(TaskTransitionError, match="may not also"):
        tk.check(TaskTransition(task_id="t1", src=TaskState.COMPLETED,
                                dst=TaskState.VERIFIED, actor=actor,
                                role=TaskRole.VERIFIER, at_seq=1,
                                executed_by=actor),
                 _task(TaskState.COMPLETED, executed_by=actor))


@given(holder=identities, actor=identities)
@settings(max_examples=200, deadline=None)
def test_a_lease_authorizes_only_its_holder(holder, actor):
    assume(holder != actor)
    task = _task(TaskState.EXECUTING, executed_by=holder,
                 lease=Lease(lease_id="L1", holder=holder, granted_seq=0,
                             expires_after_seq=100))
    with pytest.raises(LeaseError, match="held by"):
        tk.check(TaskTransition(task_id="t1", src=TaskState.EXECUTING,
                                dst=TaskState.COMPLETED, actor=actor,
                                role=TaskRole.WORKER, at_seq=1,
                                lease_id="L1", result_digest=DIG),
                 task)


@given(at_seq=st.integers(min_value=101, max_value=10_000))
@settings(max_examples=100, deadline=None)
def test_a_lapsed_lease_never_authorizes_however_late(at_seq):
    """A worker back from the dead does not get to report success."""
    task = _task(TaskState.EXECUTING, executed_by="w",
                 lease=Lease(lease_id="L1", holder="w", granted_seq=0,
                             expires_after_seq=100))
    with pytest.raises(LeaseError, match="lapsed"):
        tk.check(TaskTransition(task_id="t1", src=TaskState.EXECUTING,
                                dst=TaskState.COMPLETED, actor="w",
                                role=TaskRole.WORKER, at_seq=at_seq,
                                lease_id="L1", result_digest=DIG),
                 task)


@given(bad=st.one_of(st.none(), st.text(max_size=70), st.integers()))
@settings(max_examples=200, deadline=None)
def test_completion_requires_a_real_digest(bad):
    """COMPLETED cites bytes. A citation that is not a digest is a sentence."""
    from qta_agent.canonical import is_digest

    assume(not is_digest(bad))
    task = _task(TaskState.EXECUTING, executed_by="w",
                 lease=Lease(lease_id="L1", holder="w", granted_seq=0,
                             expires_after_seq=100))
    with pytest.raises(TaskTransitionError):
        tk.check(TaskTransition(task_id="t1", src=TaskState.EXECUTING,
                                dst=TaskState.COMPLETED, actor="w",
                                role=TaskRole.WORKER, at_seq=1,
                                lease_id="L1", result_digest=bad),
                 task)


@given(src=task_states, dst=task_states, role=task_roles)
@settings(max_examples=500, deadline=None)
def test_every_permitted_task_move_is_a_declared_edge(src, dst, role):
    """The converse direction: nothing is permitted that the table lacks.

    A gate that permits a move the table does not describe is a gate whose
    behaviour cannot be reviewed by reading the table.
    """
    task = _task(src, executed_by="w",
                 lease=Lease(lease_id="L1", holder="a", granted_seq=0,
                             expires_after_seq=10_000))
    try:
        edge = tk.check(
            TaskTransition(task_id="t1", src=src, dst=dst, actor="a",
                           role=role, at_seq=1, lease_id="L1",
                           result_digest=DIG), task)
    except (TaskTransitionError, LeaseError):
        return
    assert tk.find_edge(src, dst) is edge
    assert role in edge.roles


# ---------------------------------------------------------------------------
# the job machine, as a table
# ---------------------------------------------------------------------------

@given(src=sealed_job_states, dst=job_states)
@settings(max_examples=400, deadline=None)
def test_no_job_leaves_a_sealed_state(src, dst):
    with pytest.raises(sch.JobTransitionError):
        sch.check_edge(src, dst, "j1")


@given(src=job_states, dst=job_states)
@settings(max_examples=400, deadline=None)
def test_every_permitted_job_move_is_a_declared_edge(src, dst):
    try:
        edge = sch.check_edge(src, dst, "j1")
    except sch.JobTransitionError:
        return
    assert sch.find_edge(src, dst) is edge


@given(a=st.integers(min_value=1, max_value=64),
       b=st.integers(min_value=1, max_value=64))
@settings(max_examples=200, deadline=None)
def test_backoff_is_monotone_and_capped(a, b):
    """Backoff must never shrink with attempts, and must never run away.

    A backoff that decreases lets a hot-looping failure retry faster the
    worse it gets; an uncapped one turns a transient outage into a job that
    is effectively abandoned without ever being marked failed.
    """
    lo, hi = min(a, b), max(a, b)
    assert backoff_for(lo) <= backoff_for(hi)
    assert backoff_for(hi) <= sch.BACKOFF_MAX_SEQS
    assert backoff_for(lo) >= 0


@given(priority=st.integers(min_value=-10,
                            max_value=sch.MAX_PRIORITY + 10),
       waited=st.integers(min_value=0, max_value=100_000))
@settings(max_examples=300, deadline=None)
def test_effective_priority_never_escapes_its_bounds(priority, waited):
    """Aging must relieve starvation without inventing authority.

    A job that waits long enough moves up the queue. It must not be able to
    age past the most urgent priority the scale defines, or the bound stops
    meaning anything and aging becomes a way to obtain urgency nobody granted.
    """
    assume(sch.MIN_PRIORITY <= priority <= sch.MAX_PRIORITY)
    job = sch.Job(job_id="j", work_digest=DIG, submitter="s",
                  priority=priority, enqueued_seq=0)
    eff = job.effective_priority(at_seq=waited)
    # Lower is more urgent, so aging SUBTRACTS and the floor is the bound.
    assert sch.MIN_PRIORITY <= eff <= sch.MAX_PRIORITY
    assert eff <= priority, "aging must never make a job less urgent"


# ---------------------------------------------------------------------------
# capability checking, over generated mismatches
# ---------------------------------------------------------------------------

def _grant(**kw):
    base = dict(capability_id="c1", subject="agent-1",
                action=Action.EXECUTE_TOOL, task_id="t1", tool_id="probe",
                scope=("verification/stage10/probe",), issued_seq=1,
                expires_after_seq=100)
    base.update(kw)
    return issue(**base)


@given(field=st.sampled_from(["actor", "task_id", "tool_id"]),
       other=identities)
@settings(max_examples=300, deadline=None)
def test_a_grant_refuses_a_request_differing_in_any_single_field(field, other):
    """Bounded means bounded along every axis, not the ones a test picked."""
    from qta_agent.capability import CapabilitySet

    base = dict(actor="agent-1", action=Action.EXECUTE_TOOL, task_id="t1",
                tool_id="probe", paths=("verification/stage10/probe/x",))
    assume(other != base[field])
    caps = CapabilitySet(issued={"c1": _grant()}, at_seq=2)
    caps.check("c1", Request(**base))          # the matching request passes
    with pytest.raises(CapabilityDenied):
        caps.check("c1", Request(**{**base, field: other}))


@given(at_seq=st.integers(min_value=101, max_value=100_000))
@settings(max_examples=200, deadline=None)
def test_an_expired_grant_authorizes_nothing_at_any_later_position(at_seq):
    from qta_agent.capability import CapabilitySet

    caps = CapabilitySet(issued={"c1": _grant()}, at_seq=at_seq)
    with pytest.raises(CapabilityExpired):
        caps.check("c1", Request(actor="agent-1", action=Action.EXECUTE_TOOL,
                                 task_id="t1", tool_id="probe",
                                 paths=("verification/stage10/probe/x",)))


@given(at_seq=st.integers(min_value=2, max_value=100))
@settings(max_examples=100, deadline=None)
def test_a_revoked_grant_authorizes_nothing_at_any_position(at_seq):
    from qta_agent.capability import CapabilitySet

    caps = CapabilitySet(issued={"c1": _grant()}, revoked=frozenset({"c1"}),
                         at_seq=at_seq)
    with pytest.raises(CapabilityRevoked):
        caps.check("c1", Request(actor="agent-1", action=Action.EXECUTE_TOOL,
                                 task_id="t1", tool_id="probe",
                                 paths=("verification/stage10/probe/x",)))


# ---------------------------------------------------------------------------
# stateful: drive the real scheduler through arbitrary histories
# ---------------------------------------------------------------------------

class SchedulerMachine(RuleBasedStateMachine):
    """Arbitrary interleavings of the queue's operations.

    The invariants below are the ones the scheduler exists to hold. Each is
    checked after EVERY step, so a violation is reported as the shortest
    sequence that reaches it rather than as whatever long history first
    happened to show it.
    """

    jobs = Bundle("jobs")

    def __init__(self):
        super().__init__()
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp())
        self.log = EventLog(self.dir / "log.jsonl")
        policy = PolicyStore(self.log).load()
        policy.publish(default_policy(), actor="owner")
        self.s = Scheduler(self.log, policy=policy,
                           policy_id=default_policy().policy_id)
        self.counter = 0
        self.leases = {}

    def _digest(self, n) -> str:
        return f"{n:064x}"

    @rule(target=jobs)
    def enqueue_root(self):
        self.counter += 1
        jid = f"j{self.counter}"
        self.s.enqueue(job_id=jid, work_digest=self._digest(self.counter),
                       submitter="submitter")
        return jid

    @rule(target=jobs, parent=jobs)
    def enqueue_child(self, parent):
        self.counter += 1
        jid = f"j{self.counter}"
        try:
            self.s.enqueue(job_id=jid, work_digest=self._digest(self.counter),
                           submitter="submitter", depends_on=(parent,))
        except sch.SchedulerError:
            # Refusing a dependency that can never succeed is the behaviour
            # under test. Enqueue the job without it so the bundle stays
            # populated and the run keeps exploring.
            self.s.enqueue(job_id=jid, work_digest=self._digest(self.counter),
                           submitter="submitter")
        return jid

    @rule()
    def reconcile(self):
        self.s.reconcile()

    @rule(jid=jobs, lease_seqs=st.integers(min_value=1, max_value=40))
    def dispatch(self, jid, lease_seqs):
        if self.s.get(jid).state is not JobState.READY:
            return
        lease = f"L{jid}-{self.s.at_seq()}"
        self.s.dispatch(job_id=jid, worker="worker-1", lease_id=lease,
                        lease_seqs=lease_seqs)
        self.leases[jid] = lease

    @rule(jid=jobs,
          failure=st.one_of(st.none(), st.sampled_from(list(FailureClass))))
    def report(self, jid, failure):
        if self.s.get(jid).state is not JobState.DISPATCHED:
            return
        try:
            self.s.report(job_id=jid, worker="worker-1", failure=failure)
        except sch.JobTransitionError:
            # A lapsed lease refusing a late report is the behaviour under
            # test, not a failure of it. Nothing moved, so the invariants
            # still hold over the state this leaves.
            pass

    @rule(jid=jobs)
    def cancel(self, jid):
        if self.s.get(jid).state in sch.TERMINAL:
            return
        self.s.cancel(job_id=jid, actor="owner", reason="property test")

    @rule(jid=jobs)
    def invalidate(self, jid):
        if self.s.get(jid).state is not JobState.SUCCEEDED:
            return
        self.s.invalidate(job_id=jid, actor="owner", reason="input changed")

    # ---- invariants ----------------------------------------------------
    @invariant()
    def the_log_always_verifies(self):
        assert self.log.verify().ok

    @invariant()
    def no_job_ever_leaves_a_sealed_state(self):
        """Read from the LOG, so it catches a move the projection smoothed."""
        seen = {}
        for ev in self.log.read():
            if ev.action != sch.ACT_JOB_TRANSITION:
                continue
            jid = ev.payload["job_id"]
            prev = seen.get(jid)
            src = JobState(ev.payload["src"])
            assert prev is None or prev not in sch.SEALED, (
                f"{jid} left sealed state {prev}")
            assert prev is None or prev == src, (
                f"{jid}: transition claims src {src}, projection was at "
                f"{prev}")
            seen[jid] = JobState(ev.payload["dst"])

    @invariant()
    def a_ready_job_has_no_unsatisfied_dependency(self):
        jobs = self.s.all_jobs()
        for jid, job in jobs.items():
            if job.state is not JobState.READY:
                continue
            for dep in job.depends_on:
                assert jobs[dep].state is JobState.SUCCEEDED, (
                    f"{jid} is READY while dependency {dep} is "
                    f"{jobs[dep].state.value}")

    @invariant()
    def nothing_pending_waits_on_work_nobody_will_do(self):
        """The cancellation cascade's whole point.

        A dependent left WAITING on a CANCELLED or FAILED parent waits
        forever, and waiting forever looks exactly like slow.
        """
        jobs = self.s.all_jobs()
        dead = {JobState.CANCELLED, JobState.FAILED, JobState.INVALIDATED}
        for jid, job in jobs.items():
            if job.state not in sch.PENDING:
                continue
            for dep in job.depends_on:
                assert jobs[dep].state not in dead, (
                    f"{jid} is {job.state.value} while dependency {dep} is "
                    f"{jobs[dep].state.value}; it will wait forever")

    @invariant()
    def a_dispatched_job_always_holds_a_lease(self):
        for jid, job in self.s.all_jobs().items():
            if job.state is JobState.DISPATCHED:
                assert job.lease_id, f"{jid} is DISPATCHED with no lease"
                assert job.lease_holder, f"{jid} has a lease with no holder"

    @invariant()
    def the_projection_equals_a_fresh_replay(self):
        """A live projection that drifts from the log is the log lying."""
        fresh = Scheduler(self.log, policy=self.s.policy,
                          policy_id=self.s.policy_id).load()
        live = {k: v.to_record() for k, v in self.s.all_jobs().items()}
        again = {k: v.to_record() for k, v in fresh.all_jobs().items()}
        assert live == again

    @invariant()
    def attempts_never_exceed_the_budget(self):
        for jid, job in self.s.all_jobs().items():
            assert job.attempts <= job.max_attempts, (
                f"{jid} made {job.attempts} attempts against a budget of "
                f"{job.max_attempts}")

    def teardown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


TestSchedulerMachine = SchedulerMachine.TestCase
TestSchedulerMachine.settings = settings(
    max_examples=30, stateful_step_count=25, deadline=None,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much,
                           HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------
# stateful: the capability ledger under arbitrary issue/revoke histories
# ---------------------------------------------------------------------------

class LedgerMachine(RuleBasedStateMachine):
    """Issue and revoke arbitrarily; the ledger must always agree with a
    replay, and a revoked grant must never come back."""

    caps = Bundle("caps")

    def __init__(self):
        super().__init__()
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp())
        self.log = EventLog(self.dir / "log.jsonl")
        self.ledger = CapabilityLedger(self.log).load()
        self.counter = 0
        self.revoked = set()

    @rule(target=caps)
    def issue_one(self):
        self.counter += 1
        cid = f"c{self.counter}"
        self.ledger.issue(_grant(capability_id=cid), actor="scheduler")
        return cid

    @rule(cid=caps)
    def revoke_one(self, cid):
        if cid in self.revoked:
            return
        self.ledger.revoke(cid, actor="owner", reason="property test")
        self.revoked.add(cid)

    @rule(cid=caps)
    def reissue_is_refused(self, cid):
        with pytest.raises(CapabilityError):
            self.ledger.issue(_grant(capability_id=cid), actor="scheduler")

    @invariant()
    def a_replay_reaches_the_same_state(self):
        fresh = CapabilityLedger(self.log).load()
        assert fresh.issued_ids() == self.ledger.issued_ids()
        assert fresh.revoked_ids() == self.ledger.revoked_ids()

    @invariant()
    def revocation_is_never_undone(self):
        assert set(self.ledger.revoked_ids()) == self.revoked

    @invariant()
    def a_revoked_grant_authorizes_nothing(self):
        caps = self.ledger.in_force(self.log.verify().head_seq)
        for cid in self.revoked:
            with pytest.raises(CapabilityRevoked):
                caps.check(cid, Request(
                    actor="agent-1", action=Action.EXECUTE_TOOL,
                    task_id="t1", tool_id="probe",
                    paths=("verification/stage10/probe/x",)))

    def teardown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


TestLedgerMachine = LedgerMachine.TestCase
TestLedgerMachine.settings = settings(
    max_examples=30, stateful_step_count=20, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
