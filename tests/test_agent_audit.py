"""Audit queries: the log has to answer questions, not just hold them."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.audit import (  # noqa: E402
    REDACTED, REQUIRED_RECORDS, AuditIndex, redact,
)
from qta_agent.events import ChainBroken, EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    SUBMITTER_ID, VERIFIER_ID, WORKER_ID, GovernedStage10,
)

WS = "verification/stage10/_pytest_audit"


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _run(gov, **over):
    kw = dict(tool_id="stage10.emit_artifact",
              inputs={"out_dir": gov.out_rel, "name": "a.json",
                      "payload": {"v": 1}},
              submitter=SUBMITTER_ID, worker=WORKER_ID,
              verifier=VERIFIER_ID)
    kw.update(over)
    return gov.run(**kw)


# --- reconstructing a chain -------------------------------------------------

def test_a_governed_run_explains_itself_end_to_end(gov):
    run = _run(gov)
    exp = AuditIndex.from_log(gov.log).explain_task(run.task_id)

    assert exp.outcome == "VERIFIED"
    assert exp.complete, f"unexpected provenance gaps: {exp.gaps}"
    actions = [s.action for s in exp.steps]
    for required in REQUIRED_RECORDS["VERIFIED"]:
        assert required in actions, f"{required} missing from the chain"
    assert set(exp.actors) >= {SUBMITTER_ID, WORKER_ID, VERIFIER_ID}


def test_the_chain_names_the_tool_version_and_the_authority(gov):
    """A chain that does not say WHICH tool ran is not provenance."""
    run = _run(gov)
    exp = AuditIndex.from_log(gov.log).explain_task(run.task_id)
    ex = next(s for s in exp.steps if s.action == "task.execution")
    assert ex.detail["tool_id"] == "stage10.emit_artifact"
    assert ex.detail["tool_version"] == "1.0.0"
    assert ex.detail["tool_digest"]
    cap = next(s for s in exp.steps if s.action == "capability.issue")
    assert cap.detail["tool_id"] == "stage10.emit_artifact"
    assert cap.detail["scope"] == ["verification/stage10"]


def test_the_rendered_chain_is_readable(gov):
    run = _run(gov)
    text = AuditIndex.from_log(gov.log).explain_task(run.task_id).render()
    assert "VERIFIED" in text
    assert "COMPLETED -> VERIFIED" in text
    assert "PROVENANCE GAPS" not in text


def test_an_unknown_subject_is_reported_not_invented(gov):
    _run(gov)
    exp = AuditIndex.from_log(gov.log).explain_task("task-does-not-exist")
    assert exp.outcome == "UNKNOWN" and not exp.complete
    assert exp.steps == ()


# --- the question enforcement cannot ask ------------------------------------

def test_a_verified_task_missing_its_evidence_record_is_a_gap(gov, tmp_path):
    """THE point of this module.

    The state machine only sees the transition in front of it. Only a reader
    of the whole history can notice that a VERIFIED task has nothing showing
    what it produced -- and a hole in provenance is indistinguishable from a
    fabrication nobody happened to notice.
    """
    run = _run(gov)
    kept = [json.loads(line) for line in
            gov.log.path.read_text().splitlines()]
    trimmed = [r for r in kept if r["action"] != "task.evidence"]
    # Rebuild a well-formed chain WITHOUT the evidence record, so the log
    # itself verifies and only the provenance is short.
    rebuilt = tmp_path / "trimmed.jsonl"
    log2 = EventLog(rebuilt)
    for r in trimmed:
        log2.append(actor=r["actor"], action=r["action"], target=r["target"],
                    payload=r["payload"])
    exp = AuditIndex.from_log(log2).explain_task(run.task_id)
    assert exp.outcome == "VERIFIED"
    assert not exp.complete
    assert any("task.evidence" in g for g in exp.gaps), exp.gaps


def test_a_log_showing_self_verification_is_flagged(gov, tmp_path):
    """Unreachable through the gate, and checked anyway.

    This reads the LOG, so it catches a history written by something that did
    not go through the state machine at all -- which is the only way such a
    record could exist, and exactly the case where nobody is watching.
    """
    log2 = EventLog(tmp_path / "forged.jsonl")
    tid = "task-forged"
    log2.append(actor=WORKER_ID, action="task.create", target=tid,
                payload={"task_id": tid, "tool_id": "t", "submitter": WORKER_ID,
                         "inputs_digest": "a" * 64})
    log2.append(actor="s", action="capability.issue", target=tid,
                payload={"task_id": tid, "tool_id": "t"})
    log2.append(actor=WORKER_ID, action="task.execution", target=tid,
                payload={"task_id": tid, "tool_id": "t"})
    log2.append(actor=WORKER_ID, action="task.evidence", target=tid,
                payload={"task_id": tid, "artifacts": {}})
    log2.append(actor=WORKER_ID, action="task.transition", target=tid,
                payload={"task_id": tid, "src": "EXECUTING",
                         "dst": "COMPLETED", "role": "WORKER",
                         "executed_by": WORKER_ID})
    log2.append(actor=WORKER_ID, action="task.transition", target=tid,
                payload={"task_id": tid, "src": "COMPLETED",
                         "dst": "VERIFIED", "role": "VERIFIER"})
    exp = AuditIndex.from_log(log2).explain_task(tid)
    assert not exp.complete
    assert any("executor and verifier are both" in g for g in exp.gaps), exp.gaps


def test_audit_all_puts_the_incomplete_chains_first(gov, tmp_path):
    """An auditor should not have to sort the findings themselves."""
    log2 = EventLog(tmp_path / "mixed.jsonl")
    log2.append(actor="w", action="task.create", target="task-b",
                payload={"task_id": "task-b", "tool_id": "t",
                         "submitter": "w", "inputs_digest": "a" * 64})
    log2.append(actor="w", action="task.transition", target="task-b",
                payload={"task_id": "task-b", "src": "EXECUTING",
                         "dst": "VERIFIED", "role": "VERIFIER"})
    results = AuditIndex.from_log(log2).audit_all()
    assert results and not results[0].complete


# --- an audit over an unverified log is worse than none ---------------------

def test_an_audit_refuses_a_tampered_log(gov):
    """A confident answer with no basis is worse than a refusal."""
    _run(gov)
    lines = gov.log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["note"] = "tampered"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    gov.log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        AuditIndex.from_log(gov.log)


# --- tracing bytes ----------------------------------------------------------

def test_an_artifact_is_traced_by_content_not_by_name(gov):
    run = _run(gov)
    idx = AuditIndex.from_log(gov.log)
    dg = next(iter(run.artifacts.values()))
    hits = idx.trace_artifact(dg)
    assert len(hits) == 1
    task_id, rel, seq = hits[0]
    assert task_id == run.task_id and rel.endswith("a.json")


@pytest.mark.parametrize("bad", ["a.json", "", "A" * 64, "z" * 64, None, 42])
def test_tracing_requires_a_digest(gov, bad):
    _run(gov)
    idx = AuditIndex.from_log(gov.log)
    with pytest.raises(ValueError, match="not a sha256 digest"):
        idx.trace_artifact(bad)


def test_an_untracked_digest_traces_to_nothing_rather_than_guessing(gov):
    _run(gov)
    assert AuditIndex.from_log(gov.log).trace_artifact("b" * 64) == ()


# --- who did what -----------------------------------------------------------

def test_every_action_is_attributable_to_an_actor(gov):
    run = _run(gov)
    idx = AuditIndex.from_log(gov.log)
    assert set(idx.actors()) >= {
        "owner", "scheduler", WORKER_ID, VERIFIER_ID, "system"}
    assert idx.actions_by("nobody") == ()
    assert run.task_id in idx.subjects()

    # The verifier does more than one thing now -- it also reports the
    # outcome to the scheduler and files the run's note. What must remain
    # true is narrower and more important than "exactly one action": the
    # verifier performed the VERIFIED transition, and did NOT perform any of
    # the executor's.
    by_verifier = idx.actions_by(VERIFIER_ID)
    transitions = [e for e in by_verifier
                   if e.action == "task.transition"]
    assert [e.payload["dst"] for e in transitions] == ["VERIFIED"]
    assert not [e for e in by_verifier
                if e.action in ("task.execution", "task.evidence")], (
        "the verifier executed or captured evidence for the work it was "
        "verifying")
    by_worker = idx.actions_by(WORKER_ID)
    assert not [e for e in by_worker if e.action == "task.transition"
                and e.payload.get("dst") == "VERIFIED"]


def test_the_timeline_covers_every_record(gov):
    _run(gov)
    idx = AuditIndex.from_log(gov.log)
    assert len(idx.timeline()) == gov.log.verify().count


# --- redaction --------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "token", "api_key", "API-KEY", "password", "secret", "Authorization",
    "bearer_token", "private_key", "credential",
])
def test_credential_shaped_keys_are_redacted(key):
    assert redact({key: "hunter2"})[key] == REDACTED
    assert redact({"outer": [{key: "hunter2"}]})["outer"][0][key] == REDACTED


def test_redaction_matches_keys_not_values(gov):
    """Digests must survive; guessing whether a 64-char string is a token
    would blank the provenance this module exists to show."""
    dg = "a" * 64
    assert redact({"result_digest": dg})["result_digest"] == dg
    assert redact({"scope": ["verification/stage10"]})["scope"] == \
        ["verification/stage10"]


def test_an_explanation_redacts_on_the_way_out(gov):
    run = _run(gov)
    exp = AuditIndex.from_log(gov.log).explain_task(run.task_id)
    rec = exp.to_record()
    blob = json.dumps(rec)
    assert "hunter2" not in blob
    # And the digests are still there to be read.
    assert any(s["detail"].get("tool_digest") for s in rec["steps"]
               if s["action"] == "task.execution")


def test_the_audit_is_part_of_the_production_path():
    """A provenance gap must fail the build, not be discovered later.

    An auditor that only runs when someone remembers to run it audits nothing
    in the cases that matter. The governed rule asserts completeness and
    embeds the chain in its report, so the provenance travels with the
    artifact rather than living only in a log somebody has to find.
    """
    rule = (ROOT / "Snakefile").read_text(encoding="utf-8") \
        .split("rule s10_governed:", 1)[1].split("\nrule ", 1)[0]
    assert "AuditIndex" in rule
    assert "assert explanation.complete" in rule
    assert "explanation.to_record()" in rule
    # The policy join and the record audit are on the path too. A decision
    # that names a digest nobody published is an assertion that a policy
    # allowed the run, which is what an unauthorized run would also produce.
    assert "assert decision.complete" in rule
    assert "index.denials()" in rule
    assert "index.audit_records()" in rule


# --- authority records: the twin of the task chain ---------------------------

def _authority_world(tmp_path):
    """A store with an evidence-backed record, built the ordinary way."""
    from qta_agent.authority import Role, State
    from qta_agent.store import AuthorityStore

    log = EventLog(tmp_path / "auth.jsonl")
    ev = EvidenceStore(tmp_path / "ev")
    store = AuthorityStore(log, evidence=ev).load()
    return log, ev, store, Role, State


def _promote(store, ev, Role, State, rid, *, proposer="alice",
             verifier="bob", promoter="carol", depends_on=()):
    report = ev.put(f"report for {rid}".encode())
    store.create(record_id=rid, kind="claim", proposer=proposer,
                 depends_on=tuple(depends_on))
    store.transition(record_id=rid, dst=State.UNDER_REVIEW, actor=verifier,
                     role=Role.VERIFIER)
    store.transition(record_id=rid, dst=State.VERIFIED, actor=verifier,
                     role=Role.VERIFIER,
                     evidence={"verification_report": report})
    store.transition(record_id=rid, dst=State.PROMOTED, actor=promoter,
                     role=Role.PROMOTER, policy_id="p1",
                     evidence={"verification_report": report,
                               "policy_id": "p1"})
    return report


def _revoke(store, ev, Role, State, rid, *, actor="carol"):
    reason = ev.put(f"revocation of {rid}".encode())
    return store.transition(record_id=rid, dst=State.REVOKED, actor=actor,
                            role=Role.PROMOTER,
                            evidence={"revocation_reason": reason})


def test_an_authority_record_explains_itself(tmp_path):
    log, ev, store, Role, State = _authority_world(tmp_path)
    _promote(store, ev, Role, State, "r1")

    exp = AuditIndex.from_log(log).explain_record("r1")
    assert exp.outcome == "PROMOTED"
    assert exp.complete, f"unexpected gaps: {exp.gaps}"
    assert [s.detail.get("dst") for s in exp.steps
            if s.action == "record.transition"] == [
        "UNDER_REVIEW", "VERIFIED", "PROMOTED"]
    assert set(exp.actors) == {"alice", "bob", "carol"}
    assert "PROVENANCE GAPS" not in exp.render()


def test_an_unknown_record_is_reported_not_invented(tmp_path):
    log, ev, store, Role, State = _authority_world(tmp_path)
    _promote(store, ev, Role, State, "r1")
    exp = AuditIndex.from_log(log).explain_record("nope")
    assert exp.outcome == "UNKNOWN" and not exp.complete and exp.steps == ()


def test_a_task_id_is_not_an_authority_record(gov):
    """The two explains must not answer each other's questions."""
    run = _run(gov)
    idx = AuditIndex.from_log(gov.log)
    assert idx.explain_record(run.task_id).outcome == "UNKNOWN"
    assert idx.records() == ()


