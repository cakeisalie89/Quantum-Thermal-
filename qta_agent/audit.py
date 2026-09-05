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

REDACTION

Audit output is meant to be read, pasted into incident notes, and attached to
reports. Anything that looks like a credential is replaced before it leaves
this module. That is a coarse net over a surface that should not contain
secrets in the first place -- the executor replaces the child's environment
rather than inheriting it, and raw tool output never enters the log -- so this
is the second line, not the first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

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


class AuditIndex:
    """A queryable view over a verified log."""

    def __init__(self, events):
        self.events = list(events)
        self._by_target: dict = {}
        self._by_actor: dict = {}
        for ev in self.events:
            self._by_target.setdefault(ev.target, []).append(ev)
            self._by_actor.setdefault(ev.actor, []).append(ev)

    @classmethod
    def from_log(cls, log) -> "AuditIndex":
        """Build from a log, verifying it first. Fail closed.

        An audit over an unverified log answers questions about a document
        that may have been rewritten, which is worse than refusing: it
        produces a confident answer with no basis.
        """
        log.verify().raise_if_bad()
        return cls(log.read())

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
            else:
                summary = f"{ev.action}"
                detail = dict(p)
            steps.append(Step(ev.seq, ev.wall_time, ev.actor, ev.action,
                              summary, detail))

        gaps = self._gaps(task_id, outcome, seen_actions, steps)
        actors = tuple(sorted({s.actor for s in steps}))
        return Explanation(task_id, outcome, tuple(steps), gaps, actors)

    def _gaps(self, task_id: str, outcome: str, seen: set,
              steps: tuple) -> tuple:
        """Structural holes in a chain. The question enforcement cannot ask."""
        gaps = []
        for required in REQUIRED_RECORDS.get(outcome, ()):
            if required not in seen:
                gaps.append(
                    f"{outcome} without a {required} record: the transition "
                    "was permitted, but nothing supports it")

        if outcome == "VERIFIED":
            executed_by = None
            verified_by = None
            for s in steps:
                if s.action == "task.transition":
                    if s.detail.get("dst") == "COMPLETED":
                        executed_by = s.detail.get("executed_by") or s.actor
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
