"""An actor is who a key says it is, when a registry of keys is given.

The hash chain makes a history tamper-EVIDENT: change one byte and every
later hash disagrees. It does not make it AUTHENTIC. A writer holding the
file can rewrite it end to end -- every record re-hashed, the head witness
too -- and the chain verifies; it can append a complete lifecycle under any
actor name, and every reader re-authorizes it. Plan 9.7: "actors are names,
not keys".

These tests hold the seam that closes it (``qta_agent.principals``) and the
two readers that use it. Each attack is shown to pass the hash chain first,
so the refusal is the signature's and not the chain's: actor substitution, a
tampered signature, a tampered payload, a wrong key, a key nobody
registered, an actor with no key, and a missing attestation. Keys here are
deterministic TEST identities; the production registry is not configured,
and says so rather than authenticating nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import ed25519  # noqa: E402
from qta_agent import principals as pr  # noqa: E402
from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.checkpoint import CheckpointStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.reconstruct import reconstruct  # noqa: E402
from qta_agent.store import AuthorityStore, StoreError  # noqa: E402

ALICE, BOB, MALLORY = (pr.test_identity(n)
                       for n in ("alice", "bob", "mallory"))


def _registry(*signers):
    return pr.KeyRegistry([s.registered() for s in signers],
                          allow_test_keys=True)


REGISTRY = _registry(ALICE, BOB, MALLORY)


# ---- the primitive ----------------------------------------------------------

#: RFC 8032 section 7.1, TEST 1-3: secret, public, message, signature.
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
     "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("secret,public,message,signature", RFC8032)
def test_the_primitive_reproduces_the_rfc_vectors(secret, public, message,
                                                  signature):
    sk, pk, msg, sig = map(bytes.fromhex, (secret, public, message,
                                           signature))
    assert ed25519.public_key(sk) == pk
    assert ed25519.sign(sk, msg) == sig
    assert ed25519.verify(pk, msg, sig)


def test_the_primitive_refuses_what_it_did_not_sign():
    sk = bytes.fromhex(RFC8032[1][0])
    pk, sig = ed25519.public_key(sk), ed25519.sign(sk, b"r")
    assert not ed25519.verify(pk, b"s", sig)                 # message
    assert not ed25519.verify(pk, b"r", sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not ed25519.verify(ALICE.public, b"r", sig)       # key
    assert not ed25519.verify(pk, b"r", sig[:32])            # length
    assert not ed25519.verify(pk[:31], b"r", sig)
    # s >= the group order: the malleable twin of a valid signature.
    s = int.from_bytes(sig[32:], "little") + ed25519.Q
    assert not ed25519.verify(pk, b"r",
                              sig[:32] + int.to_bytes(s, 32, "little"))


# ---- a history --------------------------------------------------------------

def _history(tmp_path, *, unsigned_by=None):
    """Three records by alice, bob and alice, each attested; optionally one
    more appended by ``unsigned_by`` with no attestation."""
    log = EventLog(tmp_path / "log.jsonl")
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    for i, who in enumerate((ALICE, BOB, ALICE)):
        pr.signed_append(log, att, who, action="record.create",
                         target=f"r{i}",
                         payload={"record_id": f"r{i}", "kind": "k",
                                  "proposer": who.principal})
    if unsigned_by is not None:
        log.append(actor=unsigned_by, action="record.create", target="r9",
                   payload={"record_id": "r9", "kind": "k",
                            "proposer": unsigned_by})
    return log, att


def _events(log):
    report, events = log.read_verified()
    report.raise_if_bad()
    return events


def _rewrite(tmp_path, log, change):
    """The file rewritten end to end, the way its holder could: every record
    re-hashed and chained, so the log verifies and only a signature can
    tell. ``change(event) -> dict`` of fields to replace."""
    out = EventLog(tmp_path / "rewritten.jsonl")
    for ev in _events(log):
        f = {"actor": ev.actor, "action": ev.action, "target": ev.target,
             "payload": dict(ev.payload)}
        f.update(change(ev) or {})
        out.append(event_id=ev.event_id, wall_time=ev.wall_time, **f)
    return out


def _kinds(report):
    return {k for _, k, _ in report.findings}


def test_a_genuine_history_authenticates(tmp_path):
    log, att = _history(tmp_path)
    report = pr.authenticate(_events(log), att, REGISTRY)
    assert report.ok and report.authenticated == 3 and not report.refused


# ---- the attacks, each past the hash chain ------------------------------------

def test_actor_substitution_is_refused(tmp_path):
    """bob's record renamed to mallory's -- a registered principal -- and the
    history re-chained. The chain verifies; bob's attestation, moved to the
    new hash, names a key of bob's. And the rename moved every later hash,
    so alice's record after it no longer carries her signature either."""
    log, att = _history(tmp_path)
    old = {ev.seq: ev.hash for ev in _events(log)}
    forged = _rewrite(tmp_path, log, lambda ev: {"actor": "mallory"}
                      if ev.actor == "bob" else None)
    new = {ev.seq: ev.hash for ev in _events(forged)}
    moved = pr.Attestations(tmp_path / "moved.jsonl")
    by_hash = att.read().by_hash
    for seq, h in old.items():
        for a in by_hash[h]:
            moved.add({**a, "event_hash": new[seq]})
    report = pr.authenticate(_events(forged), moved, REGISTRY)
    assert {(s, k) for s, k, _ in report.findings} == {
        (1, pr.WRONG_PRINCIPAL), (2, pr.BAD_SIGNATURE)}
    assert report.refused == {1, 2}


