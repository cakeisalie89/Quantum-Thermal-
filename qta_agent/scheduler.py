"""Durable scheduling: work that is chosen, not merely started.

WHAT A QUEUE IS NOT

An in-memory list of callables is a queue only while the process lives. When
it dies, the work does not fail -- it disappears, along with any record that
it was ever wanted. Nothing detects the loss, because the thing that would
have detected it was in the list too.

A job here is a record in the same hash-chained log as everything else. Its
position in the queue, its dependencies, its attempts, its owner and its reason
for being where it is are all projections of that log, so a restarted process
recovers the queue rather than starting an empty one.

READINESS IS A DECISION, NOT AN ARRIVAL

A job does not run because it exists. :meth:`Scheduler.readiness` asks, in
order: are the dependencies satisfied; is any of them cancelled, failed or
invalidated; has the cited evidence resolved; is the required capability live;
does the policy in force permit this dispatch; is there capacity. Each answer
is recorded, so "why has this not run" is answerable without reading code.

Crucially, a dependency that FAILED does not leave its dependents waiting
forever. They move to :attr:`JobState.BLOCKED`, which is a state an operator
can query, rather than sitting in WAITING looking like they are about to start.

TWO STATE MACHINES, ON PURPOSE

:mod:`qta_agent.tasks` owns execution lifecycle: leased, executing, completed,
verified. This module owns queue lifecycle: waiting, ready, dispatched,
retrying. They are separate because they answer different questions and fail
in different ways -- a task can be EXECUTING while its job is being cancelled,
and collapsing the two would make that race unrepresentable rather than
handled.

EVERY CLOCK IS THE LOG

Backoff, lease expiry and aging are all measured in sequence numbers. Wall
time is recorded and never consulted for a decision, for the same reason as in
:mod:`qta_agent.capability`: two readers of the same log must reach the same
verdict, and they do not share a clock.

RETRY IS CLASSIFIED, NOT AUTOMATIC

:class:`FailureClass` distinguishes what may be retried from what may not. A
policy denial, a failed verification and a fabricated-evidence failure are
never retried: retrying a refusal converts it into a rate-limited refusal, and
retrying a failed verification is how a flaky pass is eventually obtained.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import FrozenSet

from .canonical import digest, is_digest
from .policy import Effect, PolicyRequest, document, rule

ACT_ENQUEUE = "scheduler.enqueue"
ACT_JOB_TRANSITION = "scheduler.transition"
ACT_PRIORITY = "scheduler.priority"

#: Priorities are bounded and small. 0 is most urgent. An unbounded priority
#: is a denial-of-service on every other job, and a float one makes ordering
#: depend on representation.
MIN_PRIORITY = 0
MAX_PRIORITY = 9

#: A job's effective priority improves by one step for every this many
#: sequence numbers it has waited. Bounded aging is the whole starvation
#: defence: without it a stream of priority-0 work starves priority-9 work
#: forever, and with unbounded aging priority stops meaning anything.
AGING_INTERVAL = 100

#: Backoff doubles per attempt, in sequence numbers, capped here.
BACKOFF_BASE_SEQS = 4
BACKOFF_MAX_SEQS = 1024

#: Attempts allowed before a retryable failure becomes permanent.
DEFAULT_MAX_ATTEMPTS = 3

#: Refused above this. A dependency list nobody can read is not a dependency
#: list, and an unbounded one makes readiness evaluation unbounded too.
MAX_DEPENDENCIES = 64


class SchedulerError(Exception):
    """Base class. Every failure here is fail-closed."""


class JobTransitionError(SchedulerError):
    """The queue state machine forbids this move."""


class UnknownJob(SchedulerError):
    """No such job in the projection."""


class DuplicateJob(SchedulerError):
    """This job, or this idempotency key, is already present."""


class CapacityError(SchedulerError):
    """The declared resource need can never be met by this executor."""


class JobState(str, Enum):
    """Where a unit of work sits in the queue."""

    #: Enqueued; at least one dependency is not yet satisfied.
    WAITING = "WAITING"
    #: Every precondition holds; eligible for dispatch.
    READY = "READY"
    #: Handed to a worker under a lease.
    DISPATCHED = "DISPATCHED"
    #: A retryable failure; waiting for backoff to elapse.
    RETRY_WAIT = "RETRY_WAIT"
    #: The underlying work was verified.
    SUCCEEDED = "SUCCEEDED"
    #: Permanently failed, or the retry budget is spent.
    FAILED = "FAILED"
    #: An upstream dependency failed, was cancelled, or was invalidated.
    #: A distinct state from FAILED because this job never ran, and an
    #: operator's next action differs completely.
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    #: Succeeded once, then an input it depended on changed.
    INVALIDATED = "INVALIDATED"


#: The work is finished: no further PROGRESS is possible. Not the same as
#: sealed -- a SUCCEEDED job can still have a fact recorded about it, namely
#: that one of its inputs later changed. Collapsing the two distinctions is
#: what made the task machine's equivalent edge unreachable while the table
#: still declared it.
TERMINAL: FrozenSet[JobState] = frozenset({
    JobState.SUCCEEDED, JobState.FAILED, JobState.BLOCKED,
    JobState.CANCELLED, JobState.INVALIDATED,
})

#: States in which a job is neither finished nor running: it is the
#: scheduler's outstanding work.
PENDING: FrozenSet[JobState] = frozenset({
    JobState.WAITING, JobState.READY, JobState.RETRY_WAIT,
})


class FailureClass(str, Enum):
    """Why an attempt did not succeed. Decides whether it may be retried."""

    #: Deterministic. The same inputs will fail the same way.
    PERMANENT = "PERMANENT"
    #: Infrastructure: a disk, a socket, a runner that went away.
    TRANSIENT = "TRANSIENT"
    TIMEOUT = "TIMEOUT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    CANCELLED = "CANCELLED"
    #: The policy in force refused it.
    POLICY_DENIED = "POLICY_DENIED"
    #: A cited digest did not resolve, or artifacts did not re-derive.
    EVIDENCE_FAILED = "EVIDENCE_FAILED"
    #: An independent verifier rejected the result.
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


#: The only classes a retry may follow. Everything else is permanent, and the
#: three security-relevant ones are permanent deliberately: retrying a
#: refusal turns it into a slower refusal, and retrying a failed verification
#: is a way of eventually obtaining a pass.
RETRYABLE: FrozenSet[FailureClass] = frozenset({
    FailureClass.TRANSIENT, FailureClass.TIMEOUT,
    FailureClass.RESOURCE_EXHAUSTED,
})


@dataclass(frozen=True)
class JobEdge:
    src: JobState
    dst: JobState
    reason: str


def _edges() -> tuple:
    E = JobEdge
    pre_terminal = (JobState.WAITING, JobState.READY, JobState.DISPATCHED,
                    JobState.RETRY_WAIT)
    return (
        E(JobState.WAITING, JobState.READY, "every precondition holds"),
        E(JobState.WAITING, JobState.BLOCKED,
          "a dependency will never satisfy"),
        E(JobState.READY, JobState.DISPATCHED, "a worker took ownership"),
        E(JobState.READY, JobState.BLOCKED,
          "a dependency stopped satisfying before dispatch"),
        E(JobState.READY, JobState.WAITING,
          "a precondition stopped holding before dispatch"),
        E(JobState.DISPATCHED, JobState.SUCCEEDED, "the work was verified"),
        E(JobState.DISPATCHED, JobState.RETRY_WAIT,
          "a retryable failure, with attempts remaining"),
        E(JobState.DISPATCHED, JobState.FAILED,
          "a permanent failure, or the retry budget is spent"),
        E(JobState.DISPATCHED, JobState.READY,
          "the lease lapsed; the work is available again"),
        E(JobState.DISPATCHED, JobState.BLOCKED,
          "a dependency was invalidated while this was running"),
        E(JobState.RETRY_WAIT, JobState.READY, "the backoff elapsed"),
        E(JobState.RETRY_WAIT, JobState.FAILED,
          "the retry budget was withdrawn"),
        E(JobState.RETRY_WAIT, JobState.BLOCKED,
          "a dependency stopped satisfying while waiting to retry"),
        # An input changed after the work was accepted.
        E(JobState.SUCCEEDED, JobState.INVALIDATED,
          "an input this job depended on changed"),
        *[E(s, JobState.CANCELLED, "cancelled before it could finish")
          for s in pre_terminal],
    )


EDGES: tuple = _edges()
_BY_PAIR = {(e.src, e.dst): e for e in EDGES}
INITIAL: JobState = JobState.WAITING

#: States with no outgoing edge at all. DERIVED from the table, so the guard
#: and the table cannot disagree.
SEALED: FrozenSet[JobState] = frozenset(
    s for s in JobState if not any(e.src is s for e in EDGES))


def _can_still_succeed() -> FrozenSet[JobState]:
    """States from which SUCCEEDED is still reachable, by backward search.

    Derived from the edge table for the same reason :data:`SEALED` is: a
    hand-written list beside the table is a second place to forget, and the
    two would drift the first time an edge changed.
    """
    reachable = {JobState.SUCCEEDED}
    changed = True
    while changed:
        changed = False
        for e in EDGES:
            if e.dst in reachable and e.src not in reachable:
                reachable.add(e.src)
                changed = True
    return frozenset(reachable)


#: A dependency in any state OUTSIDE this set can never succeed, so anything
#: depending on it can never become ready.
CAN_STILL_SUCCEED: FrozenSet[JobState] = _can_still_succeed()


def find_edge(src: JobState, dst: JobState) -> JobEdge | None:
    return _BY_PAIR.get((src, dst))


def allowed_targets(src: JobState) -> FrozenSet[JobState]:
    return frozenset(e.dst for e in EDGES if e.src == src)


@dataclass(frozen=True)
class Job:
    """One scheduled unit of work. Identity is stable across everything."""

    job_id: str
    #: What the job is FOR, as a content digest. Two enqueues of the same work
    #: under one idempotency key must describe the same thing, and this is
    #: what makes that checkable rather than assumed.
    work_digest: str
    submitter: str
    priority: int = MAX_PRIORITY
    state: JobState = INITIAL
    revision: int = 0
    #: job_ids that must succeed first.
    depends_on: tuple = ()
    #: Evidence digests that must resolve before this may be dispatched.
    requires_evidence: tuple = ()
    #: A capability that must be live at dispatch, or None.
    requires_capability: str | None = None
    #: Declared resource need, e.g. ``{"slots": 1}``.
    resources: dict = field(default_factory=dict)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    attempts: int = 0
    #: Log position before which a retry may not be dispatched.
    backoff_until_seq: int = -1
    idempotency_key: str | None = None
    #: The execution-side task this job dispatched, once there is one.
    task_id: str | None = None
    lease_id: str | None = None
    lease_holder: str | None = None
    lease_expires_after_seq: int = -1
    last_failure: str | None = None
    enqueued_seq: int = -1
    updated_seq: int = -1
    reason: str = ""

    def to_record(self) -> dict:
        return {
            "job_id": self.job_id, "work_digest": self.work_digest,
            "submitter": self.submitter, "priority": self.priority,
            "state": self.state.value, "revision": self.revision,
            "depends_on": list(self.depends_on),
            "requires_evidence": list(self.requires_evidence),
            "requires_capability": self.requires_capability,
            "resources": dict(sorted(self.resources.items())),
            "max_attempts": self.max_attempts, "attempts": self.attempts,
            "backoff_until_seq": self.backoff_until_seq,
            "idempotency_key": self.idempotency_key,
            "task_id": self.task_id, "lease_id": self.lease_id,
            "lease_holder": self.lease_holder,
            "lease_expires_after_seq": self.lease_expires_after_seq,
            "last_failure": self.last_failure,
            "enqueued_seq": self.enqueued_seq,
            "updated_seq": self.updated_seq, "reason": self.reason,
        }

    def lease_is_live(self, at_seq: int) -> bool:
        return (self.lease_id is not None
                and at_seq <= self.lease_expires_after_seq)

    def effective_priority(self, at_seq: int) -> int:
        """Priority improved by waiting. Deterministic in the log position.

        A pure function of ``(priority, enqueued_seq, at_seq)``, so two
        readers of the same log compute the same order -- which is the
        property that makes the queue reproducible after a restart.
        """
        if self.enqueued_seq < 0:
            return self.priority
        waited = max(0, at_seq - self.enqueued_seq)
        return max(MIN_PRIORITY, self.priority - waited // AGING_INTERVAL)


@dataclass(frozen=True)
class Readiness:
    """Why a job may or may not be dispatched right now."""

    ready: bool
    #: Set when the job can never become ready and must be BLOCKED.
    fatal: bool = False
    reason: str = ""
    blocked_by: tuple = ()

    def to_record(self) -> dict:
        return {"ready": self.ready, "fatal": self.fatal,
                "reason": self.reason, "blocked_by": list(self.blocked_by)}


def backoff_for(attempt: int) -> int:
    """Sequence numbers to wait before attempt ``attempt`` may be retried."""
    if attempt < 1:
        return 0
    return min(BACKOFF_MAX_SEQS, BACKOFF_BASE_SEQS * (2 ** (attempt - 1)))


def default_policy(policy_id: str = "scheduler.default") -> "object":
    """A minimal, explicit scheduling policy.

    Offered as a starting point, not as a default that applies when nobody
    published one: :class:`Scheduler` requires a policy store and refuses to
    dispatch under a policy that was never published. A scheduler with an
    implicit policy has no policy.
    """
    return document(
        policy_id=policy_id, version=1,
        description=("Baseline scheduling policy: anyone may enqueue and "
                     "dispatch; only the SCHEDULER role may raise priority."),
        rules=(
            rule(rule_id="deny-priority-escalation-by-workers",
                 effect=Effect.DENY, actions=("scheduler.raise_priority",),
                 subjects=("*",), roles=("WORKER", "SUBMITTER"),
                 resources=("*",),
                 reason=("only the scheduler may make work more urgent; "
                         "otherwise every submitter is priority 0")),
            rule(rule_id="allow-scheduling", effect=Effect.ALLOW,
                 actions=("scheduler.enqueue", "scheduler.dispatch",
                          "scheduler.raise_priority", "scheduler.cancel"),
                 subjects=("*",), roles=("*",), resources=("*",),
                 reason="baseline: scheduling operations are permitted"),
        ))


class Scheduler:
    """The durable queue. Every decision is a projection of the log."""

    def __init__(self, log, *, policy, policy_id: str,
                 capacity: dict | None = None):
        self.log = log
        #: A :class:`~qta_agent.policy.PolicyStore`. Mandatory: a scheduler
        #: whose policy is optional is a scheduler with no policy in the one
        #: deployment where it matters.
        self.policy = policy
        self.policy_id = policy_id
        #: Resource ceilings this executor can actually satisfy.
        self.capacity = dict(capacity or {"slots": 1})
        self._jobs: dict = {}
        self._keys: dict = {}
        self._loaded_through = -1

    # ---- projection ----------------------------------------------------
    def load(self) -> "Scheduler":
        """Rebuild the queue from the verified log. Fail closed."""
        self.log.verify().raise_if_bad()
        self._jobs = {}
        self._keys = {}
        self._loaded_through = -1
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        """Fold one event in. True when it was a scheduler event."""
        p = ev.payload
        if ev.action == ACT_ENQUEUE:
            job = job_from_record(p["job"])
            if job.job_id in self._jobs:
                raise DuplicateJob(
                    f"seq {ev.seq}: job {job.job_id!r} enqueued twice; a "
                    "second enqueue would silently replace the first one's "
                    "attempts and lease")
            job = replace(job, enqueued_seq=ev.seq, updated_seq=ev.seq,
                          revision=1)
            self._jobs[job.job_id] = job
            if job.idempotency_key:
                self._keys[job.idempotency_key] = (job.job_id,
                                                   job.work_digest)
        elif ev.action == ACT_JOB_TRANSITION:
            cur = self._jobs[p["job_id"]]
            src = JobState(p["src"])
            dst = JobState(p["dst"])
            if cur.state is not src:
                raise JobTransitionError(
                    f"seq {ev.seq}: {cur.job_id!r} is {cur.state.value}, but "
                    f"the record moves it from {src.value}")
            # Re-authorize on replay: a transition the machine forbids today
            # is not applied, so a forged record cannot become state merely by
            # appearing in the file.
            edge = check_edge(src, dst, cur.job_id)
            self._jobs[cur.job_id] = _apply_edge(cur, edge, p, seq=ev.seq)
        elif ev.action == ACT_PRIORITY:
            cur = self._jobs[p["job_id"]]
            new = int(p["priority"])
            _require_priority(new)
            self._jobs[cur.job_id] = replace(
                cur, priority=new, revision=cur.revision + 1,
                updated_seq=ev.seq,
                reason=p.get("reason", "priority changed"))
        else:
            return False
        self._loaded_through = ev.seq
        return True

    # ---- reads ---------------------------------------------------------
    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise UnknownJob(f"no job {job_id!r}") from None

    def all_jobs(self) -> dict:
        return dict(self._jobs)

    def in_state(self, state: JobState) -> tuple:
        return tuple(sorted((j for j in self._jobs.values()
                             if j.state is state),
                            key=lambda j: j.job_id))

    def dependents(self, job_id: str) -> tuple:
        """Jobs that directly depend on ``job_id``."""
        return tuple(sorted((j for j in self._jobs.values()
                             if job_id in j.depends_on),
                            key=lambda j: j.job_id))

    def transitive_dependents(self, job_id: str) -> tuple:
        """Every job reachable downstream. Cycle-safe by construction."""
        seen: set = set()
        frontier = [job_id]
        while frontier:
            cur = frontier.pop()
            for j in self.dependents(cur):
                if j.job_id not in seen:
                    seen.add(j.job_id)
                    frontier.append(j.job_id)
        return tuple(sorted(seen))

    def at_seq(self) -> int:
        """The log position decisions are being made at."""
        return self.log.verify().head_seq

    # ---- readiness -----------------------------------------------------
    def readiness(self, job: Job, *, at_seq: int, resolve=None,
                  capabilities=None) -> Readiness:
        """Why this job may or may not be dispatched. Total; never raises.

        ``resolve`` is a predicate over an evidence digest and
        ``capabilities`` a :class:`~qta_agent.capability.CapabilitySet`. Both
        are optional ARGUMENTS rather than attributes so the scheduler cannot
        hold authority it was not handed; a caller that omits them gets a
        readiness answer that says so rather than one that silently skipped
        the check.
        """
        if job.state in TERMINAL:
            return Readiness(False, reason=f"job is {job.state.value}")

        fatal: list = []
        waiting: list = []
        for dep_id in job.depends_on:
            dep = self._jobs.get(dep_id)
            if dep is None:
                fatal.append(dep_id)
                continue
            if dep.state is JobState.SUCCEEDED:
                continue
            if dep.state in TERMINAL:
                # FAILED, BLOCKED, CANCELLED or INVALIDATED: this job will
                # never become ready, and leaving it in WAITING would make it
                # look like it were about to start.
                fatal.append(dep_id)
            else:
                waiting.append(dep_id)
        if fatal:
            return Readiness(
                False, fatal=True, blocked_by=tuple(sorted(fatal)),
                reason=("dependencies that will never satisfy: "
                        + ", ".join(sorted(fatal))))
        if waiting:
            return Readiness(
                False, blocked_by=tuple(sorted(waiting)),
                reason="waiting on " + ", ".join(sorted(waiting)))

        if job.state is JobState.RETRY_WAIT and at_seq < job.backoff_until_seq:
            return Readiness(
                False, reason=(f"backing off until seq "
                               f"{job.backoff_until_seq}; log is at {at_seq}"))

        if job.requires_evidence:
            if resolve is None:
                return Readiness(
                    False, reason=("this job cites evidence, and no resolver "
                                   "was supplied; refusing to treat an "
                                   "unchecked citation as satisfied"))
            missing = [d for d in job.requires_evidence if not resolve(d)]
            if missing:
                return Readiness(
                    False, fatal=False,
                    reason=("cited evidence does not resolve: "
                            + ", ".join(sorted(d[:12] for d in missing))))

        if job.requires_capability is not None:
            if capabilities is None:
                return Readiness(
                    False, reason=("this job requires capability "
                                   f"{job.requires_capability!r} and no "
                                   "capability set was supplied"))
            cap = capabilities.issued.get(job.requires_capability)
            if cap is None:
                return Readiness(
                    False, fatal=True,
                    reason=(f"capability {job.requires_capability!r} was "
                            "never issued"))
            if job.requires_capability in capabilities.revoked:
                return Readiness(
                    False, fatal=True,
                    reason=(f"capability {job.requires_capability!r} was "
                            "revoked"))
            from .capability import NEVER_EXPIRES
            if (cap.expires_after_seq != NEVER_EXPIRES
                    and at_seq > cap.expires_after_seq):
                return Readiness(
                    False, fatal=True,
                    reason=(f"capability {job.requires_capability!r} expired "
                            f"after seq {cap.expires_after_seq}"))

        over = _exceeds(job.resources, self.capacity)
        if over:
            return Readiness(
                False, fatal=True,
                reason=(f"declared resource need {over} exceeds this "
                        "executor's capacity "
                        f"{dict(sorted(self.capacity.items()))}"))

        decision = self.policy.evaluate(
            self.policy_id,
            PolicyRequest(action="scheduler.dispatch", subject=job.submitter,
                          role="SCHEDULER", resource=job.job_id,
                          task_id=job.task_id or ""))
        if not decision.allowed:
            return Readiness(False, fatal=True,
                             reason=f"policy {decision.identity}: "
                                    f"{decision.reason}")
        return Readiness(True, reason="every precondition holds")

    def ready_queue(self, *, at_seq: int | None = None, resolve=None,
                    capabilities=None) -> tuple:
        """Dispatchable jobs, in the order they should be dispatched.

        Ordered by ``(effective_priority, enqueued_seq, job_id)``. The last
        term is not decoration: without a total order, two jobs enqueued in
        the same append batch would be dispatched in dictionary order, which
        is stable within a process and not across a restart -- the exact
        difference that makes a recovered queue diverge from the one it
        replaced.
        """
        seq = self.at_seq() if at_seq is None else at_seq
        out = []
        for job in self._jobs.values():
            if job.state not in (JobState.READY, JobState.WAITING,
                                 JobState.RETRY_WAIT):
                continue
            if not self.readiness(job, at_seq=seq, resolve=resolve,
                                  capabilities=capabilities).ready:
                continue
            out.append(job)
        return tuple(sorted(
            out, key=lambda j: (j.effective_priority(seq), j.enqueued_seq,
                                j.job_id)))

    def in_flight_resources(self) -> dict:
        """Resources currently held by dispatched jobs."""
        total: dict = {}
        for job in self._jobs.values():
            if job.state is JobState.DISPATCHED:
                for k, v in job.resources.items():
                    total[k] = total.get(k, 0) + v
        return total

    def expired_leases(self, *, at_seq: int | None = None) -> tuple:
        """Dispatched jobs whose lease has lapsed. The requeue input."""
        seq = self.at_seq() if at_seq is None else at_seq
        return tuple(sorted(
            (j for j in self._jobs.values()
             if j.state is JobState.DISPATCHED and not j.lease_is_live(seq)),
            key=lambda j: j.job_id))

    # ---- writes --------------------------------------------------------
    def enqueue(self, *, job_id: str, work_digest: str, submitter: str,
                priority: int = MAX_PRIORITY, depends_on: tuple = (),
                requires_evidence: tuple = (),
                requires_capability: str | None = None,
                resources: dict | None = None,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                idempotency_key: str | None = None,
                task_id: str | None = None) -> Job:
        """Add a job. Idempotent under a key; refuses impossible work."""
        if idempotency_key:
            prior = self._keys.get(idempotency_key)
            if prior is not None:
                existing_id, existing_digest = prior
                if existing_digest != work_digest:
                    raise DuplicateJob(
                        f"idempotency key {idempotency_key!r} already "
                        f"enqueued {existing_id!r} for work "
                        f"{existing_digest[:12]}; reusing it for different "
                        f"work ({work_digest[:12]}) would suppress a real "
                        "request as if it were a retry")
                return self.get(existing_id)
        if job_id in self._jobs:
            raise DuplicateJob(f"job {job_id!r} already exists")
        if not is_digest(work_digest):
            raise SchedulerError(
                "work_digest must be a sha256 digest; without it, two "
                "enqueues under one key cannot be compared")
        _require_priority(priority)
        if (not isinstance(max_attempts, int)
                or isinstance(max_attempts, bool) or max_attempts < 1):
            raise SchedulerError("max_attempts must be an int >= 1")
        depends_on = tuple(dict.fromkeys(depends_on))
        if len(depends_on) > MAX_DEPENDENCIES:
            raise SchedulerError(
                f"{len(depends_on)} dependencies exceeds the "
                f"{MAX_DEPENDENCIES} bound")
        for dep in depends_on:
            if dep == job_id:
                raise SchedulerError(f"{job_id!r} cannot depend on itself")
            if dep not in self._jobs:
                raise SchedulerError(
                    f"dependency {dep!r} does not exist; a job may not depend "
                    "on something unrecorded, because nothing would ever "
                    "satisfy it")
            dep_state = self._jobs[dep].state
            if dep_state not in CAN_STILL_SUCCEED:
                # The same refusal as the two above, for the same reason. A
                # dependency that is already CANCELLED, FAILED, BLOCKED or
                # INVALIDATED is SEALED: no edge leads from it to SUCCEEDED,
                # so this job could never become ready. Accepting it would
                # leave a job WAITING on work nobody will ever do, and
                # waiting forever looks exactly like slow. reconcile() would
                # eventually move it to BLOCKED, which is a worse answer than
                # this one: it arrives later, to nobody in particular, and
                # the submitter has already gone.
                raise SchedulerError(
                    f"dependency {dep!r} is {dep_state.value}, from which "
                    f"SUCCEEDED is unreachable; {job_id!r} could never become "
                    "ready. Refusing to enqueue work that would wait forever")
        for dg in requires_evidence:
            if not is_digest(dg):
                raise SchedulerError(
                    f"requires_evidence entry {dg!r} is not a sha256 digest")
        resources = dict(resources or {})
        for k, v in resources.items():
            if not isinstance(k, str) or not k:
                raise SchedulerError(f"resource name {k!r} must be a str")
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise SchedulerError(
                    f"resource {k!r} must be a non-negative int, got {v!r}")
        over = _exceeds(resources, self.capacity)
        if over:
            raise CapacityError(
                f"job {job_id!r} declares {over}, which this executor's "
                f"capacity {dict(sorted(self.capacity.items()))} can never "
                "satisfy; refusing to enqueue work that would wait forever")

        decision = self.policy.evaluate(
            self.policy_id,
            PolicyRequest(action="scheduler.enqueue", subject=submitter,
                          role="SUBMITTER", resource=job_id,
                          task_id=task_id or ""))
        decision.raise_if_denied()

        job = Job(job_id=job_id, work_digest=work_digest, submitter=submitter,
                  priority=priority, depends_on=depends_on,
                  requires_evidence=tuple(requires_evidence),
                  requires_capability=requires_capability,
                  resources=resources, max_attempts=max_attempts,
                  idempotency_key=idempotency_key, task_id=task_id)
        ev = self.log.append(actor=submitter, action=ACT_ENQUEUE,
                             target=job_id, payload={"job": job.to_record()})
        self.apply(ev)
        return self.get(job_id)

    def set_priority(self, *, job_id: str, priority: int, actor: str,
                     role: str, reason: str) -> Job:
        """Change urgency. Raising it is a policy decision, not a setter."""
        job = self.get(job_id)
        _require_priority(priority)
        if not reason:
            raise SchedulerError(
                "a priority change requires a reason; 'why is this urgent' is "
                "the question an operator asks first")
        if priority < job.priority:
            decision = self.policy.evaluate(
                self.policy_id,
                PolicyRequest(action="scheduler.raise_priority", subject=actor,
                              role=role, resource=job_id,
                              task_id=job.task_id or ""))
            decision.raise_if_denied()
        ev = self.log.append(
            actor=actor, action=ACT_PRIORITY, target=job_id,
            payload={"job_id": job_id, "priority": priority, "role": role,
                     "reason": reason})
        self.apply(ev)
        return self.get(job_id)

    def transition(self, *, job_id: str, dst: JobState, actor: str,
                   reason: str = "", expected_revision: int | None = None,
                   **fields) -> Job:
        """Authorize and commit one queue transition."""
        job = self.get(job_id)
        if (expected_revision is not None
                and job.revision != expected_revision):
            raise SchedulerError(
                f"{job_id}: expected revision {expected_revision}, found "
                f"{job.revision}; the job changed since it was read")
        edge = check_edge(job.state, dst, job_id)
        payload = {"job_id": job_id, "src": job.state.value, "dst": dst.value,
                   "reason": reason or edge.reason}
        payload.update({k: v for k, v in fields.items() if v is not None})
        ev = self.log.append(actor=actor, action=ACT_JOB_TRANSITION,
                             target=job_id, payload=payload)
        self.apply(ev)
        return self.get(job_id)

    def mark_ready(self, job_id: str, *, actor: str = "scheduler") -> Job:
        return self.transition(job_id=job_id, dst=JobState.READY, actor=actor)

    def dispatch(self, *, job_id: str, worker: str, lease_id: str,
                 lease_seqs: int, actor: str = "scheduler",
                 task_id: str | None = None, resolve=None,
                 capabilities=None) -> Job:
        """Hand a job to a worker under a lease. Re-checks readiness first.

        The re-check is not redundant with :meth:`ready_queue`: a caller may
        have taken the queue, done something slow, and come back. A dispatch
        that skipped it would be dispatching against a snapshot.
        """
        job = self.get(job_id)
        at = self.at_seq()
        if job.state is not JobState.READY:
            raise JobTransitionError(
                f"{job_id!r} is {job.state.value}; only a READY job may be "
                "dispatched")
        r = self.readiness(job, at_seq=at, resolve=resolve,
                           capabilities=capabilities)
        if not r.ready:
            raise JobTransitionError(
                f"refusing to dispatch {job_id!r}: {r.reason}")
        held = self.in_flight_resources()
        combined = {k: held.get(k, 0) + v for k, v in job.resources.items()}
        over = _exceeds(combined, self.capacity)
        if over:
            raise CapacityError(
                f"dispatching {job_id!r} would put in-flight usage at {over}, "
                f"above capacity {dict(sorted(self.capacity.items()))}")
        if not isinstance(lease_seqs, int) or isinstance(lease_seqs, bool) \
                or lease_seqs < 1:
            raise SchedulerError("lease_seqs must be an int >= 1")
        return self.transition(
            job_id=job_id, dst=JobState.DISPATCHED, actor=actor,
            reason=f"leased to {worker}", lease_id=lease_id,
            lease_holder=worker, task_id=task_id,
            lease_expires_after_seq=at + 1 + lease_seqs,
            attempts=job.attempts + 1)

    def report(self, *, job_id: str, worker: str,
               failure: FailureClass | None = None, detail: str = "",
               actor: str | None = None) -> Job:
        """Record the outcome of a dispatched attempt.

        Refuses a report from anyone but the lease holder, and refuses one
        from a holder whose lease has lapsed. A worker back from the dead
        reporting success is reporting on work someone else may already have
        redone -- and, worse, may have redone differently.
        """
        job = self.get(job_id)
        at = self.at_seq()
        if job.state is not JobState.DISPATCHED:
            raise JobTransitionError(
                f"{job_id!r} is {job.state.value}; only a DISPATCHED job "
                "takes an outcome report")
        if job.lease_holder != worker:
            raise JobTransitionError(
                f"{job_id!r} is leased to {job.lease_holder!r}, not "
                f"{worker!r}")
        if not job.lease_is_live(at):
            raise JobTransitionError(
                f"lease {job.lease_id!r} lapsed after seq "
                f"{job.lease_expires_after_seq}; the log is at {at}. A late "
                "report does not get to decide the outcome.")
        who = actor or worker
        if failure is None:
            return self.transition(job_id=job_id, dst=JobState.SUCCEEDED,
                                   actor=who, reason=detail or "verified",
                                   lease_id="", lease_holder="",
                                   lease_expires_after_seq=-1)
        if not isinstance(failure, FailureClass):
            raise SchedulerError(
                f"failure must be a FailureClass, got {failure!r}")
        note = f"{failure.value}: {detail}" if detail else failure.value
        if failure in RETRYABLE and job.attempts < job.max_attempts:
            return self.transition(
                job_id=job_id, dst=JobState.RETRY_WAIT, actor=who,
                reason=note, last_failure=note, lease_id="", lease_holder="",
                lease_expires_after_seq=-1,
                backoff_until_seq=at + backoff_for(job.attempts))
        failed = self.transition(
            job_id=job_id, dst=JobState.FAILED, actor=who, reason=note,
            last_failure=note, lease_id="", lease_holder="",
            lease_expires_after_seq=-1)
        self._block_dependents_of(job_id, actor=who, why=note)
        return failed

    def _block_dependents_of(self, job_id: str, *, actor: str,
                             why: str) -> tuple:
        """Everything downstream of work that can no longer succeed.

        FOUND BY THE STATEFUL PROPERTY TEST, as an asymmetry rather than a
        crash. :meth:`cancel` cascades and says why -- "the alternative is a
        dependent that waits on work nobody will ever do" -- and
        :meth:`invalidate` cascades for the same reason. A terminal FAILURE
        has exactly that consequence for a dependent and did not cascade: the
        dependent stayed WAITING until somebody happened to run
        :meth:`reconcile`.

        Leaving it to reconcile is a real difference, not a timing detail. A
        job WAITING on a dead parent is indistinguishable from a job waiting
        on a slow one, so nothing alerts and nobody looks; and if the process
        dies before the next tick, the queue on disk describes work that is
        still pending when it is not.

        BLOCKED rather than CANCELLED, following :meth:`invalidate`: the
        premise is gone, but nobody decided to stop this job, and an operator
        responds to the two differently.
        """
        moves = []
        for dep_id in self.transitive_dependents(job_id):
            dep = self.get(dep_id)
            if dep.state in TERMINAL:
                continue
            moves.append(self.transition(
                job_id=dep_id, dst=JobState.BLOCKED, actor=actor,
                reason=f"upstream {job_id} can no longer succeed: {why}",
                blocked_by=[job_id]))
        return tuple(moves)

    def cancel(self, *, job_id: str, actor: str, reason: str,
               cascade: bool = True) -> tuple:
        """Cancel a job and, by default, everything downstream of it.

        Cascading is the default because the alternative is a dependent that
        waits on work nobody will ever do. Cancelled dependents are recorded
        as CANCELLED rather than BLOCKED: the cause was a decision, not a
        failure, and an operator responds to the two differently.
        """
        job = self.get(job_id)
        if job.state in TERMINAL:
            raise JobTransitionError(
                f"{job_id!r} is already {job.state.value}; cancelling a "
                "finished job would rewrite how it finished")
        decision = self.policy.evaluate(
            self.policy_id,
            PolicyRequest(action="scheduler.cancel", subject=actor,
                          role="SUBMITTER", resource=job_id,
                          task_id=job.task_id or ""))
        decision.raise_if_denied()
        cancelled = [self.transition(job_id=job_id, dst=JobState.CANCELLED,
                                     actor=actor, reason=reason)]
        if cascade:
            for dep_id in self.transitive_dependents(job_id):
                dep = self.get(dep_id)
                if dep.state in TERMINAL:
                    continue
                cancelled.append(self.transition(
                    job_id=dep_id, dst=JobState.CANCELLED, actor=actor,
                    reason=f"upstream {job_id} was cancelled: {reason}"))
        return tuple(cancelled)

    def reconcile(self, *, actor: str = "scheduler", resolve=None,
                  capabilities=None) -> tuple:
        """Bring the queue into agreement with the facts. Idempotent.

        This is the operation a restarted process runs, and the one a
        supervisor runs on a tick. It requeues lapsed leases, blocks jobs
        whose dependencies will never satisfy, and promotes jobs whose
        preconditions now hold. Every move it makes is an event, so an
        operator can see what the scheduler decided while nobody was looking.
        """
        moves: list = []
        for job in self.expired_leases():
            moves.append(self.transition(
                job_id=job.job_id, dst=JobState.READY, actor=actor,
                reason=(f"lease {job.lease_id!r} lapsed after seq "
                        f"{job.lease_expires_after_seq}"),
                lease_id="", lease_holder="", lease_expires_after_seq=-1))
        # Snapshot the ids first: transitions mutate the projection, and
        # iterating it while it changes would silently skip jobs.
        for job_id in sorted(self._jobs):
            job = self.get(job_id)
            if job.state not in PENDING:
                continue
            at = self.at_seq()
            r = self.readiness(job, at_seq=at, resolve=resolve,
                               capabilities=capabilities)
            if r.fatal and job.state is not JobState.BLOCKED:
                moves.append(self.transition(
                    job_id=job_id, dst=JobState.BLOCKED, actor=actor,
                    reason=r.reason, blocked_by=list(r.blocked_by)))
            elif r.ready and job.state is not JobState.READY:
                moves.append(self.transition(
                    job_id=job_id, dst=JobState.READY, actor=actor,
                    reason=r.reason))
            elif (not r.ready and not r.fatal
                  and job.state is JobState.READY):
                moves.append(self.transition(
                    job_id=job_id, dst=JobState.WAITING, actor=actor,
                    reason=r.reason))
        return tuple(moves)

    def invalidate(self, *, job_id: str, actor: str, reason: str) -> tuple:
        """A succeeded job's input changed. Mark it and everything downstream.

        Downstream jobs that already succeeded are INVALIDATED; ones still
        pending become BLOCKED, because their premise is gone. Neither is
        rewritten: the history keeps saying they succeeded, and a new lineage
        is what re-doing the work produces.
        """
        job = self.get(job_id)
        if job.state is not JobState.SUCCEEDED:
            raise JobTransitionError(
                f"{job_id!r} is {job.state.value}; only a SUCCEEDED job can "
                "be invalidated -- nothing else was ever treated as current")
        moved = [self.transition(job_id=job_id, dst=JobState.INVALIDATED,
                                 actor=actor, reason=reason)]
        for dep_id in self.transitive_dependents(job_id):
            dep = self.get(dep_id)
            if dep.state is JobState.SUCCEEDED:
                moved.append(self.transition(
                    job_id=dep_id, dst=JobState.INVALIDATED, actor=actor,
                    reason=f"upstream {job_id} was invalidated: {reason}"))
            elif dep.state in PENDING:
                moved.append(self.transition(
                    job_id=dep_id, dst=JobState.BLOCKED, actor=actor,
                    reason=f"upstream {job_id} was invalidated: {reason}",
                    blocked_by=[job_id]))
            elif dep.state is JobState.DISPATCHED:
                moved.append(self.transition(
                    job_id=dep_id, dst=JobState.BLOCKED, actor=actor,
                    reason=(f"upstream {job_id} was invalidated while this "
                            f"was running: {reason}"),
                    blocked_by=[job_id]))
        return tuple(moved)

    # ---- snapshot ------------------------------------------------------
    def snapshot(self) -> dict:
        return {"snapshot_version": 1,
                "loaded_through": self._loaded_through,
                "jobs": {jid: j.to_record()
                         for jid, j in sorted(self._jobs.items())},
                "idempotency": {k: list(v)
                                for k, v in sorted(self._keys.items())}}

    def snapshot_digest(self) -> str:
        return digest(self.snapshot())


# ---- module-level helpers ----------------------------------------------
def check_edge(src: JobState, dst: JobState, job_id: str) -> JobEdge:
    """Authorize a queue transition, or raise. This is the whole gate."""
    edge = find_edge(src, dst)
    if edge is None:
        if src in TERMINAL:
            raise JobTransitionError(
                f"{src.value} is terminal; job {job_id!r} cannot leave it. "
                "Re-doing the work is a NEW job, which leaves a trail.")
        raise JobTransitionError(
            f"no edge {src.value} -> {dst.value} for {job_id!r}; permitted "
            f"targets are {sorted(s.value for s in allowed_targets(src))}")
    return edge


def _apply_edge(job: Job, edge: JobEdge, payload: dict, *, seq: int) -> Job:
    """Fold an authorized transition into a job. Pure; no I/O."""
    updates = {"state": edge.dst, "revision": job.revision + 1,
               "updated_seq": seq,
               "reason": payload.get("reason") or edge.reason}
    for name in ("lease_id", "lease_holder", "task_id", "last_failure"):
        if name in payload:
            val = payload[name]
            updates[name] = None if val == "" else val
    for name in ("lease_expires_after_seq", "backoff_until_seq", "attempts"):
        if name in payload:
            updates[name] = int(payload[name])
    if edge.dst in TERMINAL or edge.dst in PENDING:
        # Leaving DISPATCHED always drops the lease. Doing it here rather than
        # trusting each caller to pass the clearing fields is what stops a
        # requeued job from carrying an owner it no longer has.
        updates.setdefault("lease_id", None)
        updates.setdefault("lease_holder", None)
        updates.setdefault("lease_expires_after_seq", -1)
    return replace(job, **updates)


def _require_priority(priority: int) -> None:
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise SchedulerError(
            f"priority must be an int, got {type(priority).__name__}")
    if not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise SchedulerError(
            f"priority {priority} is outside [{MIN_PRIORITY}, "
            f"{MAX_PRIORITY}]; an unbounded priority is a denial of service "
            "on every other job")


def _exceeds(need: dict, capacity: dict) -> dict:
    """The entries of ``need`` that ``capacity`` cannot satisfy."""
    over = {}
    for k, v in sorted(need.items()):
        if v > capacity.get(k, 0):
            over[k] = v
    return over


def job_from_record(rec: dict) -> Job:
    """Rebuild a job from a log payload, validating its shape."""
    if not isinstance(rec, dict):
        raise SchedulerError(f"job record is {type(rec).__name__}")
    known = set(Job.__dataclass_fields__)
    unknown = set(rec) - known
    if unknown:
        raise SchedulerError(
            f"job record carries unknown fields {sorted(unknown)}; refusing "
            "to project a job this version does not fully understand")
    try:
        return Job(
            job_id=rec["job_id"], work_digest=rec["work_digest"],
            submitter=rec["submitter"], priority=rec["priority"],
            state=JobState(rec.get("state", INITIAL.value)),
            revision=rec.get("revision", 0),
            depends_on=tuple(rec.get("depends_on", ())),
            requires_evidence=tuple(rec.get("requires_evidence", ())),
            requires_capability=rec.get("requires_capability"),
            resources=dict(rec.get("resources", {})),
            max_attempts=rec.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            attempts=rec.get("attempts", 0),
            backoff_until_seq=rec.get("backoff_until_seq", -1),
            idempotency_key=rec.get("idempotency_key"),
            task_id=rec.get("task_id"), lease_id=rec.get("lease_id"),
            lease_holder=rec.get("lease_holder"),
            lease_expires_after_seq=rec.get("lease_expires_after_seq", -1),
            last_failure=rec.get("last_failure"),
            enqueued_seq=rec.get("enqueued_seq", -1),
            updated_seq=rec.get("updated_seq", -1),
            reason=rec.get("reason", ""))
    except (KeyError, TypeError, ValueError) as exc:
        raise SchedulerError(f"job record is malformed: {exc}") from exc
