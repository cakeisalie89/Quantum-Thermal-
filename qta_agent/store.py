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
from .events import ChainBroken, Event, EventLog, EventLogError


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
#: The digest of a projection snapshot, recorded at the position it covers.
#:
#: A checkpoint file says "the state at seq K is blob D". Nothing
#: authenticated that: the file's self-hash is recomputable by whoever can
#: write the file, so a rewritten checkpoint pointing at a forged snapshot
#: restored cleanly while the log itself still verified. The claim is now
#: also made IN the log, where the hash chain covers it. See D-2026-30.
ACT_CHECKPOINT_STATE = "checkpoint.state"

#: Default actor for a checkpoint anchor. A snapshot asserts no authority
#: over any record -- it is a statement about a POSITION -- so this record
#: needs no role and grants none. It is still attributed, because an
#: unattributed record in an audited log is a gap in the audit.
CHECKPOINT_ACTOR = "checkpointer"

#: The actions THIS reducer applies. Everything else on the log is either
#: another subsystem's business (skipped) or unrecognised (refused) -- see
#: :mod:`qta_agent.actions` for why those two cases must be told apart.
OWNED = frozenset({ACT_CREATE, ACT_TRANSITION, ACT_DEPEND,
                   ACT_CHECKPOINT_STATE})



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
        #: An anchor at ``_loaded_through``, so catching up costs O(new)
        #: rather than O(history). See Scheduler for why that matters.
        self._anchor = None

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
        self._anchor = None
        for ev in self.log.read():
            self._apply(ev)
        self._reanchor()
        return self

    def _reanchor(self) -> None:
        """Take an anchor at the position this projection has reached."""
        self._anchor = (self.log.anchor_at(self._loaded_through)
                        if self._loaded_through >= 0 else None)

    def catch_up(self, *, force: bool = False) -> "AuthorityStore":
        """Fold in everything appended since this projection last read.

        The same rule the scheduler follows, for the same reason. Two
        processes each read a record as UNDER_REVIEW and each appended a
        transition out of it; both appends were correct on their own terms,
        and the log was then unreplayable, because the second record moves a
        record from a state the replay has already left.

        ``force`` skips the cheap witness check, which can legitimately lag
        the log after a crash between an append and the witness update.
        """
        if not force:
            witness = None
            try:
                witness = self.log.head()
            except Exception:            # noqa: BLE001 - unreadable: do the work
                witness = None
            if witness is not None and witness.seq <= self._loaded_through:
                return self
        self._fold_new()
        return self

    def _fold_new(self) -> None:
        """Fold everything this projection has not seen, in O(new).

        Anchored, for the reason given in the scheduler: a full read here
        made every governed write cost the whole history. Falls back to a
        full verified read when there is no anchor or the anchor no longer
        describes the bytes at its offset.
        """
        if self._anchor is not None:
            try:
                events, moved = self.log.advance(self._anchor)
            except EventLogError:
                self._anchor = None
            else:
                self._anchor = moved
                for ev in events:
                    if ev.seq > self._loaded_through:
                        self._apply(ev)
                return
        self.log.verify().raise_if_bad()
        for ev in self.log.read():
            if ev.seq > self._loaded_through:
                self._apply(ev)
        self._reanchor()

    def _append_decided(self, build):
        """Re-read, rebuild the record and write, all under one lock.

        ``build`` is a function rather than a dict because a record whose
        payload was decided before the re-read is exactly the record this
        exists to prevent. It may raise, and that refusal is the caller's
        answer: the loser of a race discovers there that the transition it
        wanted is no longer available.
        """
        def under_lock(head_seq):
            # The chain was verified by append_decided immediately above.
            self._fold_new()
            return build()

        ev = self.log.append_decided(under_lock)
        self._apply(ev)
        # The anchor is NOT updated here, and does not need to be: the fold
        # inside the lock above already moved it, so it lags this projection
        # by exactly the one record just written. The next catch-up re-reads
        # that record and skips it by sequence number, which is O(1).
        return ev

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
        if ev.action == ACT_CHECKPOINT_STATE:
            # A FACT ABOUT THE PROJECTION, NOT A CHANGE TO IT.
            #
            # Folding it into records would make "somebody took a snapshot"
            # and "somebody changed a record" the same kind of event. It
            # moves the position and nothing else; load_from reads it back
            # out of the tail to check what a checkpoint file claims.
            self._loaded_through = ev.seq
            return
        key = p.get("idempotency_key")
        if key:
            self._applied_keys[key] = p.get("record_id", ev.target)
        if ev.action == ACT_CREATE:
            rid = p["record_id"]
            # A CREATE INTRODUCES A CLAIM. IT DOES NOT ASSERT A VERDICT.
            #
            # This read `state` from the payload, so a single appended line
            # produced a record born in PROMOTED -- the state that carries
            # canonical authority and is reachable only from VERIFIED by I1
            # -- and store.canonical() reported it. The transition reducer
            # above was hardened to refuse a forged walk to PROMOTED; this
            # skipped the walk entirely by starting at the destination, so
            # the state machine was never consulted at all.
            #
            # It also replaced an existing record wholesale, which is the
            # same defect pointed at history instead of at authority: a
            # second create for a live id reset its state and its evidence.
            if rid in self._records:
                raise StoreError(
                    f"seq {ev.seq}: {rid!r} already exists and this record "
                    "creates it again; a second create would silently "
                    "replace the first one's state, evidence and history")
            claimed_state = p.get("state", INITIAL.value)
            if claimed_state != INITIAL.value:
                raise StoreError(
                    f"seq {ev.seq}: {rid!r} is created directly in "
                    f"{claimed_state}. A create introduces a claim; every "
                    f"state after {INITIAL.value} is reached by a transition "
                    "that is authorized on its own terms.")
            # The proposer is who the log says wrote this. Separation of
            # duties is checked against it -- a proposer may not verify its
            # own record -- so a create that could name one could choose the
            # party it has to differ from.
            claimed_proposer = p["proposer"]
            if claimed_proposer != ev.actor:
                raise StoreError(
                    f"seq {ev.seq}: {rid!r} names {claimed_proposer!r} as "
                    f"its proposer but was appended by {ev.actor!r}; who "
                    "proposed a record is the log's to say, and separation "
                    "of duties is measured from it")
            evidence = dict(p.get("evidence", {}))
            for k, v in evidence.items():
                # FORM, not resolution: evidence may legitimately have been
                # archived since, and requiring it to resolve here would turn
                # an archival policy into a retroactive authority failure.
                # Same reasoning as the transition reducer above.
                if not is_digest(v):
                    raise StoreError(
                        f"seq {ev.seq}: evidence {k!r} on {rid!r} is not a "
                        "sha256 digest, so what was cited could be altered "
                        "after the fact")
            for dep in p.get("depends_on", ()):
                if dep not in self._records:
                    raise StoreError(
                        f"seq {ev.seq}: {rid!r} depends on {dep!r}, which "
                        "nothing has recorded; a record may not depend on "
                        "something unrecorded")
            self._records[rid] = Record(
                record_id=rid, kind=p["kind"], proposer=ev.actor,
                state=INITIAL,
                revision=1, evidence=evidence,
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
            if rid not in self._records:
                # A raw KeyError leaks the projection's internals and skips
                # the domain error every other refusal here raises.
                raise StoreError(
                    f"seq {ev.seq}: a dependency record names {rid!r}, which "
                    "was never created")
            cur = self._records[rid]
            for dep in p["depends_on"]:
                if dep not in self._records:
                    raise StoreError(
                        f"seq {ev.seq}: {rid!r} is made to depend on "
                        f"{dep!r}, which nothing has recorded; an "
                        "unsatisfiable dependency is one nothing will ever "
                        "resolve")
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
    def checkpoint(self, checkpoints, *, blobs=None,
                   actor: str = CHECKPOINT_ACTOR):
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

        THE SAME CLAIM IS ALSO WRITTEN TO THE LOG.

        A checkpoint file is authenticated by nothing -- its self-hash is
        recomputable by anyone who can write the file, and the checkpoint
        module says so in its own docstring. So the file alone could say
        "the state at seq K is blob D" about any blob at all. The claim is
        therefore ALSO appended as a ``checkpoint.state`` record, where the
        hash chain covers it and rewriting it breaks verification.

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

        # THE CLAIM GOES IN THE LOG, WHERE THE HASH CHAIN COVERS IT.
        #
        # Order matters and is not arbitrary. The checkpoint is created
        # FIRST, against the head the snapshot describes, so cp.seq names
        # that position rather than the position of this record. The record
        # then lands at cp.seq + 1, which puts it in the tail load_from
        # replays -- the one stretch a checkpointed load does read.
        #
        # If the process dies between the append and the write below, the log
        # carries an anchor for a checkpoint nobody has. That is a fact about
        # a snapshot that exists in the blob store and is harmless; the
        # reverse order would leave a checkpoint nothing anchors, which is
        # the state this whole record exists to make unloadable.
        self.log.append(
            actor=actor, action=ACT_CHECKPOINT_STATE,
            target=f"seq:{cp.seq}",
            payload={"through_seq": cp.seq, "state_digest": dg,
                     "head_hash": cp.head_hash})
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

        # WHAT THE CHECKPOINT FILE SAYS, AGAINST WHAT THE LOG SAYS.
        #
        # Until D-2026-30 the snapshot was pinned only by the checkpoint
        # file, whose self-hash anyone able to write the file can recompute.
        # A rewritten checkpoint naming a forged snapshot loaded cleanly,
        # produced a record the gate would have refused, and left the log
        # verifying perfectly -- because the forgery was never in the log.
        #
        # The anchoring record is in the tail this load already reads and
        # already verified, so the check costs nothing beyond the comparison.
        anchored = False
        for ev in log.read_from(cp.anchor):
            if ev.action == ACT_CHECKPOINT_STATE:
                p = ev.payload if isinstance(ev.payload, dict) else {}
                if p.get("through_seq") == cp.seq:
                    if p.get("state_digest") != cp.state_digest:
                        raise StoreError(
                            f"checkpoint at seq {cp.seq} pins snapshot "
                            f"{str(cp.state_digest)[:12]} but the log records "
                            f"{str(p.get('state_digest'))[:12]} for that "
                            "position. The log is what the projection is "
                            "reconstructed from; a file claiming otherwise is "
                            "claiming a state this history never reached.")
                    # The head hash is NOT re-compared here, deliberately.
                    #
                    # verify_with above already required the record at the
                    # checkpoint's offset to BE at cp.seq and to hash to
                    # cp.head_hash. A checkpoint whose head hash is wrong
                    # dies there, with a message about the log's bytes,
                    # before this loop runs. A comparison here would be a
                    # guard no mutation can kill -- which this repository
                    # has removed four times already rather than keep as a
                    # line that looks like protection and is not.
                    #
                    # The record still CARRIES head_hash: the second reader
                    # checks it against the record actually at that seq,
                    # which is a different question asked by a different
                    # reader, and that one is killable.
                    anchored = True
            store._apply(ev)
        if not anchored:
            raise StoreError(
                f"checkpoint at seq {cp.seq} is not anchored in the log: no "
                "checkpoint.state record names that position. A snapshot "
                "nothing in the hash chain vouches for is a file, and a file "
                "is not evidence about a history.")
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
        """Records that carry canonical authority, foundations included.

        A RECORD IS CANONICAL WHEN ITS STATE SAYS SO **AND** EVERYTHING IT
        RESTS ON IS CANONICAL TOO.

        This used to be the first half alone, and the second half was
        somebody's job to remember. ``store.py`` applies one event at a time
        and never looks at dependents; :mod:`qta_agent.invalidation` cascades
        only when a caller runs it. So revoking a foundation left every
        record promoted on the strength of it still PROMOTED, still returned
        here, and still described as canonical authority -- with every
        individual transition legal and nothing in the enforcement path able
        to say otherwise. The auditor reported it as a provenance gap, which
        is an observation after the fact rather than an answer to "what is
        canonical right now".

        It is transitive by construction, because the shortcut is the bug
        :mod:`qta_agent.invalidation` was written against: marking, or here
        excluding, only the immediate children leaves the grandchild
        canonical on the same withdrawn input.

        A dependency cycle resolves to NOT canonical. A cycle is a modelling
        error, and the fail-closed answer to "is this authority sound" when
        the graph cannot say is no.

        This does not change any record's ``state``: a withdrawal still
        leaves its dependents PROMOTED until a cascade runs, and the audit
        gap for that is still the right report. What changes is that reading
        the canonical set no longer hands back authority resting on an input
        nobody believes any more.
        """
        verdict: dict = {}

        def sound(rid: str, seen: frozenset) -> bool:
            if rid in verdict:
                return verdict[rid]
            if rid in seen:
                return False                  # a cycle decides nothing
            rec = self._records.get(rid)
            if rec is None or rec.state is not State.PROMOTED:
                return False
            ok = all(sound(dep, seen | {rid}) for dep in rec.depends_on)
            verdict[rid] = ok
            return ok

        return {k: v for k, v in self._records.items()
                if sound(k, frozenset())}

    # ---- writes -------------------------------------------------------
    def create(self, *, record_id: str, kind: str, proposer: str,
               evidence: dict | None = None, depends_on: tuple = (),
               policy_id: str | None = None,
               idempotency_key: str | None = None) -> Record:
        # Read the log before deciding anything. Every check below is about
        # the state of the store, and this projection's copy of it is only
        # current until another process writes.
        self.catch_up()
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
        evidence = dict(evidence or {})
        for k, v in evidence.items():
            if not is_digest(v):
                raise StoreError(
                    f"evidence {k!r} must be a sha256 digest so it cannot be "
                    f"altered after being cited; got {type(v).__name__}")
        self._require_evidence_exists(evidence)

        def build():
            # Re-checked on every attempt, against the log as it stands.
            # "This id is free" and "these dependencies exist" are both
            # facts about a moment, and another process creating the same id
            # between the check and the write is exactly the race.
            if record_id in self._records:
                raise StoreError(f"record {record_id!r} already exists")
            for dep in depends_on:
                if dep not in self._records:
                    raise StoreError(
                        f"dependency {dep!r} does not exist; a record may "
                        "not depend on something unrecorded")
            return dict(
                actor=proposer, action=ACT_CREATE, target=record_id,
                payload={"record_id": record_id, "kind": kind,
                         "proposer": proposer, "state": INITIAL.value,
                         "evidence": evidence,
                         "depends_on": list(depends_on),
                         "policy_id": policy_id,
                         "idempotency_key": idempotency_key})

        self._append_decided(build)
        return self.get(record_id)

    def transition(self, *, record_id: str, dst: State, actor: str,
                   role: Role, evidence: dict | None = None,
                   policy_id: str | None = None,
                   expected_revision: int | None = None,
                   stale_reason: str | None = None,
                   idempotency_key: str | None = None) -> Record:
        """Authorize and commit a state transition."""
        self.catch_up()
        if idempotency_key:
            done_for = self._applied_keys.get(idempotency_key)
            if done_for is not None:
                if done_for != record_id:
                    raise StoreError(
                        f"idempotency key {idempotency_key!r} already "
                        f"completed a request for {done_for!r}; reusing it "
                        f"for {record_id!r} would return the wrong record")
                return self.get(record_id)
        evidence = dict(evidence or {})

        def build():
            # EVERY decision here is re-made against the log at the instant
            # of the write: the source state, the revision, the edge and the
            # evidence. A transition recorded from a src the replay has
            # already left leaves a perfectly valid chain nobody can replay.
            cur = self.get(record_id)
            if (expected_revision is not None
                    and cur.revision != expected_revision):
                raise ConcurrencyError(
                    f"{record_id}: expected revision {expected_revision}, "
                    f"found {cur.revision}; the record changed since it was "
                    "read")
            req = TransitionRequest(
                record_id=record_id, src=cur.state, dst=dst, actor=actor,
                role=role, evidence={**cur.evidence, **evidence},
                proposer=cur.proposer, policy_id=policy_id or cur.policy_id)
            # raises TransitionError if not permitted, including when a
            # cited digest does not resolve in the attached evidence store
            edge = check(req, resolve=self._resolver)
            return dict(
                actor=actor, action=ACT_TRANSITION, target=record_id,
                payload={"record_id": record_id, "src": cur.state.value,
                         "dst": dst.value, "role": role.value,
                         "evidence": evidence,
                         "policy_id": policy_id or cur.policy_id,
                         "stale_reason": stale_reason,
                         "edge_reason": edge.reason,
                         "idempotency_key": idempotency_key})

        self._append_decided(build)
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
        self.catch_up()

        def build():
            self.get(record_id)  # raises UnknownRecord if it does not exist
            for dep in depends_on:
                if dep not in self._records:
                    raise StoreError(f"dependency {dep!r} does not exist")
                if dep == record_id:
                    raise StoreError(
                        f"{record_id!r} cannot depend on itself")
            return dict(
                actor=actor, action=ACT_DEPEND, target=record_id,
                payload={"record_id": record_id,
                         "depends_on": list(depends_on)})

        self._append_decided(build)
        return self.get(record_id)
