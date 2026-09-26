"""A scientific result is VERIFIED only on evidence that supports it -- at the store.

Until this, the rule lived in ``governed_model.decide``, and a caller writing
the transition to the store directly got past it: the edge checked that the
cited report EXISTED, and a FAIL report exists. Now the store reads what the
record cites and refuses the edge. Every refusal is exercised here against
documents the ``scientific`` package actually writes, with a control that the
genuine pair passes and that other record kinds are untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import result_rules  # noqa: E402
from qta_agent.authority import Role, State, TransitionError  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402


@pytest.fixture(scope="module")
def pair():
    """A real ResultBundle and the real independent check of it."""
    from scientific.checks.reduction_2d import run_check
    from scientific.model import run_model
    from scientific.models.thermal_1d import Thermal1DModel
    bundle = run_model(Thermal1DModel(), {"n_cells": 60, "n_eval": 20})
    report = run_check(bundle, verifier_id="checker")
    return bundle.to_record(), report.to_record()


def _put(ev, doc) -> str:
    return ev.put(json.dumps(doc, sort_keys=True).encode())


@pytest.fixture()
def world(tmp_path, pair):
    ev = EvidenceStore(tmp_path / "evidence")
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"), evidence=ev)
    bundle, report = pair
    return store, ev, bundle, report


def _under_review(store, ev, bundle, rid="r1", kind=result_rules.KIND):
    store.create(record_id=rid, kind=kind, proposer="proposer",
                 evidence={"result_bundle": _put(ev, bundle)})
    store.transition(record_id=rid, dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    return rid


def _verify(store, rid, report_sha):
    return store.transition(record_id=rid, dst=State.VERIFIED,
                            actor="reviewer", role=Role.VERIFIER,
                            evidence={"verification_report": report_sha})


def test_the_genuine_pair_satisfies_the_rule(pair):
    """Held to what the scientific package writes, not to a fixture."""
    assert result_rules.verification_problems(*pair) == []


def test_a_supported_result_is_verified(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle)
    assert _verify(store, rid, _put(ev, report)).state is State.VERIFIED


@pytest.mark.parametrize("change,why", [
    ({"status": "FAIL"}, "reported FAIL"),
    ({"status": "NOT_RUN"}, "reported NOT_RUN"),
    ({"subject_digest": "c" * 64}, "different bundle"),
    ({"check_type": "INVARIANT"}, "not an independent check"),
    ({"producer_implementation_digest": "d" * 64}, "another producer"),
    ({"verifier_implementation_digest": "PRODUCER"}, "producer's own code"),
    ({"verifier_implementation_digest": "not-a-digest"},
     "producer's own code"),
])
def test_the_store_refuses_a_report_that_does_not_support_it(world, change,
                                                             why):
    """THE bypass that was open: a direct transition citing a report that
    exists and does not support the result."""
    store, ev, bundle, report = world
    if change.get("verifier_implementation_digest") == "PRODUCER":
        change = {"verifier_implementation_digest":
                  bundle["implementation_digest"]}
    rid = _under_review(store, ev, bundle)
    with pytest.raises(TransitionError, match=why):
        _verify(store, rid, _put(ev, {**report, **change}))
    assert store.get(rid).state is State.UNDER_REVIEW


def test_a_bundle_whose_invariant_failed_is_not_verified(world):
    store, ev, bundle, report = world
    from qta_agent.canonical import digest
    broken = json.loads(json.dumps(bundle))
    broken["invariants"][0]["holds"] = False
    rid = _under_review(store, ev, broken)
    with pytest.raises(TransitionError, match="invariants not holding"):
        _verify(store, rid, _put(ev, {**report,
                                      "subject_digest": digest(broken)}))


def test_a_store_without_evidence_cannot_verify_a_result(tmp_path, pair):
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"))
    store.create(record_id="r", kind=result_rules.KIND, proposer="p",
                 evidence={"result_bundle": "a" * 64})
    store.transition(record_id="r", dst=State.UNDER_REVIEW, actor="v",
                     role=Role.VERIFIER)
    with pytest.raises(TransitionError, match="no evidence attached"):
        store.transition(record_id="r", dst=State.VERIFIED, actor="v",
                         role=Role.VERIFIER,
                         evidence={"verification_report": "b" * 64})


def test_evidence_that_is_not_json_is_a_problem_not_a_pass(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle)
    with pytest.raises(TransitionError, match="cannot be read"):
        _verify(store, rid, ev.put(b"\x00 not json"))


def test_other_record_kinds_are_untouched(world):
    """Control: the rule is scoped to scientific results."""
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle, kind="stage10_artifact")
    fail = _put(ev, {**report, "status": "FAIL"})
    assert _verify(store, rid, fail).state is State.VERIFIED


def test_promotion_is_held_to_the_same_rule(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle)
    good = _put(ev, report)
    _verify(store, rid, good)
    with pytest.raises(TransitionError, match="reported FAIL"):
        store.transition(record_id=rid, dst=State.PROMOTED, actor="promoter",
                         role=Role.PROMOTER, policy_id="p",
                         evidence={"verification_report":
                                   _put(ev, {**report, "status": "FAIL"}),
                                   "policy_id": "p"})
    rec = store.transition(record_id=rid, dst=State.PROMOTED,
                           actor="promoter", role=Role.PROMOTER,
                           policy_id="p",
                           evidence={"verification_report": good,
                                     "policy_id": "p"})
    assert rec.state is State.PROMOTED


def test_a_bundle_with_no_invariants_is_not_verified(world):
    """An empty invariant list is not 'every invariant holds'."""
    store, ev, bundle, report = world
    from qta_agent.canonical import digest
    bare = {**json.loads(json.dumps(bundle)), "invariants": []}
    rid = _under_review(store, ev, bare)
    with pytest.raises(TransitionError, match="none run"):
        _verify(store, rid, _put(ev, {**report,
                                      "subject_digest": digest(bare)}))


def test_a_record_citing_no_bundle_is_not_verified(world):
    store, ev, bundle, report = world
    store.create(record_id="nobundle", kind=result_rules.KIND,
                 proposer="proposer")
    store.transition(record_id="nobundle", dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    with pytest.raises(TransitionError, match="cites no result_bundle"):
        _verify(store, "nobundle", _put(ev, report))
