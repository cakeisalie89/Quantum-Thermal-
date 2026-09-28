"""A signed append across a crash, a registry the deployment pinned, and a
key's life measured in log positions (D-2026-93).

Two file appends are not atomic, and nothing here claims they are. The
ORDER is what is arranged: under the writer lock, the attestation of the
exact record is durable before the record. Each crash point below leaves a
state with one meaning, and no reader authenticates anything it should not.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import principals as pr  # noqa: E402
from qta_agent.canonical import canonical_bytes  # noqa: E402
from qta_agent.events import (  # noqa: E402
    PROFILE_AUTHENTICATED_REQUIRED, ChainBroken, EventLog, EventLogError,
)

OWNER = pr.test_identity("owner")
ALICE = pr.test_identity("alice")
ALICE2 = pr.test_identity("alice-next")          # a second key for alice


class Crash(Exception):
    """The process died here."""


def _registry(*keys):
    return pr.KeyRegistry(keys or [OWNER.registered(), ALICE.registered()],
                          allow_test_keys=True)


def _auth(att, registry=None):
    return pr.Authenticator(att, registry or _registry())


def _open(tmp_path, registry=None):
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    log = EventLog(tmp_path / "log.jsonl",
                   authenticator=_auth(att, registry))
    return log, att


def _started(tmp_path, n=2):
    log, att = _open(tmp_path)
    pr.begin_history(log, att, OWNER)
    for i in range(n):
        _put(log, att, i)
    return log, att


def _put(log, att, i, signer=ALICE, **kw):
    return pr.signed_append(log, att, signer, action="record.create",
                            target=f"r{i}",
                            payload={"record_id": f"r{i}", "kind": "k",
                                     "proposer": signer.principal}, **kw)


def _read(tmp_path, registry=None):
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    return EventLog(tmp_path / "log.jsonl",
                    authenticator=_auth(att, registry)).read_verified()


def _crash_append(log, att, i, how):
    """signed_append, with the process dying at ``how``."""
    real = pr._preparer(att, ALICE)

    def before_write(ev):
        if how == "before_prepare":
            raise Crash
        if how == "during_prepare":
            line = canonical_bytes(ALICE.attest(ev)) + b"\n"
            with att.path.open("ab") as fh:
                fh.write(line[: len(line) // 2])
            raise Crash
        real(ev)
        if how == "after_prepare":
            raise Crash
        if how == "during_record":
            line = canonical_bytes(ev.to_record()) + b"\n"
            with log.path.open("ab") as fh:
                fh.write(line[: len(line) // 2])
            raise Crash
    with pytest.raises(Crash):
        log.append(actor="alice", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "alice"}, before_write=before_write)


# ---- the order ---------------------------------------------------------------
def test_the_attestation_is_durable_before_the_record(tmp_path):
    log, att = _started(tmp_path, n=0)
    lines = {}

    def spy(ev):
        pr._preparer(att, ALICE)(ev)
        lines["log"] = len(log.path.read_bytes().splitlines())
        lines["att"] = len(att.path.read_bytes().splitlines())
    log.append(actor="alice", action="record.create", target="r",
               payload={"record_id": "r", "kind": "k", "proposer": "alice"},
               before_write=spy)
    assert lines == {"log": 1, "att": 2}, (
        "when the record is about to be written, its attestation is already "
        "on disk and the record is not")


def test_an_unsigned_append_to_a_required_history_is_refused_at_the_write(
        tmp_path):
    log, att = _started(tmp_path)
    before = log.path.read_bytes()
    with pytest.raises(EventLogError, match="prepared before it"):
        log.append(actor="alice", action="record.create", target="r9",
                   payload={"record_id": "r9", "kind": "k",
                            "proposer": "alice"})
    assert log.path.read_bytes() == before


def test_a_pinned_deployment_refuses_an_unsigned_first_event(tmp_path):
    log = EventLog(tmp_path / "log.jsonl",
                   required_profile=PROFILE_AUTHENTICATED_REQUIRED)
    with pytest.raises(EventLogError, match="prepared before it"):
        log.append(actor="p", action="record.create", target="r",
                   payload={"record_id": "r", "kind": "k", "proposer": "p"})


def test_an_unsigned_required_declaration_is_refused(tmp_path):
    with pytest.raises(EventLogError, match="prepared before it"):
        EventLog(tmp_path / "log.jsonl").append(
            actor="owner", action="history.security_profile",
            target="history",
            payload={"profile": PROFILE_AUTHENTICATED_REQUIRED})


# ---- each crash point --------------------------------------------------------
@pytest.mark.parametrize("how", ["before_prepare", "during_prepare",
                                 "after_prepare"])
def test_a_crash_before_the_record_leaves_a_history_that_authenticates(
        tmp_path, how):
    log, att = _started(tmp_path)
    head = log.verify().head_seq
    _crash_append(log, att, 9, how)
    report, events = _read(tmp_path)
    assert report.ok, report.problems
    assert events[-1].seq == head, "the crashed append did not happen"
    seen = att.read()
    if how == "during_prepare":
        assert seen.torn_tail and not seen.malformed
    # And the next signed append goes through, repairing what was left.
    ev = _put(log, att, 10)
    report, events = _read(tmp_path)
    assert report.ok, report.problems
    assert events[-1].seq == ev.seq == head + 1
    after = att.read()
    assert not after.torn_tail and not after.malformed
    if how == "after_prepare":
        assert len(after.aborted) == 1, "the stale prepare was aborted"


def test_a_crash_during_the_record_write_fails_only_on_the_torn_tail(
        tmp_path):
    log, att = _started(tmp_path)
    head = log.verify().head_seq
    _crash_append(log, att, 9, "during_record")
    report, events = _read(tmp_path)
    assert report.torn_tail_only(), (
        "the log refuses its torn line as it always has; authentication adds "
        f"nothing to it -- the prepare is pending: {report.problems}")
    assert [e.seq for e in events][-1] == head
    # The operator's repair is the log's own: drop the partial line.
    data = log.path.read_bytes()
    log.path.write_bytes(data[: data.rfind(b"\n") + 1])
    log2, att2 = _open(tmp_path)
    _put(log2, att2, 10)
    report, events = _read(tmp_path)
    assert report.ok, report.problems
    assert events[-1].seq == head + 1


def test_a_crash_after_both_writes_loses_nothing(tmp_path):
    """The record and its attestation are durable; only the witness lags."""
    log, att = _started(tmp_path)
    witness = log.head_path.read_bytes()
    ev = _put(log, att, 9)
    log.head_path.write_bytes(witness)
    report, events = _read(tmp_path)
    assert report.ok, report.problems
    assert events[-1].hash == ev.hash


# ---- nothing is authenticated that should not be -----------------------------
def test_a_pending_prepare_does_not_authenticate_another_record(tmp_path):
    """After a crash between the writes, a record written into the file at
    that position -- not the one prepared -- is not authenticated."""
    log, att = _started(tmp_path)
    _crash_append(log, att, 9, "after_prepare")
    forged = EventLog(tmp_path / "log.jsonl")
    forged.authenticator = None
    head = log.verify()
    from qta_agent.canonical import digest
    body = {"seq": head.head_seq + 1, "event_id": "forged", "wall_time": 1.0,
            "actor": "alice", "action": "record.create", "target": "r666",
            "payload": {"record_id": "r666", "kind": "k",
                        "proposer": "alice"},
            "prev_hash": head.head_hash, "canonical_form_version": 1}
    with log.path.open("ab") as fh:
        fh.write(canonical_bytes(dict(body, hash=digest(body))) + b"\n")
    report, events = _read(tmp_path)
    assert not report.ok
    assert any("MISSING" in p for p in report.problems), report.problems
    assert events[-1].seq == head.head_seq


def test_an_abort_of_a_committed_event_is_a_finding(tmp_path):
    log, att = _started(tmp_path)
    committed = log.verify().head_hash
    att._append({"abort": committed, "seq": log.verify().head_seq})
    report, _ = _read(tmp_path)
    assert not report.ok
    assert any(pr.ABORTED_COMMITTED in p for p in report.problems)



def test_only_one_past_the_head_can_be_pending(tmp_path):
    """A prepare in flight is at head + 1, where the lock puts it. An
    attestation of no event of this history at any OTHER position is not a
    prepare anybody could be making: it is DANGLING."""
    log, att = _started(tmp_path, n=2)
    stray = dict(json.loads(att.path.read_bytes().splitlines()[1]),
                 event_hash="e" * 64)
    assert stray["seq"] != log.verify().head_seq + 1
    with att.path.open("ab") as fh:
        fh.write(canonical_bytes(stray) + b"\n")
    report, _ = _read(tmp_path)
    assert not report.ok
    assert any(pr.DANGLING in p for p in report.problems)

def test_two_prepares_in_flight_are_not_both_pending(tmp_path):
    log, att = _started(tmp_path)
    head = log.verify().head_seq
    for fake in ("a" * 64, "b" * 64):
        att._append({"seq": head + 1, "event_hash": fake,
                     "key_id": ALICE.key_id, "sig": "00" * 64})
    report, _ = _read(tmp_path)
    assert not report.ok
    assert any(pr.DANGLING in p for p in report.problems)


def test_a_torn_line_that_is_not_the_last_is_malformed(tmp_path):
    """Only the LAST line can be a prepare that did not finish. A broken
    line with a newline after it is damage: the history fails, and no
    writer extends it."""
    log, att = _started(tmp_path)
    with att.path.open("ab") as fh:
        fh.write(b'{"seq": 1, "event_ha\n')
    with pytest.raises(ChainBroken, match=pr.MALFORMED):
        _put(log, att, 9)
    report, _ = _read(tmp_path)
    assert not report.ok
    assert any(pr.MALFORMED in p for p in report.problems)



def test_a_signature_in_another_encoding_is_not_the_signers(tmp_path):
    """Found by the fuzzer against the parser before this change: one
    uppercase digit in ``sig`` is the same signature bytes, and it
    authenticated. A record in an encoding no signer writes is malformed,
    and the event it claimed is unattested."""
    log, att = _started(tmp_path, n=1)
    lines = att.path.read_bytes().splitlines()
    rec = json.loads(lines[-1])
    digit = next(i for i, c in enumerate(rec["sig"]) if c in "abcdef")
    rec["sig"] = (rec["sig"][:digit] + rec["sig"][digit].upper()
                  + rec["sig"][digit + 1:])
    att.path.write_bytes(b"\n".join(lines[:-1] + [canonical_bytes(rec)])
                         + b"\n")
    report, events = _read(tmp_path)
    assert not report.ok
    assert events[-1].seq == rec["seq"] - 1, "the read ends before it"
    assert any(pr.MALFORMED in p for p in report.problems)


def test_a_registry_key_in_another_encoding_is_refused():
    doc = _registry().to_document()
    doc["keys"][0]["public"] = doc["keys"][0]["public"].upper()
    with pytest.raises(pr.AuthenticationError, match="lowercase"):
        pr.KeyRegistry.from_document(doc, allow_test_keys=True)


def test_nesting_that_exhausts_the_parser_is_malformed_not_a_crash(tmp_path):
    log, att = _started(tmp_path, n=1)
    with att.path.open("ab") as fh:
        fh.write(b"[" * 100_000 + b"\n")
    seen = att.read()
    assert seen.malformed, "counted, not raised"
    report, _ = _read(tmp_path)
    assert not report.ok
    assert any(pr.MALFORMED in p for p in report.problems)


@pytest.mark.parametrize("data", [b"[" * 100_000, b"\xff\xfe{", b"{",
                                  b'{"schema": 1e999}'])
def test_registry_bytes_that_are_not_a_document_are_refused(data):
    with pytest.raises(pr.AuthenticationError):
        pr.KeyRegistry.from_bytes(data, allow_test_keys=True)


def test_a_pinned_registry_that_cannot_be_canonicalised_is_refused():
    with pytest.raises(pr.AuthenticationError, match="pinned"):
        pr.KeyRegistry.from_document({"schema": float("nan")},
                                     pinned_digest="0" * 64)

def test_replay_is_deterministic(tmp_path):
    log, att = _started(tmp_path)
    _crash_append(log, att, 9, "after_prepare")
    _put(log, att, 10)
    first = pr.authenticate(log.read_verified()[1], att, _registry())
    second = pr.authenticate(_read(tmp_path)[1], att, _registry())
    assert (first.authenticated, first.findings, first.refused) == (
        second.authenticated, second.findings, second.refused)


@pytest.mark.parametrize("line", [
    {"seq": [1], "event_hash": "a" * 64, "key_id": "k", "sig": "00"},
    {"seq": 1, "event_hash": ["a"], "key_id": "k", "sig": "00"},
    {"seq": 1, "event_hash": "a" * 64, "key_id": 7, "sig": "00"},
    {"seq": 1, "event_hash": "a" * 64, "key_id": "k", "sig": "0" * 10_000},
    {"seq": True, "event_hash": "a" * 64, "key_id": "k", "sig": "00"},
    {"abort": ["x"], "seq": 1},
])
def test_an_ill_typed_line_is_malformed_not_a_crash(tmp_path, line):
    log, att = _started(tmp_path)
    att._append(line)
    report, _ = _read(tmp_path)
    assert not report.ok
    assert any(pr.MALFORMED in p for p in report.problems)


# ---- a key's life, in log positions -------------------------------------------
def _rotating(tmp_path, *, rotate_at):
    """alice's first key speaks until ``rotate_at - 1``, her second from
    ``rotate_at``."""
    old = replace(ALICE.registered(), valid_until_seq=rotate_at - 1,
                  replacement_key=_alice2().key_id)
    new = replace(_alice2().registered(), valid_from_seq=rotate_at)
    return _registry(OWNER.registered(), old, new)


def _alice2():
    return pr.Signer("alice", ALICE2.secret, test=True)


def test_a_rotation_hands_over_at_a_position(tmp_path):
    reg = _rotating(tmp_path, rotate_at=3)
    log, att = _open(tmp_path, reg)
    pr.begin_history(log, att, OWNER)
    _put(log, att, 1)
    _put(log, att, 2)
    _put(log, att, 3, signer=_alice2())
    report, events = _read(tmp_path, reg)
    assert report.ok, report.problems
    assert len(events) == 4


def test_the_old_key_past_its_rotation_is_expired(tmp_path):
    reg = _rotating(tmp_path, rotate_at=2)
    log, att = _open(tmp_path, reg)
    pr.begin_history(log, att, OWNER)
    _put(log, att, 1)
    log.authenticator = None       # the WRITER does not police the registry
    log.required_profile = None
    log._profile = None
    with pytest.raises(EventLogError):
        _put(log, att, 2)          # ... but its head check still reads
    report, _ = _read(tmp_path, reg)
    assert report.ok
    # Written straight past it: the reader refuses.
    att2 = pr.Attestations(tmp_path / "log.attestations.jsonl")
    bare = EventLog(tmp_path / "log.jsonl", authenticator=pr.Authenticator(
        att2, _registry(OWNER.registered(), ALICE.registered())))
    _put(bare, att2, 2)
    report, _ = _read(tmp_path, reg)
    assert not report.ok
    assert any(pr.KEY_EXPIRED in p for p in report.problems)


def test_the_new_key_before_its_rotation_is_not_yet_valid(tmp_path):
    reg = _rotating(tmp_path, rotate_at=5)
    wide = _registry(OWNER.registered(), ALICE.registered(),
                     _alice2().registered())
    log, att = _open(tmp_path, wide)
    pr.begin_history(log, att, OWNER)
    _put(log, att, 1, signer=_alice2())
    report, _ = _read(tmp_path, reg)
    assert not report.ok
    assert any(pr.KEY_NOT_YET_VALID in p for p in report.problems)


def test_a_revocation_refuses_forward_and_leaves_history_standing(tmp_path):
    log, att = _started(tmp_path, n=3)
    revoked = replace(ALICE.registered(), revoked_at_seq=3,
                      revocation_reason="retired")
    reg = _registry(OWNER.registered(), revoked)
    report, events = _read(tmp_path, reg)
    assert not report.ok
    assert [e.seq for e in events] == [0, 1, 2], (
        "authorship before the revocation stands")
    assert any(pr.KEY_REVOKED in p for p in report.problems)


def test_a_declared_compromise_reaches_back_as_far_as_declared(tmp_path):
    log, att = _started(tmp_path, n=3)
    burnt = replace(ALICE.registered(), revoked_at_seq=3,
                    revocation_reason="stolen", compromised_from_seq=2)
    report, events = _read(tmp_path, _registry(OWNER.registered(), burnt))
    assert [e.seq for e in events] == [0, 1]
    assert any(pr.KEY_COMPROMISED in p for p in report.problems)


@pytest.mark.parametrize("change, why", [
    ({"valid_from_seq": 5, "valid_until_seq": 4}, "expires before it begins"),
    ({"revoked_at_seq": 3}, "names both where and why"),
    ({"replacement_key": "test:ed25519:nobody"}, "replacement"),
])
def test_an_incoherent_lifecycle_is_refused(change, why):
    with pytest.raises(pr.AuthenticationError, match=why):
        _registry(OWNER.registered(), replace(ALICE.registered(), **change))


def test_a_replacement_of_another_principal_is_refused():
    other = replace(ALICE.registered(), replacement_key=OWNER.key_id)
    with pytest.raises(pr.AuthenticationError, match="same principal"):
        _registry(OWNER.registered(), other)


# ---- the registry is the one the deployment pinned ----------------------------
def test_the_document_round_trips_and_its_digest_is_what_is_pinned():
    reg = _rotating(None, rotate_at=4)
    doc = reg.to_document()
    again = pr.KeyRegistry.from_document(
        doc, allow_test_keys=True, pinned_digest=pr.registry_digest(doc))
    assert again.to_document() == doc


def test_a_tampered_registry_is_refused_at_the_pin():
    doc = _registry().to_document()
    pin = pr.registry_digest(doc)
    mallory = pr.test_identity("mallory").registered()
    doc["keys"].append(dict(doc["keys"][0], key_id=mallory.key_id,
                            principal="mallory",
                            public=mallory.public.hex()))
    with pytest.raises(pr.AuthenticationError, match="pinned"):
        pr.KeyRegistry.from_document(doc, allow_test_keys=True,
                                     pinned_digest=pin)


def test_production_needs_the_pin_as_well_as_the_document(
        tmp_path, monkeypatch):
    carol = pr.Signer("carol", bytes(range(32)))
    doc = pr.KeyRegistry([carol.registered()]).to_document()
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(pr, "PRODUCTION_KEYS", str(path))
    with pytest.raises(pr.NotConfigured):
        pr.production_registry()
    monkeypatch.setattr(pr, "PRODUCTION_REGISTRY_DIGEST", "0" * 64)
    with pytest.raises(pr.AuthenticationError, match="pinned"):
        pr.production_registry()
    monkeypatch.setattr(pr, "PRODUCTION_REGISTRY_DIGEST",
                        pr.registry_digest(doc))
    assert pr.production_registry().get(carol.key_id) is not None


def test_a_v1_document_still_reads_as_forever_valid():
    doc = {"schema": pr.REGISTRY_SCHEMA, "keys": [
        {"key_id": ALICE.key_id, "principal": "alice", "scheme": "ed25519",
         "public": ALICE.public.hex()}]}
    key = pr.KeyRegistry.from_document(doc, allow_test_keys=True).get(
        ALICE.key_id)
    assert key.status == "ACTIVE" and key.refusal_at(10 ** 9) is None


@pytest.mark.parametrize("entry_change", [
    {"key_id": 7}, {"principal": ""}, {"public": "zz" * 32},
    {"public": "00" * 31}, {"valid_from_seq": -1}, {"valid_from_seq": True},
    {"status": "REVOKED"}, {"extra": 1},
])
def test_an_ill_typed_registry_entry_is_refused_not_crashed(entry_change):
    doc = _registry().to_document()
    doc["keys"][0] = dict(doc["keys"][0], **entry_change)
    with pytest.raises(pr.AuthenticationError):
        pr.KeyRegistry.from_document(doc, allow_test_keys=True)


def test_a_registry_in_the_log_is_not_a_registry(tmp_path):
    """The history cannot choose what authenticates it: a key 'registered'
    in the log's own events authenticates nothing."""
    log, att = _started(tmp_path)
    mallory = pr.test_identity("mallory")
    pr.signed_append(log, att, ALICE, action="record.create", target="reg",
                     payload={"record_id": "reg", "kind": "k",
                              "proposer": "alice",
                              "registry": _registry(
                                  mallory.registered()).to_document()})
    raw = EventLog(tmp_path / "log.jsonl")
    raw.authenticator = pr.Authenticator(att, _registry())
    raw._profile = None
    with pytest.raises(EventLogError):
        raw.append(actor="mallory", action="record.create", target="m",
                   payload={"record_id": "m", "kind": "k",
                            "proposer": "mallory"})
    report, _ = _read(tmp_path)
    assert report.ok
