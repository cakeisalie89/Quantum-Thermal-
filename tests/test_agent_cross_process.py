"""The scheduler and the authority store, under real concurrent processes.

WHAT THIS FOUND

tests/test_agent_concurrency.py proves the event log survives concurrent
writers: the chain stays intact and nobody's record is lost. That is a
property of the LOG. It says nothing about the reducers built on it, and the
reducers turned out to have a defect of their own, which four processes
reproduced on the first attempt:

    two processes each read a job as READY, each appended a transition out
    of READY, and the log became permanently unreplayable.

Nothing failed at the time. Both appends held the writer lock, both verified
the chain, both wrote well-formed records. The damage was semantic: the
second record moves a job from a state the replay has already left, so every
later ``load()`` refused the log -- and an authority log that cannot be
rebuilt cannot be repaired either, because the history is the authority.

THE FIX THESE TESTS PIN

An append that a decision depends on is now CONDITIONAL on the log not
having moved since the decision was made
(:meth:`qta_agent.events.EventLog.append_if_head`). The loser of a race
re-reads, discovers the job is no longer READY, and refuses -- which is the
outcome the single-process tests always described and the multi-process
reality did not deliver.

WHY THE PROCESSES ARE REAL

A thread would not have found it. The defect is in a read-decide-write
sequence over a file, and the interleaving that breaks it is available to
any two things holding the file open -- two processes on one host, or the
same program run twice by an operator who forgot the first was still going.
"""
from __future__ import annotations

import multiprocessing as mp
import random
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from hangguard import PROCESS_DEADLINE_S  # noqa: E402

from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.scheduler import (  # noqa: E402
    ACT_JOB_TRANSITION, JobState, Scheduler, default_policy,
)
from qta_agent.store import AuthorityStore  # noqa: E402

WORK = digest({"work": "stage10"})

#: Enough writers that a lost race is certain rather than likely. Four was
#: what reproduced the original defect on its first run.
RACERS = 4

#: How long a worker will wait at the start line. Bounded, because a worker
#: that waits forever parks the whole suite and reports nothing -- the same
#: rule the rest of the concurrency tests follow.
START_TIMEOUT_S = 30.0


# ---- module-level workers: multiprocessing with "spawn" needs them here ---
def _wait_for_start(go: str) -> None:
    """Line every worker up so the race is real rather than sequential."""
    path = Path(go)
    deadline = time.monotonic() + START_TIMEOUT_S
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError("the start signal never arrived")
        time.sleep(0.001)


def _open_scheduler(path):
    sys.path.insert(0, str(ROOT))
    from qta_agent.events import EventLog as _EL
    from qta_agent.policy import PolicyStore as _PS
    from qta_agent.scheduler import Scheduler as _S
    log = _EL(path)
    return _S(log, policy=_PS(log).load(), policy_id="scheduler.default",
              capacity={"slots": 1000}).load()


def _dispatch_worker(args):
    path, tag, go = args
    sched = _open_scheduler(path)
    _wait_for_start(go)
    try:
        sched.dispatch(job_id="j1", worker=f"w{tag}", lease_id=f"L{tag}",
                       lease_seqs=200)
        return ("won", tag, "")
    except Exception as exc:                    # noqa: BLE001 - reported
        return ("refused", tag, f"{type(exc).__name__}: {exc}")


def _dispatch_many_worker(args):
    path, tag, job_ids, go = args
    sched = _open_scheduler(path)
    _wait_for_start(go)
    taken = []
    for jid in job_ids:
        try:
            sched.dispatch(job_id=jid, worker=f"w{tag}",
                           lease_id=f"L{tag}-{jid}", lease_seqs=500)
            taken.append(jid)
        except Exception:                       # noqa: BLE001 - counted
            pass
    return (tag, taken)


def _transition_worker(args):
    path, tag, go = args
    sys.path.insert(0, str(ROOT))
    from qta_agent.authority import Role as _Role, State as _State
    from qta_agent.events import EventLog as _EL
    from qta_agent.store import AuthorityStore as _AS
    store = _AS(_EL(path)).load()
    _wait_for_start(go)
    try:
        store.transition(record_id="r1", dst=_State.UNDER_REVIEW,
                         actor=f"verifier-{tag}", role=_Role.VERIFIER)
        return ("won", tag, "")
    except Exception as exc:                    # noqa: BLE001 - reported
        return ("refused", tag, f"{type(exc).__name__}: {exc}")


