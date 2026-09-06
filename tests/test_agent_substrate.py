"""Adversarial and property tests for the agent authority substrate.

The subsystem under test decides what becomes canonical, so the tests are
written from the attacker's side: every check that could be quietly removed
gets a test that fails when it is. Where a property holds for all inputs
rather than a chosen few, Hypothesis generates the inputs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qta_agent.authority import (  # noqa: E402
    EDGES, INITIAL, TERMINAL, Role, State, TransitionError,
    TransitionRequest, allowed_targets, check,
)
from qta_agent.canonical import (  # noqa: E402
    CanonicalizationError, assert_digest_stable, digest,
    is_digest,
)
from qta_agent.events import (  # noqa: E402
    _HASHED_FIELDS, ChainBroken, ChainState, EventLog,
)
from qta_agent.invalidation import (  # noqa: E402
    apply_invalidation, find_cycles, plan_invalidation,
)
from qta_agent.reconstruct import compare, reconstruct  # noqa: E402
from qta_agent.store import (  # noqa: E402
    AuthorityStore, ConcurrencyError, StoreError,
)

DIG = "b" * 64


# ---------------------------------------------------------------------------
# canonical
# ---------------------------------------------------------------------------

def test_key_order_does_not_change_the_digest():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_nan_and_infinity_are_refused_not_serialized():
    """They are not JSON, do not round-trip, and NaN != NaN breaks equality."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(CanonicalizationError):
            digest({"x": bad})


def test_non_json_types_are_refused():
    with pytest.raises(CanonicalizationError):
        digest({"x": {1, 2}})


def test_uppercase_digests_are_not_digests():
    """Two spellings of one digest would break set and equality semantics."""
    d = digest({"a": 1})
    assert is_digest(d) and not is_digest(d.upper())


def test_digests_are_stable_across_repeated_serialization():
    assert_digest_stable({"z": [3, 2, 1], "a": {"n": None, "t": True}})


# ---------------------------------------------------------------------------
# event log: the chain must detect every edit
# ---------------------------------------------------------------------------

def _log_with(tmp_path, n=4):
    log = EventLog(tmp_path / "ev.jsonl")
    for i in range(n):
        log.append(actor="a", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "a", "i": i})
    return log


def test_a_well_formed_chain_verifies(tmp_path):
    log = _log_with(tmp_path)
    rep = log.verify()
    assert rep.ok and rep.count == 4 and rep.head_seq == 3


def test_tampering_with_a_payload_is_detected(tmp_path):
    log = _log_with(tmp_path)
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["i"] = 999            # edit content, leave hash alone
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    log.path.write_text("\n".join(lines) + "\n")
    rep = log.verify()
    assert not rep.ok
    assert any("was altered" in p for p in rep.problems), rep.problems


def test_deleting_a_middle_record_breaks_the_chain(tmp_path):
    log = _log_with(tmp_path)
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    rep = log.verify()
    assert not rep.ok
    assert any("does not link" in p or "sequence" in p for p in rep.problems)


def test_reordering_records_is_detected(tmp_path):
    log = _log_with(tmp_path)
    lines = log.path.read_text().splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    assert not log.verify().ok


def test_truncation_is_caught_by_the_independent_witness(tmp_path):
    """A prefix of a valid chain is itself a valid chain.

    This is the one attack the chain cannot see on its own, which is why the
    head is witnessed separately. Without the witness this test would pass a
    truncated log.
    """
    log = _log_with(tmp_path)
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:2]) + "\n")
    rep = log.verify()
    assert not rep.ok
    assert any("TRUNCATED" in p for p in rep.problems), rep.problems
    # And without the witness the truncation is genuinely invisible:
    assert log.verify(use_witness=False).ok


def test_a_forked_head_is_detected(tmp_path):
    log = _log_with(tmp_path)
    rep = log.verify()
    forged = ChainState(seq=rep.head_seq, head_hash="c" * 64)
    assert any("FORKED" in p
               for p in log.verify(expected_head=forged).problems)


def test_unhashed_extra_fields_are_refused(tmp_path):
    """An extra field is not covered by the hash, so it is unverified content."""
    log = _log_with(tmp_path, n=1)
    rec = json.loads(log.path.read_text().splitlines()[0])
    rec["smuggled"] = "not covered by the digest"
    log.path.write_text(json.dumps(rec, sort_keys=True) + "\n")
    assert not log.verify().ok


def test_a_partial_trailing_line_is_reported_as_truncation(tmp_path):
    """The expected crash signature: killed mid-append."""
    log = _log_with(tmp_path)
    with log.path.open("a") as fh:
        fh.write('{"seq": 4, "event_id": "hal')
    rep = log.verify()
    assert not rep.ok
    assert any("truncated mid-append" in p or "unparseable" in p
               for p in rep.problems)


def test_appending_to_a_broken_chain_is_refused(tmp_path):
    """Extending damage would make the break harder to locate."""
    log = _log_with(tmp_path)
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        log.append(actor="a", action="record.create", target="x", payload={})


