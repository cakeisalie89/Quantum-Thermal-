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
    ACT_TASK_TRANSITION, SUBMITTER_ID, VERIFIER_ID, WORKER_ID,
    GovernedStage10, stage10_registry,
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


def _task_events(gov) -> list:
    """Task-lifecycle events only.

    The constructor bootstraps a policy and three identities onto the log, so
    "nothing happened" is no longer "the log is empty" -- it is "no task was
    created". Asserting the older, coarser thing would have started passing
    for the wrong reason the moment anything else shared the log.
    """
    return [ev.action for ev in gov.log.read()
            if ev.action.startswith("task.")]


def _run(gov, **over):
    kw = dict(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
              submitter=SUBMITTER_ID, worker=WORKER_ID,
              verifier=VERIFIER_ID)
    kw.update(over)
    return gov.run(**kw)


def _read_cap(gov, task_id="t-direct", cap_id="cap-direct-read"):
    """Mint the read grant verification needs, recorded in the log.

    Verification now reads through the governed boundary, so calling
    ``_verify_artifacts`` directly needs a real capability. Minting one here
    keeps these tests about VERIFICATION semantics; the tests that the read
    boundary refuses an unauthorized verifier live in test_agent_readpath.py
    and below.
    """
    from qta_agent.capability import Action, issue
    from qta_agent.governed_stage10 import READ_ROOT_ID, WORKSPACE_PREFIX
    from qta_agent.readpath import read_scope

    cap = issue(capability_id=cap_id, subject=VERIFIER_ID,
                action=Action.READ_PATHS, task_id=task_id,
                scope=read_scope(READ_ROOT_ID, WORKSPACE_PREFIX),
                issued_seq=gov.log.verify().head_seq + 1)
    gov.capabilities.issue(cap, actor="scheduler")
    return cap_id


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
    assert _task_events(gov) == [], (
        "the bootstrap's policy and identity records are expected; a TASK "
        "record for work that could never run is not")


def test_the_same_actor_cannot_execute_and_verify(gov):
    """Refused before any work is done, not after.

    An agent that verifies its own work has not verified anything, so this
    fails at the call rather than producing a VERIFIED task nobody checked.
    """
    with pytest.raises(ValueError, match="verifies its own work"):
        _run(gov, worker=WORKER_ID, verifier=WORKER_ID)
    assert _task_events(gov) == []


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
                         dst=TaskState.VERIFIED, actor=WORKER_ID,
                         role=TaskRole.VERIFIER, at_seq=99,
                         executed_by=WORKER_ID)
    completed = task.__class__(**{**task.__dict__,
                                  "state": TaskState.COMPLETED})
    with pytest.raises(TaskTransitionError, match="verifies its own work"):
        check(req, completed)


def test_a_worker_with_a_lapsed_lease_cannot_report_completion(gov):
    """A worker back from the dead reports on work someone else may have redone."""
    run = _run(gov)
    task = gov.projection().get(run.task_id)
    from qta_agent.tasks import Lease, TaskTransition, check
    lapsed = Lease(lease_id="L1", holder=WORKER_ID, granted_seq=1,
                   expires_after_seq=5)
    executing = task.__class__(**{**task.__dict__,
                                  "state": TaskState.EXECUTING,
                                  "lease": lapsed})
    req = TaskTransition(task_id=task.task_id, src=TaskState.EXECUTING,
                         dst=TaskState.COMPLETED, actor=WORKER_ID,
                         role=TaskRole.WORKER, at_seq=99, lease_id="L1",
                         result_digest="a" * 64)
    with pytest.raises(LeaseError, match="lapsed"):
        check(req, executing)


def test_a_verified_run_stops_verifying_once_its_artifact_changes(gov):
    """Verification re-derives from disk, so it cannot pass on stale bytes."""
    run = _run(gov)
    rel = next(iter(run.artifacts))
    (ROOT / rel).write_text('{"value": 999}\n', encoding="utf-8")
    ok, why = gov._verify_artifacts(
        run.artifacts, task_id=run.task_id,
        capability_id=_read_cap(gov, run.task_id))
    assert not ok and "no longer hashes" in why