def _create_worker(args):
    path, tag, go = args
    sys.path.insert(0, str(ROOT))
    from qta_agent.events import EventLog as _EL
    from qta_agent.store import AuthorityStore as _AS
    store = _AS(_EL(path)).load()
    _wait_for_start(go)
    try:
        store.create(record_id="shared", kind="claim", proposer=f"p{tag}")
        return ("won", tag, "")
    except Exception as exc:                    # noqa: BLE001 - reported
        return ("refused", tag, f"{type(exc).__name__}: {exc}")


def _unconditional_worker(args):
    """The control: an append that does NOT check the head.

    Writes the same record the scheduler would write, by hand, with the
    ordinary append. This is what the code used to do, and it is here so
    that the conditional append is shown to be the thing making the
    difference rather than some incidental change in timing.
    """
    path, tag, src, go = args
    sys.path.insert(0, str(ROOT))
    from qta_agent.events import EventLog as _EL
    from qta_agent.scheduler import ACT_JOB_TRANSITION as _ACT
    sched = _open_scheduler(path)
    job = sched.get("j1")
    _wait_for_start(go)
    # ``src`` comes from the PARENT, which is what makes this deterministic:
    # both records claim to move the job out of the same state, which is
    # exactly what two processes that each read it as READY would write. A
    # worker re-reading here would see the other's record when the pool
    # happened to run both tasks in one process, and the control would
    # quietly stop controlling for anything.
    _EL(path).append(
        actor="scheduler", action=_ACT, target="j1",
        payload={"job_id": "j1", "src": src,
                 "dst": "DISPATCHED", "reason": f"leased to w{tag}",
                 "lease_id": f"L{tag}", "lease_holder": f"w{tag}",
                 "lease_expires_after_seq": 10_000, "lease_renewals": 0,
                 "attempts": job.attempts + 1})
    return tag


OPS = ("enqueue", "dispatch", "report", "priority", "reconcile", "renew")


def _stress_worker(args):
    """A long randomized run against a shared log.

    Every operation is one another worker may be doing at the same moment on
    the same job. Refusals are expected and counted; what is NOT allowed is
    for the log to stop being replayable, which is checked by the parent
    once every worker has finished.
    """
    path, tag, rounds, seed = args
    sys.path.insert(0, str(ROOT))
    from qta_agent.canonical import digest as _digest
    from qta_agent.scheduler import FailureClass as _FC
    sched = _open_scheduler(path)
    rng = random.Random(seed)
    done = refused = 0
    for i in range(rounds):
        op = rng.choice(OPS)
        jid = f"j-{rng.randrange(12)}"
        try:
            if op == "enqueue":
                sched.enqueue(job_id=jid, work_digest=_digest({"j": jid}),
                              submitter="sub")
            elif op == "dispatch":
                sched.dispatch(job_id=jid, worker=f"w{tag}",
                               lease_id=f"L{tag}-{i}", lease_seqs=60)
            elif op == "report":
                sched.report(job_id=jid, worker=f"w{tag}",
                             failure=None if rng.random() < 0.7
                             else _FC.TRANSIENT)
            elif op == "priority":
                sched.set_priority(job_id=jid, priority=rng.randrange(1, 9),
                                   actor="scheduler", role="SCHEDULER",
                                   reason="stress")
            elif op == "renew":
                sched.renew_lease(job_id=jid, worker=f"w{tag}",
                                  lease_id=f"L{tag}-{i - 1}", lease_seqs=120)
            else:
                sched.reconcile()
            done += 1
        except Exception:                       # noqa: BLE001 - counted
            refused += 1
    return (tag, done, refused)


