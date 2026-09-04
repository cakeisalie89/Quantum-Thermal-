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

from dataclasses import dataclass, field, replace

from .authority import (
    INITIAL,
    Role,
    State,
    TransitionRequest,
    check,
)
from .canonical import is_digest
from .events import EventLog, Event


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
        self._applied_keys: set = set()
        self._loaded_through: int = -1

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
        self._applied_keys = set()
        self._loaded_through = -1
        for ev in self.log.read():
            self._apply(ev)
        return self

    def _apply(self, ev: Event) -> None:
        """Fold one event into the projection.

        Deliberately tolerant of nothing: an event the reducer does not
        understand is an error, not a skip. Silently ignoring an unknown
        action would let a future writer add authority-relevant events that
        older readers quietly drop.
        """
        p = ev.payload
        key = p.get("idempotency_key")
        if key:
            self._applied_keys.add(key)
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
            self._records[rid] = replace(
                cur, state=State(p["dst"]), revision=cur.revision + 1,
                evidence={**cur.evidence, **p.get("evidence", {})},
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
        else:
            raise StoreError(
                f"seq {ev.seq}: unknown action {ev.action!r}; refusing to "
                "project a log this reducer does not fully understand")
        self._loaded_through = ev.seq

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
        if record_id in self._records:
            raise StoreError(f"record {record_id!r} already exists")
        if idempotency_key and idempotency_key in self._applied_keys:
            return self.get(record_id)
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
        if idempotency_key and idempotency_key in self._applied_keys:
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
