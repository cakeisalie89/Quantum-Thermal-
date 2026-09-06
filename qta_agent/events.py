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

COST, AND WHY IT IS NOT A SIDE ISSUE

:meth:`append` used to verify the ENTIRE chain before every write, which is
O(n) per append and O(n^2) over a history. Measured: 2.1 ms per append at 100
records, 10.4 ms at 800, with each doubling of n roughly quadrupling total
time. At a hundred thousand records an append would cost over a second, and
building such a log would take most of a day.

That is a security property, not a performance footnote. A check whose cost
grows without bound is a check that gets switched off, and the same defect had
already been found once in this package's checkpointing.

So an :class:`EventLog` verifies the whole chain on its FIRST append and
verifies only the tail after an :class:`Anchor` thereafter. The guarantee that
buys, stated exactly:

  * no writer extends a chain that was already broken when it started;
  * no writer extends damage that appeared at or after its anchor, including
    damage from another process interleaved between two of its own appends;
  * damage done to the PREFIX during this writer's lifetime is not caught by
    its appends -- it is caught by :meth:`verify`, which every reader performs
    before projecting, and by the next writer's first append.

``full_verify_every`` restores periodic whole-chain checking for a deployment
that wants the middle case closed at a quadratic price, and defaults to off
because that price is the one that ends up disabling the check entirely.

CONCURRENT WRITERS

An append is read-then-write: it verifies the chain to learn the head, then
writes a record linked to it. Two writers doing that at once both read the
same head and both write a record claiming the same ``seq``, which does not
merely lose one of them -- it CORRUPTS the log. Every later append then fails
against a broken chain, so a moment of concurrency ends the log's life.

That is not hypothetical; it was measured. Four processes appending to one log
produced four records at seq 0, and 56 of the 60 appends afterwards were
refused against the chain they had broken.

:meth:`append` and :meth:`append_verified` therefore hold an exclusive
``flock`` on a sidecar lock file across the whole verify-and-write section.
What that does and does not give you:

  * it serializes every writer that goes through this class, on one host and
    on a local filesystem;
  * ``flock`` is ADVISORY -- a process that opens the file and writes to it
    directly is not stopped, and nothing here can stop it;
  * ``flock`` semantics over NFS and some network filesystems are unreliable,
    so a log shared that way is not protected by this;
  * on a platform without ``fcntl`` the append REFUSES rather than proceeding
    unlocked, because an unlocked append is the failure above.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

try:                                    # POSIX advisory locking
    import fcntl
