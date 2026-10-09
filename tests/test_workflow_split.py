"""The generic scientific workflow reaches no legacy QTA target (directive 16).

The Snakefile holds the generic workflow and its default target,
``scientific_generic``; ``workflow/legacy_qta.smk`` holds the hardware-era
pipeline -- the Stage-7 chain over ``qta_full_sim.py``, the machine-FSM and
hardware-governance suites, the gate table, the Stage-8 QTA payload -- kept
invocable explicitly. Which side a rule is on is where it is defined.

What is asserted is taken from Snakemake's own DAG (a dry run), not from
reading the rules: the default target and ``scientific_generic`` schedule no
rule defined in the legacy file, and none of the generic rules they schedule
names a legacy file or a legacy-ontology module. The control is the legacy
aggregate, whose DAG the same parse must see full of legacy rules -- so a
parse that saw nothing would fail here rather than pass everywhere.
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

import framework_boundary as FB  # noqa: E402

SNAKEFILE = ROOT / "Snakefile"
LEGACY = ROOT / "workflow" / "legacy_qta.smk"
_RULE = re.compile(r"^rule (\w+):", re.M)


def _rules(path: Path) -> set:
    return set(_RULE.findall(path.read_text(encoding="utf-8")))


def _blocks(path: Path) -> dict:
    """``{rule: its text}``, each block running to the next rule or the next
    top-level statement."""
    text = path.read_text(encoding="utf-8")
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


def test_every_rule_is_on_exactly_one_side():
    generic, legacy = _rules(SNAKEFILE), _rules(LEGACY)
    assert generic and legacy
    assert not generic & legacy, generic & legacy
    assert "scientific_generic" in generic and "legacy_qta" in legacy


def test_the_default_target_is_the_generic_workflow():
    assert dag(None) == dag("scientific_generic")
    assert {"scientific_generic", "s10_governed_model", "s10_governed",
            "s10_governed_index", "s10_canonical_untouched"} <= dag(None)


def test_the_generic_workflow_reaches_no_legacy_rule():
    reached = dag("scientific_generic") & _rules(LEGACY)
    assert not reached, sorted(reached)
    assert dag("scientific_generic") <= _rules(SNAKEFILE)


def test_no_generic_rule_it_runs_names_a_legacy_file_or_module():
    """The rules the generic DAG schedules, read for what they open: no
    legacy file (the gate table, qta_full_sim.py, the BOM, a hardware
    registry) and no legacy-ontology module (the machine FSM, hardware
    governance, the mode sequence)."""
    b = FB.Boundary()
    names = sorted(b.legacy_files)
    tokens = b.tokens
    blocks = _blocks(SNAKEFILE)
    found = {}
    for rule in dag("scientific_generic"):
        text = blocks[rule]
        hits = [n for n in names if n in text] + \
            [t for t in tokens if t in text]
        if hits:
            found[rule] = hits
    assert not found, found


def test_the_check_sees_legacy_rules_where_they_are():
    """Control: the legacy aggregate's DAG is the hardware-era pipeline, and
    the same parse and the same name scan find it."""
    legacy_dag = dag("legacy_qta")
    assert {"full_verification", "gate_table_validated",
            "invariants_validated", "canonical_outputs", "s8_report",
            "s10_report"} <= legacy_dag
    blocks = _blocks(LEGACY)
    assert "results_gate_table.csv" in blocks["gate_table_validated"]
    assert "machine_fsm" in blocks["invariants_validated"]


@pytest.mark.parametrize("target", ["full_verification", "s8_full",
                                    "s10_full"])
def test_the_legacy_targets_stay_invocable(target):
    """Directive 16: separated, not deleted."""
    assert target in dag(target)


def test_the_generic_default_target_runs_the_end_to_end_demonstration():
    """Section 28: the DAG represents current generic harness execution --
    proposal to decision -- and the rule doing it runs the demonstration
    tool, whose judge fails it unless the negative twin is refused: no
    vacuous target."""
    assert "harness_demo" in dag(None)
    block = _blocks(SNAKEFILE)["harness_demo"]
    assert "tools/harness_demo.py run" in block
    assert "directory(" in block, "the demo's workspace is a declared output"
    src = (ROOT / "tools" / "harness_demo.py").read_text(encoding="utf-8")
    assert "THE NEGATIVE TWIN WAS NOT REJECTED" in src
    assert "return 0 if rep[\"accepted\"] else 1" in src