def test_a_missing_artifact_fails_verification(gov):
    run = _run(gov)
    rel = next(iter(run.artifacts))
    (ROOT / rel).unlink()
    ok, why = gov._verify_artifacts(
        run.artifacts, task_id=run.task_id,
        capability_id=_read_cap(gov, run.task_id))
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


def test_a_forged_record_cannot_pick_its_own_starting_state(gov):
    """THE hole the test above was masking, found by the hostile campaign.

    ``test_a_forged_transition_in_the_log_is_not_applied`` chose EXECUTING ->
    VERIFIED, which is not an edge at all, so the record was refused for a
    reason that had nothing to do with the property being claimed. Choose a
    pair that IS an edge and name a convenient ``src``, and the replay applied
    it: every pair in the table is available to a forger, because the check
    ran against the starting state the RECORD supplied.

    This moved a task out of VERIFIED -- a sealed state -- which is the exact
    thing the state machine exists to make impossible.
    """
    run = _run(gov)
    assert gov.projection().tasks[run.task_id].state is TaskState.VERIFIED

    gov.log.append(
        actor="attacker", action=ACT_TASK_TRANSITION, target=run.task_id,
        payload={"task_id": run.task_id, "src": TaskState.EXECUTING.value,
                 "dst": TaskState.TIMED_OUT.value, "role": "SYSTEM"})
    with pytest.raises(TaskTransitionError, match="moves it from"):
        gov.projection()


def test_a_worker_cannot_name_a_fictitious_executor_and_verify_its_own_work(
        gov):
    """THE SEPARATION-OF-DUTIES BYPASS, found by differential comparison.

    Separation of duties is checked against ``task.executed_by``, and the
    projection used to take that from a transition PAYLOAD -- a field written
    by the same actor. So the worker holding the lease could complete its own
    task while naming a fictitious executor, and then verify it as VERIFIER.

    It worked. "An agent that verifies its own work has not verified
    anything" is the central claim of this package, and it was defeated by
    one string in a payload.

    Nothing found it by reading the code. The independent replay reads the
    executor from the EXECUTION record, the projection read it from the
    payload, and comparing them disagreed at exactly the prefix between the
    two -- with the bypass underneath.
    """
    run = _run(gov)
    recs = [json.loads(x) for x in gov.log.path.read_text().splitlines()]
    cut = next(i for i, r in enumerate(recs)
               if r["action"] == "task.execution") + 1

    forged = ROOT / WS / "forged"
    if forged.exists():
        shutil.rmtree(forged)
    forged.mkdir(parents=True)
    log2 = EventLog(forged / "log.jsonl")
    for r in recs[:cut]:
        log2.append(actor=r["actor"], action=r["action"], target=r["target"],
                    payload=r["payload"])
    lease = next(r["payload"]["lease"] for r in recs
                 if r["action"] == ACT_TASK_TRANSITION
                 and r["payload"]["dst"] == "LEASED")
    dg = next(r["payload"]["result_digest"] for r in recs
              if r["action"] == ACT_TASK_TRANSITION
              and r["payload"]["dst"] == "COMPLETED")
    log2.append(actor=WORKER_ID, action=ACT_TASK_TRANSITION,
                target=run.task_id,
                payload={"task_id": run.task_id, "src": "EXECUTING",
                         "dst": "COMPLETED", "role": "WORKER",
                         "lease_id": lease["lease_id"],
                         "executed_by": "ghost-executor",
                         "result_digest": dg})
    try:
        g2 = GovernedStage10(root=ROOT, log=log2, evidence=gov.evidence)
        with pytest.raises(TaskTransitionError, match="execution record says"):
            g2.projection()
    finally:
        shutil.rmtree(forged)


def test_the_projection_learns_the_executor_from_the_execution_record(gov):
    """Stated as the property, not as one attack."""
    run = _run(gov)
    execs = [ev for ev in gov.log.read() if ev.action == "task.execution"]
    assert len(execs) == 1
    assert gov.projection().tasks[run.task_id].executed_by == execs[0].actor


