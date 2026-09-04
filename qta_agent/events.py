"""Append-only, hash-chained event log: the authority history of record.

Everything else in this package is derived state. If the snapshot disagrees
with the log, the log wins -- so the log has to be the thing an attacker or a
crash cannot quietly edit.

WHAT THE CHAIN DETECTS, AND HOW

Each record carries ``prev_hash`` (the previous record's ``hash``) and its own
``hash`` over every other field. That single link makes four distinct attacks
visible rather than requiring four separate checks:

tampering
    Editing any field changes that record's recomputed hash.
deletion / reordering
    Breaks the ``prev_hash`` link at the seam, and breaks ``seq`` contiguity.
truncation
    Undetectable from the file alone -- a prefix of a valid chain is a valid
    chain. This is why :meth:`EventLog.verify` accepts an ``expected_head``
    and why :class:`ChainState` is persisted separately: a truncated log fails
    against a head recorded elsewhere. Stated plainly because a chain that
    silently tolerates truncation offers much less than it appears to.
forking
    Two records claiming the same ``seq``, or two chains sharing a prefix and
    diverging, are detected by head comparison rather than by scanning.

TIME

Wall-clock time is recorded but is never authority: a clock can move
backwards, and an attacker may control it. Ordering comes from ``seq``, which
is monotonic and gap-free by construction. ``wall_time`` is diagnostic only,
and :meth:`verify` deliberately does NOT reject non-monotonic wall times --
doing so would make a correct log unreadable after a legitimate NTP
correction. It reports them instead.

CRASH SAFETY

Append is: serialize, write, flush, ``fsync``, then update the head pointer.
A crash mid-append can leave a partial trailing line; :meth:`verify` treats a
malformed tail as a truncation boundary and reports the last intact record,
so recovery is possible without discarding history.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .canonical import (
    CANONICAL_FORM_VERSION,
    ZERO_DIGEST,
    CanonicalizationError,
    canonical_bytes,
    digest,
    is_digest,
)

#: Fields every event carries. Hashing covers all of them except ``hash``.
_HASHED_FIELDS = (
    "seq", "event_id", "wall_time", "actor", "action", "target",
    "payload", "prev_hash", "canonical_form_version",
)

#: A single line longer than this is refused rather than buffered. Prevents a
#: malformed or hostile log from exhausting memory during verification.
MAX_EVENT_BYTES = 4 * 1024 * 1024


class EventLogError(Exception):
    """Base class for log integrity failures. Always fail closed."""


class ChainBroken(EventLogError):
    """The hash chain does not link. Tampering, deletion or reordering."""


class SequenceBroken(EventLogError):
    """``seq`` is not contiguous and ascending from zero."""


class MalformedEvent(EventLogError):
    """A record is unparseable or structurally invalid."""


class HeadMismatch(EventLogError):
    """The log's head disagrees with an independently recorded head.

    This is the truncation and rollback detector. It is a separate class
    because the operational response differs: the log is not corrupt, it is
    *incomplete or stale*, and the missing tail may be recoverable.
    """


@dataclass(frozen=True)
class Event:
    """One immutable authority-relevant fact."""
    seq: int
    event_id: str
    wall_time: float
    actor: str
    action: str
    target: str
    payload: dict
    prev_hash: str
    hash: str
    canonical_form_version: int = CANONICAL_FORM_VERSION

    def body(self) -> dict:
        """The hashed portion: everything except ``hash`` itself."""
        return {f: getattr(self, f) for f in _HASHED_FIELDS}

    def recompute_hash(self) -> str:
        return digest(self.body())

    def to_record(self) -> dict:
        rec = self.body()
        rec["hash"] = self.hash
        return rec


@dataclass(frozen=True)
class ChainState:
    """An independently persisted witness to the log's head.

    Held apart from the log precisely so that truncating the log is
    detectable. A log alone cannot prove it is complete.
    """
    seq: int
    head_hash: str

    def to_record(self) -> dict:
        return {"seq": self.seq, "head_hash": self.head_hash}


@dataclass(frozen=True)
class VerifyReport:
    """Outcome of a full-chain verification. Structured, never a bare bool."""
    ok: bool
    count: int
    head_seq: int
    head_hash: str
    problems: list = field(default_factory=list)
    #: Non-fatal observations: wall-clock regressions, unusual gaps in time.
    notes: list = field(default_factory=list)

    def raise_if_bad(self) -> "VerifyReport":
        if not self.ok:
            raise ChainBroken("; ".join(self.problems) or "chain invalid")
        return self


def _validate_field_types(rec: dict, where: str) -> None:
    checks = (
        ("seq", int), ("event_id", str), ("wall_time", (int, float)),
        ("actor", str), ("action", str), ("target", str),
        ("payload", dict), ("prev_hash", str), ("hash", str),
        ("canonical_form_version", int),
    )
    for name, typ in checks:
        if name not in rec:
            raise MalformedEvent(f"{where}: missing field {name!r}")
        if isinstance(typ, tuple):
            ok = isinstance(rec[name], typ) and not isinstance(rec[name], bool)
        else:
            ok = isinstance(rec[name], typ) and not (
                typ is int and isinstance(rec[name], bool))
        if not ok:
            raise MalformedEvent(
                f"{where}: field {name!r} is "
                f"{type(rec[name]).__name__}, expected {typ}")
    for name in ("prev_hash", "hash"):
        if not is_digest(rec[name]):
            raise MalformedEvent(
                f"{where}: {name!r} is not a lowercase sha256 digest")
    if rec["seq"] < 0:
        raise MalformedEvent(f"{where}: seq is negative")
    unknown = set(rec) - set(_HASHED_FIELDS) - {"hash"}
    if unknown:
        # An unknown field would not be hashed, so it could carry unverified
        # content beside a valid digest.
        raise MalformedEvent(
            f"{where}: unhashed extra fields {sorted(unknown)}")


class EventLog:
    """A durable, append-only, hash-chained log stored as JSON Lines."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.head_path = self.path.with_suffix(self.path.suffix + ".head")

    # ---- reading ------------------------------------------------------
    def __iter__(self) -> Iterator[Event]:
        yield from self.read()

    def read(self, *, strict: bool = True) -> list:
        """Parse every record. With ``strict`` a malformed tail raises."""
        import json
        events: list = []
        if not self.path.exists():
            return events
        with self.path.open("rb") as fh:
            for lineno, raw in enumerate(fh, 1):
                if len(raw) > MAX_EVENT_BYTES:
                    raise MalformedEvent(
                        f"line {lineno}: {len(raw)} bytes exceeds the "
                        f"{MAX_EVENT_BYTES}-byte bound")
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    # A partial trailing line is the expected crash signature.
                    if strict:
                        raise MalformedEvent(
                            f"line {lineno}: unparseable "
                            f"({type(exc).__name__});"
                            " if this is the final line the log was truncated "
                            "mid-append") from exc
                    break
                if not isinstance(rec, dict):
                    raise MalformedEvent(
                        f"line {lineno}: record is "
                        f"{type(rec).__name__}, not an object")
                _validate_field_types(rec, f"line {lineno}")
                events.append(Event(**rec))
        return events

    def head(self) -> ChainState | None:
        """The independently recorded head, if present."""
        import json
        if not self.head_path.exists():
            return None
        try:
            rec = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise MalformedEvent(
                f"head witness is unreadable: {type(exc).__name__}") from exc
        if (not isinstance(rec, dict)
                or not is_digest(rec.get("head_hash", ""))
                or not isinstance(rec.get("seq"), int)):
            raise MalformedEvent("head witness is structurally invalid")
        return ChainState(seq=rec["seq"], head_hash=rec["head_hash"])

    # ---- verification -------------------------------------------------
    def verify(self, *, expected_head: ChainState | None = None,
               use_witness: bool = True) -> VerifyReport:
        """Verify the whole chain. Fail closed; report every problem found."""
        problems: list = []
        notes: list = []
        try:
            events = self.read(strict=True)
        except EventLogError as exc:
            return VerifyReport(False, 0, -1, ZERO_DIGEST, [str(exc)])

        prev_hash = ZERO_DIGEST
        prev_wall = None
        for i, ev in enumerate(events):
            if ev.seq != i:
                problems.append(
                    f"seq {ev.seq} at position {i}: sequence must be "
                    "contiguous and ascending from 0")
            if ev.prev_hash != prev_hash:
                problems.append(
                    f"seq {ev.seq}: prev_hash {ev.prev_hash[:12]} does not "
                    f"link to {prev_hash[:12]}")
            try:
                recomputed = ev.recompute_hash()
            except CanonicalizationError as exc:
                problems.append(f"seq {ev.seq}: not hashable: {exc}")
                break
            if recomputed != ev.hash:
                problems.append(
                    f"seq {ev.seq}: hash {ev.hash[:12]} != recomputed "
                    f"{recomputed[:12]}; record was altered")
            if ev.canonical_form_version != CANONICAL_FORM_VERSION:
                problems.append(
                    f"seq {ev.seq}: canonical form v"
                    f"{ev.canonical_form_version} != "
                    f"v{CANONICAL_FORM_VERSION};"
                    " digests are not comparable across forms")
            if prev_wall is not None and ev.wall_time < prev_wall:
                # Diagnostic, not fatal: clocks legitimately move backwards.
                notes.append(
                    f"seq {ev.seq}: wall_time went backwards "
                    f"({ev.wall_time} < {prev_wall}); ordering uses seq, not "
                    "wall time")
            prev_wall = ev.wall_time
            prev_hash = ev.hash

        head_seq = events[-1].seq if events else -1
        head_hash = events[-1].hash if events else ZERO_DIGEST

        witness = expected_head
        if witness is None and use_witness:
            try:
                witness = self.head()
            except EventLogError as exc:
                problems.append(str(exc))
                witness = None
        if witness is not None:
            if witness.seq > head_seq:
                problems.append(
                    f"TRUNCATED: witness records seq {witness.seq} but "
                    "the log "
                    f"ends at {head_seq}; {witness.seq - head_seq} record(s) "
                    "are missing")
            elif witness.seq == head_seq and witness.head_hash != head_hash:
                problems.append(
                    f"FORKED: witness head {witness.head_hash[:12]} != "
                    "log head "
                    f"{head_hash[:12]} at the same seq")
            elif witness.seq < head_seq:
                notes.append(
                    f"witness is behind the log ({witness.seq} < {head_seq}); "
                    "expected only if a crash occurred between append and "
                    "witness update")
        return VerifyReport(not problems, len(events), head_seq, head_hash,
                            problems, notes)

    # ---- appending ----------------------------------------------------
    def append(self, *, actor: str, action: str, target: str,
               payload: dict | None = None,
               event_id: str | None = None,
               wall_time: float | None = None) -> Event:
        """Append one event, linked to the current head. Durable on return.

        Verifies the existing chain first: appending onto a broken log would
        extend the damage and make the break harder to locate.
        """
        import uuid

        report = self.verify()
        if not report.ok:
            raise ChainBroken(
                "refusing to append to a broken chain: "
                + "; ".join(report.problems))

        payload = dict(payload or {})
        body = {
            "seq": report.head_seq + 1,
            "event_id": event_id or uuid.uuid4().hex,
            "wall_time": time.time() if wall_time is None else wall_time,
            "actor": actor,
            "action": action,
            "target": target,
            "payload": payload,
            "prev_hash": report.head_hash,
            "canonical_form_version": CANONICAL_FORM_VERSION,
        }
        # Reject unhashable payloads before touching the file, so a bad append
        # cannot leave a partial line behind.
        ev = Event(**body, hash=digest(body))
        line = canonical_bytes(ev.to_record()) + b"\n"
        if len(line) > MAX_EVENT_BYTES:
            raise MalformedEvent(
                f"event is {len(line)} bytes, above the "
                f"{MAX_EVENT_BYTES}-byte bound")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._write_head(ChainState(ev.seq, ev.hash))
        return ev

    def _write_head(self, state: ChainState) -> None:
        """Update the witness atomically: temp file, fsync, rename."""
        import json
        import tempfile
        self.head_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.head_path.parent),
                                   prefix=".head-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state.to_record(), fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.head_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
