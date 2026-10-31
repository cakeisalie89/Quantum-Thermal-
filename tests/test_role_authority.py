"""Who may hold a role: a registry rooted outside the log (AUD-011, AUD-017).

Each attack below was open before the role registry existed. An actor took a
role by naming it in a request, or by appending a record that named it, or
by registering an agent that claimed it, and every reader agreed.

Each attack is now refused live, before its record exists, and again on
replay by the reader that enforces and by the independent reader. Each has
a control beside it: the same act by a principal the registry grants
passes. A refusal that also stopped the grantee would be measuring nothing.

The two readers are compared by refusal KIND, the code
:mod:`agent_substrate.roles` defines. Their wording is their own.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import roles as R  # noqa: E402
from agent_substrate.authority import Role, State, TransitionError  # noqa: E402
from agent_substrate.checkpoint import CheckpointStore  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.netauth import (EgressGrant, NetworkAuthority,  # noqa: E402
                                     NetworkError)
from agent_substrate.policy import (Effect, PolicyError,  # noqa: E402
                                    PolicyStore, document, rule)
from agent_substrate.reconstruct import (reconstruct,  # noqa: E402
                                         reconstruct_subsystems,
                                         reconstruct_tasks)
from agent_substrate.secrets import SecretError, SecretGrant, SecretStore  # noqa: E402
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _g(gid, principal, role, scope, **kw) -> R.RoleGrant:
    return R.RoleGrant(gid, principal, role, tuple(scope), **kw)


#: The registry most tests read under. Nobody named here is mallory.
BASE = (
    _g("g-alice", "alice", R.PROPOSER, ["kind:*"]),
    _g("g-bob", "bob", R.VERIFIER, ["kind:note"]),
    _g("g-carol", "carol", R.PROMOTER, ["kind:note"]),
    _g("g-owner", "owner", R.POLICY_OWNER, ["policy:p1"]),
    _g("g-sam", "sam", R.SECRET_ISSUER, ["secret:db"]),
    _g("g-eve", "eve", R.EGRESS_ISSUER, ["egress:api.example.org"]),
    _g("g-vic", "vic", R.VERIFIER, ["kind:*"], may_delegate=True),
)


def _reg(*extra, base=BASE) -> R.RoleRegistry:
    return R.RoleRegistry(list(base) + list(extra))


def _create(log, rid="r1", kind="note", actor="alice"):
    log.append(actor=actor, action="record.create", target=rid,
               payload={"record_id": rid, "kind": kind, "proposer": actor,
                        "state": "PROPOSED", "evidence": {"claim": _h(rid)},
                        "depends_on": [], "policy_id": None,
                        "idempotency_key": None})


def _move(log, rid, src, dst, actor, role, evidence=None, policy_id=None):
    log.append(actor=actor, action="record.transition", target=rid,
               payload={"record_id": rid, "src": src, "dst": dst,
                        "role": role, "evidence": evidence or {},
                        "policy_id": policy_id, "stale_reason": None,
                        "edge_reason": "", "idempotency_key": None})


def _walk(log, rid, *, verifier, promoter):
    """A record taken from PROPOSED to PROMOTED by raw records, the way a
    writer able to append would take it."""
    _move(log, rid, "PROPOSED", "UNDER_REVIEW", verifier, "VERIFIER")
    _move(log, rid, "UNDER_REVIEW", "VERIFIED", verifier, "VERIFIER",
          {"verification_report": _h(f"{rid}-report")})
    _move(log, rid, "VERIFIED", "PROMOTED", promoter, "PROMOTER",
          {"policy_id": "p1"}, policy_id="p1")


def _kind_of(refusal_text: str) -> str:
    found = [k for k in R.REFUSALS if f"{k}:" in refusal_text]
    assert len(found) == 1, refusal_text
    return found[0]


# ---- the root ----------------------------------------------------------------
def test_a_registry_is_read_only_at_the_digest_the_deployment_pinned():
    doc = _reg().to_document()
    pinned = pr.registry_digest(doc)
    assert R.RoleRegistry.from_document(doc, pinned_digest=pinned).digest \
        == pinned
    doc["grants"][0]["role"] = R.PROMOTER          # one field changed
    with pytest.raises(R.RoleError, match="not the one the deployment pinned"):
        R.RoleRegistry.from_document(doc, pinned_digest=pinned)


@pytest.mark.parametrize("change, match", [
    ({"role": "ADMIN"}, "is not one of"),
    ({"scope": ["anything"]}, "scope entry"),
    ({"scope": []}, "non-empty list"),
    ({"role": "POLICY_OWNER", "scope": ["policy:p"], "may_delegate": True},
     "the root's to give"),
    ({"valid_from_seq": 5, "valid_until_seq": 4}, "ends before it begins"),
    ({"revoked_at_seq": 3}, "names both where and why"),
    ({"valid_from_seq": -1}, "valid_from_seq is a seq"),
    ({"may_delegate": "yes"}, "true or false"),
])
def test_a_grant_the_registry_cannot_read_is_refused(change, match):
    entry = {**BASE[0].to_entry(), **change}
    doc = {"schema": R.SCHEMA, "registry_version": 1, "key_registry": None,
           "grants": [entry]}
    with pytest.raises(R.RoleError, match=match):
        R.RoleRegistry.from_document(doc)


def test_a_grant_named_twice_is_refused():
    with pytest.raises(R.RoleError, match="registered twice"):
        R.RoleRegistry([BASE[0], BASE[0]])


def test_a_registry_bound_to_a_key_registry_names_only_its_principals():
    """A role binds an authenticated identity: a grant to a name with no key
    is refused before any grant is believed."""
    with pytest.raises(R.RoleError, match="holds no key"):
        R.RoleRegistry(BASE, principals={"alice", "bob"})
    doc = _reg().to_document()
    doc["key_registry"] = "a" * 64
    with pytest.raises(R.RoleError, match="another key registry"):
        R.RoleRegistry.from_document(doc, key_registry="b" * 64)


def test_no_production_registry_is_provisioned_and_none_is_invented():
    assert R.PRODUCTION_ROLES is None
    assert R.PRODUCTION_ROLE_REGISTRY_DIGEST is None
    with pytest.raises(R.RoleNotConfigured, match="owner decision"):
        R.production_role_registry()


# ---- the authority store: verifying and promoting (AUD-011) ------------------
def test_an_actor_without_a_grant_cannot_verify_or_promote(tmp_path):
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"),
                           roles=_reg()).load()
    store.create(record_id="r1", kind="note", proposer="alice",
                 evidence={"claim": _h("c")})
    with pytest.raises(TransitionError) as exc:
        store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                         actor="mallory", role=Role.VERIFIER)
    assert _kind_of(str(exc.value)) == R.NOT_GRANTED
    store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                     role=Role.VERIFIER)
    store.transition(record_id="r1", dst=State.VERIFIED, actor="bob",
                     role=Role.VERIFIER,
                     evidence={"verification_report": _h("rep")})
    # The verifier holds VERIFIER and not PROMOTER: one identity does not
    # take every role the edges allow.
    with pytest.raises(TransitionError) as exc:
        store.transition(record_id="r1", dst=State.PROMOTED, actor="bob",
                         role=Role.PROMOTER, evidence={"policy_id": "p1"},
                         policy_id="p1")
    assert _kind_of(str(exc.value)) == R.NOT_GRANTED
    size = store.log.path.stat().st_size
    # control: the promoter the registry names
    store.transition(record_id="r1", dst=State.PROMOTED, actor="carol",
                     role=Role.PROMOTER, evidence={"policy_id": "p1"},
                     policy_id="p1")
    assert store.log.path.stat().st_size > size
    again = AuthorityStore(EventLog(store.log.path), roles=_reg()).load()
    assert again.get("r1").state is State.PROMOTED


def test_a_refused_role_leaves_the_log_as_it_was(tmp_path):
    """Refused before the record exists, as every decided write is
    (AUD-014's class): the history stays one every reader can load."""
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"),
                           roles=_reg()).load()
    before = store.log.path.read_bytes() if store.log.path.exists() else b""
    with pytest.raises(StoreError, match=R.NOT_GRANTED):
        store.create(record_id="r1", kind="note", proposer="mallory")
    after = store.log.path.read_bytes() if store.log.path.exists() else b""
    assert after == before


