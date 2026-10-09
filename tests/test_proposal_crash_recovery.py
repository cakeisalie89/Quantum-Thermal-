"""Crash and recovery around the assembled proposal-to-decision path
(section 44; D-2026-110).

The real path runs once, in a process of its own that then exits: a recorded
AI proposal received, submitted as a governed model run keyed by its id,
independently checked, decided by a reviewer. Its log is then cut after
every append the path makes -- proposal accepted, task created and bound,
queued, dispatched, leased, computation started, artifact written, evidence
captured, completed, verified, recorded, the check, the decision's two
appends -- with the head witness rewritten to agree, which is what a crash
right after that append leaves. The process that held the lease is gone, as
it would be. For each cut the state is rebuilt from the log alone and the
path is resumed by doing what an operator does: submit the same proposal
again.

The recoverable state at every cut, and what recovery must never do:

* the rebuilt state is never MORE advanced than the cut: no record, and no
  decision, that is not in it;
* a durable proposal is never lost: once its receipt is in the log, the
  ingress knows it;
* the path finishes, at the uncrashed run's verdict (REJECTED: the series
  check is not an admitted independent check);
* nothing is duplicated: one receipt, one executed model task, one result
  record, one pickup, one verdict;
* verification is never skipped: the model task's verdict is a different
  actor's, the record's decision cites a check report that exists, and the
  second reader replays the whole history without an anomaly.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import harness_demo as HD  # noqa: E402
from qta_agent import proposals as PR  # noqa: E402
from qta_agent.authority import State  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.reconstruct import reconstruct, reconstruct_tasks  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402

WS = "verification/stage10/_pytest_proposal_crash"
KEEP = ROOT / "verification" / "stage10" / "_pytest_proposal_crash_ref"
MODEL_TOOL = "model.thermal.slab_transient.run"

_REFERENCE = f"""
import json, sys
sys.path[:0] = [{str(ROOT)!r}, {str(ROOT / "tools")!r}]
import harness_demo as HD
from pathlib import Path
from qta_agent import proposals as PR
from qta_multiphysics.stack.rag_index import retrieve
HD.WS = {WS!r}
log, ev, g = HD._world(Path({str(ROOT / WS)!r}))
ctx = PR.assemble_context(HD.QUERY, retrieve=retrieve, k=3)
ing, receipt = HD._receive(log, ctx, 0)
pid = receipt.envelope.proposal_id
run = ing.submit(pid, g, out_dir={WS + "/slab"!r})
chk = g.check(run, check_id="thermal.slab_series",
              out_dir={WS + "/slab_series"!r})
rec = g.decide(run, chk)
print(json.dumps({{"pid": pid, "record_id": run.record_id,
                  "task_id": run.governed.task_id,
                  "final": rec.state.value}}))