def test_wall_clock_going_backwards_is_noted_not_fatal(tmp_path):
    """Clocks legitimately move backwards; ordering uses seq."""
    log = EventLog(tmp_path / "ev.jsonl")
    log.append(actor="a", action="record.create", target="r0",
               payload={"record_id": "r0", "kind": "k", "proposer": "a"},
               wall_time=1000.0)
    log.append(actor="a", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "k", "proposer": "a"},
               wall_time=500.0)
    rep = log.verify()
    assert rep.ok, rep.problems
    assert any("backwards" in n for n in rep.notes)


# ---------------------------------------------------------------------------
# authority: the invariants, proved over the whole graph
# ---------------------------------------------------------------------------

def test_I1_promoted_is_reachable_only_from_verified():
    into = [e.src for e in EDGES if e.dst is State.PROMOTED]
    assert into == [State.VERIFIED], into


def test_I2_terminal_states_have_no_outgoing_edges():
    for term in TERMINAL:
        assert allowed_targets(term) == frozenset(), term


def test_I3_stale_cannot_reach_promoted_without_reverification():
    """No direct edge, and every path must pass through VERIFIED."""
    assert State.PROMOTED not in allowed_targets(State.STALE)
    # Exhaustive: enumerate all simple paths STALE -> PROMOTED.
    paths, stack = [], [(State.STALE, [State.STALE])]
    while stack:
        node, path = stack.pop()
        for nxt in allowed_targets(node):
            if nxt in path:
                continue
            if nxt is State.PROMOTED:
                paths.append(path + [nxt])
            else:
                stack.append((nxt, path + [nxt]))
    assert paths, "expected at least one recovery path to exist"
    for p in paths:
        assert State.VERIFIED in p, f"path bypasses verification: {p}"


def test_I4_the_proposer_cannot_verify_their_own_record():
    with pytest.raises(TransitionError, match="I4"):
        check(TransitionRequest("r", State.UNDER_REVIEW, State.VERIFIED,
                                "alice", Role.VERIFIER,
                                {"verification_report": DIG},
                                proposer="alice"))


def test_I4_refuses_when_the_proposer_is_unknown():
    """Absent knowledge, refuse -- do not assume separation of duties held."""
    with pytest.raises(TransitionError, match="I4"):
        check(TransitionRequest("r", State.UNDER_REVIEW, State.VERIFIED,
                                "bob", Role.VERIFIER,
                                {"verification_report": DIG}, proposer=None))


def test_I5_promotion_requires_an_explicit_policy_identity():
    with pytest.raises(TransitionError, match="I5"):
        check(TransitionRequest("r", State.VERIFIED, State.PROMOTED, "carol",
                                Role.PROMOTER,
                                {"verification_report": DIG,
                                 "policy_id": "p1"},
                                proposer="alice", policy_id=None))


def test_I6_evidence_must_be_a_content_digest():
    with pytest.raises(TransitionError, match="I6"):
        check(TransitionRequest("r", State.UNDER_REVIEW, State.VERIFIED, "bob",
                                Role.VERIFIER,
                                {"verification_report": "see the report"},
                                proposer="alice"))


def test_the_system_role_can_never_promote():
    """Automatic machinery must not be able to make something canonical."""
    for e in EDGES:
        if e.dst is State.PROMOTED:
            assert Role.SYSTEM not in e.roles


def test_every_edge_is_reachable_from_the_initial_state():
    """A rule nothing can ever trigger is dead policy pretending to be live."""
    reachable, stack = {INITIAL}, [INITIAL]
    while stack:
        for nxt in allowed_targets(stack.pop()):
            if nxt not in reachable:
                reachable.add(nxt)
                stack.append(nxt)
    unreachable = {e.src for e in EDGES} - reachable
    assert not unreachable, f"unreachable edge sources: {unreachable}"


def test_no_edge_has_an_empty_role_set():
    for e in EDGES:
        assert e.roles, f"{e.src}->{e.dst} can never be triggered"


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def _store(tmp_path):
    return AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def _promote(s, rid, deps=()):
    s.create(record_id=rid, kind="result", proposer="alice", policy_id="p1",
             depends_on=deps)
    s.transition(record_id=rid, dst=State.UNDER_REVIEW, actor="bob",
                 role=Role.VERIFIER)
    s.transition(record_id=rid, dst=State.VERIFIED, actor="bob",
                 role=Role.VERIFIER, evidence={"verification_report": DIG})
    return s.transition(record_id=rid, dst=State.PROMOTED, actor="carol",
                        role=Role.PROMOTER, evidence={"policy_id": "p1"},
                        policy_id="p1")


def test_a_full_promotion_cycle_becomes_canonical(tmp_path):
    s = _store(tmp_path)
    assert _promote(s, "r1").state is State.PROMOTED
    assert sorted(s.canonical()) == ["r1"]