def test_a_forged_walk_to_promoted_is_refused_by_both_readers(tmp_path):
    """The AUD-011 reproduction. Mallory, registered nowhere, takes alice's
    claim through review, verification and promotion by appending three
    records. Without a registry both readers accept it: the attack is real.
    With one, both refuse it, and give the same kind."""
    log = EventLog(tmp_path / "log.jsonl")
    _create(log, "r1")
    _walk(log, "r1", verifier="mallory", promoter="mallory")
    # the baseline: neither reader asks who holds a role
    assert AuthorityStore(EventLog(log.path)).load().get("r1").state \
        is State.PROMOTED
    assert reconstruct(log).records["r1"]["state"] == "PROMOTED"
    # under the registry
    with pytest.raises(StoreError) as exc:
        AuthorityStore(EventLog(log.path), roles=_reg()).load()
    assert _kind_of(str(exc.value)) == R.NOT_GRANTED
    rec = reconstruct(log, roles=_reg())
    assert rec.records["r1"]["state"] == "PROPOSED"
    assert rec.canonical_ids() == ()
    assert _kind_of(rec.unauthorized[0]) == R.NOT_GRANTED


def test_a_forged_create_is_refused_by_both_readers(tmp_path):
    """A record proposed under a name the registry grants nothing: neither
    reader takes it, so nothing can later be verified on it."""
    log = EventLog(tmp_path / "log.jsonl")
    _create(log, "r1", actor="mallory")
    assert AuthorityStore(EventLog(log.path)).load().get("r1")    # baseline
    with pytest.raises(StoreError) as exc:
        AuthorityStore(EventLog(log.path), roles=_reg()).load()
    assert _kind_of(str(exc.value)) == R.NOT_GRANTED
    rec = reconstruct(log, roles=_reg())
    assert "r1" not in rec.records
    assert [_kind_of(u) for u in rec.unauthorized] == [R.NOT_GRANTED]


