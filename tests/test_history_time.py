"""An expiry is judged at the latest time the history has reached
(D-2026-168).

A record's wall time is what its writer's clock said. The log reports a
clock that went backwards and does not refuse it -- a legitimate NTP
correction must not make a correct log unreadable -- so the rules that
judged a policy's ``not_after`` at a record's OWN wall time could be beaten
by setting the clock back: an expired policy authorized a policy decision, a
promotion and an inference reservation, and every replay, the second
reader's included, accepted the record.

Each case below sets the clock back past an expiry the history had already
reached: on the write path, and as a record a writer WITHOUT the rule wrote
(the module's ``judged_at`` replaced by the old rule while it writes), which
is what a replay must refuse. Each has a control that differs only in the
clock or the position.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_inference_gateway as G  # noqa: E402

from agent_substrate import inference as I  # noqa: E402
from agent_substrate import policy as P  # noqa: E402
from agent_substrate import store as ST  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.checkpoint import CheckpointStore  # noqa: E402
from agent_substrate.events import EventLog, judged_at  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    Effect, PolicyError, PolicyNotInForce, PolicyRequest, PolicyStore,
    document, rule,
)
from agent_substrate.reconstruct import (  # noqa: E402
    _policy_in_force, reconstruct, reconstruct_subsystems,
)
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402

DIG = "d" * 64
EXPIRES = 1500.0
REQ = PolicyRequest(action="record.promote", subject="carol",
                    role="PROMOTER", resource="r1")


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock(1000.0)
    monkeypatch.setattr(time, "time", c)
    return c


def _doc(pid="promote", **kw):
    return document(policy_id=pid, version=1, rules=(rule(
        rule_id="r1", effect=Effect.ALLOW, actions=("record.promote",),
        subjects=("*",), roles=("*",), resources=("*",)),), **kw)


def _old_rule(monkeypatch, module):
    """A writer built before D-2026-168: an expiry judged at the record's
    own wall time."""
    monkeypatch.setattr(module, "judged_at", lambda latest, wall: wall)


def _unwritten(log, fn, exc, match):
    """``fn`` is refused BEFORE anything is written: a refusal raised by the
    fold of a record already appended would leave a history every later
    load refuses."""
    before = log.head().seq
    with pytest.raises(exc, match=match):
        fn()
    assert log.head().seq == before, "the refused record was written"


def _reach(ps: PolicyStore, clock: Clock, t: float, pid="later") -> None:
    """Something is written at ``t``: the history has reached it."""
    clock.t = t
    ps.publish(_doc(pid), actor="owner")


def test_the_rule():
    assert judged_at(None, 5.0) == 5.0
    assert judged_at(9.0, 5.0) == 9.0           # a clock set back
    assert judged_at(5.0, 9.0) == 9.0           # a clock moving on
    # folding the record in first changes nothing: writer and replay agree
    assert judged_at(judged_at(9.0, 5.0), 5.0) == judged_at(9.0, 5.0)


# ------------------------------------------------------------ policy decisions

def test_a_decision_with_the_clock_set_back_past_an_expiry_is_refused(
        clock, tmp_path):
    ps = PolicyStore(EventLog(tmp_path / "log.jsonl")).load()
    ps.publish(_doc(not_after=EXPIRES), actor="owner")
    clock.t = 1200.0
    assert ps.decide_and_record("promote", REQ, actor="carol").allowed
    _reach(ps, clock, 2000.0)
    clock.t = 1200.0                            # the same reading, later
    _unwritten(ps.log, lambda: ps.decide_and_record("promote", REQ,
                                                    actor="carol"),
               PolicyNotInForce, "expired")
    with pytest.raises(PolicyNotInForce, match="expired"):
        ps.in_force("promote")


def test_a_decision_a_writer_without_the_rule_recorded_is_refused_by_both(
        clock, monkeypatch, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ps = PolicyStore(log).load()
    ps.publish(_doc(not_after=EXPIRES), actor="owner")
    _reach(ps, clock, 2000.0)
    clock.t = 1200.0
    _old_rule(monkeypatch, P)
    assert ps.decide_and_record("promote", REQ, actor="carol").allowed
    monkeypatch.undo()
    rep, _ = log.read_verified()
    assert rep.ok and any("went backwards" in n for n in rep.notes)
    with pytest.raises(PolicyError, match="expired"):
        PolicyStore(log).load()
    sub = reconstruct_subsystems(log)
    assert any("expired at 1500.0" in a for a in sub.anomalies), \
        sub.anomalies


def test_control_a_clock_set_back_before_anything_expired_refuses_nothing(
        clock, tmp_path):
    """The rule judges expiry; it does not refuse a clock that went back."""
    log = EventLog(tmp_path / "log.jsonl")
    ps = PolicyStore(log).load()
    ps.publish(_doc(not_after=5000.0), actor="owner")
    _reach(ps, clock, 2000.0)
    clock.t = 1200.0
    assert ps.decide_and_record("promote", REQ, actor="carol").allowed
    PolicyStore(log).load()
    assert reconstruct_subsystems(log).anomalies == []


# ------------------------------------------------------------------ promotions

def _bound(tmp_path, clock):
    log = EventLog(tmp_path / "log.jsonl")
    ps = PolicyStore(log).load()
    ps.publish(_doc(not_after=EXPIRES), actor="owner")
    s = AuthorityStore(log).load()
    s.bind_policies(actor="owner", publishers=("owner",))
    s.create(record_id="r1", kind="result", proposer="alice",
             policy_id="promote")
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                 role=Role.VERIFIER)
    s.transition(record_id="r1", dst=State.VERIFIED, actor="bob",
                 role=Role.VERIFIER, evidence={"verification_report": DIG})
    return log, ps, s


def _promote(s):
    return s.transition(record_id="r1", dst=State.PROMOTED, actor="carol",
                        role=Role.PROMOTER, policy_id="promote",
                        evidence={"policy_id": "promote"})


def test_a_promotion_with_the_clock_set_back_past_an_expiry_is_refused(
        clock, tmp_path):
    log, ps, s = _bound(tmp_path, clock)
    _reach(ps, clock, 2000.0)
    clock.t = 1200.0
    _unwritten(log, lambda: _promote(s), StoreError, "expired")


def test_control_the_same_promotion_before_the_history_reached_the_expiry(
        clock, tmp_path):
    log, ps, s = _bound(tmp_path, clock)
    clock.t = 1200.0
    assert _promote(s).state is State.PROMOTED
    assert reconstruct(log).unauthorized == []


def test_a_promotion_a_writer_without_the_rule_recorded_is_refused_by_both(
        clock, monkeypatch, tmp_path):
    log, ps, s = _bound(tmp_path, clock)
    _reach(ps, clock, 2000.0)
    clock.t = 1200.0
    _old_rule(monkeypatch, ST)
    _promote(s)
    monkeypatch.undo()
    with pytest.raises(StoreError, match="expired"):
        AuthorityStore(log).load()
    assert any("expired at 1500.0" in u
               for u in reconstruct(log).unauthorized)


def test_a_checkpoint_carries_the_time_the_history_reached(
        clock, monkeypatch, tmp_path):
    """The checkpoint is taken after the history reached the expiry, with
    the clock already set back, so the tail a checkpointed load folds --
    the checkpoint's own record included -- holds only the set-back clock:
    without the time in the snapshot, the forged promotion would be judged
    at 1200 and admitted."""
    log, ps, s = _bound(tmp_path, clock)
    _reach(ps, clock, 2000.0)
    clock.t = 1200.0
    blobs = EvidenceStore(tmp_path / "blobs")
    cps = CheckpointStore(tmp_path / "cp")
    s.checkpoint(cps, blobs=blobs)
    assert s.snapshot()["latest_wall"] == 2000.0
    _old_rule(monkeypatch, ST)
    _promote(s)
    monkeypatch.undo()
    with pytest.raises(StoreError, match="expired"):
        AuthorityStore.load_from(log, cps, blobs=blobs)


def test_a_snapshot_with_a_malformed_time_is_refused(clock, tmp_path):
    log, ps, s = _bound(tmp_path, clock)
    snap = s.snapshot()
    for bad in ("2000", True, [2000.0]):
        with pytest.raises(StoreError, match="latest_wall"):
            AuthorityStore(log)._restore({**snap, "latest_wall": bad})


# ------------------------------------------------------- inference reservations

@pytest.fixture
def world(monkeypatch, tmp_path):
    """The gateway world, every writer on one clock that can be set back."""
    c = Clock(1_000_000.0)
    monkeypatch.setattr(time, "time", c)
    w = G.World(tmp_path)
    w.clock = c
    w.gw = w.gateway()
    w.policies.publish(document(
        policy_id=G.POLICY, version=2, rules=G._policy(1).rules,
        not_after=c.t + 100), actor="owner")
    w.context = w.build_context(policy_identity=f"{G.POLICY}@2")
    return w


def _infer(w, key):
    return w.gw.infer(w.request(key=key), I.render_prompt(w.context))


def test_a_reservation_with_the_clock_set_back_past_an_expiry_is_refused(
        world):
    w = world
    w.clock.t += 50
    assert _infer(w, "k1").ok                         # control: in force
    _reach(w.policies, w.clock, w.clock.t + 150)
    w.clock.t -= 150                                  # the same reading
    before = w.log.head().seq
    with pytest.raises(I.InferenceRefused, match="CONTEXT_STALE") as exc:
        _infer(w, "k2")
    assert "expired" in exc.value.reason
    # a refusal is recorded (granted: False), and nothing is dispatched
    assert [e.action for e in w.records(I.ACT_RESERVE)[-1:]] == \
        [I.ACT_RESERVE] and w.log.head().seq == before + 1
    assert w.records(I.ACT_RESERVE)[-1].payload["granted"] is False


def test_a_reservation_a_writer_without_the_rule_recorded_is_refused_by_both(
        world, monkeypatch):
    w = world
    _reach(w.policies, w.clock, w.clock.t + 200)
    w.clock.t -= 150
    with monkeypatch.context() as m:
        _old_rule(m, I)
        _old_rule(m, P)
        assert _infer(w, "k1").ok
    with pytest.raises(I.InferenceError, match="expired"):
        I.InferenceGateway(w.log, transcripts=w.transcripts,
                           policies=PolicyStore(w.log).load(),
                           provider=w.provider).load()
    assert any("budget refuses" in a
               for a in reconstruct_subsystems(w.log).anomalies)


# ------------------------------------------------------------ decision proofs

def test_a_decision_proof_judges_the_policy_at_the_history_s_time(
        clock, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ps = PolicyStore(log).load()
    ps.publish(_doc(not_after=EXPIRES), actor="owner")
    _reach(ps, clock, 1200.0, "before")            # seq 1, at 1200
    _reach(ps, clock, 2000.0)                      # seq 2, at 2000
    _reach(ps, clock, 1200.0, "after")             # seq 3, at 1200 again
    _, events = log.read_verified()
    assert _policy_in_force(events, "promote@1", 1) is True    # control
    assert _policy_in_force(events, "promote@1", 3) is False
