"""A crash at EVERY boundary of the governed run, not one.

WHY EVERY BOUNDARY AND NOT A CHOSEN FEW

The governed path appends about a dozen records in order: a policy decision,
a task, a queue entry, a lease, an execution record, evidence, transitions, a
context manifest, a note. Those steps are individually durable and are NOT one
atomic unit -- there is no two-phase commit here, and pretending otherwise
would be worse than saying so.

What can be established instead is stronger than it sounds: that EVERY prefix
of that sequence is a state recovery handles, and that the answer at each one
is named rather than discovered at 2am. A test that crashes at three chosen
points proves three points. This one truncates the log after every single
record and asks the same questions of each.

THE QUESTIONS, ASKED AT EVERY PREFIX

  * does the chain still verify? (a crash mid-run must not corrupt history)
  * does the projection either succeed or REFUSE -- never hand back a state
    it cannot justify?
  * do the live projection and the independent replay agree?
  * is anything VERIFIED without the evidence record that supports it?
  * is any artifact digest cited before the record that captured it?

THE PART THIS DOES NOT CLOSE

A crash between the execution record and the COMPLETED transition still
leaves the task EXECUTING. That is recoverable -- the lease lapses and the
scheduler requeues the work -- but it is not automatic, and no test here
makes it so. The gap is real and stays in the matrix; what this file removes
is the excuse that nobody knows which prefixes produce it.
"""
from __future__ import annotations

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
    SUBMITTER_ID, VERIFIER_ID, WORKER_ID, GovernedStage10,
)
from qta_agent.reconstruct import (  # noqa: E402
    compare_tasks, reconstruct_tasks,
)
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_atomicity"

#: What an operator does about a task found in each state after a crash. The
#: value of writing them down is that a state with no entry is a state nobody
#: has decided about -- and the test below fails on one.
RECOVERY = {
    TaskState.CREATED: "resubmit; nothing ran",
    TaskState.VALIDATED: "resubmit; nothing ran",
    TaskState.QUEUED: "the scheduler dispatches it again",
    TaskState.LEASED: "the lease lapses and reconcile returns it to READY",
    TaskState.EXECUTING: (
        "the lease lapses and reconcile requeues the WORK. The task record "
        "stays EXECUTING until something moves it, which is the gap R39 "
        "still carries"),
    TaskState.COMPLETED: "a verifier picks it up; the bytes are already cited",
    TaskState.VERIFIED: "nothing to do",
    TaskState.REJECTED: "nothing to do; the inputs were refused",
    TaskState.FAILED: "resubmit if the failure was transient",
    TaskState.TIMED_OUT: "resubmit with a larger bound, or split the work",
    TaskState.CANCELLED: "nothing to do; somebody decided",
    TaskState.INVALIDATED: "re-derive; an input changed",
}


@pytest.fixture(scope="module")
def finished():
    """One complete governed run, kept so every prefix comes from real bytes.

    Synthesising a plausible log would test the synthesiser. These are the
    records the production path actually wrote, in the order it wrote them.
    """
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    gov = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                          evidence=EvidenceStore(base / "evidence"))
    gov.out_rel = f"{WS}/out"
    run = gov.run(tool_id="stage10.emit_artifact",
                  inputs={"out_dir": gov.out_rel, "name": "a.json",
                          "payload": {"v": 1}},
                  submitter=SUBMITTER_ID, worker=WORKER_ID,
                  verifier=VERIFIER_ID)
    assert run.state is TaskState.VERIFIED
    lines = (base / "log.jsonl").read_text(encoding="utf-8").splitlines()
    yield {"gov": gov, "run": run, "lines": lines, "base": base,
           "evidence": base / "evidence"}
    if base.exists():
        shutil.rmtree(base)


def _prefix_world(finished, n, tmp_path):
    """A world whose log holds only the first ``n`` records.

    Written into a fresh directory with NO head witness, because that is the
    honest shape of this failure: a process that died after appending n
    records and before writing the n+1st. A witness pointing past the tail
    would be the DIFFERENT failure -- truncation of a completed log -- and
    that one is covered in test_agent_crash_recovery.py.
    """
    d = tmp_path / f"prefix-{n}"
    d.mkdir(parents=True)
    (d / "log.jsonl").write_text(
        "\n".join(finished["lines"][:n]) + ("\n" if n else ""),
        encoding="utf-8")
    shutil.copytree(finished["evidence"], d / "evidence")
    gov = GovernedStage10(root=ROOT, log=EventLog(d / "log.jsonl"),
                          evidence=EvidenceStore(d / "evidence"))
    return gov


def _boundaries(finished):
    return list(range(1, len(finished["lines"]) + 1))


def test_the_run_produced_enough_boundaries_to_be_worth_sweeping(finished):
    """A sweep over two records would pass and prove nothing."""
    assert len(finished["lines"]) >= 10, (
        f"only {len(finished['lines'])} records; the governed path is "
        "supposed to leave a trail at every stage")


