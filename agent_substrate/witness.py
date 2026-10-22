"""Independent witnesses to the log's head (Assurance E).

THE LIMIT THIS ADDRESSES (M-3)

The log keeps its own head witness (:class:`events.ChainState`), and it
lives beside the log. A writer that can rewrite one can rewrite the other:
a log truncated or rewritten together with its head witness verifies, and
every reader of it is consistently wrong. Only a head held where that
writer cannot reach shows it. This module is the protocol for holding one.

THE PROTOCOL

A CHECKPOINT names one head of one log, as three lines of text:

    <origin>          the log's identity: a name and the hash of its first
                      record, so a checkpoint is never replayed onto
                      another log
    <seq>             the head's position
    <head hash>       the head record's hash

A WITNESS is a principal controlled independently of the writer. It holds
an Ed25519 key and, for each log it witnesses, the last checkpoint it
cosigned, durably. Shown a new checkpoint, it is also shown the records
between its last one and the new one. For a hash chain those records ARE
the consistency proof: each re-hashes, links to the one before, and the
last is the new head. It cosigns only when

* it was provisioned for that origin;
* the new position is not below its last (no rollback);
* at its last position, the head is the one it cosigned (no fork);
* the records re-hash, run contiguously from its last head, and end at
  the new head (the new head extends the old one).

It records the new checkpoint BEFORE it returns the cosignature, so a
cosignature it gave is never one it has forgotten. Asked again for the
checkpoint it last cosigned, it signs it again: a caller that lost the
answer to a crash loses nothing.

A COSIGNATURE is Ed25519 over ``witness-cosignature/1``, the time, and the
checkpoint text, with the witness's id. The shapes follow C2SP's
tlog-checkpoint and tlog-witness: an origin line, a size and a head, and a
witness that cosigns only an extension of what it last cosigned. There is
no Merkle tree here: the log is a hash chain, so consistency is proven by
the records themselves, O(new records) rather than O(log n). That is the
price of not replacing the event log.

WHAT A WITNESS ATTESTS

That it saw this head, and that this head extends every head it saw before
for the same log. It attests to the history's CONSISTENCY. It does not
attest to the truth of anything in it: a witness cosigns a forged record as
readily as a true one, as long as it extends what the witness saw.

VERIFYING A LOG AGAINST ITS WITNESSES (:func:`verify_witnessed`)

Given a log, cosignatures (from the log's holder or asked of each witness),
and a :class:`TrustRoot`, the verifier:

* ignores, and reports, a cosignature by a witness the trust root does not
  name, one whose signature does not verify, or one for another log;
* reports TRUNCATED when the log ends before a cosigned position;
* reports FORKED when the log's record at a cosigned position has another
  hash;
* says how far the witnessing reaches: the highest position a quorum of
  authorized witnesses cosigned and the log agrees with, and how many
  records after it nobody has witnessed. STALE is a statement of that
  reach against a stated bound, never an implication that it covers the
  head.

TRUST ROOT

The trust root is data the verifier is HANDED: which witness ids may vouch,
their public keys, the quorum, and the log's origin. Choosing the witnesses,
provisioning their keys, and keeping the trust root where the writer cannot
change it are the owner's acts and deployment decisions. Only TEST keys
exist in this repository, signed by the REFERENCE_ONLY provider. No
independent production witness exists, and none is claimed.

FAILURE AND RECOVERY

* A witness that cannot be reached: the log keeps appending, and
  verification reports the unwitnessed suffix.
* A witness that lost its state refuses that origin until it is
  re-provisioned. It never trusts whatever it is shown first: a witness
  that forgot is a witness an attacker can restart.
* A witness shown a fork or a rollback refuses and keeps its state. The
  refusal is the evidence.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .events import Event
from .signature import ReferenceEd25519, SignatureProvider

SCHEME = "witness-cosignature/1"
_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class WitnessError(Exception):
    """A checkpoint, proof or cosignature refused. Fail closed."""


class Rollback(WitnessError):
    """A checkpoint below one the witness already cosigned."""


class Fork(WitnessError):
    """A head that does not extend the one the witness cosigned."""


@dataclass(frozen=True)
class Checkpoint:
    origin: str
    seq: int
    head: str

    def __post_init__(self):
        if not isinstance(self.origin, str) or "\n" in self.origin or \
                not self.origin.strip():
            raise WitnessError(f"origin {self.origin!r} is not one line")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or \
                self.seq < 0:
            raise WitnessError(f"seq {self.seq!r} is not a position")
        if not isinstance(self.head, str) or not _HEX64.match(self.head):
            raise WitnessError(f"head {self.head!r} is not a sha256 digest")

    def text(self) -> bytes:
        return f"{self.origin}\n{self.seq}\n{self.head}\n".encode()

    def to_record(self) -> dict:
        return {"origin": self.origin, "seq": self.seq, "head": self.head}

    @classmethod
    def from_record(cls, rec) -> "Checkpoint":
        if not isinstance(rec, dict) or set(rec) != {"origin", "seq", "head"}:
            raise WitnessError("a checkpoint is exactly origin, seq, head")
        return cls(rec["origin"], rec["seq"], rec["head"])


@dataclass(frozen=True)
class Cosignature:
    witness_id: str
    time: int
    signature: str
    checkpoint: Checkpoint

    def message(self) -> bytes:
        return cosigned_message(self.checkpoint, self.time)

    def to_record(self) -> dict:
        return {"witness_id": self.witness_id, "time": self.time,
                "signature": self.signature,
                "checkpoint": self.checkpoint.to_record()}

    @classmethod
    def from_record(cls, rec) -> "Cosignature":
        if not isinstance(rec, dict) or set(rec) != {
                "witness_id", "time", "signature", "checkpoint"}:
            raise WitnessError("a cosignature is exactly witness_id, time, "
                               "signature, checkpoint")
        t = rec["time"]
        if isinstance(t, bool) or not isinstance(t, int):
            raise WitnessError("a cosignature's time is whole seconds")
        if not isinstance(rec["signature"], str):
            raise WitnessError("a cosignature's signature is hex")
        return cls(rec["witness_id"], t, rec["signature"],
                   Checkpoint.from_record(rec["checkpoint"]))


def cosigned_message(cp: Checkpoint, t: int) -> bytes:
    return f"{SCHEME}\ntime {t}\n".encode() + cp.text()


def origin_of(name: str, events) -> str:
    """A log's origin: its operator-chosen name and its first record's
    hash. Two logs with one name have two origins."""
    if not isinstance(name, str) or not _ID.match(name):
        raise WitnessError(f"log name {name!r} is not an id")
    if not events:
        raise WitnessError("an empty log has no origin to witness")
    return f"{name}/{events[0].hash}"


def checkpoint_of(name: str, events) -> Checkpoint:
    """The checkpoint of a verified log's head."""
    return Checkpoint(origin_of(name, events), events[-1].seq,
                      events[-1].hash)


