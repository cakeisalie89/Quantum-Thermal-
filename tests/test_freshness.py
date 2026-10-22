"""Freshness: an authentic but outdated observation is not a current fact,
and nothing about history is rewritten to say so (Assurance F).

The acceptance, stated as tests: a measurement recorded from a VERIFIED
source stays a valid record of what was observed, byte for byte; whether
it still holds is judged at a stated time under the policy the
objective's owner recorded for its source; once that runs out it decides
nothing new, and the ledger appends EXPIRED and carries the loss to what
rested on it, while a re-observation renews a verdict instead.

Then the directive's attacks: clock changes, missing re-observations,
delayed and future-dated data, source replacement, contradictory
measurements, and records a writer without the rules appended -- each
refused by the ledger's replay AND noted by the second reader, each with a
control that differs only in what is under test.

The measurements here are FIXTURES: bundles written by the test, of a
source named ``probe-1``. Nothing here was measured.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_research_ledger as RL  # noqa: E402

from agent_substrate import freshness as F  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402
from agent_substrate.reconstruct import (  # noqa: E402
    _rs_instant, reconstruct_subsystems,
)

T0 = 1_800_000_000.0                       # 2027-01-15T08:00:00Z
HOUR = 3600.0
PROBE = {"model_id": "probe-1", "quantity": "T_probe", "op": "<",
         "value": 400.0, "unit": "K"}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class World(RL.World):
    """The research ledger's world, every writer on one clock, with a
    measured source beside the simulated one."""

    def __init__(self, tmp, clock: Clock):
        self.clock = clock
        super().__init__(tmp)
        self.led = R.ResearchLedger(self.log, evidence=self.evidence,
                                    clock=clock).load()

    def reload(self):
        return R.ResearchLedger(self.log, evidence=self.evidence,
                                clock=self.clock).load()

    def policy(self, kind, params=None, *, model="probe-1", actor="owner",
               pid=None):
        self.led.record_freshness(
            policy_id=pid or f"fresh-{model}", objective_id="o1",
            model_id=model, kind=kind, params=params or {},
            rationale=f"how fast {model}'s reading goes out of date",
            actor=actor)

    def measured(self, rid, value, observed_at, *, model="probe-1",
                 state="VERIFIED"):
        """A VERIFIED record whose bundle is a reading OF THE WORLD that
        says when it was taken. A fixture: nothing was measured."""
        body = {"model_id": model,
                "outputs": [{"name": "T_probe", "status": "OK",
                             "quantity": {"value": value, "unit": "K",
                                          "resolution": 0.5}}]}
        if observed_at is not None:
            body["observed_at"] = iso(observed_at) \
                if isinstance(observed_at, float) else observed_at
        bundle = json.dumps(body).encode()
        dg = self.evidence.put(bundle)
        self.bundles[rid] = bundle
        # a real authority record, reviewed by someone other than its
        # proposer, so the second reader's account of it is a real one
        report = self.evidence.put(f"calibration check of {rid}".encode())
        store = AuthorityStore(self.log, evidence=self.evidence).load()
        store.create(record_id=rid, kind="measurement", proposer="ingest",
                     evidence={"result_bundle": dg})
        store.transition(record_id=rid, dst=State.UNDER_REVIEW,
                         actor="reviewer", role=Role.VERIFIER)
        if state == "VERIFIED":
            store.transition(record_id=rid, dst=State.VERIFIED,
                             actor="reviewer", role=Role.VERIFIER,
                             evidence={"verification_report": report})
        return dg

    def withdraw(self, rid, dst=State.STALE):
        AuthorityStore(self.log, evidence=self.evidence).load().transition(
            record_id=rid, dst=dst, actor="system", role=Role.SYSTEM,
            evidence={"invalidated_by": self.evidence.put(
                f"{rid}: replaced by a recalibrated instrument".encode())},
            stale_reason="replaced by a recalibrated instrument")

    def currency(self):
        return self.led.currency("o1")

    def standing(self, oid):
        return self.currency()["observations"][oid]["standing"]

    def second_reader(self):
        return reconstruct_subsystems(self.log).anomalies


@pytest.fixture
def clock(monkeypatch):
    c = Clock(T0)
    monkeypatch.setattr(time, "time", c)
    return c


@pytest.fixture
def w(tmp_path, clock):
    return World(tmp_path, clock)


def _decided(w, kind="INTERVAL", params=None, value=350.0):
    """An owner's policy, a fresh reading, and a hypothesis it supports."""
    if kind is not None:
        w.policy(kind, params if params is not None else {"seconds": HOUR})
    w.measured("m1", value, T0)
    w.clock.t = T0 + 60
    w.observe("obs-1", "m1")
    w.hypothesis(prediction=dict(PROBE))
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"


