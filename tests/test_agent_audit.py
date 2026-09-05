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
from qta_agent.governed_stage10 import GovernedStage10  # noqa: E402

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
              submitter="owner", worker="w1", verifier="v2")
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
    assert set(exp.actors) >= {"owner", "w1", "v2"}


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
    log2.append(actor="w1", action="task.create", target=tid,
                payload={"task_id": tid, "tool_id": "t", "submitter": "w1",
                         "inputs_digest": "a" * 64})
    log2.append(actor="s", action="capability.issue", target=tid,
                payload={"task_id": tid, "tool_id": "t"})
    log2.append(actor="w1", action="task.execution", target=tid,
                payload={"task_id": tid, "tool_id": "t"})
    log2.append(actor="w1", action="task.evidence", target=tid,
                payload={"task_id": tid, "artifacts": {}})
    log2.append(actor="w1", action="task.transition", target=tid,
                payload={"task_id": tid, "src": "EXECUTING",
                         "dst": "COMPLETED", "role": "WORKER",
                         "executed_by": "w1"})
    log2.append(actor="w1", action="task.transition", target=tid,
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
    assert set(idx.actors()) >= {"owner", "scheduler", "w1", "v2", "system"}
    verifier_actions = idx.actions_by("v2")
    assert len(verifier_actions) == 1
    assert verifier_actions[0].payload["dst"] == "VERIFIED"
    assert idx.actions_by("nobody") == ()
    assert run.task_id in idx.subjects()


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
