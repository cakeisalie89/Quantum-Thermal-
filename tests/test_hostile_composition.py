"""Hostile Cases A-H: each crosses a boundary, and each is refused.

The composition every case attacks:

    an AI PROPOSES -> governed computation RUNS it -> an independent check
    JUDGES it -> a separate authority DECIDES -> an independent reader can
    RECONSTRUCT the decision.

A case is refused when it would let one of those stand in for another.
Each case drives the real components -- the governed path, the authority
store, the second reader, the release verifier -- and each has a control
that differs only in what makes the case hostile. Deeper, per-rule suites
are named in each case; this file proves the cases compose end to end.

    A  a correctly signed (integrity-valid) artifact with an incorrect result
    B  altered or substituted verification evidence
    C  an AI acting as its own reviewer
    D  an expired or revoked authority policy
    E  a learned prediction presented as a validated finding
    F  evidence from a different implementation, dataset, parameters or
       backend
    G  a checker that did not run, or whose report cannot be read
    H  two individually valid records that cannot both stand
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tools", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate import result_rules  # noqa: E402
from agent_substrate.agents import (BOOTSTRAP, AgentDirectory,  # noqa: E402
                                    AgentRole, EscalationError,
                                    PrincipalKind, identity)
from agent_substrate.authority import Role, State, TransitionError  # noqa: E402
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_model import (  # noqa: E402
    CHECK_WORKER_ID, REVIEWER_ID, GovernedModelRuns, ModelRunRefused)
from agent_substrate.governed_tasks import POLICY_ID, SUBMITTER_ID  # noqa: E402
from agent_substrate.policy import PolicyStore  # noqa: E402
from agent_substrate.store import StoreError  # noqa: E402
from governed_case import CHECK, MODEL, OTHER, PARAMS  # noqa: E402

import offline_verify as OV  # noqa: E402

WS = "verification/stage10/_pytest_hostile"
PROMOTER = "result-promoter"


@pytest.fixture(scope="module")
def world():
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    (base / "genuine").mkdir(parents=True)
    g = GovernedModelRuns(root=ROOT, log=EventLog(base / "genuine" / "log.jsonl"),
                          evidence=EvidenceStore(base / "genuine" / "evidence"))
    run = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/genuine/run")
    chk = g.check(run, check_id=CHECK, out_dir=f"{WS}/genuine/check")
    other = g.propose(**MODEL, parameters=OTHER, out_dir=f"{WS}/genuine/other")
    other_chk = g.check(other, check_id=CHECK,
                        out_dir=f"{WS}/genuine/other-check")
    yield run, chk, other, other_chk
    if base.exists():
        shutil.rmtree(base)


def _copy(name: str) -> Path:
    src, dst = ROOT / WS / "genuine", ROOT / WS / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copytree(src / "evidence", dst / "evidence")
    shutil.copy(src / "log.jsonl", dst / "log.jsonl")
    return dst


def _open(base: Path) -> GovernedModelRuns:
    return GovernedModelRuns(root=ROOT, log=EventLog(base / "log.jsonl"),
                             evidence=EvidenceStore(base / "evidence"))


def _put(base: Path, doc) -> str:
    return EvidenceStore(base / "evidence").put(
        json.dumps(doc, indent=1, sort_keys=True).encode())


def _doc(base: Path, sha: str) -> dict:
    return json.loads(EvidenceStore(base / "evidence").get(sha))


def _under_review(g: GovernedModelRuns, rid: str, bundle_sha: str,
                  kind: str = result_rules.KIND) -> None:
    g.authority.create(record_id=rid, kind=kind, proposer=SUBMITTER_ID,
                       evidence={"result_bundle": bundle_sha})
    g.authority.transition(record_id=rid, dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)


def _verify(g: GovernedModelRuns, rid: str, report_sha: str, *,
            actor: str = REVIEWER_ID):
    return g.authority.transition(
        record_id=rid, dst=State.VERIFIED, actor=actor, role=Role.VERIFIER,
        evidence={"verification_report": report_sha})


def _forge_line(base: Path, rid: str, src: str, dst: str, actor: str,
                evidence: dict, *, policy_id=None) -> None:
    EventLog(base / "log.jsonl").append(
        actor=actor, action="record.transition", target=rid,
        payload={"record_id": rid, "src": src, "dst": dst,
                 "role": "VERIFIER" if dst == "VERIFIED" else "PROMOTER",
                 "evidence": evidence, "policy_id": policy_id,
                 "stale_reason": None, "edge_reason": "forged",
                 "idempotency_key": None,
                 "admission_policy": result_rules.CURRENT_POLICY})


# ---- the control every case is measured against ------------------------------

def test_control_the_genuine_composition_is_admitted_and_reconstructed(
        world):
    run, chk, _, _ = world
    base = _copy("control")
    g = _open(base)
    assert g.decide(run, chk).state is State.VERIFIED
    g.authority.transition(
        record_id=run.record_id, dst=State.PROMOTED, actor=PROMOTER,
        role=Role.PROMOTER, policy_id=POLICY_ID,
        evidence={"verification_report": chk.report_sha256,
                  "policy_id": POLICY_ID})
    recon = rc.reconstruct(EventLog(base / "log.jsonl"),
                           evidence=EvidenceStore(base / "evidence"))
    assert recon.unauthorized == []
    assert run.record_id in recon.canonical_ids()


# ---- A: integrity is not correctness -------------------------------------------

def test_case_a_an_integrity_valid_artifact_with_an_incorrect_result(world):
    """The bundle's bytes hash to exactly what the record cites -- integrity
    holds -- and its result fails its own invariant. Integrity does not
    admit it; the content rule refuses it, live and on replay."""
    run, chk, _, _ = world
    base = _copy("case-a")
    g = _open(base)
    bundle = json.loads(json.dumps(_doc(base, run.bundle_sha256)))
    bundle["invariants"][0]["holds"] = False
    bad = _put(base, bundle)
    report = _put(base, {**_doc(base, chk.report_sha256),
                         "subject_digest": digest(bundle)})
    _under_review(g, "case-a", bad)
    with pytest.raises(TransitionError, match="invariants not holding"):
        _verify(g, "case-a", report)
    _forge_line(base, "case-a", "UNDER_REVIEW", "VERIFIED", REVIEWER_ID,
                {"verification_report": report})
    recon = rc.reconstruct(EventLog(base / "log.jsonl"),
                           evidence=EvidenceStore(base / "evidence"))
    assert any("case-a" in u for u in recon.unauthorized)


def test_case_a_a_release_signature_never_attests_a_result(tmp_path):
    """The release verifier's report says what no level establishes."""
    sys.path.insert(0, str(ROOT / "tools"))
    import supply_chain as SC
    out = tmp_path / "rc"
    SC.build(out, allow_dirty=True)
    rep = OV.verify_bundle(out, OV.load_policy(
        ROOT / "docs" / "supply_chain_ci_policy.json"), mode="digests")
    assert "scientific validity of any result" in rep["does_not_attest"]
    assert "that any computation is correct" in rep["does_not_attest"]
    assert set(rep["levels"]) == set(OV.LEVELS)