def test_the_replay_reads_src_from_itself_not_from_the_record(gov):
    """Stated as the property rather than as one attack.

    scheduler.apply and reconstruct.reconstruct both already re-authorize
    from the state THEY replayed. This projection was the odd one out, and
    it is the one on the production path.
    """
    src = (ROOT / "qta_agent" / "governed_stage10.py").read_text(
        encoding="utf-8")
    rule = src.split("elif ev.action == ACT_TASK_TRANSITION:", 1)[1]
    rule = rule.split("elif ev.action in", 1)[0]
    assert "src=task.state" in rule, (
        "the replay must build its request from the state it replayed; "
        "re-authorizing against a src the writer supplied authorizes nothing")
    assert "TaskState(p[\"src\"])" in rule, (
        "the record's claim must still be READ, so a disagreement can be "
        "reported rather than silently corrected")


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
    ok, why = gov._verify_artifacts(
        run.artifacts, task_id=run.task_id,
        capability_id=_read_cap(gov, run.task_id))
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
    # TWO grants, and the split is the point: the worker may EXECUTE, the
    # verifier may READ. One grant covering both would mean the executor's
    # authority is what lets its own work be checked.
    assert len(issued) == 2, [i["action"] for i in issued]
    execute = [i for i in issued if i["action"] == "EXECUTE_TOOL"]
    reads = [i for i in issued if i["action"] == "READ_PATHS"]
    assert len(execute) == 1 and len(reads) == 1

    spec = stage10_registry().get("stage10.emit_artifact")
    assert tuple(execute[0]["scope"]) == tuple(spec.writable_scope), (
        f"grant scope {execute[0]['scope']} != declared writable scope "
        f"{list(spec.writable_scope)}")
    assert execute[0]["tool_id"] == "stage10.emit_artifact"
    assert execute[0]["subject"] == WORKER_ID
    assert reads[0]["subject"] == VERIFIER_ID, (
        "the read grant belongs to the verifier, not to the executor")


def test_a_timed_out_run_never_reaches_completed_or_verified(gov):
    """G5, deterministically: a non-COMPLETED outcome stops the chain.

    The earlier attempt at this relied on a tool FAILING because its output
    directory was refused. That killed the mutation locally and SURVIVED in
    hosted CI -- an environment-dependent kill, which is not a kill at all. It
    is the same class of mistake as counting a harness timeout: the mutation
    died for a reason the fixture did not control.

    A one-millisecond wall-clock bound is controlled. No Python interpreter
    starts in a millisecond, on any runner, so the outcome is TIMED_OUT every
    time -- and the assertion is on the LOG, which must contain no COMPLETED
    and no VERIFIED transition. With the early return removed, the run would
    proceed to evidence capture and verification, and those records would
    appear.
    """
    from qta_agent.tools import Determinism, Field_, Registry, ToolSpec

    slow = ToolSpec(
        tool_id="stage10.emit_artifact", version="1.0.0",
        summary="the same tool under a bound nothing can meet",
        inputs=(Field_("out_dir", "str"), Field_("name", "str"),
                Field_("payload", "dict")),
        outputs=(Field_("path", "str"), Field_("sha256", "str")),
        determinism=Determinism.BYTE_IDENTICAL,
        writable_scope=("verification/stage10",), timeout_s=0.001)
    gov.registry = Registry([slow])
    from qta_agent.execution import Executor
    gov.executor = Executor(gov.registry, workspace=ROOT)

    run = _run(gov)
    assert run.state is TaskState.TIMED_OUT, (
        f"a run that never finished ended {run.state.value}")
    assert run.outcome == "TIMED_OUT"
    assert not run.artifacts

    moves = [(e.payload["src"], e.payload["dst"]) for e in gov.log.read()
             if e.action == ACT_TASK_TRANSITION]
    assert ("EXECUTING", "COMPLETED") not in moves, (
        "a timed-out run was recorded as completed")
    assert not any(dst == "VERIFIED" for _, dst in moves), (
        "a timed-out run reached VERIFIED")
    assert ("EXECUTING", "TIMED_OUT") in moves
    assert gov.log.verify().ok


