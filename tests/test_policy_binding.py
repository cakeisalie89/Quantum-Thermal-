"""A promotion is bound to a policy that was published, by whom, and is
still in force.

D-2026-148. I5 required only that a promotion NAME a policy. The authority
store held no view of the policy subsystem, so a promotion under a policy
nobody published, or one its owner had withdrawn, was admitted and
canonical -- and policies could not be withdrawn or expire at all.

Now:

* a policy's owner (whoever published version 1) may WITHDRAW it, and a
  document may carry ``not_after``; either way it authorizes nothing after;
* a history may DECLARE, once, the principals whose policies bind its
  promotions. From then on a promotion's policy must be published on that
  history by one of them, not withdrawn, and not expired at the promotion --
  decided on the write path, re-decided on replay, and restated by the
  second reader. The governed path declares it at start-up.

Every refusal below has a control that differs only in the reason under
test.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_substrate.authority import Role, State
from agent_substrate.events import EventLog
from agent_substrate.policy import (Effect, PolicyError, PolicyNotInForce,
                                    PolicyStore, UnknownPolicy, document,
                                    rule)
from agent_substrate.reconstruct import reconstruct
from agent_substrate.store import AuthorityStore, StoreError

DIG = "d" * 64


def _doc(pid: str = "promote", version: int = 1, **kw):
    return document(policy_id=pid, version=version, rules=(rule(
        rule_id="r1", effect=Effect.ALLOW, actions=("record.promote",),
        subjects=("*",), roles=("*",), resources=("*",)),), **kw)


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "log.jsonl")


def _verified(s: AuthorityStore, rid: str) -> None:
    s.create(record_id=rid, kind="result", proposer="alice",
             policy_id="promote")
    s.transition(record_id=rid, dst=State.UNDER_REVIEW, actor="bob",
                 role=Role.VERIFIER)
    s.transition(record_id=rid, dst=State.VERIFIED, actor="bob",
                 role=Role.VERIFIER, evidence={"verification_report": DIG})


def _promote(s: AuthorityStore, rid: str, pid: str = "promote"):
    return s.transition(record_id=rid, dst=State.PROMOTED, actor="carol",
                        role=Role.PROMOTER, evidence={"policy_id": pid},
                        policy_id=pid)


@pytest.fixture
def bound(tmp_path):
    """A history whose owner published 'promote' and bound promotions to
    the owner's policies."""
    log = _log(tmp_path)
    PolicyStore(log).load().publish(_doc(), actor="owner")
    s = AuthorityStore(log).load()
    s.bind_policies(actor="owner", publishers=("owner",))
    return log, s


# ---- the policy subsystem: withdrawal and expiry ----------------------------

def test_a_withdrawn_policy_is_not_in_force_from_its_withdrawal_on(tmp_path):
    ps = PolicyStore(_log(tmp_path)).load()
    ps.publish(_doc(), actor="owner")
    before = ps.versions("promote")[0][0]
    ps.withdraw("promote", actor="owner", reason="superseded by review")
    with pytest.raises(PolicyNotInForce, match="withdrawn"):
        ps.in_force("promote")
    assert ps.in_force_at("promote", before).identity == "promote@1"
    with pytest.raises(PolicyNotInForce, match="withdrawn"):
        ps.in_force_at("promote", before + 1)
    reloaded = PolicyStore(_log(tmp_path)).load()
    assert reloaded.withdrawn("promote")[1] == "owner"


def test_only_the_owner_withdraws_and_only_once(tmp_path):
    """Refused BEFORE anything is written: each refusal leaves a log that
    still loads, with the policy as it was. (Found by mutation PB13 and
    PB17: with the write-path check deleted, the record was appended and
    only the replay of it raised -- the same message, and a log nobody
    could load.)"""
    log = _log(tmp_path)
    ps = PolicyStore(log).load()
    ps.publish(_doc(), actor="owner")
    with pytest.raises(PolicyError, match="its owner"):
        ps.withdraw("promote", actor="mallory", reason="mine now")
    assert PolicyStore(log).load().in_force("promote").version == 1
    ps.withdraw("promote", actor="owner", reason="done")
    with pytest.raises(PolicyNotInForce, match="already"):
        ps.withdraw("promote", actor="owner", reason="again")
    with pytest.raises(PolicyError, match="not republished"):
        ps.publish(_doc(version=2), actor="owner")
    reloaded = PolicyStore(log).load()
    assert reloaded.versions("promote")[-1][1] == 1
    assert reloaded.withdrawn("promote")[1] == "owner"


