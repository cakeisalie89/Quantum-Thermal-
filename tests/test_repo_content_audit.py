"""The current tree carries none of the retired programme, and saying so is
tested by putting it back.

``tools/repo_content_audit.py`` reads every tracked file. These tests hold
the committed tree to ZERO findings, then reintroduce, one at a time, each
kind of retired content -- a file under a retired root, a removed path, a
retired output under a new name's directory, an import of the old
namespace, a dynamic import of a retired module, a reader of the gate table,
the apparatus vocabulary in a document, an unclassified file, an abused
allowance, a protocol identifier the audit does not know -- into a copy of
the tree, and require the audit to find exactly that.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import repo_content_audit as A  # noqa: E402


@pytest.fixture(scope="module")
def clean():
    return A.audit()


def test_the_tree_is_clean(clean):
    assert clean["findings"] == [], clean["findings"][:20]
    assert clean["ok"] is True


def test_the_committed_report_is_current(clean):
    committed = (ROOT / A.OUT).read_text(encoding="utf-8")
    assert committed == A.render(clean)


def test_every_tracked_file_has_an_area(clean):
    assert sum(clean["by_area"].values()) == clean["files"]
    assert clean["files"] == len(A.tracked())


def test_every_allowance_is_used(clean):
    """An allowance nobody needs is how an exception outlives its reason."""
    for pattern, a in clean["allowances"].items():
        assert a["files_using"] > 0, pattern


def test_the_inventory_has_been_applied():
    """The forensic inventory's dispositions are facts about this tree, and
    none is still PENDING: every REMOVE is absent, every move landed."""
    import repo_inventory as RI
    doc = json.loads((ROOT / RI.OUT).read_text(encoding="utf-8"))
    assert RI.problems(doc, RI.current_tree(), applied=True) == []


# ---- deliberate reintroduction ------------------------------------------

@pytest.fixture
def copy(tmp_path):
    """The tracked tree, copied: what the audit reads, and a git index of
    its own so ``tracked()`` sees what a commit would."""
    dst = tmp_path / "repo"
    files = A.tracked(ROOT)
    for rel in files:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    subprocess.run(["git", "init", "-q", str(dst)], check=True)
    subprocess.run(["git", "-C", str(dst), "add", "-A"], check=True)
    return dst


def _add(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", rel], check=True)


def _found(root):
    return [(f["category"], f["path"]) for f in A.audit(root)["findings"]]


@pytest.mark.parametrize("rel,text,category", [
    # a file under a retired root
    ("qta_multiphysics/thermal_1d.py", "x = 1\n", "RETIRED_PATH"),
    ("attic/notes.md", "kept for later\n", "RETIRED_PATH"),
    # a removed path, put back
    ("GLOSSARY.md", "a glossary\n", "RETIRED_PATH"),
    # a retired output under a directory that is not retired
    ("docs/results_gate_table.csv", "gate,status\n", "RETIRED_PATH"),
    # the old namespace imported
    ("scientific/x_probe.py", "import qta_agent.store\n", "RETIRED_IMPORT"),
    # a retired module named for dynamic import
    ("tools/x_probe.py",
     "import importlib\nimportlib.import_module('qta_full_sim')\n",
     "RETIRED_IMPORT"),
    ("tools/x_builtin.py", "m = __import__('qta_full_sim')\n",
     "RETIRED_IMPORT"),
    # the bare root of a retired package, which no single inventory row names
    ("tools/x_root.py", "import qta_multiphysics\n", "RETIRED_IMPORT"),
    # a submodule of a retired package that no inventory row names
    ("tools/x_sub.py", "from qta_agent.brand_new import x\n",
     "RETIRED_IMPORT"),
    # an import inside a function is still a dependency
    ("scientific/x_lazy.py",
     "def f():\n    import qta_agent.store\n    return 1\n",
     "RETIRED_IMPORT"),
    # a status that reads the gate table
    ("tools/x_status.py",
     "rows = open('results_gate_table.csv').read()\n", "GATE_DEPENDENCY"),
    ("docs/x_status.json", '{"scientific_PASS_count": 0}\n',
     "GATE_DEPENDENCY"),
    # the apparatus vocabulary in a document
    ("docs/x_note.md", "the cryopanel saturates in mode B\n",
     "HARDWARE_TERM"),
    ("README_extra.md", "an NV-centre ODMR readout\n", "HARDWARE_TERM"),
    # a file nobody placed
    ("data/x.bin", "unclassified\n", "UNCLASSIFIED"),
])
def test_reintroduced_content_is_found(copy, rel, text, category):
    assert _found(copy) == []          # the copy is clean to begin with
    _add(copy, rel, text)
    found = _found(copy)
    assert (category, rel) in found, found


def test_a_new_identifier_in_the_old_style_is_found(copy):
    """The declared protocol identifiers are exact strings. A NEW one in the
    old namespace is not grandfathered by resemblance."""
    _add(copy, "agent_substrate/x_probe.py",
         'SCHEMA = "qta-agent/new-schema/v1"\n')
    assert ("HARDWARE_TERM", "agent_substrate/x_probe.py") in _found(copy)


def test_a_declared_identifier_is_not_a_finding(copy):
    _add(copy, "agent_substrate/x_probe.py",
         'DOMAIN = b"qta-agent/event-attestation/v1\\x00"\n')
    assert _found(copy) == []


def test_an_allowance_does_not_cover_a_neighbour(copy):
    """Allowances are by path: the same vocabulary one directory over is a
    finding."""
    _add(copy, "tests/fuzz_corpus_extra/finding.json",
         '{"traceback": "qta_multiphysics/x.py"}\n')
    assert ("HARDWARE_TERM",
            "tests/fuzz_corpus_extra/finding.json") in _found(copy)


def test_an_unused_allowance_is_reported(monkeypatch, copy):
    monkeypatch.setitem(A.ALLOWANCES, "docs/nothing-here-*.md",
                        "a reason that no longer applies")
    assert ("STALE_ALLOWANCE", "docs/nothing-here-*.md") in _found(copy)


def test_the_check_mode_refuses_a_stale_report(tmp_path, monkeypatch):
    """``--check`` compares the committed report with a fresh audit; a
    report that says less than the tree is refused."""
    report = tmp_path / "report.json"
    report.write_text("{}\n")
    monkeypatch.setattr(A, "OUT", report.relative_to(tmp_path))
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "audit", lambda: {"files": 0, "findings": [],
                                             "ok": True})
    assert A.main(["--check"]) == 1
