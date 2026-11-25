"""Who wrote each record, checked a second time without the producer's code
(Stage 2, S2-2).

``tools/verify_assurance.py`` restates actor authentication -- RFC 8032
verification, the key-registry document and its lifecycle, the attestation
file -- with the standard library alone. The separate process reads a
governed history with it, because it may not import
``agent_substrate.principals``; the portable verifier will check a
package's history with it.

A second implementation is worth having only while it gives the first one's
verdicts. Each test below puts the same bytes to both and compares what
they say: the RFC's vectors, signatures the producer made, one real history
under every kind of finding the producer names, and the registry documents
the producer refuses.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import principals as pr  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "verify_assurance_restated", ROOT / "tools" / "verify_assurance.py")
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)

#: RFC 8032 section 7.1, TEST 1-3: secret, public, message, signature.
RFC8032 = [
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
     "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]
ALICE, BOB, OWNER, ZED = (pr.test_identity(n)
                          for n in ("alice", "bob", "owner", "zed"))


@pytest.mark.parametrize("public, message, signature", RFC8032)
def test_the_rfc_vectors_verify_and_nothing_else_does(public, message,
                                                      signature):
    pub, msg, sig = (bytes.fromhex(x) for x in (public, message, signature))
    assert V.ed25519_verify(pub, msg, sig)
    assert not V.ed25519_verify(pub, msg + b"\x00", sig)
    bent = bytearray(sig)
    bent[5] ^= 1
    assert not V.ed25519_verify(pub, msg, bytes(bent))
    # an S at or past the group order is not a signature (malleability)
    s = int.from_bytes(sig[32:], "little") + V._ED_L
    if s < 2 ** 256:
        assert not V.ed25519_verify(
            pub, msg, sig[:32] + s.to_bytes(32, "little"))
    assert not V.ed25519_verify(pub[:31], msg, sig)
    assert not V.ed25519_verify(pub, msg, sig[:63])


def test_it_agrees_with_the_producer_on_signatures_it_made():
    rng = random.Random(20261101)
    for i in range(24):
        who = pr.test_identity(f"p{i}")
        msg = rng.randbytes(rng.randrange(0, 200))
        sig = who.provider.sign(who.secret, msg)
        assert V.ed25519_verify(who.public, msg, sig)
        other = pr.test_identity(f"q{i}")
        assert not V.ed25519_verify(other.public, msg, sig)
        bent = bytearray(sig)
        bent[rng.randrange(64)] ^= 1 << rng.randrange(8)
        assert V.ed25519_verify(who.public, msg, bytes(bent)) == \
            who.provider.verify(who.public, msg, bytes(bent))


# ---- one history, every finding the producer names ---------------------------

def _legacy(tmp: Path):
    """A legacy history (so unsigned records can exist beside signed ones)
    with records by alice, bob and an actor no key is registered for."""
    log = EventLog(tmp / "log.jsonl")
    att = pr.Attestations(tmp / "log.attestations.jsonl")
    keys = pr.KeyRegistry([s.registered() for s in (ALICE, BOB, OWNER)],
                          allow_test_keys=True)
    for who, n in ((ALICE, 0), (BOB, 1), (ALICE, 2), (BOB, 3)):
        pr.signed_append(log, att, who, registry=keys, action="note.write",
                         target=f"n{n}", payload={"n": n})
    return log, att, keys


def _attest(att: pr.Attestations, signer, ev) -> None:
    """An attestation line written by hand, as a forger would: the
    signer's key over the event's hash, whoever the event names."""
    att.add({"seq": ev.seq, "event_hash": ev.hash, "key_id": signer.key_id,
             "sig": signer.provider.sign(
                 signer.secret, pr.DOMAIN + ev.hash.encode()).hex()})


def _kinds(findings) -> list:
    return sorted((s if s is not None else -1, k) for s, k, _ in findings)


def _damage(kind: str, log, att, keys):
    """Make ``kind`` happen to the history; return the registry document to
    read it under when the kind is about the registry."""
    _, events = log.read_verified()
    if kind == "MISSING":
        log.append(actor="alice", action="note.write", target="u",
                   payload={})
    elif kind == "NO_KEY":
        log.append(actor="carol", action="note.write", target="u",
                   payload={})
    elif kind == "WRONG_PRINCIPAL":
        log.append(actor="alice", action="note.write", target="u",
                   payload={})
        _attest(att, BOB, log.read_verified()[1][-1])
    elif kind == "UNKNOWN_KEY":
        log.append(actor="alice", action="note.write", target="u",
                   payload={})
        _attest(att, ZED, log.read_verified()[1][-1])
    elif kind == "BAD_SIGNATURE":
        lines = att.path.read_bytes().split(b"\n")
        rec = json.loads(lines[1])
        rec["sig"] = ("0" if rec["sig"][0] != "0" else "1") + rec["sig"][1:]
        lines[1] = json.dumps(rec, sort_keys=True,
                              separators=(",", ":")).encode()
        att.path.write_bytes(b"\n".join(lines))
    elif kind == "KEY_REVOKED":
        doc = keys.to_document()
        for k in doc["keys"]:
            if k["principal"] == "alice":
                k.update(revoked_at_seq=2, revocation_reason="rotated out",
                         status="REVOKED")
        return doc
    elif kind == "KEY_NOT_YET_VALID":
        doc = keys.to_document()
        for k in doc["keys"]:
            if k["principal"] == "bob":
                k.update(valid_from_seq=2)
        return doc
    elif kind == "KEY_COMPROMISED":
        doc = keys.to_document()
        for k in doc["keys"]:
            if k["principal"] == "bob":
                k.update(compromised_from_seq=0)
        return doc
    elif kind == "DANGLING":
        att.add({"seq": 9, "event_hash": hashlib.sha256(b"x").hexdigest(),
                 "key_id": ALICE.key_id, "sig": "0" * 128})
    elif kind == "ABORTED_COMMITTED":
        with att.path.open("ab") as fh:
            fh.write(json.dumps({"abort": events[1].hash, "seq": 1},
                                sort_keys=True).encode() + b"\n")
    elif kind == "MALFORMED":
        with att.path.open("ab") as fh:
            fh.write(b'{"seq": 1, "not": "an attestation"}\n')
    elif kind == "PENDING":
        # one complete prepare one past the head: not a finding for either
        att.add({"seq": events[-1].seq + 1,
                 "event_hash": hashlib.sha256(b"next").hexdigest(),
                 "key_id": ALICE.key_id, "sig": "0" * 128})
    elif kind == "TORN_TAIL":
        with att.path.open("ab") as fh:
            fh.write(b'{"seq": 4, "event_ha')
    return None


KINDS = ["CLEAN", "MISSING", "NO_KEY", "WRONG_PRINCIPAL", "UNKNOWN_KEY",
         "BAD_SIGNATURE", "KEY_REVOKED", "KEY_NOT_YET_VALID",
         "KEY_COMPROMISED", "DANGLING", "ABORTED_COMMITTED", "MALFORMED",
         "PENDING", "TORN_TAIL"]


@pytest.mark.parametrize("kind", KINDS)
def test_it_gives_the_producer_s_verdict_on_every_finding_kind(tmp_path,
                                                               kind):
    log, att, keys = _legacy(tmp_path)
    doc = _damage(kind, log, att, keys) or keys.to_document()
    _, events = log.read_verified()
    first = pr.authenticate(events, att, pr.KeyRegistry.from_document(
        doc, allow_test_keys=True))
    second = V.authenticate_events(
        events, V.read_attestation_file(att.path.read_bytes()),
        V.read_key_registry(doc, allow_test_keys=True))
    assert _kinds(first.findings) == _kinds(second["findings"])
    assert first.refused == second["refused"]
    assert first.authenticated == second["authenticated"]
    expected = kind not in ("CLEAN", "PENDING", "TORN_TAIL")
    assert bool(second["findings"]) == expected, second["findings"]
    if expected:
        assert kind in {k for _, k, _ in second["findings"]}


def test_a_catch_up_read_does_not_call_the_rest_of_the_file_dangling(
        tmp_path):
    log, att, keys = _legacy(tmp_path)
    _, events = log.read_verified()
    first = pr.authenticate(events[:2], att, keys, complete=False)
    second = V.authenticate_events(
        events[:2], V.read_attestation_file(att.path.read_bytes()),
        V.read_key_registry(keys.to_document(), allow_test_keys=True),
        complete=False)
    assert first.findings == [] and second["findings"] == []


# ---- the registry document ----------------------------------------------------

def _doc():
    return pr.KeyRegistry([s.registered() for s in (ALICE, BOB)],
                          allow_test_keys=True).to_document()


def _break(doc, how):
    keys = doc["keys"]
    if how == "a chosen key id":
        keys[0]["key_id"] = "test:ed25519:" + "0" * 32
    elif how == "a key registered twice":
        keys.append(dict(keys[0]))
    elif how == "uppercase hex":
        keys[0]["public"] = keys[0]["public"].upper()
    elif how == "a field more":
        keys[0]["note"] = "x"
    elif how == "a status its lifecycle contradicts":
        keys[0]["status"] = "REVOKED"
    elif how == "an expiry before it begins":
        keys[0].update(valid_from_seq=5, valid_until_seq=2,
                       status="ROTATED")
    elif how == "a revocation with no reason":
        keys[0].update(revoked_at_seq=3, status="REVOKED")
    elif how == "a replacement of another principal":
        keys[0]["replacement_key"] = keys[1]["key_id"]
    elif how == "another schema":
        doc["schema"] = "harness/key-registry/v3"
    elif how == "a seq that is not one":
        keys[0]["valid_from_seq"] = -1
    return doc


@pytest.mark.parametrize("how", [
    "a chosen key id", "a key registered twice", "uppercase hex",
    "a field more", "a status its lifecycle contradicts",
    "an expiry before it begins", "a revocation with no reason",
    "a replacement of another principal", "another schema",
    "a seq that is not one"])
def test_a_registry_the_producer_refuses_is_refused(how):
    doc = _break(_doc(), how)
    with pytest.raises(pr.AuthenticationError):
        pr.KeyRegistry.from_document(doc, allow_test_keys=True)
    with pytest.raises(V.Refused):
        V.read_key_registry(doc, allow_test_keys=True)


def test_a_test_key_is_read_only_where_test_keys_are_admitted():
    with pytest.raises(pr.AuthenticationError):
        pr.KeyRegistry.from_document(_doc())
    with pytest.raises(V.Refused):
        V.read_key_registry(_doc(), allow_test_keys=False)
    assert set(V.read_key_registry(_doc(), allow_test_keys=True)) == {
        ALICE.key_id, BOB.key_id}
