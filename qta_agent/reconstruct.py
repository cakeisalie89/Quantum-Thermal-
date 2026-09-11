"""Independent reconstruction of durable state from the event log alone.

This module exists to answer one question without trusting the running
system: *given only the log, what is canonical?*

It is written as a SECOND implementation on purpose. It does not import
:class:`~qta_agent.store.AuthorityStore` and does not share its reducer, and
it does not import the modules whose decisions it re-checks -- not their
enums, not their tables, and above all not their ``check`` functions. If both
agree, that is differential evidence: two implementations reading the same
evidence reached the same verdict. Reusing the store's reducer, or calling
the gate to ask whether the gate would have allowed something, would make the
comparison circular and worthless, which is why the duplication is deliberate
rather than an oversight.

That duplication was, for a long time, only HALF true here. The nine
subsystems below restated every rule in plain strings; the two machines at
the top of this file -- authority records and tasks -- imported
``authority.check`` and ``tasks.check`` and handed each replayed record
straight back to them. A weakened production gate would have been reproduced
faithfully by the reader that exists to notice it, and the differential
comparison would have come back empty: the most reassuring possible output
from two readers that never disagreed about anything. Both are restated now,
in the block under the imports.

Where the store folds events into dataclasses through ``dataclasses.replace``,
this walks the log with plain dictionaries and re-derives each field from
scratch. The two disagree loudly if either has a bug.

It trusts NOTHING except the log's bytes:
  * not the live process,
  * not any snapshot,
  * not the store's projection,
  * not conversation history or a model's recollection.

Every transition is re-authorized during replay against THIS module's own
statement of the rules. An event those rules would refuse today is reported
rather than applied -- which is how a log written by a compromised or older
writer, or under a since changed policy, becomes visible instead of being
silently absorbed. Whether this module's statement of the rules still matches
production's is a question for the conformance tests, which can name a
difference; it is not a question a reader that called production could ever
have asked.

TWO MACHINES, THE SAME TREATMENT

:func:`reconstruct` covers authority records. :func:`reconstruct_tasks` covers
the task lifecycle, and it exists for a reason that is not symmetry: the task
projection is the one on the PRODUCTION path, and it is the one that turned
out to re-authorize forged records against a starting state the record itself
declared. A second implementation is the defence against that class -- not
because the second one is more careful, but because two readers that disagree
say so, and a single reader with a hole says nothing at all.

Both now judge records the same way as well: against restated rules, with a
refusal recorded as a string rather than raised as somebody else's exception
type.

The two replays differ in what they do about a refusal, and deliberately.
``governed_stage10.projection`` is ENFORCEMENT: it raises, because a reader
that cannot tell which records went through the gate must not hand back a
state. This module is DIAGNOSIS: it records the problem and keeps going, so
one bad record does not hide the twenty after it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import actions
from .events import EventLog

# ---------------------------------------------------------------------------
# THE RULES, RESTATED.
#
# This module used to import ``authority.check`` and ``tasks.check`` and hand
# each replayed record to them. That made the "independent" re-authorization a
# call back into the implementation it exists to check: if the production gate
# was wrong, this reader was wrong in the same way and the differential
# comparison came back empty -- the most reassuring possible output from two
# readers that never actually disagreed about anything.
#
# The docstring on :class:`SubsystemReconstruction` below already said so, in
# those words, about the nine subsystems it covers. The two machines at the
# TOP of this file -- authority records and tasks -- were the ones still doing
# it, which is the shape of defect this whole module exists to find: the rule
# was written down, the rule was right, and the code beside it did the other
# thing.
#
# So: everything a replayed record is judged against is spelled out here, in
# plain strings and plain dicts, derived from nothing. That has a cost -- two
# statements of the same rule can drift -- and the cost is paid deliberately:
#
#   * drift is a TEST failure, not a silent agreement. The conformance tests
#     in tests/test_agent_differential.py compare these tables element by
#     element against the production ones and name the difference.
#   * agreement by construction is not recoverable by any test at all. A
#     reader that calls the gate agrees with a broken gate perfectly, and
#     there is no assertion that can see it from outside.
#
# A restatement that is merely a copy of the table is still worth having for
# the second reason. It is not worth pretending it is more than that: two
# statements can share a MISUNDERSTANDING that the log cannot reveal, so an
# empty diff here is evidence, not proof.
# ---------------------------------------------------------------------------

#: Every authority state this reader knows. A record naming anything else is
#: reported rather than applied: a state this reader cannot reason about is
#: not a state it may silently carry forward.
_AUTH_STATES = frozenset({
    "PROPOSED", "UNDER_REVIEW", "VERIFIED", "PROMOTED", "STALE",
    "SUPERSEDED", "REVOKED", "REJECTED",
})

#: Who may act on an authority record.
_AUTH_ROLES = frozenset({"PROPOSER", "VERIFIER", "PROMOTER", "SYSTEM"})

#: Where a record is born.
_AUTH_INITIAL = "PROPOSED"

#: States nothing may leave (I2). Leaving one is a revival, and recovery is a
#: NEW record with new evidence, which leaves a trail.
_AUTH_TERMINAL = frozenset({"REVOKED", "REJECTED"})

#: The one state carrying canonical authority.
_AUTH_PROMOTED = "PROMOTED"


@dataclass(frozen=True)
class _Rule:
    """One permitted authority transition, as this reader states it."""

    #: Roles permitted to trigger it. Empty would mean nobody.
    roles: frozenset
    #: Evidence keys that must be present, and digest-shaped unless the key
    #: is an identity rather than content.
    evidence: frozenset = frozenset()
    #: True when the actor must differ from the record's proposer (I4).
    distinct_actor: bool = False


#: The authority transition graph. (src, dst) -> the rule that permits it.
#: Any pair absent from this table is forbidden, which is what makes the
#: forbidden states unreachable rather than merely unwritten.
_AUTH_EDGES = {
    ("PROPOSED", "UNDER_REVIEW"): _Rule(frozenset({"VERIFIER"})),
    ("PROPOSED", "REJECTED"): _Rule(
        frozenset({"VERIFIER"}), frozenset({"rejection_reason"}), True),
    ("UNDER_REVIEW", "VERIFIED"): _Rule(
        frozenset({"VERIFIER"}), frozenset({"verification_report"}), True),
    ("UNDER_REVIEW", "REJECTED"): _Rule(
        frozenset({"VERIFIER"}), frozenset({"rejection_reason"}), True),
    # I1: the ONLY edge into PROMOTED, and it needs a different actor.
    ("VERIFIED", "PROMOTED"): _Rule(
        frozenset({"PROMOTER"}),
        frozenset({"verification_report", "policy_id"}), True),
    ("VERIFIED", "STALE"): _Rule(
        frozenset({"SYSTEM"}), frozenset({"invalidated_by"})),
    ("PROMOTED", "STALE"): _Rule(
        frozenset({"SYSTEM"}), frozenset({"invalidated_by"})),
    ("PROMOTED", "SUPERSEDED"): _Rule(
        frozenset({"PROMOTER"}), frozenset({"superseded_by"})),
    ("PROMOTED", "REVOKED"): _Rule(
        frozenset({"PROMOTER"}), frozenset({"revocation_reason"})),
    ("VERIFIED", "REVOKED"): _Rule(
        frozenset({"PROMOTER"}), frozenset({"revocation_reason"})),
    # I3: STALE returns only through re-verification.
    ("STALE", "UNDER_REVIEW"): _Rule(frozenset({"VERIFIER"})),
    ("STALE", "SUPERSEDED"): _Rule(
        frozenset({"PROMOTER"}), frozenset({"superseded_by"})),
    ("STALE", "REVOKED"): _Rule(
        frozenset({"PROMOTER"}), frozenset({"revocation_reason"})),
    ("SUPERSEDED", "REVOKED"): _Rule(
        frozenset({"PROMOTER"}), frozenset({"revocation_reason"})),
}

#: Evidence keys that name an IDENTITY rather than a piece of content, and so
#: are required to be present and non-empty rather than digest-shaped. Stated
#: as a set rather than as a special case buried in a loop so that adding a
#: second such key is a decision somebody makes on purpose.
_AUTH_IDENTITY_EVIDENCE = frozenset({"policy_id"})

_HEX = frozenset("0123456789abcdef")


def _is_digest(value: object) -> bool:
    """True for a lowercase 64-character sha256 hex digest, and nothing else.

    Restated rather than imported for the same reason as everything else
    here. Uppercase is rejected deliberately: one logical digest with two
    spellings makes a set of digests contain duplicates that compare unequal.
    """
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX for c in value))


def _auth_refusal(*, record_id, src, dst, actor, role, evidence, proposer,
                  policy_id):
    """Why this authority transition would be refused, or None if it stands.

    A STRING rather than an exception, because this reader diagnoses instead
    of enforcing: one bad record must not hide the twenty after it.
    """
    if src not in _AUTH_STATES:
        return (f"{src!r} is not an authority state this reader knows, so it "
                "cannot say what may follow it")
    if dst not in _AUTH_STATES:
        return (f"{dst!r} is not an authority state this reader knows; a "
                "record may not invent one")
    if role not in _AUTH_ROLES:
        return (f"{role!r} is not a role this reader knows; a record may not "
                "invent the authority it acts under")

    if src in _AUTH_TERMINAL:
        return (f"I2: {src} is terminal; {record_id} cannot leave it. Create "
                "a new record with new evidence instead.")

    rule = _AUTH_EDGES.get((src, dst))
    if rule is None:
        return (f"no edge {src} -> {dst}; permitted targets are "
                f"{sorted(d for (s, d) in _AUTH_EDGES if s == src)}")

    if role not in rule.roles:
        return (f"role {role} may not perform {src} -> {dst}; requires one "
                f"of {sorted(rule.roles)}")

    if rule.distinct_actor:
        if proposer is None:
            return (f"I4: {src} -> {dst} requires a distinct actor, but the "
                    "record's proposer is unknown; refusing rather than "
                    "assuming separation of duties")
        if actor == proposer:
            return (f"I4: {actor!r} proposed {record_id} and may not also "
                    f"perform {src} -> {dst}")

    missing = sorted(rule.evidence - set(evidence))
    if missing:
        return f"I6: {src} -> {dst} requires evidence {missing}"

    for key in sorted(rule.evidence):
        val = evidence.get(key)
        if key in _AUTH_IDENTITY_EVIDENCE:
            if not isinstance(val, str) or not val:
                return f"I5: {key} must be a non-empty id"
            continue
        if not _is_digest(val):
            return (f"I6: evidence {key!r} must be a sha256 digest, got "
                    f"{type(val).__name__}; evidence is referenced by content "
                    "so it cannot be altered after being cited")

    if dst == _AUTH_PROMOTED and policy_id is None:
        return "I5: promotion requires an explicit policy identity in force"
    return None


#: Every task state this reader knows.
_TASK_STATES = frozenset({
    "CREATED", "VALIDATED", "QUEUED", "LEASED", "EXECUTING", "COMPLETED",
    "FAILED", "TIMED_OUT", "CANCELLED", "VERIFIED", "REJECTED", "INVALIDATED",
})

#: Who may move a task.
_TASK_ROLES = frozenset({"SUBMITTER", "SCHEDULER", "WORKER", "VERIFIER",
                         "SYSTEM"})

_TASK_INITIAL = "CREATED"

#: Finished work. No further PROGRESS is possible from these -- which is not
#: the same as sealed: VERIFIED -> INVALIDATED records a fact ABOUT finished
#: work, and is in the table below for exactly that reason.
_TASK_TERMINAL = frozenset({"VERIFIED", "REJECTED", "CANCELLED",
                            "INVALIDATED"})

_TASK_LEASED = "LEASED"
_TASK_QUEUED = "QUEUED"
_TASK_COMPLETED = "COMPLETED"
_TASK_VERIFIED = "VERIFIED"


@dataclass(frozen=True)
class _TaskRule:
    """One permitted task transition, as this reader states it."""

    roles: frozenset
    #: True when the actor must differ from the one that executed the task.
    distinct_actor: bool = False
    #: True when the mover must hold the task's current, unexpired lease.
    lease: bool = False


#: States a task may be cancelled from: every pre-terminal one.
_TASK_CANCELLABLE = ("CREATED", "VALIDATED", "QUEUED", "LEASED", "EXECUTING")

#: The task transition graph.
_TASK_EDGES = {
    ("CREATED", "VALIDATED"): _TaskRule(
        frozenset({"SUBMITTER", "SCHEDULER"})),
    ("CREATED", "REJECTED"): _TaskRule(
        frozenset({"SUBMITTER", "SCHEDULER"})),
    ("VALIDATED", "QUEUED"): _TaskRule(frozenset({"SCHEDULER"})),
    ("QUEUED", "LEASED"): _TaskRule(frozenset({"SCHEDULER", "WORKER"})),
    ("LEASED", "EXECUTING"): _TaskRule(frozenset({"WORKER"}), lease=True),
    ("EXECUTING", "COMPLETED"): _TaskRule(frozenset({"WORKER"}), lease=True),
    ("EXECUTING", "FAILED"): _TaskRule(frozenset({"WORKER"}), lease=True),
    ("EXECUTING", "TIMED_OUT"): _TaskRule(frozenset({"WORKER", "SYSTEM"})),
    # The ONLY edge into VERIFIED, and it needs a different actor.
    ("COMPLETED", "VERIFIED"): _TaskRule(
        frozenset({"VERIFIER"}), distinct_actor=True),
    ("COMPLETED", "REJECTED"): _TaskRule(
        frozenset({"VERIFIER"}), distinct_actor=True),
    **{(s, "CANCELLED"): _TaskRule(
        frozenset({"SUBMITTER", "SCHEDULER", "SYSTEM"}))
       for s in _TASK_CANCELLABLE},
    # A lapsed lease returns the work to the queue rather than stranding it.
    ("LEASED", "QUEUED"): _TaskRule(frozenset({"SCHEDULER", "SYSTEM"})),
    ("EXECUTING", "QUEUED"): _TaskRule(frozenset({"SCHEDULER", "SYSTEM"})),
    # Retry paths for the outcomes that are retryable.
    ("FAILED", "QUEUED"): _TaskRule(frozenset({"SCHEDULER"})),
    ("TIMED_OUT", "QUEUED"): _TaskRule(frozenset({"SCHEDULER"})),
    # An input changed, so a prior verification no longer describes it.
    ("VERIFIED", "INVALIDATED"): _TaskRule(frozenset({"SYSTEM"})),
}

#: The fields a lease record must carry, and the one it may. Restated so that
#: a lease shape this build cannot interpret is a finding rather than a
#: TypeError from somebody else's constructor.
_LEASE_REQUIRED = frozenset({"lease_id", "holder", "granted_seq",
                             "expires_after_seq"})
_LEASE_OPTIONAL = frozenset({"holder_process"})


def _parse_lease(raw):
    """The lease as plain fields, or None if this reader cannot read it."""
    if not isinstance(raw, dict) or not raw:
        return None
    keys = set(raw)
    if not _LEASE_REQUIRED <= keys:
        return None
    if not keys <= (_LEASE_REQUIRED | _LEASE_OPTIONAL):
        return None
    return dict(raw)


def _task_refusal(*, task_id, src, dst, actor, role, at_seq, lease, lease_id,
                  executed_by, result_digest):
    """Why this task transition would be refused, or None if it stands."""
    if src not in _TASK_STATES:
        return (f"{src!r} is not a task state this reader knows, so it "
                "cannot say what may follow it")
    if dst not in _TASK_STATES:
        return (f"{dst!r} is not a task state this reader knows; a record "
                "may not invent one")
    if role not in _TASK_ROLES:
        return (f"{role!r} is not a task role this reader knows; a record "
                "may not invent the authority it acts under")

    rule = _TASK_EDGES.get((src, dst))
    if rule is None:
        if src in _TASK_TERMINAL:
            return (f"{src} is terminal; task {task_id} cannot leave it. "
                    "Recovery is a NEW task, which leaves a trail; reviving "
                    "this one would not.")
        return (f"no edge {src} -> {dst}; permitted targets are "
                f"{sorted(d for (s, d) in _TASK_EDGES if s == src)}")

    if role not in rule.roles:
        return (f"role {role} may not perform {src} -> {dst}; requires one "
                f"of {sorted(rule.roles)}")

    if rule.lease:
        if not lease:
            return (f"{src} -> {dst} requires the task's lease, and it holds "
                    "none")
        if lease_id != lease.get("lease_id"):
            return (f"lease {lease_id!r} is not this task's lease "
                    f"({lease.get('lease_id')!r}); a worker reporting on work "
                    "it does not own is reporting on work someone else may "
                    "have redone")
        if lease.get("holder") != actor:
            return (f"lease {lease.get('lease_id')!r} is held by "
                    f"{lease.get('holder')!r}, not {actor!r}")
        end = lease.get("expires_after_seq")
        # Not a special case for bools, deliberately: production compares
        # ``at_seq <= expires_after_seq`` and Python's True IS 1 there, so a
        # reader that refused booleans would disagree with the gate about an
        # input the gate accepts. The check is for a value nothing can order
        # against a sequence number at all -- where production raises a
        # TypeError out of the middle of a replay and this reader says so.
        if not isinstance(end, int):
            return (f"lease {lease.get('lease_id')!r} names no sequence it "
                    "expires after, so nothing can say whether it is live")
        if at_seq > end:
            return (f"lease {lease.get('lease_id')!r} lapsed after seq "
                    f"{end}; the log is at {at_seq}. A worker back from the "
                    "dead does not get to report success.")

    if rule.distinct_actor:
        if executed_by is None:
            return (f"{src} -> {dst} requires an actor distinct from the "
                    "executor, but no executor is recorded; refusing rather "
                    "than assuming independence")
        if actor == executed_by:
            return (f"{actor!r} executed {task_id} and may not also perform "
                    f"{src} -> {dst}. An agent that verifies its own work "
                    "has not verified anything.")

    if dst == _TASK_COMPLETED and not _is_digest(result_digest or ""):
        return ("COMPLETED requires the digest of the execution result; a "
                "completion with no result to point at is an assertion")
    return None



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
            if r["state"] == _AUTH_PROMOTED))

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
                "state": p.get("state", _AUTH_INITIAL),
                "revision": 1,
                "evidence": dict(p.get("evidence", {})),
                "depends_on": list(p.get("depends_on", [])),
                "policy_id": p.get("policy_id"),
                "created_seq": ev.seq,
                "updated_seq": ev.seq,
                "stale_reason": None,
                "history": [(ev.seq, p.get("state", _AUTH_INITIAL))],
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
                # Judged against THIS module's restatement of the rules, not
                # by calling authority.check. A second reader that asks the
                # gate whether the gate would have allowed something agrees
                # with a broken gate perfectly.
                refusal = _auth_refusal(
                    record_id=rid,
                    src=cur["state"],
                    dst=p.get("dst"),
                    actor=ev.actor,
                    role=p.get("role"),
                    evidence={**cur["evidence"], **p.get("evidence", {})},
                    proposer=cur["proposer"],
                    policy_id=p.get("policy_id") or cur["policy_id"])
                if refusal is not None:
                    out.unauthorized.append(
                        f"seq {ev.seq}: {rid} {cur['state']} -> "
                        f"{p.get('dst')} "
                        f"would be refused today: {refusal}")
                    # Do NOT apply. An unauthorized transition must not become
                    # canonical merely because it is present in the log.
                    continue
            if p.get("dst") not in _AUTH_STATES:
                # Reachable only with reauthorize=False, which is a DIAGNOSTIC
                # mode and not a permissive one: a state this reader cannot
                # name is not a state it may carry forward as though it had
                # understood it.
                out.anomalies.append(
                    f"seq {ev.seq}: {rid} moves to {p.get('dst')!r}, which is "
                    "not an authority state this reader knows; not applied")
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
                            if t["state"] == _TASK_VERIFIED))


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
            if p.get("submitter") != ev.actor:
                # Restated here for the reason every rule in this module is:
                # the primary refuses such a record, so a log carrying one
                # never reaches the comparison between the two readers.
                out.anomalies.append(
                    f"seq {ev.seq}: task {tid!r} names submitter "
                    f"{p.get('submitter')!r} and was appended by "
                    f"{ev.actor!r}; whoever asked for the work is whoever "
                    "wrote the request")
                continue
            tasks[tid] = {
                "task_id": tid, "tool_id": p.get("tool_id"),
                "submitter": p.get("submitter"),
                "inputs_digest": p.get("inputs_digest"),
                "state": _TASK_INITIAL, "revision": 1,
                "executed_by": None, "result_digest": None,
                "lease": None, "depends_on": list(p.get("depends_on") or ()),
                "created_seq": ev.seq, "updated_seq": ev.seq,
                "artifacts": {}, "history": [(ev.seq, _TASK_INITIAL)],
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
            lease = _parse_lease(p["lease"])
            if lease is None:
                out.anomalies.append(
                    f"seq {ev.seq}: {tid} carries a lease record this build "
                    "cannot interpret")
        if reauthorize:
            # From the state THIS replay reached, never from the claim. A
            # forger who names a convenient src would otherwise have every
            # pair in the table available, which is exactly the defect the
            # production projection had.
            #
            # And judged by _task_refusal above rather than by tasks.check:
            # the executor the separation-of-duties rule measures against
            # comes from the execution record this replay saw, and the rule
            # itself is stated here so that weakening the production one does
            # not quietly weaken this reader too.
            held = _lease_of(cur) or lease
            refusal = _task_refusal(
                task_id=tid, src=cur["state"], dst=p.get("dst"),
                actor=ev.actor, role=p.get("role"), at_seq=ev.seq,
                lease=held,
                lease_id=(p.get("lease_id")
                          or (held.get("lease_id") if held else None)),
                executed_by=cur["executed_by"],
                result_digest=p.get("result_digest"))
            if refusal is not None:
                out.unauthorized.append(
                    f"seq {ev.seq}: {tid} {cur['state']} -> {p.get('dst')} "
                    f"would be refused today: {refusal}")
                # Do NOT apply. Presence in the log is not authority.
                continue

        # The lease follows the task, not the record. Only the move INTO
        # LEASED carries one; the moves that need it afterwards cite its id
        # and rely on the task still holding it. Replacing the lease on every
        # transition drops it at the next step and then refuses the
        # completion -- which is what this replay did until the differential
        # test compared it against the live projection and disagreed.
        dst = p.get("dst")
        if dst not in _TASK_STATES:
            # Reachable only with reauthorize=False. See the authority replay
            # for why this is an anomaly rather than a silent application.
            out.anomalies.append(
                f"seq {ev.seq}: {tid} moves to {dst!r}, which is not a task "
                "state this reader knows; not applied")
            continue
        if dst == _TASK_LEASED:
            cur["lease"] = dict(p["lease"]) if p.get("lease") else None
        elif dst == _TASK_QUEUED or dst in _TASK_TERMINAL:
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


@dataclass
class SubsystemReconstruction:
    """Authority state of the subsystems that had no second reader.

    WHY THESE ARE HERE AND NOT IN THEIR OWN MODULES

    Because a second reader that lives beside the first, imports the
    first's enums and calls the first's helpers is not a second reader --
    it is the same decision run twice, agreeing with itself. This module
    sits BELOW scheduler, policy, capability, agents, memory, netauth,
    secrets and context in the declared layering, so it cannot import any
    of them even by accident. Everything below is plain strings and plain
    dicts, and every rule is restated rather than called.

    That restatement is the point and also the cost: two implementations
    can still share a misunderstanding the log cannot reveal, which is why
    an empty diff is evidence rather than proof.
    """

    jobs: dict = field(default_factory=dict)
    policies: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    net_grants: dict = field(default_factory=dict)
    secret_grants: dict = field(default_factory=dict)
    contexts: dict = field(default_factory=dict)
    #: The one actor this log permits to mint grants, or None if none yet.
    root_issuer: "str | None" = None
    #: escalation_id -> the question, its state and who decided it.
    escalations: dict = field(default_factory=dict)
    #: service_id -> its contract as this reader rebuilt it.
    services: dict = field(default_factory=dict)
    #: "service_id/task_id" -> permitted calls counted from the log.
    service_calls: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
    events_replayed: int = 0
    head_seq: int = -1


#: Job states a job may be BORN in. Restated here rather than imported: a
#: forged enqueue naming SUCCEEDED or DISPATCHED is the attack, and a second
#: reader that asks the scheduler what counts as initial would inherit the
#: scheduler's answer along with any mistake in it.
_JOB_INITIAL = {"WAITING", "READY"}

#: Terminal job states. A transition out of one is a revival.
_JOB_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}

#: States a job waits in. Reaching one means it is nobody's right now, so
#: whatever a lease said about ownership has stopped being true.
_JOB_PENDING = {"RETRY_WAIT", "BLOCKED"}

#: Leaving DISPATCHED with a verdict on the attempt that was running. Named
#: here, in strings, for the same reason as the sets above: a second reader
#: that asked the scheduler which edges are verdicts would agree with it by
#: construction, including where the scheduler is wrong.
_JOB_OUTCOME = {"SUCCEEDED", "RETRY_WAIT", "FAILED"}

#: The out-of-band sentinel a log's first humans are admitted under. Spelled
#: out here rather than imported, like everything else in this module: if the
#: directory's sentinel and this one ever drift apart, the divergence is the
#: finding rather than something both readers agree about by construction.
_BOOTSTRAP = "out-of-band-bootstrap"

#: Where a question for a human can be. Spelled out rather than imported,
#: like every other vocabulary in this module.
_ESC_OPEN = "OPEN"
_ESC_TERMINAL = {"ANSWERED", "WITHDRAWN"}

#: The fewest options an escalation may offer. A question with one answer is
#: a notification and should not block anything; a question with none cannot
#: be answered at all. Restated: if the directory's bound and this one drift
#: apart, the divergence is the finding.
_ESC_MIN_OPTIONS = 2


def reconstruct_subsystems(log: EventLog) -> SubsystemReconstruction:
    """Replay every remaining authority subsystem, independently.

    Never raises on content: a hostile history produces findings, not an
    exception that hides the rest of the log.
    """
    report = log.verify()
    report.raise_if_bad()
    out = SubsystemReconstruction(head_seq=report.head_seq)
    for ev in log.read():
        out.events_replayed += 1
        p = ev.payload if isinstance(ev.payload, dict) else {}
        a = ev.action
        if a == "scheduler.enqueue":
            _sub_enqueue(ev, p, out)
        elif a == "scheduler.transition":
            _sub_job_transition(ev, p, out)
        elif a == "scheduler.lease_renew":
            _sub_lease_renew(ev, p, out)
        elif a == "scheduler.priority":
            _sub_priority(ev, p, out)
        elif a == "policy.publish":
            _sub_policy_publish(ev, p, out)
        elif a == "policy.decision":
            _sub_policy_decision(ev, p, out)
        elif a == "capability.root":
            _sub_capability_root(ev, p, out)
        elif a == "capability.issue":
            _sub_capability_issue(ev, p, out)
        elif a == "capability.revoke":
            _sub_capability_revoke(ev, p, out)
        elif a == "agent.register":
            _sub_agent_register(ev, p, out)
        elif a == "agent.retire":
            _sub_agent_retire(ev, p, out)
        elif a == "agent.escalation":
            _sub_escalation(ev, p, out)
        elif a == "agent.escalation.answer":
            _sub_escalation_answer(ev, p, out)
        elif a == "memory.write":
            _sub_memory_write(ev, p, out)
        elif a == "memory.status":
            _sub_memory_status(ev, p, out)
        elif a == "network.service":
            _sub_service(ev, p, out)
        elif a == "network.request":
            _sub_service_call(ev, p, out)
        elif a == "network.grant":
            _sub_grant(ev, p, out, out.net_grants, "network")
        elif a == "secret.grant":
            _sub_grant(ev, p, out, out.secret_grants, "secret")
        elif a == "context.build":
            _sub_context(ev, p, out)
    return out


def _note(out, ev, text: str) -> None:
    out.anomalies.append(f"seq {ev.seq}: {text}")


def _sub_enqueue(ev, p: dict, out) -> None:
    job = p.get("job")
    if not isinstance(job, dict):
        _note(out, ev, "enqueue carries no job record")
        return
    jid = job.get("job_id")
    if not isinstance(jid, str) or not jid:
        _note(out, ev, "enqueue names no job_id")
        return
    if jid in out.jobs:
        _note(out, ev, f"job {jid!r} enqueued twice; the second would "
                       "replace the first one's state and history")
        return
    state = job.get("state")
    if state not in _JOB_INITIAL:
        # A create introduces WORK, never a verdict. A job born SUCCEEDED
        # was never run; one born DISPATCHED arrives holding the lease that
        # the ownership check on its outcome edges would otherwise demand.
        _note(out, ev, f"job {jid!r} is enqueued directly in {state!r}; an "
                       "enqueue introduces work, not an outcome")
        return
    if job.get("submitter") != ev.actor:
        _note(out, ev, f"job {jid!r} names submitter "
                       f"{job.get('submitter')!r} but was appended by "
                       f"{ev.actor!r}")
        return
    if job.get("attempts"):
        _note(out, ev, f"job {jid!r} is enqueued with "
                       f"{job.get('attempts')} attempts already spent")
        return
    if job.get("lease_holder") or job.get("lease_id"):
        _note(out, ev, f"job {jid!r} is enqueued already holding a lease")
        return
    # The retry budget arrives here or nowhere. A job whose enqueue does not
    # state a whole, positive bound has no bound this reader can hold it to,
    # and saying so is better than quietly substituting a default that the
    # scheduler happens to use today.
    budget = job.get("max_attempts")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        _note(out, ev, f"job {jid!r} is enqueued with a retry budget of "
                       f"{budget!r}; the count has nothing to be measured "
                       "against")
        budget = None
    out.jobs[jid] = {
        "job_id": jid, "state": state,
        "work_digest": job.get("work_digest"),
        "submitter": ev.actor, "priority": job.get("priority"),
        "attempts": job.get("attempts") or 0,
        "max_attempts": budget,
        "lease_holder": job.get("lease_holder") or "",
        "lease_expires_after_seq": job.get("lease_expires_after_seq", -1),
        "idempotency_key": job.get("idempotency_key"),
        "enqueued_seq": ev.seq,
    }


def _sub_job_transition(ev, p: dict, out) -> None:
    jid = p.get("job_id")
    cur = out.jobs.get(jid)
    if cur is None:
        _note(out, ev, f"transition for unknown job {jid!r}")
        return
    src, dst = p.get("src"), p.get("dst")
    if cur["state"] != src:
        _note(out, ev, f"job {jid!r} claims src {src!r} but replay has it "
                       f"in {cur['state']!r}")
        return
    if cur["state"] in _JOB_TERMINAL:
        _note(out, ev, f"job {jid!r} leaves terminal state {src!r}")
        return

    # WHO IS ALLOWED TO SAY THIS, AND WHAT MAY IT SAY ABOUT THE BUDGET.
    #
    # Everything above this point checks the SHAPE of the move: that the
    # job exists, that it is where the record says it is, that it is not
    # coming back from the dead. None of that asks who wrote the record or
    # what the record does to the retry count -- so a hand-written line
    # naming a job's outcome on behalf of a worker that never reported,
    # or one that quietly sets attempts back to zero, replayed clean here
    # and this reader said it had no findings.
    #
    # That is worse than it sounds. The scheduler's own reducer refuses all
    # of these, so a log carrying one cannot be loaded by the primary at
    # all -- which means the comparison of the two readers never runs, and
    # THIS reader is the only one left looking at the history. A second
    # opinion that accepts a wider language than the first is not a second
    # opinion on the logs that matter.
    #
    # Restated below in this module's own terms, from the event header and
    # the state this replay has built, never from the payload's own claims.
    end = cur.get("lease_expires_after_seq", -1)
    # The writer decided at the head and its record landed one past it, so
    # possession is judged at the position the decision was taken from.
    # Judging it at ev.seq would reject a report written at the last legal
    # moment.
    holds = bool(cur.get("lease_id")) and isinstance(end, int) and (
        (ev.seq - 1) <= end)
    grants = bool(p.get("lease_holder")) or bool(p.get("lease_id"))

    # Two ways out of DISPATCHED are nobody's to sign but the supervisor's,
    # because the party who ought to sign them is the worker that stopped
    # answering: the work goes back on the queue, or the queue gives up on
    # it. They are legitimate exactly when the thing they assert is true --
    # that possession has run out -- and when they hand ownership to no one.
    handover = (src == "DISPATCHED" and dst in {"READY", "FAILED"}
                and not holds and not grants)
    if src == "DISPATCHED" and dst in _JOB_OUTCOME and not handover:
        if cur.get("lease_holder") != ev.actor:
            _note(out, ev, f"job {jid!r} is held by "
                           f"{cur.get('lease_holder')!r} and {ev.actor!r} "
                           f"records its outcome as {dst!r}; an attempt is "
                           "answered for by whoever was running it")
            return
        if not holds:
            _note(out, ev, f"job {jid!r} had possession only to seq {end!r} "
                           f"and the log is at {ev.seq}; an outcome arriving "
                           "after that decides work somebody else may "
                           "already have redone")
            return
    if (src == "DISPATCHED" and dst in {"READY", "FAILED"}
            and cur.get("lease_holder") != ev.actor and holds):
        _note(out, ev, f"job {jid!r} is taken back as though possession had "
                       f"run out, but it runs to seq {end!r}; the same work "
                       "would be handed to a second worker")
        return
    if "attempts" in p:
        want = p.get("attempts")
        # A retry is counted where one is handed out, and nowhere else. Any
        # other record that names the count is rewriting the only number
        # the budget is measured against.
        charge = src == "READY" and dst == "DISPATCHED"
        have = cur.get("attempts") or 0
        allowed = have + 1 if charge else have
        if want != allowed:
            _note(out, ev, f"job {jid!r} has {have} attempt(s) and this "
                           f"record sets {want!r}; only the hand-out edge "
                           "moves that count, and only by one")
            return
    if grants and not (src == "READY" and dst == "DISPATCHED"):
        _note(out, ev, f"job {jid!r} {src!r} -> {dst!r} carries possession; "
                       "it is taken when the work is handed out and dropped "
                       "on the way back, never granted in passing")
        return

    cur["state"] = dst
    if "lease_holder" in p:
        cur["lease_holder"] = p.get("lease_holder") or ""
    if "lease_id" in p:
        cur["lease_id"] = p.get("lease_id") or ""
    if "lease_expires_after_seq" in p:
        cur["lease_expires_after_seq"] = p.get("lease_expires_after_seq", -1)
    if "lease_renewals" in p:
        cur["lease_renewals"] = p.get("lease_renewals", 0)
    if "attempts" in p:
        cur["attempts"] = p.get("attempts")
    if dst in _JOB_TERMINAL or dst in _JOB_INITIAL or dst in _JOB_PENDING:
        # Leaving DISPATCHED drops the lease AND its renewal budget. A count
        # carried across would limit the next worker for reasons belonging to
        # a lease that no longer exists.
        cur["lease_id"] = ""
        cur["lease_holder"] = ""
        cur["lease_expires_after_seq"] = -1
        cur["lease_renewals"] = 0
    # The bound the count exists to be measured against. Checked here rather
    # than only on the hand-out edge so that a budget overrun is a finding
    # about the HISTORY, whatever combination of records produced it.
    budget = cur.get("max_attempts")
    if isinstance(budget, int) and (cur.get("attempts") or 0) > budget:
        _note(out, ev, f"job {jid!r} has spent {cur.get('attempts')} "
                       f"attempt(s) against a budget of {budget}")


#: A lease may be extended this many times before the work has to be
#: reconsidered. Restated rather than imported: if the scheduler's bound and
#: this one drifted apart, the divergence is the finding.
_MAX_LEASE_RENEWALS = 16


def _sub_lease_renew(ev, p: dict, out) -> None:
    """Extend a live lease, in this reader's own words.

    The rule this exists to check independently is that a renewal EXTENDS
    possession and never re-acquires it. A lapsed lease may already have been
    reconciled away and the work given to somebody else, so renewing one
    would restore ownership the scheduler had taken -- and the job record
    would look ordinary afterwards.

    The new end is computed from ``ev.seq``, never read from the payload, for
    the same reason a capability may not name its own issued_seq.
    """
    jid = p.get("job_id")
    cur = out.jobs.get(jid)
    if cur is None:
        _note(out, ev, f"lease renewal for unknown job {jid!r}")
        return
    if cur["state"] != "DISPATCHED":
        _note(out, ev, f"job {jid!r} is {cur['state']!r}; only a DISPATCHED "
                       "job holds a lease that could be renewed")
        return
    if cur.get("lease_holder") != ev.actor:
        _note(out, ev, f"{ev.actor!r} renews a lease held by "
                       f"{cur.get('lease_holder')!r}")
        return
    if p.get("lease_id") != cur.get("lease_id"):
        _note(out, ev, f"renewal cites lease {p.get('lease_id')!r} and job "
                       f"{jid!r} holds {cur.get('lease_id')!r}")
        return
    end = cur.get("lease_expires_after_seq")
    if not isinstance(end, int) or ev.seq > end:
        _note(out, ev, f"lease on {jid!r} lapsed after {end!r} and the log is "
                       f"at {ev.seq}; a lapsed lease is lost, not renewed")
        return
    if cur.get("lease_renewals", 0) >= _MAX_LEASE_RENEWALS:
        _note(out, ev, f"lease on {jid!r} is at the "
                       f"{_MAX_LEASE_RENEWALS}-renewal bound")
        return
    seqs = p.get("lease_seqs")
    if not isinstance(seqs, int) or isinstance(seqs, bool) or seqs < 1:
        _note(out, ev, f"lease_seqs on {jid!r} is {seqs!r}")
        return
    new_end = ev.seq + seqs
    if new_end <= end:
        _note(out, ev, f"renewing {jid!r} to {new_end} would not extend a "
                       f"lease running to {end}")
        return
    cur["lease_expires_after_seq"] = new_end
    cur["lease_renewals"] = cur.get("lease_renewals", 0) + 1


def _sub_priority(ev, p: dict, out) -> None:
    jid = p.get("job_id")
    cur = out.jobs.get(jid)
    if cur is None:
        _note(out, ev, f"priority change for unknown job {jid!r}")
        return
    cur["priority"] = p.get("priority")


def _sub_policy_publish(ev, p: dict, out) -> None:
    doc = p.get("document")
    if not isinstance(doc, dict):
        _note(out, ev, "policy.publish carries no document")
        return
    pid = doc.get("policy_id")
    versions = out.policies.setdefault(pid, [])
    version = doc.get("version")
    if any(v["version"] == version for v in versions):
        _note(out, ev, f"policy {pid!r} publishes version {version!r} twice")
        return
    if versions and version is not None and \
            versions[-1]["version"] is not None and \
            version < versions[-1]["version"]:
        # A downgrade republished later would answer questions about the
        # intervening range with rules that were superseded.
        _note(out, ev, f"policy {pid!r} publishes version {version!r} after "
                       f"{versions[-1]['version']!r}")
        return
    versions.append({"version": version, "digest": p.get("policy_digest"),
                     "effective_seq": ev.seq})


def _sub_policy_decision(ev, p: dict, out) -> None:
    d = p.get("decision")
    if not isinstance(d, dict):
        _note(out, ev, "policy.decision carries no decision")
        return
    out.decisions[ev.seq] = {
        "allowed": d.get("allowed"), "policy_id": d.get("policy_id"),
        "policy_digest": d.get("policy_digest"), "actor": ev.actor,
        "subject": d.get("subject"), "action": d.get("action"),
    }


def _sub_capability_root(ev, p: dict, out) -> None:
    """Who may mint. Restated: the writer of the record IS the root.

    A record anointing a third party, or a second root after a first, is the
    forgery this reducer exists to notice independently of the ledger that
    would also notice it.
    """
    issuer = p.get("issuer")
    if not isinstance(issuer, str) or not issuer:
        _note(out, ev, "capability.root names no issuer")
        return
    if issuer != ev.actor:
        _note(out, ev, f"{ev.actor!r} anoints {issuer!r} as root issuer; "
                       "the root is whoever establishes it, and nominating "
                       "somebody else is a delegation wearing a root's name")
        return
    if out.root_issuer is not None and out.root_issuer != issuer:
        _note(out, ev, f"{issuer!r} claims root issuer, but "
                       f"{out.root_issuer!r} already holds it; a log with "
                       "two roots has no root")
        return
    out.root_issuer = issuer


def _sub_covers(scope, rel: str) -> bool:
    """Does one of ``scope``'s prefixes cover ``rel``, by path components?

    Restated rather than imported. A string prefix test would say
    ``a/stage10x`` is under ``a/stage10``, and this reducer exists so that a
    mistake in the capability module is visible rather than shared.
    """
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        return False
    target = tuple(x for x in rel.split("/") if x)
    if any(x in ("..", ".") for x in target):
        return False
    for allowed in scope or ():
        if not isinstance(allowed, str):
            return False
        a = tuple(x for x in allowed.split("/") if x)
        if a and target[:len(a)] == a:
            return True
    return False


def _sub_attenuates(child: dict, parent: dict) -> str:
    """"" when ``child`` is no wider than ``parent``, else why it is wider."""
    if child.get("action") != parent.get("action"):
        return (f"delegation grants {child.get('action')!r} from a parent "
                f"granting {parent.get('action')!r}")
    if child.get("task_id") != parent.get("task_id"):
        return (f"delegation is for task {child.get('task_id')!r} from a "
                f"parent confined to {parent.get('task_id')!r}")
    if (child.get("tool_id") or "") != (parent.get("tool_id") or ""):
        return (f"delegation names tool {child.get('tool_id')!r}, its parent "
                f"names {parent.get('tool_id')!r}")
    outside = [x for x in (child.get("scope") or ())
               if not _sub_covers(parent.get("scope"), x)]
    if outside:
        return (f"delegation covers {sorted(outside)}, outside its parent's "
                f"scope {list(parent.get('scope') or ())}")
    pexp = parent.get("expires_after_seq")
    cexp = child.get("expires_after_seq")
    if pexp != -1:
        if cexp == -1 or (isinstance(cexp, int) and isinstance(pexp, int)
                          and cexp > pexp):
            return (f"delegation expires after {cexp}, its parent after "
                    f"{pexp}; a child cannot outlive its source")
    ciss, piss = child.get("issued_seq"), parent.get("issued_seq")
    if isinstance(ciss, int) and isinstance(piss, int) and ciss < piss:
        return (f"delegation is issued at {ciss}, before its parent at "
                f"{piss}")
    return ""


def _sub_capability_issue(ev, p: dict, out) -> None:
    cid = p.get("capability_id")
    if not isinstance(cid, str) or not cid:
        _note(out, ev, "capability.issue names no capability_id")
        return
    issued = p.get("issued_seq")
    prior = out.capabilities.get(cid)
    if prior is not None:
        if prior["body"] == {k: v for k, v in p.items()
                             if k != "task_id"}:
            return                          # a retried append of the same
        _note(out, ev, f"capability {cid!r} is issued twice with different "
                       "terms; two grants sharing an id cannot be told apart")
        return
    if issued != ev.seq:
        # WHERE a grant starts is the log's to say. One appended at seq 90
        # claiming seq 5 reads as authority in force for 5..89.
        _note(out, ev, f"capability {cid!r} claims issued_seq {issued!r} at "
                       f"seq {ev.seq}; it would predate its own record")
        return
    cap = {
        "capability_id": cid, "subject": p.get("subject"),
        "action": p.get("action"), "task_id": p.get("task_id"),
        "tool_id": p.get("tool_id"), "scope": tuple(p.get("scope") or ()),
        "issued_seq": issued,
        "expires_after_seq": p.get("expires_after_seq"),
        # Who minted it. Kept out of "body" so the grant's identity does not
        # depend on who recorded it -- and kept at all so a revocation has
        # something to be checked against.
        "issued_by": ev.actor,
        "parent_id": p.get("parent_id") or "",
        "revoked_seq": None,
        "body": {k: v for k, v in p.items() if k != "task_id"},
    }
    # MAY THIS ACTOR MINT? Restated from the same three cases the ledger
    # applies, and reached without asking it. A grant nobody was authorized
    # to create is not recorded here at all, so a divergence between the two
    # readers shows up as a missing capability rather than as agreement.
    if out.root_issuer is None:
        _note(out, ev, f"capability {cid!r} is minted with no root issuer "
                       "established; authority answering to nobody")
        return
    parent_id = cap["parent_id"]
    if parent_id:
        parent = out.capabilities.get(parent_id)
        if parent is None:
            _note(out, ev, f"capability {cid!r} is delegated from "
                           f"{parent_id!r}, which this log has not issued")
            return
        if ev.actor != parent.get("subject"):
            _note(out, ev, f"{ev.actor!r} delegated {parent_id!r}, granted to "
                           f"{parent.get('subject')!r}; delegating a grant "
                           "you do not hold is minting")
            return
        why = _sub_attenuates(cap, parent)
        if why:
            _note(out, ev, why)
            return
    elif ev.actor != out.root_issuer:
        _note(out, ev, f"{ev.actor!r} minted {cid!r}; the root issuer is "
                       f"{out.root_issuer!r}")
        return
    out.capabilities[cid] = cap


def _sub_capability_revoke(ev, p: dict, out) -> None:
    """Withdraw a grant, checking who is entitled to.

    The mint side of this reader has always asked whether the actor could
    create the grant. Nothing asked whether it could destroy one, so an
    arbitrary actor could revoke authority the root issued and this reader
    reported no finding.
    """
    cid = p.get("capability_id")
    cur = out.capabilities.get(cid)
    if cur is None:
        _note(out, ev, f"revoke for unknown capability {cid!r}")
        return
    if ev.actor != cur.get("issued_by") and ev.actor != out.root_issuer:
        _note(out, ev, f"{ev.actor!r} revokes {cid!r}, granted by "
                       f"{cur.get('issued_by')!r}; a grant is withdrawn by "
                       f"whoever made it or by the root issuer "
                       f"({out.root_issuer!r})")
        return
    if cur["revoked_seq"] is None:
        cur["revoked_seq"] = ev.seq


def _sub_agent_register(ev, p: dict, out) -> None:
    ident = p.get("identity")
    if not isinstance(ident, dict):
        _note(out, ev, "agent.register carries no identity")
        return
    iid = ident.get("instance_id")
    if not isinstance(iid, str) or not iid:
        _note(out, ev, "agent.register names no instance_id")
        return
    if iid in out.agents:
        _note(out, ev, f"instance {iid!r} registered twice")
        return
    kind = ident.get("kind")
    by = ev.actor
    registrar = out.agents.get(by)
    if kind == "HUMAN" and registrar is not None and \
            registrar.get("kind") != "HUMAN":
        # An agent that can mint a HUMAN is one step from answering its own
        # escalation, which is both halves of the human gate at once.
        _note(out, ev, f"{by!r} is not HUMAN and registers {iid!r} as HUMAN")
        return
    out.agents[iid] = {
        "instance_id": iid, "agent_id": ident.get("agent_id"),
        "kind": kind, "roles": tuple(sorted(ident.get("roles") or ())),
        "registered_by": by, "registered_seq": ev.seq, "retired_seq": None,
    }


def _sub_agent_retire(ev, p: dict, out) -> None:
    """Remove a principal, checking who is entitled to.

    Admission was checked here -- a non-human registering a human is a
    finding. Removal was not checked at all, so anything could retire
    anybody, including every human in the log. Admission and removal are
    the same authority reached from two directions, and only one of them
    was guarded.
    """
    iid = p.get("instance_id")
    cur = out.agents.get(iid)
    if cur is None:
        _note(out, ev, f"retire for unknown instance {iid!r}")
        return
    by = ev.actor
    actor = out.agents.get(by)
    live_human = (actor is not None and actor.get("kind") == "HUMAN"
                  and actor.get("retired_seq") is None)
    if by != _BOOTSTRAP and by != iid:
        if cur.get("kind") == "HUMAN" and not live_human:
            _note(out, ev, f"{by!r} retires the HUMAN {iid!r} and is not an "
                           "active human itself; subtracting people is how "
                           "the set the human gate draws from is emptied")
            return
        if cur.get("registered_by") != by and not live_human:
            _note(out, ev, f"{by!r} retires {iid!r}, admitted by "
                           f"{cur.get('registered_by')!r}; a principal is "
                           "retired by whoever admitted it, by itself, or "
                           "by an active human")
            return
    if cur["retired_seq"] is None:
        cur["retired_seq"] = ev.seq


def _sub_escalation(ev, p: dict, out) -> None:
    """Open a question for a human, in this reader's own words.

    WHY THIS EXISTS. The mutation campaign was described as covering "the
    second reader for every subsystem" while escalations had no independent
    reconstruction at all. Those two cannot both be true, and the honest
    resolutions are to build the reader or to weaken the claim. This is the
    reader.

    It shares no code with AgentDirectory. Every rule below is restated from
    plain dictionaries, which is the whole value: two implementations can
    still share a misunderstanding, and one implementation cannot disagree
    with itself.
    """
    esc = p.get("escalation")
    if not isinstance(esc, dict):
        _note(out, ev, "agent.escalation carries no escalation")
        return
    eid = esc.get("escalation_id")
    if not isinstance(eid, str) or not eid:
        _note(out, ev, "agent.escalation names no escalation_id")
        return
    if eid in out.escalations:
        _note(out, ev, f"escalation {eid!r} raised twice; the second record "
                       "would replace a question somebody may already have "
                       "answered")
        return
    # WHO RAISED IT IS THE EVENT'S ACTOR. The raiser may not answer their own
    # escalation, so a forged raiser is not only false attribution -- it is a
    # way to stop the named party answering.
    if esc.get("raised_by") != ev.actor:
        _note(out, ev, f"escalation {eid!r} says it was raised by "
                       f"{esc.get('raised_by')!r} and was appended by "
                       f"{ev.actor!r}; a question is attributed to whoever "
                       "asked it")
        return
    state = esc.get("state")
    if state != _ESC_OPEN:
        # An escalation is a question. One born ANSWERED was never asked,
        # and carries a decision nobody is recorded as having made.
        _note(out, ev, f"escalation {eid!r} is raised directly in "
                       f"{state!r}; raising a question is not answering it")
        return
    options = esc.get("options")
    if not isinstance(options, (list, tuple)):
        _note(out, ev, f"escalation {eid!r} offers {type(options).__name__} "
                       "rather than a list of options")
        return
    if len(set(options)) < _ESC_MIN_OPTIONS:
        _note(out, ev, f"escalation {eid!r} offers "
                       f"{len(set(options))} distinct option(s); an answer "
                       "that cannot be checked against what was asked is a "
                       "conversation, not a decision")
        return
    if not str(esc.get("question") or "").strip():
        _note(out, ev, f"escalation {eid!r} asks nothing")
        return
    if esc.get("answer") is not None or esc.get("answered_by") is not None:
        _note(out, ev, f"escalation {eid!r} is raised already carrying an "
                       "answer")
        return
    out.escalations[eid] = {
        "escalation_id": eid, "task_id": esc.get("task_id"),
        "raised_by": ev.actor, "state": _ESC_OPEN,
        "options": tuple(options), "answer": None, "answered_by": None,
        "raised_seq": ev.seq, "answered_seq": None,
    }


def _sub_escalation_answer(ev, p: dict, out) -> None:
    """Answer or withdraw, checking who is entitled to which."""
    eid = p.get("escalation_id")
    cur = out.escalations.get(eid)
    if cur is None:
        _note(out, ev, f"answer for escalation {eid!r}, which this history "
                       "never opened")
        return
    if cur["state"] != _ESC_OPEN:
        _note(out, ev, f"escalation {eid!r} is {cur['state']!r}; deciding it "
                       "again would rewrite a decision already recorded")
        return
    dst = p.get("state")
    if dst not in _ESC_TERMINAL:
        _note(out, ev, f"escalation {eid!r} is moved to {dst!r}, which is "
                       "neither answered nor withdrawn")
        return

    if dst == "WITHDRAWN":
        # A withdrawal is a different act from an answer: the ASKER says they
        # no longer need the decision, and no human is claimed to have made
        # one. Only the asker may do it -- otherwise a third party can retire
        # a question somebody was waiting on.
        if ev.actor != cur["raised_by"]:
            _note(out, ev, f"escalation {eid!r} was raised by "
                           f"{cur['raised_by']!r} and {ev.actor!r} withdraws "
                           "it; retiring somebody else's question is not "
                           "theirs to do")
            return
        cur["state"] = "WITHDRAWN"
        cur["answered_seq"] = ev.seq
        return

    answered_by = p.get("answered_by")
    # The payload names the decider; the header names the writer. Every check
    # below interrogates `answered_by`, so without this they all pass on a
    # borrowed name.
    if answered_by != ev.actor:
        _note(out, ev, f"escalation {eid!r} names {answered_by!r} as its "
                       f"answerer and was appended by {ev.actor!r}; an agent "
                       "that can write a person's name can sign their "
                       "decision")
        return
    ident = out.agents.get(answered_by)
    if ident is None:
        _note(out, ev, f"{answered_by!r} answered escalation {eid!r} and is "
                       "not a registered principal")
        return
    if ident.get("retired_seq") is not None:
        _note(out, ev, f"{answered_by!r} was retired after seq "
                       f"{ident['retired_seq']} and may not answer "
                       f"{eid!r}; a principal that has left cannot be the "
                       "person a decision is attributed to")
        return
    if ident.get("kind") != "HUMAN":
        # The strongest claim the directory makes, restated independently:
        # an escalation exists because the decision was not the agent's to
        # make, and no arrangement of roles substitutes for a person.
        _note(out, ev, f"{answered_by!r} is a {ident.get('kind')!r} "
                       f"principal and may not answer {eid!r}; holding a "
                       "role does not change what kind of thing is deciding")
        return
    if answered_by == cur["raised_by"]:
        _note(out, ev, f"{answered_by!r} raised escalation {eid!r} and may "
                       "not also answer it")
        return
    if p.get("answer") not in cur["options"]:
        _note(out, ev, f"answer {p.get('answer')!r} to {eid!r} is not one of "
                       f"{list(cur['options'])}")
        return
    cur["state"] = "ANSWERED"
    cur["answer"] = p.get("answer")
    cur["answered_by"] = answered_by
    cur["answered_seq"] = ev.seq


def _sub_memory_write(ev, p: dict, out) -> None:
    entry = p.get("entry")
    if not isinstance(entry, dict):
        _note(out, ev, "memory.write carries no entry; a record this reader "
                       "cannot read is refused rather than projected")
        return
    mid = entry.get("memory_id")
    if not isinstance(mid, str) or not mid:
        _note(out, ev, "memory.write names no memory_id")
        return
    if mid in out.memory:
        _note(out, ev, f"memory {mid!r} written twice")
        return
    if entry.get("author") != ev.actor:
        _note(out, ev, f"memory {mid!r} names author {entry.get('author')!r} "
                       f"but was appended by {ev.actor!r}")
        return
    out.memory[mid] = {"memory_id": mid, "author": ev.actor,
                       "status": entry.get("status") or "ACTIVE",
                       "written_seq": ev.seq}


def _sub_memory_status(ev, p: dict, out) -> None:
    mid = p.get("memory_id")
    cur = out.memory.get(mid)
    if cur is None:
        _note(out, ev, f"status change for unknown memory {mid!r}")
        return
    new = p.get("status")
    if cur["status"] == "RETRACTED" and new != "RETRACTED":
        # A withdrawn note that can be un-withdrawn is a note whose author
        # never really withdrew it.
        _note(out, ev, f"memory {mid!r} is un-retracted to {new!r}")
        return
    cur["status"] = new


def _sub_grant(ev, p: dict, out, table: dict, what: str) -> None:
    if p.get("revoke"):
        gid = p.get("grant_id")
        cur = table.get(gid)
        if cur is None:
            _note(out, ev, f"revoke for unknown {what} grant {gid!r}")
            return
        # WHO MAY TAKE IT AWAY. Issuing a grant was checked here and
        # withdrawing one was not, in this reader and in both of the
        # ledgers it reads after -- so one appended line removed authority
        # somebody else granted, and every reader agreed it was gone,
        # because a revocation is exactly as durable as a grant.
        #
        # This matters more for secrets than anywhere else: that store has
        # no reducer at all, so a secret.grant revocation appended around
        # its write path was re-read by NOTHING except this function. It has
        # one now -- the store reconstructs grant authority from the log and
        # refuses a forged revocation itself -- so this is a second opinion
        # again rather than the only one. The rule stays here because that is
        # what a second opinion is.
        if ev.actor != cur.get("granted_by"):
            _note(out, ev, f"{ev.actor!r} revokes {what} grant {gid!r}, "
                           f"granted by {cur.get('granted_by')!r}; a grant "
                           "is withdrawn by whoever made it")
            return
        if cur["revoked_seq"] is None:
            cur["revoked_seq"] = ev.seq
        return
    grant = p.get("grant")
    if not isinstance(grant, dict):
        _note(out, ev, f"{what}.grant carries no grant body")
        return
    # WHERE A GRANT STARTS IS THE LOG'S TO SAY.
    #
    # Restated here for secrets in particular: that store had no reducer at
    # all until recently, so for a long time this reader was the ONLY thing
    # that would have looked at a secret.grant record. It is a second opinion
    # again rather than the sole one, and the rule belongs in both.
    issued = grant.get("issued_seq")
    if issued is not None and issued != ev.seq:
        _note(out, ev, f"{what} grant {grant.get('grant_id')!r} claims "
                       f"issued_seq {issued!r} at seq {ev.seq}; it would "
                       "predate its own record")
        return
    gid = grant.get("grant_id") or p.get("grant_id")
    if not isinstance(gid, str) or not gid:
        _note(out, ev, f"{what} grant names no grant_id")
        return
    prior = table.get(gid)
    if prior is not None:
        if prior["digest"] == p.get("grant_digest"):
            return                          # a retried append of the same
        _note(out, ev, f"{what} grant {gid!r} is re-issued with different "
                       "terms; the live grant would be replaced by one "
                       "nobody reviewed")
        return
    table[gid] = {"grant_id": gid, "digest": p.get("grant_digest"),
                  "issued_seq": ev.seq, "revoked_seq": None,
                  "granted_by": ev.actor, "body": grant}


def _sub_service(ev, p: dict, out) -> None:
    """A service contract, in this reader's own words.

    The rule restated is that a contract in force may not be replaced by one
    nobody reviewed: every decision recorded under the old terms would
    afterwards read as though it had been made under the new.
    """
    rec = p.get("service")
    if not isinstance(rec, dict):
        _note(out, ev, "network.service carries no service")
        return
    sid = rec.get("service_id")
    if not isinstance(sid, str) or not sid:
        _note(out, ev, "network.service names no service_id")
        return
    prior = out.services.get(sid)
    if prior is not None:
        if prior["digest"] != p.get("service_digest"):
            _note(out, ev, f"service {sid!r} is registered again with "
                           "different terms; the contract in force would be "
                           "replaced by one nobody reviewed")
        return
    out.services[sid] = {
        "service_id": sid, "digest": p.get("service_digest"),
        "hosts": tuple(rec.get("hosts") or ()),
        "quota_per_task": rec.get("quota_per_task"),
        "operations": tuple(sorted(
            f"{o.get('method')} {o.get('path')}"
            for o in (rec.get("operations") or [])
            if isinstance(o, dict))),
        "registered_seq": ev.seq,
    }


def _sub_service_call(ev, p: dict, out) -> None:
    """Count a PERMITTED call against a service's per-task budget.

    Counted here rather than trusted from the primary, so a budget the
    scheduler believes is spent and one the log actually shows are two
    numbers that can be compared. Only ALLOWED calls count: a refusal cost
    the caller nothing and must not cost it a call it never made.
    """
    sid, task = p.get("service_id"), p.get("task_id")
    if not sid or not p.get("allowed"):
        return
    key = f"{sid}/{task}"
    cur = out.service_calls.get(key, 0)
    out.service_calls[key] = cur + 1
    svc = out.services.get(sid)
    if svc is not None and isinstance(svc.get("quota_per_task"), int) \
            and out.service_calls[key] > svc["quota_per_task"]:
        _note(out, ev, f"service {sid!r} has now been called "
                       f"{out.service_calls[key]} time(s) for task {task!r}, "
                       f"past its {svc['quota_per_task']}-call budget")


def _sub_context(ev, p: dict, out) -> None:
    manifest = p.get("manifest")
    digest_ = p.get("manifest_digest")
    task = (manifest or {}).get("task_id") if isinstance(manifest, dict) \
        else None
    out.contexts.setdefault(task or ev.target, []).append(
        {"digest": digest_, "seq": ev.seq})


def compare_subsystems(primary: dict, recon: SubsystemReconstruction) -> tuple:
    """Divergences between a primary projection and the second reader.

    ``primary`` is a mapping of subsystem name -> {id: {field: value}},
    extracted by the caller from the live projections. The extraction is
    the caller's because reconstruct.py may not import those layers, which
    is what keeps the two readers independent in the first place.
    """
    out: list = []
    tables = {"jobs": recon.jobs, "capabilities": recon.capabilities,
              "agents": recon.agents, "memory": recon.memory,
              "net_grants": recon.net_grants, "services": recon.services,
              "secret_grants": recon.secret_grants,
              "escalations": recon.escalations}
    for name, theirs in tables.items():
        mine = primary.get(name)
        if mine is None:
            continue
        for key in sorted(set(mine) | set(theirs)):
            a, b = mine.get(key), theirs.get(key)
            if a is None:
                out.append(Divergence(f"{name}/{key}", "presence", None,
                                      "present in the second reader"))
                continue
            if b is None:
                out.append(Divergence(f"{name}/{key}", "presence",
                                      "present in the projection", None))
                continue
            for fld, want in sorted(a.items()):
                got = b.get(fld)
                if got != want:
                    out.append(Divergence(f"{name}/{key}", fld, want, got))
    return tuple(out)


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
    """The lease this replay is currently holding, if any.

    Plain fields rather than a :class:`~qta_agent.tasks.Lease`: constructing
    the production dataclass here would import the layer this reader exists
    to second-guess, and would inherit its idea of what a lease record even
    looks like.
    """
    return _parse_lease(cur.get("lease"))


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
