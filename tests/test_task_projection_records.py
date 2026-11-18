"""The task projection reads each record in its own words (AUD-072 to
AUD-075).

``GovernedTasks._project`` is the primary reader of a governed task's
lifecycle. A second ``task.create`` under an existing id replaced the task
it named -- a task standing VERIFIED read CREATED, under another tool and
another submitter -- while the second reader refused the record. Fifteen
malformed records raised KeyError, ValueError or TypeError out of the
projection, where every refusal it makes is a TaskTransitionError. And the
second reader named a task by the record's target when its payload named
none, and folded creates naming no tool or no inputs, which the primary
refuses: the two readers held different histories. Under a role registry,
last, no grant stands behind the control plane's moves (AUD-075, the
owner's): measured here, not fixed.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate import roles as R  # noqa: E402
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_tasks import (  # noqa: E402
    POLICY_ID, SUBMITTER_ID, VERIFIER_ID, WORKER_ID, GovernedTasks,
)
from agent_substrate.tasks import TaskState, TaskTransitionError  # noqa: E402

WS = "verification/stage10/_pytest_task_records"


def _registry() -> R.RoleRegistry:
    """The runner's three parties, each granted its role over the tool,
    and the owner its policy. Nobody named here is mallory."""
    tool = ("tool:stage10.emit_artifact",)
    return R.RoleRegistry([
        R.RoleGrant("g-owner", "owner", R.POLICY_OWNER,
                    (f"policy:{POLICY_ID}",)),
        R.RoleGrant("g-submit", SUBMITTER_ID, R.PROPOSER, tool),
        R.RoleGrant("g-work", WORKER_ID, R.EXECUTOR, tool),
        R.RoleGrant("g-verify", VERIFIER_ID, R.VERIFIER, tool)])


def _workspace_runner(request, roles):
    """A governed runner writing into its own workspace subdirectory: the
    tool writes through the workspace guard, which allows one subtree."""
    name = request.node.name.replace("/", "_").replace("[", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedTasks(root=ROOT, log=EventLog(base / "log.jsonl"),
                      evidence=EvidenceStore(base / "evidence"), roles=roles)
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


@pytest.fixture()
def gov(request):
    yield from _workspace_runner(request, None)


@pytest.fixture()
def governed(request):
    """As ``gov``, under the registry above."""
    yield from _workspace_runner(request, _registry())


def _run(gov, value=1):
    return gov.run(tool_id="stage10.emit_artifact",
                   inputs={"out_dir": gov.out_rel, "name": f"a{value}.json",
                           "payload": {"label": "MODEL_ONLY",
                                       "value": value}},
                   submitter=SUBMITTER_ID, worker=WORKER_ID,
                   verifier=VERIFIER_ID)


def _reopened(gov) -> GovernedTasks:
    return GovernedTasks(root=ROOT, log=EventLog(gov.log.path),
                         evidence=gov.evidence, roles=gov.roles)


# ---- AUD-072: one create per task ------------------------------------------
@pytest.mark.parametrize("actor", [SUBMITTER_ID, "mallory"])
def test_a_second_create_for_a_task_is_refused(gov, actor):
    """Measured before the fix: the task below, VERIFIED, read CREATED
    under the second record's tool and submitter, with no executor."""
    run = _run(gov)
    assert run.state is TaskState.VERIFIED
    gov.log.append(actor=actor, action="task.create", target=run.task_id,
                   payload={"task_id": run.task_id,
                            "tool_id": "stage10.digest_index",
                            "submitter": actor,
                            "inputs_digest": digest({"x": 1}),
                            "depends_on": []})
    with pytest.raises(TaskTransitionError, match="created twice"):
        _reopened(gov).projection()
    tr = rc.reconstruct_tasks(EventLog(gov.log.path))
    assert tr.tasks[run.task_id]["state"] == "VERIFIED"
    assert [a for a in tr.anomalies if "created twice" in a]


def test_a_genuine_run_reads_alike_in_both_readers(gov):
    """Control: one create, and the tool the spec is chosen by is the
    tool the projection holds."""
    run = _run(gov)
    g = _reopened(gov)
    proj = g.projection()
    assert proj.get(run.task_id).state is TaskState.VERIFIED
    assert g._tool_of(run.task_id) == proj.get(run.task_id).tool_id
    tr = rc.reconstruct_tasks(EventLog(gov.log.path))
    assert not tr.anomalies and not tr.unauthorized
    assert rc.compare_tasks(proj, tr) == ()


# ---- AUD-073: a malformed record, in the projection's words -----------------
GOOD = {"task_id": "t1", "tool_id": "stage10.emit_artifact",
        "submitter": "owner", "inputs_digest": digest({"x": 1}),
        "depends_on": []}


def _without(key) -> dict:
    return {k: v for k, v in GOOD.items() if k != key}