def test_a_tampered_signature_is_refused(tmp_path):
    log, att = _history(tmp_path)
    lines = att.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["sig"] = ("0" if rec["sig"][0] != "0" else "1") + rec["sig"][1:]
    lines[1] = json.dumps(rec)
    att.path.write_text("\n".join(lines) + "\n")
    report = pr.authenticate(_events(log), att, REGISTRY)
    assert _kinds(report) == {pr.BAD_SIGNATURE} and report.refused == {1}


def test_a_tampered_payload_is_refused(tmp_path):
    """alice's first record's payload changed and the history re-chained:
    her attestation is of the old hash (DANGLING), the new hash has none
    (MISSING) -- and moved onto the new hash, it does not verify."""
    log, att = _history(tmp_path)
    forged = _rewrite(tmp_path, log, lambda ev: {"payload": {
        **ev.payload, "kind": "forged"}} if ev.seq == 0 else None)
    report = pr.authenticate(_events(forged), att, REGISTRY)
    assert {pr.MISSING, pr.DANGLING} <= _kinds(report)
    assert report.refused == {0, 1, 2}         # every later hash moved too
    moved = pr.Attestations(tmp_path / "moved.jsonl")
    by_hash = att.read().by_hash
    for old, new in zip(_events(log), _events(forged)):
        for a in by_hash[old.hash]:
            moved.add({**a, "event_hash": new.hash})
    report = pr.authenticate(_events(forged), moved, REGISTRY)
    assert _kinds(report) == {pr.BAD_SIGNATURE}


def test_a_wrong_key_is_refused(tmp_path):
    """bob signs alice's event with his own, registered, key."""
    log, _ = _history(tmp_path)
    ev = _events(log)[0]
    att = pr.Attestations(tmp_path / "wrong.jsonl")
    for e in _events(log):
        signer = {"alice": ALICE, "bob": BOB}[e.actor]
        att.add(signer.attest(e) if e.seq != 0 else {
            "seq": ev.seq, "event_hash": ev.hash, "key_id": BOB.key_id,
            "sig": ed25519.sign(BOB.secret, pr.DOMAIN
                                + ev.hash.encode()).hex()})
    report = pr.authenticate(_events(log), att, REGISTRY)
    assert _kinds(report) == {pr.WRONG_PRINCIPAL} and report.refused == {0}


