"""AUD-013: a campaign resumed after its process dies reaches what an
uninterrupted one reaches.

Each case runs ``tools/research_horizon.py`` in a process killed at one
point with ``os._exit``, as a reclaimed machine kills it: no handler runs,
and no ``finally``. ``--resume`` then runs in a new process, and the result
is held to an uninterrupted run of the same campaign. The campaign has two
stages, of ten and twenty scripted decisions, with a withdrawal at the
first.

The resumed campaign's counters, counted from the log, equal the
uninterrupted one's: decisions, inference calls, governed operations,
verified records, conclusions and objective states. Nothing is executed,
received or answered twice.

The two logs are compared action by action. A resume may add two kinds of
record and no others, each named:

- a stage's checkpoint, measured again because the process died before
  recording the measurement;
- one context the dead process assembled for a model call it never made,
  when it died between assembling and sending.

The process counters say what they count: ``process_restarts`` is one, and
a controller is counted once for the objective that was in flight.

The agent is a script, and its decisions are scripted decisions.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import adversarial_eval as E  # noqa: E402
import research_horizon as H  # noqa: E402

TOOL = ROOT / "tools" / "research_horizon.py"
ARGS = ("--decisions", "20", "--stages", "10,20", "--invalidate-at", "10")
#: counted from the log, so a resume cannot count them twice
FROM_THE_LOG = ("conclusions", "governed_operations", "inference_calls",
                "objectives", "scripted_decisions", "verified_records")


def _run(name: str, *extra: str, die: str | None = None, args=ARGS):
    env = {k: v for k, v in os.environ.items()
           if k != "RESEARCH_HORIZON_DIE_AT"}
    if die is not None:
        env["RESEARCH_HORIZON_DIE_AT"] = die
    return subprocess.run(
        [sys.executable, str(TOOL), "--name", name, *args, *extra],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)


def _workspace(name: str) -> Path:
    return ROOT / E.WS / name


def _actions(name: str) -> Counter:
    return Counter(json.loads(line)["action"] for line in
                   (_workspace(name) / "log.jsonl").read_text(
                       encoding="utf-8").splitlines())


def _clean(name: str) -> None:
    shutil.rmtree(_workspace(name), ignore_errors=True)
    (ROOT / E.WS / f"{name}.lock").unlink(missing_ok=True)


@pytest.fixture()
def campaign(request):
    name = "pytest-resume-" + request.node.name.split("[")[-1].strip(
        "]").replace(":", "-").replace("_", "-")[:40]
    _clean(name)
    yield name
    _clean(name)


@pytest.fixture(scope="module")
def baseline():
    name = "pytest-resume-baseline"
    _clean(name)
    try:
        r = _run(name, "--keep")
        actions = _actions(name)
    finally:
        _clean(name)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert [s["target_decisions"] for s in out["stages"]] == [10, 20]
    return out, actions


#: the records a resume may add, and at most how many of each
MAY_ADD = {"checkpoint.state": 2, "context.build": 1}


def _holds_to(out: dict, actions: Counter, baseline: tuple) -> None:
    base, base_actions = baseline
    added = {k: actions[k] - base_actions[k]
             for k in set(actions) | set(base_actions)
             if actions[k] != base_actions[k]}
    assert all(n > 0 and n <= MAY_ADD.get(k, 0) for k, n in added.items()), \
        added
    a, b = base["stages"][-1], out["stages"][-1]
    for k in FROM_THE_LOG:
        assert b["counters"][k] == a["counters"][k], k
    assert b["objective_states"] == a["objective_states"]
    assert b["duplicates"] == {"executions": 0, "inference_results": 0,
                               "receipts": 0}
    assert b["recovery_failures"] == 0
    assert b["second_reader_anomalies"] == 0 and b["log_verifies"]
    assert b["checkpoint"] == {"equal_state": True, "prefix_verified": False}
    assert b["counters"]["process_restarts"] == 1
    assert b["counters"]["resets"] == (b["counters"]["objectives"]
                                       + b["counters"]["restarts"])
    assert [s["target_decisions"] for s in out["stages"]] == [10, 20]


#: Where the process dies, and what in the directive each stands for:
#: before a step executes; during one; after the governed run's external
#: effect; after the check and before the decision; after the decision and
#: before the step that acknowledges it; between an objective and its save;
#: after a stage's withdrawal; inside a checkpoint; after a stage measured.
POINTS = ("after_register:1", "before_infer:1", "after_infer:1",
          "after_receive:1", "after_run:1", "after_check:1",
          "after_decide:1", "before_step_record:1", "after_objective:1",
          "after_invalidation:10", "mid_checkpoint:20", "after_measure:10")


@pytest.mark.parametrize("point", POINTS)
def test_a_campaign_killed_at_a_point_resumes_to_the_uninterrupted_result(
        point, baseline, campaign):
    died = _run(campaign, die=point)
    assert died.returncode == 137, (died.stdout[-800:], died.stderr[-3000:])
    assert (_workspace(campaign) / H.STATE_FILE).exists()
    resumed = _run(campaign, "--resume", "--keep")
    assert resumed.returncode == 0, resumed.stderr[-3000:]
    out = json.loads(resumed.stdout)
    _holds_to(out, _actions(campaign), baseline)
    in_flight = point.split(":")[0] not in ("after_measure",
                                            "after_invalidation",
                                            "mid_checkpoint")
    assert len(out["stages"][-1]["resumed"]) == int(in_flight)
    if in_flight:
        assert out["stages"][-1]["resumed"][0]["objective"] == "obj-00001"
        assert out["stages"][-1]["resumed"][0]["now"] == "CONCLUDED"
    # the stage boundary's withdrawals happen once, whoever records them
    base, _ = baseline
    if point == "after_invalidation:10":
        # the dead process withdrew both and recorded neither; the resume
        # settles them and says so, and repeats none
        assert [i.get("resumed") is not None
                for i in out["invalidations"]] == [True]
    else:
        assert out["invalidations"][0]["origin"].startswith("task:")
        assert [sorted(i) for i in out["invalidations"]] == \
            [sorted(i) for i in base["invalidations"]]


def test_a_crash_deep_in_a_stage_resumes_from_the_objective_last_saved(
        campaign):
    """Three objectives in one stage, and the process dies as the third
    begins. The state was saved before each objective, so the log is at
    most one objective ahead of it, and the resume finishes that one."""
    one_stage = ("--decisions", "30", "--stages", "30", "--invalidate-at",
                 "0")
    died = _run(campaign, die="after_register:2", args=one_stage)
    assert died.returncode == 137, died.stderr[-3000:]
    resumed = _run(campaign, "--resume", args=one_stage)
    assert resumed.returncode == 0, resumed.stderr[-3000:]
    s = json.loads(resumed.stdout)["stages"][-1]
    assert s["counters"]["objectives"] == 3
    assert s["resumed"] == [{"objective": "obj-00002", "found": "OPEN",
                             "now": "CONCLUDED"}]
    assert s["duplicates"] == {"executions": 0, "inference_results": 0,
                               "receipts": 0}
    assert s["recovery_failures"] == 0


def test_a_second_process_is_refused_while_one_holds_the_campaign(campaign):
    """Two processes resuming one campaign would each count the other's
    work. The lock is the kernel's: held by a live process, it refuses the
    second; released by the first's death, it lets the next one in."""
    assert _run(campaign, die="after_register:1").returncode == 137
    fd = H._hold(ROOT / E.WS / f"{campaign}.lock")   # another live holder
    try:
        busy = _run(campaign, "--resume")
    finally:
        os.close(fd)
    assert busy.returncode != 0
    assert "another process holds the campaign" in busy.stderr
    resumed = _run(campaign, "--resume")             # the holder is gone
    assert resumed.returncode == 0, resumed.stderr[-3000:]


