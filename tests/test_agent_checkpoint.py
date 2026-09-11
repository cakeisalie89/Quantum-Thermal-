"""Checkpoints: caching a verification result without caching trust.

The tests that matter most here are the ones asserting what a checkpoint
*cannot* do. A cache whose limits are only described in a docstring becomes a
cache whose limits nobody knows, and this one caches the answer to "has this
log been tampered with".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qta_agent import checkpoint as cp_mod  # noqa: E402
from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.canonical import canonical_bytes, digest  # noqa: E402
from dataclasses import replace  # noqa: E402
from qta_agent.checkpoint import (  # noqa: E402
    Checkpoint, CheckpointAheadOfLog, CheckpointCorrupt, CheckpointError,
    CheckpointMismatch, CheckpointStore, check_against, create,
)
from qta_agent.events import ChainBroken, EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.store import AuthorityStore, StoreError  # noqa: E402


def _log(tmp_path, n=5, name="ev.jsonl"):
    log = EventLog(tmp_path / name)
    for i in range(n):
        log.append(actor="a", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "a", "i": i})
    return log


# --- what a checkpoint buys -------------------------------------------------

def test_verifying_from_a_checkpoint_skips_the_prefix(tmp_path):
    log = _log(tmp_path, n=5)
    cp = cp_mod.create(log)
    for i in range(5, 8):
        log.append(actor="a", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "a", "i": i})

    rep = cp_mod.verify_with(log, cp)
    assert rep.ok
    assert rep.count == 3, "only the records after the checkpoint were read"
    assert rep.head_seq == 7


def test_an_incremental_report_says_it_is_incremental(tmp_path):
    """The field exists so the two results cannot be confused for each other."""
    log = _log(tmp_path, n=4)
    cp = cp_mod.create(log)

    weak = cp_mod.verify_with(log, cp)
    assert weak.ok and weak.prefix_verified is False
    assert weak.unverified_through == cp.seq

    strong = log.verify()
    assert strong.ok and strong.prefix_verified is True
    assert strong.unverified_through == -1


def _tamper_in_place(log, index, new_value):
    """Alter a record's payload WITHOUT changing the line's byte length.

    Length matters: the anchor carries byte offsets, so an edit that changes
    the prefix's length shifts everything after it and the anchor stops
    lining up. That incidental detection is real but it is not the property
    under test here, and relying on it would be relying on an attacker
    choosing an inconvenient edit.
    """
    lines = log.path.read_text().splitlines()
    before = len(lines[index])
    rec = json.loads(lines[index])
    rec["payload"]["i"] = new_value
    lines[index] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    assert len(lines[index]) == before, (
        "the substitution changed the line length; pick an equal-length value")
    log.path.write_text("\n".join(lines) + "\n")


def test_tampering_before_the_anchor_is_invisible_incrementally(tmp_path):
    """THE property to understand before relying on a checkpoint.

    A record inside the checkpointed prefix is altered, keeping its byte
    length so the offsets still line up. The incremental check reports ``ok``
    -- correctly, because it never claimed to have looked -- and the full
    check finds it. Both halves are asserted together so the limit cannot be
    read as a bug in one or a guarantee in the other.
    """
    log = _log(tmp_path, n=6)
    cp = cp_mod.create(log)
    _tamper_in_place(log, 1, 9)          # "i": 1 -> "i": 9, same width

    weak = cp_mod.verify_with(log, cp)
    assert weak.ok, "the incremental check does not read the prefix at all"
    assert weak.prefix_verified is False, (
        "...and it must say so, or this result is a lie by omission")

    strong = log.verify()
    assert not strong.ok
    assert any("was altered" in p for p in strong.problems), strong.problems


def test_a_length_changing_prefix_edit_is_caught_by_the_offsets(tmp_path):
    """Incidental, and worth pinning so it is not mistaken for the guarantee.

    Byte offsets exist to make the tail reachable without reading the prefix.
    A prefix edit that changes its length therefore breaks them -- so this
    class of tampering IS caught incrementally. It is a side effect of the
    seek, not a security property: an attacker who preserves the length gets
    past it, as the test above shows.
    """
    log = _log(tmp_path, n=6)
    cp = cp_mod.create(log)
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["i"] = 999999        # wider than "1"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    log.path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ChainBroken):
        cp_mod.verify_with(log, cp)


def test_appending_incrementally_produces_a_fully_verifiable_log(tmp_path):
    """The cheap writer must not produce a log the strict reader rejects."""
    log = EventLog(tmp_path / "ev.jsonl")
    log.append(actor="a", action="record.create", target="r0",
               payload={"record_id": "r0", "kind": "k", "proposer": "a"})
    anchor = log.anchor_at(0)
    for i in range(1, 25):
        _, anchor = log.append_verified(
            anchor, actor="a", action="record.create", target=f"r{i}",
            payload={"record_id": f"r{i}", "kind": "k", "proposer": "a"})

    rep = log.verify()
    assert rep.ok, rep.problems
    assert rep.count == 25 and rep.prefix_verified is True


def test_append_verified_refuses_a_broken_tail(tmp_path):
    log = _log(tmp_path, n=4)
    anchor = log.anchor_at(1)
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[3])
    rec["payload"]["i"] = 999
    lines[3] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    log.path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ChainBroken, match="broken chain"):
        log.append_verified(anchor, actor="a", action="record.create",
                            target="rX", payload={})


def test_an_older_anchor_costs_more_and_is_still_correct(tmp_path):
    """Rollback to an old checkpoint is safe, only slower."""
    log = _log(tmp_path, n=10)
    early = log.anchor_at(2)
    rep = log.verify_from(early)
    assert rep.ok and rep.count == 7 and rep.head_seq == 9
    assert rep.unverified_through == 2


# --- an anchor is checked, never trusted ------------------------------------

def test_an_anchor_pointing_at_the_wrong_record_is_refused(tmp_path):
    log = _log(tmp_path, n=5)
    good = log.anchor_at(3)
    forged = type(good)(good.seq, "b" * 64, good.record_offset,
                        good.next_offset)
    with pytest.raises(ChainBroken, match="anchor expects hash"):
        log.verify_from(forged)


def test_an_anchor_whose_offsets_no_longer_line_up_is_refused(tmp_path):
    """A rewritten log moves the bytes, so the seek lands mid-record."""
    log = _log(tmp_path, n=5)
    anchor = log.anchor_at(3)
    log.path.write_text("x" + log.path.read_text())   # shift everything by 1
    with pytest.raises(ChainBroken, match="does not describe the bytes"):
        log.verify_from(anchor)


def test_an_anchor_past_the_end_of_the_log_is_reported_as_truncation(tmp_path):
    log = _log(tmp_path, n=5)
    anchor = log.anchor_at(4)
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:3]) + "\n")
    with pytest.raises(ChainBroken, match="TRUNCATED"):
        log.verify_from(anchor)


def test_anchoring_at_a_seq_the_log_does_not_have_fails(tmp_path):
    log = _log(tmp_path, n=3)
    with pytest.raises(Exception, match="no record at seq 9"):
        log.anchor_at(9)


def test_anchoring_on_an_altered_record_is_refused(tmp_path):
    """An anchor is only worth making from a record that hashes correctly."""
    log = _log(tmp_path, n=4)
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[2])
    rec["payload"]["i"] = 999
    lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken, match="does not hash to its own"):
        log.anchor_at(2)


# --- creating a checkpoint --------------------------------------------------

def test_a_broken_log_cannot_be_checkpointed(tmp_path):
    """A checkpoint past a break would make the break permanently invisible."""
    log = _log(tmp_path, n=5)
    lines = log.path.read_text().splitlines()
    del lines[2]
    log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken, match="refusing to checkpoint"):
        cp_mod.create(log)


def test_an_empty_log_cannot_be_checkpointed(tmp_path):
    log = EventLog(tmp_path / "empty.jsonl")
    log.path.write_text("")
    with pytest.raises(CheckpointError, match="empty log"):
        cp_mod.create(log)


def test_a_checkpoint_records_whether_it_was_fully_verified(tmp_path):
    """Weaker provenance travels with the checkpoint instead of being lost."""
    log = _log(tmp_path, n=3)
    assert cp_mod.create(log).full_verification is True
    weak = cp_mod.create(log, require_full_verification=False)
    assert weak.full_verification is False
    assert weak.hash != "" and weak.recompute_hash() == weak.hash


def test_a_non_digest_state_digest_is_refused(tmp_path):
    log = _log(tmp_path, n=3)
    with pytest.raises(CheckpointError, match="sha256 digest or None"):
        cp_mod.create(log, state_digest="not-a-digest")


# --- the checkpoint file ----------------------------------------------------

def test_a_checkpoint_round_trips_byte_for_byte(tmp_path):
    log = _log(tmp_path, n=4)
    store = CheckpointStore(tmp_path / "cp")
    cp = cp_mod.create(log)
    store.write(cp)
    assert store.read(cp.seq) == cp


def test_an_altered_checkpoint_file_is_detected(tmp_path):
    log = _log(tmp_path, n=4)
    store = CheckpointStore(tmp_path / "cp")
    cp = cp_mod.create(log)
    path = store.write(cp)
    rec = json.loads(path.read_text())
    rec["seq"] = 1                       # hash left stale
    path.write_text(json.dumps(rec))
    with pytest.raises(CheckpointCorrupt, match="was altered"):
        store.read(cp.seq)


def test_the_checkpoint_hash_does_not_authenticate_it(tmp_path):
    """Recorded as a LIMIT, not a passing property.

    Anyone who can rewrite the checkpoint file can also recompute its hash.
    The self-hash catches a truncated write or a bad disk; it catches nothing
    an adversary does. This test exists so that a reader who assumes
    otherwise is contradicted by the suite rather than by an incident.

    The defence against a hostile filesystem is ``EventLog.verify``, which
    needs no checkpoint and trusts nothing -- asserted here alongside.
    """
    log = _log(tmp_path, n=5)
    store = CheckpointStore(tmp_path / "cp")
    cp = cp_mod.create(log)
    path = store.write(cp)

    rec = json.loads(path.read_text())
    rec["state_digest"] = "f" * 64                  # a lie
    body = {k: rec[k] for k in rec if k != "hash"}
    rec["hash"] = digest(body)                      # ...consistently told
    path.write_text(json.dumps(rec))

    forged = store.read(cp.seq)                     # accepted: it self-hashes
    assert forged.state_digest == "f" * 64

    # And the thing that does not care what the checkpoint says:
    assert log.verify().ok


def test_an_unhashed_extra_field_in_a_checkpoint_is_refused(tmp_path):
    """The same rule the event log applies: unhashed content is not content."""
    log = _log(tmp_path, n=3)
    store = CheckpointStore(tmp_path / "cp")
    cp = cp_mod.create(log)
    path = store.write(cp)
    rec = json.loads(path.read_text())
    rec["trust_me"] = True
    path.write_text(json.dumps(rec))
    with pytest.raises(CheckpointCorrupt, match="unhashed extra fields"):
        store.read(cp.seq)


@pytest.mark.parametrize("field_, value", [
    ("seq", -1), ("seq", "3"), ("seq", True),
    ("head_hash", "F" * 64), ("head_hash", "zz"),
    ("record_offset", -5), ("next_offset", 0),
    ("state_digest", "nope"), ("canonical_form_version", "1"),
    ("full_verification", 1),
])
def test_a_structurally_invalid_checkpoint_is_refused(tmp_path, field_, value):
    log = _log(tmp_path, n=3)
    store = CheckpointStore(tmp_path / "cp")
    cp = cp_mod.create(log)
    path = store.write(cp)
    rec = json.loads(path.read_text())
    rec[field_] = value
    body = {k: rec[k] for k in rec if k != "hash"}
    rec["hash"] = digest(body)          # valid hash over invalid content
    path.write_text(json.dumps(rec))
    with pytest.raises(CheckpointCorrupt):
        store.read(cp.seq)


def test_an_unparseable_checkpoint_is_reported_not_ignored(tmp_path):
    store = CheckpointStore(tmp_path / "cp")
    store.root.mkdir(parents=True)
    store._path(3).write_text("{not json")
    with pytest.raises(CheckpointCorrupt, match="unparseable"):
        store.read(3)
    assert store.seqs() == [3], "a corrupt checkpoint is still present"
    audit = store.audit()
    assert not audit.ok and audit.count == 0 and len(audit.problems) == 1


def test_latest_walks_back_past_a_corrupt_checkpoint(tmp_path):
    """An older valid checkpoint beats none; the corrupt one is still audited."""
    log = _log(tmp_path, n=3)
    store = CheckpointStore(tmp_path / "cp")
    good = cp_mod.create(log)
    store.write(good)
    store._path(99).write_text("{not json")

    assert store.seqs() == [good.seq, 99]
    assert store.latest() == good
    assert not store.audit().ok


def test_a_checkpoint_ahead_of_the_log_is_refused(tmp_path):
    """Records the checkpoint covered have been removed."""
    log = _log(tmp_path, n=6)
    cp = cp_mod.create(log)
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:3]) + "\n")
    with pytest.raises(CheckpointAheadOfLog, match="missing"):
        cp_mod.check_against(log, cp)


def test_a_checkpoint_for_a_SHORTER_different_log_is_refused_by_size(tmp_path):
    """And ONLY by size -- which is the whole of what `check_against` knows.

    The name this test used to carry said "for a different log", which is
    more than it shows: `log_b` is refused for being two records long, not
    for being a different log. A different log of ADEQUATE length passes
    here, and that gap was D-2026-34. The pair that shows it is
    `test_could_describe_and_does_describe_are_different_questions`.
    """
    log_a = _log(tmp_path, n=8, name="a.jsonl")
    log_b = _log(tmp_path, n=2, name="b.jsonl")
    cp = cp_mod.create(log_a)
    with pytest.raises(CheckpointAheadOfLog) as exc:
        cp_mod.check_against(log_b, cp)
    assert "byte(s) are missing" in str(exc.value), (
        "refused for some reason other than the log being too short, so "
        "this test is not about what it says it is about")


def test_a_checkpoint_from_another_canonical_form_is_refused(tmp_path):
    log = _log(tmp_path, n=3)
    cp = cp_mod.create(log)
    other = Checkpoint(
        cp.seq, cp.head_hash, cp.record_offset, cp.next_offset,
        cp.state_digest, cp.created, cp.canonical_form_version + 1,
        cp.full_verification)
    with pytest.raises(CheckpointMismatch, match="canonical form"):
        cp_mod.check_against(log, object.__new__(Checkpoint) if False else other)


def test_latest_usable_skips_a_checkpoint_that_does_not_fit_the_log(tmp_path):
    log = _log(tmp_path, n=8)
    store = CheckpointStore(tmp_path / "cp")
    early = cp_mod.create(log)
    store.write(early)

    for i in range(8, 12):
        log.append(actor="a", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "a"})
    late = cp_mod.create(log)
    store.write(late)

    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:9]) + "\n")   # drop back below `late`

    assert store.latest().seq == late.seq, "newest by name is still the newest"
    usable = store.latest_usable(log)
    assert usable is not None and usable.seq == early.seq, (
        "the newest checkpoint no longer describes this log")


# --- the store ---------------------------------------------------------------

def _promoted_store(tmp_path):
    log = EventLog(tmp_path / "l.jsonl")
    blobs = EvidenceStore(tmp_path / "blobs")
    report = blobs.put(b'{"result":"verified","gates":83,"pass":0}')
    s = AuthorityStore(log, evidence=blobs).load()
    s.create(record_id="r1", kind="claim", proposer="p",
             idempotency_key="create-r1")
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="v",
                 role=Role.VERIFIER)
    s.transition(record_id="r1", dst=State.VERIFIED, actor="v",
                 role=Role.VERIFIER, evidence={"verification_report": report})
    return log, blobs, s, report


def test_a_checkpointed_load_agrees_with_a_full_load(tmp_path):
    log, blobs, s, report = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    s.checkpoint(cps)
    s.transition(record_id="r1", dst=State.PROMOTED, actor="pm",
                 role=Role.PROMOTER, policy_id="pol-1",
                 evidence={"verification_report": report,
                           "policy_id": "pol-1"})

    cheap = AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)
    full = AuthorityStore(log, evidence=blobs).load()
    assert cheap.snapshot() == full.snapshot()
    assert cheap.get("r1").state is State.PROMOTED


def test_a_checkpointed_load_reports_that_it_skipped_the_prefix(tmp_path):
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    s.checkpoint(cps)
    cheap = AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)
    assert cheap.loaded_prefix_verified is False
    assert AuthorityStore(log, evidence=blobs).load().loaded_prefix_verified


def test_idempotency_keys_survive_a_checkpointed_load(tmp_path):
    """The subtle half of the snapshot: idempotency IS part of the state.

    A snapshot that dropped the applied keys would let a replayed request
    apply a second time -- the exact failure idempotency keys exist to
    prevent, reintroduced by the optimisation meant to be invisible.
    """
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    s.checkpoint(cps)

    cheap = AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)
    before = cheap.get("r1").revision
    again = cheap.create(record_id="r1", kind="claim", proposer="p",
                         idempotency_key="create-r1")    # already applied
    assert again.revision == before, (
        "a retried create must return the existing record, not raise")
    assert cheap.get("r1").revision == before
    assert cheap.log.verify().count == log.verify().count, (
        "a replayed idempotent request appended a second event")


def test_load_from_falls_back_to_a_full_load_when_no_checkpoint(tmp_path):
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    loaded = AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)
    assert loaded.get("r1").state is State.VERIFIED
    assert loaded.loaded_prefix_verified is True, (
        "the fallback is a real full load, and says so")


def test_require_checkpoint_turns_a_silent_fallback_into_an_error(tmp_path):
    log, blobs, _, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    with pytest.raises(StoreError, match="no usable checkpoint"):
        AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs,
                                 require_checkpoint=True)


def test_a_tampered_snapshot_blob_is_refused_by_the_evidence_store(tmp_path):
    """The snapshot is evidence, and is stored the one way evidence is."""
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    cp = s.checkpoint(cps)
    blobs._blob_path(cp.state_digest).write_bytes(b'{"records":{}}')
    with pytest.raises(Exception, match="hash to"):
        AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)


def test_a_snapshot_that_covers_a_different_seq_is_refused(tmp_path):
    """The checkpoint and the snapshot it pins must describe the same moment."""
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    snap = s.snapshot()
    snap["loaded_through"] = 0                      # claims an earlier moment
    from qta_agent.canonical import canonical_bytes
    dg = blobs.put(canonical_bytes(snap), media_type="application/json")
    cp = cp_mod.create(log, state_digest=dg)
    cps.write(cp)
    with pytest.raises(StoreError, match="does not describe"):
        AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)


def test_a_checkpoint_pinning_no_snapshot_cannot_restore_a_projection(tmp_path):
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    cps.write(cp_mod.create(log))                   # state_digest is None
    with pytest.raises(StoreError, match="pins no snapshot"):
        AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)


def test_checkpointing_without_a_blob_store_is_refused(tmp_path):
    log = EventLog(tmp_path / "l.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="k", proposer="p")
    with pytest.raises(StoreError, match="needs a blob store"):
        s.checkpoint(CheckpointStore(tmp_path / "cp"))


@pytest.mark.parametrize("bad", [
    # wrong version -- refuse rather than guess at an unknown shape
    {"snapshot_version": 2, "records": {}, "applied_keys": {},
     "loaded_through": 0},
    # no version at all
    {"records": {}, "applied_keys": {}, "loaded_through": 0},
    # records must be a mapping
    {"snapshot_version": 1, "records": [], "applied_keys": {},
     "loaded_through": 0},
    # applied_keys is a mapping now that a key records its target
    {"snapshot_version": 1, "records": {}, "applied_keys": [],
     "loaded_through": 0},
    # ...whose values name a record
    {"snapshot_version": 1, "records": {}, "applied_keys": {"k": 7},
     "loaded_through": 0},
    # a bool is an int in Python and is not a seq
    {"snapshot_version": 1, "records": {}, "applied_keys": {},
     "loaded_through": True},
    # a record whose body is not an object
    {"snapshot_version": 1, "records": {"r1": "not an object"},
     "applied_keys": {}, "loaded_through": 0},
    # a record missing a required field
    {"snapshot_version": 1, "records": {"r1": {"record_id": "r1"}},
     "applied_keys": {}, "loaded_through": 0},
])
def test_a_malformed_snapshot_is_refused_rather_than_guessed_at(tmp_path, bad):
    s = AuthorityStore(EventLog(tmp_path / "l.jsonl"))
    with pytest.raises(StoreError):
        s._restore(bad)


def test_a_snapshot_key_that_disagrees_with_its_record_is_refused(tmp_path):
    log, blobs, s, _ = _promoted_store(tmp_path)
    snap = s.snapshot()
    snap["records"]["r2"] = snap["records"].pop("r1")   # key says r2, body r1
    with pytest.raises(StoreError, match="disagrees with the record"):
        AuthorityStore(log)._restore(snap)


# ---------------------------------------------------------------------------
# Mutation-isolating tests.
#
# Four mutations survived the first run of tools/mutations/agent_checkpoint.json.
# All four were masked by an adjacent check firing on the same fixture, which is
# the recurring failure in this suite's history: the tests proved that SOMETHING
# rejected the input, not that the specific rule did.
# ---------------------------------------------------------------------------

def test_a_rolled_back_head_witness_invalidates_a_checkpoint(tmp_path):
    """C5: the witness check, isolated from the byte-length check.

    Truncating the log shrinks the file, so the offset check catches it first
    and the witness comparison never runs. Here the log is left completely
    intact and only the separately-held witness is rolled back -- which is what
    an attacker who wants the system to forget recent records would do, since
    the witness is the thing that would otherwise notice.
    """
    log = _log(tmp_path, n=8)
    cp = cp_mod.create(log)
    assert cp.seq == 7

    early = log.anchor_at(3)
    log.head_path.write_text(
        json.dumps({"seq": early.seq, "head_hash": early.head_hash}),
        encoding="utf-8")

    with pytest.raises(CheckpointAheadOfLog, match="head witness records"):
        cp_mod.check_against(log, cp)
    # Isolation: the checkpoint ends exactly at EOF, so the byte-length check
    # cannot be what refused it. Only the witness disagrees.
    assert log.path.stat().st_size == cp.next_offset


def test_check_anchor_binds_an_anchor_to_a_log_and_verifies_nothing_else(
        tmp_path):
    """The primitive underneath `describes`, and the limit of it.

    It answers one question -- is the record this anchor names the record at
    that offset in THIS log -- and it must answer it without reading the
    prefix or the tail. A damaged record on either side of the anchor is
    therefore invisible to it, and `verify` is what finds those. Asserting
    that here keeps the cheap check from being mistaken for the strong one:
    the two live one method apart.
    """
    log = _log(tmp_path, n=8)
    anchor = log.anchor_at(3)

    assert log.check_anchor(anchor).seq == 3

    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[6])
    rec["actor"] = "somebody-else"                  # AFTER the anchor
    lines[6] = json.dumps(rec, separators=(",", ":"), sort_keys=True)
    rec0 = json.loads(lines[0])
    rec0["actor"] = "somebody-else"                 # and BEFORE it
    lines[0] = json.dumps(rec0, separators=(",", ":"), sort_keys=True)
    log.path.write_text("\n".join(lines) + "\n")

    assert not log.verify().ok, (
        "the fixture does not set up the case: nothing is damaged, so "
        "check_anchor passing below says nothing about its scope")
    # Unchanged bytes at the anchor, so it still binds -- by design.
    assert log.check_anchor(log.anchor_at(3)).seq == 3


def test_an_anchor_claiming_the_wrong_seq_is_refused(tmp_path):
    """E22: seq, with the hash deliberately correct.

    A wrong seq normally comes with a wrong hash, so the hash check fires
    first and the seq check is never reached. Here the anchor names the real
    record's real hash at the real offsets and lies only about which position
    that record occupies -- which would shift every following record's
    expected seq by the same amount.
    """
    log = _log(tmp_path, n=6)
    good = log.anchor_at(3)
    lying = type(good)(1, good.head_hash, good.record_offset, good.next_offset)
    with pytest.raises(ChainBroken, match=r"anchor claims seq 1 but the record"):
        log.verify_from(lying)


def test_the_anchored_record_is_rehashed_not_taken_on_faith(tmp_path):
    """E23: the record the entire tail chains from must hash to its own hash.

    ``anchor_at`` refuses to build an anchor on an altered record, which is
    why this survived -- every anchor in the other tests comes from there. But
    an anchor is a plain value a caller can also construct, or restore from a
    checkpoint file that nothing authenticates. So the record is re-hashed at
    the point of use, not only at the point of manufacture.

    The fixture keeps the stale stored hash and points the anchor at it, so
    seq matches, the hash matches, the offsets match, and only the recompute
    disagrees.
    """
    log = _log(tmp_path, n=6)
    anchor = log.anchor_at(3)
    _tamper_in_place(log, 3, 8)          # payload changes; stored hash does not

    stale = type(anchor)(anchor.seq, anchor.head_hash, anchor.record_offset,
                         anchor.next_offset)
    with pytest.raises(ChainBroken,
                       match="the anchored record does not hash to its own"):
        log.verify_from(stale)


def test_an_anchor_whose_end_offset_is_wrong_is_named_as_such(tmp_path):
    """E24: assert WHICH rule refuses.

    A wrong end offset is caught either way -- the tail is then read from a
    position that does not start a record, and parsing or sequence contiguity
    fails. So this cannot be killed by outcome, only by diagnosis, and the
    diagnosis is the point: "the log was rewritten" sends an operator to look
    at the log, while "unparseable record after the anchor" sends them to look
    at a record that is perfectly fine.
    """
    log = _log(tmp_path, n=6)
    anchor = log.anchor_at(2)
    off_by_one = type(anchor)(anchor.seq, anchor.head_hash,
                              anchor.record_offset, anchor.next_offset + 1)
    with pytest.raises(ChainBroken, match="the log was rewritten"):
        log.verify_from(off_by_one)


def test_an_idempotency_key_cannot_be_reused_for_another_record(tmp_path):
    """S25, on create: the key records WHAT it completed, not merely that.

    With a set of used keys, a replay carrying a key that completed a
    different request would return whichever record the caller happened to
    name -- a wrong answer delivered as a success. The mapping makes it an
    error.
    """
    log = EventLog(tmp_path / "l.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="k", proposer="p", idempotency_key="k1")
    with pytest.raises(StoreError, match="already completed a request for"):
        s.create(record_id="r2", kind="k", proposer="p", idempotency_key="k1")
    assert "r2" not in s.all_records()


def test_an_idempotency_key_cannot_be_reused_across_transitions(tmp_path):
    """S25, on transition: the same rule at the other call site."""
    log = EventLog(tmp_path / "l.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="k", proposer="p")
    s.create(record_id="r2", kind="k", proposer="p")
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="v",
                 role=Role.VERIFIER, idempotency_key="t1")
    with pytest.raises(StoreError, match="already completed a request for"):
        s.transition(record_id="r2", dst=State.UNDER_REVIEW, actor="v",
                     role=Role.VERIFIER, idempotency_key="t1")
    assert s.get("r2").state is State.PROPOSED


def test_a_replayed_transition_returns_rather_than_repeating(tmp_path):
    """The half of idempotency that must still work after the reuse guard."""
    log = EventLog(tmp_path / "l.jsonl")
    s = AuthorityStore(log).load()
    s.create(record_id="r1", kind="k", proposer="p")
    first = s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="v",
                         role=Role.VERIFIER, idempotency_key="t1")
    count = log.verify().count
    again = s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="v",
                         role=Role.VERIFIER, idempotency_key="t1")
    assert again == first
    assert log.verify().count == count, "the replay appended a second event"


def test_a_checkpoint_describes_the_position_it_pins(tmp_path):
    """The projection may lag the log when other subsystems share it.

    ``AuthorityStore.checkpoint`` snapshotted whatever the projection had
    applied and pinned the log's CURRENT head. While the store was the only
    writer those were always the same position. On a shared log they are not,
    and the resulting checkpoint describes neither: ``load_from`` refuses it,
    correctly and long after the fact.
    """
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from qta_agent.checkpoint import CheckpointStore
    from qta_agent.events import EventLog
    from qta_agent.evidence import EvidenceStore
    from qta_agent.store import AuthorityStore

    log = EventLog(tmp_path / "log.jsonl")
    evidence = EvidenceStore(tmp_path / "evidence")
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    store = AuthorityStore(log, evidence=evidence).load()
    store.create(record_id="r1", kind="claim", proposer="alice")

    # Another subsystem writes to the same log; the store does not see it.
    log.append(actor="scheduler", action="scheduler.enqueue", target="j1",
               payload={"job": {"job_id": "j1", "work_digest": "0" * 64,
                                "submitter": "alice"}})
    assert store._loaded_through < log.verify().head_seq

    cp = store.checkpoint(checkpoints)
    # THE POSITION THE SNAPSHOT DESCRIBES, WHICH IS NO LONGER THE HEAD.
    #
    # This read `cp.seq == log.verify().head_seq` until D-2026-30, and the
    # intent has not changed: the checkpoint must pin the position its
    # snapshot actually covers, not a stale one. What moved is the head --
    # checkpoint() now appends a checkpoint.state record anchoring the claim
    # in the log, and that record lands one past the position it describes,
    # because a snapshot cannot contain the record announcing it.
    head = log.verify().head_seq
    assert cp.seq == head - 1
    (anchor,) = [e for e in log.read() if e.action == "checkpoint.state"]
    assert anchor.seq == head and anchor.payload["through_seq"] == cp.seq
    restored = AuthorityStore.load_from(
        EventLog(tmp_path / "log.jsonl"), checkpoints, blobs=evidence,
        evidence=evidence, require_checkpoint=True)
    assert restored.get("r1").state is store.get("r1").state


# ==========================================================================
# RETENTION, and appending while a checkpoint is being taken
#
# THE GAPS THESE CLOSE, stated as they were found:
#
#     "no cleanup or retention policy for old checkpoints"
#     "concurrent checkpoint-while-appending is untested"
#
# Retention is the interesting half. "Keep the newest N" is the obvious
# policy and it is unsafe here: the newest checkpoint is not necessarily a
# usable one, so counting backwards can delete the only point recovery could
# start from while carefully preserving several that are useless.
# ==========================================================================

def _many(tmp_path, n=12):
    """A log with ``n`` checkpoints taken along the way."""
    log = EventLog(tmp_path / "log.jsonl")
    store = CheckpointStore(tmp_path / "cp")
    for i in range(n):
        log.append(actor="a", action="record.create", target=f"t{i}",
                   payload={"record_id": f"t{i}", "state": "DRAFT",
                            "title": "x", "kind": "note"})
        store.write(create(log))
    return log, store


def _foreign(tmp_path, n, name="other.jsonl", pad=""):
    """A log sharing no content with `_many`'s, of a deliberately UNHELPFUL
    length: `pad` makes its records LONGER, so every size comparison passes
    and only content can tell the two logs apart."""
    other = EventLog(tmp_path / name)
    for i in range(n):
        other.append(actor="a", action="record.create", target=f"z{pad}{i}",
                     payload={"record_id": f"z{pad}{i}", "state": "DRAFT",
                              "title": f"x{pad}", "kind": "note"})
    return other


def test_pruning_keeps_the_newest_and_removes_the_rest(tmp_path):
    log, store = _many(tmp_path, n=12)
    before = store.seqs()
    assert len(before) == 12

    removed = store.prune(log, keep=4)

    assert sorted(removed) == before[:-4]
    assert store.seqs() == before[-4:]
    assert store.latest_usable(log) is not None


def test_pruning_below_the_keep_count_removes_nothing(tmp_path):
    log, store = _many(tmp_path, n=3)
    assert store.prune(log, keep=8) == ()
    assert len(store.seqs()) == 3


def test_a_keep_of_zero_is_refused(tmp_path):
    """A retention policy that can empty the store is deletion with a
    schedule."""
    log, store = _many(tmp_path, n=3)
    for bad in (0, -1, True, 1.5, None):
        with pytest.raises(CheckpointError) as exc:
            store.prune(log, keep=bad)
        assert "deletion with a schedule" in str(exc.value)
    assert len(store.seqs()) == 3


def test_pruning_protects_the_newest_USABLE_checkpoint(tmp_path):
    """THE defect a count-based policy would have.

    Several newer checkpoints describe a log this store no longer has. Under
    "keep the newest N" they survive and the one recovery could actually
    start from is deleted -- the store looks healthy and is empty of
    anything useful.
    """
    log, store = _many(tmp_path, n=6)
    good = store.latest().seq

    # Six more checkpoints for a DIFFERENT log, written into the same store.
    # They parse; they do not describe this log.
    other = EventLog(tmp_path / "other.jsonl")
    for i in range(6):
        other.append(actor="a", action="record.create", target=f"o{i}",
                     payload={"record_id": f"o{i}", "state": "DRAFT",
                              "title": "x", "kind": "note"})
        cp = create(other)
        store.write(replace(cp, seq=good + 10 + i,
                            hash=replace(cp, seq=good + 10 + i
                                         ).recompute_hash()))

    assert store.latest().seq > good, "the unusable ones are newer"
    assert store.latest_usable(log).seq == good

    store.prune(log, keep=3)

    assert store.latest_usable(log) is not None, (
        "pruning deleted the only checkpoint recovery could start from, "
        "while keeping newer ones that describe a different log")
    assert store.latest_usable(log).seq == good


def test_pruning_refuses_when_nothing_verifies_against_the_log(tmp_path):
    """Deleting on that basis acts on a conclusion the store cannot support:
    the LOG may be the thing that is wrong."""
    log, store = _many(tmp_path, n=10)
    # LONGER than _many's records, not shorter. This test used to set the
    # case up by making the foreign record short enough for the size
    # comparison to reject it -- a two-byte margin including a float
    # timestamp, which one hosted run lost, and the test failed with DID NOT
    # RAISE. Nothing here depends on length now (D-2026-34): these are
    # different logs because their CONTENT differs, and `describes` reads it.
    foreign = _foreign(tmp_path, n=1, name="foreign.jsonl", pad="zzzzzzz")
    assert store.latest_usable(foreign) is None, (
        "the fixture does not set up the case: something here describes the "
        "foreign log, so this would pass without the refusal under test")

    with pytest.raises(CheckpointError) as exc:
        store.prune(foreign, keep=2)
    assert "in doubt" in str(exc.value)
    assert len(store.seqs()) == 10, "it deleted while refusing"


def test_a_store_with_nothing_to_prune_does_not_ask_the_harder_question(
        tmp_path):
    """Below the keep count there is no pruning decision, so there is no
    refusal either.

    The two behaviours look independent and are not. `prune` REFUSES when no
    checkpoint verifies against the log, because deleting on that basis acts
    on a conclusion the store cannot support. That refusal is right when
    there is something to delete -- and wrong when there is not: a store
    holding fewer checkpoints than the retention count has no decision to
    make, and turning a no-op into an exception would fail every caller that
    prunes routinely on a young store.

    The early return is what separates them, and it survived a mutation
    because in the ordinary case it is a pure optimisation: with fewer
    entries than `keep`, everything is protected and the loop deletes
    nothing either way. The one case where it is load-bearing is this one,
    and nothing exercised it.
    """
    log, store = _many(tmp_path, n=2)
    # WHY THIS IS THE LONG FOREIGN LOG AND NOT A SHORT ONE.
    #
    # "Not usable against this log" used to be decided by byte offsets
    # alone: a checkpoint was refused when it ended past the end of the log
    # it was held against. So this fixture set the case up by being SHORT,
    # by a two-byte margin that included a float timestamp -- and its
    # sibling above lost that margin on a hosted run. Widening the margin
    # here closed the example; D-2026-34 closed the class, in production,
    # where "describes this log" now reads the record rather than the size.
    #
    # Being longer is the stronger fixture under that rule: the size
    # comparison passes, and the refusal comes from content or not at all.
    # The assertion stays anyway -- it is the thing that caught this.
    other = _foreign(tmp_path, n=1, pad="zzzzzzz")

    assert store.latest_usable(other) is None, (
        "the fixture does not set up the case: something here describes the "
        "other log, so this would pass without the branch under test")

    removed = store.prune(other, keep=3)

    assert removed == (), removed
    assert len(store.seqs()) == 2, "it deleted from a store below the floor"

    # AND THE OTHER SIDE, so this cannot pass by prune never refusing at all:
    # one more checkpoint takes the store above the keep count, and the same
    # call now refuses rather than guessing.
    for i in (9, 10):
        log.append(actor="a", action="record.create", target=f"t{i}",
                   payload={"record_id": f"t{i}", "state": "DRAFT",
                            "title": "x", "kind": "note"})
        store.write(create(log))
    assert len(store.seqs()) > 3, store.seqs()
    with pytest.raises(CheckpointError, match="in doubt"):
        store.prune(other, keep=3)


def test_appending_while_a_checkpoint_is_taken_leaves_both_consistent(
        tmp_path):
    """A checkpoint names a position; the log keeps moving past it.

    The property is not that the checkpoint is current -- it cannot be -- but
    that it is never WRONG: whatever position it names, the log's prefix
    through that position must still verify against it afterwards.
    """
    import threading

    log = EventLog(tmp_path / "log.jsonl")
    store = CheckpointStore(tmp_path / "cp")
    log.append(actor="a", action="record.create", target="t0",
               payload={"record_id": "t0", "state": "DRAFT", "title": "x",
                        "kind": "note"})

    stop = threading.Event()
    errors: list = []

    def appender():
        i = 0
        while not stop.is_set():
            try:
                log.append(actor="a", action="record.create",
                           target=f"w{i}",
                           payload={"record_id": f"w{i}", "state": "DRAFT",
                                    "title": "x", "kind": "note"})
            except Exception as exc:                    # pragma: no cover
                errors.append(f"append: {exc!r}")
                return
            i += 1

    t = threading.Thread(target=appender, daemon=True)
    t.start()
    try:
        taken = []
        for _ in range(40):
            try:
                taken.append(store.write(create(log)) and create(log).seq)
            except Exception as exc:                    # pragma: no cover
                errors.append(f"checkpoint: {exc!r}")
                break
    finally:
        stop.set()
        t.join(timeout=10)

    assert not errors, errors
    assert taken, "no checkpoint was taken at all"

    # EVERY checkpoint the store holds must still describe this log. A
    # checkpoint taken against a moving log that later fails to verify is
    # the failure this test exists to find.
    audit = store.audit()
    assert audit.ok, audit.problems
    for seq in store.seqs():
        check_against(log, store.read(seq))


def test_a_store_below_the_keep_count_is_not_verified_at_all(tmp_path):
    """Nothing to delete means nothing to decide.

    Isolated from the refusal test above deliberately. Both involve a log
    nothing verifies against, and they must answer differently: with more
    checkpoints than the policy keeps, prune has to choose which to delete
    and cannot, so it refuses. Below the keep count there is no choice to
    make, and verifying the log to reach that conclusion would turn a
    no-op into a failure for a store in perfectly good order.
    """
    log, store = _many(tmp_path, n=3)
    empty = EventLog(tmp_path / "empty.jsonl")
    empty.append(actor="a", action="record.create", target="z",
                 payload={"record_id": "z", "state": "DRAFT",
                          "title": "x", "kind": "note"})

    assert store.prune(empty, keep=8) == ()
    assert len(store.seqs()) == 3


# ---------------------------------------------------------------------------
# D-2026-30 (P1): a checkpoint pinned a snapshot and nothing anchored it.
#
# checkpoint.py says, in its own docstring:
#
#     WHAT AUTHENTICATES A CHECKPOINT
#     Nothing in this module. Read that sentence again before relying on one.
#
# and then, two paragraphs earlier:
#
#     a false statement about the log is still just a false statement -- it
#     cannot make a forged record authoritative, because anyone can re-run
#     the full verification and find the disagreement.
#
# The second sentence is true of the LOG and was false of the SNAPSHOT.
# Re-running the full verification confirms the log and finds no
# disagreement, because a forged snapshot is not in the log. The claim "the
# state at seq K is blob D" lived only in a file whose self-hash is
# recomputable by whoever can write the file.
#
# It is now also a record, under the hash chain.
# ---------------------------------------------------------------------------

def _forged_checkpoint(cp, state_digest):
    """The same checkpoint, pointing at a different snapshot, hash repaired.

    Exactly what somebody with write access to the checkpoint directory can
    produce, which is the threat the module's own docstring describes.
    """
    forged = replace(cp, state_digest=state_digest, hash="")
    return replace(forged, hash=forged.recompute_hash())


def test_a_checkpoint_is_anchored_by_a_record_in_the_log(tmp_path):
    """Anti-vacuity for everything below: the honest path writes the anchor."""
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    cp = s.checkpoint(cps)

    anchors = [ev for ev in log.read() if ev.action == "checkpoint.state"]
    assert len(anchors) == 1, [e.action for e in log.read()]
    (a,) = anchors
    assert a.payload["through_seq"] == cp.seq
    assert a.payload["state_digest"] == cp.state_digest
    assert a.payload["head_hash"] == cp.head_hash
    # ...and it lands AFTER the position it describes, because a snapshot
    # cannot contain the record announcing it.
    assert a.seq == cp.seq + 1


def test_a_forged_snapshot_is_refused_even_though_the_log_verifies(tmp_path):
    """The reproducer. Before D-2026-30 this loaded and read PROMOTED.

    Nothing is done to the log. It verifies, fully, at the end -- which is
    the whole point: the forgery was never in it.
    """
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    cp = s.checkpoint(cps)

    honest = json.loads(blobs.get(cp.state_digest).decode("utf-8"))
    honest["records"]["r1"]["state"] = State.PROMOTED.value
    honest["records"]["r1"]["policy_id"] = "pol-forged"
    forged_digest = blobs.put(canonical_bytes(honest),
                              media_type="application/json")
    cps.write(_forged_checkpoint(cp, forged_digest))

    assert log.verify().ok, "the log is untouched and still verifies"
    with pytest.raises(StoreError, match="the log records"):
        AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)


def test_the_forged_snapshot_would_otherwise_have_been_authoritative(tmp_path):
    """What the refusal above is worth, stated rather than implied.

    The forged snapshot promotes a record through an edge the gate refuses:
    PROPOSED -> PROMOTED is not in the table at all, and VERIFIED ->
    PROMOTED needs a distinct actor and an explicit policy. So the state it
    carries is not merely wrong, it is unreachable -- and a load that
    accepted it would have handed back a canonical record no history could
    produce.
    """
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    cp = s.checkpoint(cps)
    honest = json.loads(blobs.get(cp.state_digest).decode("utf-8"))
    assert honest["records"]["r1"]["state"] == State.VERIFIED.value
    honest["records"]["r1"]["state"] = State.PROMOTED.value
    forged = AuthorityStore(log, evidence=blobs)
    forged._restore(honest)
    assert sorted(forged.canonical()) == ["r1"], (
        "the snapshot this test forges really does make r1 canonical; "
        "without that the refusal above would be about nothing")


def test_a_checkpoint_with_no_anchor_in_the_log_is_refused(tmp_path):
    """A checkpoint file for a log that never recorded one.

    This is the shape a checkpoint written by an older build has, and the
    shape an attacker produces by writing a file into the directory. Both
    are refused, and for the same reason: a snapshot nothing in the hash
    chain vouches for is a file.
    """
    log, blobs, s, _ = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    payload = canonical_bytes(s.snapshot())
    dg = blobs.put(payload, media_type="application/json")
    cps.write(cp_mod.create(log, state_digest=dg))      # no record appended
    with pytest.raises(StoreError, match="not anchored in the log"):
        AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)


def test_an_anchor_for_a_different_position_does_not_vouch(tmp_path):
    """One position, one projection.

    An anchor exists in this log -- for an EARLIER checkpoint. A file naming
    a later position is not vouched for by it, and a check that only asked
    "is there an anchor anywhere" would pass this.
    """
    log, blobs, s, report = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    s.checkpoint(cps)                                    # anchored, at seq K
    s.transition(record_id="r1", dst=State.PROMOTED, actor="pm",
                 role=Role.PROMOTER, policy_id="pol-1",
                 evidence={"verification_report": report, "policy_id": "pol-1"})

    later = canonical_bytes(s.snapshot())
    dg = blobs.put(later, media_type="application/json")
    cps.write(cp_mod.create(log, state_digest=dg))        # unanchored, later
    with pytest.raises(StoreError, match="not anchored in the log"):
        AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)


def test_the_honest_checkpointed_load_still_works_after_all_that(tmp_path):
    """Anti-vacuity: a check that refused everything would pass every test
    above and break the feature entirely."""
    log, blobs, s, report = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    s.checkpoint(cps)
    s.transition(record_id="r1", dst=State.PROMOTED, actor="pm",
                 role=Role.PROMOTER, policy_id="pol-1",
                 evidence={"verification_report": report, "policy_id": "pol-1"})

    cheap = AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)
    full = AuthorityStore(log, evidence=blobs).load()
    assert cheap.snapshot() == full.snapshot()
    assert cheap.get("r1").state is State.PROMOTED


def test_a_later_anchor_in_the_tail_does_not_speak_for_this_checkpoint(
        tmp_path):
    """The position match, exercised where it is actually load-bearing.

    A checkpointed load reads the tail from its own anchor forward, and that
    tail contains every LATER checkpoint's anchor too. Without the match on
    ``through_seq`` the first one encountered would be compared against this
    checkpoint's snapshot, disagree, and refuse a load that is perfectly
    sound.

    An anchor for an EARLIER position can never appear in a later
    checkpoint's tail, which is why the obvious version of this test proves
    nothing: it sets up a case the reader cannot reach. This one is the
    reachable direction.
    """
    log, blobs, s, report = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    first = s.checkpoint(cps)

    s.transition(record_id="r1", dst=State.PROMOTED, actor="pm",
                 role=Role.PROMOTER, policy_id="pol-1",
                 evidence={"verification_report": report, "policy_id": "pol-1"})
    later = s.checkpoint(cps)
    assert later.seq > first.seq
    assert later.state_digest != first.state_digest, (
        "both checkpoints pin the same snapshot, so a mismatch could not be "
        "observed and this test would prove nothing")
    # Leave only the older checkpoint, so the load starts from it and reads
    # the later anchor on its way forward.
    (tmp_path / "cp" / f"{later.seq:012d}.checkpoint.json").unlink()
    assert cps.latest_usable(log).seq == first.seq

    restored = AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)
    assert restored.get("r1").state is State.PROMOTED


# ==========================================================================
# D-2026-34: "USABLE" WAS DECIDED BY THE LOG'S SIZE, NOT BY THE LOG
#
# `latest_usable` documented itself as "the newest checkpoint that both
# parses and DESCRIBES `log`" and tested that with `check_against`, which
# documents itself as answering whether a checkpoint COULD describe a log:
# it compares the checkpoint's end offset against `stat().st_size` and its
# seq against the head witness. Both are properties of the log's SHAPE.
#
# Two different logs of similar length have the same shape. So a checkpoint
# of one passed against the other, its offsets seeking into the middle of an
# unrelated record, and the two consumers of that answer acted on it:
#
#   * `prune` protects everything from the newest usable checkpoint upward
#     and REFUSES outright when nothing is usable, because deleting on that
#     basis acts on a conclusion the store cannot support. Against a foreign
#     log of ten longer records it did not refuse -- it found seq 9 "usable"
#     and deleted eight checkpoints while the log was exactly as in doubt as
#     the refusal describes.
#
#   * `load_from` walks backwards so that an older valid checkpoint is still
#     reachable when the newest is not. A foreign checkpoint that merely fit
#     stopped the walk at the top, and the load failed on it rather than
#     continuing to the one that could have restored the projection.
#
# The fix is that "describes" now means what the word means: `describes()`
# reads the one record at the checkpoint's offset and requires it to BE the
# record the checkpoint names. It is O(1) -- a seek and a line -- and still
# verifies nothing, which is why `check_against` keeps existing for
# `verify_with`, where a full verification follows immediately anyway.
#
# THE TEST DEFECT THIS ALSO CLOSES. Two tests below set up "no checkpoint
# describes this log" by making the foreign log's records SHORTER, so that
# the size comparison rejected them. That precondition held by a two-byte
# margin including a float timestamp whose JSON repr varies by a byte or
# three; one hosted run lost it and `test_pruning_refuses...` failed with
# DID NOT RAISE. The first fix widened the margin -- which closed the
# example, in a file where the same fixture shape appears twice. Now no
# margin is load-bearing at all: the records differ in CONTENT, which is
# what "a different log" actually means, and no timestamp can close that.
# ==========================================================================

def test_could_describe_and_does_describe_are_different_questions(tmp_path):
    """The paired test for the rule: `describes` must name a real condition.

    If `check_against` refused this pair too, the stronger check would be
    decoration and every test below would pass without it.
    """
    log, store = _many(tmp_path, n=10)
    other = _foreign(tmp_path, n=10, pad="zzzzzzz")
    cp = store.read(9)

    check_against(other, cp)            # accepts: the shape fits

    with pytest.raises(CheckpointMismatch) as exc:
        cp_mod.describes(other, cp)
    assert "does not describe this log" in str(exc.value)


def test_pruning_refuses_when_the_log_only_LOOKS_big_enough(tmp_path):
    """THE defect. Every checkpoint fits inside the foreign log by size, so
    the weak test called the newest usable and pruning proceeded -- deleting
    evidence in exactly the situation its refusal exists for."""
    log, store = _many(tmp_path, n=10)
    other = _foreign(tmp_path, n=10, pad="zzzzzzz")
    assert other.path.stat().st_size > log.path.stat().st_size, (
        "the fixture does not set up the case: the foreign log is smaller, "
        "so the size comparison alone would reject these checkpoints")
    for seq in store.seqs():
        check_against(other, store.read(seq))    # every one of them fits

    assert store.latest_usable(other) is None

    with pytest.raises(CheckpointError, match="in doubt"):
        store.prune(other, keep=2)
    assert len(store.seqs()) == 10, "it deleted while the log was in doubt"


def test_the_backwards_walk_passes_over_a_checkpoint_that_merely_fits(
        tmp_path):
    """Walking backwards is the whole design: an older valid checkpoint is
    strictly better than none. A foreign checkpoint at a HIGHER seq that fit
    by size stopped the walk at the top, so the older one that actually
    described the log was never reached."""
    log = EventLog(tmp_path / "log.jsonl")
    store = CheckpointStore(tmp_path / "cp")
    for i in range(6):
        log.append(actor="a", action="record.create", target=f"t{i}",
                   payload={"record_id": f"t{i}", "state": "DRAFT",
                            "title": "x", "kind": "note"})
        store.write(create(log))
    mine = store.latest().seq
    for i in range(6, 12):               # the log keeps moving; no new cps
        log.append(actor="a", action="record.create", target=f"t{i}",
                   payload={"record_id": f"t{i}", "state": "DRAFT",
                            "title": "x", "kind": "note"})

    other = _foreign(tmp_path, n=8)
    foreign = create(other)
    store.write(foreign)

    assert foreign.seq > mine, "the foreign checkpoint must be the newer one"
    check_against(log, foreign)          # and it fits this log by size
    assert foreign.head_hash != log.anchor_at(foreign.seq).head_hash, (
        "the two logs agree at that seq, so there is nothing to tell apart")

    assert store.latest_usable(log).seq == mine, (
        "the walk stopped at a checkpoint that does not describe this log, "
        "leaving the one that does unexamined below it")


def test_a_checkpointed_load_falls_back_to_one_it_can_actually_use(tmp_path):
    """The consumer harm, end to end: recovery failed on a checkpoint that
    only fit, while the checkpoint it needed was sitting underneath."""
    log, blobs, s, report = _promoted_store(tmp_path)
    cps = CheckpointStore(tmp_path / "cp")
    real = s.checkpoint(cps)
    s.transition(record_id="r1", dst=State.PROMOTED, actor="pm",
                 role=Role.PROMOTER, policy_id="pol-1",
                 evidence={"verification_report": report,
                           "policy_id": "pol-1"})

    # A checkpoint of a DIFFERENT log, newer by seq, small enough to fit
    # inside this one, pinning no snapshot -- so nothing could load from it.
    other = EventLog(tmp_path / "other.jsonl")
    for i in range(real.seq + 2):
        other.append(actor="a", action="record.create", target=f"z{i}",
                     payload={})
    foreign = create(other)
    cps.write(foreign)

    assert foreign.seq > real.seq
    assert foreign.state_digest is None
    check_against(log, foreign)          # the weak test accepts it

    restored = AuthorityStore.load_from(log, cps, blobs=blobs, evidence=blobs)
    assert restored.get("r1").state is State.PROMOTED
