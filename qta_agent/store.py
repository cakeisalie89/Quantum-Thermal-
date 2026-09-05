"""Transactional authority store: the live projection over the event log.

The log is the truth; this is a cache of it. That inversion is deliberate and
is what makes crash recovery tractable -- there is no state here that cannot
be rebuilt by replaying events, so a lost or corrupted snapshot costs time,
never authority.

TRANSACTION MODEL

A mutation is: authorize against the state machine, append one event, then
update the in-memory projection. The append is the commit point. A crash
before it leaves no trace; a crash after it leaves a durable event that the
next :meth:`AuthorityStore.load` replays. There is no window in which a
mutation is half-applied, because the projection is derived rather than
independently written.

CONCURRENCY

Optimistic, via ``expected_revision``. Two writers racing on one record both
read revision N; the first commits N+1; the second is refused because the log
it appends onto no longer shows N. Authority never resolves a conflict by
last-writer-wins -- the loser is told, and re-reads.

IDEMPOTENCY

Callers may supply an ``idempotency_key``. A replayed request carrying a key
already present in the log is a no-op returning the original outcome, so a
retried tool call cannot double-apply a transition.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from . import actions
from .authority import (
    INITIAL,
    Role,
    State,
    TransitionError,
    TransitionRequest,
    check,
)
from .canonical import canonical_bytes, digest, is_digest
from .events import ChainBroken, EventLog, Event


class StoreError(Exception):
    """Base for store-level refusals."""


class ConcurrencyError(StoreError):
    """The record changed since it was read. Re-read and retry."""


class UnknownRecord(StoreError):
    """No such record in the projection."""


@dataclass(frozen=True)
class Record:
    """One authority-bearing claim."""
    record_id: str
    kind: str
    proposer: str
    state: State = INITIAL
    revision: int = 0
    #: evidence key -> sha256 digest
    evidence: dict = field(default_factory=dict)
    #: record_ids this record's validity depends on
    depends_on: tuple = ()
    policy_id: str | None = None
    created_seq: int = -1
    updated_seq: int = -1
    #: Set when the record left PROMOTED because a dependency moved.
    stale_reason: str | None = None

    def to_record(self) -> dict:
        return {
            "record_id": self.record_id, "kind": self.kind,
            "proposer": self.proposer, "state": self.state.value,
            "revision": self.revision, "evidence": dict(self.evidence),
            "depends_on": list(self.depends_on), "policy_id": self.policy_id,
            "created_seq": self.created_seq, "updated_seq": self.updated_seq,
            "stale_reason": self.stale_reason,
        }


# Event action names. Kept as constants so a typo cannot silently create a
# second, unhandled action that the reducer ignores.
ACT_CREATE = "record.create"
ACT_TRANSITION = "record.transition"
ACT_DEPEND = "record.depend"

#: The actions THIS reducer applies. Everything else on the log is either
#: another subsystem's business (skipped) or unrecognised (refused) -- see
#: :mod:`qta_agent.actions` for why those two cases must be told apart.
OWNED = frozenset({ACT_CREATE, ACT_TRANSITION, ACT_DEPEND})


class AuthorityStore:
    """Live projection with transactional mutation through the log."""

    def __init__(self, log: EventLog, *, evidence=None):
        self.log = log
        #: Optional :class:`~qta_agent.evidence.EvidenceStore` (or anything
        #: with a compatible ``contains``). When present, every cited digest
        #: must resolve to content this store actually holds, which is what
        #: turns I6 from "looks like a digest" into "is evidence". When
        #: absent, citations are checked for shape only -- see
        #: :func:`~qta_agent.authority.check` for why that remains allowed.
        self.evidence = evidence
        self._records: dict = {}
        #: idempotency key -> the record it completed a request for. A set
        #: would answer 'has this key been used' but not 'for what', and a
        #: key replayed against a DIFFERENT record must be an error rather
        #: than a silent success returning someone else's record.
        self._applied_keys: dict = {}
        self._loaded_through: int = -1
        self._loaded_prefix_verified: bool = True

    @property
    def _resolver(self):
        """The digest predicate to enforce with.

        ``None`` means shape-only enforcement -- see
        :func:`~qta_agent.authority.check`.
        """
        return None if self.evidence is None else self.evidence.contains

    # ---- projection ---------------------------------------------------
    def load(self) -> "AuthorityStore":
        """Rebuild the projection from the verified log. Fail closed."""
        self.log.verify().raise_if_bad()
        self._records = {}
        self._applied_keys = {}
        self._loaded_through = -1
        self._loaded_prefix_verified = True
        for ev in self.log.read():
            self._apply(ev)
        return self

    def _apply(self, ev: Event) -> None:
        """Fold one event into the projection.

        Tolerant of exactly one thing: another subsystem's event on the same
        log. An UNRECOGNISED action is still an error, because silently
        ignoring one would let a future writer add authority-relevant events
        that older readers quietly drop -- and the state those readers
        reconstruct would then be confidently wrong.
        """
        try:
            kind = actions.require_known(ev.action, mine=OWNED,
                                         where=f"seq {ev.seq}")
        except actions.UnknownAction as exc:
            # Re-raised as this store's own error type. The classification is
            # shared; the exception contract is not, so a caller that catches
            # StoreError still catches everything this store refuses.
            raise StoreError(str(exc)) from exc
        if kind == actions.FOREIGN:
            # Another subsystem's event on the shared log. Not this reducer's
            # business, and refusing it would make one log impossible to
            # share -- which is exactly what happened before this branch
            # existed: the authority store raised on 'policy.publish'.
            self._loaded_through = ev.seq
            return
        p = ev.payload
        key = p.get("idempotency_key")
        if key:
            self._applied_keys[key] = p.get("record_id", ev.target)
        if ev.action == ACT_CREATE:
            rid = p["record_id"]
            self._records[rid] = Record(
                record_id=rid, kind=p["kind"], proposer=p["proposer"],
                state=State(p.get("state", INITIAL.value)),
                revision=1, evidence=dict(p.get("evidence", {})),
                depends_on=tuple(p.get("depends_on", ())),
                policy_id=p.get("policy_id"),
                created_seq=ev.seq, updated_seq=ev.seq)
        elif ev.action == ACT_TRANSITION:
            rid = p["record_id"]
            cur = self._records[rid]
            # RE-AUTHORIZE ON REPLAY, FROM THE STATE THIS REPLAY REACHED.
            #
            # This used to take p["dst"] and apply it. Nothing was checked:
            # not the edge, not the role, not separation of duties, not the
            # declared src -- which was written into the payload and then
            # ignored. One appended line moved a record from UNDER_REVIEW
            # straight to PROMOTED, the state that carries canonical
            # authority and is reachable only from VERIFIED by I1, and
            # store.canonical() reported it.
            #
            # reconstruct.py refused the same log correctly, and that is
            # what hid this: the test asserting "an unauthorized transition
            # in the log is not applied" asked the INDEPENDENT reader, while
            # the live projection -- the one every caller consults -- applied
            # it. A second reader is a detector, not a substitute for the
            # first reader being right.
            #
            # No resolver is passed. Evidence may legitimately have been
            # archived since a historical transition was made, and forcing
            # resolution here would turn an archival policy into a
            # retroactive authority failure. The state machine is still
            # enforced in full.
            claimed = p.get("src")
            if claimed is not None and claimed != cur.state.value:
                raise StoreError(
                    f"seq {ev.seq}: {rid!r} is {cur.state.value}, but the "
                    f"record moves it from {claimed}. A transition whose "
                    "starting state the replay does not agree with was not "
                    "written through the gate.")
            merged_evidence = {**cur.evidence, **p.get("evidence", {})}
            try:
                check(TransitionRequest(
                    record_id=rid, src=cur.state, dst=State(p["dst"]),
                    actor=ev.actor, role=Role(p["role"]),
                    evidence=merged_evidence, proposer=cur.proposer,
                    policy_id=p.get("policy_id") or cur.policy_id))
            except TransitionError as exc:
                raise StoreError(
                    f"seq {ev.seq}: {rid!r} {cur.state.value} -> "
                    f"{p.get('dst')} would be refused today: {exc}. Presence "
                    "in the log is not authority.") from exc
            self._records[rid] = replace(
                cur, state=State(p["dst"]), revision=cur.revision + 1,
                evidence=merged_evidence,
                updated_seq=ev.seq,
                stale_reason=p.get("stale_reason", cur.stale_reason),
                policy_id=p.get("policy_id", cur.policy_id))
        elif ev.action == ACT_DEPEND:
            rid = p["record_id"]
            cur = self._records[rid]
            merged = tuple(dict.fromkeys(
                cur.depends_on + tuple(p["depends_on"])))
            self._records[rid] = replace(
                cur, depends_on=merged, revision=cur.revision + 1,
                updated_seq=ev.seq)
        else:                                   # pragma: no cover - closed
            # Unreachable: require_known already classified this action as
            # MINE, and OWNED lists exactly the three handled above. Kept so
            # that adding a fourth to OWNED without handling it fails loudly.
            raise StoreError(
                f"seq {ev.seq}: {ev.action!r} is listed as owned by this "
                "reducer and has no branch handling it")
        self._loaded_through = ev.seq

    # ---- snapshotting -------------------------------------------------
    def snapshot(self) -> dict:
        """The whole projection, canonically serializable.

        ``_applied_keys`` is included because idempotency is part of the
        state: a snapshot that dropped it would let a replayed request with a
        previously-used key apply a second time, which is precisely the thing
        idempotency keys exist to stop. Sorted, because a set has no order and
        a digest over an unordered thing is not a digest over anything.
        """
        return {
            "snapshot_version": 1,
            "loaded_through": self._loaded_through,
            "records": {rid: r.to_record()
                        for rid, r in sorted(self._records.items())},
            "applied_keys": dict(sorted(self._applied_keys.items())),
        }

    def snapshot_digest(self) -> str:
        """Digest of the WHOLE snapshot, including its log position.

        That position is part of what a checkpoint pins, so it belongs here.
        It also makes this the wrong function for "are these two projections
        in the same state": on a shared log a live store's position is its own
        last write, while a freshly loaded one has read to the head, and the
        two differ while describing identical records. Use
        :meth:`state_digest` for that question.
        """
        return digest(self.snapshot())

    def state_digest(self) -> str:
        """Digest of the STATE alone: records and idempotency keys.

        Position-independent, so two projections built by different routes --
        a full replay, a checkpointed load, a live store mid-run -- can be
        compared for the thing that actually matters.
        """
        snap = self.snapshot()
        return digest({"records": snap["records"],
                       "applied_keys": snap["applied_keys"]})

    def _restore(self, snap: dict) -> None:
        """Rebuild the projection from a snapshot. Validates, never assumes."""
        if not isinstance(snap, dict) or snap.get("snapshot_version") != 1:
            raise StoreError(
                "snapshot is not a version-1 projection snapshot; refusing "
                "to guess at its shape")
        records = snap.get("records")
        keys = snap.get("applied_keys")
        through = snap.get("loaded_through")
        if (not isinstance(records, dict) or not isinstance(keys, dict)
                or not isinstance(through, int) or isinstance(through, bool)):
            raise StoreError("snapshot fields are structurally invalid")
        rebuilt = {}
        for rid, r in records.items():
            if not isinstance(r, dict):
                raise StoreError(f"snapshot record {rid!r} is not an object")
            try:
                rebuilt[rid] = Record(
                    record_id=r["record_id"], kind=r["kind"],
                    proposer=r["proposer"], state=State(r["state"]),
                    revision=r["revision"], evidence=dict(r["evidence"]),
                    depends_on=tuple(r["depends_on"]),
                    policy_id=r["policy_id"], created_seq=r["created_seq"],
                    updated_seq=r["updated_seq"],
                    stale_reason=r["stale_reason"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StoreError(
                    f"snapshot record {rid!r} is malformed: {exc}") from exc
            if rebuilt[rid].record_id != rid:
                raise StoreError(
                    f"snapshot key {rid!r} disagrees with the record it holds "
                    f"({rebuilt[rid].record_id!r})")
        self._records = rebuilt
        if not all(isinstance(k, str) and isinstance(v, str)
                   for k, v in keys.items()):
            raise StoreError(
                "snapshot applied_keys must map key -> record_id")
        self._applied_keys = dict(keys)
        self._loaded_through = through

    # ---- checkpointing -------------------------------------------------
    def checkpoint(self, checkpoints, *, blobs=None):
        """Verify the log in full, snapshot the projection, pin both.

        The snapshot goes into a content-addressed blob store and the
        checkpoint records its digest, so the two cannot drift: a snapshot
        whose bytes changed no longer resolves to the digest the checkpoint
        names, and :meth:`load_from` refuses it. This is why the checkpoint
        holds a digest rather than the state itself -- the state is evidence,
        and evidence in this package is stored in exactly one way.

        ``blobs`` defaults to the store's attached evidence store. Passing a
        separate one is allowed and is the right choice if snapshots should
        not share a retention policy with cited evidence.

        Returns the written :class:`~qta_agent.checkpoint.Checkpoint`.
        """
        from . import checkpoint as cp_mod

        target = blobs if blobs is not None else self.evidence
        if target is None:
            raise StoreError(
                "checkpointing needs a blob store for the snapshot; attach "
                "one as AuthorityStore(log, evidence=...) or pass blobs=")

        # The log can have advanced since this projection last applied an
        # event: other subsystems share it, and other processes may write to
        # it. A checkpoint that pins the CURRENT head while holding a snapshot
        # taken at an earlier position describes neither, and load_from
        # refuses it -- correctly, and only long afterwards. So the projection
        # is brought up to the head first.
        #
        # This could not happen while the store was the only writer, which is
        # exactly why it appeared the moment the log became shared.
        head = self.log.verify().head_seq
        if self._loaded_through < head:
            self.load()
        payload = canonical_bytes(self.snapshot())
        dg = target.put(payload, media_type="application/json")
        cp = cp_mod.create(self.log, state_digest=dg)
        checkpoints.write(cp)
        return cp

    @classmethod
    def load_from(cls, log, checkpoints, *, blobs, evidence=None,
                  require_checkpoint: bool = False) -> "AuthorityStore":
        """Load by restoring the newest usable snapshot and replaying the tail.

        This is the cheap load, and it is cheap for the same reason it is
        weaker: the records before the checkpoint are never read, so tampering
        with them is invisible here. ``store.loaded_prefix_verified`` is False
        afterwards, and stays False, so a caller can tell which kind of load
        produced the state they are holding.

        Falls back to a full :meth:`load` when no usable checkpoint exists --
        unless ``require_checkpoint``, which turns a missing checkpoint into
        an error rather than a silent switch to the expensive path. Use it
        where a sudden O(n) load would be a problem worth hearing about.
        """
        from . import checkpoint as cp_mod

        store = cls(log, evidence=evidence)
        cp = checkpoints.latest_usable(log)
        if cp is None:
            if require_checkpoint:
                raise StoreError(
                    "no usable checkpoint for this log, and one was required")
            return store.load()
        if cp.state_digest is None:
            raise StoreError(
                f"checkpoint at seq {cp.seq} pins no snapshot; it records a "
                "log position only and cannot restore a projection")

        raw = blobs.get(cp.state_digest)      # verified on read by the store
        try:
            snap = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise StoreError(
                f"snapshot {cp.state_digest[:12]} is unparseable: "
                f"{type(exc).__name__}") from exc
        store._restore(snap)

        if store._loaded_through != cp.seq:
            raise StoreError(
                f"snapshot covers the log through seq "
                f"{store._loaded_through} but the checkpoint anchors at "
                f"{cp.seq}; refusing to replay from a position the snapshot "
                "does not describe")

        report = cp_mod.verify_with(log, cp)
        report_ok = getattr(report, "ok", False)
        if not report_ok:
            raise ChainBroken("; ".join(report.problems) or "chain invalid")

        for ev in log.read_from(cp.anchor):
            store._apply(ev)
        store._loaded_prefix_verified = False
        return store

    @property
    def loaded_prefix_verified(self) -> bool:
        """False when this projection came from a checkpoint, not a full read.

        Exposed rather than inferred, because "did anyone actually check the
        first ten thousand records" is not a question a caller should have to
        reconstruct from how the object was built.
        """
        return self._loaded_prefix_verified

    # ---- reads --------------------------------------------------------
    def get(self, record_id: str) -> Record:
        try:
            return self._records[record_id]
        except KeyError:
            raise UnknownRecord(record_id) from None

    def all_records(self) -> dict:
        return dict(self._records)

    def canonical(self) -> dict:
        return {k: v for k, v in self._records.items()
                if v.state is State.PROMOTED}

    # ---- writes -------------------------------------------------------
    def create(self, *, record_id: str, kind: str, proposer: str,
               evidence: dict | None = None, depends_on: tuple = (),
               policy_id: str | None = None,
               idempotency_key: str | None = None) -> Record:
        # Order matters and was wrong once: checking existence first made the
        # idempotent branch unreachable, because a completed create always
        # leaves the record in place. A retried request would then get
        # "already exists" -- which is exactly the failure an idempotency key
        # is bought to prevent.
        if idempotency_key:
            done_for = self._applied_keys.get(idempotency_key)
            if done_for is not None:
                if done_for != record_id:
                    raise StoreError(
                        f"idempotency key {idempotency_key!r} already "
                        f"completed a request for {done_for!r}; reusing it "
                        f"for {record_id!r} would return the wrong record")
                return self.get(record_id)
        if record_id in self._records:
            raise StoreError(f"record {record_id!r} already exists")
        evidence = dict(evidence or {})
        for k, v in evidence.items():
            if not is_digest(v):
                raise StoreError(
                    f"evidence {k!r} must be a sha256 digest so it cannot be "
                    f"altered after being cited; got {type(v).__name__}")
        self._require_evidence_exists(evidence)
        for dep in depends_on:
            if dep not in self._records:
                raise StoreError(
                    f"dependency {dep!r} does not exist; a record may not "
                    "depend on something unrecorded")
        ev = self.log.append(
            actor=proposer, action=ACT_CREATE, target=record_id,
            payload={"record_id": record_id, "kind": kind,
                     "proposer": proposer, "state": INITIAL.value,
                     "evidence": evidence, "depends_on": list(depends_on),
                     "policy_id": policy_id,
                     "idempotency_key": idempotency_key})
        self._apply(ev)
        return self.get(record_id)

    def transition(self, *, record_id: str, dst: State, actor: str,
                   role: Role, evidence: dict | None = None,
                   policy_id: str | None = None,
                   expected_revision: int | None = None,
                   stale_reason: str | None = None,
                   idempotency_key: str | None = None) -> Record:
        """Authorize and commit a state transition."""
        if idempotency_key:
            done_for = self._applied_keys.get(idempotency_key)
            if done_for is not None:
                if done_for != record_id:
                    raise StoreError(
                        f"idempotency key {idempotency_key!r} already "
                        f"completed a request for {done_for!r}; reusing it "
                        f"for {record_id!r} would return the wrong record")
                return self.get(record_id)
        cur = self.get(record_id)
        if expected_revision is not None and cur.revision != expected_revision:
            raise ConcurrencyError(
                f"{record_id}: expected revision {expected_revision}, found "
                f"{cur.revision}; the record changed since it was read")
        evidence = dict(evidence or {})
        req = TransitionRequest(
            record_id=record_id, src=cur.state, dst=dst, actor=actor,
            role=role, evidence={**cur.evidence, **evidence},
            proposer=cur.proposer, policy_id=policy_id or cur.policy_id)
        # raises TransitionError if not permitted, including when a cited
        # digest does not resolve in the attached evidence store
        edge = check(req, resolve=self._resolver)
        ev = self.log.append(
            actor=actor, action=ACT_TRANSITION, target=record_id,
            payload={"record_id": record_id, "src": cur.state.value,
                     "dst": dst.value, "role": role.value,
                     "evidence": evidence,
                     "policy_id": policy_id or cur.policy_id,
                     "stale_reason": stale_reason,
                     "edge_reason": edge.reason,
                     "idempotency_key": idempotency_key})
        self._apply(ev)
        return self.get(record_id)

    def _require_evidence_exists(self, evidence: dict) -> None:
        """Refuse a citation at creation time, not only at promotion time.

        Catching a fabricated digest here means the log never records it. If
        the check happened only at the gate, the fabrication would already be
        a permanent, hash-chained fact by the time anyone noticed -- true
        forever that the agent claimed it, and impossible to remove.
        """
        if self.evidence is None:
            return
        from .evidence import UnknownEvidence, require_resolvable
        try:
            require_resolvable(evidence, self.evidence.contains)
        except UnknownEvidence as exc:
            raise StoreError(str(exc)) from exc

    def add_dependency(self, *, record_id: str, depends_on: tuple,
                       actor: str = "SYSTEM") -> Record:
        self.get(record_id)      # raises UnknownRecord if it does not exist
        for dep in depends_on:
            if dep not in self._records:
                raise StoreError(f"dependency {dep!r} does not exist")
            if dep == record_id:
                raise StoreError(
                    f"{record_id!r} cannot depend on itself")
        ev = self.log.append(
            actor=actor, action=ACT_DEPEND, target=record_id,
            payload={"record_id": record_id,
                     "depends_on": list(depends_on)})
        self._apply(ev)
        return self.get(record_id)
