"""Durable memory: what the agent remembers, and why that is not authority.

THE DISTINCTION THIS MODULE EXISTS TO KEEP

An agent that runs for months needs to carry conclusions forward. The danger
is not that it remembers; it is that a remembered sentence gradually acquires
the standing of a checked one. "We established last month that the coupling
term is negligible" is a proposal with a long history, and a system that
cannot tell it apart from a verified result will eventually promote it.

So memory here is a separate kind of record with a separate store, and the
separation is structural rather than advisory:

  * a memory entry lives in the event log, NEVER in the evidence store, so its
    digest does not resolve as evidence and any transition citing it is
    refused by the check that already exists;
  * nothing in the authority path imports this module -- the layering test
    enforces the direction, so an authority decision CANNOT read memory even
    by mistake;
  * an entry names what it was derived from, and when one of those sources is
    invalidated the entry becomes STALE without anyone having to remember to
    do it.

WHAT A MEMORY MAY DO

Influence a proposal. Provide a starting point. Say "last time this failed for
reason X". All of that is useful and none of it is authority.

WHAT A MEMORY MAY NOT DO

Become evidence. Satisfy a gate. Stand in for a verification. Assert its own
confidence into a decision. Override a policy. Those are refused, and each
refusal has a test named after the attack rather than after the method.

STATUS IS NOT CONFIDENCE

``confidence`` is the author's own estimate and is treated as commentary --
recorded, never compared, never summed. ``status`` is the system's, derived
from events: an entry goes STALE when a source is invalidated, SUPERSEDED when
a later entry replaces it, RETRACTED when its author withdraws it. A reader
that filters on status is asking the system; one that filters on confidence is
asking the agent about itself.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .canonical import digest, is_digest

ACT_MEMORY_WRITE = "memory.write"
ACT_MEMORY_STATUS = "memory.status"

#: An entry longer than this is refused. Memory that nobody can read is not
#: memory, and an unbounded entry is a replay-time memory bomb.
MAX_ENTRY_BYTES = 16 * 1024


class MemoryError_(Exception):
    """Base class. Every failure here is fail-closed."""


class UnknownMemory(MemoryError_):
    """No such entry."""


class MemoryStatus(str, Enum):
    """The system's view of an entry. Derived from events, never asserted."""

    ACTIVE = "ACTIVE"
    #: A source it was derived from was invalidated.
    STALE = "STALE"
    #: A later entry replaces it.
    SUPERSEDED = "SUPERSEDED"
    #: Its author withdrew it.
    RETRACTED = "RETRACTED"


#: Statuses a reader must not treat as current.
NOT_CURRENT = frozenset({MemoryStatus.STALE, MemoryStatus.SUPERSEDED,
                         MemoryStatus.RETRACTED})

#: Where a status may move to. Enforced on REPLAY as well as on the write
#: path, because a reducer that just assigns whatever a record says lets one
#: appended line rewrite history.
#:
#: Nothing leads back to ACTIVE. That is the point rather than an omission: a
#: note goes stale because a source it rested on was invalidated, and if that
#: source becomes trustworthy again the honest record is a NEW entry, not a
#: quiet reinstatement of the old one. RETRACTED is terminal for the same
#: reason -- an author withdrew a statement, and un-withdrawing it by fiat
#: would leave the log saying it was never withdrawn.
STATUS_EDGES: dict = {
    MemoryStatus.ACTIVE: frozenset({
        MemoryStatus.STALE, MemoryStatus.SUPERSEDED, MemoryStatus.RETRACTED}),
    MemoryStatus.STALE: frozenset({
        MemoryStatus.SUPERSEDED, MemoryStatus.RETRACTED}),
    MemoryStatus.SUPERSEDED: frozenset({MemoryStatus.RETRACTED}),
    MemoryStatus.RETRACTED: frozenset(),
}


