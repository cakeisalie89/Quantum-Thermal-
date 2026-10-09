"""Restarting from checkpoints (R41): the audit decides, every class of
checkpoint is told apart, nothing unusable reads as health, and the
recovered state is the state the log replays to."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import checkpoint as CP  # noqa: E402
from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402


def _history(tmp: Path, name: str = "a", n: int = 6):
    log = EventLog(tmp / f"{name}.jsonl")
    ev = EvidenceStore(tmp / f"{name}-ev")
    st = AuthorityStore(log, evidence=ev).load()
    for i in range(n):
        st.create(record_id=f"r{i}", kind="claim", proposer="p",
                  evidence={"basis": ev.put(f"basis {i}".encode())})
    st.transition(record_id="r0", dst=State.UNDER_REVIEW, actor="v",
                  role=Role.VERIFIER)
    return log, ev, st


def test_a_usable_checkpoint_restarts_with_weaker_trust(tmp_path):
    log, ev, st = _history(tmp_path)
    cps = CP.CheckpointStore(tmp_path / "cps")
    first = st.checkpoint(cps, blobs=ev)
    st.create(record_id="late", kind="claim", proposer="p",
              evidence={"basis": ev.put(b"late")})
    second = st.checkpoint(cps, blobs=ev)
    store, rep = AuthorityStore.recover_and_compare(log, cps, blobs=ev,
                                                    evidence=ev)
    assert rep["verdict"] == CP.AUDIT_USABLE and rep["healthy"]
    assert rep["mode"] == "CHECKPOINT_ASSISTED"
    assert rep["checkpoint_seq"] == second.seq > first.seq
    assert rep["prefix_verified"] is False
    assert rep["agrees"] and rep["records"] == 7
    assert store.get("late").state is State.PROPOSED


def test_no_checkpoints_is_a_full_replay_and_says_so(tmp_path):
    log, ev, _ = _history(tmp_path)
    store, rep = AuthorityStore.recover(log, CP.CheckpointStore(
        tmp_path / "none"), blobs=ev, evidence=ev)
    assert rep["verdict"] == CP.AUDIT_EMPTY
    assert rep["mode"] == "FULL_REPLAY" and rep["prefix_verified"] is True


def test_a_foreign_log_checkpoint_is_none_usable_never_success(tmp_path):
    log, ev, _ = _history(tmp_path, "a")
    other, oev, ost = _history(tmp_path, "b", n=6)
    cps = CP.CheckpointStore(tmp_path / "cps")
    ost.checkpoint(cps, blobs=oev)
    store, rep = AuthorityStore.recover(log, cps, blobs=ev, evidence=ev)
    assert rep["verdict"] == CP.AUDIT_NONE_USABLE
    assert rep["healthy"] is False and rep["mode"] == "FULL_REPLAY"
    assert [c for _, c in rep["classified"]] == [CP.CP_FOREIGN_LOG]
    assert store.loaded_prefix_verified is True


def test_a_corrupt_newest_checkpoint_is_reported_not_skipped(tmp_path):
    log, ev, st = _history(tmp_path)
    cps = CP.CheckpointStore(tmp_path / "cps")
    good = st.checkpoint(cps, blobs=ev)
    st.create(record_id="x", kind="claim", proposer="p",
              evidence={"basis": ev.put(b"x")})
    bad = st.checkpoint(cps, blobs=ev)
    path = cps._path(bad.seq)
    rec = json.loads(path.read_text())
    rec["head_hash"] = "0" * 64
    path.write_text(json.dumps(rec))
    store, rep = AuthorityStore.recover_and_compare(log, cps, blobs=ev,
                                                    evidence=ev)
    assert rep["verdict"] == CP.AUDIT_UNPARSEABLE
    assert rep["healthy"] is False
    assert rep["checkpoint_seq"] == good.seq
    assert dict((s, c) for s, c in rep["classified"])[bad.seq] == \
        CP.CP_CORRUPT
    assert rep["agrees"]


def test_a_checkpoint_ahead_of_a_truncated_log_is_stale(tmp_path):
    log, ev, st = _history(tmp_path, n=8)
    cps = CP.CheckpointStore(tmp_path / "cps")
    st.checkpoint(cps, blobs=ev)
    lines = log.path.read_text().splitlines(keepends=True)
    short = tmp_path / "short.jsonl"
    short.write_text("".join(lines[:4]))
    store, rep = AuthorityStore.recover(EventLog(short), cps, blobs=ev,
                                        evidence=ev)
    assert rep["verdict"] == CP.AUDIT_NONE_USABLE
    assert [c for _, c in rep["classified"]] == [CP.CP_AHEAD_OF_LOG]
    assert rep["mode"] == "FULL_REPLAY"


def test_a_malformed_checkpoint_file_is_corrupt(tmp_path):
    log, ev, st = _history(tmp_path)
    cps = CP.CheckpointStore(tmp_path / "cps")
    cp = st.checkpoint(cps, blobs=ev)
    cps._path(cp.seq).write_text("{not json")
    audit = cps.audit(log)
    assert audit.verdict == CP.AUDIT_NONE_USABLE
    assert audit.classified == ((cp.seq, CP.CP_CORRUPT),)
    _, rep = AuthorityStore.recover(log, cps, blobs=ev, evidence=ev)
    assert rep["mode"] == "FULL_REPLAY" and rep["healthy"] is False


def test_the_recovered_state_is_the_logs_not_the_checkpoints(tmp_path):
    """A snapshot that disagrees with the log is refused, not restored: the
    checkpoint is never a second source of truth."""
    log, ev, st = _history(tmp_path)
    cps = CP.CheckpointStore(tmp_path / "cps")
    st.checkpoint(cps, blobs=ev)
    forged = EvidenceStore(tmp_path / "forged")
    for p in (tmp_path / "a-ev").rglob("*"):
        if p.is_file():
            dst = tmp_path / "forged" / p.relative_to(tmp_path / "a-ev")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, dst)
    cp = cps.latest_usable(log)
    blob = forged.root / cp.state_digest[:2] / cp.state_digest[2:] \
        if hasattr(forged, "root") else None
    if blob is None or not blob.is_file():
        pytest.skip("evidence layout not addressable from the test")
    snap = json.loads(blob.read_bytes())
    snap["records"] = {}
    blob.write_bytes(json.dumps(snap).encode())
    with pytest.raises(Exception):
        AuthorityStore.recover(log, cps, blobs=forged, evidence=ev)


def test_full_verification_from_genesis_remains_available(tmp_path):
    log, ev, st = _history(tmp_path)
    cps = CP.CheckpointStore(tmp_path / "cps")
    st.checkpoint(cps, blobs=ev)
    full = AuthorityStore(log, evidence=ev).load()
    assert full.loaded_prefix_verified is True
    assert log.verify().ok


def test_a_foreign_checkpoint_longer_in_bytes_is_foreign_not_ahead(tmp_path):
    """D-2026-109: the other log's records are longer, so its checkpoint
    ends past this log's last byte. That used to read as "records removed"
    (AHEAD_OF_LOG) -- intermittently, since whether it happens depended on
    timestamps and ids. This log reaches the checkpoint's seq; the
    checkpoint is foreign, and is now classed so on every run."""
    log, ev, _ = _history(tmp_path, "a")
    other = EventLog(tmp_path / "b.jsonl")
    oev = EvidenceStore(tmp_path / "b-ev")
    ost = AuthorityStore(other, evidence=oev).load()
    pad = "x" * 64
    for i in range(6):
        ost.create(record_id=f"r{i}-{pad}", kind="claim", proposer="p",
                   evidence={"basis": oev.put(f"basis {i}".encode())})
    ost.transition(record_id=f"r0-{pad}", dst=State.UNDER_REVIEW,
                   actor="v", role=Role.VERIFIER)
    cps = CP.CheckpointStore(tmp_path / "cps")
    cp = ost.checkpoint(cps, blobs=oev)
    assert cp.next_offset > log.path.stat().st_size
    assert CP._last_seq(log.path) >= cp.seq
    assert cps.audit(log).classified == ((cp.seq, CP.CP_FOREIGN_LOG),)


def test_a_log_truncated_in_place_is_ahead_whatever_its_witness_says(
        tmp_path):
    log, ev, st = _history(tmp_path, n=8)
    cps = CP.CheckpointStore(tmp_path / "cps")
    cp = st.checkpoint(cps, blobs=ev)
    lines = log.path.read_text().splitlines(keepends=True)
    log.path.write_text("".join(lines[:4]))     # the head witness is untouched
    assert cps.audit(log).classified == ((cp.seq, CP.CP_AHEAD_OF_LOG),)


@pytest.mark.parametrize("body, want", [
    (b"", None),
    (b'{"seq": 0}\n{"seq": 1}\n', 1),
    (b'{"seq": 0}\n{"seq": 1}\n{"seq": 2, "tor', 1),
    (b'{"seq": 0}\nnot json\n', None),
    (b'{"seq": true}\n', None),
])
def test_the_last_record_is_read_and_a_torn_tail_is_not_one(tmp_path,
                                                             body, want):
    p = tmp_path / "log.jsonl"
    p.write_bytes(body)
    assert CP._last_seq(p) == want