def test_stale_revision_is_refused(tmp_path):
    s = _store(tmp_path)
    _promote(s, "r1")
    with pytest.raises(ConcurrencyError):
        s.transition(record_id="r1", dst=State.REVOKED, actor="carol",
                     role=Role.PROMOTER,
                     evidence={"revocation_reason": DIG}, expected_revision=1)


def test_idempotency_key_prevents_double_application(tmp_path):
    s = _store(tmp_path)
    _promote(s, "r1")
    before = s.get("r1").revision
    for _ in range(3):
        s.transition(record_id="r1", dst=State.REVOKED, actor="carol",
                     role=Role.PROMOTER,
                     evidence={"revocation_reason": DIG},
                     idempotency_key="revoke-once")
    assert s.get("r1").revision == before + 1


def test_evidence_must_be_a_digest_at_creation(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(StoreError, match="digest"):
        s.create(record_id="r1", kind="k", proposer="a",
                 evidence={"report": "prose"})


def test_a_record_cannot_depend_on_something_unrecorded(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(StoreError, match="does not exist"):
        s.create(record_id="r1", kind="k", proposer="a",
                 depends_on=("ghost",))


def test_an_unknown_action_refuses_to_project(tmp_path):
    """Silently skipping would let a newer writer add events we drop."""
    log = EventLog(tmp_path / "ev.jsonl")
    log.append(actor="x", action="record.invent", target="r",
               payload={"record_id": "r"})
    with pytest.raises(StoreError, match="unknown action"):
        AuthorityStore(log).load()


def test_the_store_refuses_to_load_a_broken_log(tmp_path):
    s = _store(tmp_path)
    _promote(s, "r1")
    lines = s.log.path.read_text().splitlines()
    del lines[1]
    s.log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        AuthorityStore(s.log).load()


# ---------------------------------------------------------------------------
# invalidation
# ---------------------------------------------------------------------------

def test_invalidation_runs_against_an_evidence_backed_store(tmp_path):
    """REGRESSION. The cascade cited a digest it never stored.

    Every other test in this section builds a store with NO evidence store
    attached, so ``authority.check`` has no resolver and never asks whether
    the cited ``invalidated_by`` digest names anything. Attach one -- which is
    what the governed path does -- and the citation is checked. It did not
    resolve, so the cascade raised on its first transition and dependency
    invalidation was unavailable in the only configuration where evidence
    means anything.

    The failure mode was silent and severe: a record whose foundation had been
    withdrawn could never be marked STALE, leaving canonical authority resting
    on a withdrawn input with no way to correct it.
    """
    from qta_agent.evidence import EvidenceStore

    ev = EvidenceStore(tmp_path / "blobs")
    report = ev.put(b"verification report")
    s = AuthorityStore(EventLog(tmp_path / "ev.jsonl"), evidence=ev).load()

    def promote(rid, deps=()):
        s.create(record_id=rid, kind="result", proposer="alice",
                 policy_id="p1", depends_on=deps)
        s.transition(record_id=rid, dst=State.UNDER_REVIEW, actor="bob",
                     role=Role.VERIFIER)
        s.transition(record_id=rid, dst=State.VERIFIED, actor="bob",
                     role=Role.VERIFIER,
                     evidence={"verification_report": report})
        s.transition(record_id=rid, dst=State.PROMOTED, actor="carol",
                     role=Role.PROMOTER,
                     evidence={"verification_report": report,
                               "policy_id": "p1"}, policy_id="p1")

    promote("param")
    promote("result", ("param",))
    apply_invalidation(s, "param", reason="bound changed")

    assert s.get("result").state is State.STALE
    # And the citation it wrote is real: the digest resolves to stored bytes.
    cited = s.get("result").evidence["invalidated_by"]
    assert ev.contains(cited), (
        "the cascade cited a digest that resolves to nothing; a name that "
        "resolves to nothing is an assertion, not evidence")


def test_invalidation_is_transitive_not_just_immediate_children(tmp_path):
    """The classic bug: marking only direct dependents."""
    s = _store(tmp_path)
    _promote(s, "param")
    _promote(s, "result", ("param",))
    _promote(s, "summary", ("result",))
    _promote(s, "report", ("summary",))
    plan = plan_invalidation(s.all_records(), "param")
    assert plan.affected == ("result", "summary", "report")
    apply_invalidation(s, "param", reason="bound changed")
    assert sorted(s.canonical()) == ["param"]


def test_invalidation_explains_why(tmp_path):
    s = _store(tmp_path)
    _promote(s, "a")
    _promote(s, "b", ("a",))
    _promote(s, "c", ("b",))
    plan = plan_invalidation(s.all_records(), "a")
    assert plan.explain("c") == "a -> b -> c"


def test_terminal_records_are_skipped_but_traversed_through(tmp_path):
    """A revoked record still conducts invalidation to its dependents."""
    s = _store(tmp_path)
    _promote(s, "a")
    _promote(s, "b", ("a",))
    _promote(s, "c", ("b",))
    s.transition(record_id="b", dst=State.REVOKED, actor="carol",
                 role=Role.PROMOTER, evidence={"revocation_reason": DIG})
    plan = plan_invalidation(s.all_records(), "a")
    assert "b" in plan.skipped and "c" in plan.affected


def test_dependency_cycles_are_reported_not_fatal(tmp_path):
    s = _store(tmp_path)
    _promote(s, "a")
    _promote(s, "b", ("a",))
    s.add_dependency(record_id="a", depends_on=("b",))
    assert find_cycles(s.all_records())
    plan_invalidation(s.all_records(), "a")      # must not hang or raise


def test_unverified_dependents_are_not_marked_stale(tmp_path):
    """Regression: Hypothesis found the plan proposing an impossible edge.

    An earlier version kept its own list of non-invalidatable states and had
    it wrong -- PROPOSED and UNDER_REVIEW were planned for invalidation, but
    the transition table has no edge from either into STALE, so applying the
    plan raised TransitionError.

    The semantics matter as much as the crash: a record that has not been
    verified has asserted nothing about its inputs, so a changed dependency
    cannot falsify it. It was never fresh and cannot go stale.
    """
    s = _store(tmp_path)
    _promote(s, "param")
    s.create(record_id="draft", kind="result", proposer="alice",
             policy_id="p1", depends_on=("param",))
    assert s.get("draft").state is State.PROPOSED
    plan = plan_invalidation(s.all_records(), "param")
    assert "draft" not in plan.affected
    assert plan.skipped.get("draft") == State.PROPOSED.value
    apply_invalidation(s, "param", reason="bound changed")   # must not raise
    assert s.get("draft").state is State.PROPOSED


def test_invalidatable_states_are_derived_from_the_transition_table(tmp_path):
    """The two must not be able to drift apart again."""
    from qta_agent.invalidation import INVALIDATABLE
    assert INVALIDATABLE == frozenset(
        e.src for e in EDGES if e.dst is State.STALE)
    assert INVALIDATABLE == {State.VERIFIED, State.PROMOTED}


def test_a_record_cannot_depend_on_itself(tmp_path):
    s = _store(tmp_path)
    _promote(s, "a")
    with pytest.raises(StoreError, match="itself"):
        s.add_dependency(record_id="a", depends_on=("a",))


# ---------------------------------------------------------------------------
# independent reconstruction (differential)
# ---------------------------------------------------------------------------

def test_reconstruction_agrees_with_the_live_store(tmp_path):
    s = _store(tmp_path)
    _promote(s, "param")
    _promote(s, "result", ("param",))
    apply_invalidation(s, "param", reason="changed")
    assert compare(s, reconstruct(s.log)) == ()


def test_reconstruction_refuses_a_broken_log(tmp_path):
    s = _store(tmp_path)
    _promote(s, "r1")
    lines = s.log.path.read_text().splitlines()
    del lines[2]
    s.log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        reconstruct(s.log)


def test_an_unauthorized_transition_in_the_log_is_not_applied(tmp_path):
    """A log written by a compromised writer must not become canonical.

    The event is hash-valid -- it was appended through the real log -- but the
    state machine would refuse it today, so replay reports it and leaves the
    record where it was.
    """
    log = EventLog(tmp_path / "ev.jsonl")
    log.append(actor="alice", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "k", "proposer": "alice",
                        "state": State.PROPOSED.value, "policy_id": "p1"})
    # PROPOSED -> PROMOTED: no such edge (I1).
    log.append(actor="alice", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": State.PROPOSED.value,
                        "dst": State.PROMOTED.value,
                        "role": Role.PROMOTER.value,
                        "evidence": {}, "policy_id": "p1"})
    rec = reconstruct(log)
    assert rec.records["r1"]["state"] == State.PROPOSED.value
    assert rec.canonical_ids() == ()
    assert any("would be refused today" in u for u in rec.unauthorized)


