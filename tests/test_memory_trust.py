"""Memory trust: six properties, answered separately (Tranche G, G4).

A remembered statement can be in the log, written by who it names, signed,
supported by evidence, agreed on by two readers -- and still not be a fact.
Each test below breaks ONE property and checks that the assessment says so
for that property and not for the others; the first holds every property a
memory can hold and shows the sixth is still not one of them.

Finding M-1, measured at b9b2187: the primary memory store accepted an entry
appended by 'mallory' that names 'alice' as its author, and recall() handed
it back as alice's. Only the independent reader flagged it. It is refused on
replay now, by both readers.

Finding M-2, found by this audit: the opposite divergence. The independent
reader accepted, with no anomaly, four memory histories the primary refuses
-- a retraction or a supersession by somebody other than the author, a
STALE note made ACTIVE again, and a status no store has -- and reported the
result as the memory's state. Both readers now refuse each, and
``test_both_readers_refuse_the_same_status_moves`` holds them together.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate import memory_trust as MT  # noqa: E402
from agent_substrate.memory import (ACT_MEMORY_STATUS,  # noqa: E402
                                    ACT_MEMORY_WRITE, MemoryEntry,
                                    MemoryError_, MemoryStore)
from agent_substrate.memory_trust import (NO, NOT_APPLICABLE,  # noqa: E402
                                          NOT_ESTABLISHED, PROPERTIES, YES,
                                          assess)

TEXT = "the coupling term is negligible below 40 K"


def _world(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "evidence")
    return log, ev, MemoryStore(log, evidence=ev).load()


def _values(trust) -> dict:
    return {k: a.value for k, a in trust.answers.items()}


def test_every_property_a_memory_can_hold_and_the_one_it_cannot(tmp_path):
    log, ev, mem = _world(tmp_path)
    dg = ev.put(b"a verification report")
    mem.remember(memory_id="m1", text=TEXT, author="alice",
                 derived_from=(dg,))
    t = assess(log, "m1", evidence=ev)
    assert set(t.answers) == set(PROPERTIES)
    assert _values(t) == {
        "hash_consistent": YES,
        "authenticated_actor": NOT_ESTABLISHED,   # no key vouches
        "policy_authorized": NOT_APPLICABLE,
        "evidence_supported": YES,
        "consistently_reconstructed": YES,
        "factually_established": NOT_ESTABLISHED,
    }
    assert t.status == "ACTIVE"


# ---- forged histories and compromised writers ---------------------------------

def _forge_attribution(log):
    rec = MemoryEntry(memory_id="m-forged", text=TEXT,
                      author="alice").to_record()
    log.append(actor="mallory", action=ACT_MEMORY_WRITE, target="m-forged",
               payload={"entry": rec})


def test_an_entry_attributed_to_somebody_else_is_refused_by_both_readers(
        tmp_path):
    log, ev, _ = _world(tmp_path)
    _forge_attribution(log)
    with pytest.raises(MemoryError_, match="names 'alice'"):
        MemoryStore(log, evidence=ev).load()
    assert any("names author 'alice'" in a
               for a in rc.reconstruct_subsystems(log).anomalies)
    t = assess(log, "m-forged", evidence=ev)
    assert t.answers["authenticated_actor"].value == NO
    assert t.answers["consistently_reconstructed"].value == NO
    assert t.answers["hash_consistent"].value == YES   # consistent, and false


def test_control_an_entry_by_its_own_author_loads(tmp_path):
    log, ev, _ = _world(tmp_path)
    rec = MemoryEntry(memory_id="m-own", text=TEXT,
                      author="mallory").to_record()
    log.append(actor="mallory", action=ACT_MEMORY_WRITE, target="m-own",
               payload={"entry": rec})
    assert MemoryStore(log, evidence=ev).load().get("m-own").author \
        == "mallory"


def _signed(tmp_path):
    owner, alice = pr.test_identity("owner"), pr.test_identity("alice")
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    auth = pr.Authenticator(att, pr.KeyRegistry(
        [owner.registered(), alice.registered()], allow_test_keys=True))
    log = EventLog(tmp_path / "log.jsonl", authenticator=auth)
    pr.begin_history(log, att, owner)
    return log, att, alice


def test_a_signed_writer_with_a_false_citation_is_authenticated_and_unsupported(
        tmp_path):
    """A COMPROMISED WRITER: a valid key, a false claim. Authentication
    answers who wrote it, and nothing about whether it is so."""
    log, att, alice = _signed(tmp_path)
    ev = EvidenceStore(tmp_path / "evidence")
    rec = MemoryEntry(memory_id="m-signed", text=TEXT, author="alice",
                      derived_from=("f" * 64,)).to_record()
    pr.signed_append(log, att, alice, action=ACT_MEMORY_WRITE,
                     target="m-signed", payload={"entry": rec})
    t = assess(log, "m-signed", evidence=ev)
    assert t.answers["authenticated_actor"].value == YES
    assert t.answers["evidence_supported"].value == NO
    assert "does not hold" in t.answers["evidence_supported"].why
    assert t.answers["factually_established"].value == NOT_ESTABLISHED


def test_a_removed_attestation_is_an_unauthenticated_writer_and_nothing_else(
        tmp_path):
    """Authentication and the chain are separate properties. A signed
    history whose attestation for the write is gone still links; what it
    has lost is the key that vouched for the writer."""
    log, att, alice = _signed(tmp_path)
    ev = EvidenceStore(tmp_path / "evidence")
    rec = MemoryEntry(memory_id="m-signed", text=TEXT,
                      author="alice").to_record()
    pr.signed_append(log, att, alice, action=ACT_MEMORY_WRITE,
                     target="m-signed", payload={"entry": rec})
    assert assess(log, "m-signed", evidence=ev).answers[
        "authenticated_actor"].value == YES
    lines = att.path.read_text().splitlines()
    att.path.write_text("\n".join(lines[:-1]) + "\n")
    t = assess(log, "m-signed", evidence=ev)
    assert t.answers["hash_consistent"].value == YES
    assert t.answers["authenticated_actor"].value == NO
    assert "MISSING" in t.answers["authenticated_actor"].why
    # The primary refuses the history it cannot authenticate.
    assert t.answers["consistently_reconstructed"].value == NO


def test_an_entry_that_cites_nothing_is_not_supported(tmp_path):
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    t = assess(log, "m1", evidence=ev)
    assert t.answers["evidence_supported"].value == NOT_ESTABLISHED
    assert "cites no evidence" in t.answers["evidence_supported"].why


def test_control_the_write_path_refuses_that_citation(tmp_path):
    log, ev, mem = _world(tmp_path)
    with pytest.raises(MemoryError_, match="does not resolve"):
        mem.remember(memory_id="m1", text=TEXT, author="alice",
                     derived_from=("f" * 64,))


def test_a_citation_whose_evidence_was_altered_is_not_support(tmp_path):
    log, ev, mem = _world(tmp_path)
    dg = ev.put(b"a verification report")
    mem.remember(memory_id="m1", text=TEXT, author="alice",
                 derived_from=(dg,))
    blob = ev._blob_path(dg)
    assert blob.is_file(), "premise: the blob is on disk under its digest"
    blob.chmod(0o644)
    blob.write_bytes(b"a different report")
    t = assess(log, "m1", evidence=ev)
    assert t.answers["evidence_supported"].value == NO


# ---- omitted events and truncation --------------------------------------------

def _two_and_a_retraction(tmp_path):
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    mem.remember(memory_id="m2", text="another note", author="alice")
    mem.retract("m1", actor="alice", reason="measured again; it is not")
    return log, ev


def test_an_omitted_event_is_seen(tmp_path):
    """Nothing in a history that does not verify is answered for: every
    property says so, for that reason, rather than reading the prefix."""
    log, ev = _two_and_a_retraction(tmp_path)
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    t = assess(EventLog(log.path), "m1", evidence=ev)
    assert t.answers["hash_consistent"].value == NO
    assert t.status is None
    for prop in PROPERTIES[1:]:
        assert "does not verify" in t.answers[prop].why, prop
        assert t.answers[prop].value == (
            NOT_ESTABLISHED if prop == "factually_established" else NO), prop


def test_a_truncated_tail_is_seen_against_the_recorded_head(tmp_path):
    """Dropping the retraction would hand m1 back as ACTIVE."""
    log, ev = _two_and_a_retraction(tmp_path)
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:-1]) + "\n")
    t = assess(EventLog(log.path), "m1", evidence=ev)
    assert t.answers["hash_consistent"].value == NO


def test_a_truncation_that_also_rewrites_the_head_is_not_visible_here(
        tmp_path):
    """THE STATED LIMIT, measured rather than assumed: a log truncated
    together with its head witness is consistent with itself. Only a head
    held somewhere the writer cannot reach -- a checkpoint anchor, a
    signed attestation -- sees it."""
    log, ev = _two_and_a_retraction(tmp_path)
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:-1]) + "\n")
    log.head_path.unlink()
    t = assess(EventLog(log.path), "m1", evidence=ev)
    assert t.answers["hash_consistent"].value == YES
    assert t.status == "ACTIVE"


# ---- stale views and retracted memories ----------------------------------------

def test_a_view_loaded_before_a_retraction_disagrees_with_the_log(tmp_path):
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    stale = MemoryStore(EventLog(log.path), evidence=ev).load()
    mem.retract("m1", actor="alice", reason="measured again; it is not")
    t = assess(log, "m1", evidence=ev, view=stale)
    assert t.status == "RETRACTED"
    assert t.answers["consistently_reconstructed"].value == NO
    assert "the view held says ACTIVE" in \
        t.answers["consistently_reconstructed"].why


def test_control_a_current_view_agrees(tmp_path):
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    mem.retract("m1", actor="alice", reason="measured again; it is not")
    fresh = MemoryStore(EventLog(log.path), evidence=ev).load()
    t = assess(log, "m1", evidence=ev, view=fresh)
    assert t.answers["consistently_reconstructed"].value == YES


def test_a_retracted_memory_is_reported_retracted_and_not_recalled(tmp_path):
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    mem.retract("m1", actor="alice", reason="measured again; it is not")
    assert mem.recall("coupling negligible") == ()
    t = assess(log, "m1", evidence=ev)
    assert t.status == "RETRACTED"
    assert t.answers["factually_established"].value == NOT_ESTABLISHED


def test_an_unknown_memory_holds_nothing(tmp_path):
    log, ev, _ = _world(tmp_path)
    t = assess(log, "m-none", evidence=ev)
    assert t.status is None
    assert not any(a.holds for k, a in t.answers.items()
                   if k != "hash_consistent")


# ---- the two readers, held together -------------------------------------------

#: (status before, move, by whom, refused?). Refused or not is the PRIMARY's
#: rule; the second reader must say the same.
MOVES = [
    (None, "RETRACTED", "alice", False),
    (None, "STALE", "bob", False),           # a premise gone: anybody's
    ("STALE", "STALE", "alice", False),
    (None, "RETRACTED", "bob", True),        # M-2
    (None, "SUPERSEDED", "bob", True),       # M-2
    ("STALE", "ACTIVE", "alice", True),      # M-2
    (None, "BOGUS", "alice", True),          # M-2
    ("RETRACTED", "ACTIVE", "alice", True),
    ("RETRACTED", "STALE", "alice", True),
]


@pytest.mark.parametrize("before,move,actor,refused", MOVES)
def test_both_readers_refuse_the_same_status_moves(tmp_path, before, move,
                                                   actor, refused):
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    if before:
        log.append(actor="alice", action=ACT_MEMORY_STATUS, target="m1",
                   payload={"memory_id": "m1", "status": before,
                            "reason": "r"})
    log.append(actor=actor, action=ACT_MEMORY_STATUS, target="m1",
               payload={"memory_id": "m1", "status": move, "reason": "r"})
    try:
        MemoryStore(EventLog(log.path), evidence=ev).load()
        primary_refused = False
    except MemoryError_:
        primary_refused = True
    second = rc.reconstruct_subsystems(log)
    assert primary_refused is refused
    assert bool(second.anomalies) is refused, second.anomalies
    t = assess(log, "m1", evidence=ev)
    assert (t.answers["consistently_reconstructed"].value == NO) is refused


@pytest.mark.parametrize("payload,why", [
    ({"memory_id": "m-none", "status": "STALE"}, "unknown memory 'm-none'"),
    ({"status": "STALE"}, "unknown memory None"),
    ({"memory_id": "m1", "status": "BOGUS"}, "unknown status 'BOGUS'"),
])
def test_a_malformed_status_record_is_refused_not_raised(tmp_path, payload,
                                                         why):
    """Each of these escaped load() as a raw KeyError or ValueError, past
    every caller that handles a refusal."""
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    log.append(actor="alice", action=ACT_MEMORY_STATUS, target="m1",
               payload=payload)
    with pytest.raises(MemoryError_, match=why):
        MemoryStore(EventLog(log.path), evidence=ev).load()
    assert rc.reconstruct_subsystems(log).anomalies


def test_both_readers_refuse_an_unknown_status_at_birth(tmp_path):
    log, ev, _ = _world(tmp_path)
    rec = MemoryEntry(memory_id="m1", text=TEXT, author="alice").to_record()
    rec["status"] = "BOGUS"
    log.append(actor="alice", action=ACT_MEMORY_WRITE, target="m1",
               payload={"entry": rec})
    with pytest.raises(MemoryError_, match="BOGUS"):
        MemoryStore(log, evidence=ev).load()
    assert any("unknown status 'BOGUS'" in a
               for a in rc.reconstruct_subsystems(log).anomalies)


def test_a_second_reader_that_disagrees_is_reported(tmp_path, monkeypatch):
    """A TEST DOUBLE, said so: with M-1 and M-2 repaired, no history is
    known on which the primary accepts an entry the second reader refuses
    or does not hold. The comparison is held against a second reader made
    to disagree, so that it is not lost before the next divergence is."""
    log, ev, mem = _world(tmp_path)
    mem.remember(memory_id="m1", text=TEXT, author="alice")
    real = MT.reconstruct_subsystems

    def forgetting(log_):
        out = real(log_)
        out.memory.pop("m1")
        return out

    monkeypatch.setattr(MT, "reconstruct_subsystems", forgetting)
    t = assess(log, "m1", evidence=ev)
    assert t.answers["consistently_reconstructed"].value == NO
    assert "does not hold it" in t.answers["consistently_reconstructed"].why

    def flagging(log_):
        out = real(log_)
        out.anomalies.append("seq 0: memory 'm1' disputed")
        return out

    monkeypatch.setattr(MT, "reconstruct_subsystems", flagging)
    t = assess(log, "m1", evidence=ev)
    assert t.answers["consistently_reconstructed"].value == NO
    assert "disputed" in t.answers["consistently_reconstructed"].why