def test_a_discontinuous_history_is_a_gap(tmp_path):
    """THE point of explain_record.

    ``AuthorityStore`` reads current state before appending, so it cannot
    write a transition whose src is not the previous dst. A log containing
    one was written around the store -- and every individual record in it
    still looks well-formed.
    """
    log = EventLog(tmp_path / "forged.jsonl")
    log.append(actor="alice", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": "alice", "state": "PROPOSED",
                        "evidence": {}, "depends_on": [], "policy_id": None})
    # PROPOSED -> UNDER_REVIEW never happened; the history jumps.
    log.append(actor="bob", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "UNDER_REVIEW",
                        "dst": "VERIFIED", "role": "VERIFIER",
                        "evidence": {"verification_report": "a" * 64},
                        "policy_id": None})
    exp = AuditIndex.from_log(log).explain_record("r1")
    assert not exp.complete
    assert any("was not written through it" in g for g in exp.gaps), exp.gaps


def test_an_impossible_edge_is_a_gap(tmp_path):
    log = EventLog(tmp_path / "forged.jsonl")
    log.append(actor="alice", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": "alice", "state": "PROPOSED",
                        "evidence": {}, "depends_on": [], "policy_id": None})
    log.append(actor="carol", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "PROPOSED",
                        "dst": "PROMOTED", "role": "PROMOTER",
                        "evidence": {}, "policy_id": "p1"})
    exp = AuditIndex.from_log(log).explain_record("r1")
    assert not exp.complete
    assert any("not an edge of the authority state machine" in g
               for g in exp.gaps), exp.gaps