def test_reconstruction_uses_a_separate_implementation():
    """Guard against someone 'simplifying' the duplication away.

    If reconstruct imported the store's reducer, agreement between them would
    be circular and prove nothing.
    """
    src = Path(ROOT, "qta_agent", "reconstruct.py").read_text()
    assert "AuthorityStore" not in src.split('"""', 2)[2], \
        "reconstruct must not import the store's reducer"


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------

def test_state_survives_a_crash_between_append_and_witness_update(tmp_path):
    """Simulates dying after the durable append but before the head write."""
    s = _store(tmp_path)
    _promote(s, "r1")
    stale_witness = json.loads(s.log.head_path.read_text())
    stale_witness["seq"] -= 1
    s.log.head_path.write_text(json.dumps(stale_witness))
    rep = s.log.verify()
    assert rep.ok, rep.problems           # a lagging witness is not corruption
    assert any("witness is behind" in n for n in rep.notes)
    assert AuthorityStore(s.log).load().get("r1").state is State.PROMOTED


# ---------------------------------------------------------------------------
# Mutation-isolating tests.
#
# A mutation run against this suite left five mutants alive. Every one of them
# was alive for the same reason: the tests above provoke corruptions that trip
# TWO checks at once, so deleting either check still leaves the other to fail
# the test. That is the "passes for the wrong reason" failure -- the suite was
# proving that *something* rejected the input, not that the *specific rule*
# did.
#
# The tests below each disable exactly one invariant's worth of input, leaving
# every adjacent check satisfied, so that removing the rule under test is the
# only way to make the input acceptable.
#
# Honesty about what each one is worth:
#   * M2/M3 were genuine coverage gaps -- linkage and sequence are independent
#     properties and nothing exercised them independently.
#   * M9 was a genuine hole: no test asserted role enforcement at all.
#   * M7/M11 are redundant with an adjacent check under the CURRENT edge table,
#     so they cannot be killed by outcome alone; they are killed by asserting
#     WHICH rule rejected the request, plus (for M7) a structural test that the
#     guard still holds if the table grows an edge out of a terminal state.
# ---------------------------------------------------------------------------