def test_the_same_walk_by_the_grantees_is_accepted_by_both(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _create(log, "r1")
    _walk(log, "r1", verifier="bob", promoter="carol")
    assert AuthorityStore(EventLog(log.path), roles=_reg()).load() \
        .get("r1").state is State.PROMOTED
    rec = reconstruct(log, roles=_reg())
    assert rec.unauthorized == [] and rec.canonical_ids() == ("r1",)


def _history_for(case: str, path: Path) -> tuple:
    """One forged act per case, and the registry it is judged under."""
    log = EventLog(path)
    if case == R.NOT_GRANTED:
        _create(log, "r1")
        _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "mallory", "VERIFIER")
        return log, _reg()
    if case == R.OUT_OF_SCOPE:
        _create(log, "r1", kind="secret_claim")
        _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "bob", "VERIFIER")
        return log, _reg()
    if case == R.EXPIRED:
        _create(log, "r1")                                       # seq 0
        _create(log, "r2")                                       # seq 1
        _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "dan", "VERIFIER")
        return log, _reg(_g("g-dan", "dan", R.VERIFIER, ["kind:note"],
                            valid_until_seq=1))
    if case == R.NOT_YET_VALID:
        _create(log, "r1")
        _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "dan", "VERIFIER")
        return log, _reg(_g("g-dan", "dan", R.VERIFIER, ["kind:note"],
                            valid_from_seq=5))
    assert case == R.REVOKED
    _create(log, "r1")
    _create(log, "r2")
    _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "dan", "VERIFIER")
    return log, _reg(_g("g-dan", "dan", R.VERIFIER, ["kind:note"],
                        revoked_at_seq=2, revocation_reason="left"))


@pytest.mark.parametrize("case", [R.NOT_GRANTED, R.OUT_OF_SCOPE, R.EXPIRED,
                                  R.NOT_YET_VALID, R.REVOKED])
def test_both_readers_refuse_with_the_same_kind(case, tmp_path):
    log, reg = _history_for(case, tmp_path / "log.jsonl")
    with pytest.raises(StoreError) as exc:
        AuthorityStore(EventLog(log.path), roles=reg).load()
    rec = reconstruct(log, roles=reg)
    assert _kind_of(str(exc.value)) == case
    assert [_kind_of(u) for u in rec.unauthorized] == [case]
    assert rec.records["r1"]["state"] == "PROPOSED"


def test_a_revocation_stands_from_its_position_and_not_before(tmp_path):
    """Revoked AFTER bob's acts: they stand. Revoked BEFORE one of them: a
    declared reach-back, and every reader given that registry refuses what
    follows it, by name."""
    log = EventLog(tmp_path / "log.jsonl")
    _create(log, "r1")                                           # seq 0
    _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "bob", "VERIFIER")  # 1
    _move(log, "r1", "UNDER_REVIEW", "VERIFIED", "bob", "VERIFIER",
          {"verification_report": _h("rep")})                    # seq 2
    bob = BASE[1]

    def under(at):
        base = [g for g in BASE if g.grant_id != "g-bob"]
        return R.RoleRegistry(base + [R.RoleGrant(
            bob.grant_id, bob.principal, bob.role, bob.scope,
            revoked_at_seq=at, revocation_reason="rotated")])

    assert AuthorityStore(EventLog(log.path), roles=under(3)).load() \
        .get("r1").state is State.VERIFIED
    with pytest.raises(StoreError, match="revoked at seq 2"):
        AuthorityStore(EventLog(log.path), roles=under(2)).load()
    assert [_kind_of(u) for u in reconstruct(log, roles=under(2))
            .unauthorized] == [R.REVOKED]


def test_system_is_granted_to_nobody(tmp_path):
    """An automatic transition names SYSTEM. Under a registry it is made
    by a principal holding VERIFIER or PROMOTER over the kind, and a name
    that holds neither moves nothing, not even to STALE."""
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"),
                           roles=_reg()).load()
    store.create(record_id="r1", kind="note", proposer="alice")
    store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                     role=Role.VERIFIER)
    store.transition(record_id="r1", dst=State.VERIFIED, actor="bob",
                     role=Role.VERIFIER,
                     evidence={"verification_report": _h("rep")})
    stale = {"invalidated_by": _h("dep")}
    with pytest.raises(TransitionError, match=R.NOT_GRANTED):
        store.transition(record_id="r1", dst=State.STALE, actor="SYSTEM",
                         role=Role.SYSTEM, evidence=stale)
    store.transition(record_id="r1", dst=State.STALE, actor="carol",
                     role=Role.SYSTEM, evidence=stale)
    assert store.get("r1").state is State.STALE


# ---- delegation ----------------------------------------------------------------
def test_a_holder_delegates_a_narrower_grant_and_it_holds_within_it(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ledger = R.RoleDelegations(log, _reg()).load()
    ledger.delegate(actor="vic", delegation_id="d1", parent="g-vic",
                    principal="walt", scope=["kind:note"])
    store = AuthorityStore(EventLog(log.path), roles=_reg()).load()
    store.create(record_id="r1", kind="note", proposer="alice")
    store.create(record_id="r2", kind="dataset", proposer="alice")
    store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="walt",
                     role=Role.VERIFIER)
    with pytest.raises(TransitionError) as exc:
        store.transition(record_id="r2", dst=State.UNDER_REVIEW,
                         actor="walt", role=Role.VERIFIER)
    assert _kind_of(str(exc.value)) == R.OUT_OF_SCOPE
    assert reconstruct(log, roles=_reg()).unauthorized == []