def test_every_prefix_of_a_governed_run_still_verifies(finished, tmp_path):
    """A crash mid-run must cost the tail, never the history before it."""
    problems = []
    for n in _boundaries(finished):
        d = tmp_path / f"raw-{n}"
        d.mkdir(parents=True)
        (d / "log.jsonl").write_text(
            "\n".join(finished["lines"][:n]) + "\n", encoding="utf-8")
        # Verified BEFORE constructing a runner: the constructor bootstraps a
        # policy and three identities onto an empty-looking log, so counting
        # after it would count records the crash never wrote.
        report = EventLog(d / "log.jsonl").verify()
        if not report.ok:
            problems.append((n, report.problems[:2]))
        if report.count != n:
            problems.append((n, f"read {report.count} records, expected {n}"))
    assert not problems, problems


def test_every_prefix_projects_or_refuses_and_never_invents(finished,
                                                            tmp_path):
    """The property that matters: no prefix yields an unjustified state."""
    seen_states = set()
    for n in _boundaries(finished):
        gov = _prefix_world(finished, n, tmp_path)
        try:
            proj = gov.projection()
        except Exception as exc:                   # noqa: BLE001
            raise AssertionError(
                f"prefix {n}: the projection neither succeeded nor refused "
                f"cleanly: {type(exc).__name__}: {exc}") from exc
        for task in proj.tasks.values():
            seen_states.add(task.state)
    assert seen_states, "no prefix produced a task at all"
    undecided = seen_states - set(RECOVERY)
    assert not undecided, (
        f"a crash can leave a task in {sorted(s.value for s in undecided)}, "
        "and nothing here says what an operator does about it")


def test_the_two_readers_agree_at_every_prefix(finished, tmp_path):
    """A divergence that only appears after a crash is the worst kind."""
    problems = []
    for n in _boundaries(finished):
        gov = _prefix_world(finished, n, tmp_path)
        recon = reconstruct_tasks(gov.log)
        diffs = compare_tasks(gov.projection(), recon)
        if diffs:
            problems.append((n, [str(d) for d in diffs]))
        if recon.unauthorized:
            problems.append((n, recon.unauthorized))
    assert not problems, problems


def test_no_prefix_yields_a_verified_task_without_its_evidence(finished,
                                                               tmp_path):
    """THE unsafe thing. A VERIFIED task with nothing showing what it
    produced is indistinguishable from a fabrication nobody noticed."""
    for n in _boundaries(finished):
        gov = _prefix_world(finished, n, tmp_path)
        actions = [ev.action for ev in gov.log.read()]
        for task in gov.projection().tasks.values():
            if task.state is TaskState.VERIFIED:
                assert "task.evidence" in actions, (
                    f"prefix {n}: a task reached VERIFIED with no evidence "
                    "record in the history")
                assert "task.execution" in actions, (
                    f"prefix {n}: a task reached VERIFIED with no execution "
                    "record in the history")


def test_no_prefix_cites_bytes_it_has_not_recorded(finished, tmp_path):
    """Evidence is cited by content. A citation must follow its capture."""
    for n in _boundaries(finished):
        gov = _prefix_world(finished, n, tmp_path)
        captured: set = set()
        for ev in gov.log.read():
            if ev.action == "task.evidence":
                captured.update((ev.payload.get("artifacts") or {}).values())
            if ev.action == "task.transition":
                dg = ev.payload.get("result_digest")
                if dg and ev.payload.get("dst") == TaskState.VERIFIED.value:
                    assert dg in captured or gov.evidence.contains(dg), (
                        f"prefix {n}: a verification cites {dg[:12]} before "
                        "anything recorded it")


def test_a_prefix_that_stops_mid_execution_is_recoverable_not_stuck(finished,
                                                                    tmp_path):
    """The named gap, pinned to the prefixes that actually produce it.

    R39 still carries "a crash between the execution record and the COMPLETED
    transition leaves the task EXECUTING". This says WHICH prefixes do that
    and confirms the state is one the lease machinery can act on -- rather
    than leaving the claim as prose nobody checked.
    """
    executing = []
    for n in _boundaries(finished):
        gov = _prefix_world(finished, n, tmp_path)
        for task in gov.projection().tasks.values():
            if task.state is TaskState.EXECUTING:
                executing.append(n)
                assert task.lease is not None, (
                    f"prefix {n}: EXECUTING with no lease, so nothing can "
                    "ever reclaim this work")
                assert task.lease.holder, "a lease with no holder is not a "\
                    "lease anyone can adjudicate"
    assert executing, (
        "no prefix stopped mid-execution, so this test asserts nothing about "
        "the gap it exists to pin down")


def test_the_recovery_table_covers_the_whole_state_machine(finished):
    """A state absent from the table is a state nobody decided about."""
    missing = [s.value for s in TaskState if s not in RECOVERY]
    assert not missing, missing
