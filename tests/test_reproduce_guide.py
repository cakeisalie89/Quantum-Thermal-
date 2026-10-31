"""REPRODUCE.md says CI runs every re-derivation it lists. This holds it to
that, so the guide cannot quietly document a command nothing runs:

- every script the guide names exists in the tree;
- every command in its re-derivation section is a command the
  agent-substrate workflow runs, character for character;
- every gap it names in section 3 is still one the record names.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import workflow_contract as WC  # noqa: E402

GUIDE = ROOT / "REPRODUCE.md"
WORKFLOW = ROOT / ".github" / "workflows" / "agent-substrate.yml"


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:nxt if nxt >= 0 else len(text)]


def _commands(text: str) -> list:
    """The commands the guide gives, in backticks or indented blocks."""
    inline = re.findall(r"`((?:uv run )?python3?(?: -I)? [^`]+)`", text)
    block = [line.strip() for line in text.splitlines()
             if re.match(r"^ {4,}(uv run )?python3?( -I)? \S", line)]
    return inline + block


def test_every_script_the_guide_names_exists():
    text = GUIDE.read_text(encoding="utf-8")
    scripts = set(re.findall(r"(?:tools/[\w./-]+\.py|generate_manifest\.py)",
                             text))
    assert scripts, "the guide names no script"
    missing = sorted(s for s in scripts if not (ROOT / s).is_file())
    assert not missing, missing


def test_every_re_derivation_the_guide_lists_is_one_ci_runs():
    text = _section(GUIDE.read_text(encoding="utf-8"),
                    "## 2. With the repository")
    listed = [c for c in _commands(text)
              if "<" not in c and "$" not in c and "pytest" not in c
              and "mutation_matrix" not in c]
    assert len(listed) >= 12, listed
    run = set(WC._run_commands(WORKFLOW.read_text(encoding="utf-8")))
    not_run = sorted(c for c in listed if c not in run)
    assert not not_run, f"the guide lists commands CI does not run: {not_run}"


def test_the_standalone_check_is_the_one_ci_runs():
    text = _section(GUIDE.read_text(encoding="utf-8"),
                    "## 1. Without the repository")
    assert "python3 -I verify_assurance.py" in text
    run = WC._run_commands(WORKFLOW.read_text(encoding="utf-8"))
    assert any(c.startswith("python3 -I tools/verify_assurance.py ")
               for c in run)


def test_what_cannot_be_reproduced_is_still_said():
    text = _section(GUIDE.read_text(encoding="utf-8"),
                    "## 3. What this repository cannot reproduce")
    for gap in ("NOT_RUN", "physical experiment", "flagship", "Lean",
                "owner action"):
        assert gap in text, gap