def test_self_promotion_in_a_log_is_flagged(tmp_path):
    """Unreachable through check(); checked anyway, for the same reason."""
    log = EventLog(tmp_path / "forged.jsonl")
    log.append(actor="alice", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": "alice", "state": "PROPOSED",
                        "evidence": {}, "depends_on": [], "policy_id": None})
    log.append(actor="alice", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "PROPOSED",
                        "dst": "UNDER_REVIEW", "role": "VERIFIER",
                        "evidence": {}, "policy_id": None})
    log.append(actor="alice", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "UNDER_REVIEW",
                        "dst": "VERIFIED", "role": "VERIFIER",
                        "evidence": {"verification_report": "a" * 64},
                        "policy_id": None})
    exp = AuditIndex.from_log(log).explain_record("r1")
    assert not exp.complete
    assert any("requires a distinct actor (I4)" in g for g in exp.gaps), \
        exp.gaps


def test_promotion_without_a_policy_is_a_gap(tmp_path):
    log = EventLog(tmp_path / "forged.jsonl")
    log.append(actor="alice", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": "alice", "state": "PROPOSED",
                        "evidence": {}, "depends_on": [], "policy_id": None})
    for src, dst, role, actor in (("PROPOSED", "UNDER_REVIEW", "VERIFIER",
                                   "bob"),
                                  ("UNDER_REVIEW", "VERIFIED", "VERIFIER",
                                   "bob"),
                                  ("VERIFIED", "PROMOTED", "PROMOTER",
                                   "carol")):
        log.append(actor=actor, action="record.transition", target="r1",
                   payload={"record_id": "r1", "src": src, "dst": dst,
                            "role": role, "policy_id": None,
                            "evidence": {"verification_report": "a" * 64}})
    exp = AuditIndex.from_log(log).explain_record("r1")
    assert any("names no policy (I5)" in g for g in exp.gaps), exp.gaps


