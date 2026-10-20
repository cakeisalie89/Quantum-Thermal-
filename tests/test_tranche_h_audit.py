"""The Tranche H Gate A capability matrix is held to the tree.

``docs/tranche_h/capability_matrix.json`` says, row by row, what exists, how
it is evidenced and what is missing. A row is a claim, and these tests
refuse each way one goes stale or vacuous:

* a state outside the vocabulary;
* a cited module, test or mutation specification that does not exist;
* an IMPLEMENTED row with no code or no test;
* an ABSENT row that cites code (then it is not absent);
* an owner decision that is not defined;
* an EXTERNALLY_BLOCKED row that names no one who could unblock it;
* a reproduced gap the audit does not show;
* a summary table that no longer counts what the matrix says.
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "tranche_h" / "capability_matrix.json"
AUDIT = ROOT / "docs" / "tranche_h" / "GATE_A_AUDIT.md"


def _doc() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def test_every_row_has_a_known_state_and_a_unique_id():
    doc = _doc()
    ids = [r["id"] for r in doc["rows"]]
    assert len(ids) == len(set(ids)), "a row id is used twice"
    for r in doc["rows"]:
        assert r["state"] in doc["states"], (r["id"], r["state"])
        for key in ("section", "component", "responsibility"):
            assert r[key], f"{r['id']}: {key} is empty"


def test_every_cited_path_exists():
    for r in _doc()["rows"]:
        cited = (r["modules"] + r["evidence"]["tests"]
                 + r["evidence"]["mutation_specs"])
        missing = [p for p in cited if not (ROOT / p).exists()]
        assert not missing, f"{r['id']} cites {missing}"


def test_an_implemented_row_names_its_code_and_its_tests():
    for r in _doc()["rows"]:
        if r["state"] == "IMPLEMENTED":
            assert r["modules"], f"{r['id']} is IMPLEMENTED and names no code"
            assert r["evidence"]["tests"], (
                f"{r['id']} is IMPLEMENTED and names no test")


def test_an_absent_row_cites_no_code():
    for r in _doc()["rows"]:
        if r["state"] == "ABSENT":
            assert not r["modules"], (
                f"{r['id']} is ABSENT and cites {r['modules']}: either the "
                "code exists and the row is PARTIAL, or it does not")


def test_every_owner_decision_is_defined():
    doc = _doc()
    for r in doc["rows"]:
        od = r["owner_decision"]
        assert od is None or od in doc["owner_decisions"], (r["id"], od)


def test_a_blocked_row_names_who_can_unblock_it():
    for r in _doc()["rows"]:
        if r["state"] == "EXTERNALLY_BLOCKED":
            assert r["owner_decision"] or any(
                re.search(r"\bO-\d", m) for m in r["missing_integration"]), (
                f"{r['id']} is blocked and names no owner decision or action")


def test_every_reproduced_gap_is_shown_in_the_audit():
    audit = AUDIT.read_text(encoding="utf-8")
    for r in _doc()["rows"]:
        for gap in re.findall(r"R\d+", r["reproduced_gap"] or ""):
            assert re.search(rf"^\| {gap} \|", audit, re.MULTILINE), (
                f"{r['id']} cites {gap}, which the audit does not show")


def test_the_audit_counts_what_the_matrix_says():
    counts = collections.Counter(r["state"] for r in _doc()["rows"])
    audit = AUDIT.read_text(encoding="utf-8")
    for state, n in counts.items():
        assert re.search(rf"^\| {state} \| {n} \|", audit, re.MULTILINE), (
            f"the audit does not say {n} rows are {state}")
    assert f"{sum(counts.values())} rows" in audit
