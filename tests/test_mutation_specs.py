"""Every committed mutation spec, checked statically.

These lived in ``tests/test_mutation_harness.py``, which is the one suite of
``tools/mutations/mutation_harness.json``, and two of them read the anchors of
every committed spec in the files the specs name -- the harness's own file
among them. Each entry of that spec, applied, takes its own anchor out of
``tools/mutation_matrix.py``, so the anchor check and the no-op check failed
under every one of them, whatever it did. The harness reported the first
failure, so an entry no test of the harness's behaviour observes was reported
killed, and the spec could not show a survivor (AUD-088): H17 and H18 were
credited to the anchor check, which comes before the test that does observe
them. The null control could not see it: a comment appended to the file takes
no anchor away.

So they are here, in a file no spec runs, and the last test holds that. They
still run wherever the whole suite runs: they read the committed specs, not
the harness's behaviour, and a run of the harness does not need them.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tools" / "mutation_matrix.py"


# ---- every committed spec, checked statically -----------------------------
#
# The harness detects a drifted anchor at RUN time and exits non-zero, which
# is correct and expensive: the drift is found after the matrix has already
# spent its minutes, and only for the one spec that was run. Four mutations
# were sitting silently broken across the committed specs at once -- two whose
# anchor no longer matched anything, one that no longer parsed, and one that
# had become a no-op -- and each was testing NOTHING while the table still
# counted it.
#
# These checks are static and take about a second over all of them.

SPEC_DIR = ROOT / "tools" / "mutations"


def _specs():
    return sorted(SPEC_DIR.glob("*.json"))


def test_there_are_specs_to_check():
    """A glob that silently matches nothing would make every check below
    vacuously true."""
    assert len(_specs()) >= 10


@pytest.mark.parametrize("spec_path", _specs(), ids=lambda p: p.name)
def test_every_mutation_anchor_matches_exactly_once(spec_path):
    """An anchor matching zero times tests nothing; matching twice mutates
    somewhere nobody named."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    problems = []
    for m in spec["mutations"]:
        src = (ROOT / m["path"]).read_text(encoding="utf-8")
        n = src.count(m["find"])
        if n != 1:
            problems.append(f"{m['name']}: anchor matches {n}x in {m['path']}")
    assert not problems, problems


@pytest.mark.parametrize("spec_path", _specs(), ids=lambda p: p.name)
def test_no_mutation_is_a_no_op(spec_path):
    """A mutation identical to the source deletes no check, so the suite
    passes and the table records a kill that never happened."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    problems = []
    for m in spec["mutations"]:
        src = (ROOT / m["path"]).read_text(encoding="utf-8")
        if src.replace(m["find"], m["replace"], 1) == src:
            problems.append(f"{m['name']}: replacement changes nothing")
    assert not problems, problems


@pytest.mark.parametrize("spec_path", _specs(), ids=lambda p: p.name)
def test_every_mutated_source_still_parses(spec_path):
    """A mutation that does not compile fails every test for the wrong reason.

    It counts as a kill, and it proves only that Python rejected the file --
    nothing about whether the check it was meant to delete is load-bearing.
    """
    import ast

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    problems = []
    for m in spec["mutations"]:
        if not m["path"].endswith(".py"):
            continue
        src = (ROOT / m["path"]).read_text(encoding="utf-8")
        try:
            ast.parse(src.replace(m["find"], m["replace"], 1))
        except SyntaxError as exc:
            problems.append(f"{m['name']}: mutated {m['path']} does not "
                            f"parse: {exc.msg} (line {exc.lineno})")
    assert not problems, problems


@pytest.mark.parametrize("spec_path", _specs(), ids=lambda p: p.name)
def test_every_spec_names_suites_and_paths_that_exist(spec_path):
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    missing = [s for s in spec["suites"] if not (ROOT / s).is_file()]
    missing += [m["path"] for m in spec["mutations"]
                if not (ROOT / m["path"]).is_file()]
    assert not missing, missing


@pytest.mark.parametrize("spec_path", _specs(), ids=lambda p: p.name)
def test_every_mutation_states_why_it_matters(spec_path):
    """The rationale is what a survivor's report says. A missing or empty one
    turns a finding into a name."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    thin = [m["name"] for m in spec["mutations"]
            if len(m.get("rationale", "").strip()) < 20]
    assert not thin, thin


@pytest.mark.parametrize("spec_path", _specs(), ids=lambda p: p.name)
def test_every_committed_spec_passes_the_harness_own_validation(spec_path):
    """The harness refuses a malformed spec at run time -- on a CI shard,
    after the push. A committed spec with an empty replacement reached a
    local run that way (NF-1T closure); the same rule is applied here, so it
    fails before the commit instead."""
    spec = importlib.util.spec_from_file_location("mm_validate", HARNESS)
    mm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mm)
    problems = mm.validate_spec(json.loads(spec_path.read_text(
        encoding="utf-8")))
    assert problems == [], problems


def test_no_spec_runs_these_checks():
    """AUD-088. A spec whose suites held this file would see every one of
    its entries killed here, by the entry's own anchor going missing."""
    me = Path(__file__).resolve().relative_to(ROOT).as_posix()
    holding = [p.name for p in _specs()
               if me in json.loads(p.read_text(encoding="utf-8"))["suites"]]
    assert not holding, holding