def test_a_transition_missing_its_required_evidence_is_a_gap(tmp_path):
    log = EventLog(tmp_path / "forged.jsonl")
    log.append(actor="alice", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": "alice", "state": "PROPOSED",
                        "evidence": {}, "depends_on": [], "policy_id": None})
    log.append(actor="bob", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "PROPOSED",
                        "dst": "UNDER_REVIEW", "role": "VERIFIER",
                        "evidence": {}, "policy_id": None})
    log.append(actor="bob", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "UNDER_REVIEW",
                        "dst": "VERIFIED", "role": "VERIFIER",
                        "evidence": {}, "policy_id": None})
    exp = AuditIndex.from_log(log).explain_record("r1")
    assert any("requires evidence ['verification_report']" in g
               for g in exp.gaps), exp.gaps


def test_canonical_authority_resting_on_a_withdrawn_dependency_is_a_gap(
        tmp_path):
    """The cross-record hole, and the reason this is not redundant.

    ``store.py`` applies one event at a time and never looks at dependents.
    :mod:`qta_agent.invalidation` cascades ONLY when a caller runs it. So a
    cascade nobody ran leaves a PROMOTED record resting on a REVOKED one,
    every individual transition legal, and nothing in the enforcement path
    able to notice.
    """
    log, ev, store, Role, State = _authority_world(tmp_path)
    _promote(store, ev, Role, State, "base")
    _promote(store, ev, Role, State, "derived", depends_on=("base",))

    idx = AuditIndex.from_log(log)
    assert idx.explain_record("derived").complete, \
        idx.explain_record("derived").gaps

    # Withdraw the foundation, and deliberately do NOT run the cascade.
    _revoke(store, ev, Role, State, "base")

    idx = AuditIndex.from_log(log)
    assert idx.explain_record("base").complete, idx.explain_record("base").gaps
    exp = idx.explain_record("derived")
    assert not exp.complete
    assert any("withdrawn foundations" in g for g in exp.gaps), exp.gaps


