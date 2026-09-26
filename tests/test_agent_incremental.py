"""Catching up in O(new), and the ways that goes wrong.

WHY THIS IS A SUITE AND NOT A PERFORMANCE TEST

Every reducer here re-reads the log before it decides, because a decision
made against a stale projection is what leaves a perfect hash chain nobody
can replay. Doing that with a full read made each governed operation cost the
whole history: a profile of 120 campaign cycles spent 10 of its 13 seconds
inside ``read()``, and doubling the campaign quadrupled its wall time. That
is the third quadratic path this repository has recorded, and like the first
two it was invisible to every targeted test -- nothing is wrong with any
single operation.

tests/test_agent_performance.py has the timing guard. This file has the
mechanism, checked by COUNTING rather than by clock: how many times the log
was read whole, whether each record was folded exactly once, and what happens
when the anchor no longer describes the bytes at its offset. A count is
deterministic where a duration is not, which is what makes these usable under
the mutation harness.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest, is_digest  # noqa: E402
from qta_agent.events import ChainBroken, EventLog  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.scheduler import Scheduler, default_policy  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402


class CountingLog(EventLog):
    """An EventLog that remembers how often it was read whole.

    Subclassed rather than patched so the count survives every internal
    call: the question is not "did this caller read the log" but "did
    anything read the whole history again", and the answer has to include
    reads made three frames down.
    """

    def __init__(self, path):
        super().__init__(path)
        self.full_reads = 0
        self.tail_reads = 0

    def read(self, *, strict: bool = True):
        self.full_reads += 1
        return super().read(strict=strict)

    def advance(self, anchor):
        self.tail_reads += 1
        return super().advance(anchor)


def _sched(log):
    pol = PolicyStore(log).load()
    try:
        pol.in_force("scheduler.default")
    except Exception:                       # noqa: BLE001 - not published yet
        pol.publish(default_policy(), actor="owner")
    return Scheduler(log, policy=pol, policy_id="scheduler.default",
                     capacity={"slots": 64}).load()


def test_catching_up_after_an_append_does_not_re_read_the_history(tmp_path):
    """THE property, counted.

    A projection that has just written a record knows where it is. Reading
    the log from the start to find out is the quadratic path, and it is
    invisible to anything except a count or a stopwatch.
    """
    log = CountingLog(tmp_path / "log.jsonl")
    sched = _sched(log)
    for i in range(30):
        sched.enqueue(job_id=f"j{i}", work_digest=digest({"i": i}),
                      submitter="p1")
    after_thirty = log.full_reads
    for i in range(30, 60):
        sched.enqueue(job_id=f"j{i}", work_digest=digest({"i": i}),
                      submitter="p1")
    assert log.full_reads == after_thirty, (
        f"the second thirty appends added {log.full_reads - after_thirty} "
        "full read(s) of the history; each one costs the whole log, which "
        "is what makes a long campaign quadratic")
    assert log.tail_reads > 30, (
        "and the incremental path has to be the one doing the work, or "
        "this passes because nothing read anything at all")


def test_a_projection_folds_each_record_exactly_once(tmp_path):
    """The anchor legitimately lags a projection by its own appends, so the
    tail it returns overlaps what has already been folded. Folding twice is
    not a slow path, it is a wrong one -- a second enqueue record for a live
    job is refused by the reducer, so this shows up as an exception rather
    than as a quiet duplicate."""
    log = EventLog(tmp_path / "log.jsonl")
    sched = _sched(log)
    for i in range(10):
        sched.enqueue(job_id=f"j{i}", work_digest=digest({"i": i}),
                      submitter="p1")
        sched.catch_up(force=True)
    assert len(sched.all_jobs()) == 10
    assert all(j.revision == 1 for j in sched.all_jobs().values()), (
        "a record folded twice bumps the revision without anything having "
        "happened")


def test_another_process_s_append_is_picked_up(tmp_path):
    """Incremental must not mean blind. The whole reason a reducer catches
    up is that somebody else may have written."""
    log = EventLog(tmp_path / "log.jsonl")
    sched = _sched(log)
    sched.enqueue(job_id="j0", work_digest=digest({"i": 0}), submitter="p1")

    other = _sched(EventLog(tmp_path / "log.jsonl"))
    other.enqueue(job_id="elsewhere", work_digest=digest({"x": 1}),
                  submitter="p2")

    assert "elsewhere" not in sched.all_jobs()
    sched.catch_up(force=True)
    assert "elsewhere" in sched.all_jobs()
    assert sched.at_seq() == log.verify().head_seq


def test_at_seq_is_the_log_s_position_and_not_a_remembered_one(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    sched = _sched(log)
    before = sched.at_seq()
    EventLog(tmp_path / "log.jsonl").append(
        actor="somebody", action="policy.publish", target="t", payload={})
    assert sched.at_seq() > before, (
        "decisions are made AT a log position; a remembered one belongs to "
        "a state that no longer exists")


def test_catching_up_onto_a_damaged_tail_refuses(tmp_path):
    """The incremental path must be no more trusting than the full one."""
    log = EventLog(tmp_path / "log.jsonl")
    sched = _sched(log)
    sched.enqueue(job_id="j0", work_digest=digest({"i": 0}), submitter="p1")

    path = tmp_path / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    rec["actor"] = "mallory"
    lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fresh = EventLog(path)
    with pytest.raises(ChainBroken):
        fresh.advance(fresh.anchor_at(0))


def test_an_anchor_into_rewritten_bytes_falls_back_rather_than_trusting(
        tmp_path):
    """An anchor is a shortcut, never a trust input.

    When the bytes at its offset are no longer the ones it described, the
    projection must take the slow path -- which is strictly stronger -- and
    not continue reading from an offset that now lands mid-record.
    """
    log = EventLog(tmp_path / "log.jsonl")
    sched = _sched(log)
    for i in range(4):
        sched.enqueue(job_id=f"j{i}", work_digest=digest({"i": i}),
                      submitter="p1")
    stale = sched._anchor
    assert stale is not None

    other = EventLog(tmp_path / "log.jsonl")
    other.append(actor="p", action="policy.publish", target="t", payload={})

    # An anchor from a different, shorter log: the offsets point at bytes
    # that mean something else now.
    elsewhere = tmp_path / "other.jsonl"
    small = EventLog(elsewhere)
    small.append(actor="p", action="policy.publish", target="t",
                 payload={"a": "b" * 400})
    sched._anchor = small.anchor_at(0)
    sched.catch_up(force=True)
    assert sched.at_seq() == log.verify().head_seq, (
        "the projection followed a stale anchor instead of falling back to "
        "a full verified pass")


def test_advance_returns_the_same_anchor_when_nothing_was_written(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    for i in range(3):
        log.append(actor="p", action="policy.publish", target="t",
                   payload={"i": i})
    anchor = log.anchor_at(2)
    events, again = log.advance(anchor)
    assert events == [] and again is anchor, (
        "a caller holding an anchor over an idle log must be able to keep "
        "holding it")


def test_advance_reports_the_position_it_reached(tmp_path):
    """A tail without a new position is a shortcut that shortens nothing.

    The records come back and the anchor does not move, so the next call
    re-reads the same tail and the one after that reads a longer one -- the
    quadratic cost restored while every correctness test still passes.
    """
    log = EventLog(tmp_path / "log.jsonl")
    for i in range(6):
        log.append(actor="p", action="policy.publish", target="t",
                   payload={"i": i})
    anchor = log.anchor_at(1)
    events, moved = log.advance(anchor)
    assert [e.seq for e in events] == [2, 3, 4, 5]
    assert moved.seq == 5, (
        f"advance read up to seq 5 and reported a position at "
        f"{moved.seq}")
    again, still = log.advance(moved)
    assert again == [] and still is moved


def test_the_authority_store_is_incremental_too(tmp_path):
    log = CountingLog(tmp_path / "log.jsonl")
    store = AuthorityStore(log).load()
    for i in range(20):
        store.create(record_id=f"r{i}", kind="claim", proposer="p1")
    # A projection that starts against an EMPTY log has no anchor to hold,
    # so its first write pays a full pass to establish one. That is a fixed
    # startup cost; what must not grow is the number of full passes.
    after_twenty = log.full_reads
    for i in range(20, 40):
        store.create(record_id=f"r{i}", kind="claim", proposer="p1")
    assert log.full_reads == after_twenty, (
        f"the second twenty creations added "
        f"{log.full_reads - after_twenty} full read(s) of the history")
    assert len(store.all_records()) == 40


def test_a_fresh_load_still_verifies_everything(tmp_path):
    """The bargain the anchored path makes is that the PREFIX is trusted.

    That is only acceptable because a process that starts fresh checks the
    whole chain, so damage anywhere is caught by somebody. If load() ever
    became incremental too, nothing would ever verify the beginning.
    """
    log = EventLog(tmp_path / "log.jsonl")
    sched = _sched(log)
    for i in range(4):
        sched.enqueue(job_id=f"j{i}", work_digest=digest({"i": i}),
                      submitter="p1")

    # wall_time, and the choice is the point. Every field a REDUCER reads
    # is re-authorized on replay, so tampering with one is caught by the
    # reducer whether the chain was verified or not -- and a test using one
    # would pass with verification removed. wall_time is hashed and read by
    # nothing, so the chain check is the only thing between this edit and a
    # projection built on it.
    path = tmp_path / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["wall_time"] = rec["wall_time"] + 1e-4
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Reloading THIS scheduler, rather than building a fresh one: a new
    # PolicyStore would verify the log on the way past and the refusal
    # would come from there, which would leave Scheduler.load()'s own
    # verification untested while the test still went green.
    with pytest.raises(Exception):
        sched.load()


# ---- the digest predicate, after it was made fast ------------------------
def test_is_digest_did_not_get_looser_when_it_got_faster():
    """A hot predicate replaced by a regex is exactly where a subtle
    widening hides: every caller keeps working and the class of things
    accepted grows."""
    assert is_digest("a" * 64)
    assert is_digest("0123456789abcdef" * 4)
    assert not is_digest("A" * 64), (
        "uppercase is refused on purpose: two spellings of one digest make "
        "a set of digests contain duplicates that compare unequal")
    assert not is_digest("g" * 64)
    assert not is_digest("a" * 63)
    assert not is_digest("a" * 65)
    assert not is_digest("a" * 32 + "\n" + "b" * 31), (
        "a newline inside a 64-character string must not pass; $ would have "
        "allowed a trailing one")
    assert not is_digest("a" * 63 + "\n")
    assert not is_digest(None) and not is_digest(64) and not is_digest(b"a" * 64)