@pytest.mark.parametrize("kw, match", [
    (dict(actor="bob", parent="g-bob"), "may not be delegated"),
    (dict(actor="mallory"), "only a grant's holder delegates it"),
    (dict(principal="vic"), "to itself"),
    (dict(scope=["*"]), "wider than grant"),
    (dict(parent="d0"), "a delegation does not delegate"),
    (dict(parent="g-owner", actor="owner"), "may not be delegated"),
])
def test_a_delegation_outside_the_rules_is_refused_and_not_written(
        kw, match, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ledger = R.RoleDelegations(log, _reg()).load()
    ledger.delegate(actor="vic", delegation_id="d0", parent="g-vic",
                    principal="walt", scope=["kind:note"])
    before = log.path.read_bytes()
    args = dict(actor="vic", delegation_id="d1", parent="g-vic",
                principal="xena", scope=["kind:note"])
    args.update(kw)
    with pytest.raises(R.RoleError, match=match):
        ledger.delegate(**args)
    assert log.path.read_bytes() == before


def test_a_delegation_may_not_outlast_its_parent(tmp_path):
    reg = _reg(_g("g-uma", "uma", R.VERIFIER, ["kind:note"],
                  valid_until_seq=10, may_delegate=True))
    ledger = R.RoleDelegations(EventLog(tmp_path / "log.jsonl"), reg).load()
    for until in (None, 11):
        with pytest.raises(R.RoleError, match="outlast"):
            ledger.delegate(actor="uma", delegation_id="d1", parent="g-uma",
                            principal="walt", scope=["kind:note"],
                            valid_until_seq=until)
    ledger.delegate(actor="uma", delegation_id="d1", parent="g-uma",
                    principal="walt", scope=["kind:note"], valid_until_seq=10)


def test_a_forged_delegation_is_refused_on_replay_by_both_readers(tmp_path):
    """Mallory appends a delegation of vic's grant to herself: no reader
    takes it, and the verification she makes on it is not applied."""
    log = EventLog(tmp_path / "log.jsonl")
    _create(log, "r1")
    log.append(actor="mallory", action=R.ACT_DELEGATE, target="d1",
               payload={"delegation_id": "d1", "parent": "g-vic",
                        "role": R.VERIFIER, "principal": "mallory",
                        "scope": ["kind:note"], "valid_until_seq": None})
    _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "mallory", "VERIFIER")
    with pytest.raises(StoreError, match="only a grant's holder"):
        AuthorityStore(EventLog(log.path), roles=_reg()).load()
    with pytest.raises(R.RoleError, match="only a grant's holder"):
        R.RoleDelegations(EventLog(log.path), _reg()).load()
    rec = reconstruct(log, roles=_reg())
    assert rec.records["r1"]["state"] == "PROPOSED"
    assert len(rec.unauthorized) == 2
    assert "held by 'vic'" in rec.unauthorized[0]
    assert _kind_of(rec.unauthorized[1]) == R.NOT_GRANTED


def test_a_revoked_delegation_authorizes_nothing_after_it(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ledger = R.RoleDelegations(log, _reg()).load()
    ledger.delegate(actor="vic", delegation_id="d1", parent="g-vic",
                    principal="walt", scope=["kind:note"])
    store = AuthorityStore(EventLog(log.path), roles=_reg()).load()
    store.create(record_id="r1", kind="note", proposer="alice")
    store.create(record_id="r2", kind="note", proposer="alice")
    store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="walt",
                     role=Role.VERIFIER)
    with pytest.raises(R.RoleError, match="withdrawn by whoever made it"):
        ledger.revoke(actor="walt", delegation_id="d1", reason="mine now")
    ledger.revoke(actor="vic", delegation_id="d1", reason="rotation")
    with pytest.raises(TransitionError) as exc:
        store.transition(record_id="r2", dst=State.UNDER_REVIEW,
                         actor="walt", role=Role.VERIFIER)
    assert _kind_of(str(exc.value)) == R.REVOKED
    # what walt did before the revocation stands, for every reader
    again = AuthorityStore(EventLog(log.path), roles=_reg()).load()
    assert again.get("r1").state is State.UNDER_REVIEW
    assert reconstruct(log, roles=_reg()).unauthorized == []


def test_a_delegation_lapses_with_its_parent(tmp_path):
    """The registry revokes vic's grant: walt's copy of it goes too, from
    the same position, though nothing in the log revoked it."""
    log = EventLog(tmp_path / "log.jsonl")
    R.RoleDelegations(log, _reg()).load().delegate(
        actor="vic", delegation_id="d1", parent="g-vic", principal="walt",
        scope=["kind:note"])                                     # seq 0
    _create(log, "r1")                                           # seq 1
    _move(log, "r1", "PROPOSED", "UNDER_REVIEW", "walt", "VERIFIER")  # 2
    vic = BASE[-1]
    revoked = R.RoleRegistry(list(BASE[:-1]) + [R.RoleGrant(
        vic.grant_id, vic.principal, vic.role, vic.scope,
        revoked_at_seq=2, revocation_reason="left", may_delegate=True)])
    with pytest.raises(StoreError) as exc:
        AuthorityStore(EventLog(log.path), roles=revoked).load()
    assert _kind_of(str(exc.value)) == R.REVOKED
    assert "stands on" in str(exc.value)
    assert [_kind_of(u) for u in reconstruct(log, roles=revoked)
            .unauthorized] == [R.REVOKED]