# ---- fixtures ------------------------------------------------------------
def _world(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    pol = PolicyStore(log).load()
    pol.publish(default_policy(), actor="owner")
    sched = Scheduler(log, policy=pol, policy_id="scheduler.default",
                      capacity={"slots": 1000}).load()
    return log, sched


def _go(tmp_path):
    return str(tmp_path / "go")


def _release(tmp_path):
    Path(_go(tmp_path)).write_text("1", encoding="utf-8")


def _run(worker, args, tmp_path, procs=RACERS):
    with mp.get_context("spawn").Pool(procs) as pool:
        pending = pool.map_async(worker, args)
        time.sleep(1.5)                 # let every worker reach the line
        _release(tmp_path)
        return pending.get(timeout=PROCESS_DEADLINE_S)


def _replayable(tmp_path):
    """Rebuild the queue from the log alone. THE property under test."""
    log = EventLog(tmp_path / "log.jsonl")
    return Scheduler(log, policy=PolicyStore(log).load(),
                     policy_id="scheduler.default",
                     capacity={"slots": 1000}).load()


# ---- the scheduler -------------------------------------------------------
def test_only_one_process_can_dispatch_one_job(tmp_path):
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()

    results = _run(_dispatch_worker,
                   [(str(tmp_path / "log.jsonl"), t, _go(tmp_path))
                    for t in range(RACERS)], tmp_path)

    won = [r for r in results if r[0] == "won"]
    assert len(won) == 1, results
    assert len(results) == RACERS


def test_the_log_is_still_replayable_after_the_race(tmp_path):
    """The property the original defect broke, stated on its own.

    Separate from the test above because "one worker won" and "the authority
    record survived" are different claims, and it was the second that failed
    while the first looked fine.
    """
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()
    results = _run(_dispatch_worker,
                   [(str(tmp_path / "log.jsonl"), t, _go(tmp_path))
                    for t in range(RACERS)], tmp_path)
    winner = [r for r in results if r[0] == "won"][0]

    assert EventLog(tmp_path / "log.jsonl").verify().ok
    rebuilt = _replayable(tmp_path)
    job = rebuilt.get("j1")
    assert job.state is JobState.DISPATCHED
    assert job.lease_holder == f"w{winner[1]}", (
        "the job must be held by the worker that was told it had won")
    assert job.attempts == 1, (
        "one dispatch, one attempt: a losing racer that still incremented "
        "the attempt count would burn a job's retry budget for work no "
        "worker ever started")


def test_the_losers_are_told_the_job_moved_under_them(tmp_path):
    """A refusal has to say what happened, or the caller cannot retry well.

    "no edge DISPATCHED -> DISPATCHED" is true and useless: it describes the
    state machine rather than the race. The dispatch carries the revision it
    decided against, so the loser is told the job changed since it read it.
    """
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()
    results = _run(_dispatch_worker,
                   [(str(tmp_path / "log.jsonl"), t, _go(tmp_path))
                    for t in range(RACERS)], tmp_path)

    losers = [r[2] for r in results if r[0] == "refused"]
    assert len(losers) == RACERS - 1
    assert all("changed since it was read" in msg
               or "only a READY job" in msg for msg in losers), losers


def test_many_workers_over_many_jobs_take_each_job_once(tmp_path):
    """The realistic shape: a pool of workers draining a shared queue."""
    log, sched = _world(tmp_path)
    jobs = [f"j{i}" for i in range(12)]
    for jid in jobs:
        sched.enqueue(job_id=jid, work_digest=digest({"j": jid}),
                      submitter="sub")
    sched.reconcile()

    results = _run(_dispatch_many_worker,
                   [(str(tmp_path / "log.jsonl"), t, jobs, _go(tmp_path))
                    for t in range(RACERS)], tmp_path)

    taken = [jid for _, got in results for jid in got]
    assert sorted(taken) == sorted(jobs), (
        "every job taken exactly once: a duplicate means two workers are "
        "doing the same work, and a missing one means work was dropped")
    rebuilt = _replayable(tmp_path)
    assert all(rebuilt.get(j).state is JobState.DISPATCHED for j in jobs)


def test_the_unconditional_append_is_what_used_to_break_it(tmp_path):
    """The control, and the reason the conditional append is not decoration.

    Two hand-written records, appended the way the scheduler used to append
    them. Both land, the chain verifies, and the queue can no longer be
    rebuilt. If this test ever starts passing the replay, the reducer has
    stopped checking the source state and every test above has quietly
    become vacuous.
    """
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()

    src = sched.get("j1").state.value
    _run(_unconditional_worker,
         [(str(tmp_path / "log.jsonl"), t, src, _go(tmp_path))
          for t in range(2)],
         tmp_path, procs=2)

    report = EventLog(tmp_path / "log.jsonl").verify()
    assert report.ok, (
        "the chain is intact -- that is the point: nothing about the LOG "
        "detects this")
    landed = [e for e in EventLog(tmp_path / "log.jsonl").read()
              if e.action == ACT_JOB_TRANSITION
              and e.payload.get("dst") == "DISPATCHED"]
    assert len(landed) == 2, (
        "both hand-written records must actually land, or this controls for "
        "nothing -- an action name that no reducer recognises would be "
        "skipped as another subsystem's event and the replay would succeed "
        "for the wrong reason")
    with pytest.raises(Exception) as caught:
        _replayable(tmp_path)
    assert "DISPATCHED" in str(caught.value), caught.value


def test_the_decision_runs_against_the_head_it_will_be_written_onto(
        tmp_path):
    """The primitive itself, without the reducers on top of it.

    ``decide`` is handed the head sequence the record will link to, and is
    called after every other writer has been shut out. A decision made
    against anything else is the defect this whole file is about.
    """
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="a", action="probe", target="t", payload={})
    EventLog(tmp_path / "log.jsonl").append(
        actor="b", action="probe", target="t", payload={})

    seen = []

    def decide(head_seq):
        seen.append(head_seq)
        return dict(actor="a", action="probe", target="t", payload={})

    ev = log.append_decided(decide)
    assert seen == [1], (
        "the decision must see the log's ACTUAL head, not the position the "
        "caller last read")
    assert ev.seq == 2