def test_running_the_cascade_closes_that_gap(tmp_path):
    """The gap must describe a real omission, not merely a shape it dislikes.

    If the auditor flagged the dependency regardless of whether the cascade
    ran, it would be reporting the design rather than a defect.
    """
    from qta_agent.invalidation import apply_invalidation

    log, ev, store, Role, State = _authority_world(tmp_path)
    _promote(store, ev, Role, State, "base")
    _promote(store, ev, Role, State, "derived", depends_on=("base",))
    _revoke(store, ev, Role, State, "base")
    apply_invalidation(store, "base", reason="dependency revoked")

    exp = AuditIndex.from_log(log).explain_record("derived")
    assert exp.outcome == "STALE"
    assert exp.complete, exp.gaps


def test_a_dependency_on_something_unrecorded_is_a_gap(tmp_path):
    log = EventLog(tmp_path / "forged.jsonl")
    log.append(actor="alice", action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": "alice", "state": "PROMOTED",
                        "evidence": {}, "depends_on": ["ghost"],
                        "policy_id": "p1"})
    exp = AuditIndex.from_log(log).explain_record("r1")
    assert any("never created" in g for g in exp.gaps), exp.gaps


def test_transitions_without_a_creation_record_are_a_gap(tmp_path):
    log = EventLog(tmp_path / "forged.jsonl")
    log.append(actor="bob", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "PROPOSED",
                        "dst": "UNDER_REVIEW", "role": "VERIFIER",
                        "evidence": {}, "policy_id": None})
    exp = AuditIndex.from_log(log).explain_record("r1")
    assert any("no record.create record" in g for g in exp.gaps), exp.gaps


def test_audit_records_puts_the_holes_first(tmp_path):
    log, ev, store, Role, State = _authority_world(tmp_path)
    _promote(store, ev, Role, State, "a-clean")
    _promote(store, ev, Role, State, "z-broken", depends_on=("a-clean",))
    _revoke(store, ev, Role, State, "a-clean")

    results = AuditIndex.from_log(log).audit_records()
    assert [e.subject for e in results] == ["z-broken", "a-clean"]
    assert not results[0].complete and results[1].complete