#: One malformed record each: every one raised KeyError, ValueError or
#: TypeError out of the projection before AUD-073.
MALFORMED = {
    "a create with no task_id": ("task.create", _without("task_id")),
    "a create with no submitter": ("task.create", _without("submitter")),
    "a create with no tool": ("task.create", _without("tool_id")),
    "a create with no inputs digest": ("task.create",
                                       _without("inputs_digest")),
    "a create naming its task by a list": ("task.create",
                                           {**GOOD, "task_id": ["t1"]}),
    "a create depending on a number": ("task.create",
                                       {**GOOD, "depends_on": 7}),
    "a transition for a task never created": (
        "task.transition", {"task_id": "nope", "src": "CREATED",
                            "dst": "VALIDATED", "role": "SUBMITTER"}),
    "a transition with no source": (
        "task.transition", {"task_id": "t1", "dst": "VALIDATED",
                            "role": "SUBMITTER"}),
    "a transition from no state": (
        "task.transition", {"task_id": "t1", "src": "NOPE",
                            "dst": "VALIDATED", "role": "SUBMITTER"}),
    "a transition to no state": (
        "task.transition", {"task_id": "t1", "src": "CREATED",
                            "dst": "NOPE", "role": "SUBMITTER"}),
    "a transition in no role": (
        "task.transition", {"task_id": "t1", "src": "CREATED",
                            "dst": "VALIDATED"}),
    "a transition in a role that is none": (
        "task.transition", {"task_id": "t1", "src": "CREATED",
                            "dst": "VALIDATED", "role": "NOPE"}),
    "a lease with a field no lease has": (
        "task.transition", {"task_id": "t1", "src": "CREATED",
                            "dst": "VALIDATED", "role": "SUBMITTER",
                            "lease": {"nope": 1}}),
    "a lease that is a list": (
        "task.transition", {"task_id": "t1", "src": "CREATED",
                            "dst": "VALIDATED", "role": "SUBMITTER",
                            "lease": [1]}),
    "an execution for a task never created": (
        "task.execution", {"task_id": "nope", "result_digest": "x"}),
}


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_a_malformed_record_is_refused_in_the_projections_words(tmp_path,
                                                                label):
    action, payload = MALFORMED[label]
    log = EventLog(tmp_path / "log.jsonl")
    g = GovernedTasks(root=ROOT, log=log,
                      evidence=EvidenceStore(tmp_path / "evidence"))
    if action != "task.create":
        log.append(actor="owner", action="task.create", target="t1",
                   payload=GOOD)
    log.append(actor="owner", action=action, target="t1", payload=payload)
    with pytest.raises(TaskTransitionError, match=r"^seq \d+: "):
        g.projection()


# ---- AUD-074: the second reader names a task as the primary does ------------
def _second(tmp_path, *records):
    log = EventLog(tmp_path / "log.jsonl")
    for action, target, payload in records:
        log.append(actor="owner", action=action, target=target,
                   payload=payload)
    return rc.reconstruct_tasks(EventLog(log.path))


def test_the_second_reader_names_a_task_by_its_record(tmp_path):
    """A create naming no task_id was folded under the record's target."""
    tr = _second(tmp_path, ("task.create", "t1", _without("task_id")))
    assert "t1" not in tr.tasks
    assert [a for a in tr.anomalies if re.search(r"names task None", a)]


@pytest.mark.parametrize("missing", ["tool_id", "inputs_digest"])
def test_the_second_reader_refuses_a_create_naming_no_work(tmp_path,
                                                           missing):
    tr = _second(tmp_path, ("task.create", "t1", _without(missing)))
    assert "t1" not in tr.tasks
    assert [a for a in tr.anomalies if missing in a]


def test_a_well_formed_create_is_folded_by_the_second_reader(tmp_path):
    """Control."""
    tr = _second(tmp_path, ("task.create", "t1", GOOD))
    assert tr.tasks["t1"]["state"] == "CREATED" and not tr.anomalies


# ---- AUD-075: the control plane's moves, which no grant stands behind -------
def test_a_control_plane_move_is_made_under_no_grant(governed):
    """AUD-075, the owner's to decide: measured, not fixed. Under a
    registry naming the runner's three parties, mallory, whom no grant
    names, moves VERIFIED tasks to INVALIDATED as SYSTEM, through
    invalidate() and by a record, rejects a created task as SCHEDULER and
    cancels another as SYSTEM, and both readers hold every move. Her own
    create is refused. When the owner binds the control plane's roles,
    this test changes with the register."""
    first, second = _run(governed, 1), _run(governed, 2)
    assert first.state is second.state is TaskState.VERIFIED
    moved = governed.invalidate(first.task_id, reason="no grant says so",
                                actor="mallory")
    assert moved.state is TaskState.INVALIDATED
    governed.log.append(actor="mallory", action="task.transition",
                        target=second.task_id,
                        payload={"task_id": second.task_id, "src": "VERIFIED",
                                 "dst": "INVALIDATED", "role": "SYSTEM",
                                 "note": "no grant says so"})
    for tid, dst, role in (("t-rej", "REJECTED", "SCHEDULER"),
                           ("t-can", "CANCELLED", "SYSTEM")):
        governed.log.append(actor=SUBMITTER_ID, action="task.create",
                            target=tid,
                            payload={**GOOD, "task_id": tid,
                                     "submitter": SUBMITTER_ID})
        governed.log.append(actor="mallory", action="task.transition",
                            target=tid,
                            payload={"task_id": tid, "src": "CREATED",
                                     "dst": dst, "role": role})
    held = {first.task_id: "INVALIDATED", second.task_id: "INVALIDATED",
            "t-rej": "REJECTED", "t-can": "CANCELLED"}
    proj = _reopened(governed).projection()
    assert {t: proj.get(t).state.value for t in held} == held
    tr = rc.reconstruct_tasks(EventLog(governed.log.path), roles=_registry())
    assert {t: tr.tasks[t]["state"] for t in held} == held
    assert tr.unauthorized == [] and rc.compare_tasks(proj, tr) == ()
    governed.log.append(actor="mallory", action="task.create", target="t-x",
                        payload={**GOOD, "task_id": "t-x",
                                 "submitter": "mallory"})
    with pytest.raises(TaskTransitionError, match=R.NOT_GRANTED):
        _reopened(governed).projection()                       # control