# ---- B: substituted evidence ------------------------------------------------------

@pytest.mark.parametrize("how", ["re-serialized", "another-runs-report"])
def test_case_b_substituted_verification_evidence(world, how):
    run, chk, _, other_chk = world
    base = _copy(f"case-b-{how}")
    g = _open(base)
    if how == "re-serialized":
        # The genuine report's own fields, in other bytes: no governed
        # verification captured THESE bytes.
        report = _put(base, _doc(base, chk.report_sha256))
        assert report != chk.report_sha256
    else:
        report = other_chk.report_sha256
    why = {"re-serialized": "not an artefact of any VERIFIED governed",
           "another-runs-report": "about a different bundle"}[how]
    _under_review(g, f"case-b-{how}", run.bundle_sha256)
    with pytest.raises(TransitionError, match=why):
        _verify(g, f"case-b-{how}", report)
    # Control: the genuine report admits the same record.
    assert _verify(g, f"case-b-{how}",
                   chk.report_sha256).state is State.VERIFIED


# ---- C: an AI reviewing itself ------------------------------------------------------

def test_case_c_the_proposer_or_an_executor_cannot_decide(world):
    run, chk, _, _ = world
    g = _open(_copy("case-c"))
    for who in (run.submitter, run.worker, chk.worker):
        with pytest.raises(ModelRunRefused, match="cannot decide"):
            g.decide(run, chk, reviewer=who)


def test_case_c_the_checks_executor_cannot_admit_through_the_store(world):
    run, chk, _, _ = world
    base = _copy("case-c-store")
    g = _open(base)
    _under_review(g, "case-c", run.bundle_sha256)
    with pytest.raises(TransitionError, match="executed by the proposer"):
        _verify(g, "case-c", chk.report_sha256, actor=CHECK_WORKER_ID)
    assert _verify(g, "case-c", chk.report_sha256).state is State.VERIFIED