def test_a_forged_withdrawal_or_republication_does_not_load(tmp_path):
    log = _log(tmp_path)
    PolicyStore(log).load().publish(_doc(), actor="owner")
    log.append(actor="mallory", action="policy.withdraw", target="promote",
               payload={"policy_id": "promote", "reason": "forged"})
    with pytest.raises(PolicyError, match="withdrawn by its owner"):
        PolicyStore(log).load()


def test_a_forged_republication_after_withdrawal_does_not_load(tmp_path):
    from agent_substrate.policy import ACT_POLICY_PUBLISH
    log = _log(tmp_path)
    ps = PolicyStore(log).load()
    ps.publish(_doc(), actor="owner")
    ps.withdraw("promote", actor="owner", reason="done")
    doc = _doc(version=2)
    log.append(actor="owner", action=ACT_POLICY_PUBLISH, target="promote",
               payload={"document": doc.body(), "policy_digest": doc.digest()})
    with pytest.raises(PolicyError, match="not republished"):
        PolicyStore(log).load()


def test_an_expired_policy_is_not_in_force(tmp_path):
    ps = PolicyStore(_log(tmp_path)).load()
    ps.publish(_doc(not_after=time.time() - 60), actor="owner")
    with pytest.raises(PolicyNotInForce, match="expired"):
        ps.in_force("promote")
    ps2 = PolicyStore(EventLog(tmp_path / "other.jsonl")).load()
    ps2.publish(_doc(not_after=time.time() + 3600), actor="owner")
    assert ps2.in_force("promote").identity == "promote@1"


def test_a_document_without_expiry_keeps_the_digest_it_always_had():
    """not_after is in the body only when set: every policy published
    before this change still hashes to what its records say."""
    d = _doc()
    assert "not_after" not in d.body()
    assert _doc(not_after=1e10).digest() != d.digest()


def test_a_decision_recorded_under_a_withdrawn_policy_is_refused_on_replay(
        tmp_path):
    from agent_substrate.policy import PolicyRequest
    log = _log(tmp_path)
    ps = PolicyStore(log).load()
    ps.publish(_doc(), actor="owner")
    req = PolicyRequest(action="record.promote", subject="carol",
                        role="PROMOTER", resource="r1")
    good = ps.decide_and_record("promote", req, actor="carol")
    ps.withdraw("promote", actor="owner", reason="stop")
    with pytest.raises(PolicyNotInForce):
        ps.decide_and_record("promote", req, actor="carol")
    # The same decision, appended raw after the withdrawal: a forgery.
    rec = {k: v for k, v in good.to_record().items()}
    log.append(actor="carol", action="policy.decision", target="r1",
               payload={"decision": rec, "decision_digest": good.digest()})
    with pytest.raises(PolicyError, match="nothing published by this "
                                          "position"):
        PolicyStore(log).load()


@pytest.mark.parametrize("forge, why", [
    ("unpublished", "which nothing published"),
    ("withdrawn", "withdrawn at seq"),
    ("digest", "other than the version in force"),
])
def test_the_second_reader_restates_the_policy_a_decision_cites(
        tmp_path, forge, why):
    """D-2026-168: the second reader held decisions without asking whether
    the policy they cite was in force; it restates that now."""
    from agent_substrate.policy import PolicyRequest
    from agent_substrate.reconstruct import reconstruct_subsystems
    log = _log(tmp_path)
    ps = PolicyStore(log).load()
    ps.publish(_doc(), actor="owner")
    req = PolicyRequest(action="record.promote", subject="carol",
                        role="PROMOTER", resource="r1")
    good = ps.decide_and_record("promote", req, actor="carol")
    assert reconstruct_subsystems(log).anomalies == []      # control
    rec = good.to_record()
    if forge == "unpublished":
        rec["policy_id"] = "never-published"
    elif forge == "withdrawn":
        ps.withdraw("promote", actor="owner", reason="stop")
    else:
        rec["policy_digest"] = "e" * 64
    log.append(actor="carol", action="policy.decision", target="r1",
               payload={"decision": rec, "decision_digest": good.digest()})
    found = reconstruct_subsystems(log).anomalies
    assert any(why in a for a in found), found


