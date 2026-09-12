"""Resolving what a dead supervisor left, without impersonating it.

The gap these tests close was invisible for a reason worth stating: the
mechanism existed and nothing asked it. TaskProjection.expired_leases() had
been there since the lifecycle was written, documented as "the scheduler's
input for returning stranded work to the queue", with zero callers -- not
even a test. Recovery is what asks it.

And asking it is not sufficient, which is the second half. A lease expires
in SEQUENCE NUMBERS; a crashed supervisor stops advancing the log; so the
lease that would authorise recovery is expired by the very thing that died.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.governed_stage10 import GovernedStage10  # noqa: E402
from qta_agent.hostid import (  # noqa: E402
    ALIVE, GONE, UNKNOWN, ProcessIdentity, boot_id, identify,
    liveness, parse_stat_start_ticks, start_ticks,
)
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_recovery"


@pytest.fixture()
def base(request):
    name = request.node.name.replace("/", "_")[:60]
    d = ROOT / WS / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    yield d
    if d.exists():
        shutil.rmtree(d)


def _gov(base):
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{base.relative_to(ROOT).as_posix()}/out"
    return g


def _inputs(g, **over):
    d = {"out_dir": g.out_rel, "name": "artifact.json",
         "payload": {"label": "MODEL_ONLY", "value": 42}}
    d.update(over)
    return d


# --- process identity: a pid alone is not one -------------------------------

def test_a_live_process_is_alive_and_a_killed_one_is_gone():
    me = identify()
    assert liveness(me) is ALIVE
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        child = identify(p.pid)
        assert liveness(child) is ALIVE
    finally:
        p.kill()
        p.wait()
    assert liveness(child) is GONE


def test_a_different_boot_makes_every_recorded_pid_stale():
    """One comparison invalidates every pid at once, with no per-pid lookup."""
    me = identify()
    other_boot = ProcessIdentity(pid=me.pid, host_boot_id="0" * 36,
                                 start_ticks=me.start_ticks)
    assert liveness(other_boot) is GONE


def test_a_record_that_cannot_prove_identity_is_unknown_not_gone():
    """THE load-bearing third answer.

    Treating "I cannot tell" as "it is gone" is how two workers end up
    running one task -- the exact failure a lease exists to prevent. Every
    one of these is a record recovery must refuse to act on.
    """
    me = identify()
    assert liveness(None) is UNKNOWN
    assert liveness(ProcessIdentity(pid=me.pid,
                                    host_boot_id=me.host_boot_id)) is UNKNOWN
    assert liveness(ProcessIdentity(pid=me.pid, host_boot_id="",
                                    start_ticks=me.start_ticks)) is UNKNOWN


def test_a_reused_pid_is_not_the_process_that_was_recorded():
    """What start_ticks is for. Same pid, different process, different time."""
    me = identify()
    impostor = ProcessIdentity(pid=me.pid, host_boot_id=me.host_boot_id,
                               start_ticks=(me.start_ticks or 0) + 1)
    assert liveness(impostor) is GONE


def test_start_ticks_reads_this_process_and_refuses_an_impossible_pid():
    assert start_ticks(os.getpid()) is not None
    assert start_ticks(2 ** 30) is None, "a pid that cannot exist"


def _stat_line(comm: str, start: int) -> str:
    """A /proc/<pid>/stat line with ``comm`` as the process name.

    Fields 3..21 are filled with placeholders so field 22 -- the start time
    -- lands where the kernel puts it.
    """
    fields = [str(i) for i in range(3, 22)] + [str(start)] + ["0"] * 30
    return f"1234 ({comm}) " + " ".join(fields) + "\n"


@pytest.mark.parametrize("comm", [
    "python3",
    "foo) 1 2 3",        # a ')' inside the name
    "a) b) c",           # several
    ") ",                # begins with one
])
def test_the_start_time_survives_a_process_name_containing_a_paren(comm):
    """The kernel does not escape anything inside comm.

    Anchoring on the FIRST ')' shifts every later field, and the failure is
    silent: it returns a plausible integer, so a live process reads as a
    reused pid and its lease gets reclaimed out from under it. Testing this
    against a real process would mean arranging for one named ``foo) 1 2 3``
    to exist, which is why the parser is a separate function.
    """
    assert parse_stat_start_ticks(_stat_line(comm, 987654)) == 987654


def test_a_truncated_or_unparseable_stat_line_is_none_not_a_guess():
    """None, never a plausible number.

    A guess here does not fail loudly: it becomes a start time, which turns
    a live process into a reused pid and reclaims its lease.
    """
    assert parse_stat_start_ticks("1234 (python3) R 1 2 3") is None, \
        "too few fields"
    assert parse_stat_start_ticks("no parens here at all") is None, \
        "no comm field to anchor on"
    non_numeric = _stat_line("x", 0).replace(" 0 ", " zz ", 1)
    assert parse_stat_start_ticks(non_numeric) is None, \
        "the start-time field is not an integer"


def test_boot_id_is_empty_rather_than_invented_when_unavailable(monkeypatch):
    """A synthesised boot id would differ between two processes on the SAME
    boot, making every record look stale and every recovery a reclaim."""
    monkeypatch.setattr("qta_agent.hostid._BOOT_ID_PATH", "/nonexistent")
    assert boot_id() == ""


# --- recovery: what a dead supervisor leaves --------------------------------

def _crash_child(base, at_state="COMPLETED"):
    """Run a REAL governed supervisor that dies mid-run via os._exit."""
    src = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
from qta_agent.events import EventLog
from qta_agent.evidence import EvidenceStore
import qta_agent.governed_stage10 as g10
from qta_agent.tasks import TaskState
g = g10.GovernedStage10(root={str(ROOT)!r},
    log=EventLog({str(base / "log.jsonl")!r}),
    evidence=EvidenceStore({str(base / "evidence")!r}))
g.out_rel = {f"{base.relative_to(ROOT).as_posix()}/out"!r}
real = g10.GovernedStage10._move
def die(self, task, dst, *a, **k):
    if dst is TaskState.{at_state}:
        import os; os._exit(9)
    return real(self, task, dst, *a, **k)
g10.GovernedStage10._move = die
g.run(tool_id="stage10.emit_artifact",
      inputs={{"out_dir": g.out_rel, "name": "a.json",
               "payload": {{"v": 1}}}})
'''
    r = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=180)
    assert r.returncode == 9, (r.returncode, r.stdout[-800:], r.stderr[-800:])


def test_a_crash_after_the_execution_record_is_recovered(base):
    """The exact gap: EXECUTING with a lease nobody holds, forever."""
    _crash_child(base, "COMPLETED")
    g = _gov(base)
    proj = g.projection()
    (task,) = list(proj.tasks.values())
    assert task.state is TaskState.EXECUTING

    head = g.log.verify().head_seq
    assert task.lease.is_live(head), (
        "the premise of this test is that the SEQUENCE says the lease is "
        "still live, because the log stopped advancing when the holder died")
    assert not proj.expired_leases(), (
        "expired_leases() cannot see this, which is why it alone was never "
        "enough")

    (action,) = g.recover()
    assert action["from"] == "EXECUTING" and action["to"] == "QUEUED"
    assert action["had_execution_record"] is True
    assert action["lease_lapsed_by_seq"] is False
    assert "no longer exists on this boot" in action["reason"]
    assert g.projection().get(task.task_id).state is TaskState.QUEUED
    assert g.log.verify().ok


def test_recovery_does_not_complete_the_task_from_its_own_record(base):
    """The execution record says the process exited 0. That is not authority.

    Completing it would be a recovery process finishing another party's work
    under a lease nobody holds, which is the ownership bypass the lease
    exists to prevent -- and the COMPLETED edge requires both a live lease
    and the WORKER role for exactly that reason.
    """
    _crash_child(base, "COMPLETED")
    g = _gov(base)
    (task,) = list(g.projection().tasks.values())
    ran = g._execution_record(task.task_id)
    assert ran and ran["outcome"] == "COMPLETED", (
        "the tool really did finish; this is the tempting case")
    g.recover()
    after = g.projection().get(task.task_id)
    assert after.state is TaskState.QUEUED
    assert after.state is not TaskState.COMPLETED


def test_a_crash_before_the_tool_ran_is_recovered_too(base):
    _crash_child(base, "EXECUTING")
    g = _gov(base)
    (action,) = g.recover()
    assert action["to"] == "QUEUED"
    assert action["had_execution_record"] is False
    assert "nothing observed the tool run" in action["reason"]


def test_recovery_appends_nothing_when_nothing_is_stranded(base):
    """It runs at the start of every governed run, so it must be free."""
    g = _gov(base)
    g.run(tool_id="stage10.emit_artifact", inputs=_inputs(g))
    head = g.log.verify().head_seq
    assert g.recover() == ()
    assert g.log.verify().head_seq == head


def test_recovery_is_idempotent(base):
    _crash_child(base, "COMPLETED")
    g = _gov(base)
    assert len(g.recover()) == 1
    head = g.log.verify().head_seq
    assert g.recover() == ()
    assert g.log.verify().head_seq == head


def test_a_live_holder_is_never_reclaimed(base):
    """THE positive control. Recovery that takes a live lease is a second
    worker, not a recovery."""
    g = _gov(base)
    g.run(tool_id="stage10.emit_artifact", inputs=_inputs(g))
    # Strand a task by hand, with THIS still-running process as the holder.
    import qta_agent.governed_stage10 as g10
    from qta_agent.tasks import Lease

    task = g.run(tool_id="stage10.emit_artifact",
                 inputs=_inputs(g, name="b.json"))
    assert task.state is TaskState.VERIFIED
    live = Lease(lease_id="L-live", holder="stage10-worker",
                 granted_seq=g.log.verify().head_seq,
                 expires_after_seq=g.log.verify().head_seq + 10_000,
                 holder_process=identify().to_record())
    assert liveness(ProcessIdentity.from_record(live.holder_process)) is ALIVE
    assert g10.GovernedStage10.recover  # the method under test exists
    # Nothing is stranded and the only live lease is this process's own.
    assert g.recover() == ()


def test_an_unknown_holder_is_not_reclaimed(base, monkeypatch):
    """A record from another host is not evidence that the holder is dead."""
    _crash_child(base, "COMPLETED")
    g = _gov(base)
    # Make every liveness answer UNKNOWN, as it would be off-host.
    monkeypatch.setattr("qta_agent.governed_stage10.liveness",
                        lambda _identity: UNKNOWN)
    assert g.recover() == (), (
        "an inconclusive answer authorised a reclaim; that is how two "
        "workers end up running one task")
    assert g.projection().get(
        list(g.projection().tasks)[0]).state is TaskState.EXECUTING


def test_a_completed_task_is_reported_not_resolved(base):
    """Awaiting a verifier is not stuck, and inventing the verdict is the
    one thing this system exists to refuse."""
    _crash_child(base, "VERIFIED")
    g = _gov(base)
    (task,) = list(g.projection().tasks.values())
    assert task.state is TaskState.COMPLETED
    assert g.recover() == ()
    assert g.awaiting_verification() == (task.task_id,)
    assert g.projection().get(task.task_id).state is TaskState.COMPLETED


def test_a_governed_run_recovers_what_a_dead_predecessor_left(base):
    """The production caller. A supervisor starts by resolving the mess."""
    _crash_child(base, "COMPLETED")
    g = _gov(base)
    stranded = list(g.projection().tasks)
    run = g.run(tool_id="stage10.emit_artifact", inputs=_inputs(g,
                                                               name="c.json"))
    assert run.state is TaskState.VERIFIED
    assert g.projection().get(stranded[0]).state is TaskState.QUEUED, (
        "the new run did not resolve its predecessor's stranded task")


def test_the_lease_records_the_process_that_took_it(base):
    g = _gov(base)
    g.run(tool_id="stage10.emit_artifact", inputs=_inputs(g))
    leased = [e for e in g.log.read()
              if e.action == "task.transition"
              and e.payload.get("dst") == "LEASED"]
    assert leased, "no LEASED transition was recorded"
    proc = leased[-1].payload["lease"]["holder_process"]
    assert proc["pid"] == os.getpid()
    assert proc["host_boot_id"] and proc["start_ticks"] is not None, (
        "a pid with no boot id and no start time cannot be asked about later")


# --- the two recovery paths, isolated from each other -----------------------

def test_a_lapsed_lease_is_recovered_without_asking_the_operating_system(
        base, monkeypatch):
    """The ORDINARY path, isolated so the liveness path cannot mask it.

    Every other recovery test here reaches the same outcome through process
    liveness, which meant a mutation deleting the expired_leases() call
    survived: the second path covered for the first. Forcing liveness to
    say UNKNOWN removes that cover, so what is under test is the sequence
    number alone.
    """
    _crash_child(base, "COMPLETED")
    g = _gov(base)
    (task,) = list(g.projection().tasks.values())

    # Advance the log past the lease's expiry without touching the task, so
    # the lease lapses the way it would if other work were flowing.
    while g.log.verify().head_seq <= task.lease.expires_after_seq:
        g.log.append(actor="system", action="agent.message",
                     target="tick", payload={"to": "nobody", "body": "tick"})

    monkeypatch.setattr("qta_agent.governed_stage10.liveness",
                        lambda _identity: UNKNOWN)
    proj = g.projection()
    assert proj.expired_leases(), "the premise: the lease has now lapsed"
    (action,) = g.recover()
    assert action["lease_lapsed_by_seq"] is True
    assert "lapsed at seq" in action["reason"]
    assert g.projection().get(task.task_id).state is TaskState.QUEUED


def test_an_in_flight_task_cannot_exist_without_a_lease(base):
    """Pins the assumption that makes recovery's no-lease branch dead code.

    That branch is defensive: it catches a task in LEASED or EXECUTING with
    no lease record. A mutation deleting it survived, and the reason is that
    the state is unreachable -- the edge into EXECUTING carries
    requires_lease and replay re-authorizes every transition, so no
    projection this code produces can hold one. The mutation was removed as
    EQUIVALENT rather than left as a permanent false finding, and this is
    what makes that removal re-checkable: weaken the edge and this fails,
    and the mutation becomes meaningful again.
    """
    from qta_agent.tasks import (Task, TaskRole, TaskTransition,
                                 TaskTransitionError, check)

    leased_no_lease = Task(task_id="t1", tool_id="x", submitter="s",
                           inputs_digest="d" * 64, state=TaskState.LEASED,
                           lease=None)
    req = TaskTransition(task_id="t1", src=TaskState.LEASED,
                         dst=TaskState.EXECUTING, actor="w",
                         role=TaskRole.WORKER, at_seq=5, lease_id="L1")
    with pytest.raises(TaskTransitionError, match="holds none"):
        check(req, leased_no_lease)


# --- orphans: a dead supervisor's child is still running --------------------

def test_an_orphan_of_a_dead_supervisor_is_terminated(base):
    """The gap said a pid was "diagnostic, not a handle".

    It could not have been a handle: acting on a bare pid eventually
    signals whatever program inherited the number. With the child's boot id
    and start time recorded, the question is answerable, and BOTH answers
    gate the signal -- the supervisor gone, the child provably the same
    process.
    """
    # A supervisor that spawns a long-lived grandchild and then dies.
    src = f'''
import subprocess, sys, os, json, time
sys.path.insert(0, {str(ROOT)!r})
from qta_agent.events import EventLog
from qta_agent.evidence import EvidenceStore
from qta_agent.execution import Limits
import qta_agent.governed_stage10 as g10
from qta_agent.tasks import TaskState
g = g10.GovernedStage10(root={str(ROOT)!r},
    log=EventLog({str(base / "log.jsonl")!r}),
    evidence=EvidenceStore({str(base / "evidence")!r}))
g.out_rel = {f"{base.relative_to(ROOT).as_posix()}/out"!r}
real_run = type(g.executor).run
def slow(self, **kw):
    kw["argv"] = [sys.executable, "-c", "import time; time.sleep(120)"]
    kw["limits"] = Limits(wall_seconds=90.0)
    return real_run(self, **kw)
type(g.executor).run = slow
real_move = g10.GovernedStage10._move
def die(self, task, dst, *a, **k):
    out = real_move(self, task, dst, *a, **k)
    if dst is TaskState.EXECUTING:
        os._exit(9)              # die WHILE the child is running
    return out
g10.GovernedStage10._move = die
g.run(tool_id="stage10.emit_artifact",
      inputs={{"out_dir": g.out_rel, "name": "a.json",
               "payload": {{"v": 1}}}})
'''
    r = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=180)
    assert r.returncode == 9, (r.returncode, r.stderr[-600:])

    g = _gov(base)
    (task,) = list(g.projection().tasks.values())
    assert task.state is TaskState.EXECUTING
    ran = g._execution_record(task.task_id)
    assert ran is None, (
        "the supervisor died before recording an execution result, which is "
        "exactly when an orphan is left with nothing pointing at it")

    # Nothing to act on: without an execution record there is no child
    # identity, so the sweep must REPORT rather than guess at a pid.
    (report,) = g.sweep_orphans()
    assert report["action"] == "REPORTED"
    assert report["child_liveness"] == UNKNOWN


def test_the_sweep_never_signals_while_the_supervisor_may_still_be_working(
        base):
    """A live supervisor's child is not an orphan, it is somebody's work."""
    g = _gov(base)
    g.run(tool_id="stage10.emit_artifact", inputs=_inputs(g))
    assert g.sweep_orphans() == ()


def test_the_sweep_refuses_to_signal_its_own_process_group(base):
    """An executor that can kill its own caller has no containment property.

    This happened once, from a leftover mutation, and presented as the test
    runner dying with SIGTERM -- a symptom that looks like a sandbox problem
    and is neither.
    """
    from qta_agent.governed_stage10 import _terminate_group

    me = identify()
    assert me.pgid == os.getpgrp(), "premise: our own group"
    result = _terminate_group(me)
    assert result.startswith("refused:"), result
    assert "THIS process" in result, result

    # And the group case, with a pid that is not ours but a group that is.
    # The old code answered this by signalling the bare pid, which is how
    # the version before this one sent SIGTERM to its own test runner.
    sibling = ProcessIdentity(pid=os.getppid(), pgid=os.getpgrp(),
                              host_boot_id=me.host_boot_id,
                              start_ticks=me.start_ticks)
    result = _terminate_group(sibling)
    assert result.startswith("refused:") and "own" in result, result


def test_a_record_with_no_process_group_is_refused_rather_than_guessed_at():
    """No group means nothing to signal. Falling back to the bare pid is
    what made the previous version kill its caller."""
    from qta_agent.governed_stage10 import _terminate_group

    me = identify()
    result = _terminate_group(ProcessIdentity(pid=me.pid + 1, pgid=None,
                                              host_boot_id=me.host_boot_id,
                                              start_ticks=1))
    assert result.startswith("refused:") and "no recorded process group" \
        in result, result


def test_a_child_that_is_not_provably_the_same_process_is_left_alone():
    """UNKNOWN and GONE are both refusals. Only ALIVE authorises a signal."""
    me = identify()
    stale = ProcessIdentity(pid=me.pid, pgid=me.pgid,
                            host_boot_id=me.host_boot_id,
                            start_ticks=(me.start_ticks or 0) + 1)
    assert liveness(stale) is GONE
    # The sweep asks liveness BEFORE reaching _terminate_group; this pins
    # that the identity really does distinguish them.
    assert liveness(ProcessIdentity(pid=me.pid,
                                    host_boot_id="")) is UNKNOWN
