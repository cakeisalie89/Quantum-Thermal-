"""The production-readiness matrix is held to the tree, and says plainly
whether its contract is met (it is not while anything is blocked,
unverified or only locally tested)."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

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


@pytest.mark.parametrize("edit, why", [
    (lambda d: _row(d, "IMPLEMENTED").update(status="HOSTED_TESTED"),
     "names no hosted runs"),
    (lambda d: _row(d, "IMPLEMENTED").update(tests=["tests/nothing.py"]),
     "does not exist"),
    (lambda d: _row(d, "IMPLEMENTED").update(tests=[]), "cites no test"),
    (lambda d: _row(d, "EXTERNALLY_BLOCKED").update(note="later"),
     "names no owner action"),
    (lambda d: _row(d, "DEPLOYMENT_REQUIRED").update(note="somebody"),
     "deployment contract"),
    (lambda d: _row(d, "IMPLEMENTED").update(ledger="D-1999-001"),
     "not in the defect ledger"),
    (lambda d: _row(d, "IMPLEMENTED").update(status="DONE"), "status"),
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
