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

from agent_substrate.canonical import digest, is_digest  # noqa: E402
from agent_substrate.events import ChainBroken, EventLog  # noqa: E402
from agent_substrate.policy import PolicyStore  # noqa: E402
from agent_substrate.scheduler import Scheduler, default_policy  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402


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

    def read(self, *, strict: bool = True, **kw):
        self.full_reads += 1
        return super().read(strict=strict, **kw)

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


# ---- an anchor names what was verified (AUD-003) ---------------------------
#
# A projection used to verify the log, fold it, and then call anchor_at() for
# the position it reached: a second read, whose record was checked only
# against its own hash. Nothing compared it with the record the verification
# returned, so a history replaced between the two reads was anchored on, and
# every later advance trusted a prefix no verification had seen. The window
# below opens as early as that can matter: the moment the verified read has
# PARSED the log, before it checks a link, the log is replaced by a
# self-consistent history of the same length, its witness with it. Any read
# after that one -- in the caller, or inside the verified read itself -- sees
# the replacement.

def _replace_head_in_place(log: EventLog) -> None:
    """Rewrite the final record as another valid record of the same length,
    re-hashed, and move the witness to it."""
    from agent_substrate.canonical import canonical_bytes
    from agent_substrate.events import ChainState, Event
    lines = log.path.read_bytes().split(b"\n")
    rec = json.loads(lines[-2])
    rec["actor"] = "z" * len(rec["actor"])
    body = {k: v for k, v in rec.items() if k != "hash"}
    ev = Event(**body, hash=digest(body))
    lines[-2] = canonical_bytes(ev.to_record())
    log.path.write_bytes(b"\n".join(lines))
    log._write_head(ChainState(ev.seq, ev.hash))


class _ReplaceAfterVerifiedRead:
    """Replace the log's head once, right after the log is first parsed."""

    def __init__(self, monkeypatch):
        self.verified_head = None
        real = EventLog.read
        window = self

        def read_then_replace(log_self, **kw):
            events = real(log_self, **kw)
            if window.verified_head is None and events:
                window.verified_head = events[-1].hash
                _replace_head_in_place(log_self)
            return events

        monkeypatch.setattr(EventLog, "read", read_then_replace)


def _records(path, n=4):
    log = EventLog(path)
    store = AuthorityStore(log).load()
    for i in range(n):
        store.create(record_id=f"r{i}", kind="claim", proposer="p")
    return log


def test_a_projection_resumes_from_the_bytes_it_verified(tmp_path,
                                                         monkeypatch):
    path = tmp_path / "log.jsonl"
    _records(path)
    window = _ReplaceAfterVerifiedRead(monkeypatch)
    store = AuthorityStore(EventLog(path)).load()
    monkeypatch.undo()
    assert window.verified_head, "the window never opened"
    now = EventLog(path).read_verified()[1][-1].hash
    assert now != window.verified_head, "the history was not replaced"
    assert store._anchor.head_hash == window.verified_head, (
        "the projection anchored on a record its verification never read")


def test_a_checkpoint_names_the_record_its_verification_read(tmp_path,
                                                             monkeypatch):
    from agent_substrate.checkpoint import create
    path = tmp_path / "log.jsonl"
    log = _records(path)
    window = _ReplaceAfterVerifiedRead(monkeypatch)
    cp = create(log)
    monkeypatch.undo()
    assert window.verified_head, "the window never opened"
    assert cp.full_verification is True
    assert cp.head_hash == window.verified_head, (
        "a checkpoint marked full_verification names a record that "
        "verification did not read")


def test_an_unverified_checkpoint_is_refused_where_log_and_witness_differ(
        tmp_path):
    """No verification, so the position is the witness's; the record found
    there must be the one the witness names, not whatever is there now."""
    from agent_substrate.checkpoint import create
    from agent_substrate.events import ChainState
    log = _records(tmp_path / "log.jsonl")
    witness = log.head()
    _replace_head_in_place(log)
    log._write_head(witness)
    with pytest.raises(ChainBroken, match="verified as"):
        create(log, require_full_verification=False)
    _replace_head_in_place(log)          # the control: a witness that agrees
    head = log.head()
    assert isinstance(head, ChainState)
    assert create(log, require_full_verification=False).head_hash == \
        head.head_hash


