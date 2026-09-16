"""Audit queries: turning a log into answers.

WHY THIS IS NOT "JUST READ THE JSONL"

Recording everything is worthless if the only way to use it is to read it. The
questions that matter after an incident -- why is this canonical, what produced
these bytes, who authorized it, what did it depend on -- each require joining
several record types in a particular order, and a person doing that by hand at
2am will get it wrong. Worse, they will get it *plausibly* wrong, and a
plausible reconstruction is more dangerous than no reconstruction.

So the joins live here, once, and are tested.

THE COMPLETENESS CHECK IS THE INTERESTING PART

:meth:`AuditIndex.explain_task` reconstructs a chain. :meth:`gaps` asks the
harder question: is the chain COMPLETE? A task that reached VERIFIED must have
an execution record, an evidence record, and a capability issued for it. If any
is missing, the state is not wrong exactly -- the state machine permitted every
transition it saw -- but the provenance has a hole, and a hole in provenance is
indistinguishable from a fabrication that nobody happened to notice.

Finding that gap is a different job from enforcing the transition, and it is
one only a reader of the whole history can do.

THE SAME QUESTION, ASKED OF AUTHORITY RECORDS

:meth:`AuditIndex.explain_record` is the twin of :meth:`explain_task` over
``record.*``. It matters more, not less: a task chain describes work, an
authority chain describes what the project treats as CANONICAL. The store
applies one event at a time and never looks backwards, so three holes are
invisible to it and visible here --

  * a transition whose ``src`` is not the previous transition's ``dst``,
    which means the history was not written through :class:`AuthorityStore`
    at all (the store reads current state, so it cannot produce one);
  * an edge the state machine does not have, or one taken by the record's own
    proposer where separation of duties is required;
  * a record still PROMOTED whose dependency is no longer canonical. Nothing
    in ``store.py`` watches dependents; :mod:`qta_agent.invalidation` cascades
    only when a caller runs it, so a cascade that was never run leaves
    canonical authority resting on withdrawn foundations and no single
    transition is wrong.

POLICY DECISIONS ARE A QUERY, NOT A CHAIN

:meth:`decisions` and :meth:`denials` answer "what did this subject try, and
what refused it". ``PolicyStore.decide_and_record`` records denials precisely
so that question has an answer; leaving the records unqueryable would have
made recording them ceremony.

REDACTION

Audit output is meant to be read, pasted into incident notes, and attached to
reports. Anything that looks like a credential is replaced before it leaves
this module. That is a coarse net over a surface that should not contain
secrets in the first place -- the executor replaces the child's environment
rather than inheriting it, and raw tool output never enters the log -- so this
is the second line, not the first.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .authority import CANONICAL, INITIAL, State, find_edge
from .canonical import is_digest

#: Patterns that must never leave this module intact. Deliberately coarse: a
#: false positive costs a redacted string in a report, a false negative costs a
#: leaked credential in one.
#: Matched against a key whose separators have already been flattened to
#: spaces, so the alternatives below use " ?" rather than "[_-]?" -- writing
#: both forms would be two rules to keep in step and one of them would rot.
_SECRET_HINTS = re.compile(
    r"(?i)\b(token|secret|password|passwd|api ?key|authorization|bearer|"
    r"private ?key|credential|passphrase|session ?id|cookie)\b")

REDACTED = "[REDACTED]"


def _looks_secret(key: object) -> bool:
    """Does this key name suggest a credential?

    Separators are normalised to spaces first. Without that, ``bearer_token``
    slips through: ``_`` is a word character, so ``\btoken\b`` finds no
    boundary inside it. A test caught that, and the same reasoning covers
    ``api-key``, ``client.secret`` and every other joined form.
    """
    flattened = re.sub(r"[_\-.:/]+", " ", str(key))
    return bool(_SECRET_HINTS.search(flattened))


def redact(value):
    """Recursively blank anything whose KEY suggests a credential.

    Keyed rather than value-matched on purpose. Matching values would mean
    guessing whether a 64-character string is a digest or a token, and this
    package is full of digests that must stay readable.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = REDACTED if _looks_secret(k) else redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass(frozen=True)
class Step:
    """One link in a provenance chain, in terms a reader can act on."""

    seq: int
    when: float
    actor: str
    action: str
    summary: str
    detail: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        return {"seq": self.seq, "when": self.when, "actor": self.actor,
                "action": self.action, "summary": self.summary,
                "detail": redact(self.detail)}