def test_a_view_folded_past_a_position_answers_as_one_folded_to_it(tmp_path):
    """The governed runner asks a view caught up to the head about earlier
    positions; it must give the answer a view folded exactly there gives."""
    log = EventLog(tmp_path / "log.jsonl")
    ledger = R.RoleDelegations(log, _reg()).load()
    for i in range(3):
        _create(log, f"pad{i}")
    ledger.delegate(actor="vic", delegation_id="d1", parent="g-vic",
                    principal="walt", scope=["kind:note"])       # seq 3
    _create(log, "pad3")
    ledger.revoke(actor="vic", delegation_id="d1", reason="done")  # seq 5
    _create(log, "pad4")
    events = list(EventLog(log.path).read())
    head = R.RoleView(_reg())
    for ev in events:
        head.fold(ev)
    # Every position an act could stand at: not one a role record holds.
    acts = [ev.seq for ev in events if ev.action not in R.ACTIONS]
    for seq in acts + [len(events)]:
        upto = R.RoleView(_reg())
        for ev in events[:seq]:
            upto.fold(ev)
        for who in ("walt", "vic", "mallory"):
            assert head.refusal(who, R.VERIFIER, "kind:note", seq) == \
                upto.refusal(who, R.VERIFIER, "kind:note", seq), (who, seq)


# ---- recovery: no authority carried across registries --------------------------
def test_a_checkpoint_folded_under_another_registry_is_not_restored(tmp_path):
    """A snapshot's records were decided under its store's grants. Restored
    by a reader holding other grants, or none, they would stand unexamined:
    the snapshot is refused and the history replayed under this reader's."""
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "blobs")
    cps = CheckpointStore(tmp_path / "checkpoints")
    plain = AuthorityStore(log).load()
    _create(log, "r1")
    _walk(log, "r1", verifier="mallory", promoter="mallory")
    plain.catch_up()
    plain.checkpoint(cps, blobs=ev)
    # a reader enforcing a registry does not inherit the unexamined records
    with pytest.raises(StoreError, match=R.NOT_GRANTED):
        AuthorityStore.load_from(EventLog(log.path), cps, blobs=ev,
                                 roles=_reg())
    store, report = AuthorityStore.recover(EventLog(tmp_path / "log2.jsonl"),
                                           CheckpointStore(tmp_path / "c2"),
                                           blobs=ev, roles=_reg())
    assert report["verdict"] == "EMPTY"
    # and a snapshot made under one registry is not restored under another
    log3 = EventLog(tmp_path / "log3.jsonl")
    cps3 = CheckpointStore(tmp_path / "c3")
    s3 = AuthorityStore(log3, roles=_reg()).load()
    s3.create(record_id="r1", kind="note", proposer="alice")
    s3.checkpoint(cps3, blobs=ev)
    same = AuthorityStore.load_from(EventLog(log3.path), cps3, blobs=ev,
                                    roles=_reg())
    assert same.checkpoint_refusal is None
    assert not same.loaded_prefix_verified
    other = AuthorityStore.load_from(
        EventLog(log3.path), cps3, blobs=ev,
        roles=_reg(_g("g-x", "xena", R.VERIFIER, ["kind:note"])))
    assert "role registry" in other.checkpoint_refusal
    assert other.loaded_prefix_verified
    bare = AuthorityStore.load_from(EventLog(log3.path), cps3, blobs=ev)
    assert "role registry" in bare.checkpoint_refusal


# ---- policy ownership, secret and egress issuance (AUD-011, AUD-017) -----------
def _doc(pid="p1", version=1, **kw):
    return document(policy_id=pid, version=version, rules=(rule(
        rule_id="r1", effect=Effect.ALLOW, actions=("record.promote",),
        subjects=("*",), roles=("*",), resources=("*",)),), **kw)