def test_a_history_rolled_back_behind_the_state_is_refused(campaign):
    """The log restored from an earlier copy, its head witness with it: the
    state names a position the log no longer holds, and resuming would
    report work the history does not show."""
    assert _run(campaign, die="after_register:1").returncode == 137
    ws = _workspace(campaign)
    saved = {f: (ws / f).read_bytes() for f in ("log.jsonl",
                                                "log.jsonl.head")}
    assert _run(campaign, "--resume",
                die="after_measure:20").returncode == 137
    for f, data in saved.items():
        (ws / f).write_bytes(data)
    refused = _run(campaign, "--resume")
    assert refused.returncode != 0
    assert "rolled back or rewritten" in refused.stderr


def test_a_lagging_head_witness_does_not_stop_a_resume(campaign):
    """A witness behind the log is the window between an append and the
    witness's update, which every crash may leave. The log extends it, so
    the campaign resumes."""
    assert _run(campaign, die="after_register:1").returncode == 137
    ws = _workspace(campaign)
    old_head = (ws / "log.jsonl.head").read_bytes()
    # the resumed process finishes objective 1 inside the resume itself,
    # then dies with the log well past the witness kept above
    assert _run(campaign, "--resume",
                die="after_measure:20").returncode == 137
    (ws / "log.jsonl.head").write_bytes(old_head)
    resumed = _run(campaign, "--resume")
    assert resumed.returncode == 0, resumed.stderr[-3000:]


def test_a_torn_state_write_leaves_the_last_whole_state(campaign):
    """The state is written to a temporary file and moved into place, so a
    process killed mid-write leaves the previous state whole beside a torn
    temporary, which a resume never reads."""
    assert _run(campaign, die="after_register:1").returncode == 137
    ws = _workspace(campaign)
    (ws / (H.STATE_FILE + ".tmp")).write_text('{"schema": "torn', "utf-8")
    resumed = _run(campaign, "--resume")
    assert resumed.returncode == 0, resumed.stderr[-3000:]


def test_a_campaign_with_no_state_is_not_resumed(campaign):
    assert _run(campaign, die="after_register:1").returncode == 137
    (_workspace(campaign) / H.STATE_FILE).unlink()
    refused = _run(campaign, "--resume")
    assert refused.returncode != 0
    assert "no readable campaign state" in refused.stderr
