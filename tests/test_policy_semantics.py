"""The policy engine, an independent reference model, and the replay
verifier, compared over generated inputs (section 6.2).

THE REFERENCE MODEL

Written from the semantics ``agent_substrate.policy`` STATES -- deny
overrides, the default is deny, "*" must be spelled out, obligations come
from every matching ALLOW -- in the shape formally specified authorization
languages give them (Cedar's): a request is authorized when at least one
permit is satisfied and no forbid is; the determining policies are the
satisfied forbids if there are any, else the satisfied permits. It is a few
lines of set arithmetic over plain dictionaries, imports nothing from the
engine, and is used ONLY here. It is not a second runtime authority: nothing
outside this file calls it, and nothing is ever decided by it.

THE THREE ANSWERS

* the engine: ``PolicyDocument.evaluate``;
* the reference: :func:`authorize`, below;
* the replay verifier: the decision recorded through ``PolicyStore`` and
  read back by BOTH readers -- the policy store's replay, which re-derives
  the verdict with the engine, and the second reader, which restates the
  evaluation itself and shares no code with the engine.

Any disagreement is a reproducible defect: Hypothesis prints the document
and the request that produced it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    OBLIGATIONS, Effect, PolicyError, PolicyRequest, PolicyStore, document,
    rule,
)
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402

FIELDS = (("actions", "action"), ("subjects", "subject"),
          ("roles", "role"), ("resources", "resource"))


# ------------------------------------------------------ the reference model

def authorize(policies: list, request: dict) -> dict:
    """Cedar-shaped evaluation of a list of rule bodies (as published)."""
    def satisfied(p):
        return all(request[attr] in p[scope] or "*" in p[scope]
                   for scope, attr in FIELDS)
    sat = [p for p in policies if satisfied(p)]
    forbids = [p["rule_id"] for p in sat if p["effect"] == "DENY"]
    permits = [p["rule_id"] for p in sat if p["effect"] == "ALLOW"]
    allowed = bool(permits) and not forbids
    determining = forbids or permits
    return {"allowed": allowed,
            "effect": "ALLOW" if allowed else "DENY",
            "rule_id": determining[0] if determining else None,
            "obligations": sorted({o for p in policies
                                   if p["rule_id"] in permits
                                   for o in p["obligations"]})
            if allowed else []}


# ------------------------------------------------------ generated inputs

NAMES = st.sampled_from(["a", "b", "*"])
VALUES = st.sampled_from(["a", "b", "c", "*"])


@st.composite
def documents(draw):
    n = draw(st.integers(1, 6))
    rules = []
    for i in range(n):
        effect = draw(st.sampled_from([Effect.ALLOW, Effect.DENY]))
        rules.append(rule(
            rule_id=f"r{i}", effect=effect,
            **{scope: draw(st.lists(NAMES, min_size=1, max_size=2))
               for scope, _ in FIELDS},
            obligations=draw(st.lists(st.sampled_from(sorted(OBLIGATIONS)),
                                      max_size=2))
            if effect is Effect.ALLOW else ()))
    return document(policy_id="p", version=1, rules=rules)


@st.composite
def requests(draw):
    return PolicyRequest(**{attr: draw(VALUES) for _, attr in FIELDS},
                         task_id=draw(st.sampled_from(["", "t1"])),
                         attributes=draw(st.sampled_from(
                             [{}, {"role": "ADMIN"}, {"allow": True}])))


def _engine(doc, req) -> dict:
    d = doc.evaluate(req)
    return {"allowed": d.allowed, "effect": d.effect.value,
            "rule_id": d.rule_id, "obligations": list(d.obligations)}


def _reference(doc, req) -> dict:
    return authorize(doc.body()["rules"], req.to_record())


@settings(max_examples=2000, deadline=None, derandomize=True, database=None)
@given(documents(), requests())
def test_the_engine_and_the_reference_agree(doc, req):
    assert _engine(doc, req) == _reference(doc, req), (
        json.dumps(doc.body(), indent=1), req.to_record())


@settings(max_examples=150, deadline=None, derandomize=True, database=None)
@given(documents(), st.lists(requests(), min_size=1, max_size=4))
def test_the_replay_verifier_agrees_with_both(tmp_path_factory, doc, reqs):
    log = EventLog(tmp_path_factory.mktemp("pol") / "log.jsonl")
    store = PolicyStore(log).load()
    store.publish(doc, actor="owner")
    for req in reqs:
        d = store.decide_and_record("p", req, actor="caller")
        rec = {"allowed": d.allowed, "effect": d.effect.value,
               "rule_id": d.rule_id, "obligations": list(d.obligations)}
        assert rec == _reference(doc, req)
    PolicyStore(log).load()                    # the policy store's replay
    assert reconstruct_subsystems(log).anomalies == []   # the second reader


# --------------------------------------------- a disagreement is caught

@pytest.mark.parametrize("field, value", [
    ("allowed", True), ("effect", "ALLOW"), ("rule_id", "r1"),
    ("obligations", []),
])
def test_a_recorded_verdict_the_rules_do_not_give_is_caught_by_both(
        tmp_path, field, value):
    """The replay verifier is not vacuous: a decision record whose verdict
    the published rules do not produce -- appended around the store, with
    a digest consistent with its own body -- is refused by the policy
    store's replay and noted by the second reader."""
    from agent_substrate.canonical import digest
    doc = document(policy_id="p", version=1, rules=(
        rule(rule_id="r0", effect=Effect.DENY, actions=("a",),
             subjects=("*",), roles=("*",), resources=("*",)),
        rule(rule_id="r1", effect=Effect.ALLOW, actions=("*",),
             subjects=("*",), roles=("*",), resources=("*",),
             obligations=("record_evidence",)),
    ))
    log = EventLog(tmp_path / "log.jsonl")
    store = PolicyStore(log).load()
    store.publish(doc, actor="owner")
    deny = store.decide_and_record("p", PolicyRequest(
        action="a", subject="s", role="r", resource="x"), actor="caller")
    allow = store.decide_and_record("p", PolicyRequest(
        action="b", subject="s", role="r", resource="x"), actor="caller")
    assert reconstruct_subsystems(log).anomalies == []       # control
    honest = (allow if field == "obligations" else deny).to_record()
    forged = dict(honest, **{field: value})
    body = {k: v for k, v in forged.items() if k != "at_seq"}
    log.append(actor="caller", action="policy.decision", target="x",
               payload={"decision": forged, "decision_digest": digest(body)})
    with pytest.raises(PolicyError, match="rules do not produce"):
        PolicyStore(log).load()
    found = reconstruct_subsystems(log).anomalies
    assert any("the rules of 'p' do not give" in a for a in found), found


def test_the_second_reader_judges_under_the_version_in_force(tmp_path):
    """A later version that narrows the first is the one a decision is
    restated under: judged by the superseded rules, an honest DENY would
    look like a forgery."""
    log = EventLog(tmp_path / "log.jsonl")
    store = PolicyStore(log).load()
    every = dict(subjects=("*",), roles=("*",), resources=("*",))
    store.publish(document(policy_id="p", version=1, rules=(
        rule(rule_id="open", effect=Effect.ALLOW, actions=("*",), **every),
    )), actor="owner")
    store.publish(document(policy_id="p", version=2, rules=(
        rule(rule_id="shut", effect=Effect.DENY, actions=("a",), **every),
        rule(rule_id="open", effect=Effect.ALLOW, actions=("*",), **every),
    )), actor="owner")
    d = store.decide_and_record("p", PolicyRequest(
        action="a", subject="s", role="r", resource="x"), actor="caller")
    assert (d.allowed, d.rule_id) == (False, "shut")
    assert reconstruct_subsystems(log).anomalies == []