def _unbound_anchor_calls(source: str) -> list:
    """Lines of ``source`` that call anchor_at() without naming the hash."""
    import ast
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "anchor_at"
            and not any(k.arg == "head_hash" for k in node.keywords)]


def test_no_production_module_anchors_on_an_unbound_second_read():
    """AUD-003 at every call site: a module that holds a verified read takes
    ``report.verified_anchor``; one that does not names the hash it expects.
    """
    found = {}
    for path in sorted((ROOT / "agent_substrate").glob("*.py")):
        lines = _unbound_anchor_calls(path.read_text(encoding="utf-8"))
        if lines:
            found[path.name] = lines
    assert found == {}, found


def test_the_unbound_anchor_scan_sees_one():
    """The control: without it the scan above could be finding nothing
    because it can see nothing."""
    assert _unbound_anchor_calls(
        "a = log.anchor_at(3)\nb = log.anchor_at(3, head_hash=h)\n") == [1]


# ---- a projection holds the log to what it folded (AUD-006) ---------------
#
# A projection catches up by seq: it folds what lies past its position and
# takes everything at or below it to be what it already folded. That holds
# while the log only grows, and nothing checked it. A log restored from a
# backup -- witness and all, so every verification passes -- holds other
# records below the position. A live store then joined the restored
# history's tail to the old history's state (r0, r1, r3 where a fresh replay
# gives r0, r2, r3), decided on r1, which the log no longer held, and wrote a
# transition for it that no replay of the log can apply.
#
# Two routes lead there, and each class is held on both: the full read a
# catch-up falls back to when its anchor no longer fits, and an anchor that
# lags the records a projection appended itself, which advance() follows
# without finding anything wrong. Two projections REBUILD from scratch on
# the first route instead of folding onto what they hold; for them the
# property is that they then hold the log as it is.

def _create(log, rid):
    return log.append(actor="p", action="record.create", target=rid,
                      payload={"record_id": rid, "kind": "claim",
                               "proposer": "p"})


def _backup(path):
    return path.read_bytes(), EventLog(path).head_path.read_bytes()


def _restore_and_go_on(path, backup, *rids):
    """Put the backup back, log and witness, and let honest writers go on."""
    path.write_bytes(backup[0])
    EventLog(path).head_path.write_bytes(backup[1])
    log = EventLog(path)
    for rid in rids:
        _create(log, rid)


def _projections():
    from agent_substrate.agents import AgentDirectory
    from agent_substrate.capability import CapabilityLedger
    from agent_substrate.idempotency import IdempotencyLedger
    from agent_substrate.memory import MemoryStore
    from agent_substrate.proposals import ProposalIngress, _fold_receipts

    def own(position, hashed, folds=None):
        """Fold a record as the class folds its own append: the record is
        applied, the position moves, the anchor does not."""
        def run(p, ev):
            if folds is not None:
                folds(p, ev)
            if position is not None:
                setattr(p, position, ev.seq)
                setattr(p, hashed, ev.hash)
        return run

    return {
        # name: (make, catch_up, folds_own_record, rebuilds_on_fallback)
        "authority_store": (AuthorityStore,
                            lambda p: p.catch_up(force=True),
                            own(None, None, lambda p, ev: p._apply(ev)),
                            False),
        "scheduler": (lambda log: Scheduler(log, policy=PolicyStore(log),
                                            policy_id="scheduler.default"),
                      lambda p: p.catch_up(force=True),
                      own(None, None, lambda p, ev: p.apply(ev)), False),
        "agents": (AgentDirectory, lambda p: p._fold_new(),
                   own(None, None, lambda p, ev: p.apply(ev)), False),
        "policy": (PolicyStore, lambda p: p._fold_new(),
                   own(None, None, lambda p, ev: p.apply(ev)), False),
        "memory": (MemoryStore, lambda p: p._fold_new(),
                   own(None, None, lambda p, ev: p.apply(ev)), False),
        "capability": (CapabilityLedger, lambda p: p._fold_new(),
                       own(None, None, lambda p, ev: p.apply(ev)), False),
        "idempotency": (IdempotencyLedger, lambda p: p.catch_up(),
                        own("_folded_through", "_folded_hash",
                            lambda p, ev: p.apply(ev)), True),
        "proposals": (ProposalIngress, lambda p: p._catch_up(),
                      own("_folded_through", "_folded_hash",
                          lambda p, ev: _fold_receipts([ev], p._received)),
                      True),
    }