def test_a_decision_that_refuses_writes_nothing(tmp_path):
    """How a loser refuses: by raising out of the decision.

    Nothing is written and the exception is the caller's, unchanged -- so a
    scheduler can say "only a READY job may be dispatched" rather than the
    log saying something about sequence numbers.
    """
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="a", action="probe", target="t", payload={})

    class Refused(Exception):
        pass

    def decide(head_seq):
        raise Refused("somebody else already took this")

    with pytest.raises(Refused, match="already took"):
        log.append_decided(decide)
    assert log.verify().count == 1, (
        "a refused decision must leave the log exactly as it was")


def test_a_concurrent_append_is_not_reported_as_truncation(tmp_path,
                                                          monkeypatch):
    """The witness must be sampled BEFORE the log, and the order is the point.

    A writer appends the record and then updates the witness. A reader that
    sampled them in that same order could read the log before another
    process's append and the witness after it, and would then report
    TRUNCATED -- damage -- for a log that was merely being written to. Six
    processes produced exactly that: "witness records seq 33 but the log
    ends at 32".

    Raced deterministically rather than by hope: the second writer's append
    is triggered from inside the witness read, which is the one interleaving
    that tells the two orders apart.
    """
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="a", action="probe", target="t", payload={"i": 0})
    other = EventLog(tmp_path / "log.jsonl")

    real_read = log.read
    raced = []

    def read_then_somebody_writes(*a, **kw):
        events = real_read(*a, **kw)
        if not raced:
            # AFTER this reader has seen the log and before it could look at
            # the witness. Triggered from the read rather than from the
            # witness lookup on purpose: a race keyed to the witness fires
            # at a different point in each ordering and would pass under
            # both.
            raced.append(1)
            other.append(actor="b", action="probe", target="t",
                         payload={"i": 1})
        return events

    monkeypatch.setattr(log, "read", read_then_somebody_writes)
    report = log.verify()
    monkeypatch.undo()

    assert raced, "the concurrent append never happened; this proves nothing"
    assert report.ok, (
        f"an ordinary concurrent append was reported as damage: "
        f"{report.problems}")
    assert report.count == 1, (
        "this reader saw one record and another arrived after it looked; "
        "the next read will see both")


def test_a_refused_lease_renewal_writes_nothing(tmp_path):
    """The ordering defect, pinned.

    renew_lease decides nothing itself: every rule lives in the reducer, on
    replay. So it appended FIRST and folded second, and a refusal -- a
    lapsed lease -- was raised after the record was already durable. The
    caller saw the right exception and every later load() hit the same
    refusal with nothing to catch it.
    """
    from qta_agent.scheduler import JobTransitionError

    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()
    sched.dispatch(job_id="j1", worker="w1", lease_id="L1", lease_seqs=1)
    for i in range(4):                     # move the log past the lease
        sched.enqueue(job_id=f"filler-{i}", work_digest=digest({"f": i}),
                      submitter="sub")
    before = EventLog(tmp_path / "log.jsonl").verify().count

    with pytest.raises(JobTransitionError, match="lapsed"):
        sched.renew_lease(job_id="j1", worker="w1", lease_id="L1",
                          lease_seqs=50)

    assert EventLog(tmp_path / "log.jsonl").verify().count == before, (
        "the refusal must happen before the record, not after it")
    _replayable(tmp_path)


