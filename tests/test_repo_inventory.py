"""The forensic inventory accounts for every baseline file, and says so truly.

``docs/repository_inventory.json`` classifies every file tracked at the
transformation baseline, records the dependencies measured from the
baseline's bytes, and states what became of each file. These tests hold the
document to the rules that produced it and to the tree it describes, and
show that each way of making it lie is caught.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import repo_inventory as RI  # noqa: E402


@pytest.fixture(scope="module")
def doc():
    return json.loads((ROOT / RI.OUT).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tree():
    return RI.current_tree()


def _baseline_available() -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e",
                        RI.BASELINE_COMMIT + "^{commit}"],
                       capture_output=True)
    return r.returncode == 0


def test_the_committed_inventory_describes_this_tree(doc, tree):
    assert RI.problems(doc, tree) == []


def test_every_baseline_file_is_classified_once(doc):
    paths = [f["path"] for f in doc["files"]]
    assert len(paths) == len(set(paths)) == doc["counts"]["files"]
    assert {f["class"] for f in doc["files"]} <= set(RI.CLASSES)
    for f in doc["files"]:
        assert f["rationale"].strip() and f["action"].strip(), f["path"]
        assert f["final_disposition"] in RI.DISPOSITIONS, f["path"]


def test_the_derivation_reproduces_the_committed_bytes():
    """``derive`` from the baseline commit's objects yields the committed
    document byte for byte. Needs full history; where the history is
    required (the hosted job sets the variable) its absence FAILS."""
    if not _baseline_available():
        if os.environ.get("HARNESS_INVENTORY_DERIVATION_REQUIRED") == "1":
            pytest.fail("the baseline commit is not in this clone; the job "
                        "that requires the derivation must fetch history")
        pytest.skip("baseline commit not in this (shallow) clone")
    assert RI.render(RI.derive()) == (ROOT / RI.OUT).read_text(
        encoding="utf-8")


def test_every_rule_classifies_something():
    """A rule no baseline file matches is dead text, and dead rules are how a
    classification drifts from the files it claims to describe."""
    if not _baseline_available():
        pytest.skip("baseline commit not in this (shallow) clone")
    listing = RI.baseline_listing()
    for path in RI.EXPLICIT:
        assert path in listing, f"EXPLICIT names {path}, not a baseline file"
    for path in RI.DESTINATIONS:
        assert path in listing, f"DESTINATIONS names {path}"
    for rx, cls, _why, _act in RI._PATTERNS:
        hits = [p for p in listing if p not in RI.EXPLICIT
                and RI.classify(p)[0] == cls and rx.search(p)]
        assert hits, f"pattern {rx.pattern!r} classifies nothing"


def test_the_measured_dependencies_are_real(doc):
    """ANTI-VACUITY for the extractor: edges known from the baseline bytes
    must be found, by each kind of evidence."""
    by = {f["path"]: f for f in doc["files"]}
    # a static import, relative form
    assert "scientific/models/thermal_1d.py" in \
        by["scientific/catalog.py"]["imports"]
    # an import of a submodule pulls its package in
    assert "qta_multiphysics/stack/__init__.py" in \
        by["scientific/_governed_run.py"]["imports"]
    # a workflow naming an entry point
    assert "tools/neural.py" in \
        by[".github/workflows/agent-substrate.yml"]["references"]
    # a Snakefile including another file
    assert "workflow/legacy_qta.smk" in by["Snakefile"]["references"]
    # a dotted module name in a dispatch table (a dynamic import)
    assert "qta_agent/_stage10_tool.py" in \
        by["qta_agent/governed_stage10.py"]["references"]
    # the reverse edges are the forward ones, inverted
    for f in doc["files"]:
        for q in f["imports"]:
            assert f["path"] in by[q]["imported_by"]
        for q in f["references"]:
            assert f["path"] in by[q]["referenced_by"]


def test_every_cut_dependency_is_recorded(doc):
    cuts = {(c["from"], c["to"]) for c in doc["dependency_cuts"]
            if c["kind"] == "import"}
    assert ("scientific/catalog.py", "scientific/models/thermal_1d.py") \
        in cuts
    assert doc["counts"]["cut_imports"] == len(cuts)


# ---- each way of making the document lie is caught ----------------------
def _first(doc, cls):
    return next(f for f in doc["files"] if f["class"] == cls)


@pytest.mark.parametrize("tamper,needle", [
    (lambda d: _first(d, "REMOVE").update({"class": "PRESERVE"}),
     "classification differs"),
    (lambda d: _first(d, "PRESERVE").update({"rationale": "kept"}),
     "classification differs"),
    (lambda d: _first(d, "EXTRACT_GENERIC").update({"destinations": []}),
     "destinations differ"),
    (lambda d: d["files"].pop(), "counts"),
    (lambda d: d["files"].append(copy.deepcopy(d["files"][0])),
     "listed twice"),
    (lambda d: d["files"].append({**copy.deepcopy(d["files"][0]),
                                  "path": "nowhere/new.py"}),
     "no rule classifies"),
    (lambda d: d.update({"baseline_commit": "0" * 40}), "baseline commit"),
    (lambda d: d["dependency_cuts"].pop(), "dependency_cuts"),
    (lambda d: _first(d, "REMOVE").update(
        {"final_disposition": "RETAINED"}), "the tree says"),
])
def test_a_tampered_inventory_is_refused(doc, tree, tamper, needle):
    bad = copy.deepcopy(doc)
    tamper(bad)
    found = RI.problems(bad, tree)
    assert any(needle in p for p in found), found


def test_a_removed_file_that_returns_is_no_longer_removed(doc, tree,
                                                          tmp_path):
    """The disposition is a fact about the tree: put a REMOVED file back and
    its record stops being true."""
    gone = [f for f in doc["files"] if f["final_disposition"] == "REMOVED"]
    if not gone:
        pytest.skip("nothing removed yet")
    f = gone[0]
    target = tmp_path / f["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("back\n")
    assert RI.final_disposition(f["path"], f["class"], f["destinations"],
                                f["blob"], tree | {f["path"]},
                                tmp_path) == "PENDING"


def test_applied_refuses_anything_pending(doc, tree):
    """``--applied`` is the transformation's completion condition: it
    refuses exactly the PENDING records, and only them."""
    found = RI.problems(doc, tree, applied=True)
    pending = [f for f in doc["files"] if f["final_disposition"] == "PENDING"]
    assert len([p for p in found if "not yet applied" in p]) == len(pending)