def test_an_added_dependency_is_seen_too(tmp_path):
    """add_dependency writes a separate record; the audit must join it."""
    log, ev, store, Role, State = _authority_world(tmp_path)
    _promote(store, ev, Role, State, "base")
    _promote(store, ev, Role, State, "derived")
    store.add_dependency(record_id="derived", depends_on=("base",))
    _revoke(store, ev, Role, State, "base")
    exp = AuditIndex.from_log(log).explain_record("derived")
    assert any("withdrawn foundations" in g for g in exp.gaps), exp.gaps


# --- policy decisions: a query, not a chain ---------------------------------

def _policy_world(tmp_path):
    from qta_agent.policy import (
        ANY, Effect, PolicyDocument, PolicyRequest, PolicyStore, Rule,
    )

    log = EventLog(tmp_path / "pol.jsonl")
    store = PolicyStore(log).load()
    doc = PolicyDocument(
        policy_id="p1", version=1, description="test",
        rules=(Rule(rule_id="deny-danger", effect=Effect.DENY,
                    actions=("delete",), subjects=(ANY,), roles=(ANY,),
                    resources=(ANY,), reason="deletion is never permitted"),
               Rule(rule_id="allow-read", effect=Effect.ALLOW,
                    actions=("read",), subjects=("alice",), roles=(ANY,),
                    resources=(ANY,), reason="alice may read")))
    store.publish(doc, actor="owner")
    return log, store, doc, PolicyRequest


def test_decisions_are_queryable_and_denials_are_recorded(tmp_path):
    """decide_and_record logs denials so this question has an answer.

    A control plane that records only what it permitted cannot answer 'what
    did this agent try', which is where an incident starts.
    """
    log, store, doc, PolicyRequest = _policy_world(tmp_path)
    store.decide_and_record(
        "p1", PolicyRequest(action="read", subject="alice", role="WORKER",
                            resource="doc-1"), actor="alice")
    store.decide_and_record(
        "p1", PolicyRequest(action="delete", subject="alice", role="WORKER",
                            resource="doc-1"), actor="alice")
    store.decide_and_record(
        "p1", PolicyRequest(action="read", subject="mallory", role="WORKER",
                            resource="doc-1"), actor="mallory")

    idx = AuditIndex.from_log(log)
    assert len(idx.decisions()) == 3
    denied = idx.denials()
    assert len(denied) == 2
    assert {d.detail["request"]["subject"] for d in denied} == \
        {"alice", "mallory"}
    assert any("deletion is never permitted" in d.summary for d in denied)
    assert any("no rule matched; the default is deny" in d.summary
               for d in denied)


def test_decision_filters_are_exact_and_conjunctive(tmp_path):
    log, store, doc, PolicyRequest = _policy_world(tmp_path)
    for subject in ("alice", "alicia"):
        store.decide_and_record(
            "p1", PolicyRequest(action="read", subject=subject, role="WORKER",
                                resource="doc-1"), actor=subject)

    idx = AuditIndex.from_log(log)
    # 'alice' must not match 'alicia': an auditor who half-matches a subject
    # gets a confident answer about the wrong principal.
    assert len(idx.decisions(subject="alice")) == 1
    assert idx.decisions(subject="alice")[0].detail["allowed"] is True
    assert len(idx.decisions(subject="alic")) == 0
    # Conjunctive: both must hold.
    assert len(idx.decisions(subject="alice", action="delete")) == 0
    assert len(idx.decisions(policy_id="p1", allowed=False)) == 1
    assert len(idx.decisions(resource="doc-1")) == 2
    assert len(idx.decisions(resource="doc-2")) == 0


def test_denials_cannot_be_asked_for_allowed_decisions(tmp_path):
    """denials(allowed=True) must not quietly return permissions."""
    log, store, doc, PolicyRequest = _policy_world(tmp_path)
    store.decide_and_record(
        "p1", PolicyRequest(action="read", subject="alice", role="WORKER",
                            resource="doc-1"), actor="alice")
    idx = AuditIndex.from_log(log)
    assert idx.denials(allowed=True) == ()


