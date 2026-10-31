"""Archived history segments: the log sealed in pieces, each checkable alone.

WHY. The log is one file, verified from its first record. A reader who
holds part of an old history -- the records around one decision, kept in
cold storage -- and a checkpoint of a later tree that witnesses cosigned
wants to know that those records are the log's own, at their positions,
without the rest of the history and without trusting whoever kept them. A
hash chain cannot say that from a piece: its records link backwards, so a
piece is only as good as every record after it, up to a head somebody
vouched for. The Merkle tree over the same records (:mod:`transparency`)
can.

A SEGMENT is the canonical lines of the records start..end-1, the bytes the
log's own reader accepts, sealed with:

* ``start`` and ``end``;
* ``digest``, the SHA-256 of those bytes: a segment is stored and found by
  its content;
* ``frontier``, the roots of the perfect subtrees that cover the first
  ``start`` records: at most log2(start) + 1 hashes;
* ``root``, the tree root of the first ``end`` records;
* ``prev_hash``, its first record's chain link, and ``last_hash``, the hash
  of its last record.

A MANIFEST lists an archive's segments in order. It is not trusted: every
hash in it is recomputed from the segments' bytes, and only the last root
needs anything from outside, a consistency proof to a checkpoint.

WHAT EACH CHECK ESTABLISHES.

:func:`verify_segment`, given a checkpoint's size and root and a
consistency proof from the segment's end: each line is a record the log's
reader would accept (one canonical line, every field typed, its hash
recomputed); the records are contiguous, each links to the one before; and
they are the records at positions start..end-1 of the history whose tree
of ``size`` records has that root. That is the inclusion of a range, at a
cost beyond the records themselves of O(log size) hashes. It does not
establish anything about the records before ``start`` beyond what the
frontier commits to, including whether they form a valid chain: a tree
commits to records, not to their being one, and a witness of trees never
sees the records.

:func:`verify_archive`: all of that for every segment, and that the
segments run from the first record without a gap or an overlap, each
frontier the one the records before it give, each first link the hash
before it. That is a verification from genesis of the chain the archive
holds, and of its consistency with the checkpoint. The report says how far
the archive reaches: an archive of a prefix of the checkpoint's history
verifies AS a prefix, and ``complete`` is then False.

Neither check establishes:

* where the checkpoint came from. A checkpoint handed over with the archive
  is its keeper's word. It establishes the history when a quorum of
  witnesses the reader trusts cosigned it (:mod:`witness`).
* who wrote the records. An archive carries no attestations, so it
  authenticates no one. A history that declares AUTHENTICATED_REQUIRED is
  checked like any other, and none of its records is handed back: the log's
  own reader hands no event of such a history to a reader without an
  authenticator, and an archive is not a way around that. :func:`restore`
  writes the records back to a log, and :meth:`EventLog.read_verified`, with
  the history's authenticator, reads them. A segment does not hold the first
  record, which declares the profile, so :func:`verify_segment` is told it,
  as a deployment pins the profile of a log it opens.

ARCHIVING DISCARDS NOTHING. :func:`seal` reads a verified log and leaves it
as it was. Nothing here truncates, compacts or rewrites a log. An archive is
a checkable copy, and replay still runs from the first record.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import transparency as T
from .canonical import ZERO_DIGEST, canonical_bytes, is_digest
from .events import (
    PROFILE_UNAUTHENTICATED_LEGACY, PROFILES, Event, EventLog,
    MalformedEvent, _parse_line,
)

SCHEMA = "event-archive/1"

_FIELDS = frozenset({"start", "end", "digest", "frontier", "root",
                     "prev_hash", "last_hash"})


class ArchiveError(Exception):
    """An archive, or a segment of one, that does not check."""


def _count(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArchiveError(f"{what} {value!r} is not a count of records")
    return value


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    digest: str
    frontier: tuple
    root: str
    prev_hash: str
    last_hash: str

    @property
    def name(self) -> str:
        return f"segment {self.start}..{self.end - 1}"

    def to_record(self) -> dict:
        return {"start": self.start, "end": self.end, "digest": self.digest,
                "frontier": list(self.frontier), "root": self.root,
                "prev_hash": self.prev_hash, "last_hash": self.last_hash}

    @classmethod
    def from_record(cls, rec: Any) -> "Segment":
        if not isinstance(rec, dict) or set(rec) != _FIELDS:
            raise ArchiveError("a segment record has exactly the fields "
                               + ", ".join(sorted(_FIELDS)))
        start = _count(rec["start"], "start")
        end = _count(rec["end"], "end")
        if end <= start:
            raise ArchiveError(f"a segment from {start} to {end} holds no "
                               "records")
        front = rec["frontier"]
        if not isinstance(front, list) or \
                not all(is_digest(h) for h in front):
            raise ArchiveError("a frontier is a list of sha256 digests")
        if len(front) != bin(start).count("1"):
            raise ArchiveError(f"{len(front)} hashes are not the frontier of "
                               f"{start} records")
        for name in ("digest", "root", "prev_hash", "last_hash"):
            if not is_digest(rec[name]):
                raise ArchiveError(f"{name} is not a sha256 digest")
        if start == 0 and rec["prev_hash"] != ZERO_DIGEST:
            raise ArchiveError("the first record links to nothing")
        return cls(start, end, rec["digest"], tuple(front), rec["root"],
                   rec["prev_hash"], rec["last_hash"])


def _segments(manifest: Any) -> list:
    if not isinstance(manifest, dict) or \
            set(manifest) != {"schema", "segments"}:
        raise ArchiveError("a manifest has exactly a schema and its segments")
    if manifest["schema"] != SCHEMA:
        raise ArchiveError(f"schema {manifest['schema']!r} is not {SCHEMA}")
    segs = manifest["segments"]
    if not isinstance(segs, list) or not segs:
        raise ArchiveError("a manifest lists at least one segment")
    return [Segment.from_record(rec) for rec in segs]


def _seal(events: list, every: int) -> tuple:
    every = _count(every, "a segment of")
    if every < 1:
        raise ArchiveError("a segment holds at least one record")
    if not events:
        raise ArchiveError("an empty log has nothing to seal")
    segments, blobs = [], {}
    front: list = []
    prev = ZERO_DIGEST
    for start in range(0, len(events), every):
        chunk = events[start:start + every]
        end = start + len(chunk)
        data = b"".join(canonical_bytes(e.to_record()) + b"\n"
                        for e in chunk)
        after = T.extend(front, start, T.entries_of(chunk))
        seg = Segment(start, end, hashlib.sha256(data).hexdigest(),
                      tuple(h.hex() for h in front),
                      T.frontier_root(after, end).hex(), prev,
                      chunk[-1].hash)
        segments.append(seg.to_record())
        blobs[seg.digest] = data
        front, prev = after, chunk[-1].hash
    return {"schema": SCHEMA, "segments": segments}, blobs


def seal(log: EventLog, every: int, *, expected_head=None) -> tuple:
    """Seal a log's records into segments of ``every`` records.

    Returns ``(manifest, blobs)``: the manifest, and each segment's bytes by
    its digest. The log is read once, verified from its first record by its
    own reader, with its own authenticator, and left as it was. A log that
    does not verify is refused, not sealed."""
    report, events = log.read_verified(expected_head=expected_head)
    if not report.ok:
        raise ArchiveError("the log does not verify: "
                           + "; ".join(report.problems))
    return _seal(events, every)


def _records(seg: Segment, data: Any) -> list:
    """The segment's records, each read as the log's reader reads a line,
    and the chain they make from its first link."""
    if not isinstance(data, bytes) or \
            hashlib.sha256(data).hexdigest() != seg.digest:
        raise ArchiveError(f"{seg.name}: its bytes are not the ones sealed")
    if not data.endswith(b"\n"):
        raise ArchiveError(f"{seg.name}: its last line has no newline")
    lines = [line + b"\n" for line in data[:-1].split(b"\n")]
    if len(lines) != seg.end - seg.start:
        raise ArchiveError(f"{seg.name}: {len(lines)} lines, not "
                           f"{seg.end - seg.start} records")
    events: list = []
    problems: list = []
    notes: list = []
    prev, prev_wall = seg.prev_hash, None
    for i, line in enumerate(lines):
        where = f"{seg.name}, record {seg.start + i}"
        try:
            rec = _parse_line(line, where)
            if rec is None:
                raise MalformedEvent(f"{where}: a blank line")
            ev = Event(**rec)
        except (MalformedEvent, TypeError) as exc:
            raise ArchiveError(str(exc)) from exc
        EventLog._check_link(ev, seg.start + i, prev, prev_wall, problems,
                             notes)
        if problems:
            raise ArchiveError(f"{where}: " + "; ".join(problems))
        events.append(ev)
        prev, prev_wall = ev.hash, ev.wall_time
    if prev != seg.last_hash:
        raise ArchiveError(f"{seg.name}: its last record is not the one "
                           "sealed")
    return events


def _tree_after(seg: Segment, events: list) -> tuple:
    """The frontier after the segment, and its root, recomputed: the root
    the manifest states is checked, never used."""
    after = T.extend([bytes.fromhex(h) for h in seg.frontier], seg.start,
                     T.entries_of(events))
    end_root = T.frontier_root(after, seg.end)
    if end_root.hex() != seg.root:
        raise ArchiveError(f"{seg.name}: its records and its frontier give "
                           "another root than the one sealed")
    return after, end_root


def _check_checkpoint(end: int, end_root: bytes, size: Any, root: Any,
                      proof: Any, what: str) -> None:
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ArchiveError(f"{size!r} is not a checkpoint's size")
    if not is_digest(root):
        raise ArchiveError("a checkpoint's root is a sha256 digest")
    if not isinstance(proof, list) or not all(is_digest(h) for h in proof):
        raise ArchiveError("a consistency proof is a list of sha256 digests")
    if end > size:
        raise ArchiveError(f"{what} reaches record {end - 1}, and a "
                           f"checkpoint of {size} records does not cover it")
    if not T.verify_consistency(end, size, end_root, bytes.fromhex(root),
                                [bytes.fromhex(h) for h in proof]):
        raise ArchiveError(f"{what} is not in the tree of {size} records "
                           "with that root")


def _declared(events: list) -> str:
    """The profile a history's first records declare, by the log's own
    rule. It reads only the events, so it is asked without a log."""
    problems: list = []
    rule = EventLog._profile_of
    # the log's own rule, asked without a log: it reads only the events
    profile = rule(None, events, problems)  # type: ignore[arg-type]
    if profile is None:
        raise ArchiveError("; ".join(problems))
    return profile


def verify_segment(record: Any, data: Any, *, size: Any, root: Any,
                   proof: Any, profile: Any) -> list:
    """One segment, verified against a checkpoint of ``size`` records with
    ``root``, given a consistency proof (hex) from the segment's end to
    ``size``. ``profile`` is the one the history declares. The segment's
    records are returned when it requires no authentication, and none when
    it does. Raises :class:`ArchiveError`."""
    if profile not in PROFILES:
        raise ArchiveError(f"{profile!r} is not a profile a history "
                           "declares")
    seg = Segment.from_record(record)
    events = _records(seg, data)
    if seg.start == 0 and _declared(events) != profile:
        raise ArchiveError(f"the history's first record declares "
                           f"{_declared(events)}, not {profile}")
    _, end_root = _tree_after(seg, events)
    _check_checkpoint(seg.end, end_root, size, root, proof, seg.name)
    return events if profile == PROFILE_UNAUTHENTICATED_LEGACY else []


@dataclass(frozen=True)
class ArchiveReport:
    #: Records 0..covered-1 are verified, from the first record.
    covered: int
    #: The checkpoint's size.
    size: int
    #: The security profile the history's first record declares.
    profile: str
    #: The verified records, or none when the history requires
    #: authentication: the archive authenticates no one, and those are read
    #: after :func:`restore`, by the log's own reader.
    events: tuple

    @property
    def complete(self) -> bool:
        return self.covered == self.size


def verify_archive(manifest: Any, read: Callable[[str], bytes], *,
                   size: Any, root: Any, proof: Any) -> ArchiveReport:
    """Every segment of an archive, from the first record, against a
    checkpoint of ``size`` records with ``root``, given a consistency proof
    (hex) from the archive's end. ``read`` returns a segment's bytes by its
    digest. Raises :class:`ArchiveError`."""
    events, profile = _verified(manifest, read, size, root, proof)
    return ArchiveReport(len(events), size, profile, tuple(
        events if profile == PROFILE_UNAUTHENTICATED_LEGACY else ()))


def _verified(manifest: Any, read: Callable[[str], bytes], size: Any,
              root: Any, proof: Any) -> tuple:
    events: list = []
    front: list = []
    prev = ZERO_DIGEST
    end_root = T.EMPTY_ROOT
    for seg in _segments(manifest):
        if seg.start != len(events):
            raise ArchiveError(f"{seg.name} follows record "
                               f"{len(events) - 1}: a gap or an overlap")
        if list(seg.frontier) != [h.hex() for h in front]:
            raise ArchiveError(f"{seg.name}: its frontier is not the one the "
                               "records before it give")
        if seg.prev_hash != prev:
            raise ArchiveError(f"{seg.name}: its first link is not the hash "
                               "of the record before it")
        try:
            data = read(seg.digest)
        except (KeyError, OSError) as exc:
            raise ArchiveError(f"{seg.name}: its bytes are missing") from exc
        chunk = _records(seg, data)
        front, end_root = _tree_after(seg, chunk)
        events.extend(chunk)
        prev = chunk[-1].hash
    _check_checkpoint(len(events), end_root, size, root, proof,
                      "the archive")
    return events, _declared(events)


def restore(manifest: Any, read: Callable[[str], bytes], path: Path | str,
            *, size: Any, root: Any, proof: Any) -> Path:
    """Write a verified archive back to a log at ``path``, which must not
    exist: the verified records as their canonical lines, the bytes the
    log's reader reads. The restored log is then read as any log is, from
    its first record, with its authenticator. Returns its path."""
    events, _ = _verified(manifest, read, size, root, proof)
    data = b"".join(canonical_bytes(e.to_record()) + b"\n"
                    for e in events)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return Path(path)
