"""A resolution is a bound, and none is below zero (AUD-056).

A quantity refuses a resolution below zero where it is made, and so does
the ranking. The readers of a bundle did not: the research ledger observed
a stated resolution of -15 K as it stood, and the one evaluation held the
value to an interval whose lower end is above its upper, so that a value
supported what it contradicts. The claim check and the second reader's
verdict used the same interval and agreed. Each case here holds the rule
from one side, with a resolution of 0.5 K as the control.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import claim_check as C  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.reconstruct import (  # noqa: E402
    _rs_verdict, reconstruct_subsystems)

MODEL, Q = "thermal.slab_transient", "T_at_half"
#: predictions a value of 300 K does not satisfy, and the interval a
#: resolution of -5 K makes of it, from 305 K down to 295 K, would
WRONG_WAY = [(">", 302.0), (">=", 302.0), ("<", 298.0), ("<=", 298.0)]


def _pred(op, value):
    return {"model_id": MODEL, "quantity": Q, "op": op, "value": value,
            "unit": "K"}


def _obs(resolution, value=300.0):
    return {"observation_id": "obs-1", "model_id": MODEL,
            "values": {Q: {"value": value, "unit": "K",
                           "resolution": resolution}}}


def _bundle(resolution, value=300.0):
    return {"model_id": MODEL, "outputs": [{"name": Q, "status": "OK",
            "quantity": {"value": value, "unit": "K",
                         "resolution": resolution}}]}


# ------------------------------------------------------------ the rule
@pytest.mark.parametrize("got, holds", [
    ({"value": 300.0, "resolution": 0.5}, True),
    ({"value": 300.0, "resolution": 0}, True),
    ({"value": 300, "resolution": 0.0}, True),
    ({"value": 300.0, "resolution": -5.0}, False),
    ({"value": 300.0, "resolution": -1e-300}, False),
    ({"value": 300.0, "resolution": True}, False),
    ({"value": 300.0, "resolution": "0.5"}, False),
    ({"value": 300.0, "resolution": 10 ** 400}, False),
    ({"value": 300.0, "resolution": float("inf")}, False),
    ({"value": 300.0, "resolution": float("nan")}, False),
    ({"value": 300.0}, False),
    ({"value": float("nan"), "resolution": 0.5}, False),
    ({"value": 10 ** 400, "resolution": 0.5}, False),
    ({"value": True, "resolution": 0.5}, False),
    ("300 K", False),
])
def test_what_holds_an_interval(got, holds):
    assert R.holds_an_interval(got) is holds


def test_the_reader_of_outputs_leaves_out_a_resolution_below_zero():
    assert R.values_of(_bundle(-5.0)) == {}
    assert R.values_of(_bundle(0.5)) == {
        Q: {"value": 300.0, "unit": "K", "resolution": 0.5}}
    assert R.values_of(_bundle(0)) == {
        Q: {"value": 300.0, "unit": "K", "resolution": 0.0}}


# ------------------------------------------------- the three verdicts
@pytest.mark.parametrize("op, t", WRONG_WAY)
def test_the_evaluation_decides_nothing_on_a_resolution_below_zero(op, t):
    status, why = R.evaluate(_pred(op, t), [_obs(-5.0)])
    assert status == "INCONCLUSIVE"
    assert "bound no interval" in why
    assert R.evaluate(_pred(op, t), [_obs(0.5)])[0] == "REFUTED"
    assert R.evaluate(_pred(op, t), [_obs(0.0)])[0] == "REFUTED"


@pytest.mark.parametrize("op, t", WRONG_WAY)
def test_the_second_reader_decides_nothing_on_a_resolution_below_zero(op, t):
    assert _rs_verdict(_pred(op, t), [_obs(-5.0)]) == "INCONCLUSIVE"
    assert _rs_verdict(_pred(op, t), [_obs(0.5)]) == "REFUTED"


@pytest.mark.parametrize("op, t", WRONG_WAY + [("=", 300.0)])
def test_the_claim_check_judges_nothing_on_a_resolution_below_zero(op, t):
    claim = C.Structured(quantity=Q, model_id=MODEL, op=op, value=t,
                         unit="K")
    verdict, why, used = C.judge(claim, [_obs(-5.0)])
    assert verdict is C.Verdict.INSUFFICIENT_EVIDENCE
    assert "bounds no interval" in why
    assert used == ("obs-1",)
    assert C.judge(claim, [_obs(0.5)])[0] is (
        C.Verdict.SUPPORTED if op == "=" else C.Verdict.CONTRADICTED)


# ------------------------------------------------------------ the ledger
PRED = {"model_id": MODEL, "quantity": Q, "op": "<", "value": 400.0,
        "unit": "K"}


class World:
    """A ledger, its objective, a received HYPOTHESIS that T_at_half stays
    below 400 K, and a verified result -- as tests/test_research_ledger.py
    builds them."""

    def __init__(self, tmp):
        self.log = EventLog(Path(tmp) / "log.jsonl")
        self.evidence = EvidenceStore(Path(tmp) / "evidence")
        self.led = R.ResearchLedger(self.log, evidence=self.evidence).load()
        self.led.register(objective_id="o1", statement="is it below 400 K?",
                          owner="owner", max_steps=5, max_inference_calls=5)
        self.bundle = b""

    def hypothesis(self):
        from agent_substrate import proposals as PR
        resp = {"kind": "HYPOTHESIS", "provider": None, "model": None,
                "rationale": "proposal 1",
                "request": {"hypothesis_id": "h-any", "statement": "below",
                            "prediction": dict(PRED), "depends_on": []}}
        ctx = PR.assemble_context("q", retrieve=lambda q, k: {"hits": []})
        env = PR.ProposalEnvelope.parse(PR.envelope(
            resp, context=ctx, agent_id="agent-a",
            adapter=PR.RecordedFixtureAdapter("")))
        self.log.append(actor=PR.INGRESS_ID, action=PR.ACT_PROPOSAL_RECEIVE,
                        target=env.proposal_id,
                        payload={"envelope": env.record,
                                 "envelope_digest": env.digest(),
                                 "received_by": PR.INGRESS_ID,
                                 "authority": PR.AUTHORITY})
        self.led.hypothesize(hypothesis_id="h1", objective_id="o1",
                             statement="T_at_half stays below 400 K",
                             prediction=dict(PRED), depends_on=[],
                             proposal_id=env.proposal_id,
                             proposed_by="agent-a")

    def verified(self, *, value, resolution):
        self.bundle = json.dumps(_bundle(resolution, value)).encode()
        dg = self.evidence.put(self.bundle)
        self.log.append(actor="governed", action="record.create",
                        target="rec-1", payload={"record_id": "rec-1",
                                                 "evidence": {
                                                     "result_bundle": dg}})
        self.log.append(actor="reviewer", action="record.transition",
                        target="rec-1", payload={"record_id": "rec-1",
                                                 "dst": "VERIFIED"})
        return dg

    def observe(self):
        return self.led.observe(observation_id="obs-1", objective_id="o1",
                                record_id="rec-1", bundle_bytes=self.bundle)

    def reload(self):
        return R.ResearchLedger(self.log, evidence=self.evidence).load()

    def as_an_earlier_build_wrote_it(self, *, value, resolution):
        """The observation an earlier build's ledger wrote for such a
        bundle, its resolution as stated, and the verdict it computed."""
        self.hypothesis()
        dg = self.verified(value=value, resolution=resolution)
        self.log.append(actor=R.CONTROLLER_ID, action=R.ACT_OBSERVATION,
                        target="obs-1", payload={"observation": {
                            "observation_id": "obs-1", "objective_id": "o1",
                            "record_id": "rec-1", "result_bundle": dg,
                            "model_id": MODEL, "values": {Q: {
                                "value": value, "unit": "K",
                                "resolution": resolution}}}})
        self.log.append(actor=R.CONTROLLER_ID, action=R.ACT_STATUS,
                        target="h1", payload={
                            "target_id": "h1", "status": "SUPPORTED",
                            "reason": "computed by an earlier build",
                            "basis": ["obs-1"]})


@pytest.fixture()
def w(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize("resolution, values, verdict", [
    (-15.0, {}, "INCONCLUSIVE"),
    (0.5, {Q: {"value": 410.0, "unit": "K", "resolution": 0.5}}, "REFUTED"),
])
def test_the_ledger_observes_no_value_from_a_resolution_below_zero(
        w, resolution, values, verdict):
    """410 K is not below 400 K; held to the interval from 425 K down to
    395 K, it was."""
    w.hypothesis()
    w.verified(value=410.0, resolution=resolution)
    assert w.observe()["values"] == values
    assert w.led.evaluate_hypothesis("h1") == verdict
    w.reload()
    assert reconstruct_subsystems(w.log).anomalies == []


def test_a_history_an_earlier_build_wrote_on_such_a_value_is_refused(w):
    w.as_an_earlier_build_wrote_it(value=410.0, resolution=-15.0)
    with pytest.raises(R.ResearchError, match="reports values or a time "
                                              "its bundle does not hold"):
        w.reload()
    # read with no evidence store, the verdict is computed again, and the
    # value decides nothing
    with pytest.raises(R.ResearchError, match="computed, never asserted"):
        R.ResearchLedger(w.log).load()


def test_the_second_reader_reports_such_a_verdict(w):
    w.as_an_earlier_build_wrote_it(value=410.0, resolution=-15.0)
    found = reconstruct_subsystems(w.log).anomalies
    assert any("a hypothesis verdict SUPPORTED the cited observations do "
               "not give" in a for a in found), found