@dataclass(frozen=True)
class MemoryEntry:
    """One thing the agent remembers, with where it came from."""

    memory_id: str
    text: str
    author: str
    #: Evidence digests and record ids this was derived from. An entry with an
    #: empty list is allowed and is marked as such: an observation with no
    #: source is a hunch, and calling it one is more useful than pretending.
    derived_from: tuple = ()
    #: The author's own estimate. Commentary; never compared or summed.
    confidence: str = "unstated"
    status: MemoryStatus = MemoryStatus.ACTIVE
    #: Why it is not current, when it is not.
    status_reason: str = ""
    superseded_by: str | None = None
    created_seq: int = -1
    updated_seq: int = -1
    #: Set when this entry summarizes other entries. The summary NEVER
    #: replaces them -- see ``MemoryStore.summarize``.
    summarizes: tuple = ()

    def body(self) -> dict:
        return {"memory_id": self.memory_id, "text": self.text,
                "author": self.author,
                "derived_from": list(self.derived_from),
                "confidence": self.confidence,
                "summarizes": list(self.summarizes)}

    def digest(self) -> str:
        """Content digest of what was remembered, not of its status."""
        return digest(self.body())

    def to_record(self) -> dict:
        rec = self.body()
        rec.update({"status": self.status.value,
                    "status_reason": self.status_reason,
                    "superseded_by": self.superseded_by,
                    "created_seq": self.created_seq,
                    "updated_seq": self.updated_seq})
        return rec

    @property
    def is_current(self) -> bool:
        return self.status not in NOT_CURRENT


def entry_from_record(rec: dict) -> MemoryEntry:
    """Rebuild an entry from a log payload, validating its shape."""
    if not isinstance(rec, dict):
        raise MemoryError_(f"memory record is {type(rec).__name__}")
    known = set(MemoryEntry.__dataclass_fields__)
    unknown = set(rec) - known
    if unknown:
        raise MemoryError_(
            f"memory record carries unknown fields {sorted(unknown)}; "
            "refusing to project an entry this version does not fully "
            "understand")
    try:
        return MemoryEntry(
            memory_id=rec["memory_id"], text=rec["text"],
            author=rec["author"],
            derived_from=tuple(rec.get("derived_from", ())),
            confidence=rec.get("confidence", "unstated"),
            status=MemoryStatus(rec.get("status", "ACTIVE")),
            status_reason=rec.get("status_reason", ""),
            superseded_by=rec.get("superseded_by"),
            created_seq=rec.get("created_seq", -1),
            updated_seq=rec.get("updated_seq", -1),
            summarizes=tuple(rec.get("summarizes", ())))
    except (KeyError, TypeError, ValueError) as exc:
        raise MemoryError_(f"memory record is malformed: {exc}") from exc


