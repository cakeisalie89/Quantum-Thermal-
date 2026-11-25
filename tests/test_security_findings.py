"""The security findings register and the PR inventory are held to the tree.

docs/security/findings.json records each finding of the tranche F review:
the exact file and symbol, the precondition, the impact, how it was
reproduced, and a status. A register that names a file that does not exist,
a symbol nobody can find, or a FIXED finding with no regression test, is a
claim nobody can check -- these tests refuse each of those.

docs/security/pr17_inventory.json is derived by tools/pr_inventory.py and
must be what the generator produces from the tree as it is.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pr_inventory as PI  # noqa: E402

FINDINGS = ROOT / "docs" / "security" / "findings.json"


def _findings() -> list:
    return json.loads(FINDINGS.read_text(encoding="utf-8"))["findings"]


def test_every_finding_is_complete_and_has_a_known_status():
    doc = json.loads(FINDINGS.read_text(encoding="utf-8"))
    statuses = set(doc["statuses"])
    ids = [f["id"] for f in doc["findings"]]
    assert len(ids) == len(set(ids)), "a finding id is used twice"
    for f in doc["findings"]:
        for key in ("ledger", "priority", "area", "title", "files",
                    "precondition", "impact", "reproduction", "status"):
            assert f.get(key), f"{f['id']}: {key} is empty"
        assert f["status"] in statuses, f"{f['id']}: {f['status']}"
        assert f["priority"] in ("P0", "P1", "P2"), f["id"]


def test_every_finding_names_a_file_and_symbol_that_exist_here():
    """A finding about this tree must point at something in it. Findings
    about the default branch's files carry ref 'main' and are checked by the
    review that recorded them, not against this tree."""
    for f in _findings():
        for loc in f["files"]:
            if loc.get("ref", "HEAD") != "HEAD":
                continue
            path = ROOT / loc["path"]
            assert path.is_file(), f"{f['id']}: {loc['path']} is not tracked"
            text = path.read_text(encoding="utf-8")
            for sym in loc["symbols"]:
                leaf = sym.split(".")[-1]
                assert leaf in text, (
                    f"{f['id']}: {loc['path']} has no {sym!r}")


def test_a_fixed_finding_names_a_regression_test_that_exists():
    for f in _findings():
        if f["status"] != "FIXED":
            continue
        ref = f["reproduction"]["ref"]
        test_file = ROOT / ref.split("::")[0]
        assert f["reproduction"]["kind"] == "test", f["id"]
        assert test_file.is_file(), f"{f['id']}: {ref} does not exist"
        if "::" in ref:
            name = ref.split("::")[1].split("[")[0]
            assert f"def {name}(" in test_file.read_text(encoding="utf-8"), (
                f"{f['id']}: {ref} names no test")


def test_every_tracked_file_has_a_classification():
    for path in PI.tracked():
        sub, boundary, risk = PI.classify(path)
        assert risk in ("CRITICAL", "HIGH", "MEDIUM", "LOW"), path


def test_the_classifier_puts_authority_and_release_code_first():
    """The ordered rules are the classification; a general rule placed
    before a specific one would silently downgrade it."""
    assert PI.classify("agent_substrate/store.py")[2] == "CRITICAL"
    assert PI.classify("tools/supply_chain.py")[2] == "CRITICAL"
    assert PI.classify(".github/workflows/claude.yml")[0] == "ci-workflows"
    assert PI.classify("agent_substrate/scheduler.py")[2] == "HIGH"
    assert PI.classify("tests/test_supply_chain.py")[2] == "LOW"


def test_a_test_that_imports_a_module_by_from_import_covers_it(tmp_path):
    """The coverage column reads imports from the syntax tree: a text search
    for 'agent_substrate.learned_rules' misses 'from agent_substrate import
    learned_rules'."""
    mods = PI._imported_modules(
        "from agent_substrate import learned_rules, store\n"
        "import scientific.identity\n")
    assert {"agent_substrate.learned_rules", "agent_substrate.store",
            "scientific.identity"} <= mods


def test_the_pr_inventory_is_current():
    doc = json.loads(PI.OUT.read_text(encoding="utf-8"))
    sha = doc["default_branch_commit"]
    have = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e",
                           f"{sha}^{{tree}}"], capture_output=True)
    if have.returncode != 0:
        if os.environ.get("HARNESS_HISTORY_REQUIRED") == "1":
            pytest.fail(f"default-branch commit {sha} is not in this "
                        "checkout, and HARNESS_HISTORY_REQUIRED=1")
        pytest.skip("default-branch commit not fetched (shallow checkout)")
    assert PI.main(["check"]) == 0