# ---- the binding: write path -------------------------------------------------

def test_control_a_promotion_under_the_owners_policy_in_force(bound):
    log, s = bound
    _verified(s, "r1")
    assert _promote(s, "r1").state is State.PROMOTED
    assert reconstruct(log).unauthorized == []


@pytest.mark.parametrize("pid, why", [
    ("never-published", "was not published on this history"),
])
def test_a_promotion_under_an_unpublished_policy_is_refused(bound, pid, why):
    _, s = bound
    _verified(s, "r1")
    with pytest.raises(StoreError, match=why):
        _promote(s, "r1", pid)


def test_a_promotion_under_a_policy_its_promoter_published_is_refused(bound):
    """Publishing is open; publishing does not make one a policy
    authority. The binding names who that is."""
    log, s = bound
    PolicyStore(log).load().publish(_doc("carols-own"), actor="carol")
    _verified(s, "r1")
    with pytest.raises(StoreError, match="not a policy authority"):
        _promote(s, "r1", "carols-own")


def test_a_promotion_after_the_policy_is_withdrawn_is_refused(bound):
    log, s = bound
    _verified(s, "r0")
    _promote(s, "r0")
    _verified(s, "r1")
    PolicyStore(log).load().withdraw("promote", actor="owner",
                                     reason="revoked")
    with pytest.raises(StoreError, match="withdrawn at seq"):
        _promote(s, "r1")
    # Not retroactive: what was promoted while the policy stood, stands.
    again = AuthorityStore(log).load()
    assert again.get("r0").state is State.PROMOTED
    assert again.get("r1").state is State.VERIFIED


def test_a_promotion_after_the_policy_expired_is_refused(tmp_path):
    log = _log(tmp_path)
    PolicyStore(log).load().publish(_doc(not_after=time.time() + 0.2),
                                    actor="owner")
    s = AuthorityStore(log).load()
    s.bind_policies(actor="owner", publishers=("owner",))
    _verified(s, "r1")
    time.sleep(0.3)
    with pytest.raises(StoreError, match="expired"):
        _promote(s, "r1")


def test_the_cited_policy_is_held_too(bound):
    """The edge requires evidence["policy_id"]; a promotion naming a good
    policy while citing an unpublished one is refused, not split."""
    _, s = bound
    _verified(s, "r1")
    with pytest.raises(StoreError, match="'other' was not published"):
        s.transition(record_id="r1", dst=State.PROMOTED, actor="carol",
                     role=Role.PROMOTER, evidence={"policy_id": "other"},
                     policy_id="promote")


def test_the_binding_is_declared_once(bound):
    _, s = bound
    with pytest.raises(StoreError, match="already declared"):
        s.bind_policies(actor="mallory", publishers=("mallory",))


def test_an_unbound_history_is_unchanged(tmp_path):
    """The boundary, stated: a history that declared no binding promotes
    under any named policy, as before. The governed path always declares
    one (test_the_governed_path_declares_the_binding)."""
    s = AuthorityStore(_log(tmp_path)).load()
    _verified(s, "r1")
    assert _promote(s, "r1", "never-published").state is State.PROMOTED
    assert s.policy_binding is None


# ---- the binding: replay, by both readers -------------------------------------

def _forge_promotion(log: EventLog, pid: str) -> None:
    log.append(actor="carol", action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "VERIFIED",
                        "dst": "PROMOTED", "role": "PROMOTER",
                        "evidence": {"policy_id": pid}, "policy_id": pid,
                        "stale_reason": None, "edge_reason": "",
                        "idempotency_key": None})


