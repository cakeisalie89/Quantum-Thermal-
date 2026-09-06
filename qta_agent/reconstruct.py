"""Independent reconstruction of durable state from the event log alone.

This module exists to answer one question without trusting the running
system: *given only the log, what is canonical?*

It is written as a SECOND implementation on purpose. It does not import
:class:`~qta_agent.store.AuthorityStore` and does not share its reducer. If
both agree, that is differential evidence -- two implementations reading the
same evidence reached the same verdict. Reusing the store's reducer here would
make the comparison circular and worthless, which is why the duplication is
deliberate rather than an oversight.

Where the store folds events into dataclasses through ``dataclasses.replace``,
this walks the log with plain dictionaries and re-derives each field from
scratch. The two disagree loudly if either has a bug.

It trusts NOTHING except the log's bytes:
  * not the live process,
  * not any snapshot,
  * not the store's projection,
  * not conversation history or a model's recollection.

Every transition is re-authorized against the state machine during replay. An
event that the machine would refuse today is reported rather than applied --
which is how a log written by a compromised or older writer, or under a since
changed policy, becomes visible instead of being silently absorbed.

TWO MACHINES, THE SAME TREATMENT

:func:`reconstruct` covers authority records. :func:`reconstruct_tasks` covers
the task lifecycle, and it exists for a reason that is not symmetry: the task
projection is the one on the PRODUCTION path, and it is the one that turned
out to re-authorize forged records against a starting state the record itself
declared. A second implementation is the defence against that class -- not
because the second one is more careful, but because two readers that disagree
say so, and a single reader with a hole says nothing at all.

The two replays differ in what they do about a refusal, and deliberately.
``governed_stage10.projection`` is ENFORCEMENT: it raises, because a reader
that cannot tell which records went through the gate must not hand back a
state. This module is DIAGNOSIS: it records the problem and keeps going, so
one bad record does not hide the twenty after it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import actions
from .authority import (
    INITIAL,
    Role,
    State,
    TransitionError,
    TransitionRequest,
    check,
)
from .events import EventLog
from .tasks import (
    INITIAL as TASK_INITIAL,
)
from .tasks import (
    TERMINAL as TASK_TERMINAL,
)
from .tasks import (
    Lease,
    Task,
    TaskRole,
    TaskState,
    TaskTransition,
    TaskTransitionError,
)
from .tasks import (
    check as task_check,
)


@dataclass
class Reconstruction:
    """The verdict, plus everything needed to argue with it."""
    #: record_id -> plain dict of reconstructed fields
    records: dict = field(default_factory=dict)
    #: Transitions the state machine would refuse if replayed today.
    unauthorized: list = field(default_factory=list)
    #: Structural problems in the log that did not stop replay.
    anomalies: list = field(default_factory=list)
    events_replayed: int = 0
    #: Events belonging to another subsystem on the same log. Counted so a
    #: reader can tell "this reconstruction saw a mixed log and ignored the
    #: parts that are not authority records" from "this log had 3 events".
    foreign_events: int = 0
    head_seq: int = -1
    head_hash: str = ""

    def canonical_ids(self) -> tuple:
        return tuple(sorted(
            rid for rid, r in self.records.items()
            if r["state"] == State.PROMOTED.value))

    def states(self) -> dict:
        return {rid: r["state"] for rid, r in self.records.items()}


#: Actions this function interprets. Everything else this package writes is
#: another subsystem's and is counted rather than treated as damage.
_AUTHORITY_ACTIONS = frozenset({"record.create", "record.transition",
                                "record.depend"})


def reconstruct(log: EventLog, *, reauthorize: bool = True) -> Reconstruction:
    """Rebuild authority state from a verified log.

    Verification comes first and is fatal: reconstructing from a chain that
    does not verify would produce a confident answer from untrusted bytes.
    """
    report = log.verify()
    report.raise_if_bad()

    out = Reconstruction(head_seq=report.head_seq, head_hash=report.head_hash)
    # Deliberately dict-of-dicts rather than the store's dataclasses.
    recs: dict = out.records

    for ev in log.read():
        out.events_replayed += 1
        p = ev.payload
        action = ev.action
        rid = p.get("record_id")

        if action == "record.create":
            if rid in recs:
                out.anomalies.append(
                    f"seq {ev.seq}: duplicate create for {rid!r}")
                continue
            recs[rid] = {
                "record_id": rid,
                "kind": p.get("kind"),
                "proposer": p.get("proposer"),
                "state": p.get("state", INITIAL.value),
                "revision": 1,
                "evidence": dict(p.get("evidence", {})),
                "depends_on": list(p.get("depends_on", [])),
                "policy_id": p.get("policy_id"),
                "created_seq": ev.seq,
                "updated_seq": ev.seq,
                "stale_reason": None,
                "history": [(ev.seq, p.get("state", INITIAL.value))],
            }

        elif action == "record.transition":
            cur = recs.get(rid)
            if cur is None:
                out.anomalies.append(
                    f"seq {ev.seq}: transition for unknown record {rid!r}")
                continue
            src_claimed = p.get("src")
            if cur["state"] != src_claimed:
                out.anomalies.append(
                    f"seq {ev.seq}: {rid} claims src {src_claimed} but replay "
                    f"has it in {cur['state']}")
            if reauthorize:
                try:
                    check(TransitionRequest(
                        record_id=rid,
                        src=State(cur["state"]),
                        dst=State(p["dst"]),
                        actor=ev.actor,
                        role=Role(p["role"]),
                        evidence={**cur["evidence"], **p.get("evidence", {})},
                        proposer=cur["proposer"],
                        policy_id=p.get("policy_id") or cur["policy_id"]))
                except (TransitionError, ValueError) as exc:
                    out.unauthorized.append(
                        f"seq {ev.seq}: {rid} {cur['state']} -> "
                        f"{p.get('dst')} "
                        f"would be refused today: {exc}")
                    # Do NOT apply. An unauthorized transition must not become
                    # canonical merely because it is present in the log.
                    continue
            cur["state"] = p["dst"]
            cur["revision"] += 1
            cur["evidence"].update(p.get("evidence", {}))
            cur["updated_seq"] = ev.seq
            if p.get("stale_reason") is not None:
                cur["stale_reason"] = p["stale_reason"]
            if p.get("policy_id") is not None:
                cur["policy_id"] = p["policy_id"]
            cur["history"].append((ev.seq, p["dst"]))

        elif action == "record.depend":
            cur = recs.get(rid)
            if cur is None:
                out.anomalies.append(
                    f"seq {ev.seq}: dependency for unknown record {rid!r}")
                continue
            for dep in p.get("depends_on", []):
                if dep not in cur["depends_on"]:
                    cur["depends_on"].append(dep)
            cur["revision"] += 1
            cur["updated_seq"] = ev.seq

        elif actions.classify(action, mine=_AUTHORITY_ACTIONS) \
                == actions.FOREIGN:
            # Another subsystem's event. Counted, not applied, and NOT an
            # anomaly: the authority records this function rebuilds are not
            # affected by it. What IS an anomaly is the case below.
            out.foreign_events += 1
        else:
            out.anomalies.append(
                f"seq {ev.seq}: unknown action {action!r}; not applied. "
                "Nothing in this package writes it, so this reconstruction "
                "is missing whatever it recorded.")
    return out


@dataclass(frozen=True)
class Divergence:
    """A disagreement between the live projection and the reconstruction."""
    record_id: str
    field_name: str
    live: object
    reconstructed: object

    def __str__(self) -> str:
        return (f"{self.record_id}.{self.field_name}: live={self.live!r} "
                f"reconstructed={self.reconstructed!r}")


@dataclass
class TaskReconstruction:
    """The task lifecycle as a second reader sees it."""
    #: task_id -> plain dict of reconstructed fields
    tasks: dict = field(default_factory=dict)
    #: (owner, tool_id, key) -> plain dict of binding fields.
    #:
    #: Keyed by the TUPLE, deliberately, rather than by the digest
    #: IdempotencyLedger uses for the same scope. Sharing that digest would
    #: make the two readers agree by construction about the one thing worth
    #: checking independently -- whether two submissions occupy the same
    #: namespace. A collision or a mis-derived scope on either side shows up
    #: here as a disagreement instead of being reproduced faithfully.
    bindings: dict = field(default_factory=dict)
    #: Transitions the machine would refuse if replayed today.
    unauthorized: list = field(default_factory=list)
    #: Structural problems that did not stop replay.
    anomalies: list = field(default_factory=list)
    events_replayed: int = 0
    foreign_events: int = 0
    head_seq: int = -1
    head_hash: str = ""

    def states(self) -> dict:
        return {tid: t["state"] for tid, t in sorted(self.tasks.items())}

    def verified_ids(self) -> tuple:
        return tuple(sorted(tid for tid, t in self.tasks.items()
                            if t["state"] == TaskState.VERIFIED.value))


def reconstruct_tasks(log: EventLog, *,
                      reauthorize: bool = True) -> TaskReconstruction:
    """Replay the task lifecycle from the log alone. Never raises on content.

    A SECOND implementation, in plain dictionaries, sharing no reducer with
    ``governed_stage10.projection``. See the module docstring for why that
    duplication is the point rather than an oversight.
    """
    report = log.verify()
    report.raise_if_bad()
    out = TaskReconstruction(head_seq=report.head_seq,
                             head_hash=report.head_hash)
    tasks: dict = {}
    bindings: dict = {}
    owned = {"task.create", "task.transition", "task.execution",
             "task.evidence", "idempotency.bind"}

    for ev in log.read():
        out.events_replayed += 1
        action = ev.action
        if action not in owned:
            kind = actions.classify(action, mine=owned)
            if kind == actions.UNKNOWN:
                out.anomalies.append(
                    f"seq {ev.seq}: unknown action {action!r}; no module in "
                    "this package writes it, so this reconstruction cannot "
                    "say what it meant")
            else:
                out.foreign_events += 1
            continue
        p = ev.payload

        if action == "idempotency.bind":
            _replay_binding(ev, p, bindings, out)
            continue

        tid = p.get("task_id", ev.target)

        if action == "task.create":
            if tid in tasks:
                out.anomalies.append(
                    f"seq {ev.seq}: task {tid!r} created twice; the second "
                    "record would silently replace the first one's history")
                continue
            tasks[tid] = {
                "task_id": tid, "tool_id": p.get("tool_id"),
                "submitter": p.get("submitter"),
                "inputs_digest": p.get("inputs_digest"),
                "state": TASK_INITIAL.value, "revision": 1,
                "executed_by": None, "result_digest": None,
                "lease": None, "depends_on": list(p.get("depends_on") or ()),
                "created_seq": ev.seq, "updated_seq": ev.seq,
                "artifacts": {}, "history": [(ev.seq, TASK_INITIAL.value)],
            }
            continue

        cur = tasks.get(tid)
        if cur is None:
            out.anomalies.append(
                f"seq {ev.seq}: {action} for unknown task {tid!r}; nothing "
                "records what was being asked for")
            continue

        if action == "task.evidence":
            arts = p.get("artifacts") or {}
            cur["artifacts"].update(arts)
            continue
        if action == "task.execution":
            cur["executed_by"] = ev.actor
            cur["result_digest"] = p.get("result_digest")
            continue

        # task.transition
        claimed = p.get("src")
        if cur["state"] != claimed:
            out.anomalies.append(
                f"seq {ev.seq}: {tid} claims src {claimed} but replay has it "
                f"in {cur['state']}")
        # The same question about the OTHER field the record gets to name.
        # Who executed the task decides who is allowed to verify it, and only
        # a task.execution record establishes it. A transition may repeat
        # that answer; it may not supply one, and it may not change it.
        claimed_by = p.get("executed_by")
        if claimed_by is not None and claimed_by != cur["executed_by"]:
            out.anomalies.append(
                f"seq {ev.seq}: {tid} names {claimed_by!r} as its executor, "
                f"but replay has {cur['executed_by']!r}; the executor comes "
                "from the execution record, so this record is naming the "
                "actor that verification has to differ from")
        lease = None
        if p.get("lease"):
            try:
                lease = Lease(**p["lease"])
            except TypeError:
                out.anomalies.append(
                    f"seq {ev.seq}: {tid} carries a lease record this build "
                    "cannot interpret")
        if reauthorize:
            # From the state THIS replay reached, never from the claim. A
            # forger who names a convenient src would otherwise have every
            # pair in the table available, which is exactly the defect the
            # production projection had.
            probe = Task(
                task_id=tid, tool_id=cur["tool_id"] or "",
                submitter=cur["submitter"] or "",
                inputs_digest=cur["inputs_digest"] or "",
                state=TaskState(cur["state"]), revision=cur["revision"],
                lease=_lease_of(cur) or lease,
                executed_by=cur["executed_by"],
                result_digest=cur["result_digest"])
            try:
                task_check(TaskTransition(
                    task_id=tid, src=TaskState(cur["state"]),
                    dst=TaskState(p["dst"]), actor=ev.actor,
                    role=TaskRole(p["role"]), at_seq=ev.seq,
                    lease_id=(p.get("lease_id")
                              or (probe.lease.lease_id if probe.lease
                                  else None)),
                    executed_by=cur["executed_by"],
                    result_digest=p.get("result_digest")), probe)
            except (TaskTransitionError, ValueError, KeyError) as exc:
                out.unauthorized.append(
                    f"seq {ev.seq}: {tid} {cur['state']} -> {p.get('dst')} "
                    f"would be refused today: {exc}")
                # Do NOT apply. Presence in the log is not authority.
                continue

        # The lease follows the task, not the record. Only the move INTO
        # LEASED carries one; the moves that need it afterwards cite its id
        # and rely on the task still holding it. Replacing the lease on every
        # transition drops it at the next step and then refuses the
        # completion -- which is what this replay did until the differential
        # test compared it against the live projection and disagreed.
        dst = TaskState(p["dst"])
        if dst is TaskState.LEASED:
            cur["lease"] = dict(p["lease"]) if p.get("lease") else None
        elif dst is TaskState.QUEUED or dst in TASK_TERMINAL:
            # Requeued or finished work holds nothing: a lease that outlives
            # the work it owned is a lease somebody else has to wait out.
            cur["lease"] = None
        cur["state"] = p["dst"]
        cur["revision"] += 1
        cur["updated_seq"] = ev.seq
        if p.get("result_digest") is not None:
            cur["result_digest"] = p["result_digest"]
        # cur["executed_by"] is NOT updated here. It was, and that single
        # line put this reader back underneath the bypass the production
        # projection had already been fixed for: the reauthorization above
        # correctly used the replayed executor, and then the payload
        # overwrote it in time for the NEXT transition to be checked against
        # the forger's choice.
        cur["history"].append((ev.seq, p["dst"]))

    out.tasks = tasks
    out.bindings = bindings
    return out


def _replay_binding(ev, p: dict, bindings: dict, out) -> None:
    """Project one idempotency.bind, in this reader's own words.

    Shares no code with :class:`~qta_agent.idempotency.IdempotencyLedger`.
    The rules are restated rather than imported, because a second reader
    that calls the first one's reducer is not a second reader -- it is the
    same decision, run twice, agreeing with itself.

    Records anomalies rather than raising: this module's contract is that a
    hostile history produces findings, not an exception that hides the rest
    of the log.
    """
    if not isinstance(p, dict):
        out.anomalies.append(
            f"seq {ev.seq}: idempotency binding payload is not an object")
        return
    key, tool_id = p.get("key"), p.get("tool_id")
    task_id, request_digest = p.get("task_id"), p.get("request_digest")
    for name, value in (("key", key), ("tool_id", tool_id),
                        ("task_id", task_id),
                        ("request_digest", request_digest)):
        if not isinstance(value, str) or not value:
            out.anomalies.append(
                f"seq {ev.seq}: idempotency binding has no usable {name!r}")
            return
    # The owner is the EVENT'S actor. A payload naming its own owner chose
    # whose namespace to write into.
    claimed_owner = p.get("owner")
    if claimed_owner is not None and claimed_owner != ev.actor:
        out.anomalies.append(
            f"seq {ev.seq}: binding names owner {claimed_owner!r} but was "
            f"appended by {ev.actor!r}; a lookup answered for the claimed "
            "owner would hand one actor another's task")
        return
    claimed_seq = p.get("bound_seq")
    if claimed_seq is not None and claimed_seq != ev.seq:
        out.anomalies.append(
            f"seq {ev.seq}: binding claims bound_seq {claimed_seq!r}, which "
            "backdates the moment a duplicate would first have been caught")
        return

    scope = (ev.actor, tool_id, key)
    prior = bindings.get(scope)
    if prior is not None:
        if (prior["task_id"] == task_id
                and prior["request_digest"] == request_digest):
            return                       # a retried append of the same bind
        out.anomalies.append(
            f"seq {ev.seq}: idempotency key {key!r} for {tool_id!r} is "
            f"rebound from task {prior['task_id']!r} to {task_id!r}; every "
            "later resubmission of the original request would resolve to "
            "the new work")
        return
    bindings[scope] = {
        "key": key, "owner": ev.actor, "tool_id": tool_id,
        "task_id": task_id, "request_digest": request_digest,
        "job_id": p.get("job_id", ""), "bound_seq": ev.seq,
    }


def compare_bindings(ledger, recon) -> tuple:
    """Divergences between the ledger's bindings and the second reader's.

    The interesting direction is a binding one reader holds and the other
    does not: that is a namespace the two disagree about, and the ledger is
    what decides whether work re-runs.
    """
    out: list = []
    mine = {(b.owner, b.tool_id, b.key): b
            for b in ledger.bindings().values()}
    theirs = recon.bindings
    for scope in sorted(set(mine) | set(theirs)):
        a, b = mine.get(scope), theirs.get(scope)
        label = f"{scope[0]}/{scope[1]}/{scope[2]}"
        if a is None:
            out.append(Divergence(label, "binding", None, b["task_id"]))
            continue
        if b is None:
            out.append(Divergence(label, "binding", a.task_id, None))
            continue
        for fld, x, y in (("task_id", a.task_id, b["task_id"]),
                          ("request_digest", a.request_digest,
                           b["request_digest"]),
                          ("bound_seq", a.bound_seq, b["bound_seq"])):
            if x != y:
                out.append(Divergence(label, fld, x, y))
    return tuple(out)


def _lease_of(cur: dict):
    """Rebuild the lease this replay is currently holding, if any."""
    raw = cur.get("lease")
    if not raw:
        return None
    try:
        return Lease(**raw)
    except TypeError:                      # pragma: no cover - malformed
        return None


def compare_tasks(projection, recon: TaskReconstruction) -> tuple:
    """Diff the live task projection against the independent replay.

    Empty means two implementations reading the same bytes reached the same
    verdict. Anything else is a divergence one of them has to answer for.
    """
    diffs: list = []
    live = dict(projection.tasks)
    for tid in sorted(set(live) | set(recon.tasks)):
        if tid not in live:
            diffs.append(Divergence(tid, "<presence>", "ABSENT", "present"))
            continue
        if tid not in recon.tasks:
            diffs.append(Divergence(tid, "<presence>", "present", "ABSENT"))
            continue
        lv, rc = live[tid], recon.tasks[tid]
        for name, lval, rval in (
            ("state", lv.state.value, rc["state"]),
            ("tool_id", lv.tool_id, rc["tool_id"]),
            ("submitter", lv.submitter, rc["submitter"]),
            ("inputs_digest", lv.inputs_digest, rc["inputs_digest"]),
            ("executed_by", lv.executed_by, rc["executed_by"]),
            ("result_digest", lv.result_digest, rc["result_digest"]),
            # The lease is state, not decoration. A replay that keeps a
            # finished task's lease says the work is still owned by a worker
            # that has stopped, and nothing else in this diff would notice.
            ("lease", lv.lease.to_record() if lv.lease else None,
             rc["lease"]),
        ):
            if lval != rval:
                diffs.append(Divergence(tid, name, lval, rval))
    return tuple(diffs)


def compare(store, recon: Reconstruction) -> tuple:
    """Diff a live store against an independent reconstruction.

    Returns a tuple of :class:`Divergence`. Empty means the two implementations
    agree -- the only outcome that should ever occur in a healthy system.
    """
    diffs: list = []
    live = store.all_records()
    for rid in sorted(set(live) | set(recon.records)):
        if rid not in live:
            diffs.append(Divergence(rid, "<presence>", "ABSENT", "present"))
            continue
        if rid not in recon.records:
            diffs.append(Divergence(rid, "<presence>", "present", "ABSENT"))
            continue
        lv, rc = live[rid], recon.records[rid]
        for name, lval, rval in (
            ("state", lv.state.value, rc["state"]),
            ("kind", lv.kind, rc["kind"]),
            ("proposer", lv.proposer, rc["proposer"]),
            ("revision", lv.revision, rc["revision"]),
            ("evidence", dict(lv.evidence), rc["evidence"]),
            ("depends_on", list(lv.depends_on), rc["depends_on"]),
            ("policy_id", lv.policy_id, rc["policy_id"]),
        ):
            if lval != rval:
                diffs.append(Divergence(rid, name, lval, rval))
    return tuple(diffs)
