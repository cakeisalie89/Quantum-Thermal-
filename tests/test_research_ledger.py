"""The research ledger: hypotheses are decided by the record, never asserted.

Unit tests of the rules, each from the side that would break it: the
evaluation (resolution, units, the identity of a quantity, disagreement), who
may record what, what an observation may be read from, what a verdict and a
conclusion must rest on, and how lost currency travels. A history the ledger
would not have written, appended around it, is refused on every load.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import research as R  # noqa: E402
from agent_substrate.canonical import digest_bytes  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402

PRED = {"model_id": "thermal.slab_transient", "quantity": "T_at_half",
        "op": "<", "value": 400.0, "unit": "K"}


def _obs(value, resolution=0.5, unit="K", quantity="T_at_half",
         model="thermal.slab_transient"):
    return {"model_id": model,
            "values": {quantity: {"value": value, "unit": unit,
                                  "resolution": resolution}}}


# ------------------------------------------------------------- evaluation

@pytest.mark.parametrize("value, resolution, want", [
    (350.0, 0.5, "SUPPORTED"),
    (450.0, 0.5, "REFUTED"),
    (399.8, 0.5, "INCONCLUSIVE"),          # the interval straddles 400
    (400.0, 0.0, "REFUTED"),               # 400 is not < 400
])
def test_a_prediction_is_decided_with_its_resolution(value, resolution, want):
    assert R.evaluate(PRED, [_obs(value, resolution)])[0] == want


def test_a_value_in_another_unit_is_not_converted():
    verdict, why = R.evaluate(PRED, [_obs(77.0, unit="degC")])
    assert verdict == "INCONCLUSIVE" and "converts nothing" in why


def test_the_same_unit_at_another_place_is_another_quantity():
    """Dimensional compatibility is not quantity identity: T_at_0 is in
    kelvin too, and says nothing about T_at_half."""
    verdict, why = R.evaluate(PRED, [_obs(350.0, quantity="T_at_0")])
    assert verdict == "INCONCLUSIVE" and "reports T_at_half" in why


def test_another_model_s_output_is_not_this_one_s():
    assert R.evaluate(PRED, [_obs(350.0, model="thermal.rc2_network")])[0] \
        == "INCONCLUSIVE"


def test_disagreeing_observations_decide_nothing():
    verdict, why = R.evaluate(PRED, [_obs(350.0), _obs(450.0)])
    assert verdict == "INCONCLUSIVE" and "disagree" in why


@pytest.mark.parametrize("bad", [
    dict(PRED, op="approximately"), dict(PRED, value=float("nan")),
    dict(PRED, value=True), {k: v for k, v in PRED.items() if k != "unit"},
])
def test_a_prediction_is_structured_or_refused(bad):
    with pytest.raises(R.ResearchError):
        R.check_prediction(bad)


# ------------------------------------------------------------------ world

class World:
    def __init__(self, tmp):
        self.log = EventLog(Path(tmp) / "log.jsonl")
        self.evidence = EvidenceStore(Path(tmp) / "evidence")
        self.led = R.ResearchLedger(self.log, evidence=self.evidence).load()
        self.led.register(objective_id="o1", statement="is it below 400 K?",
                          owner="owner", max_steps=5, max_inference_calls=5)
        self.bundles: dict = {}

    def proposal(self, label, agent="agent-a", kind="HYPOTHESIS"):
        """A real receipt of a real HYPOTHESIS envelope; returns its id."""
        from agent_substrate import proposals as PR
        resp = {"kind": kind, "provider": None, "model": None,
                "rationale": f"proposal {label}",
                "request": {"hypothesis_id": "h-any", "statement": label,
                            "prediction": dict(PRED), "depends_on": []}}
        ctx = PR.assemble_context("q", retrieve=lambda q, k: {"hits": []})
        env = PR.ProposalEnvelope.parse(PR.envelope(
            resp, context=ctx, agent_id=agent,
            adapter=PR.RecordedFixtureAdapter("")))
        self.log.append(actor=PR.INGRESS_ID, action=PR.ACT_PROPOSAL_RECEIVE,
                        target=env.proposal_id,
                        payload={"envelope": env.record,
                                 "envelope_digest": env.digest(),
                                 "received_by": PR.INGRESS_ID,
                                 "authority": PR.AUTHORITY})
        return env.proposal_id

    def hypothesis(self, hid="h1", label="prop-1", agent="agent-a",
                   depends_on=(), prediction=None):
        pid = self.proposal(label, agent)
        self.led.hypothesize(hypothesis_id=hid, objective_id="o1",
                             statement="T_at_half stays below 400 K",
                             prediction=prediction or dict(PRED),
                             depends_on=list(depends_on), proposal_id=pid,
                             proposed_by=agent)

    def verified(self, rid="rec-1", value=350.0, resolution=0.5, state=
                 "VERIFIED"):
        bundle = json.dumps({"model_id": "thermal.slab_transient",
                             "outputs": [{"name": "T_at_half", "status": "OK",
                                          "quantity": {"value": value,
                                                       "unit": "K",
                                                       "resolution":
                                                           resolution}}]}
                            ).encode()
        dg = self.evidence.put(bundle)
        self.bundles[rid] = bundle
        self.log.append(actor="governed", action="record.create", target=rid,
                        payload={"record_id": rid, "evidence": {
                            "result_bundle": dg}})
        self.log.append(actor="reviewer", action="record.transition",
                        target=rid, payload={"record_id": rid, "dst": state})
        return dg

    def observe(self, oid="obs-1", rid="rec-1"):
        return self.led.observe(observation_id=oid, objective_id="o1",
                                record_id=rid, bundle_bytes=self.bundles[rid])

    def reload(self):
        return R.ResearchLedger(self.log, evidence=self.evidence).load()


@pytest.fixture()
def w(tmp_path):
    return World(tmp_path)


def refused(w, fn, match):
    """``fn`` is refused, nothing is written, and the history still loads:
    a refusal under the lock, not a record its own replay rejects."""
    before = w.log.head().seq
    with pytest.raises(R.ResearchError, match=match):
        fn()
    assert w.log.head().seq == before, "the refused record was written"
    w.reload()


def test_a_hypothesis_is_decided_by_the_record(w):
    w.hypothesis()
    w.verified()
    w.observe()
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"
    assert w.led.get("h1").basis == ("obs-1",)
    w.led.set_status("o1", "CONCLUDED", reason="h1 holds", basis=["h1"])
    assert w.reload().get("o1").status == "CONCLUDED"


def test_a_verdict_is_computed_never_asserted(w):
    w.hypothesis()
    w.verified(value=450.0)
    w.observe()
    refused(w, lambda: w.led.set_status("h1", "SUPPORTED",
                                        reason="the model is sure",
                                        basis=["obs-1"]),
            "computed, never asserted")
    # appended around the ledger, it is refused on every load
    w.log.append(actor=R.CONTROLLER_ID, action=R.ACT_STATUS, target="h1",
                 payload={"target_id": "h1", "status": "SUPPORTED",
                          "reason": "forged", "basis": ["obs-1"]})
    with pytest.raises(R.ResearchError, match="computed, never asserted"):
        w.reload()


def test_an_objective_is_its_owner_s_question(w):
    w.log.append(actor="agent-a", action=R.ACT_OBJECTIVE, target="o2",
                 payload={"objective": {
                     "objective_id": "o2", "statement": "mine",
                     "owner": "owner", "parent_id": None,
                     "limits": {"max_steps": 1, "max_inference_calls": 1}}})
    with pytest.raises(R.ResearchError, match="its owner's question"):
        w.reload()


def test_a_hypothesis_cites_a_received_proposal_of_its_proposer(w):
    refused(w, lambda: w.led.hypothesize(
        hypothesis_id="h1", objective_id="o1", statement="s",
        prediction=dict(PRED), depends_on=[], proposal_id="prop-x",
        proposed_by="agent-a"), "did not receive")
    pid = w.proposal("prop-2", agent="agent-b")
    refused(w, lambda: w.led.hypothesize(
        hypothesis_id="h1", objective_id="o1", statement="s",
        prediction=dict(PRED), depends_on=[], proposal_id=pid,
        proposed_by="agent-a"), "did not receive")


def test_an_agent_does_not_record_its_own_hypothesis(w):
    pid = w.proposal("prop-1")
    w.log.append(actor="agent-a", action=R.ACT_HYPOTHESIS, target="h1",
                 payload={"hypothesis": {
                     "hypothesis_id": "h1", "objective_id": "o1",
                     "statement": "s", "prediction": dict(PRED),
                     "depends_on": [], "proposal_id": pid,
                     "proposed_by": "agent-a"}})
    with pytest.raises(R.ResearchError, match="controller records"):
        w.reload()


def test_a_retracted_hypothesis_cannot_be_cited_as_current(w):
    w.hypothesis("h1", "prop-1")
    w.led.set_status("h1", "RETRACTED", reason="withdrawn by its author")
    pid = w.proposal("prop-2")
    refused(w, lambda: w.led.hypothesize(
        hypothesis_id="h2", objective_id="o1", statement="s",
        prediction=dict(PRED), depends_on=["h1"], proposal_id=pid,
        proposed_by="agent-a"), "not current")


@pytest.mark.parametrize("state", ["PROPOSED", "UNDER_REVIEW", "REJECTED",
                                   "STALE"])
def test_an_observation_is_read_only_from_a_verified_result(w, state):
    w.verified(state=state)
    refused(w, w.observe, "VERIFIED or PROMOTED")


def test_an_observation_s_values_are_the_bundle_s(w):
    dg = w.verified()
    w.log.append(actor=R.CONTROLLER_ID, action=R.ACT_OBSERVATION,
                 target="obs-1", payload={"observation": {
                     "observation_id": "obs-1", "objective_id": "o1",
                     "record_id": "rec-1", "result_bundle": dg,
                     "model_id": "thermal.slab_transient",
                     "values": {"T_at_half": {"value": 10.0, "unit": "K",
                                              "resolution": 0.5}}}})
    with pytest.raises(R.ResearchError, match="does not hold"):
        w.reload()


def test_an_observation_names_the_bundle_its_record_cites(w):
    w.verified()
    refused(w, lambda: w.led.observe(
        observation_id="obs-1", objective_id="o1", record_id="rec-1",
        bundle_bytes=b'{"outputs": []}'), "does not cite")


def test_a_conclusion_rests_on_a_current_decided_hypothesis(w):
    w.hypothesis()
    refused(w, lambda: w.led.set_status("o1", "CONCLUDED", reason="it is so",
                                        basis=["h1"]), "one current, decided")


def test_lost_currency_travels_to_the_conclusion(w):
    w.hypothesis()
    w.verified()
    w.observe()
    w.led.evaluate_hypothesis("h1")
    w.led.set_status("o1", "CONCLUDED", reason="h1 holds", basis=["h1"])
    # the result loses its standing
    w.log.append(actor="system", action="record.transition", target="rec-1",
                 payload={"record_id": "rec-1", "dst": "STALE"})
    moved = w.led.propagate()
    assert moved == ["obs-1", "h1", "o1"]
    again = w.reload()
    assert [again.get(x).status for x in ("obs-1", "h1", "o1")] == [
        "STALE"] * 3
    assert again.propagate() == []


def test_staleness_cannot_be_declared_without_cause(w):
    w.hypothesis()
    refused(w, lambda: w.led.set_status("h1", "STALE",
                                        reason="I would rather"),
            "lost its currency")


def test_new_disagreeing_evidence_makes_a_verdict_inconclusive(w):
    w.hypothesis()
    w.verified("rec-1", 350.0)
    w.observe("obs-1", "rec-1")
    assert w.led.evaluate_hypothesis("h1") == "SUPPORTED"
    w.verified("rec-2", 450.0)
    w.observe("obs-2", "rec-2")
    assert w.led.evaluate_hypothesis("h1") == "INCONCLUSIVE"


def test_steps_are_numbered_and_bounded(w):
    for n in range(1, 6):
        w.led.record_step(objective_id="o1", step=n, proposal_id=None,
                          act="INFER", outcome="OK")
    refused(w, lambda: w.led.record_step(objective_id="o1", step=7,
                                         proposal_id=None, act="INFER",
                                         outcome="OK"), "next")
    w.log.append(actor=R.CONTROLLER_ID, action=R.ACT_STEP, target="o1",
                 payload={"step": {"objective_id": "o1", "step": 6,
                                   "proposal_id": None, "act": "INFER",
                                   "outcome": "OK", "detail": ""}})
    with pytest.raises(R.ResearchError, match="past the objective's limit"):
        w.reload()


def test_values_are_read_from_ok_outputs_only():
    bundle = {"outputs": [
        {"name": "a", "status": "OK", "quantity": {"value": 1.0, "unit": "K",
                                                   "resolution": 0.1}},
        {"name": "b", "status": "FAILED", "quantity": None}]}
    assert R.values_of(bundle) == {"a": {"value": 1.0, "unit": "K",
                                         "resolution": 0.1}}
    assert digest_bytes(b"x")


def test_an_objective_registered_twice_is_refused_unwritten(w):
    refused(w, lambda: w.led.register(
        objective_id="o1", statement="again", owner="owner", max_steps=1,
        max_inference_calls=1), "recorded twice")


def test_a_verdict_cannot_rest_on_a_stale_observation(w):
    w.hypothesis()
    w.verified()
    w.observe()
    w.log.append(actor="system", action="record.transition", target="rec-1",
                 payload={"record_id": "rec-1", "dst": "STALE"})
    w.led.set_status("obs-1", "STALE", reason="its record lost standing")
    refused(w, lambda: w.led.set_status("h1", "SUPPORTED", reason="x",
                                        basis=["obs-1"]),
            "not all current observations")


def test_a_conclusion_goes_stale_when_its_hypothesis_is_no_longer_decided(w):
    w.hypothesis()
    w.verified("rec-1", 350.0)
    w.observe("obs-1", "rec-1")
    w.led.evaluate_hypothesis("h1")
    w.led.set_status("o1", "CONCLUDED", reason="h1 holds", basis=["h1"])
    w.verified("rec-2", 450.0)
    w.observe("obs-2", "rec-2")
    assert w.led.evaluate_hypothesis("h1") == "INCONCLUSIVE"
    assert w.led.propagate() == ["o1"]
    assert w.reload().get("o1").status == "STALE"


def test_a_decided_hypothesis_does_not_flip_in_one_move(w):
    w.hypothesis()
    w.verified("rec-1", 350.0)
    w.observe("obs-1", "rec-1")
    w.led.evaluate_hypothesis("h1")
    w.verified("rec-2", 450.0)
    w.observe("obs-2", "rec-2")
    refused(w, lambda: w.led.set_status("h1", "REFUTED", reason="latest",
                                        basis=["obs-2"]), "not a move")


# ------------------------------------------- the second reader agrees

def _second(w) -> list:
    from agent_substrate.reconstruct import reconstruct_subsystems
    return reconstruct_subsystems(w.log).anomalies


@pytest.mark.parametrize("forge, note", [
    (lambda w: w.log.append(
        actor="agent-a", action=R.ACT_OBJECTIVE, target="o2",
        payload={"objective": {"objective_id": "o2", "statement": "s",
                               "owner": "owner", "parent_id": None,
                               "limits": {"max_steps": 1,
                                          "max_inference_calls": 1}}}),
     "in 'owner''s name"),
    (lambda w: w.log.append(
        actor="agent-a", action=R.ACT_HYPOTHESIS, target="h1",
        payload={"hypothesis": {
            "hypothesis_id": "h1", "objective_id": "o1", "statement": "s",
            "prediction": dict(PRED), "depends_on": [],
            "proposal_id": w.proposal("prop-1"),
            "proposed_by": "agent-a"}}),
     "recording its own hypothesis"),
    (lambda w: (w.hypothesis(), w.verified(value=450.0), w.observe(),
                w.log.append(actor=R.CONTROLLER_ID, action=R.ACT_STATUS,
                             target="h1", payload={
                                 "target_id": "h1", "status": "SUPPORTED",
                                 "reason": "forged", "basis": ["obs-1"]})),
     "do not give"),
    (lambda w: (w.verified(state="REJECTED"), w.log.append(
        actor=R.CONTROLLER_ID, action=R.ACT_OBSERVATION, target="obs-1",
        payload={"observation": {
            "observation_id": "obs-1", "objective_id": "o1",
            "record_id": "rec-1", "result_bundle": "0" * 64,
            "model_id": "thermal.slab_transient", "values": {}}})),
     "'REJECTED'"),
    (lambda w: (w.hypothesis(), w.log.append(
        actor=R.CONTROLLER_ID, action=R.ACT_STATUS, target="o1",
        payload={"target_id": "o1", "status": "CONCLUDED",
                 "reason": "forged", "basis": ["h1"]})),
     "no decided hypothesis"),
    (lambda w: [w.log.append(
        actor=R.CONTROLLER_ID, action=R.ACT_STEP, target="o1",
        payload={"step": {"objective_id": "o1", "step": n,
                          "proposal_id": None, "act": "INFER",
                          "outcome": "OK", "detail": ""}})
        for n in range(1, 7)],
     "past its objective's limit"),
])
def test_the_second_reader_notes_each_forgery_too(w, forge, note):
    forge(w)
    with pytest.raises(R.ResearchError):
        w.reload()
    found = _second(w)
    assert any(note in a for a in found), found