def _events(w):
    _, events = w.log.read_verified()
    return events


# ------------------------------------------------------------ the acceptance

def test_an_authentic_but_outdated_observation_is_not_a_current_fact(w):
    _decided(w)
    w.led.set_status("o1", "CONCLUDED", reason="h1 holds", basis=["h1"])
    before = {e.seq: (e.hash, e.payload) for e in _events(w)}
    cur = w.currency()
    assert cur["observations"]["obs-1"]["standing"] == "FRESH"
    assert cur["hypotheses"]["h1"]["evidence"] == "FRESH"

    w.clock.t = T0 + 2 * HOUR
    cur = w.currency()
    # derived: unknown now, and the record still says what was observed
    assert cur["observations"]["obs-1"]["standing"] == "UNKNOWN"
    assert cur["observations"]["obs-1"]["status"] == "CURRENT"
    assert cur["hypotheses"]["h1"]["evidence"] == "UNKNOWN"
    # appended, never rewritten
    moved = w.led.propagate()
    assert moved == ["obs-1", "h1", "o1"]
    led = w.reload()
    assert [led.get(x).status for x in ("obs-1", "h1", "o1")] == \
        ["EXPIRED", "STALE", "STALE"]
    after = {e.seq: (e.hash, e.payload) for e in _events(w)}
    assert all(after[s] == before[s] for s in before), "history rewritten"
    assert led.get("obs-1").body["values"]["T_probe"]["value"] == 350.0
    assert led.get("h1").history[0][1] == "OPEN"           # what was held
    assert w.second_reader() == []


def test_control_within_its_interval_nothing_lapses(w):
    _decided(w)
    w.clock.t = T0 + HOUR - 1
    assert w.standing("obs-1") == "FRESH"
    assert w.led.propagate() == []


def test_the_expiry_says_how_it_was_determined_and_by_whom(w):
    _decided(w)
    w.clock.t = T0 + 2 * HOUR
    w.led.propagate()
    pol = w.currency()["observations"]["obs-1"]["policy"]
    assert pol["recorded_by"] == "owner" and pol["kind"] == "INTERVAL"
    exp = next(e for e in _events(w) if e.action == R.ACT_STATUS
               and e.payload["status"] == "EXPIRED")
    reason = exp.payload["reason"]
    assert reason.startswith("UNKNOWN: policy fresh-probe-1 (INTERVAL)")
    assert f"recorded by owner at seq {pol['recorded_at_seq']}" in reason


# ------------------------------------------------------------ no universal TTL

def test_a_source_with_no_policy_is_historical_and_never_expires(w):
    _decided(w, kind=None)
    w.clock.t = T0 + 10 * 365 * 24 * HOUR
    cur = w.currency()
    assert cur["observations"]["obs-1"]["standing"] == "HISTORICAL"
    assert cur["observations"]["obs-1"]["policy"] is None
    # usable as a record of what was observed -- and labelled as such
    assert cur["hypotheses"]["h1"]["evidence"] == "HISTORICAL"
    assert w.led.propagate() == []


def test_a_simulation_result_is_historical(w):
    w.hypothesis()
    w.verified()                                # a model's output, no time
    w.observe()
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"
    w.clock.t = T0 + 1000 * HOUR
    assert w.standing("obs-1") == "HISTORICAL"
    assert "observed_at" not in w.led.get("obs-1").body


def test_a_timed_policy_on_a_source_that_states_no_time_is_unknown(w):
    w.policy("INTERVAL", {"seconds": HOUR})
    w.measured("m1", 350.0, None)
    w.observe("obs-1", "m1")
    w.hypothesis(prediction=dict(PROBE))
    assert w.standing("obs-1") == "UNKNOWN"
    # a verdict needs usable evidence: none is, so none is recorded
    assert w.led.evaluate_hypothesis("h1") == "INCONCLUSIVE"
    assert w.led.get("h1").status == "OPEN"


