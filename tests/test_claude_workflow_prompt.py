"""The Claude workflow teaches the harness's rules.

``.github/workflows/claude.yml`` hands every @claude run a system prompt.
This holds the prompt to the harness's rules, and holds it -- and the
workflow's header -- to the same content audit as the rest of the tree.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "claude.yml"
sys.path.insert(0, str(ROOT / "tools"))

import repo_content_audit as RCA  # noqa: E402


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
    "content audit": "tools/repo_content_audit.py --check",
    "no reintroduction": "Do not reintroduce it",
    "keep useful methods": "Do not delete a useful numerical or scientific "
                           "method without an equivalent generic "
                           "implementation and its tests",
    "no merge": "do not merge",
    "no history rewrite": "do not rewrite published history",
    "no fabrication": "Never fabricate evidence",
}


def _retired_terms(text: str) -> list:
    scanned = RCA._strip_protocol(text)
    return sorted({m.group(0).lower() for _, rx in RCA._TERMS
                   for m in rx.finditer(scanned)})


def test_the_prompt_states_the_harness_rules():
    prompt = _prompt()
    missing = [k for k, phrase in REQUIRED.items() if phrase not in prompt]
    assert not missing, missing


def test_the_prompt_carries_none_of_the_retired_vocabulary():
    assert _retired_terms(_prompt()) == []


def test_the_scan_would_see_a_retired_term_in_a_prompt():
    """CONTROL: the check above reads the prompt with the audit's own
    patterns, so it is not vacuous."""
    planted = _prompt() + " " + RCA.RETIRED_NAMES[0]
    assert _retired_terms(planted)


def test_the_header_note_states_the_separation():
    text = WORKFLOW.read_text(encoding="utf-8")
    note = text[text.index("# GOVERNANCE NOTE."):text.index("name: ")]
    assert "certifies its own work" in note
    assert "tools/repo_content_audit.py" in note