def test_a_signature_made_for_another_purpose_is_refused(tmp_path):
    """alice's own key over the bare event hash -- a signature she might
    make for something else -- is not an attestation: attestations are
    signed under their own domain."""
    log, _ = _history(tmp_path)
    att = pr.Attestations(tmp_path / "bare.jsonl")
    for e in _events(log):
        signer = {"alice": ALICE, "bob": BOB}[e.actor]
        att.add(signer.attest(e) if e.seq != 0 else {
            "seq": e.seq, "event_hash": e.hash, "key_id": ALICE.key_id,
            "sig": ed25519.sign(ALICE.secret, e.hash.encode()).hex()})
    report = pr.authenticate(_events(log), att, REGISTRY)
    assert _kinds(report) == {pr.BAD_SIGNATURE} and report.refused == {0}


def test_a_key_nobody_registered_is_refused(tmp_path):
    log, att = _history(tmp_path)
    report = pr.authenticate(_events(log), att, _registry(BOB))
    assert pr.NO_KEY in _kinds(report)                   # alice has none
    stranger = pr.test_identity("alice-impostor")
    att2 = pr.Attestations(tmp_path / "impostor.jsonl")
    for e in _events(log):
        att2.add({**stranger.attest(_renamed(e, "alice-impostor")),
                  "event_hash": e.hash, "seq": e.seq}
                 if e.actor == "alice" else BOB.attest(e))
    report = pr.authenticate(_events(log), att2, REGISTRY)
    assert _kinds(report) == {pr.UNKNOWN_KEY} and report.refused == {0, 2}


def _renamed(ev, actor):
    from dataclasses import replace
    return replace(ev, actor=actor)


def test_a_missing_attestation_is_refused_for_a_required_actor(tmp_path):
    log, att = _history(tmp_path, unsigned_by="alice")
    report = pr.authenticate(_events(log), att, REGISTRY)
    assert _kinds(report) == {pr.MISSING} and report.refused == {3}
    # Only alice required: an unsigned event by somebody else is not asked.
    log2, att2 = _history(tmp_path / "b", unsigned_by="carol")
    assert pr.authenticate(_events(log2), att2, REGISTRY,
                           require=frozenset({"alice", "bob"})).ok
    assert pr.NO_KEY in _kinds(pr.authenticate(_events(log2), att2,
                                               REGISTRY))


def test_an_unreadable_attestation_line_is_a_finding(tmp_path):
    log, att = _history(tmp_path)
    with att.path.open("a") as fh:
        fh.write("not json\n")
    assert pr.MALFORMED in _kinds(pr.authenticate(_events(log), att,
                                                  REGISTRY))


def test_the_chain_alone_does_not_see_a_rewrite(tmp_path):
    """Why the seam exists: the rewritten file VERIFIES."""
    log, _ = _history(tmp_path)
    forged = _rewrite(tmp_path, log, lambda ev: {"actor": "mallory"})
    report, events = forged.read_verified()
    report.raise_if_bad()
    assert [e.actor for e in events] == ["mallory"] * 3


# ---- the readers -------------------------------------------------------------

def _authority_history(tmp_path, *, forge_last=False):
    """A record proposed by alice and taken under review by bob, attested;
    ``forge_last`` has mallory's name put on bob's review, unattested."""
    log = EventLog(tmp_path / "log.jsonl")
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    pr.signed_append(log, att, ALICE, action="record.create", target="r1",
                     payload={"record_id": "r1", "kind": "claim",
                              "proposer": "alice", "state": "PROPOSED",
                              "evidence": {}, "depends_on": []})
    store = AuthorityStore(log)
    store.load()
    rec = store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                           actor="bob", role=Role.VERIFIER)
    ev = [e for e in _events(log) if e.seq == rec.updated_seq][0]
    if forge_last:
        return log, att
    att.add(BOB.attest(ev))
    return log, att


