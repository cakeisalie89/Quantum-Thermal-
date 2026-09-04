"""Property and stateful tests for the authority substrate.

Example-based tests prove a rule holds for the cases someone thought of.
These prove it holds for cases nobody thought of, which is the failure mode
that matters for a component that decides what becomes canonical.

The stateful test is the centrepiece: Hypothesis drives the real store through
arbitrary interleavings of create / transition / depend / invalidate and
checks the invariants after every single step. A violation is reported as the
shortest sequence that produces it.
"""
from __future__ import annotations

import os
import sys

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle, RuleBasedStateMachine, initialize, invariant, precondition, rule,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qta_agent.authority import (  # noqa: E402
    EDGES, INITIAL, TERMINAL, Role, State, TransitionError, TransitionRequest,
    allowed_targets, check,
)
from qta_agent.canonical import (  # noqa: E402
    CanonicalizationError, digest, is_digest,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.invalidation import apply_invalidation, plan_invalidation  # noqa: E402
from qta_agent.reconstruct import compare, reconstruct  # noqa: E402
from qta_agent.store import AuthorityStore, StoreError  # noqa: E402

DIG = "b" * 64

#: JSON-representable values, excluding the float hazards canonicalization
#: refuses. Nested so key ordering actually gets exercised.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-10_000, 10_000)
    | st.text(max_size=24),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
    max_leaves=12,
)


# ---------------------------------------------------------------------------
# canonical serialization
# ---------------------------------------------------------------------------

@given(json_values)
@settings(max_examples=200, deadline=None)
def test_digest_is_deterministic_for_any_json_value(value):
    assert digest(value) == digest(value)


@given(st.dictionaries(st.text(min_size=1, max_size=6), json_values,
                       max_size=6))
@settings(max_examples=200, deadline=None)
def test_digest_ignores_key_insertion_order(mapping):
    shuffled = dict(reversed(list(mapping.items())))
    assert digest(mapping) == digest(shuffled)


@given(json_values)
@settings(max_examples=200, deadline=None)
def test_every_digest_is_syntactically_valid(value):
    assert is_digest(digest(value))


@given(st.floats(allow_nan=True, allow_infinity=True))
@settings(max_examples=100, deadline=None)
def test_only_finite_floats_are_serializable(f):
    if f != f or f in (float("inf"), float("-inf")):
        with pytest.raises(CanonicalizationError):
            digest({"x": f})
    else:
        assert is_digest(digest({"x": f}))


# ---------------------------------------------------------------------------
# the transition table, over arbitrary inputs
# ---------------------------------------------------------------------------

@given(st.sampled_from(list(State)), st.sampled_from(list(State)),
       st.sampled_from(list(Role)))
@settings(max_examples=400, deadline=None)
def test_no_role_can_ever_leave_a_terminal_state(src, dst, role):
    assume(src in TERMINAL)
    with pytest.raises(TransitionError):
        check(TransitionRequest("r", src, dst, "actor", role,
                                {"verification_report": DIG,
                                 "policy_id": "p", "revocation_reason": DIG,
                                 "superseded_by": DIG,
                                 "invalidated_by": DIG,
                                 "rejection_reason": DIG},
                                proposer="other", policy_id="p"))


@given(st.sampled_from(list(State)), st.sampled_from(list(Role)))
@settings(max_examples=400, deadline=None)
def test_promotion_is_unreachable_from_anything_but_verified(src, role):
    """I1, over every (source, role) pair rather than the ones we imagined."""
    assume(src is not State.VERIFIED)
    with pytest.raises(TransitionError):
        check(TransitionRequest("r", src, State.PROMOTED, "carol", role,
                                {"verification_report": DIG,
                                 "policy_id": "p"},
                                proposer="alice", policy_id="p"))


@given(st.text(min_size=1, max_size=12))
@settings(max_examples=200, deadline=None)
def test_self_verification_is_refused_for_any_identity(actor):
    """I4 must not depend on the particular names used in examples."""
    with pytest.raises(TransitionError, match="I4"):
        check(TransitionRequest("r", State.UNDER_REVIEW, State.VERIFIED,
                                actor, Role.VERIFIER,
                                {"verification_report": DIG}, proposer=actor))


