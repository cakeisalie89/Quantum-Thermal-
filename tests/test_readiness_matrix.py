"""The production-readiness matrix is held to the tree, and says plainly
whether its contract is met (it is not while anything is blocked,
unverified or only locally tested)."""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import attach_hosted_evidence as AH  # noqa: E402
import readiness as RD  # noqa: E402


def test_the_shipped_matrix_holds():
    doc = RD.load()
    assert len(doc["rows"]) >= 15, "the check would be vacuous"
    assert RD.problems(doc) == []


def test_the_contract_is_not_reported_met_while_rows_are_open():
    s = RD.summary(RD.load())
    assert s["by_status"]["EXTERNALLY_BLOCKED"] > 0
    assert s["contract_met"] is False
    assert s["verdict"] == "PRODUCTION_READINESS_CONTRACT_NOT_MET"


def _row(doc, status):
    return next(r for r in doc["rows"] if r["status"] == status)


def _hosted(doc):
    """A row made HOSTED_TESTED with the digest of what is on disk: correct
    until an edit breaks one of its rules."""
    r = next(x for x in doc["rows"] if x.get("implementation"))
    r["status"] = "HOSTED_TESTED"
    r["hosted"] = {"commit": "a" * 40, "runs": ["1"],
                   "implementation_sha256": RD.tree_digest(r)}
    return r


def test_control_a_correctly_bound_hosted_row_holds():
    doc = copy.deepcopy(RD.load())
    _hosted(doc)
    assert RD.problems(doc) == []


@pytest.mark.parametrize("edit, why", [
    (lambda d: _hosted(d).update(hosted={}), "names no hosted runs"),
    (lambda d: _hosted(d)["hosted"].update(commit="abc"),
     "names no hosted runs"),
    (lambda d: _hosted(d).update(implementation=[]),
     "names no implementation"),
    (lambda d: _hosted(d).update(implementation=["tools/nothing.py"]),
     "is not a file"),
    (lambda d: _hosted(d)["hosted"].update(implementation_sha256="0" * 64),
     "the evidence predates them"),
    (lambda d: _hosted(d).update(tests=["tests/nothing.py"]),
     "does not exist"),
    (lambda d: _hosted(d).update(tests=[]), "cites no test"),
    (lambda d: _row(d, "EXTERNALLY_BLOCKED").update(note="later"),
     "names no owner action"),
    (lambda d: _row(d, "DEPLOYMENT_REQUIRED").update(note="somebody"),
     "deployment contract"),
    (lambda d: _hosted(d).update(ledger="D-1999-001"),
     "not in the defect ledger"),
    (lambda d: _hosted(d).update(status="DONE"), "status"),
])
def test_each_rule_refuses_its_violation(edit, why):
    doc = copy.deepcopy(RD.load())
    edit(doc)
    assert any(why in p for p in RD.problems(doc)), RD.problems(doc)


def test_a_matrix_of_hosted_rows_alone_would_meet_the_contract():
    """CONTROL: the verdict can be met, so NOT_MET is a finding and not a
    constant."""
    doc = copy.deepcopy(RD.load())
    for r in doc["rows"]:
        r["status"] = "HOSTED_TESTED"
    assert RD.summary(doc)["contract_met"] is True


@pytest.mark.parametrize("status", ["IMPLEMENTED", "EXTERNALLY_BLOCKED",
                                    "UNVERIFIED"])
def test_one_open_row_keeps_the_contract_unmet(status):
    doc = copy.deepcopy(RD.load())
    for r in doc["rows"]:
        r["status"] = "HOSTED_TESTED"
    doc["rows"][0]["status"] = status
    assert RD.summary(doc)["contract_met"] is False


# ---- D-2026-154: a hosted row is bound to the code the run tested -------------

def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(root),
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    """A history of one commit: a ledger, an implementation, its test."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DEFECT_LEDGER.md").write_text("## D-1 a record\n")
    (tmp_path / "impl.py").write_text("RULE = 1\n")
    (tmp_path / "test_impl.py").write_text("def test(): pass\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "one")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                            capture_output=True, text=True).stdout.strip()
    doc = {"statuses": {s: "" for s in RD.STATUSES}, "rows": [
        {"id": "X-1", "status": "IMPLEMENTED", "ledger": "D-1", "note": "",
         "tests": ["test_impl.py"], "mutation_specs": [],
         "implementation": ["impl.py"]}]}
    return tmp_path, commit, doc


def _fetch(commit, *, conclusion="success"):
    def fetch(path):
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": [{"name": "j", "conclusion": "success"}]}
        return {"head_sha": commit, "status": "completed",
                "conclusion": conclusion, "name": "w", "event": "push"}
    return fetch


def test_control_a_row_attached_at_the_commit_it_was_tested_at_holds(repo):
    root, commit, doc = repo
    RD.attach(doc, ["X-1"], commit, ["7"], "w", fetch=_fetch(commit),
              root=root)
    assert doc["rows"][0]["status"] == "HOSTED_TESTED"
    assert RD.problems(doc, root) == []


def test_a_change_to_the_implementation_after_the_run_is_refused(repo):
    root, commit, doc = repo
    RD.attach(doc, ["X-1"], commit, ["7"], "w", fetch=_fetch(commit),
              root=root)
    (root / "impl.py").write_text("RULE = 2\n")
    assert any("the evidence predates them" in p
               for p in RD.problems(doc, root))


def test_the_digest_is_taken_from_the_commit_not_the_working_tree(repo):
    """Changed before attaching and never committed: the run did not test
    it, so the row must not hold over it."""
    root, commit, doc = repo
    (root / "impl.py").write_text("RULE = 2\n")
    RD.attach(doc, ["X-1"], commit, ["7"], "w", fetch=_fetch(commit),
              root=root)
    assert any("the evidence predates them" in p
               for p in RD.problems(doc, root))


@pytest.mark.parametrize("fetch_for", [
    lambda c: _fetch("f" * 40),
    lambda c: _fetch(c, conclusion="failure"),
])
def test_a_run_about_another_commit_or_not_green_is_refused(repo, fetch_for):
    root, commit, doc = repo
    with pytest.raises(AH.EvidenceRefused):
        RD.attach(doc, ["X-1"], commit, ["7"], "w",
                  fetch=fetch_for(commit), root=root)
    assert doc["rows"][0]["status"] == "IMPLEMENTED"


def test_a_row_without_implementation_files_cannot_be_attached(repo):
    root, commit, doc = repo
    doc["rows"][0]["implementation"] = []
    with pytest.raises(AH.EvidenceRefused):
        RD.attach(doc, ["X-1"], commit, ["7"], "w", fetch=_fetch(commit),
                  root=root)


def test_every_shipped_hosted_row_is_bound():
    for r in RD.load()["rows"]:
        if r["status"] == "HOSTED_TESTED":
            h = r["hosted"]
            assert all(x.isdigit() for x in h["runs"]), r["id"]
            assert len(h["implementation_sha256"]) == 64, r["id"]