# ---- the integrations, each shown to be load-bearing ---------------------
def test_one_log_carries_every_subsystem_of_the_run(gov):
    """The premise, on the production path rather than in a test fixture."""
    run = _run(gov)
    assert run.state is TaskState.VERIFIED
    seen = {ev.action for ev in gov.log.read()}
    for action in ("policy.publish", "policy.decision", "agent.register",
                   "scheduler.enqueue", "scheduler.transition",
                   "task.create", "capability.issue", "task.execution",
                   "task.evidence", "context.build", "memory.write"):
        assert action in seen, f"{action} is missing from the run's history"
    assert gov.log.verify().ok


def test_the_policy_decision_is_recorded_and_names_its_document(gov):
    run = _run(gov)
    assert run.policy_identity == "stage10.governed@1"
    assert run.policy_digest == gov.policy.in_force(
        "stage10.governed").digest()
    (ev,) = [e for e in gov.log.read() if e.action == "policy.decision"]
    assert ev.payload["decision"]["allowed"] is True
    assert ev.payload["decision"]["rule_id"] == "allow-governed-stage10"


def test_a_policy_that_denies_stops_the_run_before_any_task_exists(gov):
    """Load-bearing, not decorative: denial ends the run."""
    from qta_agent.policy import Effect, PolicyDenied, document, rule

    gov.policy.publish(document(
        policy_id="stage10.governed", version=2,
        rules=(rule(rule_id="halt", effect=Effect.DENY, actions=("*",),
                    subjects=("*",), roles=("*",), resources=("*",),
                    reason="this path is closed"),)), actor="owner")
    with pytest.raises(PolicyDenied, match="this path is closed"):
        _run(gov)
    assert _task_events(gov) == []


def test_the_work_really_went_through_the_scheduler(gov):
    from qta_agent.scheduler import JobState

    run = _run(gov)
    assert run.job_id and run.job_state == JobState.SUCCEEDED.value
    job = gov.scheduler.get(run.job_id)
    assert job.task_id == run.task_id
    assert job.attempts == 1
    assert job.lease_holder is None, (
        "a finished job must not still name an owner")


def test_a_failed_run_is_classified_by_the_scheduler_not_beside_it(gov):
    from qta_agent.scheduler import JobState

    run = _run(gov, inputs=_inputs(gov, name="../escape.json"))
    assert run.state in (TaskState.FAILED, TaskState.REJECTED)
    if run.job_id:
        assert gov.scheduler.get(run.job_id).state is JobState.FAILED


def test_the_context_manifest_is_recorded_and_holds_no_prompt(gov):
    from qta_agent.context import Tier, manifest_from_record

    run = _run(gov)
    (ev,) = [e for e in gov.log.read() if e.action == "context.build"]
    manifest = manifest_from_record(ev.payload["manifest"])
    assert manifest.digest() == run.context_digest
    assert manifest.policy_identity == run.policy_identity
    assert {i.tier for i in manifest.items} >= {
        Tier.OWNER_INSTRUCTION, Tier.SYSTEM_POLICY, Tier.TASK_STATE,
        Tier.TASK_EVIDENCE}
    flat = str(ev.payload)
    assert "touch no gate" not in flat, (
        "the manifest records digests and identities; storing the assembled "
        "text would put it in the authority log forever")


def test_the_run_files_a_note_that_cannot_become_evidence(gov):
    run = _run(gov)
    entry = gov.memory.get(run.memory_id)
    assert set(entry.derived_from) == set(run.artifacts.values())
    assert not gov.evidence.contains(entry.digest()), (
        "a remembered note whose digest resolved as evidence could support a "
        "transition; nothing checked it")
    assert "says nothing about scientific validity" in entry.text


def test_an_unregistered_worker_cannot_be_used(gov):
    from qta_agent.agents import IdentityError

    with pytest.raises(IdentityError, match="not registered"):
        _run(gov, worker="ghost-worker")
    assert _task_events(gov) == []


def test_an_actor_without_the_role_cannot_take_it(gov):
    from qta_agent.agents import IdentityError

    with pytest.raises(IdentityError, match="may not act as"):
        _run(gov, worker=VERIFIER_ID, verifier=WORKER_ID)


