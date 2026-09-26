"""Concurrency: real processes and real threads, not simulated interleavings.

WHY THIS SUITE EXISTS AS SEPARATE FILE

Every other test in this package drives the substrate single-threaded, and
single-threaded tests cannot see the class of defect that matters here. One
was found by running this: the event log's append is read-then-write with no
lock, so two concurrent writers both read the same head and both wrote a
record claiming the same ``seq``. That does not lose a record -- it corrupts
the chain, and every later append is then refused against the damage.

The processes here are real ``multiprocessing`` workers rather than threads
wherever the defect could hide behind the GIL, because a lock that only works
within one interpreter is not a lock for a system whose whole premise is a
durable file several things can open.
"""
from __future__ import annotations

import multiprocessing as mp
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:                # so hangguard imports without pytest
    sys.path.insert(0, HERE)

from hangguard import PROCESS_DEADLINE_S, deadline  # noqa: E402

from qta_agent.canonical import digest  # noqa: E402
from qta_agent.events import EventLog, EventLogError  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.scheduler import (  # noqa: E402
    JobState, JobTransitionError, Scheduler, SchedulerError, default_policy,
)
from qta_agent.store import AuthorityStore, ConcurrencyError  # noqa: E402

#: Small enough to stay fast, large enough that an unlocked append would
#: collide with near-certainty. Measured: at 4 x 15 the unlocked version
#: produced four records at seq 0 on the first run.
WRITERS = 4
PER_WRITER = 15

#: Every wait in this file is bounded, and that is not incidental tidiness.
#: A test that blocks reports nothing, and under the mutation harness it is
#: worse than nothing: the mutation counts as "KILLED (TIMEOUT)" while saying
#: nothing about which check was lost, and it burns the whole suite timeout on
#: every run. Two mutations here were in exactly that state -- an unbounded
#: lock wait and a leaked lock descriptor -- costing 300 seconds each per
#: hosted run to report a result nobody could act on.
#:
#: multiprocessing.Pool.map has no timeout, so map_async(...).get(timeout=)
#: is used instead; a hung worker then raises rather than parking the suite.
JOIN_TIMEOUT_S = 60.0

#: Longer than nothing here legitimately takes (every test in this file is
#: sub-second healthy) and shorter than EventLog.LOCK_TIMEOUT_S, so a single
#: append that blocks on a lock nobody will release fails here rather than
#: waiting the lock out once per append.
LOCK_WAIT_DEADLINE_S = 20.0


def _map_bounded(pool, fn, args, timeout: float = PROCESS_DEADLINE_S):
    """pool.map with a wall bound. A hung worker fails the test."""
    try:
        return pool.map_async(fn, args).get(timeout=timeout)
    except mp.TimeoutError as exc:
        raise AssertionError(
            f"a worker did not finish within {timeout}s; a blocked writer is "
            "the failure this suite exists to catch, and a test that waits "
            "for it forever reports nothing") from exc


# ---- module-level workers: multiprocessing needs them importable ---------
def _append_worker(args):
    path, tag, count = args
    sys.path.insert(0, str(ROOT))
    from qta_agent.events import EventLog as _EL
    log = _EL(path)
    ok = failed = 0
    for i in range(count):
        try:
            log.append(actor=f"writer-{tag}", action="probe", target="t",
                       payload={"writer": tag, "i": i})
            ok += 1
        except Exception:                       # noqa: BLE001 - counted
            failed += 1
    return ok, failed


def _evidence_worker(args):
    path, payload, count = args
    sys.path.insert(0, str(ROOT))
    from qta_agent.evidence import EvidenceStore as _ES
    store = _ES(path)
    return [store.put(payload.encode()) for _ in range(count)]


# ---- the event log -------------------------------------------------------
@pytest.mark.parametrize("writers,each", [(WRITERS, PER_WRITER)])
def test_concurrent_processes_cannot_corrupt_the_chain(tmp_path, writers,
                                                       each):
    """The defect this suite was written to find, and its fix.

    Without the writer lock this produced N records at seq 0 and left the log
    permanently unappendable. With it, every append lands and the chain
    verifies -- which is the only acceptable outcome, because a corrupted
    authority log cannot be repaired without discarding history.
    """
    path = tmp_path / "log.jsonl"
    args = [(str(path), tag, each) for tag in range(writers)]
    with mp.get_context("spawn").Pool(writers) as pool:
        results = _map_bounded(pool, _append_worker, args)

    assert all(failed == 0 for _, failed in results), results
    assert sum(ok for ok, _ in results) == writers * each

    report = EventLog(path).verify()
    assert report.ok, report.problems[:3]
    assert report.count == writers * each
    seqs = [ev.seq for ev in EventLog(path).read()]
    assert seqs == list(range(writers * each)), (
        "sequence numbers must be contiguous and unique; a duplicate seq is "
        "the signature of two writers reading one head")