class MemoryStore:
    """Durable agent memory, projected from the log. Never authority.

    ``evidence`` is accepted so that ``derived_from`` citations can be
    CHECKED -- an entry may not claim to come from a digest the store does not
    hold. It is a read-only use: nothing here ever writes to the evidence
    store, which is what keeps a memory from becoming citable as evidence.
    """

    def __init__(self, log, *, evidence=None):
        self.log = log
        self.evidence = evidence
        self._entries: dict = {}
        #: source digest -> memory_ids derived from it.
        self._by_source: dict = {}
        self._invalidated: set = set()

    # ---- projection ----------------------------------------------------
    def load(self) -> "MemoryStore":
        self.log.verify().raise_if_bad()
        self._entries = {}
        self._by_source = {}
        self._invalidated = set()
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        p = ev.payload
        if ev.action == ACT_MEMORY_WRITE:
            if not isinstance(p, dict) or "entry" not in p:
                # A raw KeyError here leaks the payload's shape and makes the
                # WHOLE store unloadable with an exception that names no
                # subject. Found by a killed child process writing a
                # malformed memory.write into a live log: the record was
                # wrong, and the diagnosis said 'entry'.
                raise MemoryError_(
                    f"seq {ev.seq}: a memory.write from {ev.actor!r} carries "
                    "no entry; a record this store cannot read is refused "
                    "rather than projected")
            entry = entry_from_record(p["entry"])
            if entry.memory_id in self._entries:
                raise MemoryError_(
                    f"seq {ev.seq}: memory {entry.memory_id!r} written twice; "
                    "a second write would silently replace a remembered "
                    "statement without recording that it changed")
            entry = replace(entry, created_seq=ev.seq, updated_seq=ev.seq)
            self._entries[entry.memory_id] = entry
            for src in entry.derived_from:
                self._by_source.setdefault(src, set()).add(entry.memory_id)
            # An entry derived from an already-invalidated source is born
            # stale rather than being briefly treated as current.
            if any(s in self._invalidated for s in entry.derived_from):
                self._entries[entry.memory_id] = replace(
                    entry, status=MemoryStatus.STALE,
                    status_reason="derived from an already-invalidated source",
                    updated_seq=ev.seq)
        elif ev.action == ACT_MEMORY_STATUS:
            mid = p["memory_id"]
            cur = self._entries[mid]
            # RE-AUTHORIZE ON REPLAY. This used to assign whatever the record
            # said, so one appended line moved a RETRACTED note back to
            # ACTIVE -- a statement its author had withdrawn, presented as
            # current again, in a store that feeds the agent's context.
            dst = MemoryStatus(p["status"])
            allowed = STATUS_EDGES.get(cur.status, frozenset())
            if dst is not cur.status and dst not in allowed:
                raise MemoryError_(
                    f"seq {ev.seq}: {mid!r} is {cur.status.value} and may not "
                    f"move to {dst.value}. Permitted: "
                    f"{sorted(x.value for x in allowed) or 'nothing'}. "
                    "A status a replay would refuse today does not become "
                    "state by being present in the log.")
            if dst is MemoryStatus.RETRACTED and ev.actor != cur.author:
                # The same rule the write path applies. Withdrawing someone
                # else's statement is a different act, and a replay that let
                # it through would make the write-path check advisory.
                raise MemoryError_(
                    f"seq {ev.seq}: {mid!r} was written by {cur.author!r}; "
                    f"{ev.actor!r} may not retract it.")
            self._entries[mid] = replace(
                cur, status=dst,
                status_reason=p.get("reason", ""),
                superseded_by=p.get("superseded_by", cur.superseded_by),
                updated_seq=ev.seq)
            if p.get("invalidated_source"):
                self._invalidated.add(p["invalidated_source"])
        else:
            return False
        return True

    # ---- reads ---------------------------------------------------------
    def get(self, memory_id: str) -> MemoryEntry:
        try:
            return self._entries[memory_id]
        except KeyError:
            raise UnknownMemory(f"no memory {memory_id!r}") from None

    def current(self) -> tuple:
        """Entries a reader may treat as current. Sorted, so replay agrees."""
        return tuple(sorted((e for e in self._entries.values()
                             if e.is_current), key=lambda e: e.memory_id))

    def all_entries(self) -> tuple:
        return tuple(sorted(self._entries.values(), key=lambda e: e.memory_id))

    def derived_from(self, source: str) -> tuple:
        return tuple(sorted(self._by_source.get(source, ())))

    # ---- writes --------------------------------------------------------
    def remember(self, *, memory_id: str, text: str, author: str,
                 derived_from: tuple = (), confidence: str = "unstated",
                 summarizes: tuple = ()) -> MemoryEntry:
        """Record something. Refuses citations that do not resolve."""
        if not isinstance(memory_id, str) or not memory_id:
            raise MemoryError_("memory_id must be a non-empty str")
        if not isinstance(text, str) or not text.strip():
            raise MemoryError_(
                "a memory entry must have text; an empty one is a record that "
                "something was remembered without recording what")
        if len(text.encode("utf-8")) > MAX_ENTRY_BYTES:
            raise MemoryError_(
                f"memory entry is over the {MAX_ENTRY_BYTES}-byte bound")
        if memory_id in self._entries:
            raise MemoryError_(f"memory {memory_id!r} already exists")
        derived_from = tuple(dict.fromkeys(derived_from))
        for src in derived_from:
            if is_digest(src) and self.evidence is not None:
                if not self.evidence.contains(src):
                    raise MemoryError_(
                        f"memory {memory_id!r} claims to derive from "
                        f"{src[:12]}, which does not resolve; an entry citing "
                        "evidence that does not exist would read to a later "
                        "auditor exactly like one that does")
        for other in summarizes:
            if other not in self._entries:
                raise MemoryError_(
                    f"memory {memory_id!r} summarizes {other!r}, which does "
                    "not exist")
        entry = MemoryEntry(memory_id=memory_id, text=text, author=author,
                            derived_from=derived_from, confidence=confidence,
                            summarizes=tuple(summarizes))
        ev = self.log.append(actor=author, action=ACT_MEMORY_WRITE,
                             target=memory_id,
                             payload={"entry": entry.to_record()})
        self.apply(ev)
        return self.get(memory_id)

    def summarize(self, *, memory_id: str, text: str, author: str,
                  summarizes: tuple, confidence: str = "unstated"
                  ) -> MemoryEntry:
        """Write a summary. The sources REMAIN, and remain current.

        A summary that replaced its sources would be the mechanism by which a
        long-running agent loses the ability to check itself: the detail is
        gone, the summary reads as established, and nothing records that a
        compression happened. So this adds a record and removes none.
        """
        if not summarizes:
            raise MemoryError_(
                "a summary must name what it summarizes; otherwise it is an "
                "assertion with a reassuring name")
        return self.remember(memory_id=memory_id, text=text, author=author,
                             confidence=confidence, summarizes=summarizes)

    def _set_status(self, memory_id: str, status: MemoryStatus, *,
                    actor: str, reason: str, superseded_by: str | None = None,
                    invalidated_source: str | None = None) -> MemoryEntry:
        self.get(memory_id)
        payload = {"memory_id": memory_id, "status": status.value,
                   "reason": reason}
        if superseded_by is not None:
            payload["superseded_by"] = superseded_by
        if invalidated_source is not None:
            payload["invalidated_source"] = invalidated_source
        ev = self.log.append(actor=actor, action=ACT_MEMORY_STATUS,
                             target=memory_id, payload=payload)
        self.apply(ev)
        return self.get(memory_id)

    def retract(self, memory_id: str, *, actor: str,
                reason: str) -> MemoryEntry:
        """Withdraw an entry. The text stays in the log; the status changes."""
        entry = self.get(memory_id)
        if entry.author != actor:
            raise MemoryError_(
                f"memory {memory_id!r} was written by {entry.author!r}; "
                f"{actor!r} may not retract it. Withdrawing someone else's "
                "statement is a different act from withdrawing your own, and "
                "the log should say which happened.")
        return self._set_status(memory_id, MemoryStatus.RETRACTED,
                                actor=actor, reason=reason)

    def supersede(self, *, old_id: str, new_id: str, actor: str,
                  reason: str) -> MemoryEntry:
        self.get(new_id)
        return self._set_status(old_id, MemoryStatus.SUPERSEDED, actor=actor,
                                reason=reason, superseded_by=new_id)

    def invalidate_source(self, source: str, *, actor: str,
                          reason: str) -> tuple:
        """A source changed. Everything derived from it stops being current.

        Transitive: a summary of a stale entry is stale, because compressing
        something that stopped being true does not make it true again.
        """
        moved: list = []
        seen: set = set()
        frontier = list(self.derived_from(source))
        first = True
        while frontier:
            mid = frontier.pop()
            if mid in seen:
                continue
            seen.add(mid)
            entry = self._entries.get(mid)
            if entry is None or entry.status in NOT_CURRENT:
                continue
            moved.append(self._set_status(
                mid, MemoryStatus.STALE, actor=actor,
                reason=(f"derived from {source[:12]}, which was invalidated: "
                        f"{reason}"),
                invalidated_source=source if first else None))
            first = False
            frontier.extend(
                e.memory_id for e in self._entries.values()
                if mid in e.summarizes or mid in e.derived_from)
        if not moved:
            # Record the invalidation anyway, so an entry written LATER that
            # cites this source is born stale rather than briefly current.
            self._invalidated.add(source)
        return tuple(moved)


# ---- the boundary, as a callable refusal --------------------------------
class MemoryIsNotEvidence(MemoryError_):
    """Something tried to use a remembered statement as evidence."""


def refuse_as_evidence(entry: MemoryEntry) -> None:
    """Always raises. Kept as a function so the refusal has a call site.

    The structural guarantees are the real defence -- a memory digest does not
    resolve in the evidence store, and no module in the authority path can
    import this one. This exists for the case where a caller has BOTH in hand
    and is about to pass the wrong one, so the refusal names what went wrong
    instead of surfacing as "evidence does not resolve" three layers away.
    """
    raise MemoryIsNotEvidence(
        f"memory {entry.memory_id!r} is a remembered statement, not evidence. "
        "It may inform a proposal and may not support a transition: nothing "
        "checked it, and its digest does not resolve in the evidence store "
        "precisely so that this cannot be done by accident.")
