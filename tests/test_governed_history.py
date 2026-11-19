"""A history that requires its own authorization roots (Stage 1, AUD-020).

THE GAP. A role registry was enforced by every reader GIVEN one. Nothing in
a history could require it: a reader built without a registry read a
promotion by an authenticated principal holding no PROMOTER grant as a
promotion, and the reader built with one refused it. Which of the two a
caller got was the caller's choice.

THE CLOSURE. A history may declare, as its first event, the GOVERNED
profile: every event authenticated, AND the digests of the key registry
and the role registry it is read under. A log opened on such a history
without both -- or with others -- reads nothing, every role-enforcing
reader enforces the registry its log was opened with, and the independent
reader restates the requirement in its own code. A history that declares
AUTHENTICATED_REQUIRED or nothing keeps its behaviour: those are the
explicitly identified legacy profiles, and a deployment that pins GOVERNED
refuses them as a downgrade.

Every history below is real: keys are TEST identities, every event is
attested, and the forged records are signed by principals the key registry
holds -- authenticated, and not authorized.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import archive as A  # noqa: E402
from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import roles as R  # noqa: E402
from agent_substrate.events import (  # noqa: E402
    PROFILE_AUTHENTICATED_REQUIRED, ChainBroken, EventLog, EventLogError,
)
from agent_substrate.netauth import EgressGrant, NetworkAuthority  # noqa: E402
from agent_substrate.policy import Effect, PolicyStore, document, rule  # noqa: E402
from agent_substrate.reconstruct import (  # noqa: E402
    reconstruct, reconstruct_subsystems, reconstruct_tasks,
)
from agent_substrate.secrets import SecretGrant, SecretStore  # noqa: E402
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402
from agent_substrate import transparency as T  # noqa: E402
from agent_substrate.checkpoint import CheckpointStore  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402

GOVERNED = "GOVERNED"
OWNER, ALICE, BOB, CAROL, MALLORY = (pr.test_identity(n) for n in (
    "owner", "alice", "bob", "carol", "mallory"))
SIGNERS = {s.principal: s for s in (OWNER, ALICE, BOB, CAROL, MALLORY)}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _keys(*extra) -> pr.KeyRegistry:
    return pr.KeyRegistry([s.registered() for s in (OWNER, ALICE, BOB, CAROL,
                                                    MALLORY, *extra)],
                          allow_test_keys=True)


def _roles(*grants) -> R.RoleRegistry:
    return R.RoleRegistry(list(grants) or [
        R.RoleGrant("g-alice", "alice", R.PROPOSER, ("kind:note",)),
        R.RoleGrant("g-bob", "bob", R.VERIFIER, ("kind:note",)),
        R.RoleGrant("g-carol", "carol", R.PROMOTER, ("kind:note",)),
        R.RoleGrant("g-owner", "owner", R.POLICY_OWNER, ("policy:p1",)),
    ])


class History:
    """One history on disk, its attestations, and how to open it."""

    def __init__(self, tmp: Path, *, profile=GOVERNED, keys=None,
                 roles=None):
        self.path = tmp / "log.jsonl"
        self.att = pr.Attestations(tmp / "log.attestations.jsonl")
        self.keys = keys or _keys()
        self.roles = roles or _roles()
        log = self.open()
        if profile == GOVERNED:
            pr.begin_history(log, self.att, OWNER, profile=GOVERNED,
                             roles=self.roles)
        else:
            pr.begin_history(log, self.att, OWNER, profile=profile)

    def open(self, *, keys="same", roles="same", **kw) -> EventLog:
        keys = self.keys if keys == "same" else keys
        roles = self.roles if roles == "same" else roles
        auth = None if keys is None else pr.Authenticator(self.att, keys)
        return EventLog(self.path, authenticator=auth, roles=roles, **kw)

    def put(self, who: str, action: str, target: str, payload: dict):
        pr.signed_append(self.open(), self.att, SIGNERS[who], action=action,
                         target=target, payload=payload)

    def create(self, rid="r1", who="alice", kind="note"):
        self.put(who, "record.create", rid, {
            "record_id": rid, "kind": kind, "proposer": who,
            "state": "PROPOSED", "evidence": {"claim": _h(rid)},
            "depends_on": [], "policy_id": None, "idempotency_key": None})

    def move(self, rid, src, dst, who, role, evidence=None, policy_id=None):
        self.put(who, "record.transition", rid, {
            "record_id": rid, "src": src, "dst": dst, "role": role,
            "evidence": evidence or {}, "policy_id": policy_id,
            "stale_reason": None, "edge_reason": "",
            "idempotency_key": None})

    def walk(self, rid="r1", *, verifier="bob", promoter="carol"):
        """A record taken to PROMOTED by signed raw records, the way any
        principal holding a key can take it."""
        self.create(rid)
        self.move(rid, "PROPOSED", "UNDER_REVIEW", verifier, "VERIFIER")
        self.move(rid, "UNDER_REVIEW", "VERIFIED", verifier, "VERIFIER",
                  {"verification_report": _h(f"{rid}-report")})
        self.move(rid, "VERIFIED", "PROMOTED", promoter, "PROMOTER",
                  {"policy_id": "p1"}, "p1")


def _state(store: AuthorityStore, rid="r1") -> str:
    return store.get(rid).state.value


def _second(h: History, **kw):
    """The independent reader, given its roots explicitly: it takes neither
    the authenticator nor the role registry from the log object."""
    kw.setdefault("authenticator", pr.Authenticator(h.att, h.keys))
    kw.setdefault("roles", h.roles)
    return reconstruct(h.open(), **kw)


# ---- the gap, as it stands for the legacy profiles ---------------------------
def test_the_gap_in_a_history_that_declares_no_registry(tmp_path):
    """AUD-020, reproduced. An AUTHENTICATED_REQUIRED history: every event
    is attested, and mallory -- who holds a key and no PROMOTER grant --
    promotes r1. The reader given the registry refuses the promotion; the
    reader built without one reads r1 PROMOTED. Nothing in the history
    could say which reader was right. This stays true of REQUIRED and of
    unauthenticated histories, the explicitly legacy profiles: the closure
    is a profile that declares its roots."""
    h = History(tmp_path, profile=PROFILE_AUTHENTICATED_REQUIRED)
    h.walk(promoter="mallory")
    with pytest.raises(StoreError, match=R.NOT_GRANTED):
        AuthorityStore(h.open(roles=None), roles=h.roles).load()
    without = AuthorityStore(h.open(roles=None)).load()
    assert _state(without) == "PROMOTED"
    auth = pr.Authenticator(h.att, h.keys)
    assert reconstruct(h.open(roles=None), authenticator=auth,
                       roles=h.roles).records["r1"]["state"] == "VERIFIED"
    assert reconstruct(h.open(roles=None), authenticator=auth
                       ).records["r1"]["state"] == "PROMOTED"


# ---- the closure --------------------------------------------------------------
def test_a_governed_history_declares_its_roots_at_genesis(tmp_path):
    h = History(tmp_path)
    events = h.open().read_verified()[1]
    decl = events[0].payload
    assert decl == {"profile": GOVERNED,
                    "requirement": "harness/history-security/v1",
                    "key_registry": pr.registry_digest(
                        h.keys.to_document()),
                    "role_registry": pr.registry_digest(
                        h.roles.to_document())}


def test_the_authorized_walk_reads_everywhere(tmp_path):
    """The control: the same records by the principals the registry
    grants, read by every reader with the registries the history names."""
    h = History(tmp_path)
    h.walk()
    assert _state(AuthorityStore(h.open()).load()) == "PROMOTED"
    rec = _second(h)
    assert rec.records["r1"]["state"] == "PROMOTED", rec.unauthorized
    assert not rec.unauthorized


@pytest.mark.parametrize("opened", ["no roles", "other roles", "no keys",
                                    "other keys"])
def test_a_governed_history_reads_nothing_without_its_own_roots(tmp_path,
                                                                 opened):
    h = History(tmp_path)
    h.walk(promoter="mallory")
    other_roles = _roles(R.RoleGrant("g-x", "mallory", R.PROMOTER,
                                     ("kind:*",)))
    other_keys = _keys(pr.test_identity("zed"))
    kw = {"no roles": {"roles": None}, "other roles": {"roles": other_roles},
          "no keys": {"keys": None}, "other keys": {"keys": other_keys}}
    log = h.open(**kw[opened])
    report, events = log.read_verified()
    assert not report.ok and events == []
    assert any(GOVERNED in p for p in report.problems), report.problems
    # every reader that folds the history refuses it, the second reader too
    with pytest.raises(Exception):
        AuthorityStore(log).load()
    with pytest.raises(ChainBroken):
        reconstruct(h.open(**kw[opened]))


def test_mallory_s_promotion_is_refused_by_both_readers(tmp_path):
    """Authenticated is not authorized: mallory's key is genuine, her
    promotion is refused by the primary reader with the registry the log
    was opened with -- no registry passed by the caller -- and by the
    independent reader."""
    h = History(tmp_path)
    h.walk(promoter="mallory")
    with pytest.raises(StoreError, match=R.NOT_GRANTED):
        AuthorityStore(h.open()).load()
    rec = _second(h)
    assert rec.records["r1"]["state"] == "VERIFIED"
    assert any("mallory" in u and R.NOT_GRANTED in u
               for u in rec.unauthorized), rec.unauthorized


@pytest.mark.parametrize("who, role, rid", [
    ("mallory", "VERIFIER", "r1"),        # a forged verifier
    ("alice", "VERIFIER", "r1"),          # the proposer verifying
])
def test_a_forged_verification_is_refused_by_both_readers(tmp_path, who,
                                                          role, rid):
    h = History(tmp_path)
    h.create(rid)
    h.move(rid, "PROPOSED", "UNDER_REVIEW", who, role)
    with pytest.raises(StoreError, match="Presence in the log is not "
                                          "authority"):
        AuthorityStore(h.open()).load()
    assert _second(h).records[rid]["state"] == "PROPOSED"


def test_a_reader_cannot_enforce_another_registry_than_its_log_s(tmp_path):
    h = History(tmp_path)
    h.walk()
    other = _roles(R.RoleGrant("g-x", "mallory", R.PROMOTER, ("kind:*",)))
    with pytest.raises(R.RoleError, match="not the one its log"):
        AuthorityStore(h.open(), roles=other)
    with pytest.raises(R.RoleError, match="not the one its log"):
        PolicyStore(h.open(), roles=other)


def test_policy_secret_and_egress_issuers_are_held_to_the_history_s_roots(
        tmp_path):
    """Each issuing role, enforced with the registry the log was opened
    with: mallory, authenticated, holds none of them."""
    h = History(tmp_path)
    doc = document(policy_id="p1", version=1, rules=(rule(
        rule_id="r", effect=Effect.ALLOW, actions=("x",), subjects=("*",),
        roles=("*",), resources=("*",)),))
    with pytest.raises(Exception, match="POLICY_OWNER|NOT_GRANTED"):
        PolicyStore(h.open()).load().publish(doc, actor="mallory")
    secrets = SecretStore(h.open()).load()
    secrets.register("db", "s3cret-value")
    with pytest.raises(Exception, match=R.NOT_GRANTED):
        secrets.issue(SecretGrant(grant_id="sg1", subject="worker",
                                  task_id="t1", tool_id="tool",
                                  secret_id="db", purposes=("read",)),
                      actor="mallory")
    with pytest.raises(Exception, match=R.NOT_GRANTED):
        NetworkAuthority(h.open()).load().issue(EgressGrant(
            grant_id="eg1", subject="worker", task_id="t1", tool_id="tool",
            schemes=("https",), hosts=("api.example.org",), ports=(443,),
            methods=("GET",), allow_unpinned_addresses=True),
            actor="mallory")


def test_a_governed_history_cannot_be_downgraded(tmp_path):
    """A deployment that pins GOVERNED refuses a history declaring less --
    the rewrite an attacker holding the file would make to escape the
    registries -- and a governed history never re-declares itself."""
    legacy = History(tmp_path / "a", profile=PROFILE_AUTHENTICATED_REQUIRED)
    report, events = legacy.open(required_profile=GOVERNED).read_verified()
    assert not report.ok and events == []
    assert any("DOWNGRADE" in p for p in report.problems), report.problems
    h = History(tmp_path / "b")
    with pytest.raises((EventLogError, pr.AuthenticationError)):
        pr.begin_history(h.open(), h.att, OWNER,
                         profile=PROFILE_AUTHENTICATED_REQUIRED)


def test_a_registry_substituted_after_the_fact_reads_nothing(tmp_path):
    """Registry substitution: a reader that opens the history with another
    registry than the one it declares -- after a checkpoint or at any other
    time -- reads nothing, so no state folded under one registry is restored
    under another. A store checkpoint is itself an unsigned record, which an
    authenticated history refuses: checkpointing an authenticated history
    needs a signing writer, which is not built, and the refusal is kept."""
    h = History(tmp_path)
    h.walk()
    other = _roles(*_roles().grants(), R.RoleGrant(
        "g-x", "mallory", R.PROMOTER, ("kind:*",)))
    cps = CheckpointStore(tmp_path / "checkpoints")
    blobs = EvidenceStore(tmp_path / "blobs")
    with pytest.raises(Exception):
        AuthorityStore.load_from(h.open(roles=other), cps, blobs=blobs)
    assert _state(AuthorityStore.load_from(h.open(), cps, blobs=blobs)) \
        == "PROMOTED"
    with pytest.raises(EventLogError, match="prepared before it"):
        AuthorityStore(h.open()).load().checkpoint(cps, blobs=blobs)


def test_an_archive_of_a_governed_history_hands_back_nothing_and_restores_to_the_gate(
        tmp_path):
    h = History(tmp_path)
    h.walk()
    manifest, blobs = A.seal(h.open(), 3)
    events = h.open().read_verified()[1]
    cp = {"size": len(events),
          "root": T.root(T.entries_of(events)).hex(), "proof": []}
    rep = A.verify_archive(manifest, blobs.__getitem__, **cp)
    assert rep.profile == GOVERNED and rep.events == ()
    restored = tmp_path / "restored.jsonl"
    A.restore(manifest, blobs.__getitem__, restored, **cp)
    report, got = EventLog(restored, authenticator=pr.Authenticator(
        h.att, h.keys)).read_verified()
    assert not report.ok and got == [], "no role registry, no events"
    report, got = EventLog(restored, authenticator=pr.Authenticator(
        h.att, h.keys), roles=h.roles).read_verified()
    assert report.ok and len(got) == len(events)


def test_the_independent_reader_restates_the_requirement(tmp_path,
                                                         monkeypatch):
    """With the primitive's GOVERNED gate removed, the second reader still
    refuses a log opened without the declared role registry: it reads the
    declaration itself, and does not ask the primitive."""
    from agent_substrate import events as ev_mod
    h = History(tmp_path)
    h.walk(promoter="mallory")
    monkeypatch.setattr(ev_mod.EventLog, "_governed_refusal",
                        lambda self, decl: None)
    rec = reconstruct(h.open(roles=None),
                      authenticator=pr.Authenticator(h.att, h.keys))
    assert rec.records == {} or all(
        r["state"] != "PROMOTED" for r in rec.records.values())
    assert any(GOVERNED in u for u in rec.unauthorized), rec.unauthorized
    for second in (reconstruct_tasks(h.open(roles=None)),
                   reconstruct_subsystems(h.open(roles=None))):
        notes = getattr(second, "unauthorized", None) or \
            getattr(second, "anomalies", [])
        assert any(GOVERNED in u for u in notes), notes


@pytest.mark.parametrize("given", ["role", "key"])
def test_the_independent_reader_compares_the_registry_it_is_given(
        tmp_path, monkeypatch, given):
    """ANOTHER registry, not none: with the primitive's gate removed, the
    second reader given a registry other than the declared one folds
    nothing, because it compares digests rather than asking only that it
    was given one. The other role registry grants mallory PROMOTER as well,
    so a reader folding under it would read her promotion as authorized;
    the other key registry holds every key and one more, so every signature
    still verifies under it."""
    from agent_substrate import events as ev_mod
    h = History(tmp_path)
    h.walk(promoter="mallory")
    monkeypatch.setattr(ev_mod.EventLog, "_governed_refusal",
                        lambda self, decl: None)
    keys, roles = h.keys, h.roles
    if given == "role":
        roles = _roles(
            R.RoleGrant("g-alice", "alice", R.PROPOSER, ("kind:note",)),
            R.RoleGrant("g-bob", "bob", R.VERIFIER, ("kind:note",)),
            R.RoleGrant("g-carol", "carol", R.PROMOTER, ("kind:note",)),
            R.RoleGrant("g-owner", "owner", R.POLICY_OWNER, ("policy:p1",)),
            R.RoleGrant("g-mallory", "mallory", R.PROMOTER, ("kind:note",)))
    else:
        keys = _keys(pr.test_identity("zed"))
    said = f"declares its {given} registry"
    rec = reconstruct(h.open(keys=keys, roles=roles),
                      authenticator=pr.Authenticator(h.att, keys),
                      roles=roles)
    assert all(r["state"] != "PROMOTED" for r in rec.records.values())
    assert any(said in u for u in rec.unauthorized), rec.unauthorized
    assert any(said in u for u in reconstruct_tasks(
        h.open(keys=keys, roles=roles), roles=roles).unauthorized)
    assert any(said in u for u in reconstruct_subsystems(
        h.open(keys=keys, roles=roles), roles=roles).anomalies)


@pytest.mark.parametrize("payload", [
    {"profile": GOVERNED},
    {"profile": GOVERNED, "requirement": "harness/history-security/v1",
     "key_registry": "a" * 64},
    {"profile": GOVERNED, "requirement": "v0", "key_registry": "a" * 64,
     "role_registry": "b" * 64},
    {"profile": GOVERNED, "requirement": "harness/history-security/v1",
     "key_registry": "A" * 64, "role_registry": "b" * 64},
    {"profile": GOVERNED, "requirement": "harness/history-security/v1",
     "key_registry": "a" * 64, "role_registry": "b" * 64, "extra": 1},
])
def test_a_malformed_governed_declaration_is_refused(tmp_path, payload):
    """Refused unsigned, and refused SIGNED, for its shape: an unsigned
    GOVERNED declaration is refused for being unsigned whatever it says, so
    only the signed append below reaches the shape rule."""
    log = EventLog(tmp_path / "log.jsonl")
    with pytest.raises(EventLogError):
        log.append(actor="owner", action="history.security_profile",
                   target="history", payload=payload)
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    signed = EventLog(tmp_path / "log.jsonl",
                      authenticator=pr.Authenticator(att, _keys()))
    with pytest.raises(EventLogError,
                       match="declaration states its requirement"):
        pr.signed_append(signed, att, OWNER,
                         action="history.security_profile",
                         target="history", payload=payload)
    assert signed.read_verified()[1] == []


@pytest.mark.parametrize("change", ["another requirement", "a field more"])
def test_a_malformed_governed_declaration_is_refused_on_read(
        tmp_path, monkeypatch, change):
    """The rule on the read side, for a genesis no writer here writes: signed
    by the owner and naming the very registries the reader holds, so that
    only its shape is wrong and nothing but the shape rule can refuse it."""
    from agent_substrate import events as ev_mod
    keys, roles = _keys(), _roles()
    payload = {"profile": GOVERNED,
               "requirement": "harness/history-security/v1",
               "key_registry": pr.registry_digest(keys.to_document()),
               "role_registry": pr.registry_digest(roles.to_document())}
    if change == "another requirement":
        payload["requirement"] = "harness/history-security/v0"
    else:
        payload["extra"] = 1
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    with monkeypatch.context() as m:
        m.setattr(ev_mod, "_declaration_problem", lambda payload: None)
        pr.signed_append(EventLog(tmp_path / "log.jsonl",
                                  authenticator=pr.Authenticator(att, keys)),
                         att, OWNER, action="history.security_profile",
                         target="history", payload=payload)
    log = EventLog(tmp_path / "log.jsonl",
                   authenticator=pr.Authenticator(att, keys), roles=roles)
    report, events = log.read_verified()
    assert not report.ok and events == []
    assert any("declaration states its requirement" in p
               for p in report.problems), report.problems
    with pytest.raises(ChainBroken):
        reconstruct(log, authenticator=pr.Authenticator(att, keys),
                    roles=roles)


def test_a_governed_declaration_is_written_only_signed(tmp_path):
    """A well-formed GOVERNED declaration, written by an unsigned append, is
    refused before it exists: a governed history is authenticated from its
    first record."""
    keys, roles = _keys(), _roles()
    payload = {"profile": GOVERNED,
               "requirement": "harness/history-security/v1",
               "key_registry": pr.registry_digest(keys.to_document()),
               "role_registry": pr.registry_digest(roles.to_document())}
    log = EventLog(tmp_path / "log.jsonl")
    with pytest.raises(EventLogError, match="prepared before it"):
        log.append(actor="owner", action="history.security_profile",
                   target="history", payload=payload)
    assert not (tmp_path / "log.jsonl").exists() or \
        (tmp_path / "log.jsonl").read_bytes() == b""


def test_beginning_a_governed_history_needs_both_registries(tmp_path):
    att = pr.Attestations(tmp_path / "a.jsonl")
    log = EventLog(tmp_path / "log.jsonl",
                   authenticator=pr.Authenticator(att, _keys()))
    with pytest.raises(pr.AuthenticationError, match="begin it with both"):
        pr.begin_history(log, att, OWNER, profile=GOVERNED)
    with pytest.raises(pr.AuthenticationError, match="only a GOVERNED"):
        pr.begin_history(log, att, OWNER, roles=_roles())