def test_a_refused_transition_writes_nothing(tmp_path):
    """The same property on the authority store."""
    from qta_agent.authority import TransitionError

    log = EventLog(tmp_path / "log.jsonl")
    store = AuthorityStore(log).load()
    store.create(record_id="r1", kind="claim", proposer="p1")
    before = log.verify().count

    with pytest.raises((TransitionError, Exception)):
        store.transition(record_id="r1", dst=State.PROMOTED, actor="p1",
                         role=Role.PROMOTER)
    assert EventLog(tmp_path / "log.jsonl").verify().count == before
    AuthorityStore(EventLog(tmp_path / "log.jsonl")).load()


# ---- the authority store -------------------------------------------------
def test_only_one_process_can_move_one_record(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    store = AuthorityStore(log, evidence=EvidenceStore(tmp_path / "evidence"))
    store.load()
    store.create(record_id="r1", kind="claim", proposer="p1")

    results = _run(_transition_worker,
                   [(str(tmp_path / "log.jsonl"), t, _go(tmp_path))
                    for t in range(RACERS)], tmp_path)

    assert len([r for r in results if r[0] == "won"]) == 1, results
    rebuilt = AuthorityStore(EventLog(tmp_path / "log.jsonl")).load()
    assert rebuilt.get("r1").state is State.UNDER_REVIEW
    assert rebuilt.get("r1").revision == 2, (
        "one transition, one revision: a second applied record would mean "
        "two verifiers each believe they took this record under review")


def test_only_one_process_can_create_one_record_id(tmp_path):
    """A record id is a claim to a name. Two creates would silently make one
    of the two proposers the author of a record they did not write."""
    log = EventLog(tmp_path / "log.jsonl")
    AuthorityStore(log).load()
    log.append(actor="owner", action="policy.publish", target="bootstrap",
               payload={"note": "so the log is not empty"})

    results = _run(_create_worker,
                   [(str(tmp_path / "log.jsonl"), t, _go(tmp_path))
                    for t in range(RACERS)], tmp_path)

    won = [r for r in results if r[0] == "won"]
    assert len(won) == 1, results
    losers = [r[2] for r in results if r[0] == "refused"]
    assert all("already exists" in m for m in losers), losers
    rebuilt = AuthorityStore(EventLog(tmp_path / "log.jsonl")).load()
    assert rebuilt.get("shared").proposer == f"p{won[0][1]}"


# ---- the long one --------------------------------------------------------
@pytest.mark.parametrize("rounds", [250])
def test_a_long_mixed_campaign_never_leaves_an_unreplayable_log(tmp_path,
                                                                rounds):
    """Six workers, every operation, all on one log, for a while.

    The single-race tests above each pin one interleaving that was reasoned
    about in advance. This one is here for the interleaving nobody thought
    of: enqueue against reconcile, renewal against lapse, report against
    dispatch, priority against everything.

    The assertion is deliberately narrow and absolute. Refusals are fine and
    expected -- that is what a contended queue looks like. What is never
    acceptable is a log that cannot be rebuilt, because the log IS the
    authority and there is no repair for it.
    """
    log, sched = _world(tmp_path)
    workers = 6
    results = _run(
        _stress_worker,
        [(str(tmp_path / "log.jsonl"), t, rounds, 1000 + t)
         for t in range(workers)],
        tmp_path, procs=workers)

    done = sum(d for _, d, _ in results)
    refused = sum(r for _, _, r in results)
    assert done + refused == workers * rounds

    report = EventLog(tmp_path / "log.jsonl").verify()
    assert report.ok, report.problems[:5]
    rebuilt = _replayable(tmp_path)

    # ANTI-VACUITY. Most operations here are legitimately refused -- a
    # dispatch of a job somebody else is already running, a renewal of a
    # lease that lapsed -- and that is what a contended queue looks like, so
    # a ratio would be an arbitrary line. What must be true is that the
    # campaign actually happened: every worker got work in, the log grew,
    # and the queue reached several different states rather than sitting in
    # one. A stress test where nothing was accepted proves nothing about
    # concurrency, and would pass every assertion below it.
    per_worker = {tag: d for tag, d, _ in results}
    assert all(d > 0 for d in per_worker.values()), (
        f"a worker recorded nothing at all: {per_worker}")
    assert report.count > workers * 10, (
        f"the log grew by only {report.count} records under "
        f"{workers * rounds} attempted operations")
    # Judged on the TRANSITIONS the log records, not on where the jobs
    # happened to stop. Most jobs end SUCCEEDED or in flight whatever route
    # they took, so counting final states would pass a campaign that only
    # ever enqueued and dispatched.
    moved = Counter(e.payload["dst"] for e in EventLog(
        tmp_path / "log.jsonl").read()
        if e.action == ACT_JOB_TRANSITION)
    assert len(moved) >= 4, (
        f"the campaign only ever recorded {sorted(moved)}; a run that never "
        "requeued, retried or failed anything is not exercising the "
        "transitions this is about")
    assert {"DISPATCHED", "SUCCEEDED"} <= set(moved), sorted(moved)

    for job in rebuilt.all_jobs().values():
        if job.state is JobState.DISPATCHED:
            assert job.lease_holder, (
                "a dispatched job with no holder is a lease nobody owns")
        assert job.attempts <= job.max_attempts, (
            f"{job.job_id} was attempted {job.attempts} times against a "
            f"budget of {job.max_attempts}. Found this way: the retry "
            "budget was consulted only where a worker REPORTED a failure, "
            "so a lapsed lease requeued the job forever")


def test_the_stress_campaign_is_reproducible(tmp_path):
    """Seeded, so a failure is a failure somebody can re-run.

    An unseeded stress test that fails once and never again is a rumour.
    """
    assert _stress_worker.__doc__
    first = random.Random(1000).choices(OPS, k=20)
    second = random.Random(1000).choices(OPS, k=20)
    assert first == second


def test_reconcile_survives_a_job_that_moved_under_its_scan(tmp_path,
                                                            monkeypatch):
    """reconcile scans, then writes. Another process writes in between.

    This is not an exotic interleaving: reconcile is what a supervisor runs
    on a tick and what a restarted process runs on startup, so two of them
    overlapping is the ordinary case in any deployment with more than one
    supervisor. Before the fix the whole convergence pass raised, so one
    contended job stopped every other job from being reconciled at all.
    """
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()
    sched.dispatch(job_id="j1", worker="w1", lease_id="L1", lease_seqs=1)
    for i in range(4):                     # move the log past the lease
        sched.enqueue(job_id=f"filler-{i}", work_digest=digest({"f": i}),
                      submitter="sub")

    other = Scheduler(EventLog(tmp_path / "log.jsonl"),
                      policy=PolicyStore(EventLog(
                          tmp_path / "log.jsonl")).load(),
                      policy_id="scheduler.default",
                      capacity={"slots": 1000}).load()
    real_scan = sched.expired_leases
    raced = []

    def scan_then_somebody_else_takes_it(*a, **kw):
        found = real_scan(*a, **kw)
        if found and not raced:
            raced.append([j.job_id for j in found])
            other.reconcile()
            other.dispatch(job_id="j1", worker="w2", lease_id="L2",
                           lease_seqs=500)
        return found

    monkeypatch.setattr(sched, "expired_leases",
                        scan_then_somebody_else_takes_it)
    moves = sched.reconcile()
    monkeypatch.undo()

    assert raced == [["j1"]], (
        "the scan must have found the lapsed lease, or this races nothing")
    assert not any(m.job_id == "j1" for m in moves), (
        "the requeue must have been skipped, not applied: the job is "
        "somebody else's now")
    rebuilt = _replayable(tmp_path)
    assert rebuilt.get("j1").lease_holder == "w2"
    assert rebuilt.get("j1").state is JobState.DISPATCHED


# ---- a hostile process, racing honest ones -------------------------------
#
# R54 said the adversarial campaign was "one shared world in one process",
# and that a hostile participant racing an honest one is where the advisory
# lock and the optimistic-concurrency checks would be under real pressure.
# These are that. The attacker here is a REGISTERED participant with write
# access to the log file, which is the strongest position a compromised
# component actually holds.

def _forge(path, src, dst, job_id, actor):
    """Append a well-formed transition the gate would never have written."""
    sys.path.insert(0, str(ROOT))
    from qta_agent.events import EventLog as _EL
    from qta_agent.scheduler import ACT_JOB_TRANSITION as _ACT
    _EL(path).append(
        actor=actor, action=_ACT, target=job_id,
        payload={"job_id": job_id, "src": src, "dst": dst,
                 "reason": "forged", "lease_id": "L-mallory",
                 "lease_holder": actor, "lease_expires_after_seq": 10_000,
                 "lease_renewals": 0, "attempts": 1})


def _hostile_worker(args):
    """Forge a transition while honest workers are appending."""
    path, tag, src, go = args
    _wait_for_start(go)
    _forge(path, src, "DISPATCHED", "j1", f"mallory-{tag}")
    return tag


#: Honest operations each worker completes BEFORE the forgery lands.
OPS_BEFORE_FORGERY = 2
OPS_PER_WORKER = 6
HONEST_WORKERS = 3


def _wait_for_file(path: Path, what: str) -> None:
    deadline = time.monotonic() + START_TIMEOUT_S
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} never arrived")
        time.sleep(0.001)