def test_the_run_holds_no_egress_grant_at_all(gov):
    """Default deny, on the real path: nothing here needs the network."""
    from qta_agent.netauth import NetworkRequest, parse_target

    _run(gov)
    assert not [e for e in gov.log.read() if e.action == "network.grant"]
    decision = gov.network.authorize(NetworkRequest(
        actor=WORKER_ID, task_id="t", tool_id="stage10.emit_artifact",
        target=parse_target("https://example.com/x")))
    assert decision.allowed is False
    assert "default is no network" in decision.reason


def test_execution_runs_inside_the_network_guard(gov, monkeypatch):
    """The guard is applied, not merely available.

    Asserted by attempting a connection from inside the executor call, which
    is exactly where a dependency that phones home would attempt one.
    """
    import socket

    from qta_agent.execution import Executor
    from qta_agent.netauth import GuardedConnection

    attempted = {}
    real_run = Executor.run

    def probing_run(self, **kw):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("93.184.216.34", 443))
        except GuardedConnection as exc:
            attempted["refused"] = str(exc)
        finally:
            s.close()
        return real_run(self, **kw)

    monkeypatch.setattr(Executor, "run", probing_run)
    run = _run(gov)
    assert run.state is TaskState.VERIFIED
    assert "refused" in attempted, (
        "a connection opened during execution was not refused; the guard is "
        "not on the path")
    assert "not authorized" in attempted["refused"]


def test_the_bootstrap_is_idempotent_across_runs(gov):
    """A governed run may be the first thing to touch a log, or the tenth."""
    _run(gov)
    versions = gov.policy.versions("stage10.governed")
    registrations = [e for e in gov.log.read()
                     if e.action == "agent.register"]
    _run(gov, inputs=_inputs(gov, name="second.json"))
    assert gov.policy.versions("stage10.governed") == versions
    assert len([e for e in gov.log.read()
                if e.action == "agent.register"]) == len(registrations)


def test_the_tool_inherits_nothing_from_the_parent_environment(gov,
                                                               monkeypatch):
    """The strongest secret handling available on this path.

    A credential in ``os.environ`` cannot reach a governed tool, because the
    tool's environment is BUILT rather than inherited. That is why this path
    needs no secret grants: there is nothing to grant access to.
    """
    from qta_agent.governed_stage10 import GOVERNED_ENV_KEYS

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "a-credential-in-the-parent")
    monkeypatch.setenv("GITHUB_TOKEN", "another-one")

    seen = {}
    from qta_agent.execution import Executor
    real_run = Executor.run

    def capturing_run(self, **kw):
        seen["env"] = dict(kw["env"])
        return real_run(self, **kw)

    monkeypatch.setattr(Executor, "run", capturing_run)
    run = _run(gov)
    assert run.state is TaskState.VERIFIED
    assert set(seen["env"]) == set(GOVERNED_ENV_KEYS)
    flat = " ".join(f"{k}={v}" for k, v in seen["env"].items())
    assert "a-credential-in-the-parent" not in flat
    assert "another-one" not in flat


def test_the_governed_environment_allowlist_is_enforced_not_described(gov):
    """The check is on the dict that is about to be passed, not on a comment.

    A variable added to the builder without being added to the allowlist
    fails here rather than reaching a tool.
    """
    env = gov._tool_environment()
    from qta_agent.governed_stage10 import GOVERNED_ENV_KEYS
    assert set(env) == set(GOVERNED_ENV_KEYS)
    assert env["OPENBLAS_NUM_THREADS"] == "1"


def test_the_executor_checks_against_the_log_not_the_caller_s_own_set(gov):
    """A grant that was never recorded does not exist.

    The governed path used to append an issuance event AND separately build
    ``CapabilitySet(issued={cap_id: cap})`` from the same local variable. The
    event was decorative: the executor checked against whatever the caller
    had in hand, so a caller that skipped the append would have been
    authorized anyway. The set is now projected from the log.
    """
    from qta_agent.capability import ACT_ISSUE, CapabilityLedger

    run = _run(gov)
    assert run.state is TaskState.VERIFIED
    evs = [e for e in gov.log.read() if e.action == ACT_ISSUE]
    issued = CapabilityLedger(gov.log).load()
    assert set(issued.issued_ids()) == {
        e.payload["capability_id"] for e in evs}

    live = issued.in_force(gov.log.verify().head_seq)
    assert set(live.issued) == set(issued.issued_ids())
    assert live.revoked == frozenset()