except ImportError:                     # pragma: no cover - non-POSIX
    fcntl = None

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

    WHAT IT BOUNDS, EXACTLY. The witness is written AFTER the append it
    describes, so it is a lower bound on the history rather than a mirror
    of it: truncation back to or below the witness's position is detected,
    and events appended after the last witness update are not covered. A
    process killed between the two leaves the witness one event behind,
    which verify() reports as a note rather than a problem -- the safe
    direction, since the alternative ordering would let the witness claim
    history the log never received.
    """
    seq: int
    head_hash: str

    def to_record(self) -> dict:
        return {"seq": self.seq, "head_hash": self.head_hash}


@dataclass(frozen=True)
class VerifyReport:
    """Outcome of a chain verification. Structured, never a bare bool.

    ``prefix_verified`` is the field that keeps an incremental verification
    honest. :meth:`EventLog.verify_from` re-checks only the records after a
    caller-supplied anchor and leaves this False, with
    ``unverified_through`` naming the last seq it did not look at. A report
    that says ``ok`` while carrying ``prefix_verified=False`` is saying
    something strictly weaker than one that does not, and callers that treat
    the two alike are the reason this is a field rather than a docstring.
    """
    ok: bool
    count: int
    head_seq: int
    head_hash: str
    problems: list = field(default_factory=list)
    #: Non-fatal observations: wall-clock regressions, unusual gaps in time.
    notes: list = field(default_factory=list)
    #: False when records before an anchor were trusted rather than re-checked.
    prefix_verified: bool = True
    #: Last seq that was trusted without being re-checked; -1 when none were.
    unverified_through: int = -1

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


@dataclass(frozen=True)
class Anchor:
    """A caller's assertion that the log is verified through ``seq``.

    Carries byte offsets so verification of the tail does not have to read the
    prefix to reach it. The offsets are a shortcut, never a trust input: the
    record found at ``record_offset`` must parse and must hash to
    ``head_hash``, or the anchor is refused. A rewritten log moves those
    bytes, the seek lands mid-record, and the check fails closed.
    """
    seq: int
    head_hash: str
    #: Byte offset where the record at ``seq`` begins.
    record_offset: int
    #: Byte offset just past that record, where the tail starts.
    next_offset: int

    def to_record(self) -> dict:
        return {"seq": self.seq, "head_hash": self.head_hash,
                "record_offset": self.record_offset,
                "next_offset": self.next_offset}

    @property
    def chain_state(self) -> ChainState:
        return ChainState(self.seq, self.head_hash)


class EventLog:
    """A durable, append-only, hash-chained log stored as JSON Lines."""

    #: Seconds an append will wait for the writer lock before refusing. A
    #: blocking wait with no bound is indistinguishable from a hang, and "the
    #: process is stuck" is a much worse thing to debug than "the lock was
    #: held for 30 seconds by pid N".
    LOCK_TIMEOUT_S = 30.0

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.head_path = self.path.with_suffix(self.path.suffix + ".head")
        #: A SIDECAR, not the log itself: the lock must outlive a log that is
        #: rotated, truncated or not yet created, and locking a file that does
        #: not exist is not possible.
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        #: Set by the first append and advanced by each one after it. Held per
        #: OBJECT, not per path: a new EventLog on the same file starts with
        #: no anchor and therefore full-verifies once, which is what makes
        #: "no writer extends an already-broken chain" true per process.
        self._anchor: Anchor | None = None
        self._appends_since_full = 0
        #: 0 disables periodic whole-chain verification during append. See
        #: the module docstring for exactly which case that leaves open.
        self.full_verify_every = 0

    @contextlib.contextmanager
    def exclusive(self):
        """Hold the writer lock for the whole verify-and-append section.

        Exposed rather than private because a caller performing a multi-event
        transaction needs the same lock, and reaching for a private method is
        how a second, subtly different locking discipline gets written.
        """
        if fcntl is None:                       # pragma: no cover - non-POSIX
            raise EventLogError(
                "appending needs POSIX advisory locking (fcntl), which this "
                "platform does not provide. Refusing rather than appending "
                "unlocked: two unlocked writers corrupt the chain rather "
                "than losing a record.")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.LOCK_TIMEOUT_S
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise EventLogError(
                            f"could not take the writer lock on "
                            f"{self.lock_path} within "
                            f"{self.LOCK_TIMEOUT_S}s; another writer is "
                            "holding it") from None
                    time.sleep(0.005)
            yield
        finally:
            # Closing the descriptor releases the flock -- that is the POSIX
            # guarantee, and it is what makes this safe against an exception
            # inside the block AND against the process dying. An explicit
            # LOCK_UN before the close would be a line nothing could ever
            # observe, which is worse than no line at all: it reads as a
            # safeguard and defends nothing.
            os.close(fd)

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
            if not self._check_link(ev, i, prev_hash, prev_wall,
                                    problems, notes):
                break
            prev_wall = ev.wall_time
            prev_hash = ev.hash

        head_seq = events[-1].seq if events else -1
        head_hash = events[-1].hash if events else ZERO_DIGEST
        self._check_witness(head_seq, head_hash, expected_head, use_witness,
                            problems, notes)
        return VerifyReport(not problems, len(events), head_seq, head_hash,
                            problems, notes)

    # The two verification paths -- whole-chain and from-an-anchor -- share
    # these. Two copies of "what makes a record acceptable" would drift, and
    # the incremental path is exactly where a weakened copy would go unnoticed.
    @staticmethod
    def _check_link(ev, expected_seq: int, prev_hash: str, prev_wall,
                    problems: list, notes: list) -> bool:
        """Check one record against its expected position. False = stop."""
        if ev.seq != expected_seq:
            problems.append(
                f"seq {ev.seq} at position {expected_seq}: sequence must be "
                "contiguous and ascending from 0")
        if ev.prev_hash != prev_hash:
            problems.append(
                f"seq {ev.seq}: prev_hash {ev.prev_hash[:12]} does not "
                f"link to {prev_hash[:12]}")
        try:
            recomputed = ev.recompute_hash()
        except CanonicalizationError as exc:
            problems.append(f"seq {ev.seq}: not hashable: {exc}")
            return False
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
        return True

    def _check_witness(self, head_seq: int, head_hash: str,
                       expected_head, use_witness: bool,
                       problems: list, notes: list) -> None:
        """Compare the log's head against the independently held witness."""
        witness = expected_head
        if witness is None and use_witness:
            try:
                witness = self.head()
            except EventLogError as exc:
                problems.append(str(exc))
                witness = None
        if witness is None:
            return
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

    # ---- appending ----------------------------------------------------
    def anchor_at(self, seq: int) -> "Anchor":
        """Build an anchor for ``seq`` by reading the log once.

        Deliberately the slow path. An anchor is only worth trusting because
        something verified the log to produce it, so producing one costs a
        full pass; the saving comes from every use afterwards.
        """
        if seq < 0:
            raise EventLogError(f"cannot anchor at negative seq {seq}")
        offset = 0
        with self.path.open("rb") as fh:
            for raw in fh:
                start = offset
                offset += len(raw)
                if not raw.strip():
                    continue
                rec = json.loads(raw.decode("utf-8"))
                _validate_field_types(rec, f"offset {start}")
                if rec["seq"] == seq:
                    ev = Event(**rec)
                    if ev.recompute_hash() != ev.hash:
                        raise ChainBroken(
                            f"seq {seq}: record does not hash to its own "
                            "stored hash; refusing to anchor on it")
                    return Anchor(seq, ev.hash, start, offset)
        raise EventLogError(f"no record at seq {seq}")

    def verify_from(self, anchor: "Anchor", *,
                    use_witness: bool = True) -> VerifyReport:
        """Verify only the records after ``anchor``, TRUSTING the prefix.

        This is strictly weaker than :meth:`verify` and the returned report
        says so: ``prefix_verified`` is False and ``unverified_through``
        names the last seq that was taken on faith. Tampering with records
        before the anchor is invisible here -- by construction, since not
        reading them is the entire point -- and only :meth:`verify` will find
        it.

        The anchor itself is not trusted blindly: the record at its offset
        must parse, must sit at the anchor's seq, and must hash to the
        anchor's hash. Any disagreement raises rather than silently falling
        back to a full pass, because a caller who asked for the cheap check
        and received the expensive one has been given a cost profile they did
        not choose -- and, worse, a caller who asked for the cheap check and
        received a *successful* one has no way to tell which they got.
        """
        problems: list = []
        notes: list = []

        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            raise ChainBroken(
                f"anchor claims seq {anchor.seq} but the log does not "
                "exist") from None
        if anchor.next_offset > size:
            raise ChainBroken(
                f"TRUNCATED: anchor ends at byte {anchor.next_offset} but the "
                f"log is {size} bytes; {anchor.next_offset - size} byte(s) "
                "are missing")

        with self.path.open("rb") as fh:
            fh.seek(anchor.record_offset)
            raw = fh.readline()
            if anchor.record_offset + len(raw) != anchor.next_offset:
                raise ChainBroken(
                    f"anchor at seq {anchor.seq} does not describe the bytes "
                    "now at its offset; the log was rewritten")
            try:
                rec = json.loads(raw.decode("utf-8"))
                _validate_field_types(rec, f"anchor seq {anchor.seq}")
                anchored = Event(**rec)
            except (UnicodeDecodeError, ValueError, TypeError,
                    EventLogError) as exc:
                raise ChainBroken(
                    f"anchor at seq {anchor.seq} does not point at a valid "
                    f"record: {exc}") from exc
            if anchored.seq != anchor.seq:
                raise ChainBroken(
                    f"anchor claims seq {anchor.seq} but the record there is "
                    f"seq {anchored.seq}")
            if anchored.hash != anchor.head_hash:
                raise ChainBroken(
                    f"anchor expects hash {anchor.head_hash[:12]} at seq "
                    f"{anchor.seq}, found {anchored.hash[:12]}")
            if anchored.recompute_hash() != anchored.hash:
                raise ChainBroken(
                    f"seq {anchor.seq}: the anchored record does not hash to "
                    "its own stored hash")

            tail = self._read_tail(fh, anchor.next_offset, problems)

        prev_hash = anchor.head_hash
        prev_wall = anchored.wall_time
        expected = anchor.seq + 1
        for ev in tail:
            if not self._check_link(ev, expected, prev_hash, prev_wall,
                                    problems, notes):
                break
            prev_wall = ev.wall_time
            prev_hash = ev.hash
            expected += 1

        head_seq = tail[-1].seq if tail else anchor.seq
        head_hash = tail[-1].hash if tail else anchor.head_hash
        self._check_witness(head_seq, head_hash, None, use_witness,
                            problems, notes)

        return VerifyReport(not problems, len(tail), head_seq, head_hash,
                            problems, notes, prefix_verified=False,
                            unverified_through=anchor.seq)

    def read_from(self, anchor: "Anchor") -> list:
        """Parse the records after ``anchor`` without reading the prefix.

        No verification: callers pair this with :meth:`verify_from`, which is
        the thing that decides whether these records are acceptable. Kept
        separate so a caller cannot get the parsing without having chosen a
        verification, or vice versa, by accident.
        """
        with self.path.open("rb") as fh:
            return self._read_tail(fh, anchor.next_offset, [])

    def _read_tail(self, fh, offset: int, problems: list) -> list:
        events: list = []
        fh.seek(offset)
        for raw in fh:
            if len(raw) > MAX_EVENT_BYTES:
                raise MalformedEvent(
                    f"{len(raw)} bytes exceeds the {MAX_EVENT_BYTES}-byte "
                    "bound")
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise MalformedEvent(
                    f"unparseable record after the anchor "
                    f"({type(exc).__name__}); if this is the final line the "
                    "log was truncated mid-append") from exc
            if not isinstance(rec, dict):
                raise MalformedEvent(
                    f"record is {type(rec).__name__}, not an object")
            _validate_field_types(rec, "after anchor")
            events.append(Event(**rec))
        return events

    def append(self, *, actor: str, action: str, target: str,
               payload: dict | None = None,
               event_id: str | None = None,
               wall_time: float | None = None) -> Event:
        """Append one event, linked to the current head. Durable on return.

        Verifies the existing chain first: appending onto a broken log would
        extend the damage and make the break harder to locate.
        """
        with self.exclusive():
            anchor = self._anchor
            if anchor is not None and self._needs_full_verify():
                anchor = None
            if anchor is None:
                report = self.verify()
            else:
                try:
                    report = self.verify_from(anchor)
                except ChainBroken:
                    # The anchor no longer describes the bytes at its offset:
                    # the log was rewritten, rotated or truncated. Falling
                    # back to a FULL verify is strictly stronger, not weaker,
                    # and it will refuse the append if the damage is real.
                    self._anchor = None
                    report = self.verify()
            if not report.ok:
                raise ChainBroken(
                    "refusing to append to a broken chain: "
                    + "; ".join(report.problems))
            ev, new_anchor = self._write_event(
                report.head_seq, report.head_hash, actor=actor, action=action,
                target=target, payload=payload, event_id=event_id,
                wall_time=wall_time)
            self._anchor = new_anchor
            self._appends_since_full = (
                0 if report.prefix_verified else self._appends_since_full + 1)
        return ev

    def _needs_full_verify(self) -> bool:
        """True when the periodic whole-chain check is due, if enabled."""
        return (self.full_verify_every > 0
                and self._appends_since_full >= self.full_verify_every)

    def append_verified(self, anchor: "Anchor", *, actor: str, action: str,
                        target: str, payload: dict | None = None,
                        event_id: str | None = None,
                        wall_time: float | None = None) -> tuple:
        """Append, re-checking only the records after ``anchor``.

        Returns ``(event, new_anchor)``, where the new anchor covers the
        record just written -- so a caller appending in a loop pays O(1) per
        append instead of re-hashing the whole log each time. That quadratic
        cost is not academic: it is the mechanism by which a chain check gets
        switched off in practice, and a switched-off check is worse than a
        cheap one because nothing records that it stopped running.

        The prefix is TRUSTED, exactly as in :meth:`verify_from`. This is a
        separate method rather than a keyword on :meth:`append` so that the
        weaker guarantee cannot be selected by accident, and so the default
        stays the strong one.
        """
        with self.exclusive():
            report = self.verify_from(anchor)
            if not report.ok:
                raise ChainBroken(
                    "refusing to append to a broken chain: "
                    + "; ".join(report.problems))
            out = self._write_event(
                report.head_seq, report.head_hash, actor=actor, action=action,
                target=target, payload=payload, event_id=event_id,
                wall_time=wall_time)
            self._anchor = out[1]
            return out

    def _write_event(self, head_seq: int, head_hash: str, *, actor: str,
                     action: str, target: str, payload: dict | None,
                     event_id: str | None, wall_time: float | None) -> tuple:
        """Build, bound-check and durably write one record. One writer."""
        import uuid

        payload = dict(payload or {})
        body = {
            "seq": head_seq + 1,
            "event_id": event_id or uuid.uuid4().hex,
            "wall_time": time.time() if wall_time is None else wall_time,
            "actor": actor,
            "action": action,
            "target": target,
            "payload": payload,
            "prev_hash": head_hash,
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
            # Offsets are taken from the handle, not from a prior stat: an
            # append-mode write lands at the true end of file, which a stat
            # taken earlier may no longer describe.
            start = fh.tell()
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
            end = fh.tell()
        self._write_head(ChainState(ev.seq, ev.hash))
        return ev, Anchor(ev.seq, ev.hash, start, end)

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
