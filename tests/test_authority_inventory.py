"""The authority inventory: every durable write found, classified, decided.

tools/authority_inventory.py measures which functions in agent_substrate
write which actions, and how; docs/security/authority_inventory.json
classifies each. These tests hold the shipped pair to the rule, and hold the
rule to each of its parts with a synthetic package: a negative that must
fail and a control that must pass.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import authority_inventory as AI  # noqa: E402


def _shipped() -> dict:
    return json.loads(AI.INVENTORY.read_text(encoding="utf-8"))


def test_the_shipped_inventory_agrees_with_the_code_and_its_rule_holds():
    found = AI.scan()
    assert len(found) >= 40, "the walk is finding almost nothing"
    assert AI.problems(_shipped(), found,
                       AI._open_findings(AI.FINDINGS)) == []


def test_every_writer_converted_by_d_2026_156_is_measured_decided():
    """The baseline column is informational; this is the measurement."""
    measured = {(r["module"], r["function"], r["action"]): r["mode"]
                for r in AI.scan()}
    converted = [e for e in _shipped()["writes"] if "at_baseline" in e]
    assert len(converted) >= 9
    for e in converted:
        k = (e["module"], e["function"], e["action"])
        assert measured[k] == AI.DECIDED, k


# ---- the scanner ------------------------------------------------------------

WRITERS = '''
ACT_A = "a"
ACT_B = "b"
ACT_C = "c"
ACT_D = "d"
ACT_E = "e"

class W:
    def plain(self):
        self.log.append(actor="x", action=ACT_A, target="t", payload={})

    def decided(self):
        def decide(head_seq):
            return {"actor": "x", "action": ACT_B, "target": "t"}
        self.log.append_decided(decide)

    def decided_by_dict_call(self):
        def decide(head_seq):
            return dict(actor="x", action=ACT_C, target="t")
        self._append_decided(decide)

    def through_a_helper(self):
        self.log.append(**self._entry())

    def _entry(self):
        return {"actor": "x", "action": ACT_D, "target": "t"}

    def calls_another_writer(self):
        def decide(head_seq):
            return {"actor": "x", "action": ACT_E, "target": "t"}
        self.log.append_decided(decide)
        self.plain()

    def _append_decided(self, decide):
        return self.log.append_decided(decide)
'''


@pytest.fixture()
def pkg(tmp_path):
    (tmp_path / "w.py").write_text(WRITERS, encoding="utf-8")
    return tmp_path


def _found(pkg):
    return {(r["function"], r["action"]): r["mode"] for r in AI.scan(pkg)}


def test_the_walk_finds_each_way_a_record_is_written(pkg):
    assert _found(pkg) == {
        ("W.plain", "ACT_A"): AI.APPEND,
        ("W.decided", "ACT_B"): AI.DECIDED,
        ("W.decided_by_dict_call", "ACT_C"): AI.DECIDED,
        ("W.through_a_helper", "ACT_D"): AI.APPEND,
        ("W.calls_another_writer", "ACT_E"): AI.DECIDED,
    }


def test_a_writer_is_not_credited_with_the_action_of_one_it_calls(pkg):
    """The first version followed every self.method() call, and credited
    the governed runner's attempt with the task transition its _move
    writes."""
    assert ("W.calls_another_writer", "ACT_A") not in _found(pkg)


def test_a_writer_that_both_decides_and_appends_one_action_is_an_append(
        tmp_path):
    (tmp_path / "w.py").write_text('''
class W:
    def both(self):
        self.log.append(actor="x", action=ACT_A, target="t")
        def decide(head_seq):
            return {"actor": "x", "action": ACT_A, "target": "t"}
        self.log.append_decided(decide)
''', encoding="utf-8")
    assert _found(tmp_path) == {("W.both", "ACT_A"): AI.APPEND}


# ---- the rule ---------------------------------------------------------------

def _doc(pkg, **over):
    writes = []
    for r in AI.scan(pkg):
        e = dict(r, **{"class": "FACT", "why": "a test fixture"})
        writes.append(e)
    doc = {"schema": AI.SCHEMA, "writes": writes}
    for k, v in over.items():
        doc[k] = v
    return doc


def _entry(doc, function):
    return next(e for e in doc["writes"] if e["function"] == function)


def test_control_a_classified_tree_passes(pkg):
    assert AI.problems(_doc(pkg), AI.scan(pkg), {}) == []


def test_a_decision_written_outside_the_lock_fails(pkg):
    doc = _doc(pkg)
    _entry(doc, "W.plain")["class"] = "DECISION"
    bad = AI.problems(doc, AI.scan(pkg), {})
    assert any("outside append_decided and no OPEN finding" in p
               for p in bad), bad


def test_a_use_written_outside_the_lock_fails(pkg):
    doc = _doc(pkg)
    _entry(doc, "W.through_a_helper")["class"] = "USE"
    assert AI.problems(doc, AI.scan(pkg), {})


def test_control_a_decision_written_inside_the_lock_passes(pkg):
    doc = _doc(pkg)
    _entry(doc, "W.decided")["class"] = "DECISION"
    assert AI.problems(doc, AI.scan(pkg), {}) == []


def test_control_an_open_finding_may_name_an_undecided_decision(pkg):
    doc = _doc(pkg)
    e = _entry(doc, "W.plain")
    e["class"], e["open_finding"] = "DECISION", "T-9"
    assert AI.problems(doc, AI.scan(pkg), {"T-9": "OPEN"}) == []


@pytest.mark.parametrize("status", ["FIXED", "ACCEPTED_LIMITATION", None])
def test_a_finding_that_is_not_open_excuses_nothing(pkg, status):
    doc = _doc(pkg)
    e = _entry(doc, "W.plain")
    e["class"], e["open_finding"] = "DECISION", "T-9"
    findings = {} if status is None else {"T-9": status}
    bad = AI.problems(doc, AI.scan(pkg), findings)
    assert any("not OPEN" in p for p in bad), bad


def test_an_unclassified_write_fails(pkg):
    doc = _doc(pkg)
    doc["writes"] = [e for e in doc["writes"] if e["function"] != "W.plain"]
    bad = AI.problems(doc, AI.scan(pkg), {})
    assert any("is not classified" in p for p in bad), bad


def test_an_entry_for_a_write_that_no_longer_exists_fails(pkg):
    doc = _doc(pkg)
    ghost = copy.deepcopy(_entry(doc, "W.plain"))
    ghost["function"] = "W.removed"
    doc["writes"].append(ghost)
    bad = AI.problems(doc, AI.scan(pkg), {})
    assert any("no such write exists" in p for p in bad), bad


def test_a_recorded_mode_the_code_does_not_have_fails(pkg):
    doc = _doc(pkg)
    _entry(doc, "W.plain")["mode"] = AI.DECIDED
    bad = AI.problems(doc, AI.scan(pkg), {})
    assert any("measured as 'APPEND'" in p for p in bad), bad


def test_an_entry_classified_twice_fails(pkg):
    doc = _doc(pkg)
    doc["writes"].append(copy.deepcopy(_entry(doc, "W.plain")))
    bad = AI.problems(doc, AI.scan(pkg), {})
    assert any("classified twice" in p for p in bad), bad


def test_an_unknown_class_fails(pkg):
    doc = _doc(pkg)
    _entry(doc, "W.plain")["class"] = "HARMLESS"
    bad = AI.problems(doc, AI.scan(pkg), {})
    assert any("is not one of" in p for p in bad), bad


def test_a_class_with_no_reason_fails(pkg):
    doc = _doc(pkg)
    _entry(doc, "W.plain")["why"] = "  "
    bad = AI.problems(doc, AI.scan(pkg), {})
    assert any("no reason given" in p for p in bad), bad


def test_a_wrong_schema_fails(pkg):
    bad = AI.problems(_doc(pkg, schema="something/1"), AI.scan(pkg), {})
    assert any("schema" in p for p in bad), bad


def test_the_cli_check_passes_on_the_shipped_tree(capsys):
    assert AI.main(["check"]) == 0
    assert "0 undecided under an OPEN finding" in capsys.readouterr().out