def test_the_store_loads_an_authenticated_history(tmp_path):
    log, att = _authority_history(tmp_path)
    auth = pr.Authenticator(att, REGISTRY)
    store = AuthorityStore(log, authenticator=auth).load()
    assert store.get("r1").state is State.UNDER_REVIEW
    recon = reconstruct(log, authenticator=auth)
    assert not recon.unauthorized
    assert recon.records["r1"]["state"] == "UNDER_REVIEW"


def test_both_readers_refuse_an_unattested_event(tmp_path):
    log, att = _authority_history(tmp_path, forge_last=True)
    auth = pr.Authenticator(att, REGISTRY)
    with pytest.raises(StoreError, match="does not authenticate"):
        AuthorityStore(log, authenticator=auth).load()
    recon = reconstruct(log, authenticator=auth)
    assert any("unauthenticated" in u and "MISSING" in u
               for u in recon.unauthorized), recon.unauthorized
    # Refused and not folded: the review never happened, for this reader.
    assert recon.records["r1"]["state"] == "PROPOSED"
    # Without the authenticator, both read the names as they always did.
    assert AuthorityStore(log).load().get("r1").state is State.UNDER_REVIEW


def test_a_catch_up_authenticates_what_it_folds(tmp_path):
    log, att = _authority_history(tmp_path)
    auth = pr.Authenticator(att, REGISTRY)
    store = AuthorityStore(log, authenticator=auth).load()
    log.append(actor="alice", action="record.create", target="r2",
               payload={"record_id": "r2", "kind": "claim",
                        "proposer": "alice", "state": "PROPOSED",
                        "evidence": {}, "depends_on": []})
    with pytest.raises(StoreError, match="does not authenticate"):
        store.catch_up()


def test_a_checkpoint_does_not_stand_in_for_authentication(tmp_path):
    """The snapshot says nothing about who wrote the records before it, so
    an authenticated restore reads every one."""
    log, att = _authority_history(tmp_path, forge_last=True)
    cps = CheckpointStore(tmp_path / "cp")
    blobs = EvidenceStore(tmp_path / "blobs")
    AuthorityStore(log).load().checkpoint(cps, blobs=blobs)
    assert AuthorityStore.load_from(log, cps, blobs=blobs
                                    ).loaded_prefix_verified is False
    with pytest.raises(StoreError, match="does not authenticate"):
        AuthorityStore.load_from(log, cps, blobs=blobs,
                                 authenticator=pr.Authenticator(att,
                                                                REGISTRY))


# ---- keys and registries -----------------------------------------------------

def test_a_registry_built_for_production_refuses_test_keys():
    with pytest.raises(pr.AuthenticationError, match="TEST key"):
        pr.KeyRegistry([ALICE.registered()])


def test_key_ids_are_derived_not_chosen():
    k = ALICE.registered()
    with pytest.raises(pr.AuthenticationError, match="not the id"):
        pr.KeyRegistry([pr.RegisteredKey(BOB.key_id, "alice", k.public)],
                       allow_test_keys=True)
    with pytest.raises(pr.AuthenticationError, match="twice"):
        pr.KeyRegistry([k, k], allow_test_keys=True)


def test_a_registry_document_round_trips():
    doc = REGISTRY.to_document()
    again = pr.KeyRegistry.from_document(doc, allow_test_keys=True)
    assert again.to_document() == doc
    with pytest.raises(pr.AuthenticationError, match="TEST key"):
        pr.KeyRegistry.from_document(doc)


def test_production_keys_are_pending_and_nothing_pretends_otherwise():
    assert pr.PRODUCTION_KEYS is None
    with pytest.raises(pr.NotConfigured, match="PENDING"):
        pr.production_registry()


def test_a_signer_speaks_only_for_its_principal(tmp_path):
    log, att = _history(tmp_path)
    with pytest.raises(pr.AuthenticationError, match="only its own"):
        ALICE.attest(_events(log)[1])
    with pytest.raises(pr.AuthenticationError, match="not a parameter"):
        pr.signed_append(log, att, ALICE, actor="bob", action="x",
                         target="y")
