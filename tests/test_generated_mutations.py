"""The generated operator set, and the rules a generated mutation still obeys.

WHY THIS IS A SEPARATE FILE

tools/mutations/ is a set of mutations somebody wrote after deciding which
lines were enforcement points. That is its strength and its ceiling: a check
nobody thought of is a check nothing mutates, and the report says nothing
about it either way. tools/generated_mutations.py walks the AST and applies
operators with no idea what the code means, so its coverage does not depend
on anybody's imagination.

What it must NOT do is lower the bar. A generated mutation is subject to
exactly the rules a hand-written one is -- an anchor that matches once, a
replacement that changes something, and above all source that still PARSES,
because a mutation that does not parse is "killed" by the import failing and
tests nothing at all. This repository's ledger carries four hand-written
mutations in that state; a generator can produce them by the dozen.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOL = ROOT / "tools" / "generated_mutations.py"

#: Modules the generator is exercised against. Chosen for shape rather than
#: importance: one dense in comparisons and guards, one dense in constants.
SUBJECTS = ("qta_agent/capability.py", "qta_agent/scheduler.py",
            "qta_agent/policy.py")


def _tool():
    spec = importlib.util.spec_from_file_location("generated_mutations", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _tool()


@pytest.mark.parametrize("subject", SUBJECTS)
def test_the_generator_finds_real_work_in_every_subject(gen, subject):
    found = gen.candidates(ROOT / subject)
    assert len(found) >= gen.MIN_CANDIDATES, (
        f"{subject}: {len(found)} candidate(s); a sample this small is not "
        "a sample of that module")
    kinds = {m["name"].split("_")[1] for m in found}
    assert len(kinds) >= 3, (
        f"{subject}: every candidate came from {kinds}; one operator doing "
        "all the work is a generator that has stopped generating")


@pytest.mark.parametrize("subject", SUBJECTS)
def test_every_generated_mutation_obeys_the_harness_s_rules(gen, subject):
    """The same three static rules a hand-written spec is held to."""
    src = (ROOT / subject).read_text(encoding="utf-8")
    problems = []
    for m in gen.candidates(ROOT / subject):
        if src.count(m["find"]) != 1:
            problems.append(f"{m['name']}: anchor matches "
                            f"{src.count(m['find'])}x")
            continue
        mutated = src.replace(m["find"], m["replace"], 1)
        if mutated == src:
            problems.append(f"{m['name']}: replacement changes nothing")
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            problems.append(f"{m['name']}: does not parse ({exc.msg}); it "
                            "would be 'killed' by the import failing")
    assert not problems, problems[:8]


def test_every_generated_mutation_names_the_module_it_mutates(gen):
    for m in gen.candidates(ROOT / "qta_agent/policy.py"):
        assert m["path"] == "qta_agent/policy.py"
        assert m["rationale"].startswith("line "), m["rationale"]


def test_the_sample_is_seeded_so_a_finding_can_be_re_run(gen, tmp_path):
    def sample(seed):
        out = tmp_path / f"s{seed}.json"
        subprocess.run(
            [sys.executable, str(TOOL), "qta_agent/capability.py",
             "--sample", "12", "--seed", str(seed), "--emit", str(out)],
            cwd=str(ROOT), check=True, capture_output=True, timeout=300)
        return [m["name"] for m in
                json.loads(out.read_text(encoding="utf-8"))["mutations"]]

    assert sample(11) == sample(11)
    assert sample(11) != sample(12)


def test_a_module_with_nothing_to_mutate_is_a_failure_not_a_clean_sheet(
        tmp_path):
    """The anti-vacuity floor.

    A generator pointed at the wrong thing -- an empty file, a parse that
    silently returned nothing, an operator table that stopped matching --
    would otherwise report "0 mutations, all killed" and look like the best
    result in the file.
    """
    gen = _tool()
    empty = tmp_path / "nothing.py"
    empty.write_text('"""No branches, no comparisons."""\nX = "a"\n',
                     encoding="utf-8")
    assert len(gen.candidates(empty)) < gen.MIN_CANDIDATES

    # And the floor is the TOOL's, not this test's: pointed at a module with
    # nothing in it, it has to exit non-zero rather than print a clean sheet.
    thin = ROOT / "tools" / "_generated_floor_probe.py"
    thin.write_text('"""Nothing to mutate."""\nX = "a"\n', encoding="utf-8")
    try:
        out = subprocess.run(
            [sys.executable, str(TOOL), str(thin)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    finally:
        thin.unlink(missing_ok=True)
    assert out.returncode == 1, out.stdout
    assert "not a sample of that module" in out.stdout


def test_a_module_outside_the_repository_is_refused_with_the_reason(
        tmp_path):
    """A spec names a module by its repository-relative path.

    One that has no such path cannot be run by the harness at all, and
    saying so beats a ValueError three frames down about subpaths.
    """
    outside = tmp_path / "elsewhere.py"
    outside.write_text("X = 1\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(TOOL), str(outside)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert out.returncode != 0
    assert "outside" in out.stderr + out.stdout


def test_a_survivor_is_reported_for_triage_and_does_not_fail_the_run(gen):
    """A generated survivor is NOT automatically a defect.

    A generated set is full of genuinely equivalent mutations in a way a
    hand-written one is not -- a bound no caller reaches, a comparison on a
    value whose type makes both spellings identical. Failing on them would
    make the tool unusable within a day, so it reports them and fails on the
    things that are always wrong.
    """
    src = TOOL.read_text(encoding="utf-8")
    assert "does not fail a build on them" in src
    assert '"ANCHOR DRIFT"' in src or "ANCHOR DRIFT" in src
    assert "KILLED ONLY BY TIMEOUT" in src, (
        "a timeout is not a kill in the ordinary harness, and a generated "
        "run must not be the place that exception gets made")
