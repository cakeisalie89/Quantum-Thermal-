"""Checkpoints: caching a verification result without caching trust.

THE DEFECT THIS FIXES

``EventLog.append`` verifies the whole chain before every append, and
``AuthorityStore.load`` verifies it again. Both are O(n) in the log's length,
so N appends cost O(N**2) hashing. For a long-running agent that is not a
theoretical concern: it is the mechanism by which an integrity check gets
switched off. Verification that grows without bound is verification that
someone eventually skips, and the skip is never recorded anywhere.

WHAT A CHECKPOINT IS, EXACTLY

A cached verification result. It says: *at seq K the log's head hash was H,
and a full verification passed at the time this was written.* Using it means
verifying only K+1..N and taking 0..K on faith.

WHAT IT IS NOT

An alternative source of truth. The log remains the truth; a checkpoint is a
statement about the log, and a false statement about the log is still just a
false statement -- it cannot make a forged record authoritative, because
anyone can re-run the full verification and find the disagreement.

The design consequence is that **every use of a checkpoint is recorded as a
weaker result**. :meth:`EventLog.verify_from` returns a report with
``prefix_verified=False``; nothing here can produce a report that claims a
full verification it did not perform.

WHAT AUTHENTICATES A CHECKPOINT

Nothing in this module. Read that sentence again before relying on one.

The checkpoint carries a hash over its own body, which detects accidental
corruption -- a truncated write, a bad disk. It does **not** detect a
deliberately rewritten checkpoint: anyone who can write the file can also
recompute the hash. Authentication requires a signature or an external
witness, which is a separate concern and deliberately not faked here with a
self-hash dressed up as one.

So a checkpoint is exactly as trustworthy as the directory it sits in. That
is usually fine -- the same process wrote the log -- and it is never fine for
an adversary model where the filesystem is hostile. In that model, use
:meth:`EventLog.verify`, which needs no checkpoint and trusts nothing.

ROLLBACK

An old checkpoint is safe, only slower: verifying from seq 5 of a 500-record
log re-checks 495 records instead of 0. A checkpoint *ahead* of the log is not
safe and not slow -- it means records were removed, and it is refused. That is
the same signal the separately-witnessed head gives, arrived at independently,
which is why both exist.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .canonical import CANONICAL_FORM_VERSION, digest, is_digest
from .events import Anchor, ChainBroken, EventLog, EventLogError

#: Fields covered by a checkpoint's own hash. ``hash`` is excluded; everything
#: else is included, so a field added later without being listed here would be
#: unhashed -- which is why :func:`_validate` refuses unknown fields outright.
_HASHED_FIELDS = (
    "seq", "head_hash", "record_offset", "next_offset", "state_digest",
    "created", "canonical_form_version", "full_verification",
)


class CheckpointError(EventLogError):
    """Base class. Every failure here is fail-closed."""


class CheckpointCorrupt(CheckpointError):
    """The checkpoint does not hash to its own stored hash, or is malformed."""


class CheckpointMismatch(CheckpointError):
    """The checkpoint does not describe the log it was given."""


class CheckpointAheadOfLog(CheckpointMismatch):
    """The checkpoint names a seq the log does not reach: records are gone."""


@dataclass(frozen=True)
class Checkpoint:
    """A verified position in a log, with the offsets to resume from."""
    seq: int
    head_hash: str
    record_offset: int
    next_offset: int
    #: Opaque digest of whatever projection the writer wanted to pin. This
    #: module never interprets it -- ``store.py`` fills it with the authority
    #: projection's digest, and a different caller could pin something else.
    state_digest: str | None
    created: float
    canonical_form_version: int
    #: Whether a full chain verification passed when this was written. A
    #: checkpoint written without one is recorded as such rather than
    #: rejected, so that the weaker provenance travels with it.
    full_verification: bool
    hash: str = ""

    def body(self) -> dict:
        return {f: getattr(self, f) for f in _HASHED_FIELDS}

    def recompute_hash(self) -> str:
        return digest(self.body())

    def to_record(self) -> dict:
        rec = self.body()
        rec["hash"] = self.hash
        return rec

    @property
    def anchor(self) -> Anchor:
        return Anchor(self.seq, self.head_hash, self.record_offset,
                      self.next_offset)


def _validate(rec: object, where: str) -> dict:
    if not isinstance(rec, dict):
        raise CheckpointCorrupt(
            f"{where}: checkpoint is {type(rec).__name__}, not an object")
    checks = (
        ("seq", int), ("head_hash", str), ("record_offset", int),
        ("next_offset", int), ("created", (int, float)),
        ("canonical_form_version", int), ("full_verification", bool),
        ("hash", str),
    )
    for name, typ in checks:
        if name not in rec:
            raise CheckpointCorrupt(f"{where}: missing field {name!r}")
        value = rec[name]
        ok = isinstance(value, typ)
        if ok and typ is not bool and isinstance(value, bool):
            ok = False          # bools are ints; a bool seq is not a seq
        if not ok:
            raise CheckpointCorrupt(
                f"{where}: field {name!r} is {type(value).__name__}")
    if "state_digest" not in rec:
        raise CheckpointCorrupt(f"{where}: missing field 'state_digest'")
    sd = rec["state_digest"]
    if sd is not None and not is_digest(sd):
        raise CheckpointCorrupt(
            f"{where}: state_digest is neither null nor a sha256 digest")
    for name in ("head_hash",):
        if not is_digest(rec[name]):
            raise CheckpointCorrupt(
                f"{where}: {name!r} is not a lowercase sha256 digest")
    if rec["seq"] < 0:
        raise CheckpointCorrupt(f"{where}: seq is negative")
    if rec["record_offset"] < 0 or rec["next_offset"] <= rec["record_offset"]:
        raise CheckpointCorrupt(
            f"{where}: byte offsets are not a forward range "
            f"({rec['record_offset']}, {rec['next_offset']})")
    unknown = set(rec) - set(_HASHED_FIELDS) - {"hash"}
    if unknown:
        # An unknown field would not be hashed, so it could carry unverified
        # content beside a valid digest -- the same rule the event log applies.
        raise CheckpointCorrupt(f"{where}: unhashed extra fields "
                                f"{sorted(unknown)}")
    return rec


def create(log: EventLog, *, state_digest: str | None = None,
           require_full_verification: bool = True) -> Checkpoint:
    """Verify ``log`` in full and return a checkpoint at its head.

    Refuses to checkpoint a log that does not verify. A checkpoint of a broken
    log would let every later verification skip past the break -- turning one
    corrupt record into a permanently invisible one.

    ``require_full_verification=False`` exists for the case where a caller has
    already verified by other means and is only recording the position. The
    resulting checkpoint records ``full_verification=False``, so the weaker
    provenance travels with it instead of being forgotten.
    """
    if state_digest is not None and not is_digest(state_digest):
        raise CheckpointError(
            f"state_digest must be a sha256 digest or None, got "
            f"{type(state_digest).__name__}")

    if require_full_verification:
        report = log.verify()
        if not report.ok:
            raise ChainBroken(
                "refusing to checkpoint a log that does not verify -- a "
                "checkpoint past a break makes the break invisible to every "
                "later verification: " + "; ".join(report.problems))
        if not report.prefix_verified:
            raise CheckpointError(
                "internal: log.verify() reported an unverified prefix")
        head_seq = report.head_seq
    else:
        head = log.head()
        if head is None:
            raise CheckpointError(
                "cannot checkpoint without a full verification and without a "
                "head witness; there is nothing to record a position from")
        head_seq = head.seq

    if head_seq < 0:
        raise CheckpointError("cannot checkpoint an empty log")

    anchor = log.anchor_at(head_seq)
    body = {
        "seq": anchor.seq,
        "head_hash": anchor.head_hash,
        "record_offset": anchor.record_offset,
        "next_offset": anchor.next_offset,
        "state_digest": state_digest,
        "created": time.time(),
        "canonical_form_version": CANONICAL_FORM_VERSION,
        "full_verification": bool(require_full_verification),
    }
    return Checkpoint(**body, hash=digest(body))


def verify_with(log: EventLog, cp: Checkpoint) -> "object":
    """Verify ``log`` from ``cp``, re-checking only what follows it.

    The returned report carries ``prefix_verified=False`` and
    ``unverified_through=cp.seq``. Callers that need a full result must call
    :meth:`EventLog.verify`; there is no argument to this function that
    upgrades its answer, because an API where the weak and strong results are
    the same type and the same call is an API where they get confused.
    """
    check_against(log, cp)
    return log.verify_from(cp.anchor)


def check_against(log: EventLog, cp: Checkpoint) -> None:
    """Raise unless ``cp`` could describe ``log``. Cheap; reads no records.

    Catches the case a byte-offset seek cannot: a checkpoint from a *different*
    log, or from a log that has since been truncated. ``verify_from`` would
    also fail on those, but with a message about bytes rather than about a
    checkpoint, and an operator holding the wrong checkpoint deserves to be
    told that.
    """
    if cp.hash and cp.recompute_hash() != cp.hash:
        raise CheckpointCorrupt(
            f"checkpoint hash {cp.hash[:12]} != recomputed "
            f"{cp.recompute_hash()[:12]}; it was altered")
    if cp.canonical_form_version != CANONICAL_FORM_VERSION:
        raise CheckpointMismatch(
            f"checkpoint canonical form v{cp.canonical_form_version} != "
            f"v{CANONICAL_FORM_VERSION}; digests are not comparable")
    try:
        size = log.path.stat().st_size
    except FileNotFoundError:
        raise CheckpointAheadOfLog(
            f"checkpoint names seq {cp.seq} but the log does not exist"
        ) from None
    if cp.next_offset > size:
        raise CheckpointAheadOfLog(
            f"checkpoint ends at byte {cp.next_offset} but the log is {size} "
            f"bytes; {cp.next_offset - size} byte(s) are missing, so records "
            "the checkpoint covered have been removed")
    witness = log.head()
    if witness is not None and witness.seq < cp.seq:
        raise CheckpointAheadOfLog(
            f"checkpoint names seq {cp.seq} but the head witness records "
            f"{witness.seq}; the log was truncated after the checkpoint")


class CheckpointStore:
    """Durable checkpoints, newest wins, nothing overwritten.

    One file per checkpoint, named by seq, so an older one is still there to
    fall back to when the newest turns out to describe a log nobody has any
    more. Writes are atomic; a crash leaves the previous newest in place.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, seq: int) -> Path:
        return self.root / f"{seq:012d}.checkpoint.json"

    def write(self, cp: Checkpoint) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(cp.seq)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.root), prefix=".tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cp.to_record(), fh, sort_keys=True,
                          separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return path

    def read(self, seq: int) -> Checkpoint:
        path = self._path(seq)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CheckpointError(f"no checkpoint at seq {seq}") from None
        return self._parse(raw, f"checkpoint {seq}")

    @staticmethod
    def _parse(raw: str, where: str) -> Checkpoint:
        try:
            rec = json.loads(raw)
        except ValueError as exc:
            raise CheckpointCorrupt(
                f"{where}: unparseable ({type(exc).__name__})") from exc
        rec = _validate(rec, where)
        cp = Checkpoint(**rec)
        recomputed = cp.recompute_hash()
        if recomputed != cp.hash:
            raise CheckpointCorrupt(
                f"{where}: hash {cp.hash[:12]} != recomputed "
                f"{recomputed[:12]}; the checkpoint was altered. Note this "
                "detects accidental corruption only -- anyone who can rewrite "
                "the file can recompute the hash.")
        return cp

    def seqs(self) -> list:
        """Every checkpoint seq present, ascending. Unparsed: names only."""
        if not self.root.is_dir():
            return []
        out = []
        for p in self.root.iterdir():
            name = p.name
            if not name.endswith(".checkpoint.json") or not p.is_file():
                continue
            stem = name[:-len(".checkpoint.json")]
            if stem.isdigit():
                out.append(int(stem))
        return sorted(out)

    def latest(self) -> Checkpoint | None:
        """The newest checkpoint that parses, or None.

        Walks backwards past corrupt ones rather than failing outright: an
        older valid checkpoint is strictly better than none, and the corrupt
        newest is reported by :meth:`audit` rather than by making every
        caller handle it.
        """
        for seq in reversed(self.seqs()):
            try:
                return self.read(seq)
            except CheckpointError:
                continue
        return None

    def latest_usable(self, log: EventLog) -> Checkpoint | None:
        """The newest checkpoint that both parses and describes ``log``."""
        for seq in reversed(self.seqs()):
            try:
                cp = self.read(seq)
                check_against(log, cp)
            except CheckpointError:
                continue
            return cp
        return None

    def audit(self) -> "CheckpointAudit":
        """Parse every checkpoint, reporting each failure. Never stops."""
        problems: list = []
        good = 0
        for seq in self.seqs():
            try:
                self.read(seq)
                good += 1
            except CheckpointError as exc:
                problems.append(f"seq {seq}: {exc}")
        return CheckpointAudit(not problems, good, problems)


@dataclass(frozen=True)
class CheckpointAudit:
    ok: bool
    count: int
    problems: list = field(default_factory=list)