_PROJECTIONS = sorted(_projections())


def _load(make, path):
    p = make(EventLog(path))
    return p.load() if hasattr(p, "load") else (p._catch_up(), p)[1]


@pytest.mark.parametrize("name", _PROJECTIONS)
def test_catching_up_onto_a_restored_history_does_not_fold_onto_it(
        tmp_path, name):
    """The fallback route: the anchor no longer fits, a full read follows."""
    make, catch_up, _, rebuilds = _projections()[name]
    path = tmp_path / "log.jsonl"
    _create(EventLog(path), "r0")
    backup = _backup(path)
    _create(EventLog(path), "r1")
    live = _load(make, path)
    _restore_and_go_on(path, backup, "r2", "r3")
    head = EventLog(path).read_verified()[1][-1]
    if rebuilds:
        catch_up(live)
        assert live._folded_through == head.seq
        assert live._folded_hash == head.hash, (
            "it rebuilt, and does not hold the log's head")
    else:
        with pytest.raises(ChainBroken, match="rewritten"):
            catch_up(live)
        with pytest.raises(ChainBroken, match="rewritten"):
            catch_up(live)       # and stays refused until it is reloaded
        _load(make, path)        # which a fresh projection can do


@pytest.mark.parametrize("name", _PROJECTIONS)
def test_an_anchor_behind_its_own_appends_does_not_hide_a_restored_history(
        tmp_path, name):
    """The lag route: advance() succeeds, because nothing it reads changed."""
    make, catch_up, folds_own, _ = _projections()[name]
    path = tmp_path / "log.jsonl"
    _create(EventLog(path), "r0")
    live = _load(make, path)
    backup = _backup(path)
    folds_own(live, _create(live.log, "r1"))
    _restore_and_go_on(path, backup, "r2")
    with pytest.raises(ChainBroken, match="rewritten"):
        catch_up(live)


@pytest.mark.parametrize("name", _PROJECTIONS)
def test_a_log_that_only_grew_is_caught_up_on_both_routes(tmp_path, name):
    """The control: the same moves with no restore are folded, not refused."""
    make, catch_up, folds_own, _ = _projections()[name]
    path = tmp_path / "log.jsonl"
    _create(EventLog(path), "r0")
    live = _load(make, path)
    folds_own(live, _create(live.log, "r1"))
    _create(EventLog(path), "r2")
    catch_up(live)                                   # the anchored route
    _create(EventLog(path), "r3")
    for attr in ("_anchor", "_fold_anchor"):
        if hasattr(live, attr):
            setattr(live, attr, None)
    catch_up(live)                                   # the full-read route


def test_the_restore_this_suite_models_really_diverges(tmp_path):
    """Without this the refusals above could be refusing a log that never
    changed: a fresh replay of the restored log does not hold r1."""
    path = tmp_path / "log.jsonl"
    _create(EventLog(path), "r0")
    backup = _backup(path)
    _create(EventLog(path), "r1")
    _restore_and_go_on(path, backup, "r2", "r3")
    fresh = AuthorityStore(EventLog(path)).load()
    assert sorted(fresh._records) == ["r0", "r2", "r3"]


@pytest.mark.parametrize("name", _PROJECTIONS)
def test_a_log_restored_to_before_what_was_folded_is_refused(tmp_path, name):
    """The log now ends below the position: nothing to compare, and the
    projection holds a record the log does not. Both routes."""
    make, catch_up, folds_own, rebuilds = _projections()[name]
    path = tmp_path / "log.jsonl"
    _create(EventLog(path), "r0")
    live = _load(make, path)
    backup = _backup(path)
    folds_own(live, _create(live.log, "r1"))
    _restore_and_go_on(path, backup)
    with pytest.raises(ChainBroken, match="now ends at seq 0"):
        catch_up(live)                               # the anchored route
    if rebuilds:
        return
    path2 = path.parent / "other.jsonl"              # the full-read route
    _create(EventLog(path2), "r0")
    backup = _backup(path2)
    _create(EventLog(path2), "r1")
    live = _load(make, path2)
    _restore_and_go_on(path2, backup)
    with pytest.raises(ChainBroken, match="now ends at seq 0"):
        catch_up(live)
