"""The Claude workflow teaches the framework's rules, not QTA's (directive 11).

``.github/workflows/claude.yml`` hands every @claude run a system prompt. It
used to state the QTA package's hardware-era invariants -- a PASS count of
zero, can_PASS_now=NO -- as the repository's purpose. This holds the prompt
to the framework's rules and keeps the retired ones out.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "claude.yml"


def _prompt() -> str:
    m = re.search(r'--system-prompt "([^"]*)"',
                  WORKFLOW.read_text(encoding="utf-8"))
    assert m, "the Claude workflow passes no system prompt"
    return " ".join(m.group(1).split())


REQUIRED = {
    "event-sourced authority": "authority is event-sourced",
    "presence is not authority": "presence in the log is not authority",
    "separation": "Proposal, execution and verification are separate",
    "no self-certification": "an AI cannot self-certify",
    "simulation is not measurement": "a simulation is not a measurement",
    "verification is not validation":
        "numerical verification (an independent implementation agreeing) "
        "is not experimental validation",
    "legacy boundary": "tools/framework_boundary.py --check",
    "keep useful methods": "Do not delete a useful numerical or scientific "
                           "method because its QTA wrapper is retired",
    "extract before deleting": "do not mass-delete hardware files before "
                               "their unique equations are extracted",
    "no merge": "do not merge",
    "no history rewrite": "do not rewrite published history",
    "no fabrication": "Never fabricate evidence",
}

RETIRED = ("PASS count", "can_PASS_now", "forecast-only", "Mode B",
           "Mode D", "stays zero")


def test_the_prompt_states_the_framework_rules():
    prompt = _prompt()
    missing = [k for k, phrase in REQUIRED.items() if phrase not in prompt]
    assert not missing, missing


def test_the_prompt_no_longer_teaches_the_retired_invariants():
    prompt = _prompt()
    assert [t for t in RETIRED if t in prompt] == []


def test_the_header_note_does_not_restate_them_as_current():
    text = WORKFLOW.read_text(encoding="utf-8")
    note = text[text.index("# GOVERNANCE NOTE."):text.index("name: ")]
    assert "measured_in_this_system=false, automatic_gate_effect=NONE" \
        not in note
    assert "certifies its own work" in note
