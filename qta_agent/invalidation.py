"""Transitive dependency invalidation.

The failure this prevents is specific and quiet: a parameter changes, the
result that used it is marked stale, and a *third* record that was promoted on
the strength of that result stays canonical. Authority then rests on an input
nobody believes any more. Marking only immediate children is the common
shortcut and it is exactly the bug.

Traversal is over the reverse edges (dependents), breadth-first, with an
explicit visited set. Cycles are tolerated rather than rejected at traversal
time: a dependency cycle is a modelling error worth reporting, but discovering
one midway through invalidation must not abort the invalidation and leave the
graph half-marked. :func:`find_cycles` reports them separately.

WHAT INVALIDATION CITES

Marking a record STALE requires ``invalidated_by`` evidence, and evidence is
referenced by content. :func:`_origin_evidence` therefore STORES the origin
and reason it cites rather than only hashing them; see its docstring for the
defect that distinction closes.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .authority import Role, State


@dataclass(frozen=True)
class InvalidationPlan:
    """What a change to ``origin`` implies, computed before anything moves."""
    origin: str
    #: Records that must become STALE, in breadth-first order.
    affected: tuple
    #: record_id -> the path from origin that reaches it. Answers "why".
    paths: dict
    #: Records that would be affected but are already terminal or stale.
    skipped: dict
    #: Dependency cycles observed during traversal.
    cycles: tuple = ()

    def explain(self, record_id: str) -> str:
        path = self.paths.get(record_id)
        if not path:
            return f"{record_id} is not affected by a change to {self.origin}"
        return " -> ".join(path)


def build_reverse_index(records: dict) -> dict:
    """Map each record to the records that depend on it."""
    rev: dict = {rid: set() for rid in records}
    for rid, rec in records.items():
        for dep in rec.depends_on:
            if dep in rev:
                rev[dep].add(rid)
            # A dangling dependency is a data error, not a traversal error;
            # the store refuses to create one, so reaching here means the log
            # was written by something else.
    return {k: tuple(sorted(v)) for k, v in rev.items()}


def _invalidatable_states() -> frozenset:
    """States that actually have an edge to STALE, read from the table.

    Derived rather than restated. An earlier version kept its own list of
    "not invalidatable" states and got it wrong: PROPOSED and UNDER_REVIEW
    were treated as invalidatable, but the transition table has no edge from
    either into STALE, so the plan proposed a transition the state machine
    then refused. Hypothesis found it by driving invalidation over an
    arbitrary graph.

    The semantics are right as well as convenient. A record that has not yet
    been verified has made no claim that a changed dependency could falsify;
    it was never fresh, so it cannot go stale. Only VERIFIED and PROMOTED
    assert something about their inputs.
    """
    from .authority import EDGES
    return frozenset(e.src for e in EDGES if e.dst is State.STALE)


#: Computed once from the transition table so the two cannot drift apart.
INVALIDATABLE: frozenset = _invalidatable_states()


def plan_invalidation(records: dict, origin: str) -> InvalidationPlan:
    """Compute the full transitive consequence of ``origin`` changing."""
    if origin not in records:
        raise KeyError(f"unknown record {origin!r}")
    rev = build_reverse_index(records)
    affected: list = []
    paths: dict = {origin: [origin]}
    skipped: dict = {}
    cycles: list = []
    seen = {origin}
    queue = deque([origin])
    while queue:
        cur = queue.popleft()
        for child in rev.get(cur, ()):
            if child in seen:
                # Revisiting a node already on a path from origin means the
                # dependency graph loops. Record it; do not stop.
                if child in paths and cur in paths:
                    cycles.append(tuple(paths[cur] + [child]))
                continue
            seen.add(child)
            paths[child] = paths[cur] + [child]
            state = records[child].state
            if state not in INVALIDATABLE:
                skipped[child] = state.value
                # Still traverse through it: a SUPERSEDED record's dependents
                # may themselves be live and must be reached.
                queue.append(child)
                continue
            affected.append(child)
            queue.append(child)
    return InvalidationPlan(origin=origin, affected=tuple(affected),
                            paths=paths, skipped=skipped,
                            cycles=tuple(dict.fromkeys(cycles)))


def find_cycles(records: dict) -> tuple:
    """All dependency cycles, by depth-first search with an active stack."""
    cycles: list = []
    visiting: set = set()
    done: set = set()
    stack: list = []

    def walk(rid: str) -> None:
        if rid in done:
            return
        if rid in visiting:
            if rid in stack:
                cycles.append(tuple(stack[stack.index(rid):] + [rid]))
            return
        visiting.add(rid)
        stack.append(rid)
        for dep in records[rid].depends_on:
            if dep in records:
                walk(dep)
        stack.pop()
        visiting.discard(rid)
        done.add(rid)

    for rid in sorted(records):
        walk(rid)
    return tuple(dict.fromkeys(cycles))


def apply_invalidation(store, origin: str, *, reason: str,
                       actor: str = "SYSTEM") -> InvalidationPlan:
    """Execute a plan: mark every transitively affected record STALE.

    The plan is computed first, in full, then applied. Computing and applying
    in one pass would let a mid-traversal failure leave the graph partially
    invalidated -- some records stale, others still canonical on the same
    dead input.
    """
    plan = plan_invalidation(store.all_records(), origin)
    cited = _origin_evidence(store, origin, reason)
    for rid in plan.affected:
        why = plan.explain(rid)
        store.transition(
            record_id=rid, dst=State.STALE, actor=actor, role=Role.SYSTEM,
            evidence={"invalidated_by": cited},
            stale_reason=f"{reason}: {why}")
    return plan


def _origin_evidence(store, origin: str, reason: str) -> str:
    """The digest cited as ``invalidated_by`` -- STORED, not just computed.

    A DEFECT THIS CLOSES, AND WHY EVERY TEST MISSED IT

    This used to return ``digest({...})`` and store nothing. Against a store
    with no evidence store attached that works, because :func:`authority.check`
    only resolves citations when it has a resolver -- and every test of this
    module used exactly that configuration. Attach an evidence store, which is
    what the governed path does, and the citation is checked, does not resolve,
    and the cascade raises on its FIRST transition.

    The consequence was not a cosmetic one. In the only configuration where
    evidence means anything, dependency invalidation could not run: a record
    whose foundation had been withdrawn could never be marked STALE, so
    canonical authority would rest on a withdrawn input with no way to correct
    it. :meth:`AuditIndex.explain_record` reports that state as a provenance
    gap; before this fix the gap was unfixable.

    The rule the old code broke is the one this package states everywhere
    else: a digest is a NAME, and a name that resolves to nothing is an
    assertion, not evidence. Invalidation was asserting.

    With no evidence store attached the digest is still returned unstored --
    the store has no resolver, so there is nothing to resolve against, and
    refusing here would make the state machine untestable without a
    filesystem for no gain in safety.
    """
    from .canonical import canonical_bytes, digest

    body = {"origin": origin, "reason": reason}
    evidence = getattr(store, "evidence", None)
    if evidence is None:
        return digest(body)
    return evidence.put(canonical_bytes(body), media_type="application/json")