@dataclass(frozen=True)
class Explanation:
    """The answer to 'why is this what it is'."""

    subject: str
    outcome: str
    steps: tuple
    gaps: tuple
    actors: tuple

    @property
    def complete(self) -> bool:
        """No missing links. NOT a claim that the outcome is correct."""
        return not self.gaps

    def to_record(self) -> dict:
        return {"subject": self.subject, "outcome": self.outcome,
                "complete": self.complete,
                "steps": [s.to_record() for s in self.steps],
                "gaps": list(self.gaps), "actors": list(self.actors)}

    def render(self) -> str:
        """A plain-text chain, for an incident note or a terminal."""
        lines = [f"{self.subject}: {self.outcome}"]
        for s in self.steps:
            lines.append(f"  seq {s.seq:<5} {s.actor:<18} {s.summary}")
        if self.gaps:
            lines.append("  PROVENANCE GAPS:")
            lines.extend(f"    - {g}" for g in self.gaps)
        return "\n".join(lines)


#: What a task in each terminal state must be able to show. A state reached
#: without its supporting records is a state whose provenance has a hole.
REQUIRED_RECORDS = {
    "VERIFIED": ("task.create", "capability.issue", "task.execution",
                 "task.evidence"),
    "COMPLETED": ("task.create", "capability.issue", "task.execution"),
    "FAILED": ("task.create", "task.execution"),
    "TIMED_OUT": ("task.create", "task.execution"),
}

#: Authority-record and policy action names, spelled once. They are strings
#: here rather than imports from every writer because an audit reads a
#: HISTORY: a record written by an older build must still be readable, and
#: binding these to whatever the current writer happens to call them would
#: make the reader silently skip records it should be explaining.
ACT_RECORD_CREATE = "record.create"
ACT_RECORD_TRANSITION = "record.transition"
ACT_RECORD_DEPEND = "record.depend"
ACT_POLICY_PUBLISH = "policy.publish"
ACT_POLICY_DECISION = "policy.decision"

_RECORD_ACTIONS = (ACT_RECORD_CREATE, ACT_RECORD_TRANSITION,
                   ACT_RECORD_DEPEND)

#: State names that carry canonical authority, as they appear in the log.
_CANONICAL_NAMES = frozenset(s.value for s in CANONICAL)


def _edge_for(src, dst):
    """The state-machine edge for two state NAMES, or None.

    Names, because this reads a log. A state a newer build introduced is not
    an edge this build can vouch for, and returning None makes the caller
    report that rather than crash on it.
    """
    try:
        return find_edge(State(src), State(dst))
    except ValueError:
        return None


