"""Durable task lifecycle: work that survives the process that started it.

WHY A TASK IS NOT A FUNCTION CALL

A function call's state lives in a stack frame. If the process dies the call is
simply gone -- and so is any record that it was ever attempted, what it was
allowed to do, how far it got, and whether its effects were left half-applied.
For a long-running agent that is the difference between a system that can be
audited after an incident and one that can only be guessed about.

A task here is a record in the event log. Its state is a projection of that
log, so "what was this task doing when the machine died" is answerable by
replay rather than by inference.

THE STATE MACHINE IS EXPLICIT FOR THE SAME REASON THE AUTHORITY ONE IS

Reachable states are enumerable and forbidden ones are unreachable rather than
merely unwritten. In particular:

  * A task cannot reach VERIFIED except from an execution that COMPLETED.
    TIMED_OUT and CANCELLED are terminal-ish outcomes with their own edges, so
    "it probably finished" is not a path through this table.
  * VERIFIED requires an actor distinct from the one that executed. An agent
    that executes and verifies its own work has not verified anything.
  * A cancelled task cannot become completed, even if its process happened to
    exit 0 after the cancellation was recorded. The record of the request is
    what decides, not the race.

LEASES

Work that can be picked up by more than one worker needs an owner, and an owner
that can die needs an expiry. A lease names its holder and the seq after which
someone else may take over. Completion is refused from a holder whose lease has
lapsed -- a worker that comes back from the dead and reports success is
reporting on a task somebody else may already have redone.

Expiry is in sequence numbers rather than wall time, for the same reason
capabilities are: the log's order is the only clock every reader agrees on.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import FrozenSet

from .canonical import is_digest


class TaskError(Exception):
    """Base class. Every failure here is fail-closed."""


class TaskTransitionError(TaskError):
    """The state machine forbids this move."""


class LeaseError(TaskError):
    """The lease does not authorize this."""


class UnknownTask(TaskError):
    """No such task in the projection."""


class TaskState(str, Enum):
    """Where a unit of work is. ``str`` mixin so it serializes canonically."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    EXECUTING = "EXECUTING"
    #: The tool's process exited 0. NOT a statement that the result is good.
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    #: Independently checked by someone other than the executor.
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    #: An input changed, so a previous verification no longer describes it.
    INVALIDATED = "INVALIDATED"


class TaskRole(str, Enum):
    """Who may move a task. Distinct from an actor's identity."""

    SUBMITTER = "SUBMITTER"
    SCHEDULER = "SCHEDULER"
    WORKER = "WORKER"
    VERIFIER = "VERIFIER"
    SYSTEM = "SYSTEM"


#: The work is finished. No further PROGRESS is possible from these, and
#: recovery means a NEW task, which leaves a trail.
#:
#: Terminal is not the same as sealed. A finished task can still have a fact
#: recorded ABOUT it -- VERIFIED -> INVALIDATED says an input changed, which
#: is a consequence, not a resumption. Conflating the two made that edge
#: unreachable: it was declared in the table, refused by the guard, and
#: nothing noticed, because a state machine's dead edges are invisible unless
#: something asserts they are not there.
TERMINAL: FrozenSet[TaskState] = frozenset({
    TaskState.VERIFIED, TaskState.REJECTED, TaskState.CANCELLED,
    TaskState.INVALIDATED,
})

#: Outcomes that record work having been attempted and not succeeded. Kept
#: separate from TERMINAL because a failed task may be resubmitted, while a
#: rejected one has been judged.
UNSUCCESSFUL: FrozenSet[TaskState] = frozenset({
    TaskState.FAILED, TaskState.TIMED_OUT, TaskState.CANCELLED,
})


@dataclass(frozen=True)
class TaskEdge:
    src: TaskState
    dst: TaskState
    roles: FrozenSet[TaskRole]
    reason: str
    #: True when the actor must differ from the one that executed the task.
    requires_distinct_actor: bool = False
    #: True when the mover must hold the task's current, unexpired lease.
    requires_lease: bool = False


