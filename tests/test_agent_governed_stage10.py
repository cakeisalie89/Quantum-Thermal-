"""The production caller, end to end and under attack.

This is the suite that distinguishes a control plane from a library. Every
other agent-substrate test exercises a mechanism in isolation; these run a real
Stage-10 workflow through the whole chain and then try to get a result out of
it that the chain should have refused.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest_bytes  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_TASK_TRANSITION, GovernedStage10, stage10_registry,
)
from qta_agent.tasks import (  # noqa: E402
    LeaseError, TaskRole, TaskState, TaskTransitionError,
)
from qta_agent.tools import ToolNotRegistered  # noqa: E402

WS = "verification/stage10/_pytest_governed"


@pytest.fixture()
def gov(request):
    """A governed runner writing into its own Stage-10 subdirectory.

    Inside the real workspace, not tmp_path: the tool writes through the
    Stage-10 write guard, which allows exactly one subtree. Running the
    production path anywhere else would be running a different path.
    """
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _inputs(gov, **over):
    base = {"out_dir": gov.out_rel, "name": "artifact.json",
            "payload": {"label": "MODEL_ONLY", "value": 42}}
    base.update(over)
    return base


def _run(gov, **over):
    kw = dict(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
              submitter="owner", worker="agent-worker-1",
              verifier="agent-verifier-2")
    kw.update(over)
    return gov.run(**kw)


# --- the whole chain --------------------------------------------------------

def test_a_governed_run_reaches_verified_through_every_stage(gov):
    """The end-to-end property this module exists to demonstrate."""
    run = _run(gov)
    assert run.state is TaskState.VERIFIED
    assert run.outcome == "COMPLETED"
    assert run.artifacts, "a verified run with no artifacts proves nothing"

    produced = ROOT / next(iter(run.artifacts))
    assert produced.is_file()
    assert json.loads(produced.read_text())["value"] == 42


def test_every_stage_is_recorded_in_the_hash_chained_log(gov):
    """A run that left no trail is not a governed run."""
    run = _run(gov)
    report = gov.log.verify()
    assert report.ok and report.prefix_verified

    moves = [(e.payload["src"], e.payload["dst"]) for e in gov.log.read()
             if e.action == ACT_TASK_TRANSITION]
    assert moves == [
        ("CREATED", "VALIDATED"), ("VALIDATED", "QUEUED"),
        ("QUEUED", "LEASED"), ("LEASED", "EXECUTING"),
        ("EXECUTING", "COMPLETED"), ("COMPLETED", "VERIFIED"),
    ], f"the chain took an unexpected path: {moves}"
    assert run.log_head_seq >= len(moves)


def test_the_state_survives_the_process_that_produced_it(gov):
    """Replay, not inference: a fresh projection over the same log agrees."""
    run = _run(gov)
    reopened = GovernedStage10(root=ROOT, log=EventLog(gov.log.path),
                               evidence=gov.evidence)
    assert reopened.projection().get(run.task_id).state is TaskState.VERIFIED


def test_the_artifact_is_content_addressed_not_merely_described(gov):
    """The completion cites bytes, and the bytes resolve."""
    run = _run(gov)
    for rel, dg in run.artifacts.items():
        assert gov.evidence.contains(dg), "cited evidence does not resolve"
        assert digest_bytes((ROOT / rel).read_bytes()) == dg


def test_execution_happened_in_a_separate_process(gov):
    """Recorded, because in-process execution cannot be bounded.

    The execution record carries a real exit status. A function call has no
    exit status, so its presence is the evidence that a process ran.
    """
    _run(gov)
    execs = [e.payload for e in gov.log.read()
             if e.action == "task.execution"]
    assert len(execs) == 1
    assert execs[0]["exit_status"] == 0
    assert execs[0]["tool_digest"] == \
        stage10_registry().get("stage10.emit_artifact").digest()
    assert execs[0]["limits"]["wall_seconds"] > 0


# --- what the chain must refuse ---------------------------------------------

def test_an_unregistered_tool_never_creates_a_task(gov):
    """Default deny runs before anything is recorded.

    A refused tool must not leave a task behind: a CREATED record for work
    that could never run is a permanent artefact of a request nobody
    authorized.
    """
    with pytest.raises(ToolNotRegistered):
        _run(gov, tool_id="rm_rf")
    assert gov.log.verify().count == 0


def test_the_same_actor_cannot_execute_and_verify(gov):
    """Refused before any work is done, not after.

    An agent that verifies its own work has not verified anything, so this
    fails at the call rather than producing a VERIFIED task nobody checked.
    """
    with pytest.raises(ValueError, match="verifies its own work"):
        _run(gov, worker="agent-1", verifier="agent-1")
    assert gov.log.verify().count == 0


def test_contract_violating_inputs_are_rejected_and_recorded(gov):
    """A rejection is a recorded outcome, not an exception thrown away."""
    run = _run(gov, inputs=_inputs(gov, payload="not-a-dict"))
    assert run.state is TaskState.REJECTED
    assert run.outcome == "REJECTED"
    assert not run.artifacts
    moves = [(e.payload["src"], e.payload["dst"]) for e in gov.log.read()
             if e.action == ACT_TASK_TRANSITION]
    assert moves == [("CREATED", "REJECTED")]
    assert gov.log.verify().ok


def test_a_write_outside_the_workspace_is_refused_by_the_guard(gov):
    """The substrate adds authority; it does not replace the existing guard.

    The capability scope and the Stage-10 write allowlist agree, so a governed
    run cannot reach a path an ungoverned one could not.
    """
    run = _run(gov, inputs=_inputs(gov, out_dir="outputs"))
    assert run.state is not TaskState.VERIFIED
    assert (ROOT / "outputs" / "artifact.json").exists() is False


def test_a_completed_task_cannot_be_verified_by_the_executor(gov):
    """The state machine refuses it even when called directly.

    The convenience check in ``run`` is not the enforcement; this is. Bypassing
    the front door must not bypass the rule.
    """
    run = _run(gov)
    task = gov.projection().get(run.task_id)
    from qta_agent.tasks import TaskTransition, check
    req = TaskTransition(task_id=task.task_id, src=TaskState.COMPLETED,
                         dst=TaskState.VERIFIED, actor="agent-worker-1",
                         role=TaskRole.VERIFIER, at_seq=99,
                         executed_by="agent-worker-1")
    completed = task.__class__(**{**task.__dict__,
                                  "state": TaskState.COMPLETED})
    with pytest.raises(TaskTransitionError, match="verifies its own work"):
        check(req, completed)


def test_a_worker_with_a_lapsed_lease_cannot_report_completion(gov):
    """A worker back from the dead reports on work someone else may have redone."""
    run = _run(gov)
    task = gov.projection().get(run.task_id)
    from qta_agent.tasks import Lease, TaskTransition, check
    lapsed = Lease(lease_id="L1", holder="agent-worker-1", granted_seq=1,
                   expires_after_seq=5)
    executing = task.__class__(**{**task.__dict__,
                                  "state": TaskState.EXECUTING,
                                  "lease": lapsed})
    req = TaskTransition(task_id=task.task_id, src=TaskState.EXECUTING,
                         dst=TaskState.COMPLETED, actor="agent-worker-1",
                         role=TaskRole.WORKER, at_seq=99, lease_id="L1",
                         result_digest="a" * 64)
    with pytest.raises(LeaseError, match="lapsed"):
        check(req, executing)


def test_a_verified_run_stops_verifying_once_its_artifact_changes(gov):
    """Verification re-derives from disk, so it cannot pass on stale bytes."""
    run = _run(gov)
    rel = next(iter(run.artifacts))
    (ROOT / rel).write_text('{"value": 999}\n', encoding="utf-8")
    ok, why = gov._verify_artifacts(run.artifacts)
    assert not ok and "no longer hashes" in why


def test_a_missing_artifact_fails_verification(gov):
    run = _run(gov)
    rel = next(iter(run.artifacts))
    (ROOT / rel).unlink()
    ok, why = gov._verify_artifacts(run.artifacts)
    assert not ok and "no longer on disk" in why


def test_a_run_that_produced_nothing_cannot_be_verified(gov):
    """A completion with nothing to point at is not verifiable."""
    ok, why = gov._verify_artifacts({})
    assert not ok and "no artifacts" in why


def test_the_projection_refuses_a_log_it_does_not_fully_understand(gov):
    """An unknown action is an error, not a skip.

    Silently ignoring one would let a future writer add authority-relevant
    events that older readers quietly drop -- and the projection would look
    healthy while disagreeing with the log.
    """
    _run(gov)
    gov.log.append(actor="x", action="task.invented", target="t",
                   payload={"task_id": "t"})
    with pytest.raises(ValueError, match="unknown action"):
        gov.projection()


def test_the_projection_refuses_a_broken_log(gov):
    run = _run(gov)
    lines = gov.log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["note"] = "tampered"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    gov.log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(Exception):
        gov.projection().get(run.task_id)


def test_a_forged_transition_in_the_log_is_not_applied(gov):
    """Replay re-authorizes; presence in the log is not authority.

    A hand-written EXECUTING -> VERIFIED record skips the only edge into
    VERIFIED. Appending it makes it a permanent fact that it was ATTEMPTED,
    and the projection still refuses to treat it as state.
    """
    run = _run(gov)
    gov.log.append(
        actor="attacker", action=ACT_TASK_TRANSITION, target=run.task_id,
        payload={"task_id": run.task_id, "src": "EXECUTING",
                 "dst": "VERIFIED", "role": "VERIFIER", "note": "forged"})
    with pytest.raises(TaskTransitionError):
        gov.projection()


# --- the production wiring itself -------------------------------------------

def test_the_governed_rule_is_part_of_the_ordinary_stage10_workflow():
    """A governed path nobody runs is not a production path.

    The directive's own bar: a test-only caller does not count, a demo script
    does not count, an unused CLI does not count. What makes this real is that
    ``s10_full`` -- the aggregate rule the Stage-10 workflow and its CI job
    actually invoke -- depends on the governed run's output. Removing that
    dependency would turn the control plane back into a library, so it is
    pinned here rather than left to a reviewer noticing.
    """
    snakefile = (ROOT / "Snakefile").read_text(encoding="utf-8")
    assert "rule s10_governed:" in snakefile

    after = snakefile.split("rule s10_full:", 1)[1].split("\nrule ", 1)[0]
    assert "governed/governed_run.json" in after, (
        "s10_full no longer depends on the governed run; the production path "
        "would stop being exercised by the ordinary workflow")


def test_the_governed_rule_fails_the_build_on_an_unverified_run():
    """A green build must not launder away a missing verification.

    A rule that wrote its report regardless of outcome would report success
    for a run that timed out or was rejected -- which is worse than having no
    governed path, because it makes the absence of verification look like
    verification.
    """
    snakefile = (ROOT / "Snakefile").read_text(encoding="utf-8")
    rule = snakefile.split("rule s10_governed:", 1)[1].split("\nrule ", 1)[0]
    assert "TaskState.VERIFIED" in rule
    assert "assert run.state is TaskState.VERIFIED" in rule
    assert "assert gov.log.verify().ok" in rule


def test_the_bridge_writes_through_the_stage10_guard_not_around_it():
    """The substrate adds authority; it does not replace the existing guard."""
    tool = (ROOT / "qta_agent" / "_stage10_tool.py").read_text(encoding="utf-8")
    assert "guard_output_dir" in tool, (
        "the governed tool must use the Stage-10 write guard, so a governed "
        "run is subject to the same allowlist as an ungoverned one")
    assert "write_json_deterministic" in tool


# ---------------------------------------------------------------------------
# The task state machine, rule by rule.
#
# Nine mutations survived the first matrix run against this suite. All nine
# were the same omission: the lifecycle was exercised only THROUGH the governed
# path, which walks the happy route and never puts a single rule under
# pressure. A state machine tested only by its intended path is a state machine
# whose forbidden transitions have never been attempted.
# ---------------------------------------------------------------------------

from qta_agent.tasks import (  # noqa: E402
    TERMINAL, Lease, Task, TaskTransition, apply_transition, check,
)

DIG = "a" * 64


def _task(**kw):
    base = dict(task_id="t1", tool_id="probe", submitter="owner",
                inputs_digest=DIG, state=TaskState.CREATED)
    base.update(kw)
    return Task(**base)


def _req(**kw):
    base = dict(task_id="t1", src=TaskState.CREATED, dst=TaskState.VALIDATED,
                actor="owner", role=TaskRole.SUBMITTER, at_seq=10)
    base.update(kw)
    return TaskTransition(**base)


@pytest.mark.parametrize("state", sorted(TERMINAL, key=lambda s: s.value))
def test_a_terminal_task_cannot_be_revived(state):
    """Q1: recovery is a NEW task, which leaves a trail. Reviving does not."""
    with pytest.raises(TaskTransitionError, match="terminal"):
        check(_req(src=state, dst=TaskState.QUEUED, role=TaskRole.SCHEDULER),
              _task(state=state))


def test_a_role_that_does_not_own_an_edge_cannot_take_it():
    """Q3: role, with every other precondition satisfied.

    CREATED -> VALIDATED needs no lease, no distinct actor and no result
    digest, so the role is the only thing that can refuse this.
    """
    with pytest.raises(TaskTransitionError, match="role WORKER may not"):
        check(_req(role=TaskRole.WORKER), _task())
    assert check(_req(role=TaskRole.SUBMITTER), _task()).dst is \
        TaskState.VALIDATED


def test_only_the_lease_holder_may_report_on_the_work():
    """Q5: the lease names a holder, and the holder is checked."""
    lease = Lease("L1", "worker-1", granted_seq=1, expires_after_seq=100)
    task = _task(state=TaskState.EXECUTING, lease=lease)
    req = dict(src=TaskState.EXECUTING, dst=TaskState.COMPLETED,
               role=TaskRole.WORKER, lease_id="L1", result_digest=DIG)
    with pytest.raises(LeaseError, match="is held by"):
        check(_req(actor="worker-2", **req), task)
    assert check(_req(actor="worker-1", **req), task).dst is TaskState.COMPLETED


def test_a_stale_lease_id_is_not_the_current_lease():
    """Q7: identity, not merely presence.

    A worker holding a lease that has since been reissued is reporting on work
    somebody else now owns -- and its own actor name still matches.
    """
    lease = Lease("L2", "worker-1", granted_seq=1, expires_after_seq=100)
    task = _task(state=TaskState.EXECUTING, lease=lease)
    with pytest.raises(LeaseError, match="is not this task's lease"):
        check(_req(src=TaskState.EXECUTING, dst=TaskState.COMPLETED,
                   actor="worker-1", role=TaskRole.WORKER, lease_id="L1",
                   result_digest=DIG), task)


def test_verification_refuses_when_no_executor_is_recorded():
    """Q9: independence is established, never assumed.

    With no executor on record there is nothing to be distinct FROM, so the
    machine refuses rather than treating an unknown as a different party.
    """
    task = _task(state=TaskState.COMPLETED, executed_by=None)
    with pytest.raises(TaskTransitionError, match="no executor is recorded"):
        check(_req(src=TaskState.COMPLETED, dst=TaskState.VERIFIED,
                   actor="verifier", role=TaskRole.VERIFIER), task)


@pytest.mark.parametrize("bad", [None, "", "not-a-digest", "A" * 64, 42])
def test_completion_requires_the_digest_of_a_real_result(bad):
    """Q10: a completion with nothing to point at is an assertion."""
    lease = Lease("L1", "worker-1", granted_seq=1, expires_after_seq=100)
    task = _task(state=TaskState.EXECUTING, lease=lease)
    with pytest.raises(TaskTransitionError, match="requires the digest"):
        check(_req(src=TaskState.EXECUTING, dst=TaskState.COMPLETED,
                   actor="worker-1", role=TaskRole.WORKER, lease_id="L1",
                   result_digest=bad), task)


def test_requeued_work_drops_the_dead_workers_lease():
    """Q11: otherwise stranded work stays stranded.

    The lease is what makes ownership adjudicable. Returning a task to the
    queue while it still carries the lapsed holder's lease means nobody else
    can take it -- the recovery path would leave the work permanently stuck.
    """
    lease = Lease("L1", "worker-1", granted_seq=1, expires_after_seq=5)
    task = _task(state=TaskState.EXECUTING, lease=lease)
    req = _req(src=TaskState.EXECUTING, dst=TaskState.QUEUED, actor="scheduler",
               role=TaskRole.SCHEDULER, at_seq=99)
    edge = check(req, task)
    requeued = apply_transition(task, edge, req, seq=99)
    assert requeued.lease is None, "requeued work kept a dead worker's lease"
    assert requeued.state is TaskState.QUEUED


def test_a_terminal_transition_also_drops_the_lease():
    lease = Lease("L1", "worker-1", granted_seq=1, expires_after_seq=100)
    task = _task(state=TaskState.EXECUTING, lease=lease)
    req = _req(src=TaskState.EXECUTING, dst=TaskState.CANCELLED,
               actor="owner", role=TaskRole.SUBMITTER, at_seq=50)
    edge = check(req, task)
    assert apply_transition(task, edge, req, seq=50).lease is None


# --- the governed path's remaining gaps -------------------------------------

def test_an_artifact_that_is_not_resolvable_as_evidence_fails_verification(gov):
    """G9: on disk and correctly hashed is not the same as held as evidence.

    Provenance that points at a digest the evidence store cannot resolve is a
    citation to nothing -- the exact hole the store was built to close, and it
    must be closed on this path too.
    """
    run = _run(gov)
    rel, dg = next(iter(run.artifacts.items()))
    # The file is untouched and still hashes correctly; only the store loses it.
    gov.evidence._blob_path(dg).unlink()
    ok, why = gov._verify_artifacts(run.artifacts)
    assert not ok and "not resolvable as evidence" in why
    assert digest_bytes((ROOT / rel).read_bytes()) == dg, (
        "fixture is wrong: the file must still be correct on disk")


def test_the_capability_is_scoped_to_exactly_what_the_tool_declared(gov):
    """G10: a grant must not be broader than the tool's writable scope.

    Widening it to a parent directory would let a compromised tool write
    outside what its own contract declares, while every other check still
    passes. The grant is read back from the log rather than from the caller.
    """
    _run(gov)
    issued = [e.payload for e in gov.log.read()
              if e.action == "capability.issue"]
    assert len(issued) == 1
    spec = stage10_registry().get("stage10.emit_artifact")
    assert tuple(issued[0]["scope"]) == tuple(spec.writable_scope), (
        f"grant scope {issued[0]['scope']} != declared writable scope "
        f"{list(spec.writable_scope)}")
    assert issued[0]["tool_id"] == "stage10.emit_artifact"
    assert issued[0]["action"] == "EXECUTE_TOOL"