# ------------------------------------------------- who determines freshness

def test_freshness_is_the_owner_s_determination(w):
    for actor in ("research-controller", "agent-a"):
        RL.refused(w, lambda: w.policy("INTERVAL", {"seconds": 1.0},
                                       actor=actor), "owner")
    w.policy("INTERVAL", {"seconds": HOUR})
    RL.refused(w, lambda: w.policy("HISTORICAL", pid="looser"),
               "not rewritten")


@pytest.mark.parametrize("kind, params, why", [
    ("TTL", {}, "is not one of"),
    ("INTERVAL", {}, "takes exactly"),
    ("INTERVAL", {"seconds": 60, "grace": 5}, "takes exactly"),
    ("INTERVAL", {"seconds": 0}, "positive"),
    ("PERIODIC", {"seconds": float("inf")}, "positive"),
    ("PERIODIC", {"seconds": True}, "positive"),
    ("UNTIL_EVENT", {"action": "record.create", "target": " "},
     "must name one"),
    ("HISTORICAL", {"seconds": 60}, "takes exactly"),
])
def test_a_malformed_policy_is_refused(w, kind, params, why):
    RL.refused(w, lambda: w.policy(kind, params), why)


def test_a_policy_states_its_reason(w):
    RL.refused(w, lambda: w.led.record_freshness(
        policy_id="p", objective_id="o1", model_id="probe-1",
        kind="HISTORICAL", params={}, rationale="  ", actor="owner"),
        "states why")


# --------------------------------------------------------------- clock changes

def test_a_clock_set_back_does_not_make_an_expired_reading_fresh(w):
    _decided(w)
    w.measured("m2", 352.0, T0 + 2 * HOUR)       # the history moves on
    w.clock.t = T0 + 2 * HOUR
    w.observe("obs-2", "m2")
    w.hypothesis("h2", label="prop-2", prediction=dict(PROBE))
    w.clock.t = T0 + 30 * 60                     # set back, inside obs-1's
    cur = w.currency()
    assert cur["observations"]["obs-1"]["standing"] == "UNKNOWN"
    assert cur["at"] == T0 + 2 * HOUR
    RL.refused(w, lambda: w.led.set_status(
        "h2", "SUPPORTED", reason="the clock says it is fresh",
        basis=["obs-1"]), "UNKNOWN")


def test_a_verdict_a_writer_without_the_rule_recorded_is_refused_by_both(
        w, monkeypatch):
    _decided(w)
    w.measured("m2", 352.0, T0 + 2 * HOUR)
    w.clock.t = T0 + 2 * HOUR
    w.observe("obs-2", "m2")
    w.hypothesis("h2", label="prop-2", prediction=dict(PROBE))
    w.clock.t = T0 + 30 * 60
    with monkeypatch.context() as m:
        m.setattr(R, "judged_at", lambda latest, wall: wall)
        w.led.set_status("h2", "SUPPORTED", reason="old rule",
                         basis=["obs-1"])
    with pytest.raises(R.ResearchError, match="UNKNOWN"):
        w.reload()
    assert any("freshness had run out" in a for a in w.second_reader())


# ---------------------------------------------- missing external observations

def test_a_periodic_reading_never_repeated_goes_unknown(w):
    _decided(w, kind="PERIODIC", params={"seconds": 600.0})
    w.clock.t = T0 + 700
    assert w.standing("obs-1") == "UNKNOWN"
    assert w.led.propagate() == ["obs-1", "h1"]
    assert w.reload().get("h1").status == "STALE"


def test_a_re_observation_renews_the_verdict_instead(w):
    _decided(w, kind="PERIODIC", params={"seconds": 600.0})
    w.measured("m2", 351.0, T0 + 500)
    w.clock.t = T0 + 520
    w.observe("obs-2", "m2")
    assert w.standing("obs-1") == "SUPERSEDED"
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"
    h = w.reload().get("h1")
    assert h.status == "SUPPORTED" and h.basis == ("obs-2",)
    assert h.history[-1][1] == "SUPPORTED"     # decided again, appended
    assert w.led.propagate() == ["obs-1"]      # the old one EXPIRED; h1 not
    assert w.reload().get("h1").status == "SUPPORTED"
    assert w.currency()["hypotheses"]["h1"]["evidence"] == "FRESH"
    assert w.second_reader() == []


