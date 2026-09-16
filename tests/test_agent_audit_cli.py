"""The read-only auditor, exercised by BEHAVIOUR rather than by presence.

A command that exists is not a command that works. Every test here runs the
tool the way an operator would -- on a real governed history and on a forged
one -- and checks three things a committed auditor has to get right:

  * it answers the question,
  * it reports a finding in the EXIT STATUS, not only in prose nobody pipes,
  * and it does not touch the log.

The last one is asserted by hashing the file before and after every single
command, because "read-only" is exactly the kind of property that is true
when written and quietly false two refactors later -- and an auditor that
mutates the history it is auditing has destroyed the evidence it was called
to preserve.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tools"))
import audit_log as CLI  # noqa: E402

from qta_agent.canonical import digest_bytes  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_TASK_TRANSITION, SUBMITTER_ID, VERIFIER_ID, WORKER_ID,
    GovernedStage10,
)

WS = "verification/stage10/_pytest_auditcli"


@pytest.fixture()
def governed(request):
    """A real governed run, so the tool is tested on production shapes."""
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    run = g.run(tool_id="stage10.emit_artifact",
                inputs={"out_dir": g.out_rel, "name": "a.json",
                        "payload": {"v": 1}},
                submitter=SUBMITTER_ID, worker=WORKER_ID,
                verifier=VERIFIER_ID)
    yield g, run
    if base.exists():
        shutil.rmtree(base)


@pytest.fixture()
def forged(tmp_path):
    """One actor moving a task end to end, naming a ghost as its executor."""
    log = EventLog(tmp_path / "log.jsonl")
    tid = "t-forged"
    dg = digest_bytes(b"a result nobody produced")

    def tr(src, dst, role, **extra):
        payload = {"task_id": tid, "src": src, "dst": dst, "role": role}
        payload.update(extra)
        log.append(actor="mallory", action=ACT_TASK_TRANSITION, target=tid,
                   payload=payload)

    log.append(actor="mallory", action="task.create", target=tid,
               payload={"task_id": tid, "tool_id": "probe",
                        "submitter": "mallory", "inputs_digest": dg})
    tr("CREATED", "VALIDATED", "SUBMITTER")
    tr("VALIDATED", "QUEUED", "SCHEDULER")
    tr("QUEUED", "LEASED", "WORKER",
       lease={"lease_id": "L1", "holder": "mallory", "granted_seq": 3,
              "expires_after_seq": 9999})
    tr("LEASED", "EXECUTING", "WORKER", lease_id="L1")
    log.append(actor="mallory", action="task.execution", target=tid,
               payload={"task_id": tid, "result_digest": dg,
                        "outcome": "COMPLETED", "tool_id": "probe"})
    tr("EXECUTING", "COMPLETED", "WORKER", lease_id="L1",
       executed_by="a-ghost", result_digest=dg)
    tr("COMPLETED", "VERIFIED", "VERIFIER")
    return log, tid


def _run(log_path, *argv, capsys=None):
    """In-process, so a finding is a return value rather than a stack trace."""
    return CLI.main([str(log_path), *argv])


ALL_COMMANDS = [
    ("verify",), ("subjects",), ("actors",), ("gaps",), ("replay",),
    ("decisions",), ("decisions", "--all"), ("decisions", "--denied"),
    ("timeline",),
]


# --- it answers ------------------------------------------------------------

@pytest.mark.parametrize("argv", ALL_COMMANDS)
def test_every_command_answers_on_a_real_governed_history(governed, argv,
                                                          capsys):
    gov, _ = governed
    assert _run(gov.log.path, *argv) == CLI.OK, argv
    assert capsys.readouterr().out, f"{argv} printed nothing"


def test_explain_renders_the_chain_of_a_verified_task(governed, capsys):
    gov, run = governed
    assert _run(gov.log.path, "explain", run.task_id) == CLI.OK
    out = capsys.readouterr().out
    assert "VERIFIED" in out
    assert "ran stage10.emit_artifact" in out
    assert "PROVENANCE GAPS" not in out


def test_a_governed_history_has_no_gaps_and_the_replay_agrees(governed,
                                                              capsys):
    gov, _ = governed
    assert _run(gov.log.path, "gaps") == CLI.OK
    assert "no provenance gaps" in capsys.readouterr().out
    assert _run(gov.log.path, "replay") == CLI.OK
    assert "found nothing to report" in capsys.readouterr().out


def test_the_json_form_is_actually_machine_readable(governed, capsys):
    """A --json flag that emits prose is worse than no flag."""
    gov, run = governed
    assert _run(gov.log.path, "--json", "explain", run.task_id) == CLI.OK
    rec = json.loads(capsys.readouterr().out)
    assert rec["subject"] == run.task_id
    assert rec["outcome"] == "VERIFIED"
    assert rec["complete"] is True
    assert [s["seq"] for s in rec["steps"]] == sorted(
        s["seq"] for s in rec["steps"]), "steps must be in log order"


def test_decisions_names_the_rule_that_decided(governed, capsys):
    gov, _ = governed
    assert _run(gov.log.path, "decisions", "--all") == CLI.OK
    out = capsys.readouterr().out
    assert "ALLOW" in out and "stage10.governed@1" in out


# --- it reports findings in the EXIT STATUS --------------------------------

def test_a_forged_history_is_a_finding_not_a_clean_report(forged, capsys):
    """The whole point. A tool that printed this and exited 0 would be run
    in a pipeline that ignored it."""
    log, tid = forged
    assert _run(log.path, "gaps") == CLI.FINDING
    out = capsys.readouterr().out
    assert "executor and verifier are both 'mallory'" in out


def test_the_replay_reports_what_it_refused(forged, capsys):
    log, _ = forged
    assert _run(log.path, "replay") == CLI.FINDING
    out = capsys.readouterr().out
    assert "would be refused today" in out
    assert "0 verified" in out


def test_explaining_the_forged_task_is_also_a_finding(forged, capsys):
    log, tid = forged
    assert _run(log.path, "explain", tid) == CLI.FINDING
    assert "PROVENANCE GAPS" in capsys.readouterr().out


def test_a_broken_chain_is_a_finding_from_every_command(governed, capsys):
    """And it must not look like the tool falling over.

    A traceback and a finding are read very differently by whoever is on
    call, and only one of them is this tool working.
    """
    gov, _ = governed
    lines = gov.log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["note"] = "tampered"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    gov.log.path.write_text("\n".join(lines) + "\n")

    assert _run(gov.log.path, "verify") == CLI.FINDING
    assert "BROKEN" in capsys.readouterr().out
    for argv in (("gaps",), ("subjects",), ("replay",), ("timeline",)):
        assert _run(gov.log.path, *argv) == CLI.FINDING, argv
        assert "does not verify" in capsys.readouterr().err


def test_a_question_that_cannot_be_ASKED_is_distinct_from_a_finding(governed):
    """Exit 2, not 1. 'No such task' and 'this task is broken' are different
    answers, and a caller that cannot tell them apart will treat a typo as
    an incident."""
    gov, _ = governed
    assert _run(gov.log.path, "explain", "task-that-does-not-exist") == \
        CLI.CANNOT_ASK
    assert _run(gov.log.path, "decision", "9999") == CLI.CANNOT_ASK
    assert CLI.main([str(ROOT / "no" / "such" / "log.jsonl"), "verify"]) == \
        CLI.CANNOT_ASK


# --- it does not write -----------------------------------------------------

@pytest.mark.parametrize("argv", ALL_COMMANDS)
def test_no_command_modifies_the_log(governed, argv):
    """Hashed before and after, for every command, every time.

    Not a docstring promise: an auditor is run mid-incident by somebody who
    is not certain what they are doing, and the cost of being wrong here is
    the evidence.
    """
    gov, _ = governed
    before = hashlib.sha256(gov.log.path.read_bytes()).hexdigest()
    _run(gov.log.path, *argv)
    after = hashlib.sha256(gov.log.path.read_bytes()).hexdigest()
    assert before == after, f"{argv} modified the log"


def test_the_auditor_writes_nothing_anywhere_beneath_the_log(governed):
    """Not only the log file: no sidecar, no index, no 'audit happened'
    record, no lock left behind."""
    gov, run = governed
    base = gov.log.path.parent
    before = {p: p.stat().st_mtime_ns for p in sorted(base.rglob("*"))
              if p.is_file()}
    for argv in ALL_COMMANDS + [("explain", run.task_id)]:
        _run(gov.log.path, *argv)
    after = {p: p.stat().st_mtime_ns for p in sorted(base.rglob("*"))
             if p.is_file()}
    assert set(after) == set(before), (
        f"the auditor created or removed files: "
        f"{set(after) ^ set(before)}")
    assert after == before, "the auditor modified a file it should only read"


def test_the_auditor_appends_no_event_of_its_own(governed):
    gov, _ = governed
    n_before = gov.log.verify().count
    for argv in ALL_COMMANDS:
        _run(gov.log.path, *argv)
    assert gov.log.verify().count == n_before


# --- it is a real command, not an importable function ----------------------

def test_it_runs_as_a_subprocess_with_the_documented_exit_codes(governed):
    """The in-process tests above would pass for a module that cannot be
    invoked. This is the interface an operator and a CI step actually use.
    """
    gov, run = governed
    ok = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "audit_log.py"),
         str(gov.log.path), "explain", run.task_id],
        capture_output=True, text=True, timeout=120)
    assert ok.returncode == CLI.OK, ok.stderr
    assert "VERIFIED" in ok.stdout

    missing = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "audit_log.py"),
         str(gov.log.path), "explain", "nope"],
        capture_output=True, text=True, timeout=120)
    assert missing.returncode == CLI.CANNOT_ASK
    assert "no task 'nope'" in missing.stderr


def test_the_workflow_runs_the_auditor_over_the_governed_run():
    """A committed auditor nobody runs is an auditor of nothing.

    The point of this row is not that the file exists; it is that a hosted
    run asks these questions of a real history on every push, and fails when
    the answer is a finding.
    """
    wf = (ROOT / ".github" / "workflows" / "agent-substrate.yml").read_text(
        encoding="utf-8")
    assert "tools/audit_log.py" in wf, (
        "the read-only auditor is not run by CI, so nothing keeps it working")
    assert "task_log.jsonl" in wf, (
        "CI must run the auditor over the governed run's real log, not a "
        "fixture built for it")
