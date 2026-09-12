"""Two questions this repository kept answering wrongly, asked in one place.

WHAT FILES ARE IN THE REPOSITORY  -- three red pushes came from asking git
grep, which sees tracked files only, so a file one commit away from
existing was invisible to every structural guard.

WHAT DOES CI ACTUALLY RUN  -- the matrix makes claims about hosted
coverage, those claims live in YAML, and nothing local notices YAML
changing. Delete the full-suite job and every local check still passes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import repo_scope as RS  # noqa: E402
import workflow_contract as WC  # noqa: E402


# --- repository scope -------------------------------------------------------

def test_a_file_one_commit_away_from_existing_is_in_scope():
    """THE defect. An untracked, unignored file will be in the next commit."""
    probe = ROOT / "tools" / "_scope_probe_a.py"
    assert not probe.exists()
    probe.write_text("# probe\n")
    try:
        assert probe.relative_to(ROOT).as_posix() in RS.repository_files("*.py")
    finally:
        probe.unlink()


def test_an_ignored_file_is_not_in_scope():
    """The invariant the tracked-only version was protecting.

    A quarantined mutation copy cannot make the import graph wrong, and
    sweeping it in would fail these guards for reasons unrelated to code.
    """
    d = ROOT / ".mutation-quarantine" / "_scope_probe"
    d.mkdir(parents=True, exist_ok=True)
    probe = d / "b.py"
    probe.write_text("import qta_agent.store  # probe\n")
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q",
                            str(probe)], capture_output=True)
        assert r.returncode == 0, "the probe is not actually ignored"
        assert probe.relative_to(ROOT).as_posix() not in \
            RS.repository_files("*.py")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_content_search_and_file_set_agree():
    """files_matching reads in Python rather than shelling to git grep.

    Two tools with two ideas of which files exist is the disagreement this
    module exists to remove.

    The marker is ASSEMBLED rather than written literally: a literal would
    appear in this test file, which is itself in the repository, so the
    search would find two files and the assertion would be about the test
    rather than about the probe.
    """
    marker = "MARKER" + "_" + "a1b2c3d4"
    probe = ROOT / "tools" / "_scope_probe_c.py"
    probe.write_text(f"{marker} = 1\n")
    try:
        hits = RS.files_matching(marker)
        assert hits == (probe.relative_to(ROOT).as_posix(),), hits
    finally:
        probe.unlink()


def test_non_test_references_excludes_tests_and_finds_production():
    """The 'does this defence have a caller that is not its own test'
    question, which this project has had to ask three times."""
    prod = RS.non_test_references("check_egress_composition")
    assert "qta_agent/netauth.py" in prod, (
        "the production caller added for the confused-deputy fix is gone")
    assert not [f for f in prod if f.startswith("tests/")]


def test_expired_leases_now_has_a_production_caller():
    """It had ZERO callers -- not even a test -- while being documented as
    the scheduler's input for returning stranded work to the queue. That is
    why a crashed supervisor stranded a task forever."""
    prod = RS.non_test_references("expired_leases")
    assert "qta_agent/governed_stage10.py" in prod


def test_the_scope_helper_skips_the_retired_corpus():
    """attic/ is TRACKED, so git lists it and only _SKIP drops it.

    Picking .venv for this would have proved nothing: git already ignores
    it, so --exclude-standard drops it before _SKIP is consulted, and a
    mutation deleting _SKIP entirely still passed. attic/ is the entry that
    does the work.
    """
    tracked_attic = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", "attic/*"],
        capture_output=True, text=True).stdout.split()
    assert tracked_attic, "premise: attic/ holds tracked files"
    # At the "*.py" pattern attic happens to contribute nothing, so the skip
    # does no work there and a mutation deleting it would survive. The
    # pattern where it bites is the general one.
    scanned = RS.repository_files("*")
    assert not [f for f in scanned if f.startswith("attic/")], (
        "the retired corpus is being scanned as if it were live code")
    assert not [f for f in scanned if f.startswith(".venv/")]


def test_a_symbol_with_regex_metacharacters_is_searched_literally():
    """non_test_references takes a SYMBOL, not a pattern.

    Unescaped, a name like ``run(`` searches for a group that matches
    something else entirely, and a caller that is missing reads as present
    -- which is the direction that matters for a guard whose whole job is
    to notice an absent caller.
    """
    probe = ROOT / "tools" / "_scope_probe_d.py"
    literal = "zz" + "_probe(x)"
    probe.write_text(f"# {literal}\n")
    try:
        assert RS.non_test_references(literal) == (
            probe.relative_to(ROOT).as_posix(),)
        # The same string treated as a pattern matches a DIFFERENT thing:
        # "zz_probe(x)" as a regex is "zz_probe" followed by a group "x".
        assert RS.files_matching("zz" + "_probex") == ()
    finally:
        probe.unlink()


# --- workflow contract ------------------------------------------------------

def test_the_live_workflow_satisfies_its_contract():
    assert WC.problems() == ()


def test_a_missing_job_is_reported():
    body = WC.AGENT_WF.read_text(encoding="utf-8").replace(
        "\n  full-suite:\n", "\n  removed-suite:\n")
    assert any("full-suite" in x for x in WC.missing_jobs(body))


def test_a_removed_command_is_reported():
    body = WC.AGENT_WF.read_text(encoding="utf-8").replace(
        "uv run python -m pytest tests/ -q", "echo skipped")
    assert any("pytest tests/" in x for x in WC.missing_commands(body))


def test_a_mutation_spec_that_no_step_runs_is_reported():
    """Adding a matrix, running it locally and never wiring it in.

    It then protects nothing on any push, and the row citing it is claiming
    hosted coverage it does not have.
    """
    body = WC.AGENT_WF.read_text(encoding="utf-8").replace(
        "tools/mutations/agent_recovery.json", "tools/mutations/nothing.json")
    assert "tools/mutations/agent_recovery.json" in \
        WC.unrun_mutation_specs(body)


def test_every_mutation_spec_on_disk_runs_in_ci():
    assert WC.unrun_mutation_specs() == ()


def test_an_unpinned_action_is_reported():
    body = WC.AGENT_WF.read_text(encoding="utf-8").replace(
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "actions/checkout@v4")
    assert "actions/checkout@v4" in WC.uses_unpinned_actions(body)


def test_the_workflows_own_comment_is_not_reported_as_an_action():
    """A guard whose first finding is its own documentation is a guard
    nobody will believe. The header says "every `uses:` below is an
    immutable 40-hex commit object", and a loose pattern flagged it."""
    assert WC.uses_unpinned_actions() == ()


@pytest.mark.parametrize("job", sorted(WC.REQUIRED_JOBS))
def test_each_required_job_is_present_with_its_reason(job):
    assert job not in " ".join(WC.missing_jobs())
    assert WC.REQUIRED_JOBS[job]
