"""The framework's claims boundary is held by code that exists (directive 14).

``tools/claims_enforcement.py`` reads ``docs/claims_boundary.json`` and
``CLAIMS_BOUNDARY.md``. Here: the repository holds it, and each rule the
checker applies is shown catching the thing it exists for -- a required
boundary dropped, code or a test that does not exist, a boundary the page does
not state.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import claims_enforcement as CE  # noqa: E402

REG = json.loads((ROOT / "docs" / "claims_boundary.json").read_text(
    encoding="utf-8"))
DOC = (ROOT / "CLAIMS_BOUNDARY.md").read_text(encoding="utf-8")


def test_the_repository_holds_its_claims_boundary():
    assert CE.problems() == []
    assert CE.main([]) == 0


def test_every_required_boundary_is_registered():
    # 11 from directive 14; 14 since NF-1T (EB12-EB14: what a learned
    # prediction and a counted, abstractly validated architecture are not).
    assert len(REG["boundaries"]) >= len(CE.REQUIRED) == 14


@pytest.mark.parametrize("bid", [e["id"] for e in REG["boundaries"]])
def test_dropping_a_boundary_is_a_finding(bid):
    reg = copy.deepcopy(REG)
    reg["boundaries"] = [e for e in reg["boundaries"] if e["id"] != bid]
    assert any(p.startswith("REQUIRED:") for p in CE.problems(reg, DOC))


@pytest.mark.parametrize("statement", [
    "A simulation result is a measurement.",
    "A measurement is not a simulation result.",
    "A simulation result is not a surrogate prediction.",
    "A simulation result, not a measurement.",
])
def test_a_boundary_stated_otherwise_is_not_that_boundary(statement):
    """Inverted, reversed, about something else, or no negation at all:
    each names the words and states a different boundary, or none."""
    reg = copy.deepcopy(REG)
    reg["boundaries"][0]["statement"] = statement
    found = CE.problems(reg, DOC + "\n" + statement + "\n")
    assert ("REQUIRED: no boundary says a simulation result is not a "
            "measurement") in found, found


def test_code_that_does_not_define_the_symbol_is_a_finding():
    reg = copy.deepcopy(REG)
    reg["boundaries"][0]["enforced_in"][0]["symbol"] = "NO_SUCH_SYMBOL"
    found = CE.problems(reg, DOC)
    assert any("defines no 'NO_SUCH_SYMBOL'" in p for p in found), found


def test_a_module_that_does_not_exist_is_a_finding():
    reg = copy.deepcopy(REG)
    reg["boundaries"][0]["enforced_in"][0]["module"] = "scientific/nowhere.py"
    assert any("does not exist" in p for p in CE.problems(reg, DOC))


def test_a_test_that_does_not_exist_is_a_finding():
    reg = copy.deepcopy(REG)
    reg["boundaries"][1]["tests"] = [
        "tests/test_scientific_interfaces.py::test_nothing_like_this"]
    found = CE.problems(reg, DOC)
    assert any("test_nothing_like_this does not exist" in p
               for p in found), found


def test_a_boundary_with_no_test_or_no_code_is_a_finding():
    reg = copy.deepcopy(REG)
    reg["boundaries"][2]["tests"] = []
    reg["boundaries"][3]["enforced_in"] = []
    found = CE.problems(reg, DOC)
    assert any("names no test" in p for p in found)
    assert any("names no code" in p for p in found)


def test_a_boundary_the_page_does_not_state_is_a_finding():
    statement = REG["boundaries"][4]["statement"]
    found = CE.problems(REG, DOC.replace(statement, "(removed)"))
    assert any("does not state" in p for p in found), found


def test_an_empty_registry_enforces_nothing():
    assert CE.problems({"boundaries": []}, DOC) == [
        "the registry holds no boundaries; nothing is enforced"]


def test_the_cli_fails_when_the_boundary_is_not_held(monkeypatch, capsys):
    monkeypatch.setattr(CE, "problems", lambda: ["EB1: planted"])
    assert CE.main([]) == 1
    assert "claims boundary NOT held" in capsys.readouterr().out


def test_a_boundary_registered_twice_is_a_finding():
    reg = copy.deepcopy(REG)
    reg["boundaries"].append(copy.deepcopy(reg["boundaries"][0]))
    found = CE.problems(reg, DOC)
    assert f"{REG['boundaries'][0]['id']}: registered twice" in found, found


@pytest.mark.parametrize("source,defined", [
    ("def SYM():\n    pass\n", True),
    ("class SYM:\n    pass\n", True),
    ("SYM = frozenset()\n", True),
    ("SYM: frozenset = frozenset()\n", True),
    ("def other():\n    SYM = 1\n    return SYM\n", False),
    ("class Other:\n    def SYM(self):\n        pass\n", False),
    ("# SYM\nOTHER = 'SYM'\n", False),
])
def test_a_symbol_is_defined_only_at_module_level(tmp_path, source,
                                                   defined):
    """Enforcing code is code the module offers: a name bound only inside a
    function, a method, or mentioned in a comment or a string enforces
    nothing."""
    mod = tmp_path / "m.py"
    mod.write_text(source, encoding="utf-8")
    assert CE._defined(mod, "SYM") is defined


def test_the_hardware_era_claims_are_kept_as_legacy():
    """Directive 13 and 14: the QTA statements are not erased; they are the
    legacy verifier's, in a document that says it is history."""
    legacy = ROOT / "docs" / "legacy" / "qta" / "CLAIMS_BOUNDARY.md"
    assert legacy.is_file()
    assert "Mode B" in legacy.read_text(encoding="utf-8")
    assert "Mode B" not in DOC.split("## What is out of scope")[0]