@pytest.mark.parametrize("prepare, pid, why", [
    (lambda log: None, "never-published", "not published"),
    (lambda log: PolicyStore(log).load().publish(_doc("own"),
                                                 actor="carol"),
     "own", "not a policy authority"),
    (lambda log: PolicyStore(log).load().withdraw(
        "promote", actor="owner", reason="revoked"), "promote", "withdrawn"),
])
def test_a_forged_promotion_is_refused_by_both_readers(bound, prepare, pid,
                                                       why):
    log, s = bound
    _verified(s, "r1")
    prepare(log)
    _forge_promotion(log, pid)
    with pytest.raises(StoreError, match=why):
        AuthorityStore(log).load()
    recon = reconstruct(log)
    assert any("r1" in u and "PROMOTED" in u and why in u
               for u in recon.unauthorized), recon.unauthorized
    assert recon.records["r1"]["state"] == "VERIFIED"


def test_control_the_same_forged_line_under_the_policy_in_force(bound):
    """The line above, naming the owner's policy in force: both readers
    fold it -- so the refusals are for the policy, not for the line."""
    log, s = bound
    _verified(s, "r1")
    _forge_promotion(log, "promote")
    assert AuthorityStore(log).load().get("r1").state is State.PROMOTED
    assert reconstruct(log).unauthorized == []


def test_a_second_binding_does_not_load_and_the_reader_flags_it(bound):
    log, _ = bound
    log.append(actor="mallory", action="authority.policy_binding",
               target="authority.promotion",
               payload={"publishers": ["mallory"]})
    with pytest.raises(StoreError, match="declared at seq"):
        AuthorityStore(log).load()
    assert any("second policy binding" in u
               for u in reconstruct(log).unauthorized)


def test_the_binding_survives_a_checkpoint(bound, tmp_path):
    from agent_substrate.checkpoint import CheckpointStore
    from agent_substrate.evidence import EvidenceStore
    log, s = bound
    _verified(s, "r1")
    blobs = EvidenceStore(tmp_path / "blobs")
    cps = CheckpointStore(tmp_path / "cps")
    s.checkpoint(cps, blobs=blobs)
    PolicyStore(log).load().withdraw("promote", actor="owner",
                                     reason="after the checkpoint")
    restored = AuthorityStore.load_from(log, cps, blobs=blobs,
                                        require_checkpoint=True)
    assert restored.policy_binding == s.policy_binding
    with pytest.raises(StoreError, match="withdrawn"):
        _promote(restored, "r1")


def test_unpublished_is_refused_even_before_any_policy_exists(tmp_path):
    log = _log(tmp_path)
    s = AuthorityStore(log).load()
    s.bind_policies(actor="owner", publishers=("owner",))
    _verified(s, "r1")
    with pytest.raises(StoreError, match="not published"):
        _promote(s, "r1")
    with pytest.raises(UnknownPolicy):
        PolicyStore(log).load().in_force("promote")


# ---- the subsystem reader restates the withdrawal rules ----------------------

def test_the_subsystem_reader_flags_what_the_policy_store_refuses(tmp_path):
    from agent_substrate.reconstruct import reconstruct_subsystems
    log = _log(tmp_path)
    PolicyStore(log).load().publish(_doc(), actor="owner")
    assert reconstruct_subsystems(log).anomalies == []
    for actor, pid in (("mallory", "promote"), ("owner", "nothing")):
        log.append(actor=actor, action="policy.withdraw", target=pid,
                   payload={"policy_id": pid, "reason": "x"})
    found = reconstruct_subsystems(log).anomalies
    assert any("'mallory' withdraws 'promote'" in a for a in found), found
    assert any("'nothing', which nothing published" in a for a in found)


def test_the_auditor_says_a_decision_postdates_its_policys_withdrawal(
        tmp_path):
    from agent_substrate.audit import AuditIndex
    from agent_substrate.policy import PolicyRequest
    log = _log(tmp_path)
    ps = PolicyStore(log).load()
    ps.publish(_doc(), actor="owner")
    good = ps.decide_and_record(
        "promote", PolicyRequest(action="record.promote", subject="carol",
                                 role="PROMOTER", resource="r1"),
        actor="carol")
    assert AuditIndex.from_log(log).explain_decision(good.at_seq).gaps == ()
    ps.withdraw("promote", actor="owner", reason="stop")
    log.append(actor="carol", action="policy.decision", target="r1",
               payload={"decision": good.to_record(),
                        "decision_digest": good.digest()})
    late = log.read_verified()[0].head_seq
    gaps = AuditIndex.from_log(log).explain_decision(late).gaps
    assert any("was withdrawn by 'owner'" in g for g in gaps), gaps