def proof_records(events, after: int, upto: int) -> list:
    """The records a witness at ``after`` needs to reach ``upto``."""
    return [e.to_record() for e in events if after < e.seq <= upto]


# ----------------------------------------------------------------- the witness

@dataclass
class Witness:
    """One witness, its key and its durable state: one file per origin.

    A FIXTURE in this repository: in-process, with a TEST key. The protocol
    is the deliverable; an independent deployment of it is the owner's."""

    witness_id: str
    secret: bytes
    state_dir: Path
    provider: SignatureProvider = field(default_factory=ReferenceEd25519)
    clock: Callable = time.time

    def __post_init__(self):
        if not _ID.match(self.witness_id):
            raise WitnessError(f"witness id {self.witness_id!r} is not an id")
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def public_key(self) -> bytes:
        return self.provider.public_key(self.secret)

    # -- state ---------------------------------------------------------------
    def _path(self, origin: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", origin)
        return self.state_dir / f"{safe}.json"

    def _load(self, origin: str) -> Checkpoint:
        try:
            rec = json.loads(self._path(origin).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise WitnessError(
                f"{self.witness_id} holds no state for {origin!r}: it was "
                "never provisioned for it, or lost what it held. It does not "
                "trust what it is shown first; re-provisioning is the "
                "owner's act") from None
        cp = Checkpoint.from_record(rec)
        if cp.origin != origin:
            raise WitnessError(f"{self.witness_id}'s state for {origin!r} "
                               "names another origin")
        return cp

    def _store(self, cp: Checkpoint) -> None:
        path = self._path(cp.origin)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cp.to_record(), fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def provision(self, genesis: Checkpoint) -> None:
        """The owner's act: start witnessing ``genesis.origin`` from its first
        record. Refused for an origin it already holds state for."""
        if self._path(genesis.origin).exists():
            raise WitnessError(f"{self.witness_id} already witnesses "
                               f"{genesis.origin!r}")
        if genesis.seq != 0 or not genesis.origin.endswith("/" + genesis.head):
            raise WitnessError("a witness is provisioned at a log's first "
                               "record, the one its origin names")
        self._store(genesis)

    def latest(self, origin: str) -> Cosignature:
        """The last checkpoint it cosigned for ``origin``, signed again now:
        what a verifier asks the witness itself, where the log's holder
        cannot reach."""
        return self._cosign(self._load(origin))

    # -- the one operation ---------------------------------------------------
    def add_checkpoint(self, new: Checkpoint, proof: list) -> Cosignature:
        last = self._load(new.origin)
        if new.seq < last.seq:
            raise Rollback(f"{new.origin}: seq {new.seq} is below seq "
                           f"{last.seq}, which {self.witness_id} cosigned")
        if new.seq == last.seq:
            if new.head != last.head:
                raise Fork(f"{new.origin}: seq {new.seq} has head "
                           f"{new.head[:12]}, and {self.witness_id} "
                           f"cosigned {last.head[:12]} there")
            if proof:
                raise WitnessError("a checkpoint at the witnessed head "
                                   "needs no records")
            return self._cosign(last)
        self._extends(last, new, proof)
        self._store(new)
        return self._cosign(new)

    def _extends(self, last: Checkpoint, new: Checkpoint, proof) -> None:
        """Every record from ``last`` to ``new``: re-hashed, contiguous,
        each linked to the one before, ending at ``new.head``."""
        if not isinstance(proof, list) or len(proof) != new.seq - last.seq:
            raise WitnessError(f"{new.origin}: {last.seq} -> {new.seq} needs "
                               f"exactly {new.seq - last.seq} record(s)")
        prev = last.head
        for want, rec in enumerate(proof, start=last.seq + 1):
            try:
                ev = Event(**rec)
            except TypeError as exc:
                raise WitnessError(f"record {want}: not a log record "
                                   f"({exc})") from None
            if ev.seq != want:
                raise WitnessError(f"record {want}: numbered {ev.seq}")
            if ev.recompute_hash() != ev.hash:
                raise WitnessError(f"record {want}: does not hash to "
                                   f"{str(ev.hash)[:12]}")
            if ev.prev_hash != prev:
                raise Fork(f"{new.origin}: record {want} links to "
                           f"{ev.prev_hash[:12]}, not to {prev[:12]}, the "
                           f"head {self.witness_id} cosigned")
            prev = ev.hash
        if prev != new.head:
            raise WitnessError(f"{new.origin}: the records end at "
                               f"{prev[:12]}, not at {new.head[:12]}")

    def _cosign(self, cp: Checkpoint) -> Cosignature:
        t = int(self.clock())
        sig = self.provider.sign(self.secret, cosigned_message(cp, t))
        return Cosignature(self.witness_id, t, sig.hex(), cp)


# ----------------------------------------------------------- the verification

@dataclass(frozen=True)
class TrustRoot:
    """Which witnesses may vouch for which log, and how many must."""

    origin: str
    witnesses: dict            # witness id -> public key (bytes)
    quorum: int = 1

    def __post_init__(self):
        if isinstance(self.quorum, bool) or not isinstance(self.quorum, int) \
                or not 1 <= self.quorum <= len(self.witnesses):
            raise WitnessError(f"quorum {self.quorum!r} of "
                               f"{len(self.witnesses)} witness(es)")


@dataclass
class WitnessReport:
    head_seq: int
    #: the highest position a quorum vouched for and the log agrees with,
    #: or -1 when no quorum vouched for any
    witnessed_through: int = -1
    #: records after it that no quorum has witnessed
    unwitnessed: int = 0
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    stale: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems and not self.stale and \
            self.witnessed_through >= 0


def verify_witnessed(events, cosignatures, root: TrustRoot, *,
                     provider=None, max_lag: int | None = None,
                     max_age_s: float | None = None,
                     now: float | None = None) -> WitnessReport:
    """``events`` (a VERIFIED read of the log) against ``cosignatures``.

    TRUNCATED and FORKED are problems. A cosignature that does not count --
    another log's, an unauthorized witness's, a signature that does not
    verify -- is a note: anybody can hand a verifier junk, and junk is not
    evidence of anything. ``max_lag`` (records) and ``max_age_s`` (seconds
    since the newest counted cosignature, against ``now``) bound how far the
    witnessing may lag before the report is STALE."""
    provider = provider or ReferenceEd25519()
    head = events[-1].seq if events else -1
    out = WitnessReport(head_seq=head)
    if events and origin_of(root.origin.rsplit("/", 1)[0], events) != \
            root.origin:
        out.problems.append(f"this log's origin is not {root.origin!r}: "
                            "the trust root is for another log")
        return out
    best: dict = {}             # witness id -> (seq, time) of what counts
    for c in cosignatures:
        cp = c.checkpoint
        if c.witness_id not in root.witnesses:
            out.notes.append(f"{c.witness_id}: not a witness the trust root "
                             "names; not counted")
            continue
        if cp.origin != root.origin:
            out.notes.append(f"{c.witness_id}: cosigned another log; not "
                             "counted")
            continue
        try:
            sig = bytes.fromhex(c.signature)
        except ValueError:
            sig = b""
        if not provider.verify(root.witnesses[c.witness_id], c.message(),
                               sig):
            out.notes.append(f"{c.witness_id}: signature over seq {cp.seq} "
                             "does not verify; not counted")
            continue
        if cp.seq > head:
            out.problems.append(
                f"TRUNCATED: {c.witness_id} cosigned seq {cp.seq}, and the "
                f"log ends at {head}")
            continue
        if events[cp.seq].hash != cp.head:
            out.problems.append(
                f"FORKED: {c.witness_id} cosigned {cp.head[:12]} at seq "
                f"{cp.seq}; the log holds {events[cp.seq].hash[:12]} there")
            continue
        if cp.seq >= best.get(c.witness_id, (-1, 0))[0]:
            best[c.witness_id] = (cp.seq, c.time)
    reach = sorted((s for s, _ in best.values()), reverse=True)
    if len(reach) >= root.quorum:
        out.witnessed_through = reach[root.quorum - 1]
    out.unwitnessed = head - out.witnessed_through
    if max_lag is not None and out.unwitnessed > max_lag:
        out.stale = True
        out.notes.append(f"STALE: {out.unwitnessed} record(s) after seq "
                         f"{out.witnessed_through} are unwitnessed; the "
                         f"bound is {max_lag}")
    if max_age_s is not None:
        newest = max((t for _, t in best.values()), default=None)
        at = time.time() if now is None else now
        if newest is None or at - newest > max_age_s:
            out.stale = True
            out.notes.append(f"STALE: the newest counted cosignature is "
                             f"from {newest}; the bound is {max_age_s}s")
    return out