"""


def _path(log, g, ctx):
    """The path, end to end, as an operator re-runs it after a crash."""
    ing, receipt = HD._receive(log, ctx, 0)
    pid = receipt.envelope.proposal_id
    run = ing.submit(pid, g, out_dir=f"{WS}/slab")
    chk = g.check(run, check_id="thermal.slab_series",
                  out_dir=f"{WS}/slab_series")
    rec = g.decide(run, chk)
    return pid, run, chk, rec


@pytest.fixture(scope="module")
def reference():
    from qta_multiphysics.stack.rag_index import retrieve
    HD.WS = WS
    out = subprocess.run([sys.executable, "-c", _REFERENCE], cwd=ROOT,
                         capture_output=True, text=True, timeout=900)
    assert out.returncode == 0, out.stderr[-4000:]
    ref = json.loads(out.stdout.strip().splitlines()[-1])
    base = ROOT / WS
    log = EventLog(base / "log.jsonl")
    _, events = log.read_verified()
    if KEEP.exists():
        shutil.rmtree(KEEP)
    shutil.copytree(base, KEEP)
    ref.update(
        ctx=PR.assemble_context(HD.QUERY, retrieve=retrieve, k=3),
        events=events,
        lines=log.path.read_text(encoding="utf-8").splitlines(keepends=True))
    return ref


def _first(events, pred, nth=0):
    return [e.seq for e in events if pred(e)][nth]


def _move(task_id, dst):
    return lambda e: (e.action == "task.transition"
                      and e.payload.get("task_id") == task_id
                      and e.payload.get("dst") == dst)


def _model(ref):
    return ref["task_id"]


#: (name, which append the crash follows)
STAGES = {
    "proposal accepted":
        lambda r, ev: _first(ev, lambda e: e.action == "proposal.receive"),
    "task created, not yet bound":
        lambda r, ev: _first(ev, lambda e: e.action == "task.create"),
    "task bound to the proposal":
        lambda r, ev: _first(ev, lambda e: e.action == "idempotency.bind"),
    "task validated": lambda r, ev: _first(ev, _move(_model(r), "VALIDATED")),
    "job enqueued":
        lambda r, ev: _first(ev, lambda e: e.action == "scheduler.enqueue"),
    "task queued": lambda r, ev: _first(ev, _move(_model(r), "QUEUED")),
    "job dispatched, task not yet leased":
        lambda r, ev: _first(ev, lambda e: e.action == "scheduler.transition"
                             and e.payload.get("dst") == "DISPATCHED"),
    "lease issued": lambda r, ev: _first(ev, _move(_model(r), "LEASED")),
    "context built":
        lambda r, ev: _first(ev, lambda e: e.action == "context.build"),
    "computation started":
        lambda r, ev: _first(ev, _move(_model(r), "EXECUTING")),
    "artifact written":
        lambda r, ev: _first(ev, lambda e: e.action == "task.execution"),
    "evidence captured":
        lambda r, ev: _first(ev, lambda e: e.action == "task.evidence"),
    "computation completed":
        lambda r, ev: _first(ev, _move(_model(r), "COMPLETED")),
    "re-executed by the verifier":
        lambda r, ev: _first(ev, lambda e: e.action == "task.reexecution"),
    "verification completed":
        lambda r, ev: _first(ev, _move(_model(r), "VERIFIED")),
    "job outcome reported":
        lambda r, ev: _first(ev, lambda e: e.action == "scheduler.transition"
                             and e.payload.get("dst") == "SUCCEEDED"),
    "result recorded":
        lambda r, ev: _first(ev, lambda e: e.action == "record.create"),
    "check verified":
        lambda r, ev: _first(ev, lambda e: e.action == "task.transition"
                             and e.payload.get("dst") == "VERIFIED", nth=1),
    "decision picked up":
        lambda r, ev: _first(ev, lambda e: e.action == "record.transition"
                             and e.payload.get("dst") == "UNDER_REVIEW"),
    "decision appended":
        lambda r, ev: _first(ev, lambda e: e.action == "record.transition"
                             and e.payload.get("dst") == r["final"]),
}


def _crash_at(ref, upto: int):
    """The workspace a crash right after event ``upto`` leaves: the log to
    that event, a head witness that agrees, every evidence blob (a blob
    stored before the record citing it is harmless: nothing cites it), and
    the model's outputs once the execution that wrote them is in the log."""
    from qta_agent.evidence import EvidenceStore
    from qta_agent.governed_model import GovernedModelRuns
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    log_path = base / "log.jsonl"
    log_path.write_text("".join(ref["lines"][:upto + 1]), encoding="utf-8")
    last = json.loads(ref["lines"][upto])
    EventLog(log_path).head_path.write_text(json.dumps(
        {"seq": last["seq"], "head_hash": last["hash"]}), encoding="utf-8")
    shutil.copytree(KEEP / "evidence", base / "evidence")
    if upto >= _first(ref["events"], lambda e: e.action == "task.execution"):
        shutil.copytree(KEEP / "slab", base / "slab")
    log = EventLog(log_path)
    ev = EvidenceStore(base / "evidence")
    return log, ev, GovernedModelRuns(root=ROOT, log=log, evidence=ev)


def _executions(events) -> dict:
    """task_id -> how many times a task running the model was executed."""
    model = {e.payload["task_id"] for e in events
             if e.action == "task.create"
             and e.payload.get("tool_id") == MODEL_TOOL}
    out: dict = {}
    for e in events:
        if e.action == "task.execution" and e.payload["task_id"] in model:
            out[e.payload["task_id"]] = out.get(e.payload["task_id"], 0) + 1
    return out


