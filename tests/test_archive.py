"""Archived history segments (Phase C): the log sealed in pieces, each
checkable alone against a tree checkpoint.

A segment is checked as the log's reader checks a line, as a chain from its
first link, and as the records at its positions of the tree a checkpoint
commits to. Everything a keeper of the archive could do to it is refused:
bytes changed, a record re-hashed and its chain repaired, records dropped,
reordered or moved, a frontier altered, a checkpoint the segment is not in,
one too old to cover it, and a manifest with a gap, an overlap, a splice of
two logs, or a segment missing. What verifies, verifies only as far as it
reaches: an archive of a prefix says so. And the archive authenticates no
one: a history that requires authentication still requires it after a
restore.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import archive as A  # noqa: E402
from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import transparency as T  # noqa: E402
from agent_substrate import witness as W  # noqa: E402
from agent_substrate.signature import ReferenceEd25519  # noqa: E402
from agent_substrate.canonical import ZERO_DIGEST, canonical_bytes  # noqa: E402
from agent_substrate.events import (  # noqa: E402
    PROFILE_AUTHENTICATED_REQUIRED, PROFILE_UNAUTHENTICATED_LEGACY, Event,
    EventLog,
)

LEGACY = PROFILE_UNAUTHENTICATED_LEGACY


def _log(path: Path, n: int, tag: str = "t") -> EventLog:
    log = EventLog(path)
    for i in range(n):
        log.append(actor="writer", action="note.fixture", target=f"{tag}{i}",
                   payload={"i": i})
    return log


def _events(log: EventLog) -> list:
    report, events = log.read_verified()
    report.raise_if_bad()
    return events


def _checkpoint(events: list, size: int | None = None) -> dict:
    size = len(events) if size is None else size
    return {"size": size,
            "root": T.root(T.entries_of(events[:size])).hex()}


def _proof(events: list, end: int, size: int) -> list:
    return W.tree_proof(events, end, size) if end < size else []


def _segment(rec, data, **kw):
    """One segment checked as a deployment that pins its log's profile
    checks it: these logs require no authentication."""
    kw.setdefault("profile", LEGACY)
    return A.verify_segment(rec, data, **kw)


def _reseal(rec: dict, data: bytes, **changes) -> dict:
    """A segment record re-sealed over altered bytes, as a keeper who
    controls the archive would re-seal it: digest, last hash and root all
    made to agree with the new bytes wherever they can be."""
    out = dict(rec, **changes)
    out["digest"] = hashlib.sha256(data).hexdigest()
    return out


@pytest.fixture
def world(tmp_path):
    log = _log(tmp_path / "log.jsonl", 23)
    manifest, blobs = A.seal(log, 5)
    return log, _events(log), manifest, blobs


# ---- what verifies ------------------------------------------------------------
def test_an_archive_verifies_from_genesis_and_restores_record_for_record(
        world, tmp_path):
    log, events, manifest, blobs = world
    before = log.path.read_bytes()
    assert [s["end"] - s["start"] for s in manifest["segments"]] == \
        [5, 5, 5, 5, 3]
    rep = A.verify_archive(manifest, blobs.__getitem__,
                           proof=[], **_checkpoint(events))
    assert rep.complete and rep.covered == 23
    assert [e.hash for e in rep.events] == [e.hash for e in events]
    assert rep.profile == PROFILE_UNAUTHENTICATED_LEGACY
    restored = tmp_path / "restored.jsonl"
    A.restore(manifest, blobs.__getitem__, restored, proof=[],
              **_checkpoint(events))
    assert restored.read_bytes() == b"".join(blobs[s["digest"]]
                                             for s in manifest["segments"])
    again = _events(EventLog(restored))
    assert [e.hash for e in again] == [e.hash for e in events]
    # sealing read the log and left it exactly as it was
    assert log.path.read_bytes() == before
    with pytest.raises(FileExistsError):
        A.restore(manifest, blobs.__getitem__, restored, proof=[],
                  **_checkpoint(events))


def test_one_segment_verifies_alone_against_a_later_checkpoint(tmp_path):
    """Each segment, with nothing of the log but its own bytes, a frontier
    and a proof of O(log n) hashes, is the log's records at its positions
    in a tree that grew after it was sealed."""
    log = _log(tmp_path / "log.jsonl", 40)
    manifest, blobs = A.seal(log, 8)
    for i in range(25):
        log.append(actor="writer", action="note.fixture", target=f"u{i}",
                   payload={"u": i})
    events = _events(log)
    size = len(events)
    for rec in manifest["segments"]:
        proof = _proof(events, rec["end"], size)
        got = _segment(rec, blobs[rec["digest"]], proof=proof,
                               **_checkpoint(events))
        assert [e.hash for e in got] == \
            [e.hash for e in events[rec["start"]:rec["end"]]]
        # what a segment needs beyond its records does not grow with n
        assert len(rec["frontier"]) <= max(1, rec["start"].bit_length())
        assert len(proof) <= 2 * size.bit_length()


def test_witnessed_checkpoint_composes_with_a_segment(tmp_path):
    """A reader holding one segment and a tree a witness cosigned: the
    cosignature checks with the witness's public key, and the segment
    checks against the tree it signs. The witness is a fixture with a
    TEST key."""
    log = _log(tmp_path / "log.jsonl", 12)
    events = _events(log)
    manifest, blobs = A.seal(log, 4)
    key = hashlib.sha256(b"test-witness-key:w1").digest()
    wit = W.TreeWitness("w1", key, tmp_path / "witness")
    wit.provision_tree(W.TreeCheckpoint(W.origin_of("lab-log", events), 1,
                                        T.root(T.entries_of(events[:1]))
                                        .hex()))
    cosig = wit.add_tree_checkpoint(W.tree_checkpoint_of("lab-log", events),
                                    W.tree_proof(events, 1, len(events)))
    assert ReferenceEd25519().verify(wit.public_key, cosig.message(),
                                       bytes.fromhex(cosig.signature))
    cp = cosig.checkpoint
    rec = manifest["segments"][1]
    got = _segment(rec, blobs[rec["digest"]], size=cp.size,
                           root=cp.root,
                           proof=_proof(events, rec["end"], cp.size))
    assert [e.seq for e in got] == [4, 5, 6, 7]


# ---- what a keeper of the segment cannot do -----------------------------------
def _lines(data: bytes) -> list:
    return [line + b"\n" for line in data[:-1].split(b"\n")]


def _rehashed(events: list, start: int, edit) -> bytes:
    """The segment's records with one edited, its hash recomputed and every
    later link in the segment repaired: a consistent forgery of the chain."""
    out, prev = [], events[0].prev_hash
    for i, ev in enumerate(events):
        body = ev.body()
        if i == start:
            body = edit(body)
        body["prev_hash"] = prev
        h = hashlib.sha256(canonical_bytes(body)).hexdigest()
        out.append(canonical_bytes(dict(body, hash=h)) + b"\n")
        prev = h
    return b"".join(out)


def _forgeries(rec, data, seg_events):
    lines = _lines(data)
    yield "a byte changed", rec, data[:10] + b"X" + data[11:], "not the ones"
    swapped = lines[1] + lines[0] + b"".join(lines[2:])
    yield "two records reordered", _reseal(rec, swapped), swapped, "seq"
    dropped = b"".join(lines[:-1])
    yield "the last record dropped", _reseal(rec, dropped), dropped, "lines"
    yield ("a record dropped and the end moved",
           _reseal(rec, dropped, end=rec["end"] - 1,
                   last_hash=seg_events[-2].hash), dropped, "another root")
    blank = lines[0] + b"\n" + b"".join(lines[1:-1])
    yield "a blank line for a record", _reseal(rec, blank), blank, "blank"
    spaced = lines[0][:-2] + b" }\n" + b"".join(lines[1:])
    yield "a line not canonical", _reseal(rec, spaced), spaced, "canonical"
    torn = data[:-1]
    yield "the last newline torn off", _reseal(rec, torn), torn, "newline"
    forged = _rehashed(seg_events, 2,
                       lambda b: dict(b, payload={"i": 999}))
    last = json.loads(_lines(forged)[-1])["hash"]
    yield ("a record edited, re-hashed, its chain repaired",
           _reseal(rec, forged, last_hash=last), forged, "another root")
    yield "the sealed root changed", dict(rec, root="ab" * 32), data, \
        "another root"
    yield "the sealed last hash changed", dict(rec, last_hash="cd" * 32), \
        data, "last record"


def test_every_forgery_of_a_segment_is_refused(world):
    _, events, manifest, blobs = world
    rec = manifest["segments"][2]
    data = blobs[rec["digest"]]
    seg_events = events[rec["start"]:rec["end"]]
    seen = 0
    for label, frec, fdata, why in _forgeries(rec, data, seg_events):
        with pytest.raises(A.ArchiveError, match=why):
            _segment(frec, fdata, proof=_proof(events, frec["end"],
                                                       len(events)),
                             **_checkpoint(events))
        seen += 1
    assert seen == 10


def test_a_forgery_with_its_root_re_sealed_is_refused_by_the_checkpoint(
        world):
    """The keeper re-seals everything the segment states, root included:
    the segment now agrees with itself, and is not in the checkpoint's
    tree."""
    _, events, manifest, blobs = world
    rec = manifest["segments"][1]
    seg_events = events[rec["start"]:rec["end"]]
    forged = _rehashed(seg_events, 0, lambda b: dict(b, target="forged"))
    fev = [Event(**json.loads(x)) for x in _lines(forged)]
    after = T.extend([bytes.fromhex(h) for h in rec["frontier"]],
                     rec["start"], T.entries_of(fev))
    frec = _reseal(rec, forged, last_hash=fev[-1].hash,
                   root=T.frontier_root(after, rec["end"]).hex())
    proof = _proof(events, rec["end"], len(events))
    with pytest.raises(A.ArchiveError, match="not in the tree"):
        _segment(frec, forged, proof=proof, **_checkpoint(events))


def test_a_segment_moved_or_given_a_wrong_frontier_is_refused(world):
    _, events, manifest, blobs = world
    rec = manifest["segments"][1]
    data = blobs[rec["digest"]]
    cp = _checkpoint(events)
    moved = dict(rec, start=6, end=11,
                 frontier=[h.hex() for h in T.frontier(
                     T.entries_of(events[:6]))])
    with pytest.raises(A.ArchiveError, match="seq"):
        _segment(moved, data, proof=_proof(events, 11, 23), **cp)
    bent = dict(rec, frontier=["ee" * 32] + rec["frontier"][1:])
    with pytest.raises(A.ArchiveError, match="another root"):
        _segment(bent, data, proof=_proof(events, 10, 23), **cp)
    short = dict(rec, frontier=rec["frontier"] + ["ee" * 32])
    with pytest.raises(A.ArchiveError, match="not the frontier"):
        _segment(short, data, proof=_proof(events, 10, 23), **cp)


def test_a_checkpoint_the_segment_is_not_in_is_refused(world, tmp_path):
    _, events, manifest, blobs = world
    rec = manifest["segments"][3]                       # records 15..19
    data = blobs[rec["digest"]]
    # too old: a checkpoint of 17 records does not cover record 19
    with pytest.raises(A.ArchiveError, match="does not cover"):
        _segment(rec, data, proof=[], **_checkpoint(events, 17))
    # a fork: the same first twelve records, then another history
    fork = EventLog(tmp_path / "fork.jsonl")
    for ev in events[:12]:
        fork.append(actor=ev.actor, action=ev.action, target=ev.target,
                    payload=ev.payload, event_id=ev.event_id,
                    wall_time=ev.wall_time)
    for i in range(11):
        fork.append(actor="mallory", action="note.fixture", target=f"x{i}",
                    payload={"x": i})
    fev = _events(fork)
    assert fev[11].hash == events[11].hash
    for proof in (_proof(fev, 20, 23), _proof(events, 20, 23)):
        with pytest.raises(A.ArchiveError, match="not in the tree"):
            _segment(rec, data, proof=proof, **_checkpoint(fev))
    # before the fork the segment is in both histories, as it should be
    early = manifest["segments"][1]
    _segment(early, blobs[early["digest"]],
                     proof=_proof(fev, 10, 23), **_checkpoint(fev))


@pytest.mark.parametrize("bad", [
    {"size": 0}, {"size": True}, {"root": "AB" * 32}, {"proof": ["zz"]},
    {"proof": "00" * 32},
])
def test_a_malformed_checkpoint_or_proof_is_refused(world, bad):
    _, events, manifest, blobs = world
    rec = manifest["segments"][0]
    args = dict(_checkpoint(events), proof=_proof(events, 5, 23))
    args.update(bad)
    with pytest.raises(A.ArchiveError):
        _segment(rec, blobs[rec["digest"]], **args)


@pytest.mark.parametrize("change", [
    {"start": -1}, {"start": True}, {"end": 10}, {"digest": "x"},
    {"frontier": "aa" * 32}, {"frontier": ["AA" * 32]}, {"root": None},
    {"prev_hash": "1" * 64}, {"extra": 1},
])
def test_a_malformed_segment_record_is_refused(world, change):
    _, events, manifest, blobs = world
    rec = dict(manifest["segments"][0], **change)
    with pytest.raises(A.ArchiveError):
        _segment(rec, blobs[manifest["segments"][0]["digest"]],
                         proof=_proof(events, 5, 23), **_checkpoint(events))


# ---- what a keeper of the archive cannot do -----------------------------------
def _verify(manifest, blobs, events, end=None):
    end = len(events) if end is None else end
    return A.verify_archive(manifest, blobs.__getitem__,
                            proof=_proof(events, end, len(events)),
                            **_checkpoint(events))


def test_a_manifest_with_a_gap_an_overlap_or_a_reordering_is_refused(world):
    _, events, manifest, blobs = world
    segs = manifest["segments"]
    for label, bad, why in (
            ("gap", segs[:2] + segs[3:], "gap or an overlap"),
            ("overlap", segs[:3] + segs[2:], "gap or an overlap"),
            ("reordered", [segs[1], segs[0]] + segs[2:],
             "gap or an overlap"),
            ("not from genesis", segs[1:], "gap or an overlap")):
        with pytest.raises(A.ArchiveError, match=why):
            _verify(dict(manifest, segments=bad), blobs, events)


def test_a_splice_of_two_logs_is_refused(world, tmp_path):
    """Segments of another log that agree with themselves, put where this
    log's were: the frontier and the first link give the splice away even
    before the checkpoint does."""
    _, events, manifest, blobs = world
    other = _log(tmp_path / "other.jsonl", 23, tag="o")
    omanifest, oblobs = A.seal(other, 5)
    spliced = manifest["segments"][:2] + omanifest["segments"][2:]
    both = {**blobs, **oblobs}
    with pytest.raises(A.ArchiveError, match="frontier"):
        _verify(dict(manifest, segments=spliced), both, events)
    # with the frontier made to fit, the first link still does not
    seg = dict(omanifest["segments"][2],
               frontier=manifest["segments"][2]["frontier"])
    with pytest.raises(A.ArchiveError, match="first link"):
        _verify(dict(manifest, segments=manifest["segments"][:2] + [seg]),
                both, events, end=15)


def test_missing_bytes_and_a_wrong_schema_are_refused(world):
    _, events, manifest, blobs = world
    with pytest.raises(A.ArchiveError, match="missing"):
        _verify(manifest, {}, events)
    for bad in ({**manifest, "schema": "event-archive/2"},
                {**manifest, "extra": 1}, {**manifest, "segments": []},
                []):
        with pytest.raises(A.ArchiveError):
            _verify(bad, blobs, events)


def test_an_archive_of_a_prefix_verifies_as_a_prefix(world):
    _, events, manifest, blobs = world
    prefix = dict(manifest, segments=manifest["segments"][:3])
    rep = _verify(prefix, blobs, events, end=15)
    assert rep.covered == 15 and rep.size == 23 and not rep.complete
    # and an archive is never more than the checkpoint
    with pytest.raises(A.ArchiveError, match="does not cover"):
        A.verify_archive(manifest, blobs.__getitem__, proof=[],
                         **_checkpoint(events, 20))


def test_sealing_refuses_what_does_not_verify(tmp_path):
    log = _log(tmp_path / "log.jsonl", 6)
    raw = log.path.read_bytes().split(b"\n")
    raw[3] = raw[3].replace(b'"t3"', b'"tX"')
    log.path.write_bytes(b"\n".join(raw))
    with pytest.raises(A.ArchiveError, match="does not verify"):
        A.seal(log, 2)
    for every in (0, -1, True, 2.0):
        with pytest.raises(A.ArchiveError):
            A._seal(_events(_log(tmp_path / f"ok{every}.jsonl", 3)), every)
    with pytest.raises(A.ArchiveError, match="nothing to seal"):
        A._seal([], 2)


# ---- the archive authenticates no one -----------------------------------------
OWNER = pr.test_identity("owner")
ALICE = pr.test_identity("alice")


def _authenticator(att):
    return pr.Authenticator(att, pr.KeyRegistry(
        [OWNER.registered(), ALICE.registered()], allow_test_keys=True))


def test_a_required_history_restored_still_requires_authentication(
        tmp_path):
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    log = EventLog(tmp_path / "log.jsonl", authenticator=_authenticator(att))
    pr.begin_history(log, att, OWNER)
    for i in range(4):
        pr.signed_append(log, att, ALICE, action="record.create",
                         target=f"r{i}",
                         payload={"record_id": f"r{i}", "kind": "k",
                                  "proposer": ALICE.principal})
    manifest, blobs = A.seal(log, 2)
    events = _events(log)
    rep = A.verify_archive(manifest, blobs.__getitem__, proof=[],
                           **_checkpoint(events))
    assert rep.complete and rep.profile == PROFILE_AUTHENTICATED_REQUIRED
    assert rep.events == (), "the archive hands back no unauthenticated record"
    for rec in manifest["segments"]:
        got = A.verify_segment(rec, blobs[rec["digest"]],
                               profile=PROFILE_AUTHENTICATED_REQUIRED,
                               proof=_proof(events, rec["end"], len(events)),
                               **_checkpoint(events))
        assert got == []
    # pinned the wrong way, the first segment says otherwise
    first = manifest["segments"][0]
    with pytest.raises(A.ArchiveError, match="declares"):
        A.verify_segment(first, blobs[first["digest"]], profile=LEGACY,
                         proof=_proof(events, first["end"], len(events)),
                         **_checkpoint(events))
    restored = tmp_path / "restored.jsonl"
    A.restore(manifest, blobs.__getitem__, restored, proof=[],
              **_checkpoint(events))
    report, got = EventLog(restored).read_verified()
    assert not report.ok and not got, "no authenticator, no events"
    report, got = EventLog(restored,
                           authenticator=_authenticator(att)).read_verified()
    assert report.ok and [e.hash for e in got] == [e.hash for e in events]


def test_the_genesis_link_is_required(world):
    _, events, manifest, blobs = world
    rec = dict(manifest["segments"][0], prev_hash="1" * 64)
    with pytest.raises(A.ArchiveError, match="links to nothing"):
        _segment(rec, blobs[rec["digest"]], proof=[],
                         **_checkpoint(events, 5))
    assert manifest["segments"][0]["prev_hash"] == ZERO_DIGEST


def test_a_count_is_a_number_not_a_truth_value(world):
    """``true`` is 1 to a careless reader: a segment sealed at record 1
    with its start written ``true``, or one ending at 1 with its end
    written so, is refused."""
    _, events, _, _ = world
    manifest, blobs = A._seal(events, 1)
    one = manifest["segments"][1]
    zero = manifest["segments"][0]
    for rec in (dict(one, start=True), dict(zero, end=True)):
        with pytest.raises(A.ArchiveError, match="count"):
            _segment(rec, blobs[rec["digest"]], proof=[],
                             **_checkpoint(events, rec["end"] + 0))


def test_bytes_that_are_not_bytes_are_refused(world):
    _, events, manifest, blobs = world
    rec = manifest["segments"][0]
    with pytest.raises(A.ArchiveError, match="not the ones"):
        _segment(rec, blobs[rec["digest"]].decode(),
                         proof=_proof(events, 5, 23), **_checkpoint(events))


def test_a_profile_is_one_a_history_declares(world):
    _, events, manifest, blobs = world
    rec = manifest["segments"][1]
    for bad in ("LEGACY", None, PROFILE_AUTHENTICATED_REQUIRED.lower()):
        with pytest.raises(A.ArchiveError, match="profile"):
            A.verify_segment(rec, blobs[rec["digest"]], profile=bad,
                             proof=_proof(events, 10, 23),
                             **_checkpoint(events))
    # a legacy history's first segment agrees with its pin
    first = manifest["segments"][0]
    assert len(_segment(first, blobs[first["digest"]],
                        proof=_proof(events, 5, 23),
                        **_checkpoint(events))) == 5