def _honest_worker_staged(args):
    """Six enqueues, with a rendezvous after the second.

    THE RENDEZVOUS IS THE POINT. The first version of this test released
    every process at once and slept, hoping the forgery would land in the
    middle of the honest campaign. On a hosted runner it did not: all
    eighteen operations finished first, the assertion's own anti-vacuity
    band was missed, and the suite went red for winning a race rather than
    for a defect. A test whose PASS depends on timing is a test that will
    eventually report something untrue in one direction or the other.

    So the interleaving is arranged rather than hoped for: this worker
    announces that it is underway, waits for the attacker to strike, and
    only then continues. What it measures is unchanged -- how many honest
    operations survive -- and the number is now the same on every machine.
    """
    path, tag, go, work = args
    sched = _open_scheduler(path)
    _wait_for_start(go)
    done = 0
    for i in range(OPS_PER_WORKER):
        if i == OPS_BEFORE_FORGERY:
            Path(work, f"midway-{tag}").write_text("1", encoding="utf-8")
            _wait_for_file(Path(work, f"forged-{POISONING_FORGE}"),
                           "the forgery that poisons the log")
            # A FRESH READER for the rest, and that is what makes the
            # number exact.
            #
            # An EXISTING reader does not fail on the next operation: it
            # folds incrementally from an anchor, so it notices the forged
            # record only when its own fold crosses it -- measured here at
            # three more operations, and dependent on how the three workers
            # interleave. That is the real behaviour and it is not a count
            # a test can assert.
            #
            # A reader that starts AFTER the forgery replays from the
            # beginning and cannot get past it, every time. Same denial,
            # stated where it is deterministic: this is the participant who
            # restarts, or the one who arrives late, and neither can work
            # again.
            #
            # It cannot even OPEN: load() replays, the replay hits the
            # forged record, and the reader never exists. Counted as a
            # failure of every remaining operation, which is what it is.
            try:
                sched = _open_scheduler(path)
            except Exception:                   # noqa: BLE001 - counted
                sched = None
        if sched is None:
            continue
        try:
            sched.enqueue(job_id=f"h{tag}-{i}",
                          work_digest=digest({"t": tag, "i": i}),
                          submitter="p1")
            done += 1
        except Exception:                       # noqa: BLE001 - counted
            pass
    return (tag, done)