@pytest.mark.parametrize("stage", list(STAGES))
def test_recovery_from_each_stage_finishes_once_and_verified(reference,
                                                             stage):
    ref = reference
    upto = STAGES[stage](ref, ref["events"])
    log, ev, g = _crash_at(ref, upto)
    _, before = log.read_verified()

    # the rebuilt state is the cut's and no more
    store = AuthorityStore(log, evidence=ev).load()
    created = [e.target for e in before if e.action == "record.create"]
    assert sorted(store.all_records()) == sorted(created)
    decided = [e for e in before if e.action == "record.transition"
               and e.payload.get("dst") in ("VERIFIED", "REJECTED")]
    for rid in created:
        assert store.get(rid).state.value in (
            ("PROPOSED", "UNDER_REVIEW") if not decided else (ref["final"],))
    # a durable proposal is never lost
    assert ref["pid"] in PR.received(log)

    pid, run, chk, rec = _path(log, g, ref["ctx"])
    assert pid == ref["pid"]
    assert rec.state is State(ref["final"])
    bound = any(e.action == "idempotency.bind" for e in before)
    if bound:
        # the task the proposal was bound to is the one that finished
        assert run.governed.task_id == ref["task_id"]
        assert run.record_id == ref["record_id"]

    _, after = log.read_verified()
    rid = run.record_id
    assert sum(e.action == "proposal.receive" for e in after) == 1
    assert [e.target for e in after if e.action == "record.create"] == [rid]
    picks = [e for e in after if e.action == "record.transition"
             and e.payload.get("dst") == "UNDER_REVIEW"]
    verdicts = [e for e in after if e.action == "record.transition"
                and e.payload.get("dst") in ("VERIFIED", "REJECTED")]
    assert len(picks) == 1 and len(verdicts) == 1
    # one model task executed: once, or -- when the crash fell between its
    # execution record and its completion, so the attempt was taken up
    # again -- twice, the first record standing as evidence of the attempt
    # that died. A task orphaned before its binding is never executed.
    ev_ = ref["events"]
    executed = _first(ev_, lambda e: e.action == "task.execution")
    completed = _first(ev_, _move(_model(ref), "COMPLETED"))
    assert _executions(after) == {
        run.governed.task_id: 2 if executed <= upto < completed else 1}
    orphans = {e.payload["task_id"] for e in after
               if e.action == "task.create"} - {
        run.governed.task_id} - {e.payload["task_id"] for e in after
                                 if e.action == "task.create"
                                 and e.payload.get("tool_id") != MODEL_TOOL}
    tasks = reconstruct_tasks(log)
    for tid in orphans:
        assert tasks.tasks[tid]["state"] == "CREATED"
    assert len(orphans) == (1 if stage == "task created, not yet bound"
                            else 0)
    # verification was not skipped: the model task's verdict is the
    # verifier's, and the decision cites a report that exists
    assert tasks.tasks[run.governed.task_id]["state"] == "VERIFIED"
    final = AuthorityStore(log, evidence=ev).load().get(rid)
    cited = final.evidence["verification_report"]
    assert cited == chk.report_sha256 and ev.get(cited)
    recon = reconstruct(log)
    assert recon.unauthorized == [] and recon.anomalies == []
    assert tasks.unauthorized == [] and tasks.anomalies == []


def _register_executor(g, name: str) -> None:
    from qta_agent.agents import AgentRole, PrincipalKind, identity
    g.gov.agents.register(identity(agent_id=name, instance_id=name,
                                   kind=PrincipalKind.AGENT,
                                   roles={AgentRole.EXECUTOR}), by="system")