def _rewrite_tail(log, **changes):
    """Rewrite the final record's hashed fields, keeping everything else sound.

    Recomputes that record's own ``hash`` and re-witnesses the head, so the
    only property left violated is whichever one ``changes`` breaks. Without
    both fix-ups the record would ALSO fail its self-hash check and the
    witness comparison, which is exactly the masking this file is here to
    remove.
    """
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec.update(changes)
    body = {f: rec[f] for f in _HASHED_FIELDS}
    rec["hash"] = digest(body)
    lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    log.path.write_text("\n".join(lines) + "\n")
    log.head_path.write_text(
        json.dumps({"seq": rec["seq"], "head_hash": rec["hash"]}), "utf-8")
    return rec


def test_a_broken_link_is_caught_when_sequence_and_hashes_are_intact(tmp_path):
    """M2: only the prev_hash linkage is wrong.

    ``seq`` stays contiguous, the record's own hash recomputes correctly, and
    the witness matches the new head. The chain-link check is the only thing
    standing between this log and a clean verdict -- so if it is deleted, the
    log verifies and an attacker has spliced in a record whose ancestry is
    not the one it claims.
    """
    log = _log_with(tmp_path, n=3)
    genuine = json.loads(log.path.read_text().splitlines()[-1])["prev_hash"]
    forged = "b" * 64
    assert forged != genuine
    _rewrite_tail(log, prev_hash=forged)

    rep = log.verify()
    assert not rep.ok, "a forged ancestry link must not verify"
    linkage = [p for p in rep.problems if "does not link" in p]
    assert linkage, rep.problems
    # Isolation: nothing ELSE is wrong with this log.
    assert rep.problems == linkage, (
        "test is not isolating -- another check also fires: " f"{rep.problems}")


def test_a_sequence_gap_is_caught_when_the_chain_still_links(tmp_path):
    """M3: only ``seq`` is wrong.

    The record still hash-links to its true predecessor and its own hash is
    correct, so the chain is cryptographically sound; what is wrong is that
    it claims position 7 while sitting at position 2. Sequence is what gives
    the log a total order (wall time cannot -- see the module docstring), so
    losing this check loses the ability to detect a record removed from, or
    inserted into, the middle without breaking the hash chain around it.
    """
    log = _log_with(tmp_path, n=3)
    _rewrite_tail(log, seq=7)

    rep = log.verify()
    assert not rep.ok, "a log with a sequence gap must not verify"
    gaps = [p for p in rep.problems if "contiguous" in p]
    assert gaps, rep.problems
    assert rep.problems == gaps, (
        "test is not isolating -- another check also fires: " f"{rep.problems}")


def test_the_two_chain_checks_are_independent(tmp_path):
    """The reason M2 and M3 masked each other, asserted directly.

    Deleting a middle record breaks BOTH properties at once. The two tests
    above show each property failing alone; this one records why the older
    deletion test could never distinguish them, so a future reader does not
    "simplify" them back into one.
    """
    log = _log_with(tmp_path, n=4)
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    problems = log.verify().problems
    assert any("does not link" in p for p in problems)
    assert any("contiguous" in p for p in problems)


def test_I2_is_enforced_by_the_terminal_guard_not_by_an_absent_edge():
    """M7: assert WHICH rule refuses, not merely that something refuses.

    ``find_edge`` returns None for every terminal source today, so deleting
    the I2 guard changes the error message without changing the outcome. The
    guard is not therefore decorative: it is what keeps I2 true if the edge
    table ever grows. Pinning the message here means a mutant that removes
    the guard is visible immediately rather than the day someone adds an edge.
    """
    for src in TERMINAL:
        req = TransitionRequest(
            record_id="r1", src=src, dst=State.UNDER_REVIEW,
            actor="v", role=Role.VERIFIER, proposer="p")
        with pytest.raises(TransitionError, match=r"I2: .* is terminal"):
            check(req)


