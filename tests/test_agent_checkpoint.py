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
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.checkpoint import (  # noqa: E402
    Checkpoint, CheckpointAheadOfLog, CheckpointCorrupt, CheckpointError,
    CheckpointMismatch, CheckpointStore,
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
    with pytest.raises(ChainBroken):
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


def test_a_checkpoint_for_a_different_log_is_refused(tmp_path):
    log_a = _log(tmp_path, n=8, name="a.jsonl")
    log_b = _log(tmp_path, n=2, name="b.jsonl")
    cp = cp_mod.create(log_a)
    with pytest.raises(CheckpointAheadOfLog):
        cp_mod.check_against(log_b, cp)


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
    assert cp.seq == log.verify().head_seq
    restored = AuthorityStore.load_from(
        EventLog(tmp_path / "log.jsonl"), checkpoints, blobs=evidence,
        evidence=evidence, require_checkpoint=True)
    assert restored.get("r1").state is store.get("r1").state
