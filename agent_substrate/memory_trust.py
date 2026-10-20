"""What a remembered statement has, and what it does not (Tranche G, G4).

A memory entry can be many things at once, and the danger this module exists
for is a reader collapsing them into one. "It is in the log" is not "the
person it names wrote it"; "the person it names wrote it" is not "it cites
evidence"; "it cites evidence" is not "a second reader agrees"; and none of
those is "it is true". :func:`assess` answers each question separately, from
the log, and says why:

``hash_consistent``
    The chain verifies -- every record links to the one before -- and
    against its independently written head, so a truncated tail is seen. A
    log rewritten together with its head witness is consistent with itself;
    that is all this property says. Authentication is NOT part of it: the
    chain is read past the security profile's gate, and who signed what is
    the next property's question.
``authenticated_actor``
    The record that wrote the entry was appended by the author it names, AND
    the log's authenticator vouches for THAT record. Without an
    authenticator the first half is all there is, and the answer is
    NOT_ESTABLISHED: the actor is a name a record states.
``policy_authorized``
    Nothing here gates a memory write by policy -- memory is not authority,
    and the gates are for what is. NOT_APPLICABLE, said rather than implied.
``evidence_supported``
    Every digest the entry cites resolves in the evidence store and still
    hashes to itself. An entry that cites nothing is NOT_ESTABLISHED; one
    citing a digest the store does not hold is NO -- a false citation reads
    exactly like a true one to anybody who does not look it up.
``consistently_reconstructed``
    The primary projection and the independent reader agree on the entry,
    and neither refuses the history. Given a ``view`` -- a projection some
    reader is holding -- it must agree too: a view loaded before a
    retraction still says ACTIVE, and that disagreement is the finding.
``factually_established``
    Never, for a memory. A fact is established by a record the authority
    store holds VERIFIED or PROMOTED, through the gates; a memory is at most
    a pointer to one. NOT_ESTABLISHED, always, with the reason.

The answers are YES, NO, NOT_ESTABLISHED and NOT_APPLICABLE. Only YES holds.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .events import EventLog, EventLogError
from .memory import ACT_MEMORY_WRITE, MemoryError_, MemoryStore
from .reconstruct import reconstruct_subsystems

YES, NO = "YES", "NO"
NOT_ESTABLISHED, NOT_APPLICABLE = "NOT_ESTABLISHED", "NOT_APPLICABLE"
PROPERTIES = ("hash_consistent", "authenticated_actor", "policy_authorized",
              "evidence_supported", "consistently_reconstructed",
              "factually_established")


@dataclass(frozen=True)
class Answer:
    value: str
    why: str

    @property
    def holds(self) -> bool:
        return self.value == YES


@dataclass(frozen=True)
class MemoryTrust:
    memory_id: str
    #: The status the log gives the entry, or None where it cannot say.
    status: str | None
    answers: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        return {"memory_id": self.memory_id, "status": self.status,
                **{k: {"value": a.value, "why": a.why}
                   for k, a in self.answers.items()}}


class _Chain(EventLog):
    """The chain and its head witness, and nothing else: the security
    profile's gate admits every linked record, so that authentication is
    answered by its own property rather than folded into this one. Used to
    READ, by this module only; nothing read through it is projected."""

    def _security_gate(self, events, problems, *, whole):
        return events


def _hash_consistent(chain: _Chain):
    report, events = chain.read_verified()
    if not report.ok:
        return Answer(NO, "; ".join(report.problems[:3])), events
    return Answer(YES, f"the chain verifies through seq {report.head_seq}, "
                       "against its recorded head"), events


def _write_record(events: list, memory_id: str):
    for ev in events:
        if ev.action == ACT_MEMORY_WRITE and isinstance(ev.payload, dict):
            entry = ev.payload.get("entry") or {}
            if isinstance(entry, dict) and entry.get("memory_id") == memory_id:
                return ev, entry
    return None, None


def assess(log: EventLog, memory_id: str, *, evidence=None,
           view: MemoryStore | None = None) -> MemoryTrust:
    """Each of the six properties of ``memory_id``, answered separately."""
    answers: dict = {}
    answers["hash_consistent"], events = _hash_consistent(_Chain(log.path))
    if not answers["hash_consistent"].holds:
        why = "the history does not verify, so nothing in it can be read"
        for prop in PROPERTIES[1:]:
            answers[prop] = Answer(NO if prop != "factually_established"
                                   else NOT_ESTABLISHED, why)
        return MemoryTrust(memory_id, None, answers)

    ev, entry = _write_record(events, memory_id)
    if ev is None:
        for prop in PROPERTIES[1:]:
            answers[prop] = Answer(NO, f"no record writes {memory_id!r}")
        return MemoryTrust(memory_id, None, answers)

    # -- authenticated actor
    if entry.get("author") != ev.actor:
        answers["authenticated_actor"] = Answer(
            NO, f"the entry names {entry.get('author')!r} as its author and "
                f"was appended by {ev.actor!r}")
    elif log.authenticator is None:
        answers["authenticated_actor"] = Answer(
            NOT_ESTABLISHED,
            f"appended by {ev.actor!r}, the author it names; no "
            "authenticator was given, so that is a name the record states "
            "and no key vouches for it")
    else:
        report = log.authenticator(events, complete=True)
        found = [f"{k}: {d}" for s, k, d in report.findings if s == ev.seq]
        answers["authenticated_actor"] = (
            Answer(NO, f"the record at seq {ev.seq} does not authenticate: "
                       + "; ".join(found or ["refused"]))
            if ev.seq in report.refused else
            Answer(YES, f"appended by {ev.actor!r}, the author it names, "
                        "and the authenticator vouches for that record"))

    # -- policy
    answers["policy_authorized"] = Answer(
        NOT_APPLICABLE, "no policy gates a memory write: memory is not "
                        "authority, and the gates are for what is")

    # -- evidence
    cited = tuple(entry.get("derived_from") or ())
    digests = [c for c in cited if isinstance(c, str) and len(c) == 64]
    if not digests:
        answers["evidence_supported"] = Answer(
            NOT_ESTABLISHED, "the entry cites no evidence")
    elif evidence is None:
        answers["evidence_supported"] = Answer(
            NOT_ESTABLISHED, f"cites {len(digests)} digest(s); no evidence "
                             "store was given to look them up in")
    else:
        missing = [d for d in digests if not evidence.contains(d)]
        answers["evidence_supported"] = (
            Answer(NO, f"cites {len(missing)} digest(s) the evidence store "
                       f"does not hold, or no longer verifies: "
                       f"{', '.join(d[:12] for d in missing)}")
            if missing else
            Answer(YES, f"all {len(digests)} cited digest(s) resolve and "
                        "verify"))

    # -- reconstruction
    status = None
    try:
        primary = MemoryStore(
            EventLog(log.path, authenticator=log.authenticator),
            evidence=evidence).load().get(memory_id)
    except (MemoryError_, EventLogError) as exc:
        primary = None
        answers["consistently_reconstructed"] = Answer(
            NO, f"the primary projection refuses the history: {exc}")
    try:
        sub = reconstruct_subsystems(log)
    except EventLogError as exc:
        primary = None
        answers["consistently_reconstructed"] = Answer(
            NO, f"the independent reader refuses the history: {exc}")
    if primary is not None:
        second = sub.memory.get(memory_id)
        flagged = [a for a in sub.anomalies if repr(memory_id) in a]
        status = primary.status.value
        if second is None or flagged:
            answers["consistently_reconstructed"] = Answer(
                NO, "the independent reader "
                    + ("does not hold it" if second is None else
                       f"reports: {flagged[0]}"))
        elif (second.get("author"), second.get("status")) != (
                primary.author, status):
            answers["consistently_reconstructed"] = Answer(
                NO, f"the readers disagree: primary {primary.author!r} "
                    f"{status}, independent {second.get('author')!r} "
                    f"{second.get('status')}")
        elif view is not None and _view_status(view, memory_id) != status:
            answers["consistently_reconstructed"] = Answer(
                NO, f"the view held says {_view_status(view, memory_id)}; "
                    f"the log says {status}. A projection loaded before a "
                    "later record is answering for a history it has not "
                    "read")
        else:
            answers["consistently_reconstructed"] = Answer(
                YES, f"both readers hold {primary.author!r}, {status}")

    answers["factually_established"] = Answer(
        NOT_ESTABLISHED,
        "a memory is never evidence; a fact is established by a record the "
        "authority store holds VERIFIED or PROMOTED, and a memory is at most "
        "a pointer to one")
    return MemoryTrust(memory_id, status, answers)


def _view_status(view: MemoryStore, memory_id: str) -> str | None:
    try:
        return view.get(memory_id).status.value
    except MemoryError_:
        return None