#: Which forgery actually denies service, and why it is the SECOND one.
#:
#: Both attackers write ``j1: READY -> DISPATCHED``. The first is a legal
#: transition -- j1 really is READY after reconcile -- so it folds cleanly
#: and nothing notices. It is the second that is impossible, because by then
#: j1 is DISPATCHED, and that is the record the reducer refuses.
#:
#: Waiting on the first one is what made this test report 15 of 18 rather
#: than the 6 it was arranged to produce: the honest workers resumed after a
#: forgery that had done nothing, and began failing whenever the real one
#: happened to land. The subtlety is worth a name.
POISONING_FORGE = 1


def _hostile_worker_staged(args):
    """Strike once every honest worker is underway, then say so.

    Ordered: the attacker that lands second is the one whose record cannot
    be applied, so it waits for the first rather than racing it.
    """
    path, tag, go, work = args
    _wait_for_start(go)
    if tag == 0:
        for t in range(HONEST_WORKERS):
            _wait_for_file(Path(work, f"midway-{t}"), f"honest worker {t}")
    else:
        _wait_for_file(Path(work, f"forged-{tag - 1}"),
                       f"forgery {tag - 1}")
    _forge(path, "READY", "DISPATCHED", "j1", f"mallory-{tag}")
    Path(work, f"forged-{tag}").write_text("1", encoding="utf-8")
    return tag


def _honest_worker(args):
    """Ordinary work, alongside the attacker."""
    path, tag, go = args
    sched = _open_scheduler(path)
    _wait_for_start(go)
    done = 0
    for i in range(6):
        try:
            sched.enqueue(job_id=f"h{tag}-{i}",
                          work_digest=digest({"t": tag, "i": i}),
                          submitter="p1")
            done += 1
        except Exception:                       # noqa: BLE001 - counted
            pass
    return (tag, done)