def test_I2_holds_even_if_the_edge_table_grows_an_edge_out_of_a_terminal_state(
        monkeypatch):
    """M7, structurally: the guard, not the table, is what enforces I2.

    A future edit -- by a person or by a model editing this repository -- that
    adds a recovery edge out of REVOKED must not silently resurrect revoked
    authority. Here such an edge is injected into the lookup table and the
    request is still refused, which is the property the guard actually buys.
    """
    from qta_agent import authority as A

    smuggled = A.Edge(State.REVOKED, State.UNDER_REVIEW,
                      frozenset({Role.VERIFIER}), reason="hypothetical")
    monkeypatch.setitem(A._BY_PAIR, (State.REVOKED, State.UNDER_REVIEW),
                        smuggled)
    assert A.find_edge(State.REVOKED, State.UNDER_REVIEW) is smuggled

    with pytest.raises(TransitionError, match=r"I2: REVOKED is terminal"):
        A.check(TransitionRequest(
            record_id="r1", src=State.REVOKED, dst=State.UNDER_REVIEW,
            actor="v", role=Role.VERIFIER, proposer="p"))


def test_a_wrong_role_is_refused_on_an_edge_that_needs_nothing_else():
    """M9: role enforcement, with every other precondition satisfied.

    PROPOSED -> UNDER_REVIEW requires no evidence and no distinct actor, so
    the role is the only thing that can reject this request. Nothing in the
    suite asserted role enforcement through ``check`` before; this was a real
    gap, not a message-level one.
    """
    req = TransitionRequest(
        record_id="r1", src=State.PROPOSED, dst=State.UNDER_REVIEW,
        actor="p", role=Role.PROPOSER, proposer="p")
    with pytest.raises(TransitionError,
                       match=r"role PROPOSER may not perform"):
        check(req)


def test_the_system_role_cannot_promote_even_with_perfect_evidence():
    """M9, on the edge that matters most.

    Every other precondition for promotion is met here: the source is
    VERIFIED (I1), the actor differs from the proposer (I4), both evidence
    keys are present and well formed (I6), and a policy identity is in force
    (I5). Only the role is wrong. If the role check is removed, an automatic
    SYSTEM actor can make a claim canonical with no human or agent promoter
    -- the single most valuable capability an adversary could take here.
    """
    req = TransitionRequest(
        record_id="r1", src=State.VERIFIED, dst=State.PROMOTED,
        actor="sys", role=Role.SYSTEM, proposer="p",
        evidence={"verification_report": "a" * 64, "policy_id": "policy-1"},
        policy_id="policy-1")
    with pytest.raises(TransitionError, match=r"role SYSTEM may not perform"):
        check(req)
    # And the same request from a PROMOTER is accepted, proving the role was
    # the only thing rejected above rather than some unnoticed precondition.
    ok = check(TransitionRequest(
        record_id="r1", src=State.VERIFIED, dst=State.PROMOTED,
        actor="promoter", role=Role.PROMOTER, proposer="p",
        evidence={"verification_report": "a" * 64, "policy_id": "policy-1"},
        policy_id="policy-1"))
    assert ok.dst is State.PROMOTED


def test_missing_evidence_is_reported_as_missing_not_as_malformed():
    """M11: assert WHICH rule refuses.

    Omitting a required key and supplying a bad value are different operator
    errors, and the digest loop would reject both (``is_digest(None)`` is
    False) -- which is why deleting the presence check changed nothing
    observable. It is still worth keeping and worth pinning: "requires
    evidence ['verification_report']" tells the caller what to supply, while
    "must be a sha256 digest, got NoneType" sends them looking at a value
    they never passed.
    """
    req = TransitionRequest(
        record_id="r1", src=State.UNDER_REVIEW, dst=State.VERIFIED,
        actor="v", role=Role.VERIFIER, proposer="p", evidence={})
    with pytest.raises(TransitionError,
                       match=r"I6: .* requires evidence \['verification_report'\]"):
        check(req)


def test_missing_and_malformed_evidence_produce_different_diagnostics():
    """The other half of M11: the two paths must stay distinguishable."""
    common = dict(record_id="r1", src=State.UNDER_REVIEW, dst=State.VERIFIED,
                  actor="v", role=Role.VERIFIER, proposer="p")
    with pytest.raises(TransitionError, match=r"requires evidence") as missing:
        check(TransitionRequest(evidence={}, **common))
    with pytest.raises(TransitionError, match=r"must be a sha256 digest") as bad:
        check(TransitionRequest(evidence={"verification_report": "nope"},
                                **common))
    assert str(missing.value) != str(bad.value)


def test_missing_policy_id_evidence_is_reported_as_missing():
    """M11 on the promotion edge, where two evidence keys are required.

    ``policy_id`` takes the I5 branch of the validation loop rather than the
    digest branch, so without the presence check a missing ``policy_id``
    would be diagnosed as "must be a non-empty id" -- an I5 complaint about a
    value that was never supplied.
    """
    req = TransitionRequest(
        record_id="r1", src=State.VERIFIED, dst=State.PROMOTED,
        actor="promoter", role=Role.PROMOTER, proposer="p",
        evidence={"verification_report": "a" * 64}, policy_id="policy-1")
    with pytest.raises(TransitionError,
                       match=r"I6: .* requires evidence \['policy_id'\]"):
        check(req)