def test_only_a_policy_owner_publishes_and_a_forgery_does_not_load(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    store = PolicyStore(log, roles=_reg()).load()
    with pytest.raises(PolicyError, match=R.NOT_GRANTED):
        store.publish(_doc(), actor="mallory")
    with pytest.raises(PolicyError, match=R.OUT_OF_SCOPE):
        store.publish(_doc("p2"), actor="owner")
    store.publish(_doc(), actor="owner")                 # control
    forged = _doc("p2")
    log.append(actor="mallory", action="policy.publish", target="p2",
               payload={"document": forged.body(),
                        "policy_digest": forged.digest()})
    with pytest.raises(PolicyError, match=R.NOT_GRANTED):
        PolicyStore(EventLog(log.path), roles=_reg()).load()
    with pytest.raises(StoreError, match=R.NOT_GRANTED):
        AuthorityStore(EventLog(log.path), roles=_reg()).load()
    sub = reconstruct_subsystems(log, roles=_reg())
    assert "p2" not in sub.policies and "p1" in sub.policies
    assert [_kind_of(a) for a in sub.anomalies] == [R.NOT_GRANTED]
    # and the authority reader does not count it as a policy a promotion
    # could stand on
    assert [_kind_of(u) for u in reconstruct(log, roles=_reg())
            .unauthorized] == [R.NOT_GRANTED]
    assert reconstruct(log).unauthorized == []                    # baseline
    assert PolicyStore(EventLog(log.path)).load().versions("p2")  # baseline


def _secret_grant(gid="sg1", secret_id="db") -> SecretGrant:
    return SecretGrant(grant_id=gid, subject="worker", task_id="t1",
                       tool_id="tool", secret_id=secret_id,
                       purposes=("read",))


def test_only_a_secret_issuer_grants_a_secret(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    store = SecretStore(log, roles=_reg()).load()
    store.register("db", "s3cret-value")
    store.register("ops", "other-value")
    with pytest.raises(SecretError, match=R.NOT_GRANTED):
        store.issue(_secret_grant(), actor="mallory")
    with pytest.raises(SecretError, match=R.OUT_OF_SCOPE):
        store.issue(_secret_grant("sg2", "ops"), actor="sam")
    store.issue(_secret_grant(), actor="sam")            # control
    forged = _secret_grant("sg3", "ops")
    from dataclasses import replace
    forged = replace(forged, issued_seq=len(list(log.read())))
    log.append(actor="mallory", action="secret.grant", target="t1",
               payload={"grant": forged.body(),
                        "grant_digest": forged.digest()})
    with pytest.raises(SecretError, match=R.NOT_GRANTED):
        SecretStore(EventLog(log.path), roles=_reg()).load()
    SecretStore(EventLog(log.path)).load()               # baseline: it loads
    sub = reconstruct_subsystems(log, roles=_reg())
    assert set(sub.secret_grants) == {"sg1"}
    assert [_kind_of(a) for a in sub.anomalies] == [R.NOT_GRANTED]
    with pytest.raises(SecretError, match="positions in a log"):
        SecretStore(None, roles=_reg())


def _egress(gid="eg1", hosts=("api.example.org",)) -> EgressGrant:
    return EgressGrant(grant_id=gid, subject="worker", task_id="t1",
                       tool_id="tool", schemes=("https",), hosts=hosts,
                       ports=(443,), methods=("GET",),
                       allow_unpinned_addresses=True)


def test_only_an_egress_issuer_grants_egress_and_only_to_its_hosts(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    auth = NetworkAuthority(log, roles=_reg()).load()
    with pytest.raises(NetworkError, match=R.NOT_GRANTED):
        auth.issue(_egress(), actor="mallory")
    with pytest.raises(NetworkError, match=R.OUT_OF_SCOPE):
        auth.issue(_egress("eg2", ("api.example.org", "evil.example.com")),
                   actor="eve")
    with pytest.raises(NetworkError, match="no hosts permits nothing"):
        auth.issue(_egress("eg3", ()), actor="eve")
    auth.issue(_egress(), actor="eve")                   # control
    from dataclasses import replace
    forged = replace(_egress("eg4", ("evil.example.com",)),
                     issued_seq=len(list(log.read())))
    log.append(actor="eve", action="network.grant", target="t1",
               payload={"grant": forged.body(),
                        "grant_digest": forged.digest(),
                        "grant_id": "eg4"})
    with pytest.raises(NetworkError, match=R.OUT_OF_SCOPE):
        NetworkAuthority(EventLog(log.path), roles=_reg()).load()
    sub = reconstruct_subsystems(log, roles=_reg())
    assert set(sub.net_grants) == {"eg1"}
    assert [_kind_of(a) for a in sub.anomalies] == [R.OUT_OF_SCOPE]


def test_a_policy_names_no_role(tmp_path):
    """No escalation through a policy update: a policy the owner publishes
    may name anyone in its text, and it grants nobody a role."""
    log = EventLog(tmp_path / "log.jsonl")
    PolicyStore(log, roles=_reg()).load().publish(
        _doc(description="mallory verifies everything"), actor="owner")
    store = AuthorityStore(EventLog(log.path), roles=_reg()).load()
    store.create(record_id="r1", kind="note", proposer="alice")
    with pytest.raises(TransitionError, match=R.NOT_GRANTED):
        store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                         actor="mallory", role=Role.VERIFIER)


# ---- an authenticated principal is not an authorized one -----------------------
OWNER, ALICE, BOB, CAROL, MALLORY = (pr.test_identity(n) for n in
                                     ("owner", "alice", "bob", "carol",
                                      "mallory"))


def test_an_authenticated_principal_without_a_grant_cannot_promote(tmp_path):
    """Every event of this history is attested by a key the registry holds
    for its actor. Mallory's promotion verifies as hers, and is still not
    authority: a signature says who, the role registry says may."""
    keys = pr.KeyRegistry([s.registered() for s in
                           (OWNER, ALICE, BOB, CAROL, MALLORY)],
                          allow_test_keys=True)
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    auth = pr.Authenticator(att, keys)
    log = EventLog(tmp_path / "log.jsonl", authenticator=auth)
    pr.begin_history(log, att, OWNER)

    def signed(signer, action, rid, payload):
        pr.signed_append(log, att, signer, action=action, target=rid,
                         payload=payload)

    signed(ALICE, "record.create", "r1",
           {"record_id": "r1", "kind": "note", "proposer": "alice",
            "state": "PROPOSED", "evidence": {}, "depends_on": [],
            "policy_id": None, "idempotency_key": None})
    for src, dst, actor, role, evidence, policy in (
            ("PROPOSED", "UNDER_REVIEW", BOB, "VERIFIER", {}, None),
            ("UNDER_REVIEW", "VERIFIED", BOB, "VERIFIER",
             {"verification_report": _h("rep")}, None),
            ("VERIFIED", "PROMOTED", MALLORY, "PROMOTER",
             {"policy_id": "p1"}, "p1")):
        signed(actor, "record.transition", "r1",
               {"record_id": "r1", "src": src, "dst": dst, "role": role,
                "evidence": evidence, "policy_id": policy,
                "stale_reason": None, "edge_reason": "",
                "idempotency_key": None})
    reg = _reg()
    # authenticated, and without a registry, promoted
    assert AuthorityStore(log, authenticator=auth).load().get("r1").state \
        is State.PROMOTED
    with pytest.raises(StoreError) as exc:
        AuthorityStore(log, authenticator=auth, roles=reg).load()
    assert _kind_of(str(exc.value)) == R.NOT_GRANTED
    assert "'mallory'" in str(exc.value)
    rec = reconstruct(log, authenticator=auth, roles=reg)
    assert rec.canonical_ids() == ()
    assert [_kind_of(u) for u in rec.unauthorized] == [R.NOT_GRANTED]


# ---- the governed runner: a registration is not a grant (AUD-011) --------------
WS = "verification/stage10/_pytest_roles"


@pytest.fixture()
def workspace(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    yield base, f"{WS}/{name}/out"
    if base.exists():
        shutil.rmtree(base)
    if (ROOT / WS).exists() and not any((ROOT / WS).iterdir()):
        (ROOT / WS).rmdir()


def _governed_registry(*, verifier=True) -> R.RoleRegistry:
    from agent_substrate.governed_tasks import (POLICY_ID, SUBMITTER_ID,
                                                VERIFIER_ID, WORKER_ID)
    tool = "tool:stage10.emit_artifact"
    grants = [_g("g-gov-owner", "owner", R.POLICY_OWNER,
                 [f"policy:{POLICY_ID}"]),
              _g("g-gov-submit", SUBMITTER_ID, R.PROPOSER, [tool]),
              _g("g-gov-work", WORKER_ID, R.EXECUTOR, [tool])]
    if verifier:
        grants.append(_g("g-gov-verify", VERIFIER_ID, R.VERIFIER, [tool]))
    return R.RoleRegistry(grants)


def _governed(base, roles):
    from agent_substrate.governed_tasks import GovernedTasks
    return GovernedTasks(root=ROOT, log=EventLog(base / "log.jsonl"),
                         evidence=EvidenceStore(base / "evidence"),
                         roles=roles)


def _inputs(out_rel):
    return {"out_dir": out_rel, "name": "artifact.json",
            "payload": {"label": "MODEL_ONLY", "value": 42}}


def test_an_agent_anyone_registers_as_verifier_holds_no_role(workspace):
    """AUD-011's second half: the directory admits an AGENT holding any
    roles from any registrar. Under a registry that admission confers
    nothing, and the run is refused before a task exists."""
    from agent_substrate.agents import AgentRole, PrincipalKind, identity
    from agent_substrate.governed_tasks import (SUBMITTER_ID, VERIFIER_ID,
                                                WORKER_ID)
    base, out_rel = workspace
    gov = _governed(base, _governed_registry())
    gov.agents.register(identity(agent_id="v-evil", instance_id="v-evil",
                                 kind=PrincipalKind.AGENT,
                                 roles={AgentRole.VERIFIER}), by="mallory")
    gov.agents.require("v-evil", AgentRole.VERIFIER)   # the directory: yes
    before = [ev.action for ev in gov.log.read()]
    with pytest.raises(R.RoleRefused) as exc:
        gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(out_rel),
                submitter=SUBMITTER_ID, worker=WORKER_ID, verifier="v-evil")
    assert exc.value.kind == R.NOT_GRANTED
    assert [ev.action for ev in gov.log.read()] == before
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(out_rel),
                  submitter=SUBMITTER_ID, worker=WORKER_ID,
                  verifier=VERIFIER_ID)                  # control
    assert run.state.value == "VERIFIED"
    assert reconstruct_tasks(gov.log, roles=_governed_registry()) \
        .unauthorized == []


def test_a_task_verified_by_a_party_without_the_role_is_refused_on_replay(
        workspace):
    """A run made with no registry, read back under one that grants the
    verifier nothing: the projection on the production path refuses the
    verification, and the independent reader does not apply it."""
    from agent_substrate.governed_tasks import (SUBMITTER_ID, VERIFIER_ID,
                                                WORKER_ID)
    from agent_substrate.tasks import TaskTransitionError
    base, out_rel = workspace
    gov = _governed(base, None)
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(out_rel),
                  submitter=SUBMITTER_ID, worker=WORKER_ID,
                  verifier=VERIFIER_ID)
    assert run.state.value == "VERIFIED"
    with pytest.raises(TaskTransitionError) as exc:
        _governed(base, _governed_registry(verifier=False)).projection()
    assert _kind_of(str(exc.value)) == R.NOT_GRANTED
    assert "as VERIFIER" in str(exc.value)
    rec = reconstruct_tasks(gov.log, roles=_governed_registry(verifier=False))
    assert [_kind_of(u) for u in rec.unauthorized] == [R.NOT_GRANTED]
    assert rec.tasks[run.task_id]["state"] != "VERIFIED"
    assert _governed(base, _governed_registry()).projection() \
        .tasks[run.task_id].state.value == "VERIFIED"         # control