def test_propagation_renews_where_it_would_otherwise_end(w):
    """INTERVAL: two readings fresh at once, the verdict decided on the
    first; when it lapses, the second still decides the same way."""
    _decided(w)
    w.measured("m2", 351.0, T0 + HOUR / 2)
    w.clock.t = T0 + HOUR / 2
    w.observe("obs-2", "m2")
    assert w.led.get("h1").basis == ("obs-1",)
    w.clock.t = T0 + HOUR + 60
    assert w.led.propagate() == ["obs-1"]
    h = w.reload().get("h1")
    assert (h.status, h.basis) == ("SUPPORTED", ("obs-2",))


def test_a_verdict_is_as_current_as_its_weakest_reading(w):
    w.policy("INTERVAL", {"seconds": HOUR})
    w.measured("m1", 350.0, T0)
    w.measured("m2", 351.0, T0 + HOUR / 2)
    w.clock.t = T0 + HOUR / 2
    w.observe("obs-1", "m1")
    w.observe("obs-2", "m2")
    w.hypothesis(prediction=dict(PROBE))
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"
    assert set(w.led.get("h1").basis) == {"obs-1", "obs-2"}
    assert w.currency()["hypotheses"]["h1"]["evidence"] == "FRESH"
    w.clock.t = T0 + HOUR + 60
    cur = w.currency()
    assert [cur["observations"][o]["standing"]
            for o in ("obs-1", "obs-2")] == ["UNKNOWN", "FRESH"]
    assert cur["hypotheses"]["h1"]["evidence"] == "UNKNOWN"


def test_a_verdict_is_renewed_only_when_its_evidence_lapsed(w):
    _decided(w)
    w.measured("m2", 351.0, T0 + 120)
    w.clock.t = T0 + 120
    w.observe("obs-2", "m2")
    RL.refused(w, lambda: w.led.set_status(
        "h1", "SUPPORTED", reason="again", basis=["obs-1", "obs-2"]),
        "only once evidence it rested on has lapsed")
    w.clock.t = T0 + HOUR + 10
    RL.refused(w, lambda: w.led.set_status(
        "h1", "SUPPORTED", reason="again", basis=["obs-1"]), "UNKNOWN")


# ------------------------------------------------------------- delayed data

def test_a_delayed_reading_is_recorded_and_decides_nothing(w):
    w.policy("INTERVAL", {"seconds": HOUR})
    w.measured("m1", 350.0, T0 - 2 * HOUR)       # arrives two hours late
    w.observe("obs-1", "m1")
    assert w.led.get("obs-1").body["observed_at"] == iso(T0 - 2 * HOUR)
    assert w.standing("obs-1") == "UNKNOWN"
    w.hypothesis(prediction=dict(PROBE))
    assert w.led.evaluate_hypothesis("h1") == "INCONCLUSIVE"
    RL.refused(w, lambda: w.led.set_status(
        "h1", "SUPPORTED", reason="it is a real reading", basis=["obs-1"]),
        "UNKNOWN")


def test_a_late_arrival_does_not_supersede_a_later_reading(w):
    _decided(w, kind="PERIODIC", params={"seconds": 600.0})
    w.measured("m0", 450.0, T0 - 300)           # older, arriving now
    w.clock.t = T0 + 120
    w.observe("obs-0", "m0")
    assert w.standing("obs-1") == "FRESH"
    assert w.standing("obs-0") == "SUPERSEDED"
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"


def test_a_reading_from_the_future_is_refused(w):
    w.policy("INTERVAL", {"seconds": HOUR})
    w.measured("m1", 350.0, T0 + 300)            # the source's clock is ahead
    RL.refused(w, lambda: w.observe("obs-1", "m1"), "after the")


