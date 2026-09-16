"""Verification in a process that cannot import what it is checking.

THE GAP THESE CLOSE, stated as it was found:

    "separation is implementation-level only: same process, same language,
     same canonical-form module"
    "no separate-process or separate-dependency verifier"

reconstruct.py restates every authority rule rather than importing the
reducers it checks, which is real separation kept by DISCIPLINE. Discipline
decays silently: one future edit importing the primary reducer "to remove the
duplication" makes the comparison circular while it goes on reporting
agreement. So the same reader also runs where that shortcut is unavailable.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_SEPARATE_VERIFY, GovernedStage10,
)
from qta_agent.separate_verify import (  # noqa: E402
    EXIT_CLEAN, EXIT_FINDINGS, EXIT_IMPORT_GUARD, EXIT_UNREADABLE,
    SeparateVerification, SeparateVerificationFailed,
    verify_in_separate_process,
)
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_separate"
VERIFIER = ROOT / "tools" / "independent_verify.py"


@pytest.fixture()
def log(tmp_path):
    lg = EventLog(tmp_path / "log.jsonl")
    for i in range(3):
        lg.append(actor="a", action="record.create", target=f"r{i}",
                  payload={"record_id": f"r{i}", "state": "DRAFT",
                           "title": "x", "kind": "note"})
    return lg


@pytest.fixture()
def gov(request, tmp_path):
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


def _inputs(gov, **over):
    base = {"out_dir": gov.out_rel, "name": "artifact.json",
            "payload": {"label": "MODEL_ONLY", "value": 42}}
    base.update(over)
    return base


# --------------------------------------------------------------------------
# It really is another process, and it really cannot cheat
# --------------------------------------------------------------------------

def test_a_clean_log_verifies(log):
    v = verify_in_separate_process(log.path, root=ROOT)
    assert v.ok, v.reason
    assert v.exit_status == EXIT_CLEAN
    assert v.events_replayed == 3
    assert v.raise_if_bad() is v


def test_the_verifier_refuses_to_import_the_reducers_it_checks():
    """The whole point, asserted as an ImportError rather than a convention.

    A reader that imports the primary reducer is a second call to the first
    implementation, and it agrees with itself for free.
    """
    from tools.independent_verify import FORBIDDEN

    probe = (
        "import sys; sys.path.insert(0, %r);\n"
        "from tools.independent_verify import _Refuse;\n"
        "sys.meta_path.insert(0, _Refuse());\n"
        "import qta_agent.scheduler\n" % str(ROOT)
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode != 0
    assert "refuses to import" in proc.stderr

    # And the list is not empty, which a guard over nothing would be.
    assert len(FORBIDDEN) >= 8
    for name in ("qta_agent.scheduler", "qta_agent.governed_stage10",
                 "qta_agent.capability", "qta_agent.policy"):
        assert name in FORBIDDEN


def test_the_verifier_source_does_not_import_a_primary_reducer():
    """Structural, so the guard cannot be removed and the import added."""
    src = VERIFIER.read_text(encoding="utf-8")
    body = src.partition("def main()")[2]
    for forbidden in ("qta_agent.scheduler", "qta_agent.governed_stage10",
                      "qta_agent.store", "qta_agent.audit"):
        assert f"import {forbidden}" not in body


def test_it_runs_in_a_different_process(log, tmp_path):
    """Not a function call wearing the word 'independent'."""
    marker = tmp_path / "pid.json"
    proc = subprocess.run(
        [sys.executable, str(VERIFIER), str(log.path), "--root", str(ROOT)],
        capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 0
    doc = json.loads(proc.stdout)
    assert doc["ok"] and doc["events_replayed"] == 3
    marker.write_text("ok", encoding="utf-8")


# --------------------------------------------------------------------------
# A crashed verifier is not a pass
# --------------------------------------------------------------------------

def test_a_log_with_findings_is_refused(log):
    """A forged record makes the independent process say no."""
    log.append(actor="mallory", action="record.transition", target="r0",
               payload={"record_id": "r0", "src": "PROMOTED",
                        "dst": "SUPERSEDED", "policy_id": "p"})
    v = verify_in_separate_process(log.path, root=ROOT)
    assert not v.ok
    assert v.exit_status == EXIT_FINDINGS
    assert v.findings
    with pytest.raises(SeparateVerificationFailed):
        v.raise_if_bad()


def test_an_unreadable_log_is_refused(tmp_path):
    bad = tmp_path / "not-a-log.jsonl"
    bad.write_text("{ this is not json\n", encoding="utf-8")
    v = verify_in_separate_process(bad, root=ROOT)
    assert not v.ok
    assert v.exit_status == EXIT_UNREADABLE


def test_a_missing_verifier_is_refused_rather_than_skipped(log, tmp_path):
    """A verifier that is not there has not agreed with anything."""
    empty = tmp_path / "no-tools"
    (empty / "tools").mkdir(parents=True)
    v = verify_in_separate_process(log.path, root=empty)
    assert not v.ok
    assert "has not agreed with anything" in v.reason


def test_a_verifier_that_cannot_start_is_refused(log):
    v = verify_in_separate_process(
        log.path, root=ROOT, python="/nonexistent/python")
    assert not v.ok
    assert "could not be started" in v.reason


def test_a_timeout_is_refused_not_ignored(log, monkeypatch):
    """A verifier that hangs is one that never says no."""
    real = subprocess.run

    def _slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else "?", timeout=0.01)

    monkeypatch.setattr(subprocess, "run", _slow)
    v = verify_in_separate_process(log.path, root=ROOT, timeout_s=0.01)
    assert not v.ok
    assert "never says no" in v.reason
    monkeypatch.setattr(subprocess, "run", real)


def test_an_unparseable_answer_is_not_a_verdict(log, monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "this is not json"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())
    v = verify_in_separate_process(log.path, root=ROOT)
    assert not v.ok
    assert "not a verdict" in v.reason


def test_exit_zero_with_ok_false_is_refused(log, monkeypatch):
    """A status and a verdict that disagree are not evidence."""
    class _Proc:
        returncode = 0
        stdout = json.dumps({"ok": False, "reason": "something"})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())
    v = verify_in_separate_process(log.path, root=ROOT)
    assert not v.ok
    assert "disagree" in v.reason


def test_an_unexpected_exit_status_is_a_crashed_verifier(log, monkeypatch):
    class _Proc:
        returncode = 139
        stdout = json.dumps({"ok": False})
        stderr = "Segmentation fault"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())
    v = verify_in_separate_process(log.path, root=ROOT)
    assert not v.ok
    assert "crashed verifier is not a pass" in v.reason


def test_the_import_guard_status_says_the_verifier_is_wrong(log, monkeypatch):
    """Not a log finding. It sends an operator to a different place."""
    class _Proc:
        returncode = EXIT_IMPORT_GUARD
        stdout = json.dumps({"ok": False, "reason": "import guard: nope"})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())
    v = verify_in_separate_process(log.path, root=ROOT)
    assert not v.ok
    assert "its agreement would have meant nothing" in v.reason


# --------------------------------------------------------------------------
# The production caller
# --------------------------------------------------------------------------

def test_a_governed_run_records_the_independent_verdict(gov):
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))
    assert run.state is TaskState.VERIFIED

    recs = [e.payload for e in gov.log.read()
            if e.action == ACT_SEPARATE_VERIFY]
    assert len(recs) == 1, "the governed run did not consult another process"
    assert recs[0]["ok"] and recs[0]["exit_status"] == 0
    assert recs[0]["events_replayed"] > 0


def test_a_run_is_REJECTED_when_the_independent_process_refuses(
        gov, monkeypatch):
    """The verdict is load-bearing, not decorative.

    A verifier whose answer changes nothing is a log line, and this project
    has a name for those.
    """
    import qta_agent.governed_stage10 as G

    monkeypatch.setattr(
        G, "verify_in_separate_process",
        lambda *a, **kw: SeparateVerification(
            ok=False, exit_status=1, reason="a forged transition at seq 9"))

    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))

    assert run.state is TaskState.REJECTED
    assert "independent process verification refused" in run.reason
    assert "forged transition at seq 9" in run.reason


def test_the_refusal_is_recorded_even_when_it_refuses(gov, monkeypatch):
    """'The independent verifier said no' is exactly the fact an auditor
    must be able to find."""
    import qta_agent.governed_stage10 as G

    monkeypatch.setattr(
        G, "verify_in_separate_process",
        lambda *a, **kw: SeparateVerification(
            ok=False, exit_status=2, reason="unreadable"))
    gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))

    recs = [e.payload for e in gov.log.read()
            if e.action == ACT_SEPARATE_VERIFY]
    assert recs and recs[-1]["ok"] is False
    assert recs[-1]["exit_status"] == 2


def test_a_crashed_verifier_does_not_produce_a_verified_run(gov, monkeypatch):
    import qta_agent.governed_stage10 as G

    monkeypatch.setattr(
        G, "verify_in_separate_process",
        lambda *a, **kw: SeparateVerification(
            ok=False, exit_status=139, reason="a crashed verifier is not a pass"))
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))
    assert run.state is not TaskState.VERIFIED
