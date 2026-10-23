"""The workflow is one file, its default target is the harness's own path, and
it reaches nothing the repository retired.

The Snakefile is the only workflow definition: no ``include:``, no second
rule file. What its default target runs is taken from Snakemake's own DAG (a
dry run), not from reading the rules: ``scientific_generic`` schedules the
demonstration, the governed paths, the representations, the retrieval index
and the tree check, and no rule it schedules names a retired file or module.
The opt-in explorations stay out of the default DAG and stay invocable. The
name scan has its control: a rule naming a retired output is found by the
same scan.
"""
from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import repo_content_audit as RCA  # noqa: E402

SNAKEFILE = ROOT / "Snakefile"
_RULE = re.compile(r"^rule (\w+):", re.M)

DEFAULT_RULES = {"scientific_generic", "harness_demo", "governed_model",
                 "governed_tasks", "governed_index", "viz_vtk", "viz_usd",
                 "retrieval_index", "tree_untouched"}
OPT_IN = ("uq_sobol", "mdao_doe")


def _rules(text: str) -> set:
    return set(_RULE.findall(text))


def _blocks(text: str) -> dict:
    """``{rule: its text}``, each block running to the next rule or the next
    top-level statement."""
    out = {}
    for m in _RULE.finditer(text):
        rest = text[m.end():]
        stop = re.search(r"^\S", rest, re.M)
        out[m.group(1)] = rest[:stop.start()] if stop else rest
    return out


@functools.lru_cache(maxsize=None)
def dag(target: str | None) -> frozenset:
    """The rules a dry run of ``target`` (the default when None) schedules,
    read from Snakemake's job-stats table."""
    # the target goes first: --quiet takes several values and would take it
    cmd = [sys.executable, "-m", "snakemake"] + ([target] if target else []) \
        + ["-n", "--forceall", "--nolock", "--quiet", "rules"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    lines = r.stdout.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.split() == ["job", "count"])
    jobs = set()
    for line in lines[start + 2:]:
        parts = line.split()
        if not parts or parts[0] == "total":
            break
        jobs.add(parts[0])
    return frozenset(jobs)


def _retired_mentions(block: str) -> list:
    """Retired output names, retired roots and retired modules a rule's text
    names."""
    retired_modules = sorted(RCA.retired_from_inventory(ROOT)[1])
    hits = [n for n in RCA.RETIRED_NAMES if n in block]
    hits += [r for r in RCA.RETIRED_ROOTS if r in block]
    hits += [m for m in retired_modules if re.search(
        rf"(?<![\w.]){re.escape(m)}(?![\w])", block)]
    return hits


def test_the_snakefile_is_the_only_workflow_definition():
    text = SNAKEFILE.read_text(encoding="utf-8")
    assert not re.search(r"^\s*include\s*:", text, re.M), (
        "a second rule file is a second workflow nobody reviews with this one")
    assert not (ROOT / "workflow").exists()
    assert not list(ROOT.glob("*.smk"))


def test_the_default_target_is_the_generic_workflow():
    assert dag(None) == dag("scientific_generic")
    assert dag(None) == DEFAULT_RULES, sorted(dag(None) ^ DEFAULT_RULES)


@pytest.mark.parametrize("target", OPT_IN)
def test_the_opt_in_explorations_are_invocable_and_not_default(target):
    assert target in dag(target)
    assert target not in dag(None)


def test_every_rule_is_scheduled_by_some_target():
    """No dead rule: each is in the default DAG or is an opt-in target."""
    rules = _rules(SNAKEFILE.read_text(encoding="utf-8"))
    assert rules == DEFAULT_RULES | set(OPT_IN), sorted(
        rules ^ (DEFAULT_RULES | set(OPT_IN)))


def test_no_rule_names_a_retired_file_or_module():
    blocks = _blocks(SNAKEFILE.read_text(encoding="utf-8"))
    assert set(blocks) == DEFAULT_RULES | set(OPT_IN)
    found = {rule: hits for rule, text in blocks.items()
             if (hits := _retired_mentions(text))}
    assert not found, found


def test_every_rule_writes_only_under_the_workspace():
    text = SNAKEFILE.read_text(encoding="utf-8")
    for rule, block in _blocks(text).items():
        if rule == "scientific_generic":
            assert "output:" not in block, "the aggregate writes nothing"
            continue
        out = block.split("output:", 1)
        assert len(out) == 2, f"{rule} declares no output"
        section = re.split(r"^\s{4}(?:run|shell|input|params|log):",
                           out[1], maxsplit=1, flags=re.M)[0]
        code = "\n".join(line for line in section.splitlines()
                         if not line.lstrip().startswith("#"))
        paths = re.findall(r'f?"([^"]+)"', code)
        assert paths, f"{rule}: no output path parsed"
        for path in paths:
            assert path.startswith(("{W10}/", "verification/")), (rule, path)


def test_the_retired_name_scan_finds_a_planted_rule():
    """CONTROL: the scan above is not vacuous. A rule naming a retired output
    or a retired module is found."""
    # Built from the audit's own lists, so this file names nothing retired.
    name = RCA.RETIRED_NAMES[0]
    package = RCA.RETIRED_ROOTS[0].rstrip("/")
    planted = (f'\n    input: "{name}"\n    run:\n'
               f'        import {package}.solver\n')
    hits = _retired_mentions(planted)
    assert name in hits
    assert package in hits, hits


def test_the_generic_default_target_runs_the_end_to_end_demonstration():
    """The DAG represents current generic harness execution -- proposal to
    decision -- and the rule doing it runs the demonstration tool, whose
    judge fails it unless the negative twin is refused: no vacuous target."""
    assert "harness_demo" in dag(None)
    block = _blocks(SNAKEFILE.read_text(encoding="utf-8"))["harness_demo"]
    assert "tools/harness_demo.py run" in block
    assert "directory(" in block, "the demo's workspace is a declared output"
    src = (ROOT / "tools" / "harness_demo.py").read_text(encoding="utf-8")
    assert "THE NEGATIVE TWIN WAS NOT REJECTED" in src
    assert "return 0 if rep[\"accepted\"] else 1" in src