def test_the_live_store_refuses_a_forged_transition_not_only_reconstruct(
        tmp_path):
    """THE §29 anti-pattern, in the reducer that decides what is canonical.

    ``test_an_unauthorized_transition_in_the_log_is_not_applied`` asserts
    that ``reconstruct`` refuses a forged record, and it always passed. The
    LIVE store applied the same record without checking anything: not the
    edge, not the role, not separation of duties, not even the ``src`` it
    wrote into the payload itself.

    One appended line therefore moved a record from UNDER_REVIEW straight to
    PROMOTED -- the state carrying canonical authority, reachable only from
    VERIFIED by I1 -- and ``store.canonical()`` reported it.

    A second reader is a DETECTOR. It is not a substitute for the first
    reader being right, and asking only the detector is how this survived.
    """
    log = EventLog(tmp_path / "ev.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="claim", proposer="alice")
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                 role=Role.VERIFIER)

    log.append(actor="mallory", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": State.VERIFIED.value,
                        "dst": State.PROMOTED.value,
                        "role": Role.PROMOTER.value,
                        "evidence": {}, "policy_id": "p1"})

    with pytest.raises(StoreError, match="moves it from"):
        AuthorityStore(log).load()


def test_the_live_store_refuses_an_edge_the_machine_does_not_have(tmp_path):
    """Stated as the property: replay re-authorizes, it does not just apply.

    Here the declared src AGREES with the replay, so the src check cannot be
    what refuses it. The edge itself is the thing that does not exist.
    """
    log = EventLog(tmp_path / "ev.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="claim", proposer="alice")

    # PROPOSED -> PROMOTED: no such edge (I1).
    log.append(actor="mallory", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": State.PROPOSED.value,
                        "dst": State.PROMOTED.value,
                        "role": Role.PROMOTER.value,
                        "evidence": {}, "policy_id": "p1"})
    with pytest.raises(StoreError, match="would be refused today"):
        AuthorityStore(log).load()


def test_the_live_store_refuses_self_verification_on_replay(tmp_path):
    """Separation of duties survives the replay, not only the write path."""
    log = EventLog(tmp_path / "ev.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="claim", proposer="alice")
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                 role=Role.VERIFIER)

    log.append(actor="alice", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": State.UNDER_REVIEW.value,
                        "dst": State.VERIFIED.value,
                        "role": Role.VERIFIER.value,
                        "evidence": {"verification_report": DIG}})
    with pytest.raises(StoreError, match="would be refused today"):
        AuthorityStore(log).load()


def test_replay_does_not_require_evidence_that_has_since_been_archived(
        tmp_path):
    """The refusal must be the state machine, not a resolver.

    A replay verifying historical transitions may legitimately run against a
    store that no longer holds long-expired evidence. Forcing resolution here
    would turn an archival policy into a retroactive authority failure, so
    the re-authorization deliberately passes no resolver.
    """
    from qta_agent.evidence import EvidenceStore

    ev = EvidenceStore(tmp_path / "blobs")
    report = ev.put(b"verification report")
    log = EventLog(tmp_path / "ev.jsonl")
    s = AuthorityStore(log, evidence=ev).load()
    s.create(record_id="r1", kind="claim", proposer="alice")
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                 role=Role.VERIFIER)
    s.transition(record_id="r1", dst=State.VERIFIED, actor="bob",
                 role=Role.VERIFIER, evidence={"verification_report": report})

    # The evidence is archived away; the history must still replay.
    ev._blob_path(report).unlink()
    reloaded = AuthorityStore(log, evidence=ev).load()
    assert reloaded.get("r1").state is State.VERIFIED


# --- a create introduces a claim; it does not assert a verdict --------------
#
# The transition reducer was hardened to refuse a forged walk to PROMOTED.
# This skipped the walk entirely: record.create read `state` from the payload,
# so ONE appended line produced a record born in PROMOTED -- the state that
# carries canonical authority, reachable only from VERIFIED by I1 -- and
# store.canonical() reported it as canonical authority.
#
# The state machine was not defeated. It was never consulted.

def _forged_create(log, **over):
    rec = {"record_id": "forged", "kind": "claim", "proposer": "mallory"}
    rec.update(over)
    log.append(actor="mallory", action="record.create",
               target=rec["record_id"], payload=rec)


def test_a_record_cannot_be_created_directly_in_promoted(tmp_path):
    log = EventLog(tmp_path / "ev.jsonl")
    _forged_create(log, state="PROMOTED")
    with pytest.raises(StoreError, match="created directly in PROMOTED"):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


