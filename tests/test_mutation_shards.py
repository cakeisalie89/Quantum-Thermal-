"""The mutation matrices run in shards, and nothing is lost by it.

Each property the sharding claims in tools/mutation_shards.py is checked here
against the real plan and the real workflow, and each workflow rule against a
copy with exactly that rule broken, so a check that could not fail would show
up as a test that cannot fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import mutation_shards as MS  # noqa: E402
import workflow_contract as WC  # noqa: E402

WF = MS.WORKFLOW.read_text(encoding="utf-8")


# --- the plan ----------------------------------------------------------------

def test_every_spec_is_in_exactly_one_shard():
    flat = [s for shard in MS.plan() for s in shard]
    assert sorted(flat) == MS.specs()
    assert len(flat) == len(set(flat))
    assert len(MS.specs()) > 40


def test_there_are_as_many_shards_as_declared_and_none_is_empty():
    shards = MS.plan()
    assert len(shards) == MS.SHARDS
    assert all(shards)


def test_the_plan_is_deterministic_and_order_free():
    names = MS.specs()
    assert MS.plan(names) == MS.plan(list(reversed(names))) == MS.plan()


def test_the_weights_are_the_measured_ones_where_measured():
    w = MS.weights(MS.specs())
    for name, secs in MS.WEIGHTS.items():
        assert w[name] == secs
    assert set(MS.WEIGHTS) <= set(MS.specs()), (
        "a weight for a spec that no longer exists")


def test_an_unmeasured_spec_is_weighed_not_ignored():
    unmeasured = [s for s in MS.specs() if s not in MS.WEIGHTS]
    assert unmeasured, "every spec measured; this test has nothing to see"
    w = MS.weights(MS.specs())
    assert all(w[s] > 0 for s in unmeasured)


def test_the_plan_is_balanced_within_the_greedy_bound():
    """LPT is within 4/3 of optimal, and optimal is at least the larger of
    the heaviest spec and the mean. A plan outside that is not LPT."""
    w = MS.weights(MS.specs())
    loads = [sum(w[s] for s in shard) for shard in MS.plan()]
    floor = max(max(w.values()), sum(w.values()) / MS.SHARDS)
    assert max(loads) <= 4 / 3 * floor


# --- the workflow ------------------------------------------------------------

def test_the_committed_workflow_matches_the_plan():
    assert MS.check_workflow(WF) == []


def test_every_spec_is_named_in_the_workflow_matrix():
    block = WF[WF.index(MS.BEGIN):WF.index(MS.END)]
    for s in MS.specs():
        assert f"tools/mutations/{s}" in block


def _drop_line(text, needle):
    lines = text.splitlines(keepends=True)
    hit = [i for i, line in enumerate(lines) if needle in line]
    assert len(hit) == 1, (needle, len(hit))
    del lines[hit[0]]
    return "".join(lines)


def _sub(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new)


@pytest.mark.parametrize("breaks,expect", [
    (lambda t: _drop_line(t, "tools/mutations/agent_tasks.json"),
     "not what tools/mutation_shards.py --emit generates"),
    (lambda t: _sub(t, "fail-fast: false\n      matrix:\n        include:\n"
                    + MS.BEGIN, "fail-fast: true\n      matrix:\n"
                    "        include:\n" + MS.BEGIN),
     "fail-fast: false"),
    (lambda t: _drop_line(t, "if: ${{ always() }}"), "always()"),
    (lambda t: _sub(t, "needs.mutation-shards.result",
                    "needs.mutation-shards.outputs"),
     "result to be success"),
    (lambda t: _drop_line(t, "needs: [mutation-shards]"), "wait for"),
    (lambda t: _sub(t, "--run ${{ matrix.specs }}", "--run"),
     "does not run its shard's specs"),
    (lambda t: _sub(t, "  mutation-matrices:", "  something-else:"),
     "no mutation-matrices job"),
    (lambda t: _sub(t, MS.END, ""), "missing or duplicated"),
])
def test_each_way_to_lose_coverage_is_refused(breaks, expect):
    found = MS.check_workflow(breaks(WF))
    assert any(expect in p for p in found), found


def test_the_workflow_contract_consults_the_shard_check(monkeypatch):
    """K1: the check must be USED by the verifier CI runs, not only exist."""
    monkeypatch.setattr(MS, "check_workflow", lambda text=None: ["planted"])
    assert any("planted" in p for p in WC.problems())


# --- the runner --------------------------------------------------------------

def _shard(i=0):
    return [f"tools/mutations/{s}" for s in MS.plan()[i]]


def test_an_empty_shard_fails():
    assert MS.run([], runner=lambda p: 0) == 1


def test_a_list_that_is_not_a_shard_fails():
    """A hand-edited or stale matrix is caught when it runs, too."""
    assert MS.run(_shard(1) + _shard(2), runner=lambda p: 0) == 1
    assert MS.run(_shard(1)[:-1], runner=lambda p: 0) == 1


def test_a_green_shard_passes():
    ran = []
    assert MS.run(_shard(1), runner=lambda p: ran.append(p) or 0) == 0
    assert ran == _shard(1)


def test_one_red_spec_fails_the_shard_and_every_spec_still_runs():
    ran = []
    first = _shard(1)[0]

    def runner(p):
        ran.append(p)
        return 1 if p == first else 0
    assert MS.run(_shard(1), runner=runner) == 1
    assert ran == _shard(1)


@pytest.mark.parametrize("rc", [2, 3, -9, 124])
def test_any_nonzero_matrix_exit_fails(rc):
    """A timeout, an interrupted harness or a killed process is not a pass."""
    assert MS.run(_shard(2), runner=lambda p: rc) == 1


def test_a_shard_that_changes_the_tree_fails(monkeypatch):
    states = iter(["before", "after"])
    monkeypatch.setattr(MS, "_tracked_state", lambda: next(states))
    assert MS.run(_shard(1), runner=lambda p: 0) == 1


def test_the_emitted_block_round_trips():
    assert MS.emit() in WF


def test_an_empty_shard_fails_even_when_the_plan_has_one(monkeypatch):
    """With fewer specs than shards the plan holds an empty shard, and []
    would then be 'a shard of the plan'. It must still fail: a shard that
    ran nothing has shown nothing."""
    monkeypatch.setattr(MS, "plan", lambda *a, **k: [[], ["x.json"]])
    assert MS.run([], runner=lambda p: 0) == 1


def _repo(tmp_path):
    import subprocess
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                       capture_output=True)
    git("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
        "-m", "base")
    return git


@pytest.mark.parametrize("leave", ["modified", "staged", "untracked"])
def test_the_tree_state_sees_what_a_shard_can_leave_behind(tmp_path, leave):
    """Directive 41: the working tree, the index and stray files. The state
    only saw the first; a staged probe and an untracked one are both things
    a stopped test run has left (D-2026-82)."""
    git = _repo(tmp_path)
    before = MS._tracked_state(tmp_path)
    if leave == "modified":
        (tmp_path / "a.py").write_text("x = 2\n")
    elif leave == "staged":
        (tmp_path / "probe.py").write_text("")
        git("add", "-N", "probe.py")
    else:
        (tmp_path / "probe.py").write_text("")
    assert MS._tracked_state(tmp_path) != before


def test_the_tree_state_is_stable_on_a_clean_tree(tmp_path):
    _repo(tmp_path)
    assert MS._tracked_state(tmp_path) == MS._tracked_state(tmp_path)


def test_the_shard_job_checks_the_tree_with_git_even_when_it_failed():
    job = (ROOT / ".github" / "workflows" / "agent-substrate.yml").read_text(
        encoding="utf-8").split("mutation-shards:", 1)[1].split(
        "mutation-matrices:", 1)[0]
    step = job.split("the shard left the tree exactly as it found it", 1)[1]
    # Written without ${{ }} on purpose: the aggregate job's
    # "if: ${{ always() }}" is a line the coverage checks find by text, and
    # it must stay the only one.
    assert "if: always()" in step.split("run:", 1)[0]
    for check in ("git diff --exit-code", "git diff --cached --exit-code",
                  "git ls-files --others --exclude-standard"):
        assert check in step, check


def test_legacy_specs_are_kept_and_scheduled_by_no_shard():
    """Directive 21: the hardware-governance spec retired with its subject,
    and the cryopanel's operating-point spec with the component model
    (directive 22). Each is kept, as historical evidence, and no active
    shard runs it."""
    import csv
    legacy = sorted((ROOT / "tools" / "mutations" / "legacy").glob("*.json"))
    assert [p.name for p in legacy] == ["cryopanel_operating_point.json",
                                        "hardware_governance.json"]
    scheduled = {s for shard in MS.plan() for s in shard}
    disp = {r["path"]: r["disposition"] for r in csv.DictReader(
        (ROOT / "FILE_DISPOSITION.csv").open(encoding="utf-8"))}
    for p in legacy:
        rel = p.relative_to(ROOT).as_posix()
        assert p.name not in scheduled
        assert disp[rel] == "RETIRE_TO_HISTORY", rel
        assert f"tools/mutations/legacy/{p.name}" not in (
            ROOT / ".github" / "workflows" / "agent-substrate.yml"
        ).read_text(encoding="utf-8")