def test_a_decision_is_joined_to_the_document_that_made_it(tmp_path):
    log, store, doc, PolicyRequest = _policy_world(tmp_path)
    d = store.decide_and_record(
        "p1", PolicyRequest(action="delete", subject="alice", role="WORKER",
                            resource="doc-1"), actor="alice")

    exp = AuditIndex.from_log(log).explain_decision(d.at_seq)
    assert exp.outcome == "DENY"
    assert exp.complete, exp.gaps
    assert [s.action for s in exp.steps] == ["policy.publish", "policy.decision"]
    assert doc.digest()[:12] in exp.steps[0].summary


def test_a_decision_citing_an_unpublished_policy_digest_is_a_gap(tmp_path):
    """Two documents with one id and version but different content.

    That is a tampering signature, and only the digest distinguishes them.
    """
    log, store, doc, PolicyRequest = _policy_world(tmp_path)
    d = store.decide_and_record(
        "p1", PolicyRequest(action="delete", subject="alice", role="WORKER",
                            resource="doc-1"), actor="alice")
    # Rewrite the decision's cited digest, then rebuild a well-formed chain
    # so the LOG verifies and only the join is wrong.
    kept = [json.loads(line) for line in log.path.read_text().splitlines()]
    for rec in kept:
        if rec["action"] == "policy.decision":
            rec["payload"]["decision"]["policy_digest"] = "f" * 64
    forged = EventLog(log.path.parent / "forged.jsonl")
    for rec in kept:
        forged.append(actor=rec["actor"], action=rec["action"],
                      target=rec["target"], payload=rec["payload"])

    exp = AuditIndex.from_log(forged).explain_decision(d.at_seq)
    assert not exp.complete
    assert any("tampering signature" in g for g in exp.gaps), exp.gaps


def test_a_decision_under_a_policy_this_log_never_published_is_a_gap(tmp_path):
    log = EventLog(tmp_path / "orphan.jsonl")
    ev = log.append(actor="a", action="policy.decision", target="doc-1",
                    payload={"decision": {"allowed": True, "policy_id": "ghost",
                                          "version": 1,
                                          "policy_digest": "a" * 64,
                                          "rule_id": "r", "effect": "ALLOW",
                                          "request": {"subject": "a",
                                                      "action": "read",
                                                      "resource": "doc-1"},
                                          "reason": "because"},
                             "decision_digest": "b" * 64})
    exp = AuditIndex.from_log(log).explain_decision(ev.seq)
    assert not exp.complete
    assert any("never published" in g for g in exp.gaps), exp.gaps


def test_an_absent_decision_is_reported_not_invented(tmp_path):
    log, store, doc, PolicyRequest = _policy_world(tmp_path)
    exp = AuditIndex.from_log(log).explain_decision(9999)
    assert exp.outcome == "UNKNOWN" and not exp.complete and exp.steps == ()


def test_policy_versions_are_read_from_the_log_not_the_projection(tmp_path):
    """The auditor must not depend on the code whose decision is in question."""
    from qta_agent.policy import ANY, Effect, PolicyDocument, Rule

    log, store, doc, PolicyRequest = _policy_world(tmp_path)
    v2 = PolicyDocument(
        policy_id="p1", version=2, description="tightened",
        rules=(Rule(rule_id="deny-all", effect=Effect.DENY, actions=(ANY,),
                    subjects=(ANY,), roles=(ANY,), resources=(ANY,)),))
    store.publish(v2, actor="owner")

    versions = AuditIndex.from_log(log).policy_versions("p1")
    assert [v for _, v, _ in versions] == [1, 2]
    assert [dg for _, _, dg in versions] == [doc.digest(), v2.digest()]
    assert AuditIndex.from_log(log).policy_versions("other") == ()


def test_the_governed_run_records_the_decision_that_permitted_it(gov):
    """The query must work on the production history, not only a fixture."""
    run = _run(gov)
    idx = AuditIndex.from_log(gov.log)
    allowed = idx.decisions(allowed=True)
    assert allowed, "the governed run recorded no permitting decision"
    assert any(d.detail["policy_digest"] == run.policy_digest
               for d in allowed), (
        "no recorded decision matches the policy digest the run reports")
    seq = next(d.seq for d in allowed
               if d.detail["policy_digest"] == run.policy_digest)
    exp = idx.explain_decision(seq)
    assert exp.outcome == "ALLOW" and exp.complete, exp.gaps