def test_another_workers_resubmission_does_not_take_over_a_live_lease(
        reference):
    """The takeover is the same worker identity resuming the attempt its
    own dispatch began. A different worker resubmitting the same proposal
    finds the job leased to someone else, live, and is told so: it runs
    nothing, leases nothing, and the rightful resubmission still finishes."""
    from qta_agent.governed_model import ModelRunRefused
    ref = reference
    log, ev, g = _crash_at(ref, STAGES["lease issued"](ref, ref["events"]))
    _register_executor(g, "another-worker")
    env = PR.received(log)[ref["pid"]]["envelope"]
    req = env.request
    _, before = log.read_verified()
    with pytest.raises(ModelRunRefused, match="QUEUED"):
        g.propose(model_id=req["model_id"],
                  model_version=req["model_version"],
                  parameters=req["parameters"], out_dir=f"{WS}/slab",
                  submitter=env.agent_id, reuse=False,
                  idempotency_key=ref["pid"], worker="another-worker")
    _, after = log.read_verified()
    added = after[len(before):]
    assert not any(e.actor == "another-worker" for e in added)
    assert not any(e.action in ("task.execution", "scheduler.transition")
                   for e in added)
    _, run, _, rec = _path(log, g, ref["ctx"])
    assert run.governed.task_id == ref["task_id"]
    assert rec.state is State(ref["final"])


def test_a_completed_task_whose_records_disagree_is_never_verified(
        reference):
    """A COMPLETED task is verified on resume from what its attempt
    recorded, so the records must agree as the attempt required before it
    completed. Forged here by a writer that skipped that check -- the output
    changed after the execution record, the capture took the changed bytes,
    and the task was completed anyway: the re-execution reproduces the
    declared digest and the disk matches the capture, so only the agreement
    check stands between this and a VERIFIED verdict. It must stay
    COMPLETED and be reported, not verified."""
    from qta_agent.governed_model import ModelRunRefused
    from qta_agent.tasks import TaskRole, TaskState
    ref = reference
    log, ev, g = _crash_at(ref, STAGES["artifact written"](ref, ref["events"]))
    tid = ref["task_id"]
    task = g.gov.projection().get(tid)
    ran = g.gov._execution_record(tid)
    field = ROOT / WS / "slab" / "temperature_field.bin"
    field.write_bytes(field.read_bytes() + b"\0")
    g.gov._capture(ROOT / WS / "slab", tid)
    g.gov._move(task, TaskState.COMPLETED, task.lease.holder,
                TaskRole.WORKER, lease_id=task.lease.lease_id,
                executed_by=task.lease.holder,
                result_digest=ran["result_digest"])
    with pytest.raises(ModelRunRefused, match="COMPLETED"):
        _path(log, g, ref["ctx"])
    _, after = log.read_verified()
    assert not any(e.action == "task.transition"
                   and e.payload.get("task_id") == tid
                   and e.payload.get("dst") in ("VERIFIED", "REJECTED")
                   for e in after)
    assert not any(e.action == "record.create" for e in after)


@pytest.mark.parametrize("crash", ["before the file", "torn temp file",
                                   "file cut short", "no crash"])