def test_a_capability_the_log_never_recorded_authorizes_nothing(gov,
                                                                monkeypatch):
    """The failure mode the projection exists to prevent.

    The denial surfaces as an execution OUTCOME rather than an exception --
    ``DENIED`` is one of the outcomes the executor is built to return, so the
    refusal is recorded rather than thrown away. What matters is that the run
    does not reach VERIFIED and that no process was started.
    """
    from qta_agent.capability import CapabilityLedger

    def skip_the_record(self, cap, *, actor):
        # The caller mints a grant and never records it in the log.
        return cap

    monkeypatch.setattr(CapabilityLedger, "issue", skip_the_record)
    run = _run(gov)
    assert run.state is not TaskState.VERIFIED
    assert run.outcome == "DENIED", run.reason
    assert "was ever issued" in run.reason, run.reason
    assert run.artifacts == {}


def test_a_revoked_capability_stops_authorizing_without_the_caller_s_help(gov):
    from qta_agent.capability import CapabilityRevoked, Request, Action

    run = _run(gov)
    cap_id = next(e.payload["capability_id"] for e in gov.log.read()
                  if e.action == "capability.issue"
                  and e.payload["action"] == "EXECUTE_TOOL")
    gov.capabilities.revoke(cap_id, actor="owner", reason="rotated")

    live = gov.capabilities.in_force(gov.log.verify().head_seq)
    with pytest.raises(CapabilityRevoked):
        live.check(cap_id, Request(actor=WORKER_ID,
                                   action=Action.EXECUTE_TOOL,
                                   task_id=run.task_id,
                                   tool_id="stage10.emit_artifact",
                                   paths=("verification/stage10",)))


def test_two_grants_cannot_share_an_id_in_the_log(gov, tmp_path):
    from qta_agent.capability import (
        ACT_ISSUE, Action, CapabilityError, CapabilityLedger, issue,
    )

    _run(gov)
    cap_id = gov.capabilities.issued_ids()[0]
    other = issue(capability_id=cap_id, subject="mallory",
                  action=Action.WRITE_PATHS, task_id="t",
                  scope=("verification/stage10",), issued_seq=0)
    gov.log.append(actor="mallory", action=ACT_ISSUE, target="t",
                   payload={"task_id": "t", **other.body()})
    with pytest.raises(CapabilityError, match="issued twice"):
        CapabilityLedger(gov.log).load()


def test_the_executor_and_src_fallbacks_are_unreachable_differences():
    """Why two mutations were REMOVED from the spec rather than left surviving.

    The projection writes ``src=task.state`` and
    ``executed_by=task.executed_by or claimed_by``. Reversing either operand
    order changes nothing, because the guards immediately above refuse any
    transition whose claim disagrees with the replay -- so by the time the
    expression runs, the operands are equal.

    An equivalent mutation surviving is not evidence of an unprotected check,
    and leaving one in the matrix would be a permanent false finding. This
    enumerates the combinations so the claim is CHECKED rather than asserted
    in a spec comment nobody re-derives.
    """
    differing = []
    for recorded in (None, "worker", "ghost"):
        for claimed in (None, "worker", "ghost"):
            if claimed and recorded and claimed != recorded:
                continue                    # refused before either is read
            if (recorded or claimed) != (claimed or recorded):
                differing.append((recorded, claimed))
    assert not differing, differing

    # And the guard that makes them unreachable is really there.
    src = (ROOT / "qta_agent" / "governed_stage10.py").read_text(
        encoding="utf-8")
    # Matched on fragments that survive the f-string line breaks: the first
    # version of this check searched for a sentence the source only contains
    # across two lines, and failed for a guard that was present.
    assert "claimed_by != task.executed_by" in src
    assert "task.state is not claimed" in src


# --- R31: verification reads through the governed boundary ------------------