def _trace(base: Path, out_rel: str) -> dict:
    """Where each record of one governed run stands, on a fresh log: the
    positions a run with the same inputs on another fresh log takes too.
    Returns ``{name: seq}`` and the log."""
    from agent_substrate.governed_tasks import (SUBMITTER_ID, VERIFIER_ID,
                                                WORKER_ID)
    gov = _governed(base, None)
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(out_rel),
                  submitter=SUBMITTER_ID, worker=WORKER_ID,
                  verifier=VERIFIER_ID)
    assert run.state.value == "VERIFIED"
    at: dict = {"start": None}
    for ev in gov.log.read():
        p = ev.payload
        if ev.action == "task.create":
            at["start"] = ev.seq - 1
            at["create"] = ev.seq
        elif ev.action == "task.execution":
            at["execution"] = ev.seq
        elif ev.action == "task.transition" and p.get("dst") == "EXECUTING":
            at["executing"] = ev.seq
        elif ev.action == "task.transition" and p.get("dst") == "VERIFIED":
            at["verified"] = ev.seq
    return at, gov.log


def _ending(party: str, until: int) -> R.RoleRegistry:
    """The governed registry with ``party``'s grant ending at ``until``."""
    reg = _governed_registry()
    return R.RoleRegistry([
        R.RoleGrant(g.grant_id, g.principal, g.role, g.scope,
                    valid_until_seq=until) if g.grant_id == party else g
        for g in reg.grants()])


