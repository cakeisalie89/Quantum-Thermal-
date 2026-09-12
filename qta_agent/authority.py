"""The authority state machine: what may become canonical, and how.

This is the subsystem an adversary -- or a more capable future model -- would
attack first, because everything downstream trusts its verdict. It is
therefore specified as an explicit transition table rather than scattered
``if`` statements, so that the reachable states are enumerable and the
forbidden ones are provably unreachable rather than merely unwritten.

THE INVARIANTS, AND WHY EACH EXISTS

I1  PROMOTED is reachable only from VERIFIED.
    Promotion is the moment a claim becomes canonical. Allowing it from
    PROPOSED would make verification optional in practice.

I2  REVOKED and REJECTED are terminal.
    Withdrawn authority that can be re-promoted is not withdrawn. Recovery
    from these states requires a NEW record with new evidence, which leaves
    an audit trail; silently reviving the old one would not.

I3  STALE cannot return to PROMOTED directly.
    A dependency changed, so the prior verification no longer describes the
    current inputs. The path back runs through VERIFIED, which requires fresh
    evidence.

I4  Separation of duties: the proposer may not verify their own record.
    An agent that can propose and verify has no verification at all -- only a
    more expensive way to assert.

I5  Policy cannot self-authorize.
    A transition is evaluated against the policy identity recorded when the
    transition is requested. Changing policy does not retroactively bless
    transitions already made, and a policy change is itself an event.

I6  Every transition requires the evidence its edge declares.
    Evidence is referenced by digest, so a transition cannot cite evidence
    that does not exist or has since changed.

CONCURRENCY

Transitions are applied through the event log, which is totally ordered by
``seq``. Two concurrent attempts to transition the same record both append;
the second observes the first's resulting state and is rejected if that state
no longer permits the edge. Last-writer-wins is never used for authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet


class State(str, Enum):
    """Authority states. ``str`` mixin so they serialize canonically."""

    PROPOSED = "PROPOSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    VERIFIED = "VERIFIED"
    PROMOTED = "PROMOTED"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"
    REJECTED = "REJECTED"


class Role(str, Enum):
    """Who may act. Distinct from identity: one identity may hold several."""

    PROPOSER = "PROPOSER"
    VERIFIER = "VERIFIER"
    PROMOTER = "PROMOTER"
    #: Automatic transitions with no human or agent author -- dependency
    #: invalidation, supersession. SYSTEM can never promote.
    SYSTEM = "SYSTEM"


#: States from which nothing may leave. Encoded once, asserted in tests.
TERMINAL: FrozenSet[State] = frozenset({State.REVOKED, State.REJECTED})

#: The only state carrying canonical authority.
CANONICAL: FrozenSet[State] = frozenset({State.PROMOTED})


class TransitionError(Exception):
    """A transition was refused. Authority never fails open."""


@dataclass(frozen=True)
class Edge:
    """One permitted transition."""
    src: State
    dst: State
    #: Roles permitted to trigger it. Empty means nobody -- unreachable.
    roles: FrozenSet[Role]
    #: Evidence keys that must be present and digest-referenced.
    requires_evidence: FrozenSet[str] = frozenset()
    #: If True, the actor must differ from the record's proposer (I4).
    requires_distinct_actor: bool = False
    reason: str = ""


def _edges() -> tuple:
    E = Edge
    return (
        E(State.PROPOSED, State.UNDER_REVIEW, frozenset({Role.VERIFIER}),
          reason="a verifier picks up the claim"),
        E(State.PROPOSED, State.REJECTED, frozenset({Role.VERIFIER}),
          requires_evidence=frozenset({"rejection_reason"}),
          requires_distinct_actor=True,
          reason="refused before review completed"),
        E(State.UNDER_REVIEW, State.VERIFIED, frozenset({Role.VERIFIER}),
          requires_evidence=frozenset({"verification_report"}),
          requires_distinct_actor=True,
          reason="verification passed (I4: not the proposer)"),
        E(State.UNDER_REVIEW, State.REJECTED, frozenset({Role.VERIFIER}),
          requires_evidence=frozenset({"rejection_reason"}),
          requires_distinct_actor=True,
          reason="verification failed"),
        # I1: the ONLY edge into PROMOTED.
        E(State.VERIFIED, State.PROMOTED, frozenset({Role.PROMOTER}),
          requires_evidence=frozenset({"verification_report", "policy_id"}),
          requires_distinct_actor=True,
          reason="I1: promotion requires prior verification"),
        E(State.VERIFIED, State.STALE, frozenset({Role.SYSTEM}),
          requires_evidence=frozenset({"invalidated_by"}),
          reason="a dependency changed before promotion"),
        E(State.PROMOTED, State.STALE, frozenset({Role.SYSTEM}),
          requires_evidence=frozenset({"invalidated_by"}),
          reason="a dependency of canonical state changed"),
        E(State.PROMOTED, State.SUPERSEDED, frozenset({Role.PROMOTER}),
          requires_evidence=frozenset({"superseded_by"}),
          reason="a newer record takes over"),
        E(State.PROMOTED, State.REVOKED, frozenset({Role.PROMOTER}),
          requires_evidence=frozenset({"revocation_reason"}),
          reason="authority withdrawn"),
        E(State.VERIFIED, State.REVOKED, frozenset({Role.PROMOTER}),
          requires_evidence=frozenset({"revocation_reason"}),
          reason="withdrawn before promotion"),
        # I3: STALE returns only through re-verification.
        E(State.STALE, State.UNDER_REVIEW, frozenset({Role.VERIFIER}),
          reason="I3: re-verification is the only route back"),
        E(State.STALE, State.SUPERSEDED, frozenset({Role.PROMOTER}),
          requires_evidence=frozenset({"superseded_by"}),
          reason="replaced rather than revalidated"),
        E(State.STALE, State.REVOKED, frozenset({Role.PROMOTER}),
          requires_evidence=frozenset({"revocation_reason"}),
          reason="withdrawn while stale"),
        E(State.SUPERSEDED, State.REVOKED, frozenset({Role.PROMOTER}),
          requires_evidence=frozenset({"revocation_reason"}),
          reason="withdrawn after supersession"),
    )


EDGES: tuple = _edges()

#: Fast lookup, built once. (src, dst) -> Edge
_BY_PAIR = {(e.src, e.dst): e for e in EDGES}

#: The initial state of any newly created record.
INITIAL: State = State.PROPOSED


def allowed_targets(src: State) -> FrozenSet[State]:
    return frozenset(e.dst for e in EDGES if e.src == src)


def find_edge(src: State, dst: State) -> Edge | None:
    return _BY_PAIR.get((src, dst))


@dataclass(frozen=True)
class TransitionRequest:
    """A proposed transition, evaluated as a whole."""
    record_id: str
    src: State
    dst: State
    actor: str
    role: Role
    #: Evidence key -> digest. Values must be sha256 digests (I6).
    evidence: dict = field(default_factory=dict)
    #: The record's original proposer, for I4.
    proposer: str | None = None
    #: Policy identity in force when this was requested (I5).
    policy_id: str | None = None


def check(req: TransitionRequest, *, resolve=None) -> Edge:
    """Authorize a transition, or raise. This is the whole gate.

    Returns the :class:`Edge` that permits it so the caller can record which
    rule applied -- an audit trail of *why*, not merely *that*.

    ``resolve`` is an optional predicate over a digest, satisfied when the
    named content is actually held (``EvidenceStore.contains``). Without it,
    I6 is enforced syntactically: evidence must *look like* a digest. That is
    not nothing -- it stops a free-text "verified by me" from being recorded
    as evidence -- but it does not stop a fabricated citation, because every
    64-character hex string looks exactly like every other one. Supply a
    resolver wherever a transition can make something canonical.

    It is optional rather than mandatory because the state machine must stay
    testable without a filesystem, and because a replay verifying historical
    transitions may legitimately run against a store that no longer holds
    long-expired evidence; forcing resolution there would turn an archival
    policy into a retroactive authority failure.
    """
    from .canonical import is_digest

    if req.src in TERMINAL:
        raise TransitionError(
            f"I2: {req.src.value} is terminal; {req.record_id} cannot "
            "leave it. Create a new record with new evidence instead.")

    edge = find_edge(req.src, req.dst)
    if edge is None:
        raise TransitionError(
            f"no edge {req.src.value} -> {req.dst.value}; permitted targets "
            f"are {sorted(s.value for s in allowed_targets(req.src))}")

    if req.role not in edge.roles:
        raise TransitionError(
            f"role {req.role.value} may not perform "
            f"{req.src.value} -> {req.dst.value}; requires one of "
            f"{sorted(r.value for r in edge.roles)}")

    if edge.requires_distinct_actor:
        if req.proposer is None:
            raise TransitionError(
                f"I4: {req.src.value} -> {req.dst.value} requires a distinct "
                "actor, but the record's proposer is unknown; refusing rather "
                "than assuming separation of duties")
        if req.actor == req.proposer:
            raise TransitionError(
                f"I4: {req.actor!r} proposed {req.record_id} and may not also "
                f"perform {req.src.value} -> {req.dst.value}")

    missing = sorted(edge.requires_evidence - set(req.evidence))
    if missing:
        raise TransitionError(
            f"I6: {req.src.value} -> {req.dst.value} requires evidence "
            f"{missing}")

    for key in sorted(edge.requires_evidence):
        val = req.evidence.get(key)
        # policy_id is an identity, not a content digest; everything else is
        # referenced by digest so it cannot be cited and then changed.
        if key == "policy_id":
            if not isinstance(val, str) or not val:
                raise TransitionError("I5: policy_id must be a non-empty id")
            continue
        if not is_digest(val):
            raise TransitionError(
                f"I6: evidence {key!r} must be a sha256 digest, got "
                f"{type(val).__name__}; evidence is referenced by content so "
                "it cannot be altered after being cited")

    if req.dst is State.PROMOTED and req.policy_id is None:
        raise TransitionError(
            "I5: promotion requires an explicit policy identity in force")

    if resolve is not None:
        # I6, completed: the citation must name content that exists. Checked
        # over the whole evidence mapping, not only the required keys -- an
        # extra key holding a fabricated digest is still a fabricated
        # citation, and it will be read as evidence by anyone auditing this
        # record later.
        from .evidence import UnknownEvidence, require_resolvable
        try:
            require_resolvable(req.evidence, resolve)
        except UnknownEvidence as exc:
            raise TransitionError(f"I6: {exc}") from exc

    return edge


def is_canonical(state: State) -> bool:
    return state in CANONICAL