def test_verification_reads_are_recorded_with_what_was_opened(gov):
    """A read that decides whether work is VERIFIED is worth an audit trail."""
    from qta_agent.readpath import ACT_FILE_READ

    run = _run(gov)
    reads = [e for e in gov.log.read() if e.action == ACT_FILE_READ]
    assert reads, "verification read nothing through the governed boundary"
    for ev in reads:
        p = ev.payload
        assert p["allowed"] is True
        assert p["request"]["purpose"] == "verification"
        assert p["request"]["actor"] == VERIFIER_ID
        assert p["result"]["identity"]["inode"] > 0
    digests = {e.payload["result"]["digest"] for e in reads}
    assert digests == set(run.artifacts.values()), (
        "the bytes verification read are not the bytes the task cited")


def test_a_verifier_without_a_read_grant_cannot_verify(gov):
    """Default deny reaches the verification step too.

    Verification is a read, and a read nobody authorized is not a check --
    it is the verifier's own judgement that it was allowed.
    """
    run = _run(gov)
    ok, why = gov._verify_artifacts(
        run.artifacts, task_id=run.task_id, capability_id="cap-not-issued")
    assert not ok
    assert "not authorized" in why


def test_an_artifact_replaced_by_a_symlink_fails_verification(gov):
    """The artifact is swapped for a link to bytes that hash correctly.

    Without the read boundary this passed: the link was followed, the target
    hashed to the cited digest, and verification confirmed an artifact that
    was no longer in the workspace at all.
    """
    import os

    run = _run(gov)
    rel = next(iter(run.artifacts))
    path = ROOT / rel
    decoy = path.parent / "decoy.json"
    decoy.write_bytes(path.read_bytes())          # identical content
    os.unlink(path)
    os.symlink(decoy, path)
    try:
        ok, why = gov._verify_artifacts(
            run.artifacts, task_id=run.task_id,
            capability_id=_read_cap(gov, run.task_id))
        assert not ok
        assert "safely" in why or "symbolic link" in why
    finally:
        os.unlink(path)
        os.replace(decoy, path)


def test_an_artifact_replaced_by_a_fifo_fails_instead_of_hanging(gov):
    """Verification must not be stoppable by substituting a named pipe."""
    import os
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "tests"))
    from hangguard import deadline

    run = _run(gov)
    rel = next(iter(run.artifacts))
    path = ROOT / rel
    saved = path.read_bytes()
    os.unlink(path)
    os.mkfifo(path)
    try:
        with deadline(10.0):
            ok, why = gov._verify_artifacts(
                run.artifacts, task_id=run.task_id,
                capability_id=_read_cap(gov, run.task_id))
        assert not ok
        assert "safely" in why
    finally:
        os.unlink(path)
        path.write_bytes(saved)


def test_the_read_grant_does_not_authorize_writing(gov):
    """Separate authorities, asserted rather than assumed."""
    from qta_agent.capability import Action, CapabilityDenied, Request

    run = _run(gov)
    read_cap = next(e.payload["capability_id"] for e in gov.log.read()
                    if e.action == "capability.issue"
                    and e.payload["action"] == "READ_PATHS")
    live = gov.capabilities.in_force(gov.log.verify().head_seq)
    with pytest.raises(CapabilityDenied):
        live.check(read_cap, Request(
            actor=VERIFIER_ID, action=Action.WRITE_PATHS,
            task_id=run.task_id, paths=("verification/stage10/x",)))


def test_an_artifact_with_a_second_hard_link_fails_verification(gov):
    """A governed artifact was written once and should have one name.

    A second hard link is another way to reach the same bytes, and it can
    live outside the workspace entirely -- so the content verification
    confirms is reachable by a name no authority ever saw.
    """
    import os

    run = _run(gov)
    rel = next(iter(run.artifacts))
    path = ROOT / rel
    alias = path.parent / "alias.json"
    os.link(path, alias)
    try:
        ok, why = gov._verify_artifacts(
            run.artifacts, task_id=run.task_id,
            capability_id=_read_cap(gov, run.task_id))
        assert not ok
        assert "names" in why or "safely" in why
    finally:
        os.unlink(alias)