def test_case_c_an_agent_cannot_answer_for_a_person(tmp_path):
    d = AgentDirectory(EventLog(tmp_path / "log.jsonl")).load()
    with pytest.raises(Exception, match="both PROPOSER and REVIEWER"):
        identity(agent_id="a", instance_id="a0", kind=PrincipalKind.AGENT,
                 roles={AgentRole.PROPOSER, AgentRole.REVIEWER})
    d.register(identity(agent_id="a", instance_id="a1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.PROPOSER}), by="system")
    d.register(identity(agent_id="b", instance_id="b1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.REVIEWER}), by="system")
    d.escalate(escalation_id="e", task_id="t", question="promote?",
               raised_by="a1", options=("yes", "no"))
    with pytest.raises(EscalationError, match="AGENT principal"):
        d.answer(escalation_id="e", answered_by="b1", answer="yes",
                 reason="looks fine")


def test_case_c_the_agents_identity_cannot_sign_a_release(tmp_path):
    pol = json.loads((ROOT / "docs" / "supply_chain_ci_policy.json")
                     .read_text())
    pol.update(schema=OV.RELEASE_SCHEMA, label="RELEASE_TRUST_ROOT",
               workflow_path=".github/workflows/claude.yml",
               ref="refs/tags/v1", issued_by="owner",
               not_after=(dt.date.today() + dt.timedelta(30)).isoformat(),
               trust_config_sha256="a" * 64)
    pol["signer_identity"] = (f"{pol['source_repository']}/"
                              f"{pol['workflow_path']}@{pol['ref']}")
    f = tmp_path / "p.json"
    f.write_text(json.dumps(pol))
    with pytest.raises(OV.Refused, match="AI agent's workflow"):
        OV.load_policy(f)


# ---- D: a revoked or expired policy ---------------------------------------------------

def test_case_d_a_withdrawn_policy_does_not_promote(world):
    run, chk, _, _ = world
    base = _copy("case-d")
    g = _open(base)
    g.decide(run, chk)
    PolicyStore(EventLog(base / "log.jsonl")).load().withdraw(
        POLICY_ID, actor="owner", reason="revoked")
    g = _open(base)
    with pytest.raises(StoreError, match="withdrawn"):
        g.authority.transition(
            record_id=run.record_id, dst=State.PROMOTED, actor=PROMOTER,
            role=Role.PROMOTER, policy_id=POLICY_ID,
            evidence={"verification_report": chk.report_sha256,
                      "policy_id": POLICY_ID})


def test_case_d_an_expired_release_policy_authorizes_nothing(tmp_path):
    pol = json.loads((ROOT / "docs" / "supply_chain_ci_policy.json")
                     .read_text())
    pol.update(schema=OV.RELEASE_SCHEMA, label="RELEASE_TRUST_ROOT",
               workflow_path=".github/workflows/release.yml",
               ref="refs/tags/v1", issued_by="owner",
               not_after="2000-01-01", trust_config_sha256="a" * 64)
    pol["signer_identity"] = (f"{pol['source_repository']}/"
                              f"{pol['workflow_path']}@{pol['ref']}")
    f = tmp_path / "p.json"
    f.write_text(json.dumps(pol))
    with pytest.raises(OV.Refused, match="expired"):
        OV.load_policy(f)


# ---- E: a learned output dressed as a finding -------------------------------------------

def test_case_e_a_learned_record_cannot_be_verified_however_it_is_cited(
        world):
    """The genuine bundle and the genuine report -- evidence that would
    admit a scientific result -- cited by a learned record: refused, live
    and by the second reader."""
    run, chk, _, _ = world
    base = _copy("case-e")
    g = _open(base)
    _under_review(g, "case-e", run.bundle_sha256, kind="learned_evaluation")
    with pytest.raises(StoreError, match="learned"):
        _verify(g, "case-e", chk.report_sha256)
    _forge_line(base, "case-e", "UNDER_REVIEW", "VERIFIED", REVIEWER_ID,
                {"verification_report": chk.report_sha256})
    recon = rc.reconstruct(EventLog(base / "log.jsonl"),
                           evidence=EvidenceStore(base / "evidence"))
    assert any("case-e" in u and "learned" in u for u in recon.unauthorized)


# ---- F: evidence about something else ---------------------------------------------------

def test_case_f_a_check_of_other_parameters_does_not_admit_this_run(world):
    run, chk, other, other_chk = world
    base = _copy("case-f")
    g = _open(base)
    _under_review(g, "case-f", run.bundle_sha256)
    with pytest.raises(TransitionError, match="about a different bundle"):
        _verify(g, "case-f", other_chk.report_sha256)
    assert digest(_doc(base, other.bundle_sha256)) != \
        digest(_doc(base, run.bundle_sha256))


def test_case_f_a_check_by_the_same_implementation_is_not_independent(
        world):
    run, chk, _, _ = world
    base = _copy("case-f-same")
    g = _open(base)
    bundle = _doc(base, run.bundle_sha256)
    report = _put(base, {**_doc(base, chk.report_sha256),
                         "verifier_implementation_digest":
                             bundle["implementation_digest"]})
    _under_review(g, "case-f-same", run.bundle_sha256)
    with pytest.raises(TransitionError, match="the producer's own code"):
        _verify(g, "case-f-same", report)


# ---- G: a checker that did not run, or cannot be read -----------------------------------

def test_case_g_a_checker_that_did_not_run_admits_nothing(world):
    run, chk, _, _ = world
    base = _copy("case-g")
    g = _open(base)
    report = _put(base, {**_doc(base, chk.report_sha256),
                         "status": "NOT_RUN"})
    _under_review(g, "case-g", run.bundle_sha256)
    with pytest.raises(TransitionError, match="reported NOT_RUN"):
        _verify(g, "case-g", report)


def test_case_g_a_report_nobody_can_read_is_cited_by_nobody(world):
    """A citation of evidence the store does not hold is refused live (I6),
    and on replay a reader without the evidence says UNVERIFIABLE -- never
    VERIFIED."""
    run, chk, _, _ = world
    base = _copy("case-g-missing")
    g = _open(base)
    _under_review(g, "case-g", run.bundle_sha256)
    with pytest.raises((TransitionError, StoreError)):
        _verify(g, "case-g", "e" * 64)
    _verify(g, "case-g", chk.report_sha256)
    recon = rc.reconstruct(EventLog(base / "log.jsonl"), evidence=None)
    assert any("case-g" in u for u in recon.unverifiable)
    assert recon.records["case-g"]["admission"] == result_rules.UNVERIFIABLE


# ---- H: two valid records that cannot both stand ----------------------------------------

def test_case_h_two_valid_answers_to_one_question_do_not_both_stand(
        tmp_path):
    p = tmp_path / "log.jsonl"
    d = AgentDirectory(EventLog(p)).load()
    d.register(identity(agent_id="ai", instance_id="ai1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.PROPOSER}), by="system")
    for h in ("alice", "bob"):
        d.register(identity(agent_id=h, instance_id=h,
                            kind=PrincipalKind.HUMAN,
                            roles={AgentRole.REVIEWER}), by=BOOTSTRAP)
    d.escalate(escalation_id="e", task_id="t", question="release?",
               raised_by="ai1", options=("yes", "no"))
    a = AgentDirectory(EventLog(p)).load()
    b = AgentDirectory(EventLog(p)).load()
    a.answer(escalation_id="e", answered_by="alice", answer="yes",
             reason="reviewed")
    with pytest.raises(EscalationError, match="ANSWERED"):
        b.answer(escalation_id="e", answered_by="bob", answer="no",
                 reason="reviewed")
    assert AgentDirectory(EventLog(p)).load().escalation("e").answer == "yes"


def test_case_h_two_valid_next_versions_of_one_policy(tmp_path):
    from agent_substrate.policy import (Effect, PolicyVersionError,
                                        document, rule)

    def doc(v, note):
        return document(policy_id="p", version=v, description=note,
                        rules=(rule(rule_id="r", effect=Effect.ALLOW,
                                    actions=("*",), subjects=("*",),
                                    roles=("*",), resources=("*",)),))
    p = tmp_path / "log.jsonl"
    PolicyStore(EventLog(p)).load().publish(doc(1, "first"), actor="owner")
    a = PolicyStore(EventLog(p)).load()
    b = PolicyStore(EventLog(p)).load()
    a.publish(doc(2, "a's"), actor="owner")
    with pytest.raises(PolicyVersionError):
        b.publish(doc(2, "b's"), actor="owner")
    assert PolicyStore(EventLog(p)).load().in_force("p").description == "a's"