@pytest.mark.parametrize("party, last_good, refused_at", [
    ("g-gov-submit", "start", "create"),
    ("g-gov-work", "executing", "execution"),
    ("g-gov-verify", "execution", "verified"),
])
def test_task_replay_refuses_the_first_record_past_a_grant(
        workspace, party, last_good, refused_at):
    """Each party's grant ends one record before its own act. Both readers
    refuse exactly that record, as EXPIRED, and accept everything before
    it."""
    from agent_substrate.tasks import TaskTransitionError
    base, out_rel = workspace
    at, log = _trace(base, out_rel)
    reg = _ending(party, at[last_good])
    with pytest.raises(TaskTransitionError) as exc:
        _governed(base, reg).projection()
    assert _kind_of(str(exc.value)) == R.EXPIRED
    assert f"seq {at[refused_at]}:" in str(exc.value)
    rec = reconstruct_tasks(log, roles=reg)
    assert rec.unauthorized[0].startswith(f"seq {at[refused_at]}:")
    assert _kind_of(rec.unauthorized[0]) == R.EXPIRED
    assert _governed(base, _governed_registry()).projection()   # control


@pytest.mark.parametrize("party, until_at, refused", [
    ("g-gov-submit", "start", "create"),
    ("g-gov-work", "executing", "execution"),
    ("g-gov-verify", "execution", "verified"),
])
def test_a_grant_ending_mid_run_refuses_the_act_before_it_is_written(
        workspace, party, until_at, refused):
    """The run starts while every party holds its role, and one grant ends
    before that party's act. The act is refused where it is written, and
    nothing is written for it: the history stays one every reader loads.
    A check made only at the start of the run, or only on replay, leaves
    a record every later load refuses."""
    from agent_substrate.governed_tasks import (SUBMITTER_ID, VERIFIER_ID,
                                                WORKER_ID)
    base, out_rel = workspace
    # the log of each run, and the directory its tool writes, kept apart
    at, trace = _trace(base / "trace", out_rel.replace("/out", "/trace_out"))
    live = base / "live"
    live.mkdir()
    reg = _ending(party, at[until_at])
    gov = _governed(live, reg)
    with pytest.raises(R.RoleRefused) as exc:
        gov.run(tool_id="stage10.emit_artifact",
                inputs=_inputs(out_rel.replace("/out", "/live_out")),
                submitter=SUBMITTER_ID, worker=WORKER_ID,
                verifier=VERIFIER_ID)
    assert exc.value.kind == R.EXPIRED
    written = [ev for ev in gov.log.read()]
    # the positions the trace predicted are the ones this run took
    assert [(e.seq, e.action) for e in written] == \
        [(e.seq, e.action) for e in trace.read()][:len(written)]
    assert all(ev.seq < at[refused] for ev in written
               if ev.action.startswith("task.")), \
        "a record was written for the refused act"
    _governed(live, reg).projection()                 # the history loads
    assert reconstruct_tasks(gov.log, roles=reg).unauthorized == []