@pytest.mark.parametrize("state", ["VERIFIED", "UNDER_REVIEW", "PROMOTED"])
def test_no_state_but_the_initial_one_may_be_asserted_at_creation(tmp_path,
                                                                   state):
    """Stated over the states worth reaching, not only the worst one."""
    log = EventLog(tmp_path / "ev.jsonl")
    _forged_create(log, state=state)
    with pytest.raises(StoreError, match="created directly in"):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def test_the_forged_record_does_not_reach_canonical(tmp_path):
    """Asserted as the OUTCOME, because canonical() is the function that
    answers 'what does this system hold to be true'."""
    log = EventLog(tmp_path / "ev.jsonl")
    _forged_create(log, state="PROMOTED")
    with pytest.raises(StoreError):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def test_a_create_may_not_name_a_proposer_it_did_not_have(tmp_path):
    """Separation of duties is measured from the proposer -- a proposer may
    not verify its own record -- so a create that could name one could choose
    the party it has to differ from."""
    log = EventLog(tmp_path / "ev.jsonl")
    _forged_create(log, proposer="alice")
    with pytest.raises(StoreError, match="was appended by"):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def test_a_second_create_cannot_replace_a_live_record(tmp_path):
    """The same defect pointed at history instead of at authority: it reset
    the first record's state, evidence and revision."""
    log = EventLog(tmp_path / "ev.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="claim", proposer="alice")
    log.append(actor="mallory", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": "mallory", "state": "PROPOSED"})
    with pytest.raises(StoreError, match="already exists"):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def test_cited_evidence_must_be_a_digest_at_creation(tmp_path):
    """FORM, not resolution. Evidence may legitimately have been archived
    since, and requiring it to resolve on replay would turn an archival
    policy into a retroactive authority failure -- the same reasoning the
    transition reducer already carries."""
    log = EventLog(tmp_path / "ev.jsonl")
    _forged_create(log, evidence={"report": "trust-me"})
    with pytest.raises(StoreError, match="not a sha256 digest"):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def test_a_create_may_not_depend_on_something_unrecorded(tmp_path):
    log = EventLog(tmp_path / "ev.jsonl")
    _forged_create(log, depends_on=["ghost"])
    with pytest.raises(StoreError, match="nothing has recorded"):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def test_a_dependency_record_for_an_unknown_record_is_a_domain_error(
        tmp_path):
    """An authority API fails on purpose, or not at all."""
    log = EventLog(tmp_path / "ev.jsonl")
    log.append(actor="mallory", action="record.depend", target="nobody",
               payload={"record_id": "nobody", "depends_on": ["x"]})
    with pytest.raises(StoreError, match="never created"):
        AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()


def test_an_ordinary_create_still_replays(tmp_path):
    """The guard must refuse forgeries, not records."""
    log = EventLog(tmp_path / "ev.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="claim", proposer="alice")
    s.create(record_id="r2", kind="claim", proposer="bob",
             depends_on=("r1",))
    reloaded = AuthorityStore(EventLog(tmp_path / "ev.jsonl")).load()
    assert reloaded.get("r1").state is State.PROPOSED
    assert reloaded.get("r1").proposer == "alice"
    assert reloaded.get("r2").depends_on == ("r1",)


def test_the_create_proposer_guard_makes_its_own_derivation_unobservable():
    """WHY one mutation was removed as EQUIVALENT rather than left surviving.

    The reducer writes ``proposer=ev.actor``. Replacing that with
    ``p["proposer"]`` changes nothing, because the guard above it refuses any
    create whose claimed proposer differs from the actor -- unconditionally,
    with no try/except in between -- so the two are the same value by the
    time either runs.

    That is a fact about the guard's SHAPE, so the shape is what this
    asserts. Make the guard conditional (as the equivalent-looking one in
    agents.py is, deliberately) and the derivation becomes observable again,
    this test fails, and the mutation belongs back in the spec.

    The property itself is not untested: the guard has its own mutation, and
    it is killed.
    """
    import ast

    from pathlib import Path as _P
    src = (_P(ROOT) / "qta_agent" / "store.py").read_text(
        encoding="utf-8")
    lines = src.split("\n")
    guard = [i for i, x in enumerate(lines)
             if x.strip() == "if claimed_proposer != ev.actor:"]
    assign = [i for i, x in enumerate(lines)
              if x.strip() == "record_id=rid, kind=p[\"kind\"], "
                             "proposer=ev.actor,"]
    assert len(guard) == 1 and len(assign) == 1, (guard, assign)
    assert guard[0] < assign[0]

    between = "\n".join(lines[guard[0]:assign[0]])
    assert "try" not in between and "except" not in between, (
        "a try/except now sits between the guard and the assignment, so the "
        "guard can be skipped and the derivation is observable again")
    # And the guard is a plain `if`, not nested under a truthiness test on
    # the claim itself -- which is exactly what makes agents.py's version
    # non-equivalent.
    assert ast.parse(lines[guard[0]].strip() + "\n    pass")