class AuditIndex:
    """A queryable view over a verified log."""

    def __init__(self, events):
        self.events = list(events)
        #: ``(lo, hi)`` when this index covers only part of the log, else
        #: None. Carried so an answer over a slice can say it is one.
        self.window: tuple | None = None
        self._by_target: dict = {}
        self._by_actor: dict = {}
        for ev in self.events:
            self._by_target.setdefault(ev.target, []).append(ev)
            self._by_actor.setdefault(ev.actor, []).append(ev)

    @classmethod
    def from_log(cls, log, *, since_seq: int | None = None,
                 until_seq: int | None = None) -> "AuditIndex":
        """Build from a log, verifying it first. Fail closed.

        An audit over an unverified log answers questions about a document
        that may have been rewritten, which is worse than refusing: it
        produces a confident answer with no basis.

        A WINDOW IS NOT A SHORTCUT PAST VERIFICATION

        ``since_seq`` / ``until_seq`` narrow what the index HOLDS, and the
        whole log is still verified first. That ordering is the point: the
        cost this exists to bound is holding every event in memory, not
        checking the chain, and skipping the check to save time would make
        the window a way to audit a rewritten history cheaply.

        A windowed index says so. :attr:`window` is non-None, and every
        query that could mislead a reader into thinking they saw everything
        carries that with it -- see :meth:`window_note`.
        """
        log.verify().raise_if_bad()
        if since_seq is None and until_seq is None:
            return cls(log.read())
        lo = -1 if since_seq is None else int(since_seq)
        hi = None if until_seq is None else int(until_seq)
        if hi is not None and hi < lo:
            raise ValueError(
                f"window [{lo}, {hi}] ends before it starts; an empty window "
                "would answer every question with 'nothing happened'")
        kept = [ev for ev in log.read()
                if ev.seq >= lo and (hi is None or ev.seq <= hi)]
        idx = cls(kept)
        idx.window = (lo, hi)
        return idx

    def window_note(self) -> str:
        """"" for a full index; a sentence naming the window otherwise.

        Attached to anything a reader might mistake for a complete answer.
        An audit that quietly answers about a slice is worse than one that
        refuses, because the reader cannot tell.
        """
        if self.window is None:
            return ""
        lo, hi = self.window
        end = "end" if hi is None else hi
        return (f"NOTE: this index covers seq {lo}..{end} only; events "
                "outside that window were not read")

    # ---- time and position ranges --------------------------------------
    def between_seq(self, lo: int, hi: int) -> tuple:
        """Every event in a POSITION range, inclusive. The exact query.

        Position rather than wall time is the query that always means
        something: seq is the log's own order and two readers agree on it.
        """
        if hi < lo:
            raise ValueError(f"seq range [{lo}, {hi}] ends before it starts")
        return tuple(
            Step(ev.seq, ev.wall_time, ev.actor, ev.action,
                 f"{ev.action} on {ev.target}", dict(ev.payload))
            for ev in self.events if lo <= ev.seq <= hi)

    def between_wall(self, start: float, end: float) -> tuple:
        """Every event in a WALL-CLOCK range, inclusive of both ends.

        Offered because an incident arrives as a time, and refused as a
        basis for anything else: wall time on an event is what the writing
        machine's clock said, it is not ordered, and two events with the
        same stamp have no defined sequence. Use it to FIND a region and
        :meth:`between_seq` to reason about one.
        """
        if end < start:
            raise ValueError(
                f"wall range [{start}, {end}] ends before it starts")
        return tuple(
            Step(ev.seq, ev.wall_time, ev.actor, ev.action,
                 f"{ev.action} on {ev.target}", dict(ev.payload))
            for ev in self.events if start <= ev.wall_time <= end)

    # ---- cross-task ----------------------------------------------------
    def tasks_using_capability(self, capability_id: str) -> tuple:
        """Which tasks a grant was USED for, not merely issued against.

        Issuance names one task, and the question an incident asks is what
        the grant went on to touch. Read from the execution and read records
        that cite it rather than from the grant, because a grant describes
        an intention and those describe what happened.
        """
        out: dict = {}
        for ev in self.events:
            p = ev.payload if isinstance(ev.payload, dict) else {}
            cited = (p.get("capability_id")
                     or (p.get("capability") or {}).get("capability_id")
                     if isinstance(p.get("capability"), dict)
                     else p.get("capability_id"))
            if cited != capability_id:
                continue
            tid = p.get("task_id") or ev.target
            out.setdefault(tid, []).append(ev.seq)
        return tuple(sorted((tid, tuple(seqs)) for tid, seqs in out.items()))

    def decisions_by_policy_version(self, policy_id: str,
                                    version: int) -> tuple:
        """Everything ONE published version of a policy decided.

        The cross-task question the row named: "what did this policy version
        decide". Matching on the version rather than the id, because a policy
        that has been republished has decided different things under each.
        """
        return tuple(
            step for step in self.decisions(policy_id=policy_id)
            if step.detail.get("version") == version)

    def tasks_touched_by(self, actor: str) -> tuple:
        """Every task one actor appears in, with the actions it took there."""
        out: dict = {}
        for ev in self._by_actor.get(actor, ()):
            p = ev.payload if isinstance(ev.payload, dict) else {}
            tid = p.get("task_id") or ev.target
            out.setdefault(tid, set()).add(ev.action)
        return tuple(sorted((tid, tuple(sorted(acts)))
                            for tid, acts in out.items()))

    def tool_uses(self, tool_id: str) -> tuple:
        """Every execution of one tool, across every task."""
        out = []
        for ev in self.events:
            p = ev.payload if isinstance(ev.payload, dict) else {}
            if p.get("tool_id") != tool_id:
                continue
            out.append(Step(
                ev.seq, ev.wall_time, ev.actor, ev.action,
                f"{ev.action} of {tool_id} on "
                f"{p.get('task_id') or ev.target}", dict(p)))
        return tuple(out)

    # ---- queries -------------------------------------------------------
    def subjects(self) -> tuple:
        return tuple(sorted(self._by_target))

    def actors(self) -> tuple:
        return tuple(sorted(self._by_actor))

    def actions_by(self, actor: str) -> tuple:
        """Everything one actor did, in order. The 'who touched this' query."""
        return tuple(self._by_actor.get(actor, ()))

    def explain_task(self, task_id: str) -> Explanation:
        """Reconstruct why a task ended as it did, and say what is missing."""
        events = self._by_target.get(task_id, [])
        if not events:
            return Explanation(task_id, "UNKNOWN", (),
                               (f"no records for {task_id!r}",), ())

        steps = []
        outcome = "UNKNOWN"
        seen_actions = set()
        transitions: list = []
        for ev in events:
            seen_actions.add(ev.action)
            p = ev.payload
            if ev.action == "task.create":
                summary = (f"created for tool {p.get('tool_id')!r}, inputs "
                           f"{str(p.get('inputs_digest'))[:12]}")
                detail = {"tool_id": p.get("tool_id"),
                          "inputs_digest": p.get("inputs_digest")}
            elif ev.action == "task.transition":
                summary = (f"{p.get('src')} -> {p.get('dst')} "
                           f"as {p.get('role')}")
                if p.get("note"):
                    summary += f"  ({str(p['note'])[:80]})"
                outcome = p.get("dst", outcome)
                detail = {k: p.get(k) for k in
                          ("src", "dst", "role", "result_digest",
                           "executed_by")}
                transitions.append((ev.seq, p.get("src"), p.get("dst")))
            elif ev.action == "capability.issue":
                summary = (f"granted {p.get('action')} on tool "
                           f"{p.get('tool_id')!r} scoped to {p.get('scope')}, "
                           f"expiring after seq {p.get('expires_after_seq')}")
                detail = {k: p.get(k) for k in
                          ("capability_id", "action", "tool_id", "scope",
                           "expires_after_seq")}
            elif ev.action == "task.execution":
                summary = (f"ran {p.get('tool_id')} v{p.get('tool_version')} "
                           f"-> {p.get('outcome')} "
                           f"(exit={p.get('exit_status')}, "
                           f"signal={p.get('signal_number')}, "
                           f"{p.get('duration_s', 0):.2f}s)")
                detail = {k: p.get(k) for k in
                          ("outcome", "tool_id", "tool_version", "tool_digest",
                           "exit_status", "signal_number", "determinism",
                           "limits", "stdout_digest", "stderr_digest")}
            elif ev.action == "task.evidence":
                arts = p.get("artifacts", {})
                summary = f"captured {len(arts)} artifact(s) as evidence"
                detail = {"artifacts": arts}
            elif ev.action == "idempotency.bind":
                # The owner is the EVENT'S actor here too. Reporting the
                # payload's claim would let a forged binding name whoever it
                # liked as the submitter in the audit trail, which is the
                # shape that already bit this module once: the auditor read
                # an attacker-controlled field and told the reader the
                # attack had worked.
                summary = (
                    f"bound idempotency key {p.get('key')!r} for tool "
                    f"{p.get('tool_id')!r} to request "
                    f"{str(p.get('request_digest'))[:12]}, owned by "
                    f"{ev.actor!r}; a later submission of the same request "
                    "under this key resolves here and does not re-execute")
                detail = {"key": p.get("key"), "owner": ev.actor,
                          "tool_id": p.get("tool_id"),
                          "request_digest": p.get("request_digest"),
                          "job_id": p.get("job_id")}
            else:
                summary = f"{ev.action}"
                detail = dict(p)
            steps.append(Step(ev.seq, ev.wall_time, ev.actor, ev.action,
                              summary, detail))

        gaps = self._gaps(task_id, outcome, seen_actions, steps,
                          transitions)
        gaps += self._execution_count_gaps(task_id, steps)
        actors = tuple(sorted({s.actor for s in steps}))
        return Explanation(task_id, outcome, tuple(steps), gaps, actors)

    def _execution_count_gaps(self, task_id: str, steps: tuple) -> tuple:
        """Did the work run more than once under one task identity?

        This is the question an idempotency claim actually rests on, and it
        is answerable from the log rather than from the ledger's own
        bookkeeping: one task, one execution record. A SUPPRESSED duplicate
        writes nothing -- that is the whole point of suppressing it -- so
        the evidence for "it did not run twice" is the absence of a second
        record here, not the presence of a note saying so.
        """
        runs = [s for s in steps if s.action == "task.execution"]
        if len(runs) <= 1:
            return ()
        seqs = ", ".join(str(s.seq) for s in runs)
        return (
            f"{task_id} has {len(runs)} execution records (seq {seqs}); one "
            "task identity ran the work more than once, so anything binding "
            "a key to this task is suppressing duplicates it already let "
            "through",)

    def _gaps(self, task_id: str, outcome: str, seen: set,
              steps: tuple, transitions: list) -> tuple:
        """Structural holes in a chain. The question enforcement cannot ask."""
        gaps = []

        # A connected walk, the same check explain_record makes. Without it a
        # forged record naming a convenient src changes the OUTCOME this
        # method reports: a hostile campaign appended one claiming
        # EXECUTING -> TIMED_OUT against a task sitting in VERIFIED, the
        # projection refused it (correctly), and the audit still read the
        # task as TIMED_OUT because it took the last dst it saw.
        #
        # The two answers must not disagree. An auditor that reports a state
        # the enforcement path rejected is telling a reader the attack worked.
        expected = None
        for seq, src, dst in transitions:
            if expected is not None and src != expected:
                gaps.append(
                    f"seq {seq}: transition claims to start at {src!r} but "
                    f"the chain was at {expected!r}; a record whose starting "
                    "state the history does not agree with was not written "
                    "through the gate, and the outcome reported above is the "
                    "forged one")
            expected = dst
        for required in REQUIRED_RECORDS.get(outcome, ()):
            if required not in seen:
                gaps.append(
                    f"{outcome} without a {required} record: the transition "
                    "was permitted, but nothing supports it")

        if outcome == "VERIFIED":
            executed_by = None
            verified_by = None
            for s in steps:
                if s.action == "task.execution":
                    # The durable statement of who ran the tool, made by the
                    # actor that ran it. This is the only source for it.
                    executed_by = s.actor
                elif s.action == "task.transition":
                    if s.detail.get("dst") == "COMPLETED":
                        # NOT s.detail["executed_by"]. This check exists to
                        # catch a history that did not go through the gate,
                        # and reading the forger's own field to decide
                        # whether the forger cheated answers no every time.
                        # The actor is who the log says moved it; a payload
                        # naming someone else is itself the finding.
                        if executed_by is None:
                            executed_by = s.actor
                        claimed = s.detail.get("executed_by")
                        if claimed and claimed != executed_by:
                            gaps.append(
                                f"seq {s.seq}: the completion names "
                                f"{claimed!r} as the executor, but the log "
                                f"shows {executed_by!r}; separation of duties "
                                "is judged against the executor, so a record "
                                "that renames one is choosing its own "
                                "verifier")
                    elif s.detail.get("dst") == "VERIFIED":
                        verified_by = s.actor
            if executed_by is not None and executed_by == verified_by:
                # Should be unreachable through the state machine. Checked
                # anyway: this reads the LOG, so it catches a history written
                # by something that did not go through the gate.
                gaps.append(
                    f"executor and verifier are both {executed_by!r}; the "
                    "state machine forbids this, so a log containing it was "
                    "not written through the gate")
        return tuple(gaps)

    # ---- authority records ---------------------------------------------
    def records(self) -> tuple:
        """Every authority record id the history mentions."""
        return tuple(sorted(
            {ev.payload.get("record_id") for ev in self.events
             if ev.action in _RECORD_ACTIONS and ev.payload.get("record_id")}))

    def _record_walks(self) -> dict:
        """record_id -> (proposer, final state name, declared dependencies).

        Built once per query over the whole history, because the dependency
        check is cross-record: a record cannot tell from its own events
        whether the thing it rests on is still canonical.
        """
        walks: dict = {}
        for ev in self.events:
            p = ev.payload
            rid = p.get("record_id")
            if not rid:
                continue
            if ev.action == ACT_RECORD_CREATE:
                walks.setdefault(rid, {"proposer": p.get("proposer"),
                                       "state": INITIAL.value,
                                       "depends_on": []})
                walks[rid]["proposer"] = p.get("proposer")
                walks[rid]["state"] = p.get("state", INITIAL.value)
                walks[rid]["depends_on"] += list(p.get("depends_on") or ())
            elif ev.action == ACT_RECORD_TRANSITION:
                w = walks.setdefault(rid, {"proposer": None, "state": None,
                                           "depends_on": []})
                w["state"] = p.get("dst")
            elif ev.action == ACT_RECORD_DEPEND:
                w = walks.setdefault(rid, {"proposer": None, "state": None,
                                           "depends_on": []})
                w["depends_on"] += list(p.get("depends_on") or ())
        return walks

    def explain_record(self, record_id: str) -> Explanation:
        """Reconstruct an authority record's history and say what is missing.

        The authority twin of :meth:`explain_task`. See the module docstring
        for why the three checks below are not redundant with the state
        machine that already permitted every transition.
        """
        events = [ev for ev in self._by_target.get(record_id, [])
                  if ev.action in _RECORD_ACTIONS]
        if not events:
            return Explanation(record_id, "UNKNOWN", (),
                               (f"no authority records for {record_id!r}",),
                               ())

        steps = []
        outcome = "UNKNOWN"
        proposer = None
        created = False
        depends_on: list = []
        for ev in events:
            p = ev.payload
            if ev.action == ACT_RECORD_CREATE:
                created = True
                proposer = p.get("proposer")
                outcome = p.get("state", INITIAL.value)
                depends_on += list(p.get("depends_on") or ())
                summary = (f"proposed as kind {p.get('kind')!r} in state "
                           f"{outcome}, citing "
                           f"{len(p.get('evidence') or {})} evidence key(s)")
                detail = {k: p.get(k) for k in
                          ("kind", "proposer", "state", "evidence",
                           "depends_on", "policy_id")}
            elif ev.action == ACT_RECORD_TRANSITION:
                summary = (f"{p.get('src')} -> {p.get('dst')} "
                           f"as {p.get('role')}")
                if p.get("stale_reason"):
                    summary += f"  ({str(p['stale_reason'])[:80]})"
                outcome = p.get("dst", outcome)
                detail = {k: p.get(k) for k in
                          ("src", "dst", "role", "evidence", "policy_id",
                           "stale_reason", "edge_reason")}
            else:                                    # ACT_RECORD_DEPEND
                added = list(p.get("depends_on") or ())
                depends_on += added
                summary = f"declared a dependency on {added}"
                detail = {"depends_on": added}
            steps.append(Step(ev.seq, ev.wall_time, ev.actor, ev.action,
                              summary, detail))

        gaps = self._record_gaps(record_id, outcome, steps,
                                 created=created, proposer=proposer,
                                 depends_on=depends_on)
        actors = tuple(sorted({s.actor for s in steps}))
        return Explanation(record_id, outcome, tuple(steps), gaps, actors)

    def _record_gaps(self, record_id: str, outcome: str, steps: tuple, *,
                     created: bool, proposer, depends_on: list) -> tuple:
        """Holes only a reader of the whole history can see."""
        gaps = []
        if not created:
            gaps.append(
                f"{record_id} has transitions but no {ACT_RECORD_CREATE} "
                "record: its origin, proposer and initial evidence are "
                "unrecorded, so nothing establishes what was claimed")

        # A connected walk from PROPOSED. The store reads current state
        # before appending, so it CANNOT produce a discontinuity; one in the
        # log means the history was written around the store.
        expected = INITIAL.value if created else None
        for s in steps:
            if s.action != ACT_RECORD_TRANSITION:
                continue
            src, dst = s.detail.get("src"), s.detail.get("dst")
            if expected is not None and src != expected:
                gaps.append(
                    f"seq {s.seq}: transition claims to start at {src!r} but "
                    f"the record was at {expected!r}; the store reads current "
                    "state before appending, so this history was not written "
                    "through it")
            expected = dst
            edge = _edge_for(src, dst)
            if edge is None:
                gaps.append(
                    f"seq {s.seq}: {src} -> {dst} is not an edge of the "
                    "authority state machine; no transition through check() "
                    "could have produced it")
                continue
            if edge.requires_distinct_actor and proposer is not None \
                    and s.actor == proposer:
                gaps.append(
                    f"seq {s.seq}: {s.actor!r} proposed {record_id} and also "
                    f"performed {src} -> {dst}, which requires a distinct "
                    "actor (I4); the state machine forbids this, so a log "
                    "containing it was not written through the gate")
            if dst == State.PROMOTED.value and not (
                    s.detail.get("policy_id")
                    or (s.detail.get("evidence") or {}).get("policy_id")):
                gaps.append(
                    f"seq {s.seq}: promotion of {record_id} names no policy "
                    "(I5); nothing records under which rules it became "
                    "canonical")
            missing = sorted(edge.requires_evidence
                             - set(s.detail.get("evidence") or {}))
            # policy_id may be carried on the record rather than repeated in
            # the transition's evidence; the dedicated check above covers it.
            missing = [m for m in missing
                       if not (m == "policy_id" and s.detail.get("policy_id"))]
            if missing:
                gaps.append(
                    f"seq {s.seq}: {src} -> {dst} requires evidence "
                    f"{missing}, and the record does not carry it (I6)")

        # The cross-record hole. Nothing in store.py watches dependents.
        if outcome in _CANONICAL_NAMES and depends_on:
            walks = self._record_walks()
            for dep in sorted(set(depends_on)):
                w = walks.get(dep)
                if w is None:
                    gaps.append(
                        f"{record_id} is {outcome} but depends on {dep!r}, "
                        "which this history never created: canonical "
                        "authority resting on something unrecorded")
                elif w["state"] not in _CANONICAL_NAMES:
                    gaps.append(
                        f"{record_id} is still {outcome} while its dependency "
                        f"{dep!r} is {w['state']}; invalidation cascades only "
                        "when a caller runs it, so this is canonical "
                        "authority resting on withdrawn foundations and no "
                        "single transition is wrong")
        return tuple(gaps)

    def audit_records(self) -> tuple:
        """Explain every authority record. Gaps first."""
        return tuple(sorted((self.explain_record(r) for r in self.records()),
                            key=lambda e: (e.complete, e.subject)))

    # ---- policy ---------------------------------------------------------
    def decisions(self, *, subject: str | None = None,
                  action: str | None = None, resource: str | None = None,
                  policy_id: str | None = None,
                  allowed: bool | None = None) -> tuple:
        """Recorded policy decisions, oldest first, narrowed by any filter.

        Every filter is exact-match and conjunctive. Substring or pattern
        matching is deliberately absent: an auditor who half-matches a
        subject gets a confident answer about the wrong principal.
        """
        out = []
        for ev in self.events:
            if ev.action != ACT_POLICY_DECISION:
                continue
            d = ev.payload.get("decision") or {}
            req = d.get("request") or {}
            if subject is not None and req.get("subject") != subject:
                continue
            if action is not None and req.get("action") != action:
                continue
            if resource is not None and req.get("resource") != resource:
                continue
            if policy_id is not None and d.get("policy_id") != policy_id:
                continue
            if allowed is not None and bool(d.get("allowed")) != allowed:
                continue
            out.append(Step(
                ev.seq, ev.wall_time, ev.actor, ev.action,
                f"{'ALLOW' if d.get('allowed') else 'DENY'} "
                f"{req.get('action')} on {req.get('resource')!r} for "
                f"{req.get('subject')!r} under "
                f"{d.get('policy_id')}@{d.get('version')} "
                f"[{d.get('rule_id') or 'no rule'}]: {d.get('reason')}",
                {**d, "decision_digest": ev.payload.get("decision_digest")}))
        return tuple(out)

    def denials(self, **filters) -> tuple:
        """What was refused. The query an incident starts with."""
        filters.pop("allowed", None)
        return self.decisions(allowed=False, **filters)

    def policy_versions(self, policy_id: str) -> tuple:
        """``(seq, version, digest)`` per publication, oldest first.

        Read from the log rather than from :class:`PolicyStore` on purpose:
        an auditor asking which document decided must not depend on a
        projection built by the same code whose decision is in question.
        """
        out = []
        for ev in self.events:
            if ev.action != ACT_POLICY_PUBLISH:
                continue
            doc = ev.payload.get("document") or {}
            if doc.get("policy_id") != policy_id:
                continue
            out.append((ev.seq, doc.get("version"),
                        ev.payload.get("policy_digest")))
        return tuple(out)

    def explain_decision(self, at_seq: int) -> Explanation:
        """One decision, joined to the document version that made it.

        The join is the point. A decision record names a policy digest; the
        publication record holds the document. Checking that the digest
        matches a version this log actually published is what distinguishes
        a decision from an assertion that one was made.
        """
        ev = next((e for e in self.events
                   if e.seq == at_seq and e.action == ACT_POLICY_DECISION),
                  None)
        if ev is None:
            return Explanation(f"decision@{at_seq}", "UNKNOWN", (),
                               (f"no policy decision at seq {at_seq}",), ())
        d = ev.payload.get("decision") or {}
        steps = [s for s in self.decisions() if s.seq == at_seq]
        gaps = []
        published = self.policy_versions(d.get("policy_id") or "")
        match = [(seq, ver, dg) for seq, ver, dg in published
                 if dg == d.get("policy_digest")]
        for seq, ver, dg in published:
            if dg == d.get("policy_digest"):
                doc_ev = next(e for e in self.events if e.seq == seq)
                steps.insert(0, Step(
                    seq, doc_ev.wall_time, doc_ev.actor, doc_ev.action,
                    f"published {d.get('policy_id')} version {ver} "
                    f"({dg[:12]}), the document that decided",
                    {"policy_digest": dg, "version": ver}))
        if not published:
            gaps.append(
                f"the decision cites policy {d.get('policy_id')!r}, which "
                "this log never published: the rules it was decided under "
                "are not in the history")
        elif not match:
            gaps.append(
                f"the decision cites policy digest "
                f"{str(d.get('policy_digest'))[:12]}, which matches no "
                f"version of {d.get('policy_id')!r} in this log "
                f"(published: {[v for _, v, _ in published]}); two documents "
                "with one id and version but different content is a "
                "tampering signature")
        elif match[0][1] != d.get("version"):
            gaps.append(
                f"the decision claims version {d.get('version')} but the "
                f"document with that digest was published as version "
                f"{match[0][1]}")
        return Explanation(
            f"decision@{at_seq}",
            "ALLOW" if d.get("allowed") else "DENY",
            tuple(steps), tuple(gaps),
            tuple(sorted({s.actor for s in steps})))

    def trace_artifact(self, digest: str) -> tuple:
        """Which tasks claim to have produced these bytes.

        Plural on purpose. Two tasks producing an identical artifact is
        normal for a deterministic tool and is exactly what a reader
        investigating a duplicate needs to see.
        """
        if not is_digest(digest):
            raise ValueError(
                f"{digest!r} is not a sha256 digest; artifacts are traced by "
                "content, not by name")
        out = []
        for ev in self.events:
            if ev.action != "task.evidence":
                continue
            for rel, dg in (ev.payload.get("artifacts") or {}).items():
                if dg == digest:
                    out.append((ev.payload.get("task_id", ev.target), rel,
                                ev.seq))
        return tuple(sorted(out))

    def timeline(self) -> tuple:
        """Every event as a readable step. The incident-reconstruction view."""
        return tuple(
            Step(ev.seq, ev.wall_time, ev.actor, ev.action,
                 f"{ev.action} on {ev.target}", dict(ev.payload))
            for ev in self.events)

    # ---- metrics and emission ------------------------------------------
    def metrics(self) -> dict:
        """Counts an operator watches, derived from the log rather than kept.

        WHY DERIVED AND NOT COUNTED AS IT HAPPENS

        A counter incremented at the point of an event is a second record of
        that event, kept somewhere the log's guarantees do not reach: it
        drifts on a crash, resets on a restart, and disagrees with the
        history without anything noticing. These are folded from the same
        bytes every other answer here comes from, so a metric and an
        explanation cannot tell different stories.

        The cost is that this is O(n) in history, which is the correct trade
        for a number nobody polls in a loop. A caller that wants a cheap
        recent view builds a WINDOWED index and asks that.
        """
        by_action: dict = {}
        by_actor: dict = {}
        denials = 0
        refusals = 0
        for ev in self.events:
            by_action[ev.action] = by_action.get(ev.action, 0) + 1
            by_actor[ev.actor] = by_actor.get(ev.actor, 0) + 1
            p = ev.payload if isinstance(ev.payload, dict) else {}
            d = p.get("decision")
            if isinstance(d, dict) and d.get("allowed") is False:
                denials += 1
            if p.get("ok") is False:
                refusals += 1
        return {
            "events": len(self.events),
            "head_seq": self.events[-1].seq if self.events else -1,
            "actions": dict(sorted(by_action.items())),
            "actors": dict(sorted(by_actor.items())),
            "distinct_actors": len(by_actor),
            "policy_denials": denials,
            "recorded_refusals": refusals,
            "window": list(self.window) if self.window else None,
        }

    def emit(self, sink, *, redactor=None) -> int:
        """Write every event to a structured SINK. Returns how many.

        WHAT A SINK IS AND IS NOT

        The log is the authority history and stays the only thing anything
        is decided from. A sink is a COPY, for somewhere an operator already
        watches -- a file a collector tails, a stream a CI job keeps. Nothing
        here reads a sink back, and nothing may: a second copy that could be
        cited would be a second history, and the one on disk is the one with
        a hash chain.

        Redacted on the way out, through the same coarse net the rest of
        this module uses. That net is the SECOND line of defence and is
        stated as such: the executor builds the child's environment rather
        than inheriting it, and raw tool output never enters the log, so a
        secret should not be there to redact. This runs anyway, because the
        cost of being wrong about that is a credential in a collector.
        """
        write = getattr(sink, "write", None)
        if not callable(write):
            raise TypeError(
                "a sink must have a write() method, got "
                f"{type(sink).__name__}")
        redact_fn = redactor if redactor is not None else redact
        count = 0
        for ev in self.events:
            write(json.dumps({
                "seq": ev.seq, "wall_time": ev.wall_time, "actor": ev.actor,
                "action": ev.action, "target": ev.target,
                "payload": redact_fn(ev.payload),
            }, sort_keys=True, default=str) + "\n")
            count += 1
        flush = getattr(sink, "flush", None)
        if callable(flush):
            flush()
        return count

    def audit_all(self) -> tuple:
        """Explain every task. Returns those with provenance gaps first."""
        seen = set()
        out = []
        for ev in self.events:
            tid = ev.payload.get("task_id")
            if tid and tid not in seen:
                seen.add(tid)
                out.append(self.explain_task(tid))
        return tuple(sorted(out, key=lambda e: (e.complete, e.subject)))