@pytest.mark.parametrize("when", [
    "2027-01-15T08:00:00",               # naive
    "2027-01-15 08:00:00Z",              # not the one form
    "20270115T080000Z",                  # basic format
    "2027-W02-5T08:00:00Z",              # a week date
    "yesterday",
])
def test_a_time_that_cannot_be_ordered_is_refused(w, when):
    w.measured("m1", 350.0, when)
    RL.refused(w, lambda: w.observe("obs-1", "m1"), "observed_at")


# ----------------------------------------------------------- source replacement

def test_a_replaced_source_is_withdrawn_not_expired(w):
    _decided(w, kind="SOURCE_WITHDRAWAL", params={})
    assert w.standing("obs-1") == "FRESH"
    w.withdraw("m1")
    w.measured("m2", 351.0, T0 + 100)
    w.clock.t = T0 + 120
    assert w.standing("obs-1") == "WITHDRAWN"
    RL.refused(w, lambda: w.led.set_status(
        "obs-1", "EXPIRED", reason="lapsed"), "WITHDRAWN")
    assert w.led.propagate() == ["obs-1", "h1"]
    assert w.reload().get("obs-1").status == "STALE"
    w.observe("obs-2", "m2")
    assert w.standing("obs-2") == "FRESH"


def test_valid_until_a_named_event_recorded_after_it(w):
    """Valid until the probe's calibration record next moves."""
    store = AuthorityStore(w.log, evidence=w.evidence).load()
    store.create(record_id="cal-1", kind="calibration", proposer="lab")
    store.transition(record_id="cal-1", dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    w.policy("UNTIL_EVENT", {"action": "record.transition",
                             "target": "cal-1"})
    w.measured("m1", 350.0, T0)
    w.observe("obs-1", "m1")
    # the same event BEFORE the reading does not end it
    assert w.standing("obs-1") == "FRESH"
    store.create(record_id="cal-2", kind="calibration", proposer="lab")
    assert w.standing("obs-1") == "FRESH"       # another record moved
    store.transition(record_id="cal-1", dst=State.VERIFIED,
                     actor="reviewer", role=Role.VERIFIER,
                     evidence={"verification_report":
                               w.evidence.put(b"recalibrated")})
    assert w.standing("obs-1") == "UNKNOWN"
    assert w.led.propagate() == ["obs-1"]


def test_a_record_is_judged_against_the_history_before_it(w):
    """The writer judges a verdict against the history it appends onto, so
    the verdict is not yet among the events it sees; replay must see the
    same. A policy valid until the hypothesis is decided makes the
    difference load-bearing: the verdict is written, loads, and only then
    ends the reading."""
    w.policy("UNTIL_EVENT", {"action": R.ACT_STATUS, "target": "h1"})
    w.measured("m1", 350.0, T0)
    w.observe("obs-1", "m1")
    w.hypothesis(prediction=dict(PROBE))
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"
    assert w.reload().get("h1").status == "SUPPORTED"
    assert w.standing("obs-1") == "UNKNOWN"
    assert w.second_reader() == []
    # and the decision's proof judges it as the decision did
    from agent_substrate.reconstruct import (
        derive_decision_proof, verify_decision_proof,
    )
    proof = derive_decision_proof(w.log, _verdict_seq(w),
                                  evidence=w.evidence)
    assert [b["freshness"] for b in proof["basis"]] == ["FRESH"]
    assert verify_decision_proof(w.log, proof, evidence=w.evidence) == []


# --------------------------------------------------- contradictory readings

def test_fresh_readings_that_disagree_decide_nothing(w):
    w.policy("INTERVAL", {"seconds": HOUR})
    w.measured("m1", 350.0, T0)
    w.measured("m2", 450.0, T0)
    w.observe("obs-1", "m1")
    w.observe("obs-2", "m2")
    w.hypothesis(prediction=dict(PROBE))
    assert w.led.evaluate_hypothesis("h1") == "INCONCLUSIVE"


def test_a_contradicting_re_observation_ends_the_verdict_it_cannot_flip(w):
    _decided(w, kind="PERIODIC", params={"seconds": 600.0})
    w.measured("m2", 450.0, T0 + 300)
    w.clock.t = T0 + 320
    w.observe("obs-2", "m2")
    with pytest.raises(R.ResearchError, match="not a move"):
        w.led.evaluate_hypothesis("h1")          # SUPPORTED -> REFUTED
    assert w.led.propagate() == ["obs-1", "h1"]
    assert w.reload().get("h1").status == "STALE"
    assert w.second_reader() == []


# ------------------------------------- records appended around the ledger

def _append(w, target, status, basis=(), reason="forged"):
    w.log.append(actor=R.CONTROLLER_ID, action=R.ACT_STATUS, target=target,
                 payload={"target_id": target, "status": status,
                          "reason": reason, "basis": list(basis)})


@pytest.mark.parametrize("forge, why, noted", [
    (lambda w: _append(w, "obs-1", "EXPIRED"), "not lapsed",
     "declared EXPIRED while its freshness held"),
    (lambda w: _append(w, "h1", "SUPPORTED", ["obs-1"]),
     "records nothing", "again with nothing in its basis lapsed"),
])
def test_a_status_the_ledger_would_not_write_is_refused_by_both(
        w, forge, why, noted):
    _decided(w)
    forge(w)
    with pytest.raises(R.ResearchError, match=why):
        w.reload()
    assert any(noted in a for a in w.second_reader())


def test_a_conclusion_on_lapsed_evidence_is_refused_by_both(w):
    _decided(w)
    RL.refused(w, lambda: (setattr(w.clock, "t", T0 + 2 * HOUR),
                           w.led.set_status("o1", "CONCLUDED", reason="x",
                                            basis=["h1"])), "not usable")
    w.clock.t = T0 + 2 * HOUR
    _append(w, "o1", "CONCLUDED", ["h1"])
    with pytest.raises(R.ResearchError, match="not usable"):
        w.reload()
    assert any("freshness had run out" in a for a in w.second_reader())


def test_a_time_its_source_does_not_give_is_refused(w):
    """Appended around the ledger: the reading's record claims a later time
    than its bundle holds, to keep an old reading fresh. The bundle is
    re-read on replay."""
    w.policy("INTERVAL", {"seconds": HOUR})
    w.measured("m1", 350.0, T0 - 2 * HOUR)
    body = w.led.observe(observation_id="obs-1", objective_id="o1",
                         record_id="m1", bundle_bytes=w.bundles["m1"])
    assert body["observed_at"] == iso(T0 - 2 * HOUR)        # control
    w.reload()
    w.log.append(actor=R.CONTROLLER_ID, action=R.ACT_OBSERVATION,
                 target="obs-2", payload={"observation": {
                     **body, "observation_id": "obs-2",
                     "observed_at": iso(T0)}})
    with pytest.raises(R.ResearchError, match="a time its bundle"):
        w.reload()


def test_a_policy_appended_by_another_is_refused_by_both(w):
    w.log.append(actor="agent-a", action=R.ACT_FRESHNESS, target="p",
                 payload={"freshness": {
                     "policy_id": "p", "objective_id": "o1",
                     "model_id": "probe-1", "kind": "HISTORICAL",
                     "params": {}, "rationale": "mine now"}})
    with pytest.raises(R.ResearchError, match="owner"):
        w.reload()
    assert any("not its objective" in a for a in w.second_reader())


# ------------------------------------------------------ the decision's proof

def _verdict_seq(w, hid="h1"):
    return max(e.seq for e in _events(w) if e.action == R.ACT_STATUS
               and e.target == hid)


def test_a_verdict_s_proof_states_the_freshness_it_was_decided_on(w):
    from agent_substrate.reconstruct import (
        derive_decision_proof, verify_decision_proof,
    )
    _decided(w)
    proof = derive_decision_proof(w.log, _verdict_seq(w),
                                  evidence=w.evidence)
    assert [b["freshness"] for b in proof["basis"]] == ["FRESH"]
    assert verify_decision_proof(w.log, proof, evidence=w.evidence) == []
    # later, the reading lapses: the proof is about the decision, not now
    w.clock.t = T0 + 2 * HOUR
    assert verify_decision_proof(w.log, proof, evidence=w.evidence) == []


def test_a_verdict_on_lapsed_evidence_fails_its_proof_however_stated(
        w, monkeypatch):
    from agent_substrate.reconstruct import (
        derive_decision_proof, verify_decision_proof,
    )
    _decided(w)
    w.measured("m2", 352.0, T0 + 2 * HOUR)       # the history moves on
    w.clock.t = T0 + 2 * HOUR
    w.observe("obs-2", "m2")
    w.hypothesis("h2", label="prop-2", prediction=dict(PROBE))
    w.clock.t = T0 + 30 * 60                     # and the clock is set back
    with monkeypatch.context() as m:
        m.setattr(R, "judged_at", lambda latest, wall: wall)
        w.led.set_status("h2", "SUPPORTED", reason="old rule",
                         basis=["obs-1"])
    proof = derive_decision_proof(w.log, _verdict_seq(w, "h2"),
                                  evidence=w.evidence)
    assert [b["freshness"] for b in proof["basis"]] == ["UNKNOWN"]
    found = verify_decision_proof(w.log, proof, evidence=w.evidence)
    assert any("freshness had run out" in f for f in found), found
    forged = json.loads(json.dumps(proof))
    forged["basis"][0]["freshness"] = "FRESH"
    found = verify_decision_proof(w.log, forged, evidence=w.evidence)
    assert any("basis[0].freshness" in f for f in found), found


# --------------------------------------------------------------- the pieces

@pytest.mark.parametrize("policy, kw, want", [
    (None, {}, "HISTORICAL"),
    ({"kind": "SOURCE_WITHDRAWAL"}, {}, "FRESH"),
    ({"kind": "UNTIL_EVENT", "params": {"action": "a", "target": "t"}},
     {"event_seq": 9}, "UNKNOWN"),
    ({"kind": "UNTIL_EVENT", "params": {"action": "a", "target": "t"}},
     {}, "FRESH"),
    ({"kind": "INTERVAL", "params": {"seconds": 10}}, {"observed_at": 0.0},
     "FRESH"),
    ({"kind": "INTERVAL", "params": {"seconds": 10}}, {"observed_at": -11.0},
     "UNKNOWN"),
    ({"kind": "INTERVAL", "params": {"seconds": 10}}, {}, "UNKNOWN"),
    ({"kind": "PERIODIC", "params": {"seconds": 10}},
     {"observed_at": 0.0, "replaced_by": "o2"}, "SUPERSEDED"),
    ({"kind": "INTERVAL", "params": {"seconds": 10}},
     {"observed_at": 0.0, "source_stands": False}, "WITHDRAWN"),
])
def test_standing(policy, kw, want):
    if policy is not None:
        policy = {"policy_id": "p", **policy}
    args = {"at": 0.0, "observed_at": None, "source_stands": True, **kw}
    assert F.standing(policy, **args)[0].value == want


@pytest.mark.parametrize("text, valid", [
    ("2024-02-29T00:00:00Z", True), ("2000-02-29T00:00:00Z", True),
    ("2023-02-29T00:00:00Z", False), ("1900-02-29T00:00:00Z", False),
    ("2027-02-30T00:00:00Z", False), ("2027-04-31T00:00:00Z", False),
    ("2027-13-01T00:00:00Z", False), ("2027-00-10T00:00:00Z", False),
    ("2027-01-15T23:59:59.999999+23:59", True),
    ("2027-01-15T08:00:00+24:00", False), ("2027-01-15T08:00:60Z", False),
])
def test_both_readers_agree_at_the_calendar_s_edges(text, valid):
    try:
        first = F.instant(text)
    except F.FreshnessError:
        first = None
    second = _rs_instant(text)
    assert (first is not None) is valid and (second is not None) is valid
    if valid:
        assert abs(first - second) < 1e-6


@settings(max_examples=400, deadline=None, derandomize=True, database=None)
@given(st.one_of(
    st.datetimes(timezones=st.timezones()).map(lambda d: d.isoformat()),
    st.datetimes(timezones=st.just(timezone.utc)).map(
        lambda d: d.isoformat().replace("+00:00", "Z")),
    st.text(alphabet="0123456789-:T.Z+ ", min_size=18, max_size=32)))
def test_both_readers_read_the_same_instants(text):
    """The second reader parses times without the datetime module the first
    uses; they must accept the same strings and read the same instant."""
    try:
        first = F.instant(text)
    except F.FreshnessError:
        first = None
    second = _rs_instant(text)
    assert (first is None) == (second is None), text
    if first is not None:
        assert abs(first - second) < 1e-6, text
