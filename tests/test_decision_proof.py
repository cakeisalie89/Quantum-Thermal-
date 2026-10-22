"""Proof-carrying decisions (Assurance D).

A decision's justification is derived from the verified history as it stood
at the decision: the proposal it answered and the model call and budget
behind it, the context and the policy in force, who proposed and who
decided, the evidence it cites and whether those bytes are held, the state
of what it depends on, and what it changed. An independent verifier -- in a
process that cannot import the primary reducers -- recalculates all of it
from the log on disk and compares; it never uses the proof's own claims.

The decisions here are real: the Gate C objective run end to end, whose
reviewer's VERIFIED decision and whose ledger's SUPPORTED verdict are each
proved, and then attacked.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_research_loop as RL  # noqa: E402

from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.policy import PolicyStore  # noqa: E402
from agent_substrate.reconstruct import (  # noqa: E402
    PROOF_SCHEMA, derive_decision_proof, verify_decision_proof,
)
from agent_substrate.separate_verify import (  # noqa: E402
    verify_in_separate_process,
)
from agent_substrate.store import AuthorityStore  # noqa: E402


class Decided:
    """A finished Gate C history, and its two critical decisions."""

    def __init__(self, w):
        self.w = w
        self.evidence = w.base / "evidence"
        evs = w.events()
        self.verified = next(e for e in evs if e.action == "record.transition"
                             and e.payload.get("dst") == "VERIFIED")
        self.verdict = next(e for e in evs if e.action == "research.status"
                            and e.payload.get("status") == "SUPPORTED")

    def proof(self, ev):
        return derive_decision_proof(self.w.log, ev.seq,
                                     evidence=EvidenceStore(self.evidence))

    def verify(self, proof):
        return verify_decision_proof(self.w.log, proof,
                                     evidence=EvidenceStore(self.evidence))


@pytest.fixture(scope="module")
def decided():
    w = RL.World("decision-proof", L.scripted_agent(
        hypothesis=RL.hypothesis(), run=RL.SLAB["request"]))
    assert w.controller().run("slab-q").status == "CONCLUDED"
    yield Decided(w)
    w.clean()


# ------------------------------------------------------- honest decisions

def test_a_reviewer_s_decision_carries_its_whole_justification(decided):
    p = decided.proof(decided.verified)
    assert p["schema"] == PROOF_SCHEMA and p["accepted"] is True
    assert p["decision"]["outcome"] == "VERIFIED"
    assert p["separation_of_duties"]["distinct"] is True
    assert p["record"]["admission"] == "ADMITTED"
    assert set(p["evidence_held"]) == {"result_bundle", "run_identity",
                                       "verification_report"}
    assert all(p["evidence_held"].values())
    run = p["execution"]
    assert run["executor"] and run["task_verifier"] and run["capabilities"]
    chain = run["proposal"]
    assert chain["kind"] == "MODEL_RUN" and chain["agent_id"] == RL.AGENT
    assert chain["bound_by"] == RL.AGENT
    assert chain["inference"]["granted"] and \
        chain["inference"]["outcome"] == "OK"
    assert chain["context"]["policy_identity"] == "research.default@1"
    assert chain["context"]["policy_in_force"] is True
    assert decided.verify(p) == []


def test_a_verdict_carries_its_basis_and_the_proposal_behind_it(decided):
    p = decided.proof(decided.verdict)
    assert p["accepted"] and p["decision"]["outcome"] == "SUPPORTED"
    [b] = p["basis"]
    assert b["record_state"] == "VERIFIED" and b["bundle_held"]
    assert p["proposal"]["kind"] == "HYPOTHESIS"
    assert decided.verify(p) == []


def test_the_proof_agrees_with_the_primary_projections(decided):
    """The proof is the SECOND reader's account; the primary readers must
    say the same, or one of the two is wrong."""
    w = decided.w
    p = decided.proof(decided.verified)
    rec = AuthorityStore(w.log).load().get(p["record"]["record_id"])
    assert (rec.state.value, rec.proposer, rec.policy_id) == (
        "VERIFIED", p["record"]["proposer"], p["record"]["policy_id"])
    assert dict(rec.evidence) == p["record"]["evidence"]
    store = EvidenceStore(decided.evidence)
    assert all(store.contains(d) for d in rec.evidence.values())
    chain = p["execution"]["proposal"]
    got = PR.received(w.log)[chain["proposal_id"]]
    assert (got["agent_id"], got["kind"], got["digest"]) == (
        chain["agent_id"], chain["kind"], chain["envelope_digest"])
    gw = w._gateway()
    out = gw.outcome(chain["inference"]["request_id"])
    assert out.ok and out.attempt == chain["inference"]["attempt"]
    assert PolicyStore(w.log).load().in_force(
        "research.default").identity == chain["context"]["policy_identity"]
    verdict = decided.proof(decided.verdict)
    led = R.ResearchLedger(w.log).load()
    assert led.get("h1").status == verdict["decision"]["outcome"]
    assert list(led.get("h1").basis) == [
        b["observation_id"] for b in verdict["basis"]]


def test_an_honest_proof_verifies_in_a_process_that_cannot_cheat(decided):
    for ev in (decided.verified, decided.verdict):
        got = verify_in_separate_process(
            decided.w.base / "log.jsonl", proof=decided.proof(ev),
            evidence_root=decided.evidence)
        assert got.ok, (got.reason, got.findings)


# ------------------------------------------------------- forged proofs

def _set(d, path, value):
    *head, last = path.split(".")
    for k in head:
        d = d[int(k)] if isinstance(d, list) else d[k]
    if value is _DROP:
        del d[last]
    else:
        d[last] = value


_DROP = object()

FORGERIES = [
    # (decision, field, forged value, what the verifier must name)
    ("verified", "decision.actor", "the-owner", "decision.actor"),
    ("verified", "decision.outcome", "PROMOTED", "decision.outcome"),
    ("verified", "head.hash", "0" * 64, "head.hash"),
    ("verified", "record.evidence.verification_report", "f" * 64,
     "record.evidence.verification_report"),
    ("verified", "separation_of_duties.decider", "ai-researcher",
     "separation_of_duties.decider"),
    ("verified", "execution.proposal", None, "execution.proposal"),
    ("verified", "execution.proposal.context.policy_identity",
     "research.default@0", "policy_identity"),
    ("verified", "execution.proposal.inference.budget_id", "unlimited",
     "budget_id"),
    ("verified", "execution.capabilities", ["cap-root"],
     "execution.capabilities"),
    ("verified", "record.depends_on", {"rec-x": "VERIFIED"},
     "record.depends_on"),
    ("verified", "evidence_held", _DROP, "evidence_held: missing"),
    ("verified", "reviewer_approval", "signed by the owner",
     "reviewer_approval: in the proof, and nowhere in the history"),
    ("verdict", "basis.0.record_state", "PROMOTED", "record_state"),
    ("verdict", "basis.0.values.T_at_half.value", 1.0, "values"),
    ("verdict", "hypothesis.prediction.value", 10.0, "prediction"),
    ("verdict", "proposal.agent_id", "the-owner", "agent_id"),
]


@pytest.mark.parametrize("which, path, value, named", FORGERIES,
                         ids=[f"{f[0]}:{f[1]}" for f in FORGERIES])
def test_a_forged_proof_does_not_pass(decided, which, path, value, named):
    proof = copy.deepcopy(decided.proof(getattr(decided, which)))
    _set(proof, path, value)
    found = decided.verify(proof)
    assert found and any(named in f for f in found), found


def test_a_forged_proof_does_not_pass_in_the_separate_process(decided):
    proof = copy.deepcopy(decided.proof(decided.verified))
    proof["separation_of_duties"]["decider"] = "ai-researcher"
    got = verify_in_separate_process(
        decided.w.base / "log.jsonl", proof=proof,
        evidence_root=decided.evidence)
    assert not got.ok and got.exit_status == 1
    assert any("decider" in f for f in got.findings)


@pytest.mark.parametrize("proof, why", [
    ({}, "not a decision-proof/1"),
    ({"schema": PROOF_SCHEMA, "decision": {"seq": True}}, "no decision"),
    ({"schema": PROOF_SCHEMA, "decision": {"seq": 10**9}}, "no record"),
])
def test_a_proof_of_nothing_is_refused(decided, proof, why):
    assert any(why in f for f in decided.verify(proof))


def test_a_proof_of_an_event_that_decides_nothing_is_refused(decided):
    ev = next(e for e in decided.w.events()
              if e.action == "inference.dispatch")
    found = decided.verify({"schema": PROOF_SCHEMA,
                            "decision": {"seq": ev.seq}})
    assert any("not a decision" in f for f in found)


# ------------------------------------- decisions whose HONEST proof fails

def _copy_history(decided, tmp_path):
    shutil.copytree(decided.w.base / "evidence", tmp_path / "evidence")
    shutil.copy(decided.w.base / "log.jsonl", tmp_path / "log.jsonl")
    return EventLog(tmp_path / "log.jsonl"), tmp_path / "evidence"


def test_a_decision_whose_evidence_is_gone_fails_its_conditions(decided,
                                                                 tmp_path):
    log, evidence = _copy_history(decided, tmp_path)
    report = decided.verified.payload["evidence"]["verification_report"]
    (evidence / report[:2] / report[2:]).unlink()
    proof = derive_decision_proof(log, decided.verified.seq,
                                  evidence=EvidenceStore(evidence))
    found = verify_decision_proof(log, proof,
                                  evidence=EvidenceStore(evidence))
    assert any("not held" in f for f in found), found


def test_a_decision_the_gates_refuse_cannot_be_proved_sound(decided,
                                                            tmp_path):
    """A transition appended around the store -- the proposer verifying its
    own claim -- is recorded, and its honest proof says so."""
    log, evidence = _copy_history(decided, tmp_path)
    store = AuthorityStore(log).load()
    store.create(record_id="claim-x", kind="claim", proposer="mallory")
    store.transition(record_id="claim-x", dst=State.UNDER_REVIEW,
                     actor="bob", role=Role.VERIFIER)
    forged = log.append(actor="mallory", action="record.transition",
                        target="claim-x", payload={
                            "record_id": "claim-x", "src": "UNDER_REVIEW",
                            "dst": "VERIFIED", "role": "VERIFIER",
                            "evidence": {"verification_report": "a" * 64},
                            "policy_id": None})
    proof = derive_decision_proof(log, forged.seq,
                                  evidence=EvidenceStore(evidence))
    assert proof["accepted"] is False
    found = verify_decision_proof(log, proof,
                                  evidence=EvidenceStore(evidence))
    assert any("not one this reader accepted" in f for f in found)
    assert any("proposer" in f for f in found)


def test_a_proof_is_json_and_survives_transport(decided):
    p = decided.proof(decided.verified)
    assert decided.verify(json.loads(json.dumps(p))) == []



# ----------------------------------------- the rules, one at a time

class _LyingStore:
    """Returns bytes for every digest -- the wrong ones."""

    def get(self, digest):
        return b"not what was cited"


def test_the_reader_rehashes_what_a_store_returns(decided):
    p = derive_decision_proof(decided.w.log, decided.verified.seq,
                              evidence=_LyingStore())
    assert not any(p["evidence_held"].values())


def _ev(seq, action, wall_time=0.0, **payload):
    from types import SimpleNamespace
    return SimpleNamespace(seq=seq, action=action, payload=payload,
                           wall_time=wall_time)


def test_a_policy_is_in_force_only_as_published_and_not_withdrawn():
    from agent_substrate.reconstruct import _policy_in_force
    pub = [_ev(1, "policy.publish", document={"policy_id": "p",
                                              "version": 1}),
           _ev(2, "policy.publish", document={"policy_id": "p",
                                              "version": 2}),
           _ev(3, "policy.withdraw", policy_id="p")]
    assert _policy_in_force(pub, "p@1", at_seq=2)
    assert not _policy_in_force(pub, "p@1", at_seq=3)      # superseded
    assert _policy_in_force(pub, "p@2", at_seq=3)
    assert not _policy_in_force(pub, "p@2", at_seq=4)      # withdrawn
    assert not _policy_in_force(pub, "q@1", at_seq=4)
    assert not _policy_in_force(pub, "not-an-identity", at_seq=4)
    # expired by the time the history reached, whatever the record's own
    # clock said (D-2026-168)
    exp = [_ev(1, "policy.publish", wall_time=10.0,
               document={"policy_id": "p", "version": 1, "not_after": 20.0}),
           _ev(2, "policy.decision", wall_time=15.0),
           _ev(3, "policy.decision", wall_time=30.0),
           _ev(4, "policy.decision", wall_time=15.0)]
    assert _policy_in_force(exp, "p@1", at_seq=2)
    assert not _policy_in_force(exp, "p@1", at_seq=3)
    assert not _policy_in_force(exp, "p@1", at_seq=4)


def _record_derived(**over):
    d = {"decision": {"kind": "record", "outcome": "VERIFIED"},
         "accepted": True,
         "separation_of_duties": {"distinct": True},
         "evidence_held": {"verification_report": True},
         "record": {"kind": "scientific_result", "admission": "ADMITTED",
                    "depends_on": {"r0": "VERIFIED"}},
         "execution": {"proposal": {
             "inference": {"granted": True, "outcome": "OK"},
             "context": {"policy_in_force": True}}}}
    for path, value in over.items():
        _set(d, path, value)
    return d


@pytest.mark.parametrize("path, value, why", [
    ("accepted", False, "not one this reader accepted"),
    ("separation_of_duties.distinct", False, "proposer"),
    ("evidence_held.verification_report", False, "not held"),
    ("record.admission", "UNVERIFIABLE", "not ADMITTED"),
    ("record.depends_on.r0", "STALE", "dependency"),
    ("execution.proposal.inference.granted", False, "granted"),
    ("execution.proposal.inference.outcome", "TIMEOUT", "granted"),
    ("execution.proposal.context.policy_in_force", False, "not in force"),
])
def test_each_condition_of_a_record_decision_is_checked(path, value, why):
    from agent_substrate.reconstruct import _conditions
    assert _conditions(_record_derived()) == []
    assert any(why in f for f in _conditions(_record_derived(
        **{path: value})))


@pytest.mark.parametrize("basis, why", [
    ([], "no basis"),
    ([{"observation_id": "o", "record_state": "STALE",
       "bundle_held": True}], "rests on a record STALE"),
    ([{"observation_id": "o", "record_state": "VERIFIED",
       "bundle_held": False}], "bundle is not held"),
])
def test_each_condition_of_a_verdict_is_checked(basis, why):
    from agent_substrate.reconstruct import _conditions
    d = {"decision": {"kind": "research", "outcome": "SUPPORTED"},
         "accepted": True, "basis": basis, "proposal": None}
    assert any(why in f for f in _conditions(d))
