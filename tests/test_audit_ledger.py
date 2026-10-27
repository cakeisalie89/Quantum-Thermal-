"""The PR #17 audit ledger: a disposition is bound to the content examined.

Each rule of ``tools/audit_ledger.py`` is held here against a ledger built
for the purpose:

- a changed file the ledger does not name;
- a disposition whose file has moved on;
- an unexplained NOT_REVIEWED;
- a finding the register does not hold;
- a generated file claimed without a generator;
- an audit artefact outside the audit's paths.

``derive`` must demote a stale disposition rather than keep it. Then the
committed ledger is checked against git itself. That half needs the merge
base fetched, and it skips without it, unless ``HARNESS_HISTORY_REQUIRED=1``
(the full-suite CI job) makes the skip a failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import audit_ledger as A  # noqa: E402


def _fact(path, blob="b" * 40, change="ADDED"):
    return {"path": path, "change": change,
            "old_blob": blob if change == "DELETED" else None,
            "new_blob": None if change == "DELETED" else blob,
            "mode": "100644", "size": 3, "sha256": "0" * 64,
            "binary": False, "kind": A.kind_of(path)}


def _entry(path, status="REVIEWED_IN_FULL", blob="b" * 40, **kw):
    e = {**_fact(path, blob), "status": status,
         "reviewed_blob": blob if status in A.BOUND else None,
         "method": "read", "findings": [], "note": ""}
    e.update(kw)
    return e


@pytest.fixture
def register(tmp_path, monkeypatch):
    f = tmp_path / "findings.json"
    f.write_text(json.dumps({"findings": [{"id": "AUD-900"}]}))
    monkeypatch.setattr(A, "FINDINGS", f)
    return f


def _doc(*entries):
    return {"base": "x", "entries": list(entries)}


def test_a_clean_ledger_has_no_problem(register):
    doc = _doc(_entry("agent_substrate/events.py", findings=["AUD-900"]))
    assert A.problems(doc, [_fact("agent_substrate/events.py")]) == []


def test_a_changed_file_the_ledger_does_not_name_is_a_problem(register):
    got = A.problems(_doc(), [_fact("tools/x.py")])
    assert any("missing" in p for p in got)


def test_a_disposition_on_a_file_since_changed_is_stale(register):
    doc = _doc(_entry("tools/x.py", blob="a" * 40))
    got = A.problems(doc, [_fact("tools/x.py", blob="a" * 40)])
    assert got == []
    doc["entries"][0]["new_blob"] = "c" * 40
    got = A.problems(doc, [_fact("tools/x.py", blob="c" * 40)])
    assert any("STALE" in p for p in got)


def test_not_reviewed_says_why(register):
    doc = _doc(_entry("tools/x.py", status="NOT_REVIEWED", method=""))
    assert any("without a reason" in p
               for p in A.problems(doc, [_fact("tools/x.py")]))


def test_a_finding_the_register_lacks_is_a_problem(register):
    doc = _doc(_entry("tools/x.py", findings=["AUD-404"]))
    assert any("AUD-404" in p
               for p in A.problems(doc, [_fact("tools/x.py")]))


def test_generated_and_audit_statuses_are_held_to_their_paths(register):
    doc = _doc(_entry("tools/x.py", status="CHECKED_GENERATED"),
               _entry("tools/y.py", status="AUDIT_ARTIFACT"))
    got = A.problems(doc, [_fact("tools/x.py"), _fact("tools/y.py")])
    assert any("no generator" in p for p in got)
    assert any("outside the audit" in p for p in got)


def test_a_regenerated_file_cannot_take_a_status_bound_to_content(register):
    """It has no content facts here, so a bound status would compare
    nothing with nothing and pass."""
    e = {**_entry("final_manifest.json"), "new_blob": None,
         "reviewed_blob": None, "size": None, "sha256": None}
    fact = {**_fact("final_manifest.json"), "new_blob": None, "size": None,
            "sha256": None}
    assert any("cannot bind" in p for p in A.problems(_doc(e), [fact]))


def test_a_bound_status_says_how(register):
    doc = _doc(_entry("tools/x.py", method=""))
    assert any("says nothing of how" in p
               for p in A.problems(doc, [_fact("tools/x.py")]))


def test_derive_demotes_a_stale_disposition(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(A, "LEDGER", ledger)
    monkeypatch.setattr(A, "_git", lambda *a: "HEAD\n")
    old = _entry("tools/x.py", blob="a" * 40, method="read it")
    ledger.write_text(json.dumps({"base": "x", "entries": [old]}))
    monkeypatch.setattr(A, "changes",
                        lambda base: [_fact("tools/x.py", blob="c" * 40)])
    doc = A.derive()
    (e,) = doc["entries"]
    assert e["status"] == "NOT_REVIEWED" and "STALE" in e["note"]
    monkeypatch.setattr(A, "changes",
                        lambda base: [_fact("tools/x.py", blob="a" * 40)])
    ledger.write_text(json.dumps({"base": "x", "entries": [old]}))
    (e,) = A.derive()["entries"]
    assert e["status"] == "REVIEWED_IN_FULL"


def test_crosscheck_finds_what_the_api_disagrees_with(register):
    doc = _doc(_entry("tools/x.py", blob="a" * 40))
    api = [{"filename": "tools/x.py", "status": "modified",
            "sha": "a" * 40},
           {"filename": "tools/z.py", "status": "added", "sha": "d" * 40}]
    got = A.crosscheck(doc, api)
    assert any("modified in the API" in p for p in got)
    assert any("tools/z.py" in p for p in got)


def test_the_committed_ledger_agrees_with_git():
    doc = A.load()
    r = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e",
                        f"{doc['base']}^{{commit}}"], capture_output=True)
    if r.returncode != 0:
        if os.environ.get("HARNESS_HISTORY_REQUIRED") == "1":
            pytest.fail(f"merge base {doc['base']} is not in this "
                        "checkout, and HARNESS_HISTORY_REQUIRED=1")
        pytest.skip(f"merge base {doc['base']} not fetched (shallow)")
    assert A.problems(doc) == []
