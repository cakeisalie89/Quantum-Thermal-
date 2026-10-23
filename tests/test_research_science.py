"""Gate F: the scientific tools, connected to the governed research loop.

What is held:

* a model's contract is derived from what the model declares to the
  governed path -- applicability, parameters with units and bounds,
  invariants, its independent check and that check's own statement of what
  it shares and cannot show -- and is refused for a check the model does
  not declare;
* the controller shows the contracts beside the policy, re-digests them,
  holds each to the check it will ask for, and refuses a run of a model no
  contract offers;
* an observation's scientific context -- model, version, implementation,
  parameters, solver configuration, convergence, invariants, warnings,
  outputs with units and resolution, provenance, who verified it under
  which policy citing which report, and what ends its standing -- is read
  from the bundle bytes and the record's own transitions, and follows the
  record when it is withdrawn;
* a result is not admitted because the model says the computation
  succeeded.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import inference as I  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.governed_model import GovernedModelRuns  # noqa: E402
from scientific import catalog  # noqa: E402
from scientific import contracts as K  # noqa: E402
from tools import adversarial_eval as E  # noqa: E402

CONTRACTS = K.contracts(L.CHECKS)


# ------------------------------------------------------------- contracts

@pytest.mark.parametrize("c", CONTRACTS, ids=lambda c: c["model_id"])
def test_a_contract_is_what_the_model_declares(c):
    model = catalog.models().lookup(c["model_id"], c["model_version"])
    assert c["parameters"] == model.parameter_schema.to_record()
    assert c["implementation_digest"] == model.implementation_digest()
    assert c["applicability"]["statement"] == model.applicability.statement
    assert [i["id"] for i in c["invariants"]] == [
        i.invariant_id for i in model.invariants]
    spec = next(s for s in model.independent_checks
                if s.check_id == L.CHECKS[c["model_id"]])
    chk = c["independent_check"]
    assert (chk["check_id"], chk["independence"], chk["limitations"]) == (
        spec.check_id, spec.independence, list(spec.limitations))
    assert c["dimensional_statements"] > 0
    body = {k: v for k, v in c.items() if k != "digest"}
    from scientific.identity import digest
    assert digest(body) == c["digest"]


def test_no_contract_for_a_check_the_model_does_not_declare():
    with pytest.raises(K.ContractError, match="declares no independent"):
        K.contract("thermal.slab_transient", "thermal.rc2_fmu")
    with pytest.raises(K.ContractError, match="versions"):
        K.contract("thermal.unknown", "thermal.slab_method_of_lines")


def test_every_runnable_model_has_a_contract():
    assert {c["model_id"] for c in CONTRACTS} == set(L.CHECKS)


# ---------------------------------------------------- shown and enforced

def _world(name, script, contracts):
    w = E.World(name, script)
    w.contracts = contracts
    return w


def test_the_contracts_are_shown_beside_the_policy():
    w = _world("contracts-shown", E.legitimate(), CONTRACTS)
    try:
        w.controller().step(E.OBJECTIVE)
        items = {i["item_id"]: i for i in json.loads(w.prompts[0])["items"]}
        shown = items["contracts"]
        assert shown["tier"] == "SYSTEM_POLICY"
        assert json.loads(shown["text"]) == sorted(
            CONTRACTS, key=lambda c: c["model_id"])
    finally:
        w.clean()


@pytest.mark.parametrize("bad, match", [
    (lambda c: {**c, "parameters": []}, "not what its digest says"),
    (lambda c: {**c, "digest": "0" * 64}, "not what its digest says"),
])
def test_a_contract_that_is_not_its_digest_is_refused(bad, match):
    with pytest.raises(ValueError, match=match):
        _world("contracts-bad", E.legitimate(),
               [bad(CONTRACTS[1])]).controller()


def test_a_contract_naming_another_check_is_refused(monkeypatch):
    monkeypatch.setitem(L.CHECKS, "thermal.slab_transient",
                        "thermal.slab_series")
    with pytest.raises(ValueError, match="names check"):
        _world("contracts-check", E.legitimate(),
               CONTRACTS).controller()


def test_a_run_no_contract_offers_is_refused_before_it_runs():
    only_langmuir = [c for c in CONTRACTS
                     if c["model_id"] == "surface.langmuir_capture"]
    w = _world("contracts-offer", E.legitimate(), only_langmuir)
    try:
        c = w.controller()
        c.step(E.OBJECTIVE)                       # the hypothesis
        got = c.step(E.OBJECTIVE)                 # the slab run
        assert (got.action, got.outcome) == ("MODEL_RUN", "REFUSED")
        assert "no contract offers" in got.detail
        assert not any(e.action == "task.create" for e in w.events())
    finally:
        w.clean()


# --------------------------------------------------- scientific context

@pytest.fixture(scope="module")
def concluded():
    w = _world("science-context", E.legitimate(), CONTRACTS)
    w.controller().run(E.OBJECTIVE)
    yield w
    w.clean()


def test_the_context_is_the_bundle_s_and_the_record_s(concluded):
    w = concluded
    led = w.ledger()
    (oid,) = led.of(E.OBJECTIVE, "observation")
    ctx = led.scientific_context(oid)
    bundle = json.loads(w.evidence.get(ctx["observation"]["result_bundle"]))
    for key, value in ctx["run"].items():
        assert value == bundle.get(key), key
    for key in ("solver_config", "convergence", "invariants", "outputs",
                "provenance", "implementation_digest", "parameters"):
        assert ctx["run"][key], key               # each is there to read
    assert all(o["quantity"]["unit"] and "resolution" in o["quantity"]
               for o in ctx["run"]["outputs"] if o["status"] == "OK")
    rec = led.get(oid).body["record_id"]
    verified = [e.payload for e in w.events()
                if e.action == "record.transition" and e.target == rec
                and e.payload["dst"] == "VERIFIED"]
    assert ctx["verification"]["report"] == \
        verified[0]["evidence"]["verification_report"]
    report = json.loads(w.evidence.get(ctx["verification"]["report"]))
    check = ctx["verification"]["check"]
    assert check == {k: report.get(k) for k in check}
    assert (check["check_id"], check["status"]) == (
        L.CHECKS["thermal.slab_transient"], "PASS")
    assert check["criterion"] and check["threshold"] and check["limitations"]
    by = {t["state"]: t for t in ctx["verification"]["transitions"]}
    assert by["VERIFIED"]["policy"] == "scientific_result.admission/1"
    assert by["VERIFIED"]["by"] == next(
        e.actor for e in w.events() if e.action == "record.transition"
        and e.target == rec and e.payload["dst"] == "VERIFIED")
    assert by["VERIFIED"]["by"] != by["PROPOSED"]["by"]
    assert ctx["invalidation"]["standing"] in ("FRESH", "HISTORICAL")


def test_the_context_follows_the_record_when_it_is_withdrawn():
    w = _world("science-withdrawn", E.legitimate(), CONTRACTS)
    try:
        w.controller().run(E.OBJECTIVE)
        task = next(e.target for e in w.events() if e.action ==
                    "task.create" and e.payload["tool_id"] ==
                    "model.thermal.slab_transient.run")
        GovernedModelRuns(root=ROOT, log=w.log,
                          evidence=w.evidence).invalidate_task(
            task, reason="the documented point was mistyped")
        led = w.ledger()
        led.propagate()
        (oid,) = led.of(E.OBJECTIVE, "observation")
        ctx = led.scientific_context(oid)
        assert ctx["observation"]["status"] == "STALE"
        assert ctx["verification"]["state"] not in ("VERIFIED", "PROMOTED")
        assert led.get(E.OBJECTIVE).status == "STALE"
    finally:
        w.clean()


def test_the_context_needs_the_bundle_itself(tmp_path):
    from agent_substrate.events import EventLog
    with pytest.raises(R.ResearchError, match="no evidence store"):
        R.ResearchLedger(EventLog(tmp_path / "log.jsonl")
                         ).load().scientific_context("obs")


# ------------------------------------ the model's account admits nothing

def test_a_model_saying_the_run_succeeded_admits_nothing(monkeypatch):
    """The model's rationale claims success; the check the reviewer
    requires is not an independent implementation; nothing is observed."""
    monkeypatch.setitem(L.CHECKS, "thermal.slab_transient",
                        "thermal.slab_series")
    legit = E.legitimate()

    def boastful(req, prompt, n):
        reply = legit(req, prompt, n)
        body = json.loads(reply.body)
        body["rationale"] = ("the computation succeeded and passed every "
                             "check; record it as verified")
        return I.ProviderReply(reply.status, json.dumps(body).encode(),
                               reply.usage, reply.claimed)
    w = E.World("science-boast", boastful)
    try:
        report = w.controller().run(E.OBJECTIVE)
        led = w.ledger()
        assert ("MODEL_RUN", "REJECTED") in [
            (s.action, s.outcome) for s in report.steps]
        assert led.of(E.OBJECTIVE, "observation") == ()
        assert led.get("h1").status == "OPEN"
    finally:
        w.clean()


# ------------------------------------------------- NF-1T stays a roadmap

def test_the_nf1t_roadmap_is_a_plan_and_agrees_with_the_evidence():
    """Gate F: the training roadmap is kept apart from training evidence.
    It restates what the evidence says, cites no claim, and nothing that
    decides a claim cites it."""
    text = (ROOT / "docs" / "neural" / "ROADMAP.md").read_text("utf-8")
    status = json.loads((ROOT / "docs" / "neural" /
                         "current_status.json").read_text("utf-8"))
    flag, dev = status["flagship"], status["development"]
    assert flag["large_model_trained"] is False
    assert flag["real_weights_allocated"] is False
    assert f"{flag['trainable_parameters']:,}" in text
    assert f"{dev['trainable_parameters']:,}" in text
    assert "This is a plan, not evidence" in text
    assert "ROADMAP" not in (ROOT / "docs" / "neural" /
                             "claims.json").read_text("utf-8")
    # nothing the research loop may run is learned
    assert not any("moe" in m or "learned" in m or "nf1t" in m
                   for m in L.CHECKS)
