"""Every projection folds exactly the bytes whose integrity it established.

THE DEFECT CLASS

    log.verify().raise_if_bad()
    for ev in log.read():
        apply(ev)

verifies one read of a file and folds another. Other processes append to that
file, so a record landing between the two reads is folded without its chain
link ever having been checked by the call doing the folding. D-2026-41 closed
it in ``governed_stage10.projection`` and nowhere else; the same two lines
stood in fifteen more reducers, in ``EventLog.advance`` itself (the O(new)
path every live projection uses), and in ``AuthorityStore.load_from``, whose
comment claimed the tail it re-read was "the tail this load already reads and
already verified".

WHAT THESE TESTS HOLD OPEN

The window is opened deliberately: the FIRST read of the log (whole-file or
tail) returns, and before anything else reads, a record is appended whose own
hash is valid and whose chain link is not. A reducer that reads once never
sees it. A reducer that reads twice folds it.

The stronger property is asserted as well: every record a reducer receives is,
byte for byte, a line of the snapshot the verification pass read. That is the
directive's wording -- "the exact bytes used for reconstruction are the bytes
whose integrity was established" -- and it is checked on bytes, not on
counts, because a count can agree while the records differ.

Each test also proves the window opened (the hook fired) and that the forgery
really breaks the chain, so none of them can pass by testing nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qta_agent.agents import AgentDirectory  # noqa: E402
from qta_agent.audit import AuditIndex  # noqa: E402
from qta_agent.canonical import (  # noqa: E402
    CANONICAL_FORM_VERSION, canonical_bytes, digest,
)
from qta_agent.capability import CapabilityLedger  # noqa: E402
from qta_agent.checkpoint import CheckpointStore  # noqa: E402
from qta_agent.events import ChainBroken, Event, EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.idempotency import IdempotencyLedger  # noqa: E402
from qta_agent.memory import MemoryStore  # noqa: E402
from qta_agent.netauth import NetworkAuthority  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.reconstruct import (  # noqa: E402
    reconstruct, reconstruct_subsystems, reconstruct_tasks,
)
from qta_agent.scheduler import Scheduler  # noqa: E402
from qta_agent.secrets import SecretStore  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402

FORGED_ID = "FORGED-after-verification"


def _append_record(log, i):
    log.append(actor="a", action="record.create", target=f"r{i}",
               payload={"record_id": f"r{i}", "kind": "k", "proposer": "a"})


def _log(tmp_path, n=4):
    log = EventLog(tmp_path / "ev.jsonl")
    for i in range(n):
        _append_record(log, i)
    return log


def _forged_line(path) -> bytes:
    """A self-consistent record whose chain link does not hold.

    Its own hash is correct -- it is not garbage a parser would refuse -- and
    it creates an authority record, so any reducer that folds it produces
    state nobody authorized. Only the link to its predecessor is wrong, which
    is exactly the thing a reducer that skipped verification cannot see.
    """
    last = EventLog(path).read()[-1]
    body = {
        "seq": last.seq + 1, "event_id": "f" * 32,
        "wall_time": last.wall_time + 1.0, "actor": "mallory",
        "action": "record.create", "target": FORGED_ID,
        "payload": {"record_id": FORGED_ID, "kind": "k",
                    "proposer": "mallory"},
        "prev_hash": "0" * 64,
        "canonical_form_version": CANONICAL_FORM_VERSION,
    }
    ev = Event(**body, hash=digest(body))
    return canonical_bytes(ev.to_record()) + b"\n"


class _Window:
    """Append a forgery immediately after the FIRST read of ``path`` returns.

    ``method`` is the EventLog method whose first return opens the window:
    ``read`` for whole-log paths, ``_read_tail`` for anchored ones. The blob
    the verification pass saw is captured at that instant, before the
    forgery lands, so it is the reference every folded record is held to.
    """

    def __init__(self, monkeypatch, path, method):
        self.path = Path(path)
        self.fired = 0
        self.blob = None
        self.forged = _forged_line(self.path)
        real = getattr(EventLog, method)
        window = self

        def hooked(log_self, *args, **kwargs):
            out = real(log_self, *args, **kwargs)
            if Path(log_self.path) == window.path and window.fired == 0:
                window.fired = 1
                window.blob = window.path.read_bytes()
                with open(window.path, "ab") as fh:
                    fh.write(window.forged)
            return out

        monkeypatch.setattr(EventLog, method, hooked)

    def lines(self) -> set:
        return set(self.blob.splitlines(keepends=True))

    def assert_opened_and_real(self):
        assert self.fired == 1, (
            "the window never opened: the hooked read did not happen, so "
            "this test proves nothing about the projection")
        assert not EventLog(self.path).verify().ok, (
            "the forgery does not break the chain, so a projection that "
            "checks nothing would pass the assertions above")


def _spy(monkeypatch, cls, name):
    """Record the canonical bytes of every event the reducer receives."""
    seen: list = []
    real = getattr(cls, name)

    def spy(self, ev, *a, **k):
        seen.append(canonical_bytes(ev.to_record()) + b"\n")
        return real(self, ev, *a, **k)

    monkeypatch.setattr(cls, name, spy)
    return seen


def _assert_folded_exactly_the_verified_bytes(window, seen):
    assert seen, "the reducer received nothing, so the check below is vacuous"
    assert window.forged not in seen, (
        "a record whose chain link does not hold reached the reducer: it "
        "verified one read of the log and folded another")
    stray = [s for s in seen if s not in window.lines()]
    assert not stray, (
        f"{len(stray)} record(s) folded that are not bytes of the snapshot "
        "the verification pass read")


# --- every whole-log projection --------------------------------------------

def _policy(log):
    from qta_agent.scheduler import default_policy
    pol = PolicyStore(log).load()
    pol.publish(default_policy(), actor="owner")
    return pol


PROJECTIONS = {
    "authority_store": (lambda log: AuthorityStore(log), AuthorityStore,
                        "_apply"),
    "agents": (lambda log: AgentDirectory(log), AgentDirectory, "apply"),
    "capability": (lambda log: CapabilityLedger(log), CapabilityLedger,
                   "apply"),
    "idempotency": (lambda log: IdempotencyLedger(log), IdempotencyLedger,
                    "apply"),
    "memory": (lambda log: MemoryStore(log), MemoryStore, "apply"),
    "netauth": (lambda log: NetworkAuthority(log), NetworkAuthority,
                "apply"),
    "policy": (lambda log: PolicyStore(log), PolicyStore, "apply"),
    "secrets": (lambda log: SecretStore(log), SecretStore, "apply"),
    "scheduler": (lambda log: Scheduler(
        log, policy=PolicyStore(log).load(), policy_id="scheduler.default"),
        Scheduler, "apply"),
}


@pytest.mark.parametrize("name", sorted(PROJECTIONS))
def test_load_folds_only_the_bytes_it_verified(tmp_path, monkeypatch, name):
    log = _log(tmp_path)
    if name == "scheduler":
        _policy(log)
    make, cls, reducer = PROJECTIONS[name]
    proj = make(log)
    window = _Window(monkeypatch, log.path, "read")
    seen = _spy(monkeypatch, cls, reducer)

    proj.load()

    window.assert_opened_and_real()
    _assert_folded_exactly_the_verified_bytes(window, seen)


def test_the_authority_projection_holds_no_forged_record(tmp_path,
                                                         monkeypatch):
    """The consequence, stated as state rather than as a byte comparison."""
    log = _log(tmp_path)
    store = AuthorityStore(log)
    window = _Window(monkeypatch, log.path, "read")
    store.load()
    window.assert_opened_and_real()
    assert FORGED_ID not in store.all_records()


def test_the_audit_index_holds_only_verified_bytes(tmp_path, monkeypatch):
    log = _log(tmp_path)
    window = _Window(monkeypatch, log.path, "read")
    seen: list = []
    real_init = AuditIndex.__init__

    def spy_init(self, events):
        events = list(events)
        seen.extend(canonical_bytes(e.to_record()) + b"\n" for e in events)
        real_init(self, events)

    monkeypatch.setattr(AuditIndex, "__init__", spy_init)
    AuditIndex.from_log(log)
    window.assert_opened_and_real()
    _assert_folded_exactly_the_verified_bytes(window, seen)


def test_a_windowed_audit_index_holds_only_verified_bytes(tmp_path,
                                                          monkeypatch):
    """The windowed branch read the log a THIRD time; it is its own path."""
    log = _log(tmp_path)
    window = _Window(monkeypatch, log.path, "read")
    idx = AuditIndex.from_log(log, since_seq=0)
    window.assert_opened_and_real()
    assert idx.window is not None
    folded = [canonical_bytes(e.to_record()) + b"\n" for e in idx.events]
    _assert_folded_exactly_the_verified_bytes(window, folded)


@pytest.mark.parametrize("fn", [reconstruct, reconstruct_tasks,
                                reconstruct_subsystems],
                         ids=lambda f: f.__name__)
def test_the_second_reader_replays_the_snapshot_it_reports(tmp_path,
                                                           monkeypatch, fn):
    """The independent reader, which reported one head and replayed past it.

    ``head_seq`` came from the verification pass and ``events_replayed`` from
    a second read, so the reconstruction could describe a position it had
    replayed beyond -- the independent check disagreeing with itself.
    """
    log = _log(tmp_path)
    n = len(log.read())
    window = _Window(monkeypatch, log.path, "read")
    out = fn(log)
    window.assert_opened_and_real()
    assert out.events_replayed == n, (
        f"replayed {out.events_replayed} records from a snapshot of {n}: "
        "the extra one was never verified")
    assert out.head_seq == n - 1
    if fn is reconstruct:
        assert FORGED_ID not in out.records


# --- the anchored path: advance() and every live projection that uses it ----

def test_advance_returns_only_the_tail_it_verified(tmp_path, monkeypatch):
    log = _log(tmp_path)
    anchor = log.anchor_at(1)
    window = _Window(monkeypatch, log.path, "_read_tail")
    events, moved = log.advance(anchor)
    window.assert_opened_and_real()
    folded = [canonical_bytes(e.to_record()) + b"\n" for e in events]
    _assert_folded_exactly_the_verified_bytes(window, folded)
    assert moved.seq == 3, (
        "the new anchor must sit at the last VERIFIED record; an anchor at "
        "the forgery would make every later call trust it as prefix")


def test_advance_reads_the_tail_once(tmp_path, monkeypatch):
    """One pass is the mechanism; a second read is the window."""
    log = _log(tmp_path)
    anchor = log.anchor_at(0)
    calls = {"n": 0}
    real = EventLog._read_tail

    def counting(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(EventLog, "_read_tail", counting)
    log.advance(anchor)
    assert calls["n"] == 1, (
        f"advance() read the tail {calls['n']} times; the records it returns "
        "must be the records it checked")


@pytest.mark.parametrize("name", ["authority_store", "scheduler"])
def test_catching_up_folds_only_the_verified_tail(tmp_path, monkeypatch,
                                                  name):
    log = _log(tmp_path)
    if name == "scheduler":
        _policy(log)
    make, cls, reducer = PROJECTIONS[name]
    proj = make(log).load()
    _append_record(log, 90)             # something new for catch_up to fold
    window = _Window(monkeypatch, log.path, "_read_tail")
    seen = _spy(monkeypatch, cls, reducer)
    try:
        proj.catch_up(force=True)
    except ChainBroken:
        # Refusing is stronger and also correct; the forgery must simply
        # never become state.
        pass
    window.assert_opened_and_real()
    assert window.forged not in seen
    assert all(s in window.lines() for s in seen)


@pytest.mark.parametrize("name", ["authority_store", "scheduler"])
def test_catching_up_without_an_anchor_folds_only_verified_bytes(
        tmp_path, monkeypatch, name):
    """The fallback path: no anchor, so a full verified read.

    It is the path taken after damage forced the anchor away, which is
    precisely when a second, unverified read would do the most harm.
    """
    log = _log(tmp_path)
    if name == "scheduler":
        _policy(log)
    make, cls, reducer = PROJECTIONS[name]
    proj = make(log).load()
    _append_record(log, 90)
    proj._anchor = None                 # force the full verified fallback
    window = _Window(monkeypatch, log.path, "read")
    seen = _spy(monkeypatch, cls, reducer)
    proj.catch_up(force=True)
    window.assert_opened_and_real()
    _assert_folded_exactly_the_verified_bytes(window, seen)


def test_a_checkpointed_load_folds_only_the_tail_it_verified(tmp_path,
                                                             monkeypatch):
    """load_from verified the tail through verify_with, then re-read it."""
    log = _log(tmp_path)
    blobs = EvidenceStore(tmp_path / "blobs")
    cps = CheckpointStore(tmp_path / "cps")
    AuthorityStore(log, evidence=blobs).load().checkpoint(cps)
    _append_record(log, 50)
    window = _Window(monkeypatch, log.path, "_read_tail")
    seen = _spy(monkeypatch, AuthorityStore, "_apply")
    store = AuthorityStore.load_from(log, cps, blobs=blobs,
                                     require_checkpoint=True)
    window.assert_opened_and_real()
    _assert_folded_exactly_the_verified_bytes(window, seen)
    assert FORGED_ID not in store.all_records()
    assert store.loaded_prefix_verified is False


# --- the primitive's own contract -------------------------------------------

def _corrupt_link(path, index):
    """Rewrite record ``index`` with a broken prev_hash and a valid own hash."""
    lines = path.read_bytes().splitlines(keepends=True)
    ev = Event(**__import__("json").loads(lines[index]))
    body = ev.body()
    body["prev_hash"] = "0" * 64
    bad = Event(**body, hash=digest(body))
    lines[index] = canonical_bytes(bad.to_record()) + b"\n"
    path.write_bytes(b"".join(lines))


def test_read_verified_returns_only_the_verified_prefix(tmp_path):
    """A failed report used to come back WITH every record, checked or not.

    Callers that raise on the report are safe either way. A caller that
    inspects the records before -- or instead of -- raising is handed the
    unchecked ones, and "report says no, records say here you go" is not an
    interface anybody should have to use carefully.
    """
    log = _log(tmp_path, n=6)
    _corrupt_link(log.path, 3)
    report, events = log.read_verified()
    assert not report.ok
    assert [e.seq for e in events] == [0, 1, 2], (
        "records at and after the first broken link were returned alongside "
        "a report that refused them")


def test_read_verified_from_returns_only_the_verified_tail(tmp_path):
    log = _log(tmp_path, n=6)
    anchor = log.anchor_at(1)
    _corrupt_link(log.path, 4)
    report, events = log.read_verified_from(anchor)
    assert not report.ok
    assert report.prefix_verified is False
    assert [e.seq for e in events] == [2, 3]


def test_a_clean_log_loses_nothing_to_the_prefix_rule(tmp_path):
    """Control: the truncation must not bite when nothing is wrong."""
    log = _log(tmp_path, n=6)
    report, events = log.read_verified()
    assert report.ok and [e.seq for e in events] == list(range(6))
    report, tail = log.read_verified_from(log.anchor_at(2))
    assert report.ok and [e.seq for e in tail] == [3, 4, 5]