def _edges() -> tuple:
    return (
        TaskEdge(TaskState.CREATED, TaskState.VALIDATED,
                 frozenset({TaskRole.SUBMITTER, TaskRole.SCHEDULER}),
                 "inputs conform to the tool's contract"),
        TaskEdge(TaskState.CREATED, TaskState.REJECTED,
                 frozenset({TaskRole.SUBMITTER, TaskRole.SCHEDULER}),
                 "inputs do not conform, and never will"),
        TaskEdge(TaskState.VALIDATED, TaskState.QUEUED,
                 frozenset({TaskRole.SCHEDULER}),
                 "dependencies are satisfied"),
        TaskEdge(TaskState.QUEUED, TaskState.LEASED,
                 frozenset({TaskRole.SCHEDULER, TaskRole.WORKER}),
                 "a worker took ownership"),
        TaskEdge(TaskState.LEASED, TaskState.EXECUTING,
                 frozenset({TaskRole.WORKER}), "the tool was started",
                 requires_lease=True),
        TaskEdge(TaskState.EXECUTING, TaskState.COMPLETED,
                 frozenset({TaskRole.WORKER}),
                 "the process exited 0 -- not a claim about the result",
                 requires_lease=True),
        TaskEdge(TaskState.EXECUTING, TaskState.FAILED,
                 frozenset({TaskRole.WORKER}), "the process did not exit 0",
                 requires_lease=True),
        TaskEdge(TaskState.EXECUTING, TaskState.TIMED_OUT,
                 frozenset({TaskRole.WORKER, TaskRole.SYSTEM}),
                 "the wall-clock bound was reached"),
        # I: the ONLY edge into VERIFIED, and it needs a different actor.
        TaskEdge(TaskState.COMPLETED, TaskState.VERIFIED,
                 frozenset({TaskRole.VERIFIER}),
                 "independently checked by someone other than the executor",
                 requires_distinct_actor=True),
        TaskEdge(TaskState.COMPLETED, TaskState.REJECTED,
                 frozenset({TaskRole.VERIFIER}),
                 "the result did not survive verification",
                 requires_distinct_actor=True),
        # Cancellation is reachable from every pre-terminal state.
        *[TaskEdge(s, TaskState.CANCELLED,
                   frozenset({TaskRole.SUBMITTER, TaskRole.SCHEDULER,
                              TaskRole.SYSTEM}),
                   "cancelled before it could finish")
          for s in (TaskState.CREATED, TaskState.VALIDATED, TaskState.QUEUED,
                    TaskState.LEASED, TaskState.EXECUTING)],
        # A lapsed lease returns the work to the queue rather than
        # stranding it.
        TaskEdge(TaskState.LEASED, TaskState.QUEUED,
                 frozenset({TaskRole.SCHEDULER, TaskRole.SYSTEM}),
                 "the lease lapsed; the work is available again"),
        TaskEdge(TaskState.EXECUTING, TaskState.QUEUED,
                 frozenset({TaskRole.SCHEDULER, TaskRole.SYSTEM}),
                 "the worker died mid-execution; the work is available again"),
        # Retry paths for the outcomes that are retryable.
        TaskEdge(TaskState.FAILED, TaskState.QUEUED,
                 frozenset({TaskRole.SCHEDULER}),
                 "resubmitted after a failure"),
        TaskEdge(TaskState.TIMED_OUT, TaskState.QUEUED,
                 frozenset({TaskRole.SCHEDULER}),
                 "resubmitted after a timeout"),
        # An input changed, so a prior verification no longer describes it.
        TaskEdge(TaskState.VERIFIED, TaskState.INVALIDATED,
                 frozenset({TaskRole.SYSTEM}),
                 "an input this task depended on changed"),
    )


EDGES: tuple = _edges()
_BY_PAIR = {(e.src, e.dst): e for e in EDGES}
INITIAL: TaskState = TaskState.CREATED

#: States with no outgoing edge at all: nothing further may be recorded.
#: DERIVED from the table rather than written beside it, so the guard and the
#: table cannot drift apart.
SEALED: FrozenSet[TaskState] = frozenset(
    s for s in TaskState if not any(e.src is s for e in EDGES))


def find_edge(src: TaskState, dst: TaskState) -> TaskEdge | None:
    return _BY_PAIR.get((src, dst))


def allowed_targets(src: TaskState) -> FrozenSet[TaskState]:
    return frozenset(e.dst for e in EDGES if e.src == src)


@dataclass(frozen=True)
class Lease:
    """Ownership of a task, with an expiry the log can adjudicate."""

    lease_id: str
    holder: str
    granted_seq: int
    expires_after_seq: int

    def is_live(self, at_seq: int) -> bool:
        return at_seq <= self.expires_after_seq

    def to_record(self) -> dict:
        return {"lease_id": self.lease_id, "holder": self.holder,
                "granted_seq": self.granted_seq,
                "expires_after_seq": self.expires_after_seq}


