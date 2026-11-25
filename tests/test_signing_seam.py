"""Every writer signs, through the handle it writes with (Stage 2, S2a).

THE GAP. No authority-layer writer held a signer. An authenticated history
-- REQUIRED or GOVERNED -- refuses every unsigned record, so the stores that
govern a history could read one and never write one: it could be written
only record by record, through ``principals.signed_append``, by code that
bypassed every store's live checks. A governed run of the harness itself
was therefore impossible, and so was a checkpoint of an authenticated
history.

THE SEAM. ``EventLog(..., signing=principals.Signing(attestations,
signers))``: whichever writer appends through the handle, the record is
signed with its actor's key by the signed-append protocol -- the key
checked against the registry for that seq, the attestation durable before
the record, under the writer lock. Refused before a byte is written: an
actor the handle holds no key for, a key the registry does not hold for
that principal, and an attestation file the log's reader does not read.

A signature is not an authorization: the role registry still decides,
live in the store and on every replay, and a signing handle holding
mallory's key writes nothing mallory is not granted.

Every key is a TEST identity.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import roles as R  # noqa: E402
from agent_substrate.checkpoint import CheckpointStore  # noqa: E402
from agent_substrate.events import EventLog, EventLogError  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.reconstruct import reconstruct  # noqa: E402
from agent_substrate.authority import Role, State, TransitionError  # noqa: E402
from agent_substrate.store import CHECKPOINT_ACTOR, AuthorityStore  # noqa: E402

GOVERNED = "GOVERNED"
OWNER, ALICE, BOB, CAROL, MALLORY, CHECKPOINTER = (
    pr.test_identity(n) for n in ("owner", "alice", "bob", "carol",
                                  "mallory", CHECKPOINT_ACTOR))
EVERYONE = (OWNER, ALICE, BOB, CAROL, MALLORY, CHECKPOINTER)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Governed:
    """A GOVERNED history on disk, and handles onto it."""

    def __init__(self, tmp: Path):
        self.path = tmp / "log.jsonl"
        self.att = pr.Attestations(tmp / "log.attestations.jsonl")
        self.keys = pr.KeyRegistry([s.registered() for s in EVERYONE],
                                   allow_test_keys=True)
        self.roles = R.RoleRegistry([
            R.RoleGrant("g-alice", "alice", R.PROPOSER, ("kind:note",)),
            R.RoleGrant("g-bob", "bob", R.VERIFIER, ("kind:note",)),
            R.RoleGrant("g-carol", "carol", R.PROMOTER, ("kind:note",)),
        ])
        pr.begin_history(self.open(), self.att, OWNER, profile=GOVERNED,
                         roles=self.roles)

    def open(self, *, signers=(), attestations=None) -> EventLog:
        signing = None
        if signers:
            signing = pr.Signing(attestations or self.att, signers)
        return EventLog(self.path,
                        authenticator=pr.Authenticator(self.att, self.keys),
                        roles=self.roles, signing=signing)

    def lines(self) -> tuple:
        """The bytes of the log and of the attestation file."""
        return (self.path.read_bytes(),
                self.att.path.read_bytes() if self.att.path.exists()
                else b"")


def _walk(store: AuthorityStore) -> None:
    store.create(record_id="r1", kind="note", proposer="alice",
                 evidence={"claim": _h("r1")})
    store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                     role=Role.VERIFIER)
    store.transition(record_id="r1", dst=State.VERIFIED, actor="bob",
                     role=Role.VERIFIER,
                     evidence={"verification_report": _h("report")})
    store.transition(record_id="r1", dst=State.PROMOTED, actor="carol",
                     role=Role.PROMOTER, evidence={"policy_id": "p1"},
                     policy_id="p1")


def test_the_store_writes_a_governed_history_through_a_signing_handle(
        tmp_path):
    g = Governed(tmp_path)
    store = AuthorityStore(g.open(signers=(ALICE, BOB, CAROL))).load()
    _walk(store)
    assert store.get("r1").state is State.PROMOTED
    report, events = g.open().read_verified()
    assert report.ok, report.problems
    assert [e.actor for e in events] == ["owner", "alice", "bob", "bob",
                                         "carol"]
    # each record attested once, by the key of its own actor
    attested = g.att.read()
    assert not attested.malformed and not attested.torn_tail
    for ev in events:
        (att,) = attested.by_hash[ev.hash]
        key = g.keys.get(att["key_id"])
        assert key.principal == ev.actor and att["seq"] == ev.seq
    # the primary reader, built from the log alone, and the independent one
    assert AuthorityStore(g.open()).load().get("r1").state is State.PROMOTED
    second = reconstruct(g.open(),
                         authenticator=pr.Authenticator(g.att, g.keys),
                         roles=g.roles)
    assert second.records["r1"]["state"] == "PROMOTED"
    assert second.unauthorized == []


def test_a_handle_writes_only_as_the_principals_it_holds_keys_for(tmp_path):
    g = Governed(tmp_path)
    store = AuthorityStore(g.open(signers=(ALICE,))).load()
    store.create(record_id="r1", kind="note", proposer="alice",
                 evidence={"claim": _h("r1")})
    before = g.lines()
    with pytest.raises(pr.AuthenticationError,
                       match="holds no key for 'bob'"):
        store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                         actor="bob", role=Role.VERIFIER)
    assert g.lines() == before, "a refused write leaves nothing behind"
    assert AuthorityStore(g.open()).load().get("r1").state is \
        State.PROPOSED


@pytest.mark.parametrize("signer, kind", [
    (pr.test_identity("zed"), pr.UNKNOWN_KEY),
    (pr.Signer("alice", pr.test_identity("bob").secret, test=True),
     pr.WRONG_PRINCIPAL),
])
def test_a_key_the_registry_does_not_hold_for_the_actor_writes_nothing(
        tmp_path, signer, kind):
    g = Governed(tmp_path)
    before = g.lines()
    with pytest.raises(pr.AuthenticationError, match=kind):
        g.open(signers=(signer,)).append(
            actor=signer.principal, action="note.write", target="n1",
            payload={})
    assert g.lines() == before


def test_a_handle_attesting_where_its_reader_does_not_read_is_refused(
        tmp_path):
    g = Governed(tmp_path)
    elsewhere = pr.Attestations(tmp_path / "elsewhere.jsonl")
    before = g.lines()
    with pytest.raises(pr.AuthenticationError,
                       match="would read as unattested"):
        AuthorityStore(g.open(signers=(ALICE,), attestations=elsewhere)
                       ).load().create(record_id="r1", kind="note",
                                       proposer="alice",
                                       evidence={"claim": _h("r1")})
    assert g.lines() == before
    assert not elsewhere.path.exists()


def test_signing_is_not_authorization(tmp_path):
    """A handle holding mallory's genuine key writes nothing the registry
    does not grant her: the store refuses her live, before signing."""
    g = Governed(tmp_path)
    store = AuthorityStore(g.open(signers=(ALICE, BOB, MALLORY))).load()
    store.create(record_id="r1", kind="note", proposer="alice",
                 evidence={"claim": _h("r1")})
    before = g.lines()
    with pytest.raises(TransitionError, match=R.NOT_GRANTED):
        store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                         actor="mallory", role=Role.VERIFIER)
    assert g.lines() == before


def test_a_handle_without_keys_is_still_refused(tmp_path):
    """The seam adds a way to sign; it removes no refusal of an unsigned
    record."""
    g = Governed(tmp_path)
    with pytest.raises(EventLogError, match="prepared before it"):
        AuthorityStore(g.open()).load().create(
            record_id="r1", kind="note", proposer="alice",
            evidence={"claim": _h("r1")})


@pytest.mark.parametrize("signers, why", [
    ((), "holds no key"),
    ((ALICE, pr.test_identity("alice")), "two keys for 'alice'"),
    (("alice",), "is not a signer"),
])
def test_a_signing_handle_is_well_formed(tmp_path, signers, why):
    att = pr.Attestations(tmp_path / "a.jsonl")
    with pytest.raises(pr.AuthenticationError, match=why):
        pr.Signing(att, signers)
    with pytest.raises(pr.AuthenticationError, match="attestation file"):
        pr.Signing(str(tmp_path / "a.jsonl"), (ALICE,))


def test_an_authenticated_history_is_checkpointed_and_restored(tmp_path):
    """The residual AUD-020 left: a checkpoint is a record its writer
    appends, and an authenticated history refused it unsigned. Written by
    a handle holding the checkpointer's key, it is attested like every
    other record, and the checkpoint restores through the gate."""
    g = Governed(tmp_path)
    log = g.open(signers=(ALICE, BOB, CAROL, CHECKPOINTER))
    store = AuthorityStore(log).load()
    _walk(store)
    cps = CheckpointStore(tmp_path / "checkpoints")
    blobs = EvidenceStore(tmp_path / "blobs")
    store.checkpoint(cps, blobs=blobs)
    report, events = g.open().read_verified()
    assert report.ok, report.problems
    assert events[-1].actor == CHECKPOINT_ACTOR
    restored = AuthorityStore.load_from(g.open(), cps, blobs=blobs)
    assert restored.get("r1").state is State.PROMOTED
    # and still through the gate: no role registry, nothing restored
    with pytest.raises(Exception):
        AuthorityStore.load_from(
            EventLog(g.path, authenticator=pr.Authenticator(g.att, g.keys)),
            cps, blobs=blobs)