def test_a_crash_while_checkpointing_the_projection_recovers(reference,
                                                              crash):
    """The status projection's update: the snapshot blob, then the
    ``checkpoint.state`` claim in the log, then the file. A crash anywhere in
    that leaves a restart that either uses a whole checkpoint or replays the
    log in full -- and agrees with a full replay either way -- and resuming
    the path afterwards decides nothing a second time."""
    from qta_agent.checkpoint import CheckpointStore
    from qta_agent.governed_model import GovernedModelRuns
    ref = reference
    log, ev, g = _crash_at(ref, len(ref["lines"]) - 1)
    cps = CheckpointStore(ROOT / WS / "checkpoints")
    if crash in ("before the file", "torn temp file"):
        real = cps.write

        def dies(cp):
            if crash == "torn temp file":
                tmp = cps.root / ".tmp-crashed"
                tmp.write_bytes(real(cp).read_bytes()[:40])
                for p in cps.root.glob("*.json"):
                    p.unlink()
            raise SystemExit("crashed while writing the checkpoint")
        cps.write = dies
        with pytest.raises(SystemExit):
            g.checkpoint(cps)
        cps = CheckpointStore(ROOT / WS / "checkpoints")
    else:
        cp = g.checkpoint(cps)
        if crash == "file cut short":
            path = cps._path(cp.seq)
            path.write_bytes(path.read_bytes()[:len(path.read_bytes()) // 2])
    again = GovernedModelRuns(root=ROOT, log=log, evidence=ev,
                              checkpoints=cps)
    _, cmp = AuthorityStore.recover_and_compare(
        log, cps, blobs=ev, evidence=ev, origins=again.origins)
    assert cmp["agrees"], cmp
    assert again.recovery["mode"] == (
        "CHECKPOINT_ASSISTED" if crash == "no crash" else "FULL_REPLAY")
    assert again.recovery["healthy"] is (crash != "file cut short")
    _, run, _, rec = _path(log, again, ref["ctx"])
    assert rec.state is State(ref["final"])
    _, after = log.read_verified()
    assert sum(e.action == "record.transition"
               and e.payload.get("dst") in ("VERIFIED", "REJECTED")
               for e in after) == 1
    recon = reconstruct(log)
    assert recon.unauthorized == [] and recon.anomalies == []


def test_a_resumed_attempt_runs_under_the_rules_in_force_when_it_resumes(
        reference):
    """The policy is asked again when a stranded task is taken up: a rule
    published between the crash and the resubmission governs the resumed
    attempt. Denied, it runs nothing -- no lease, no execution, no verdict --
    and the refusal is recorded like any decision."""
    from qta_agent.governed_stage10 import stage10_policy
    from qta_agent.policy import Effect, PolicyDenied, document, rule
    ref = reference
    log, ev, g = _crash_at(ref, STAGES["task queued"](ref, ref["events"]))
    # version 1's rules, and one more: this path is closed
    g.gov.policy.publish(document(
        policy_id="stage10.governed", version=2,
        rules=(rule(rule_id="halt", effect=Effect.DENY,
                    actions=("stage10.execute",), subjects=("*",),
                    roles=("*",), resources=("*",),
                    reason="this path is closed"),
               *stage10_policy().rules)), actor="owner")
    _, before = log.read_verified()
    with pytest.raises(PolicyDenied, match="this path is closed"):
        _path(log, g, ref["ctx"])
    _, after = log.read_verified()
    added = after[len(before):]
    assert [e.action for e in added] == ["policy.decision"]
    assert added[0].payload["decision"]["allowed"] is False
    assert g.gov.projection().get(ref["task_id"]).state.value == "QUEUED"


def test_a_lapsed_lease_is_taken_up_as_a_new_attempt(reference):
    """Long after the crash -- the job's lease has lapsed by the sequence
    numbers the log moved on -- the resubmission does not take the dead
    lease over: the queue is reconciled first, the job is READY again, and
    the work is dispatched under a new lease as a second attempt, counted
    against the job's retry budget."""
    from qta_agent.governed_stage10 import LEASE_SEQS, POLICY_ID
    from qta_agent.policy import PolicyRequest
    ref = reference
    log, ev, g = _crash_at(ref, STAGES["lease issued"](ref, ref["events"]))
    for i in range(LEASE_SEQS + 1):
        g.gov.policy.decide_and_record(
            POLICY_ID, PolicyRequest(action="stage10.execute",
                                     subject="stage10-submitter",
                                     role="SUBMITTER", resource="padding",
                                     task_id=""),
            actor="stage10-submitter", target=f"padding-{i}")
    _, run, _, rec = _path(log, g, ref["ctx"])
    assert run.governed.task_id == ref["task_id"]
    assert run.governed.resumed_from == "QUEUED"
    assert rec.state is State(ref["final"])
    assert g.gov.scheduler.get(run.governed.job_id).attempts == 2
    _, after = log.read_verified()
    leases = [e.payload["lease"]["lease_id"] for e in after
              if _move(ref["task_id"], "LEASED")(e)]
    assert len(leases) == 2 and leases[0] != leases[1]


def test_a_decision_is_never_returned_for_another_checks_report(reference):
    """A decided record answers a resumed decision only when it was decided
    on the same check's report. Asked to decide on another report it
    refuses: a decision on new evidence is a new record's, never a silent
    reuse of the old verdict."""
    import dataclasses

    from qta_agent.governed_model import ModelRunRefused
    ref = reference
    log, ev, g = _crash_at(ref, len(ref["lines"]) - 1)
    _, run, chk, rec = _path(log, g, ref["ctx"])
    other = dataclasses.replace(chk, report_sha256=run.bundle_sha256)
    with pytest.raises(ModelRunRefused, match="another check report"):
        g.decide(run, other)