def test_every_writer_s_records_survive(tmp_path):
    """Not just 'the chain is valid' -- nobody's work may be silently lost."""
    path = tmp_path / "log.jsonl"
    args = [(str(path), tag, 8) for tag in range(3)]
    with mp.get_context("spawn").Pool(3) as pool:
        _map_bounded(pool, _append_worker, args)
    by_writer: dict = {}
    for ev in EventLog(path).read():
        by_writer.setdefault(ev.payload["writer"], set()).add(
            ev.payload["i"])
    assert by_writer == {0: set(range(8)), 1: set(range(8)),
                         2: set(range(8))}


def test_concurrent_threads_cannot_corrupt_the_chain(tmp_path):
    """The same property within one interpreter.

    Threads are the weaker test -- the GIL hides some interleavings -- so it
    is here as a regression guard rather than as the primary evidence.
    """
    path = tmp_path / "log.jsonl"
    errors: list = []

    def run(tag):
        log = EventLog(path)
        for i in range(10):
            try:
                log.append(actor=f"t{tag}", action="probe", target="t",
                           payload={"writer": tag, "i": i})
            except Exception as exc:            # noqa: BLE001 - collected
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT_S)
    assert not any(t.is_alive() for t in threads), "a writer never finished"
    assert not errors, errors[:3]
    assert EventLog(path).verify().ok


def test_the_head_witness_agrees_with_the_log_after_concurrent_writes(
        tmp_path):
    path = tmp_path / "log.jsonl"
    args = [(str(path), tag, 10) for tag in range(3)]
    with mp.get_context("spawn").Pool(3) as pool:
        _map_bounded(pool, _append_worker, args)
    log = EventLog(path)
    witness = log.head()
    events = log.read()
    assert witness is not None
    assert witness.seq == events[-1].seq
    assert witness.head_hash == events[-1].hash


def test_the_lock_is_a_sidecar_so_it_survives_a_missing_log(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    assert log.lock_path.name == "log.jsonl.lock"
    with log.exclusive():
        pass
    assert log.lock_path.exists()
    assert not log.path.exists(), (
        "taking the lock must not create the log; locking a file that does "
        "not exist is what the sidecar avoids")


def test_a_held_lock_makes_an_append_refuse_rather_than_hang(tmp_path,
                                                             monkeypatch):
    """A bounded wait, because a blocked append is indistinguishable from a
    hang."""
    log = EventLog(tmp_path / "log.jsonl")
    monkeypatch.setattr(EventLog, "LOCK_TIMEOUT_S", 0.2)
    holder = EventLog(tmp_path / "log.jsonl")
    # The deadline is the point, not decoration. Without it, deleting the
    # deadline CHECK inside exclusive() makes this test spin forever: the
    # mutation is then "killed" by the harness timing out, which reports
    # nothing about the lost check and costs 300s of wall clock every run.
    with deadline(5.0):
        with holder.exclusive():
            with pytest.raises(EventLogError, match="writer lock"):
                log.append(actor="a", action="probe", target="t", payload={})


def test_the_lock_is_released_even_when_the_body_raises(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    with pytest.raises(RuntimeError):
        with log.exclusive():
            raise RuntimeError("boom")
    # A second acquisition would block forever if the first leaked the lock.
    with log.exclusive():
        pass


def test_the_module_states_what_the_lock_does_not_cover():
    """Advisory, one host, local filesystem. Said in the module, not here."""
    import re

    import qta_agent.events as mod
    doc = re.sub(r"\s+", " ", mod.__doc__ or "")
    assert "ADVISORY" in doc
    assert "NFS" in doc
    assert "REFUSES rather than proceeding unlocked" in doc


# ---- the evidence store --------------------------------------------------
def test_concurrent_inserts_of_the_same_content_agree(tmp_path):
    """Content addressing means the race has no losing side.

    Two writers storing identical bytes must produce one blob and one digest,
    and neither may see a partially-written file.
    """
    path = tmp_path / "evidence"
    payload = "the same bytes from every writer" * 200
    args = [(str(path), payload, 5) for _ in range(4)]
    with mp.get_context("spawn").Pool(4) as pool:
        results = _map_bounded(pool, _evidence_worker, args)

    digests = {d for batch in results for d in batch}
    assert len(digests) == 1
    store = EvidenceStore(path)
    (only,) = digests
    assert store.get(only) == payload.encode()
    assert store.verify_store().ok


def test_concurrent_inserts_of_different_content_all_survive(tmp_path):
    path = tmp_path / "evidence"
    args = [(str(path), f"writer {t} content", 3) for t in range(4)]
    with mp.get_context("spawn").Pool(4) as pool:
        results = _map_bounded(pool, _evidence_worker, args)
    digests = {d for batch in results for d in batch}
    assert len(digests) == 4
    store = EvidenceStore(path)
    assert set(store.list_digests()) == digests
    assert store.verify_store().ok


# ---- optimistic concurrency ---------------------------------------------
def test_two_writers_racing_on_one_record_do_not_both_win(tmp_path):
    """Last-writer-wins is never used for authority; the loser is told."""
    # Bounded: a leaked lock descriptor makes every later
    # append wait out the whole lock timeout, so an unbounded
    # version of this test parks the suite for minutes.
    with deadline(LOCK_WAIT_DEADLINE_S):
        from qta_agent.authority import Role, State

        log = EventLog(tmp_path / "log.jsonl")
        store = AuthorityStore(log).load()
        store.create(record_id="r1", kind="claim", proposer="alice")
        stale = store.get("r1").revision

        store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                         role=Role.VERIFIER, expected_revision=stale)
        with pytest.raises(ConcurrencyError, match="changed since it was read"):
            store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="carol",
                             role=Role.VERIFIER, expected_revision=stale)


