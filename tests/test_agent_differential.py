"""Two implementations, one log: do they agree?

WHY THIS IS A SEPARATE KIND OF EVIDENCE

A test asserts that one implementation does what its author expected. A
differential test asserts that two implementations, written separately and
sharing no reducer, reach the same verdict from the same bytes. The second
kind survives a shared misunderstanding that the first does not: an author who
misread the spec writes the test to match the code.

It also has a specific job here. The task projection is the one on the
PRODUCTION path, and it turned out to re-authorize forged records against a
starting state the record itself declared -- a hole that every one of its own
tests passed straight over. A second reader is the defence against that class,
not because it is more careful, but because two readers that disagree say so
while a single reader with a hole says nothing.

WHAT A DIVERGENCE MEANS, AND WHAT IT DOES NOT

An empty diff is evidence, not proof: both could share a mistake the log
cannot reveal. A non-empty diff is a finding one of the two has to answer for,
and it names which field disagrees rather than reporting "they differ".
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

from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_TASK_TRANSITION, SUBMITTER_ID, VERIFIER_ID, WORKER_ID,
    GovernedStage10,
)
from qta_agent.reconstruct import (  # noqa: E402
    compare_tasks, reconstruct_tasks,
)
from qta_agent.tasks import TaskState, TaskTransitionError  # noqa: E402

WS = "verification/stage10/_pytest_diff"


@pytest.fixture()
def gov(request):
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


def _run(gov, **over):
    kw = dict(tool_id="stage10.emit_artifact",
              inputs={"out_dir": gov.out_rel, "name": "a.json",
                      "payload": {"v": 1}},
              submitter=SUBMITTER_ID, worker=WORKER_ID, verifier=VERIFIER_ID)
    kw.update(over)
    return gov.run(**kw)


# --- agreement on a healthy history -----------------------------------------

def test_the_two_readers_agree_on_a_governed_run(gov):
    run = _run(gov)
    recon = reconstruct_tasks(gov.log)
    assert compare_tasks(gov.projection(), recon) == ()
    assert recon.verified_ids() == (run.task_id,)
    assert not recon.unauthorized and not recon.anomalies


def test_the_two_readers_agree_across_several_runs(gov):
    ids = []
    for i in range(3):
        ids.append(_run(gov, inputs={"out_dir": gov.out_rel,
                                     "name": f"a{i}.json",
                                     "payload": {"v": i}}).task_id)
    recon = reconstruct_tasks(gov.log)
    assert compare_tasks(gov.projection(), recon) == ()
    assert set(recon.verified_ids()) == set(ids)


def test_they_agree_on_a_run_that_was_rejected(gov):
    """Agreement on refusals matters more than agreement on successes.

    A success is the path both were written for. A rejection is where two
    readers most easily drift, because it is the branch nobody re-reads.
    """
    run = _run(gov, inputs={"out_dir": gov.out_rel, "name": "a.json",
                            "payload": "not-a-dict"})
    assert run.state is TaskState.REJECTED
    recon = reconstruct_tasks(gov.log)
    assert compare_tasks(gov.projection(), recon) == ()
    assert recon.states()[run.task_id] == TaskState.REJECTED.value


def test_the_reconstruction_shares_no_reducer_with_the_projection():
    """Guard against someone 'simplifying' the duplication away.

    Reusing the projection's reducer here would make every comparison in this
    file circular and worthless, while still passing.
    """
    import ast

    path = ROOT / "qta_agent" / "reconstruct.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
            imported.update(f"{node.module.lstrip('.')}.{a.name}"
                            for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    # Checked as IMPORTS rather than as text: the first version of this test
    # matched the module name in a docstring that explains the separation,
    # which would have failed for saying the right thing.
    assert not {i for i in imported if "governed_stage10" in i}, (
        "reconstruct imported the module it is supposed to check "
        "independently")
    assert "tasks.apply_transition" not in imported, (
        "reconstruct reused the projection's transition applier, which makes "
        "every comparison in this file circular while still passing")
    # It MAY import the transition table -- re-authorizing against a
    # different table would compare two different questions -- but it must
    # derive the resulting state itself.
    assert "tasks.check" in imported or "tasks" in imported


# --- the divergence the second reader exists to catch -----------------------

def test_both_readers_refuse_a_record_that_names_its_own_starting_state(gov):
    """The defect a hostile campaign found, checked from the other side.

    The record declares src=EXECUTING for a task sitting in VERIFIED, and
    EXECUTING -> TIMED_OUT is a real edge, so a reader that trusted the claim
    would apply it and move a sealed task.
    """
    run = _run(gov)
    gov.log.append(
        actor="attacker", action=ACT_TASK_TRANSITION, target=run.task_id,
        payload={"task_id": run.task_id, "src": TaskState.EXECUTING.value,
                 "dst": TaskState.TIMED_OUT.value, "role": "SYSTEM"})

    # Enforcement refuses the whole history and says why.
    with pytest.raises(TaskTransitionError, match="moves it from"):
        gov.projection()

    # Diagnosis keeps going, names the anomaly, and does NOT apply it.
    recon = reconstruct_tasks(gov.log)
    assert any("claims src EXECUTING" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.states()[run.task_id] == TaskState.VERIFIED.value, (
        "the independent replay applied a record whose starting state it "
        "disagreed with")


def test_an_unauthorized_transition_is_reported_and_not_applied(gov):
    """A record the machine would refuse today stays a record, not a state."""
    run = _run(gov)
    gov.log.append(
        actor=WORKER_ID, action=ACT_TASK_TRANSITION, target=run.task_id,
        payload={"task_id": run.task_id, "src": TaskState.VERIFIED.value,
                 "dst": TaskState.COMPLETED.value, "role": "WORKER"})
    recon = reconstruct_tasks(gov.log)
    assert any("would be refused today" in u for u in recon.unauthorized), \
        recon.unauthorized
    assert recon.states()[run.task_id] == TaskState.VERIFIED.value


def test_a_transition_for_a_task_that_was_never_created_is_an_anomaly(
        tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="w", action=ACT_TASK_TRANSITION, target="ghost",
               payload={"task_id": "ghost", "src": "CREATED",
                        "dst": "VALIDATED", "role": "SUBMITTER"})
    recon = reconstruct_tasks(log)
    assert any("unknown task" in a for a in recon.anomalies), recon.anomalies
    assert recon.tasks == {}


def test_a_task_created_twice_is_an_anomaly(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    for _ in range(2):
        log.append(actor="s", action="task.create", target="t1",
                   payload={"task_id": "t1", "tool_id": "probe",
                            "submitter": "s", "inputs_digest": "a" * 64})
    recon = reconstruct_tasks(log)
    assert any("created twice" in a for a in recon.anomalies), recon.anomalies


def test_an_unknown_action_is_reported_rather_than_skipped(tmp_path):
    """The FOREIGN/UNKNOWN split, from the diagnostic side.

    This module reports instead of raising -- one unreadable record must not
    hide the twenty after it -- but it must not silently drop one either.
    """
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="x", action="future.schema", target="t",
               payload={"task_id": "t"})
    recon = reconstruct_tasks(log)
    assert any("unknown action" in a for a in recon.anomalies), recon.anomalies


def test_foreign_events_are_counted_not_mistaken_for_task_records(gov):
    """Several subsystems share one log; the count says so out loud."""
    _run(gov)
    recon = reconstruct_tasks(gov.log)
    assert recon.foreign_events > 0
    assert recon.events_replayed == gov.log.verify().count


def test_compare_names_the_field_that_disagrees(gov):
    """A diff saying only 'they differ' would send a reader to read both."""
    run = _run(gov)
    recon = reconstruct_tasks(gov.log)
    recon.tasks[run.task_id]["state"] = TaskState.FAILED.value
    diffs = compare_tasks(gov.projection(), recon)
    assert len(diffs) == 1
    assert diffs[0].record_id == run.task_id
    assert diffs[0].field_name == "state"
    assert diffs[0].live == "VERIFIED" and diffs[0].reconstructed == "FAILED"
    assert "live='VERIFIED'" in str(diffs[0])


def test_a_task_present_in_one_reader_only_is_a_divergence(gov):
    run = _run(gov)
    recon = reconstruct_tasks(gov.log)
    recon.tasks["phantom"] = dict(recon.tasks[run.task_id], task_id="phantom")
    diffs = compare_tasks(gov.projection(), recon)
    assert [d.field_name for d in diffs] == ["<presence>"]
    assert diffs[0].record_id == "phantom"
    assert diffs[0].live == "ABSENT"


def test_the_reconstruction_refuses_a_tampered_log(gov):
    """A second opinion about a rewritten document is not a second opinion."""
    from qta_agent.events import ChainBroken

    _run(gov)
    lines = gov.log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["note"] = "tampered"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    gov.log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        reconstruct_tasks(gov.log)


def test_the_differential_is_part_of_the_production_path():
    """A second reader nobody runs is a second reader of nothing."""
    rule = (ROOT / "Snakefile").read_text(encoding="utf-8") \
        .split("rule s10_governed:", 1)[1].split("\nrule ", 1)[0]
    assert "reconstruct_tasks" in rule
    assert "compare_tasks" in rule
    assert "assert not divergences" in rule
    assert "recon.unauthorized" in rule and "recon.anomalies" in rule


def test_the_two_readers_agree_about_who_holds_a_lease(gov):
    """A lease that outlives its work is state, and a diff must see it.

    Nothing else in the comparison would notice: the task's STATE is right
    either way, and only the lease says whether the work is still owned by a
    worker that has already stopped. Stranded work looks exactly like busy
    work until somebody tries to take it.
    """
    run = _run(gov)
    live = gov.projection().tasks[run.task_id]
    recon = reconstruct_tasks(gov.log)

    assert live.lease is None, (
        "a VERIFIED task still holds a lease; nobody else could take this "
        "work if it ever needed redoing")
    assert recon.tasks[run.task_id]["lease"] is None
    assert compare_tasks(gov.projection(), recon) == ()

    # And a divergence in the lease alone is reported, named.
    recon.tasks[run.task_id]["lease"] = {"lease_id": "L-ghost",
                                         "holder": "worker-gone",
                                         "granted_seq": 1,
                                         "expires_after_seq": 99}
    diffs = compare_tasks(gov.projection(), recon)
    assert [d.field_name for d in diffs] == ["lease"]


# --- the second reader had the same hole, one line lower ---------------------

def _forged_history(tmp_path, *, execution_record: bool):
    """A task moved end to end by ONE actor, naming a ghost as its executor."""
    from qta_agent.canonical import digest_bytes

    log = EventLog(tmp_path / "log.jsonl")
    tid = "t-forged"
    dg = digest_bytes(b"a result nobody produced")

    def tr(src, dst, role, **extra):
        payload = {"task_id": tid, "src": src, "dst": dst, "role": role}
        payload.update(extra)
        log.append(actor="mallory", action=ACT_TASK_TRANSITION, target=tid,
                   payload=payload)

    log.append(actor="mallory", action="task.create", target=tid,
               payload={"task_id": tid, "tool_id": "probe",
                        "submitter": "mallory", "inputs_digest": dg})
    tr("CREATED", "VALIDATED", "SUBMITTER")
    tr("VALIDATED", "QUEUED", "SCHEDULER")
    tr("QUEUED", "LEASED", "WORKER",
       lease={"lease_id": "L1", "holder": "mallory", "granted_seq": 3,
              "expires_after_seq": 9999})
    tr("LEASED", "EXECUTING", "WORKER", lease_id="L1")
    if execution_record:
        log.append(actor="mallory", action="task.execution", target=tid,
                   payload={"task_id": tid, "result_digest": dg,
                            "outcome": "COMPLETED", "tool_id": "probe"})
    tr("EXECUTING", "COMPLETED", "WORKER", lease_id="L1",
       executed_by="a-ghost", result_digest=dg)
    tr("COMPLETED", "VERIFIED", "VERIFIER")
    return log, tid


@pytest.mark.parametrize("execution_record", [True, False])
def test_the_reconstruction_does_not_take_the_executor_from_a_payload(
        tmp_path, execution_record):
    """The line that put this reader back underneath a fixed bypass.

    The re-authorization above it was already correct: it probed with the
    executor THIS replay had derived, from the execution record. Then, five
    lines later, ``cur["executed_by"] = p["executed_by"]`` overwrote that
    with the payload's claim -- in time for the NEXT transition to be checked
    against the forger's choice of counterparty.

    So the second opinion agreed with the first one's defect while looking
    like an independent check. Both parametrizations matter: with an
    execution record the claim contradicts a known executor, and without one
    it invents an executor from nothing.
    """
    log, tid = _forged_history(tmp_path, execution_record=execution_record)
    recon = reconstruct_tasks(log)

    assert recon.states()[tid] != TaskState.VERIFIED.value
    assert recon.verified_ids() == ()
    assert any("as its executor" in a for a in recon.anomalies), \
        recon.anomalies
    assert any("would be refused today" in u for u in recon.unauthorized), \
        recon.unauthorized


def test_both_readers_refuse_the_forged_history_the_same_way(tmp_path, gov):
    """Agreement about a REFUSAL is the comparison that matters here.

    The production projection raises; this reader records and continues --
    that difference is designed. What must not differ is the verdict: if one
    of them called this task VERIFIED the package's central claim would be
    false, and the diff is what says so.
    """
    from qta_agent.governed_stage10 import GovernedStage10

    log, tid = _forged_history(tmp_path, execution_record=True)
    g2 = GovernedStage10(root=ROOT, log=log, evidence=gov.evidence)
    with pytest.raises(TaskTransitionError, match="execution record says"):
        g2.projection()
    assert reconstruct_tasks(log).verified_ids() == ()