@given(st.text(max_size=70))
@settings(max_examples=300, deadline=None)
def test_only_real_digests_satisfy_an_evidence_requirement(token):
    """I6 over arbitrary strings, not just the obviously-wrong ones."""
    req = TransitionRequest("r", State.UNDER_REVIEW, State.VERIFIED, "bob",
                            Role.VERIFIER, {"verification_report": token},
                            proposer="alice")
    if is_digest(token):
        check(req)
    else:
        with pytest.raises(TransitionError, match="I6"):
            check(req)


# ---------------------------------------------------------------------------
# stateful: drive the real store through arbitrary histories
# ---------------------------------------------------------------------------

class AuthorityMachine(RuleBasedStateMachine):
    """Arbitrary interleavings; invariants checked after every step."""

    ids = Bundle("ids")

    def __init__(self):
        super().__init__()
        import tempfile
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp())
        self.store = AuthorityStore(EventLog(self.dir / "ev.jsonl")).load()
        self.counter = 0

    @initialize()
    def _seed(self):
        pass

    @rule(target=ids)
    def create_root(self):
        self.counter += 1
        rid = f"r{self.counter}"
        self.store.create(record_id=rid, kind="result", proposer="alice",
                          policy_id="p1")
        return rid

    @rule(target=ids, parent=ids)
    def create_child(self, parent):
        self.counter += 1
        rid = f"r{self.counter}"
        self.store.create(record_id=rid, kind="result", proposer="alice",
                          policy_id="p1", depends_on=(parent,))
        return rid

    @rule(rid=ids)
    def review(self, rid):
        if self.store.get(rid).state in (State.PROPOSED, State.STALE):
            self.store.transition(record_id=rid, dst=State.UNDER_REVIEW,
                                  actor="bob", role=Role.VERIFIER)

    @rule(rid=ids)
    def verify_record(self, rid):
        if self.store.get(rid).state is State.UNDER_REVIEW:
            self.store.transition(record_id=rid, dst=State.VERIFIED,
                                  actor="bob", role=Role.VERIFIER,
                                  evidence={"verification_report": DIG})

    @rule(rid=ids)
    def promote(self, rid):
        if self.store.get(rid).state is State.VERIFIED:
            self.store.transition(record_id=rid, dst=State.PROMOTED,
                                  actor="carol", role=Role.PROMOTER,
                                  evidence={"policy_id": "p1"},
                                  policy_id="p1")

    @rule(rid=ids)
    def revoke(self, rid):
        st_ = self.store.get(rid).state
        if st_ in (State.PROMOTED, State.VERIFIED, State.STALE,
                   State.SUPERSEDED):
            self.store.transition(record_id=rid, dst=State.REVOKED,
                                  actor="carol", role=Role.PROMOTER,
                                  evidence={"revocation_reason": DIG})

    @rule(rid=ids)
    def invalidate(self, rid):
        apply_invalidation(self.store, rid, reason="property test")

    # ---- invariants, checked after every rule ------------------------
    @invariant()
    def promoted_records_were_verified_first(self):
        """I1 as a history property, not merely an edge property."""
        recon = reconstruct(self.store.log)
        for rid, rec in recon.records.items():
            states = [s for _, s in rec["history"]]
            if State.PROMOTED.value in states:
                i = states.index(State.PROMOTED.value)
                assert State.VERIFIED.value in states[:i], (
                    f"{rid} reached PROMOTED without prior VERIFIED: {states}")

    @invariant()
    def terminal_states_are_never_left(self):
        recon = reconstruct(self.store.log)
        for rid, rec in recon.records.items():
            states = [s for _, s in rec["history"]]
            for term in (State.REVOKED.value, State.REJECTED.value):
                if term in states:
                    assert states.index(term) == len(states) - 1, (
                        f"{rid} left terminal {term}: {states}")

    @invariant()
    def no_canonical_record_depends_on_a_stale_one(self):
        """The whole point of transitive invalidation."""
        recs = self.store.all_records()
        dead = {State.STALE, State.REVOKED, State.REJECTED}
        for rid, rec in recs.items():
            if rec.state is not State.PROMOTED:
                continue
            for dep in rec.depends_on:
                assert recs[dep].state not in dead, (
                    f"canonical {rid} depends on {dep} in {recs[dep].state}")

    @invariant()
    def reconstruction_always_agrees(self):
        assert compare(self.store, reconstruct(self.store.log)) == ()

    @invariant()
    def the_log_always_verifies(self):
        assert self.store.log.verify().ok


TestAuthorityMachine = AuthorityMachine.TestCase
TestAuthorityMachine.settings = settings(
    max_examples=40, stateful_step_count=30, deadline=None,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much],
)