def test_two_workers_cannot_both_take_one_job(tmp_path):
    # Bounded: a leaked lock descriptor makes every later
    # append wait out the whole lock timeout, so an unbounded
    # version of this test parks the suite for minutes.
    with deadline(LOCK_WAIT_DEADLINE_S):
        log = EventLog(tmp_path / "log.jsonl")
        pol = PolicyStore(log).load()
        pol.publish(default_policy(), actor="owner")
        sched = Scheduler(log, policy=pol, policy_id="scheduler.default",
                          capacity={"slots": 4}).load()
        sched.enqueue(job_id="j1", work_digest=digest({"w": 1}),
                      submitter="owner")
        sched.reconcile()

        sched.dispatch(job_id="j1", worker="w1", lease_id="L1", lease_seqs=50)
        with pytest.raises(JobTransitionError, match="only a READY job"):
            sched.dispatch(job_id="j1", worker="w2", lease_id="L2",
                           lease_seqs=50)
        assert sched.get("j1").lease_holder == "w1"


def test_a_stale_revision_cannot_move_a_job(tmp_path):
    # Bounded: a leaked lock descriptor makes every later
    # append wait out the whole lock timeout, so an unbounded
    # version of this test parks the suite for minutes.
    with deadline(LOCK_WAIT_DEADLINE_S):
        log = EventLog(tmp_path / "log.jsonl")
        pol = PolicyStore(log).load()
        pol.publish(default_policy(), actor="owner")
        sched = Scheduler(log, policy=pol, policy_id="scheduler.default").load()
        sched.enqueue(job_id="j1", work_digest=digest({"w": 1}),
                      submitter="owner")
        stale = sched.get("j1").revision
        sched.set_priority(job_id="j1", priority=2, actor="scheduler",
                           role="SCHEDULER", reason="bump")
        with pytest.raises(SchedulerError, match="changed since it was read"):
            sched.transition(job_id="j1", dst=JobState.READY, actor="scheduler",
                             expected_revision=stale)


def test_a_platform_without_advisory_locking_refuses_to_append(tmp_path,
                                                               monkeypatch):
    """Refusing beats appending unlocked.

    An unlocked append does not lose a record; it corrupts the chain. So a
    platform that cannot provide the lock gets an error naming the reason
    rather than a log that works until the first moment of concurrency.
    """
    import qta_agent.events as mod

    monkeypatch.setattr(mod, "fcntl", None)
    log = EventLog(tmp_path / "log.jsonl")
    with pytest.raises(EventLogError, match="POSIX advisory locking"):
        log.append(actor="a", action="probe", target="t", payload={})
    assert not (tmp_path / "log.jsonl").exists(), (
        "the refusal must happen before anything is written")


def test_the_lock_descriptor_is_closed_so_the_next_writer_can_proceed(
        tmp_path, monkeypatch):
    """Closing the descriptor is what releases a flock.

    A leaked descriptor holds the lock for the life of the process, and the
    next writer then waits for a holder that has already finished.
    """
    monkeypatch.setattr(EventLog, "LOCK_TIMEOUT_S", 1.0)
    log = EventLog(tmp_path / "log.jsonl")
    for i in range(3):
        with log.exclusive():
            pass
    log.append(actor="a", action="probe", target="t", payload={"i": 0})
    log.append(actor="a", action="probe", target="t", payload={"i": 1})
    assert log.verify().count == 2
