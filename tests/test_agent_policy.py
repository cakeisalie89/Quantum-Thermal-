"""The policy engine: rules that decide, and decisions that outlive the run.

The requirement these tests defend is not "there is a policy object". It is
that a decision made months ago can be re-derived from the log alone: which
document, which version, which rule, and what it said at the time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import is_digest  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.policy import (  # noqa: E402
    ACT_POLICY_DECISION, ACT_POLICY_PUBLISH, ANY, Effect, PolicyDenied,
    PolicyError, PolicyRequest, PolicyStore, PolicyVersionError,
    UnknownPolicy, document, document_from_record, rule,
)


def _rule(rid="r1", effect=Effect.ALLOW, actions=("act",), subjects=(ANY,),
          roles=(ANY,), resources=(ANY,), reason=""):
    return rule(rule_id=rid, effect=effect, actions=actions,
                subjects=subjects, roles=roles, resources=resources,
                reason=reason)


def _doc(version=1, rules=None, policy_id="p"):
    return document(policy_id=policy_id, version=version,
                    rules=rules or (_rule(),))


def _req(action="act", subject="alice", role="WORKER", resource="res"):
    return PolicyRequest(action=action, subject=subject, role=role,
                         resource=resource)


@pytest.fixture()
def store(tmp_path):
    return PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


# ---- rule construction --------------------------------------------------
def test_empty_match_field_is_refused_not_treated_as_any():
    """An empty field on a DENY rule would forbid nothing.

    That is the dangerous direction: a truncated or half-edited prohibition
    silently stops prohibiting, and the ALLOW rule it was written to carve
    out of takes over. Refusing both effects keeps the failure loud.
    """
    with pytest.raises(PolicyError, match=r"empty.*mean 'any'"):
        _rule(effect=Effect.DENY, actions=())
    with pytest.raises(PolicyError, match="empty"):
        _rule(subjects=[])


def test_bare_string_match_field_is_refused():
    """``actions="act"`` would iterate to {'a','c','t'} and match nothing."""
    with pytest.raises(PolicyError, match="bare string"):
        _rule(actions="act")


def test_rule_fields_must_be_non_empty_strings():
    with pytest.raises(PolicyError, match="non-empty str"):
        _rule(actions=("act", ""))
    with pytest.raises(PolicyError, match="non-empty str"):
        _rule(subjects=("alice", 7))


def test_rule_id_and_effect_are_validated():
    with pytest.raises(PolicyError, match="rule_id"):
        _rule(rid="")
    with pytest.raises(PolicyError, match="effect must be an Effect"):
        _rule(effect="ALLOW")


# ---- document construction ---------------------------------------------
def test_empty_rule_set_is_refused():
    with pytest.raises(PolicyError, match="no rules"):
        document(policy_id="p", version=1, rules=())


def test_version_zero_is_refused():
    with pytest.raises(PolicyError, match=r"version must be an int >= 1"):
        document(policy_id="p", version=0, rules=(_rule(),))


def test_duplicate_rule_ids_are_refused():
    with pytest.raises(PolicyError, match="duplicate rule_id"):
        document(policy_id="p", version=1,
                 rules=(_rule("same"), _rule("same", effect=Effect.DENY)))


def test_document_digest_covers_rules_not_only_identity():
    a = _doc(rules=(_rule("r", effect=Effect.ALLOW),))
    b = _doc(rules=(_rule("r", effect=Effect.DENY),))
    assert a.policy_id == b.policy_id and a.version == b.version
    assert a.digest() != b.digest(), (
        "two documents with the same identity and different rules must be "
        "distinguishable, or a swapped policy is undetectable")
    assert is_digest(a.digest())


# ---- evaluation ---------------------------------------------------------
def test_no_matching_rule_denies():
    d = _doc(rules=(_rule("r", actions=("other",)),)).evaluate(_req())
    assert d.allowed is False
    assert d.rule_id is None
    assert "default is deny" in d.reason


def test_deny_overrides_allow_regardless_of_order():
    """The whole reason evaluation is not first-match."""
    allow = _rule("allow-all", effect=Effect.ALLOW)
    deny = _rule("deny-alice", effect=Effect.DENY, subjects=("alice",))
    for rules in ((allow, deny), (deny, allow)):
        d = document(policy_id="p", version=1, rules=rules).evaluate(_req())
        assert d.allowed is False, (
            "an ALLOW placed first must not defeat a DENY; under first-match "
            "it would, and the mistake would be invisible")
        assert d.rule_id == "deny-alice"


def test_every_field_must_match_conjunctively():
    r = _rule("r", actions=("act",), subjects=("alice",), roles=("WORKER",),
              resources=("res",))
    doc = document(policy_id="p", version=1, rules=(r,))
    assert doc.evaluate(_req()).allowed is True
    for over in ({"action": "other"}, {"subject": "mallory"},
                 {"role": "VERIFIER"}, {"resource": "elsewhere"}):
        assert doc.evaluate(_req(**over)).allowed is False, over


def test_attributes_are_never_consulted():
    """A free-form attribute match would make the rule language open-ended."""
    doc = _doc(rules=(_rule("r", subjects=("alice",)),))
    req = PolicyRequest(action="act", subject="mallory", role="WORKER",
                        resource="res", attributes={"subject": "alice"})
    assert doc.evaluate(req).allowed is False


def test_decision_digest_excludes_position_but_covers_verdict():
    doc = _doc()
    d = doc.evaluate(_req())
    from dataclasses import replace as _replace
    moved = _replace(d, at_seq=99)
    assert moved.digest() == d.digest(), (
        "the same decision recorded at a different position is the same "
        "decision")
    flipped = _replace(d, allowed=False)
    assert flipped.digest() != d.digest()


def test_raise_if_denied_names_what_was_refused():
    d = _doc(rules=(_rule("r", actions=("other",)),)).evaluate(_req())
    with pytest.raises(PolicyDenied, match="p@1"):
        d.raise_if_denied()


# ---- publication and versioning ----------------------------------------
def test_unpublished_policy_authorizes_nothing(store):
    with pytest.raises(UnknownPolicy):
        store.in_force("p")
    with pytest.raises(UnknownPolicy):
        store.evaluate("p", _req())


def test_publish_requires_gap_free_versions(store):
    store.publish(_doc(1), actor="owner")
    with pytest.raises(PolicyVersionError, match="must be version 2"):
        store.publish(_doc(3), actor="owner")
    with pytest.raises(PolicyVersionError):
        store.publish(_doc(1), actor="owner")
    store.publish(_doc(2), actor="owner")
    assert store.in_force("p").version == 2


def test_policy_is_not_retroactive(store):
    """I5, made real: a past decision is governed by the past policy."""
    permissive = _doc(1, rules=(_rule("open", effect=Effect.ALLOW),))
    store.publish(permissive, actor="owner")
    at_permissive = store.log.verify().head_seq
    assert store.evaluate("p", _req(), at_seq=at_permissive).allowed is True

    store.publish(_doc(2, rules=(_rule("shut", effect=Effect.DENY),)),
                  actor="owner")
    assert store.evaluate("p", _req()).allowed is False
    replayed = store.evaluate("p", _req(), at_seq=at_permissive)
    assert replayed.allowed is True, (
        "re-evaluating a historical action under today's rules answers a "
        "different question than the one an auditor is asking")
    assert replayed.version == 1


def test_position_before_first_publication_is_denied(store):
    store.log.append(actor="x", action="policy.decision", target="t",
                     payload={"decision": {}, "decision_digest": ""})
    store.publish(_doc(1), actor="owner")
    with pytest.raises(UnknownPolicy, match="after"):
        store.in_force_at("p", 0)


def test_versions_lists_every_publication(store):
    store.publish(_doc(1), actor="owner")
    store.publish(_doc(2), actor="owner")
    vs = store.versions("p")
    assert [v for _, v, _ in vs] == [1, 2]
    assert all(is_digest(d) for _, _, d in vs)


# ---- durability ---------------------------------------------------------
def test_policy_survives_a_restart(store, tmp_path):
    store.publish(_doc(1), actor="owner")
    store.publish(_doc(2, rules=(_rule("r2"),)), actor="owner")
    reloaded = PolicyStore(EventLog(tmp_path / "log.jsonl")).load()
    assert reloaded.in_force("p").digest() == store.in_force("p").digest()
    assert reloaded.versions("p") == store.versions("p")


def test_decisions_are_recorded_including_denials(store):
    store.publish(_doc(1, rules=(_rule("r", actions=("permitted",)),)),
                  actor="owner")
    allowed = store.decide_and_record("p", _req(action="permitted"),
                                      actor="scheduler")
    denied = store.decide_and_record("p", _req(action="forbidden"),
                                     actor="scheduler")
    assert allowed.allowed is True and denied.allowed is False
    assert allowed.at_seq >= 0 and denied.at_seq > allowed.at_seq

    recorded = [ev for ev in store.log.read()
                if ev.action == ACT_POLICY_DECISION]
    assert len(recorded) == 2, (
        "a control plane that logs only what it permitted cannot answer "
        "'what did this agent try', which is where an incident starts")
    assert recorded[1].payload["decision"]["allowed"] is False


# ---- tamper resistance --------------------------------------------------
def test_publication_with_a_mismatched_digest_is_refused(store, tmp_path):
    store.publish(_doc(1), actor="owner")
    path = tmp_path / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    rec["payload"]["document"]["rules"][0]["effect"] = "DENY"
    # Re-hash the EVENT so the chain still links: the attacker here is one who
    # can rewrite the log, and the question is whether the policy layer
    # notices that the document no longer matches its own claimed digest.
    from qta_agent.canonical import digest as _digest
    body = {k: v for k, v in rec.items() if k != "hash"}
    rec["hash"] = _digest(body)
    lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path.parent / "log.jsonl.head").write_text(
        json.dumps({"seq": rec["seq"], "head_hash": rec["hash"]},
                   sort_keys=True), encoding="utf-8")

    with pytest.raises(PolicyError, match="hashes to"):
        PolicyStore(EventLog(path)).load()


def test_rule_with_unknown_fields_is_refused():
    """A condition this reader cannot evaluate makes the rule narrower."""
    rec = _doc().body()
    rec["rules"][0]["only_on_tuesdays"] = True
    with pytest.raises(PolicyError, match="unknown fields"):
        document_from_record(rec)


def test_record_roundtrip_preserves_the_digest():
    doc = _doc(rules=(_rule("a"), _rule("b", effect=Effect.DENY)))
    assert document_from_record(doc.body()).digest() == doc.digest()


@pytest.mark.parametrize("bad", [
    None, [], "policy", {"policy_id": "p"},
    {"policy_id": "p", "version": 1, "rules": "not-a-list"},
    {"policy_id": "p", "version": 1, "rules": ["not-an-object"]},
])
def test_malformed_records_fail_closed(bad):
    with pytest.raises(PolicyError):
        document_from_record(bad)


def test_apply_ignores_foreign_actions(store):
    """One reducer among several over a shared log."""
    ev = store.log.append(actor="x", action="task.create", target="t",
                          payload={})
    assert store.apply(ev) is False


def test_a_version_gap_in_the_log_is_refused_on_replay(store, tmp_path):
    """The publish-time check is not the only way a gap can arrive.

    A forged or partially-recovered log can carry version 3 straight after
    version 1. Refusing only in :meth:`PolicyStore.publish` would leave the
    reader accepting exactly the history the writer would not have produced.
    """
    store.publish(_doc(1), actor="owner")
    gapped = _doc(3)
    store.log.append(actor="mallory", action=ACT_POLICY_PUBLISH, target="p",
                     payload={"document": gapped.body(),
                              "policy_digest": gapped.digest()})
    with pytest.raises(PolicyVersionError, match="gap-free"):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_a_backdated_version_in_the_log_is_refused_on_replay(store, tmp_path):
    store.publish(_doc(1), actor="owner")
    store.publish(_doc(2), actor="owner")
    replay = _doc(2, rules=(_rule("swapped", effect=Effect.DENY),))
    store.log.append(actor="mallory", action=ACT_POLICY_PUBLISH, target="p",
                     payload={"document": replay.body(),
                              "policy_digest": replay.digest()})
    with pytest.raises(PolicyVersionError):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_a_recorded_decision_carries_what_was_decided(store):
    """Recording that a decision happened is not recording the decision."""
    store.publish(_doc(1, rules=(_rule("r", actions=("permitted",)),)),
                  actor="owner")
    d = store.decide_and_record("p", _req(action="permitted"),
                                actor="scheduler")
    (ev,) = [e for e in store.log.read()
             if e.action == ACT_POLICY_DECISION]
    rec = ev.payload["decision"]
    assert rec["allowed"] is True
    assert rec["rule_id"] == "r"
    assert rec["policy_digest"] == store.in_force("p").digest()
    assert rec["request"]["action"] == "permitted"
    assert ev.payload["decision_digest"] == d.digest(), (
        "the recorded digest must match the decision, or the record cannot "
        "be checked against the decision it claims to be")


# --- a decision record is a claim, and the rules are the answer -------------
#
# Publication was already content-bound: a record whose document does not
# hash to the digest it cites is refused. DECISIONS were not. The reducer
# folded one in unread, which meant the log could carry an ALLOW that no
# published rule produces -- naming a rule that does not exist -- and the
# audit index repeated it verbatim as the reason something was permitted.
#
# The record carries the whole request, so the verdict is recomputable. It is
# recomputed.

def _forge(store, *, decision, digest_over=None):
    from qta_agent.canonical import digest as _digest

    body = {k: v for k, v in decision.items() if k != "at_seq"}
    store.log.append(
        actor="mallory", action=ACT_POLICY_DECISION, target="res",
        payload={"decision": decision,
                 "decision_digest": _digest(digest_over or body)})


def _denied_doc(store):
    """A policy under which _req() is denied by the closing default."""
    store.publish(_doc(1, rules=(_rule("r", actions=("something-else",)),)),
                  actor="owner")
    return store.in_force("p")


def _allow_record(doc, **over):
    rec = {"allowed": True, "policy_id": doc.policy_id,
           "version": doc.version, "policy_digest": doc.digest(),
           "rule_id": "a-rule-that-says-yes", "effect": "ALLOW",
           "request": _req().to_record(), "reason": "trust me",
           "at_seq": -1}
    rec.update(over)
    return rec


def test_a_forged_allow_is_refused_because_the_rules_say_otherwise(store,
                                                                   tmp_path):
    doc = _denied_doc(store)
    assert store.evaluate("p", _req()).allowed is False, "premise"
    _forge(store, decision=_allow_record(doc))
    with pytest.raises(PolicyError, match="A verdict the rules do not"):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_a_forged_decision_naming_a_rule_that_did_not_fire_is_refused(store,
                                                                      tmp_path
                                                                      ):
    """Not only the verdict: WHICH rule decided is the auditable part.

    An ALLOW attributed to the wrong rule sends an incident reviewer to read
    a rule that had nothing to do with it.
    """
    store.publish(_doc(1, rules=(_rule("r-real"),)), actor="owner")
    doc = store.in_force("p")
    _forge(store, decision=_allow_record(doc, rule_id="r-invented"))
    with pytest.raises(PolicyError, match="rule_id"):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_a_decision_citing_a_policy_version_that_is_not_in_force_is_refused(
        store, tmp_path):
    doc = _denied_doc(store)
    _forge(store, decision=_allow_record(doc, policy_digest="0" * 64,
                                         allowed=False, effect="DENY",
                                         rule_id=None))
    with pytest.raises(PolicyError, match="tampering signature"):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_a_decision_for_an_unpublished_policy_is_refused(store, tmp_path):
    _forge(store, decision={"allowed": True, "policy_id": "never-published",
                            "version": 1, "policy_digest": "0" * 64,
                            "rule_id": "r", "effect": "ALLOW",
                            "request": _req().to_record(), "reason": "",
                            "at_seq": -1})
    with pytest.raises(PolicyError, match="nothing published"):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_a_decision_with_no_request_cannot_be_re_derived_and_is_refused(
        store, tmp_path):
    """Without the request the verdict is an assertion, not a record.

    Refusing is the fail-closed direction: a decision nobody can re-derive
    reads, to every later tool, exactly like one that was checked.
    """
    doc = _denied_doc(store)
    rec = _allow_record(doc)
    rec.pop("request")
    _forge(store, decision=rec)
    with pytest.raises(PolicyError, match="records no request"):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_a_decision_whose_digest_does_not_match_its_body_is_refused(store,
                                                                    tmp_path):
    doc = _denied_doc(store)
    _forge(store, decision=_allow_record(doc),
           digest_over={"a different": "body"})
    with pytest.raises(PolicyError, match="hashes to"):
        PolicyStore(EventLog(tmp_path / "log.jsonl")).load()


def test_every_honest_decision_still_replays(store, tmp_path):
    """The guard must refuse forgeries, not decisions.

    Both directions in one place, because a check that also refused real
    records would be turned off rather than fixed -- and DENY is the branch
    a test suite most easily leaves out.
    """
    store.publish(_doc(1, rules=(_rule("r", actions=("permitted",)),)),
                  actor="owner")
    allowed = store.decide_and_record("p", _req(action="permitted"),
                                      actor="scheduler")
    denied = store.decide_and_record("p", _req(action="forbidden"),
                                     actor="scheduler")
    assert allowed.allowed and not denied.allowed
    reloaded = PolicyStore(EventLog(tmp_path / "log.jsonl")).load()
    assert reloaded.in_force("p").digest() == store.in_force("p").digest()


def test_a_decision_still_replays_after_a_later_version_is_published(store,
                                                                     tmp_path
                                                                     ):
    """The rules that were in force THEN decide, not the ones written since.

    This is the check most likely to be written against ``in_force`` by
    accident, which would make every historical decision fail the moment a
    policy was updated -- and the fix somebody reached for would be deleting
    the check.
    """
    store.publish(_doc(1, rules=(_rule("r", actions=("permitted",)),)),
                  actor="owner")
    store.decide_and_record("p", _req(action="permitted"), actor="scheduler")
    store.publish(_doc(2, rules=(_rule("r2", effect=Effect.DENY,
                                       actions=("permitted",)),)),
                  actor="owner")
    reloaded = PolicyStore(EventLog(tmp_path / "log.jsonl")).load()
    assert reloaded.in_force("p").version == 2