def test_a_forged_record_from_another_process_is_refused_at_replay(tmp_path):
    """The attacker has the log file. It still cannot make a claim true.

    A forged transition is well-formed, correctly hash-chained (it went
    through append, like any record) and semantically impossible. The chain
    check cannot see it -- integrity is not authority -- and the reducer
    refuses it, which is the division of labour the whole design rests on.
    """
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()

    _run(_hostile_worker,
         [(str(tmp_path / "log.jsonl"), t, "SUCCEEDED", _go(tmp_path))
          for t in range(2)], tmp_path, procs=2)

    assert EventLog(tmp_path / "log.jsonl").verify().ok, (
        "the chain is intact: the attacker used the ordinary append path, "
        "and integrity was never the thing standing in its way")
    with pytest.raises(Exception) as caught:
        _replayable(tmp_path)
    assert "SUCCEEDED" in str(caught.value) or "READY" in str(caught.value), \
        caught.value


def test_a_forgery_denies_service_and_that_is_the_choice_that_was_made(
        tmp_path):
    """The honest consequence, stated rather than discovered later.

    A reducer that refuses an impossible record refuses the LOG it is in, so
    a component with write access to the shared log can stop every honest
    participant. Measured: three workers doing six enqueues each completed
    fifteen of eighteen, and the three that failed did so after the forgery
    landed.

    That is a deliberate trade and the alternative is worse. Skipping a
    record the reducer cannot apply would let a forger prune history by
    writing something unapplicable -- turning an availability attack into an
    integrity one, which is the direction this system exists to refuse. What
    is bought for it: everything written before the forgery is intact, the
    chain still verifies, and the forged record is attributable.
    """
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()

    path = str(tmp_path / "log.jsonl")
    work = str(tmp_path)
    total = HONEST_WORKERS * OPS_PER_WORKER
    with mp.get_context("spawn").Pool(HONEST_WORKERS + 2) as pool:
        honest = pool.map_async(
            _honest_worker_staged,
            [(path, t, _go(tmp_path), work) for t in range(HONEST_WORKERS)])
        hostile = pool.map_async(
            _hostile_worker_staged,
            [(path, t, _go(tmp_path), work) for t in range(2)])
        time.sleep(1.5)
        _release(tmp_path)
        results = honest.get(timeout=PROCESS_DEADLINE_S)
        hostile.get(timeout=PROCESS_DEADLINE_S)

    done = sum(d for _, d in results)
    assert 0 < done < total, (
        f"{done} of {total} honest operations completed; the interesting "
        "case is the middle, and this run was either unaffected by the "
        "attacker or stopped before it started")
    # AND THE EXACT NUMBER, because the interleaving is arranged and the
    # post-forgery reader is a fresh one. Each worker completes its two
    # pre-forgery enqueues and none of its four afterwards. A band would
    # pass on any partial denial; this fails if the denial ever becomes
    # partial, and it fails on every machine at the same number.
    assert done == HONEST_WORKERS * OPS_BEFORE_FORGERY, (
        f"{done} honest operations survived, expected "
        f"{HONEST_WORKERS * OPS_BEFORE_FORGERY}: every worker should "
        "complete exactly its pre-forgery operations and none after")
    assert all(d == OPS_BEFORE_FORGERY for _, d in results), (
        f"the denial did not reach every participant: {sorted(results)}")

    report = EventLog(path).verify()
    assert report.ok, report.problems[:3]
    landed = {e.payload["job"]["job_id"] for e in EventLog(path).read()
              if e.action == "scheduler.enqueue"}
    assert len(landed) == done + 1, (
        f"{done} operations reported success and {len(landed) - 1} enqueue "
        "records are in the log; every one that was told it succeeded has "
        "to be there")


def test_the_attacker_is_named_by_the_record_it_wrote(tmp_path):
    """A forgery that cannot be attributed is worse than one that can.

    The log's actor field is an assertion by whoever wrote the record -- it
    proves nothing about identity on its own -- and it is still the thing
    that turns "something impossible is in the log" into "this component
    wrote it", which is where an incident starts.
    """
    log, sched = _world(tmp_path)
    sched.enqueue(job_id="j1", work_digest=WORK, submitter="sub")
    sched.reconcile()
    _run(_hostile_worker,
         [(str(tmp_path / "log.jsonl"), 0, "SUCCEEDED", _go(tmp_path))],
         tmp_path, procs=1)

    forged = [e for e in EventLog(tmp_path / "log.jsonl").read()
              if e.payload.get("reason") == "forged"]
    assert len(forged) == 1
    assert forged[0].actor == "mallory-0"
    assert forged[0].seq >= 0