@dataclass(frozen=True)
class Task:
    """One unit of governed work."""

    task_id: str
    tool_id: str
    submitter: str
    inputs_digest: str
    state: TaskState = INITIAL
    revision: int = 0
    lease: Lease | None = None
    #: The actor whose execution produced the current result, for the
    #: separation-of-duties check on verification.
    executed_by: str | None = None
    #: Digest of the ExecutionResult record, once there is one.
    result_digest: str | None = None
    depends_on: tuple = ()
    created_seq: int = -1
    updated_seq: int = -1
    reason: str = ""

    def to_record(self) -> dict:
        return {
            "task_id": self.task_id, "tool_id": self.tool_id,
            "submitter": self.submitter, "inputs_digest": self.inputs_digest,
            "state": self.state.value, "revision": self.revision,
            "lease": self.lease.to_record() if self.lease else None,
            "executed_by": self.executed_by,
            "result_digest": self.result_digest,
            "depends_on": list(self.depends_on),
            "created_seq": self.created_seq, "updated_seq": self.updated_seq,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TaskTransition:
    """A proposed move, described independently of who is asking."""

    task_id: str
    src: TaskState
    dst: TaskState
    actor: str
    role: TaskRole
    at_seq: int
    lease_id: str | None = None
    executed_by: str | None = None
    result_digest: str | None = None


def check(req: TaskTransition, task: Task) -> TaskEdge:
    """Authorize a task transition, or raise. This is the whole gate.

    Returns the edge so the caller records WHY the move was permitted, not
    merely that it was.
    """
    edge = find_edge(req.src, req.dst)
    if edge is None:
        if req.src in TERMINAL:
            raise TaskTransitionError(
                f"{req.src.value} is terminal; task {req.task_id} cannot "
                "leave it. "
                "Recovery is a NEW task, which leaves a trail; reviving this "
                "one would not.")
        raise TaskTransitionError(
            f"no edge {req.src.value} -> {req.dst.value}; permitted targets "
            f"are {sorted(s.value for s in allowed_targets(req.src))}")
    if req.role not in edge.roles:
        raise TaskTransitionError(
            f"role {req.role.value} may not perform {req.src.value} -> "
            f"{req.dst.value}; requires one of "
            f"{sorted(r.value for r in edge.roles)}")

    if edge.requires_lease:
        lease = task.lease
        if lease is None:
            raise TaskTransitionError(
                f"{req.src.value} -> {req.dst.value} requires the task's "
                "lease, and it holds none")
        if req.lease_id != lease.lease_id:
            raise LeaseError(
                f"lease {req.lease_id!r} is not this task's lease "
                f"({lease.lease_id!r}); a worker reporting on work it "
                "does not "
                "own is reporting on work someone else may have redone")
        if lease.holder != req.actor:
            raise LeaseError(
                f"lease {lease.lease_id!r} is held by {lease.holder!r}, not "
                f"{req.actor!r}")
        if not lease.is_live(req.at_seq):
            raise LeaseError(
                f"lease {lease.lease_id!r} lapsed after seq "
                f"{lease.expires_after_seq}; the log is at {req.at_seq}. A "
                "worker back from the dead does not get to report success.")

    if edge.requires_distinct_actor:
        if task.executed_by is None:
            raise TaskTransitionError(
                f"{req.src.value} -> {req.dst.value} requires an actor "
                "distinct from the executor, but no executor is recorded; "
                "refusing rather than assuming independence")
        if req.actor == task.executed_by:
            raise TaskTransitionError(
                f"{req.actor!r} executed {req.task_id} and may not also "
                f"perform {req.src.value} -> {req.dst.value}. An agent that "
                "verifies its own work has not verified anything.")

    if req.dst is TaskState.COMPLETED and not is_digest(
            req.result_digest or ""):
        raise TaskTransitionError(
            "COMPLETED requires the digest of the execution result; a "
            "completion with no result to point at is an assertion")
    return edge


def apply_transition(task: Task, edge: TaskEdge, req: TaskTransition, *,
                     seq: int, lease: Lease | None = None) -> Task:
    """Fold an authorized transition into the task. Pure; no I/O."""
    new_lease = task.lease
    if req.dst is TaskState.LEASED:
        new_lease = lease
    elif req.dst in (TaskState.QUEUED,) or req.dst in TERMINAL:
        new_lease = None
    return replace(
        task, state=req.dst, revision=task.revision + 1, lease=new_lease,
        executed_by=req.executed_by or task.executed_by,
        result_digest=req.result_digest or task.result_digest,
        updated_seq=seq, reason=edge.reason)


@dataclass(frozen=True)
class TaskProjection:
    """Tasks as of a log position. Built by replay, never mutated in place."""

    tasks: dict = field(default_factory=dict)
    at_seq: int = -1

    def get(self, task_id: str) -> Task:
        try:
            return self.tasks[task_id]
        except KeyError:
            raise UnknownTask(f"no task {task_id!r}") from None

    def in_state(self, state: TaskState) -> tuple:
        return tuple(sorted(
            (t for t in self.tasks.values() if t.state is state),
            key=lambda t: t.task_id))

    def expired_leases(self) -> tuple:
        """Leased or executing tasks whose lease has lapsed.

        The scheduler's input for returning stranded work to the queue. A task
        is not stranded because its worker is slow; it is stranded because the
        log has moved past the point the worker promised to finish by.
        """
        out = []
        for t in sorted(self.tasks.values(), key=lambda t: t.task_id):
            if (t.state in (TaskState.LEASED, TaskState.EXECUTING)
                    and t.lease is not None
                    and not t.lease.is_live(self.at_seq)):
                out.append(t)
        return tuple(out)
