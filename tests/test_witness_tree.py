"""Tree checkpoints: the witness protocol over a Merkle tree (Phase C).

A head witness (tests/test_witness.py) learns that a head extends the last
one it cosigned from every record in between. A tree witness holds the last
TREE it cosigned, the log's origin, size and RFC 9162 root, and accepts a
larger one only with a consistency proof: O(log n) hashes, whatever was
appended. Everything the head protocol refuses, this one refuses: a
rollback, a fork at the cosigned size, a history rewritten beneath the
cosigned tree, a witness that lost its state. A verifier holds a log to the
trees a quorum cosigned, and reports TRUNCATED and FORKED by root.

Every key is a TEST key, signed by the REFERENCE_ONLY provider, and every
witness runs in this process: a fixture of the protocol, not a deployment.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import transparency as T  # noqa: E402
from agent_substrate import witness as W  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402

NAME = "lab-log"


def key(label: str) -> bytes:
    return hashlib.sha256(f"test-witness-key:{label}".encode()).digest()


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class World:
    def __init__(self, tmp: Path, n: int = 5, witnesses=("w1",)):
        self.tmp = tmp
        self.clock = Clock()
        self.log = EventLog(tmp / "log.jsonl")
        self.append(n)
        self.witnesses = {
            w: W.TreeWitness(w, key(w), tmp / "witness" / w,
                             clock=self.clock) for w in witnesses}
        for wit in self.witnesses.values():
            wit.provision_tree(self.tree_at(1))
        self.cosigs: list = []

    def append(self, n: int) -> None:
        for i in range(n):
            self.log.append(actor="writer", action="note.fixture",
                            target=f"t{i}", payload={"i": i})

    def events(self):
        report, events = self.log.read_verified()
        report.raise_if_bad()
        return events

    def tree_at(self, size: int) -> W.TreeCheckpoint:
        ev = self.events()
        return W.TreeCheckpoint(W.origin_of(NAME, ev), size,
                                T.root(T.entries_of(ev[:size])).hex())

    def witness(self, wid="w1", size=None):
        ev = self.events()
        size = len(ev) if size is None else size
        last = self.witnesses[wid]._load_tree(W.origin_of(NAME, ev)).size
        proof = W.tree_proof(ev, last, size) if size > last else []
        c = self.witnesses[wid].add_tree_checkpoint(self.tree_at(size), proof)
        self.cosigs.append(c)
        return c

    def trust(self, quorum=1) -> W.TrustRoot:
        return W.TrustRoot(W.origin_of(NAME, self.events()),
                           {w: x.public_key
                            for w, x in self.witnesses.items()},
                           quorum=quorum)


def test_a_tree_witness_cosigns_an_extension_from_a_logarithmic_proof(
        tmp_path):
    w = World(tmp_path, n=3)
    w.witness()
    w.append(200)
    ev = w.events()
    proof = W.tree_proof(ev, 3, len(ev))
    # the head protocol would need every one of the 200 records
    assert len(proof) <= 2 * (len(ev) - 1).bit_length()
    c = w.witness()
    assert c.checkpoint == W.tree_checkpoint_of(NAME, ev)
    assert w.witnesses["w1"]._load_tree(c.checkpoint.origin) == c.checkpoint
    rep = W.verify_tree_witnessed(ev, w.cosigs, w.trust())
    assert rep.ok and rep.witnessed_through == ev[-1].seq


def test_a_rollback_is_refused_and_the_state_kept(tmp_path):
    w = World(tmp_path, n=6)
    w.witness()
    with pytest.raises(W.Rollback):
        w.witnesses["w1"].add_tree_checkpoint(w.tree_at(4), [])
    assert w.witnesses["w1"]._load_tree(w.tree_at(6).origin).size == 6


def test_a_fork_at_the_cosigned_size_is_refused(tmp_path):
    w = World(tmp_path, n=6)
    w.witness()
    other = W.TreeCheckpoint(w.tree_at(6).origin, 6, "f" * 64)
    with pytest.raises(W.Fork):
        w.witnesses["w1"].add_tree_checkpoint(other, [])
    with pytest.raises(W.WitnessError, match="needs no proof"):
        w.witnesses["w1"].add_tree_checkpoint(w.tree_at(6), ["0" * 64])


def test_a_history_rewritten_beneath_the_cosigned_tree_is_refused(tmp_path):
    """The rewritten log's own proof is valid for the rewritten log; it does
    not lead from the tree the witness holds, and the witness refuses."""
    w = World(tmp_path, n=6)
    w.witness()
    forged = tmp_path / "forged"
    forged.mkdir()
    flog = EventLog(forged / "log.jsonl")
    for e in w.events()[:4]:
        flog.append(actor=e.actor, action=e.action, target=e.target,
                    payload=e.payload)
    for i in range(10):
        flog.append(actor="mallory", action="note.fixture",
                    target=f"x{i}", payload={"x": i})
    report, fev = flog.read_verified()
    origin = w.tree_at(6).origin
    new = W.TreeCheckpoint(origin, len(fev), T.root(T.entries_of(fev)).hex())
    with pytest.raises(W.Fork):
        w.witnesses["w1"].add_tree_checkpoint(new, W.tree_proof(fev, 6,
                                                                len(fev)))


@pytest.mark.parametrize("proof", [["zz"], [b"\x00" * 32], ["00" * 31]])
def test_a_proof_that_is_not_a_proof_is_refused(tmp_path, proof):
    w = World(tmp_path, n=3)
    w.witness()
    w.append(5)
    with pytest.raises(W.WitnessError):
        w.witnesses["w1"].add_tree_checkpoint(w.tree_at(8), proof)


def test_a_witness_that_lost_its_state_trusts_nothing_first(tmp_path):
    w = World(tmp_path, n=3)
    w.witness()
    shutil.rmtree(tmp_path / "witness" / "w1")
    (tmp_path / "witness" / "w1").mkdir()
    with pytest.raises(W.WitnessError, match="holds no tree"):
        w.witness()


def test_provisioning_is_at_the_first_records_tree_only(tmp_path):
    w = World(tmp_path, n=3)
    fresh = W.TreeWitness("w2", key("w2"), tmp_path / "w2", clock=w.clock)
    with pytest.raises(W.WitnessError, match="first record"):
        fresh.provision_tree(w.tree_at(2))
    with pytest.raises(W.WitnessError, match="first record"):
        fresh.provision_tree(W.TreeCheckpoint(w.tree_at(1).origin, 1,
                                              "a" * 64))
    fresh.provision_tree(w.tree_at(1))
    with pytest.raises(W.WitnessError, match="already"):
        fresh.provision_tree(w.tree_at(1))


def test_a_log_cut_below_a_cosigned_tree_is_truncated(tmp_path):
    w = World(tmp_path, n=8)
    w.witness()
    ev = w.events()[:5]
    rep = W.verify_tree_witnessed(ev, w.cosigs, w.trust())
    assert any(p.startswith("TRUNCATED") for p in rep.problems)


def test_a_log_rewritten_beneath_a_cosigned_tree_is_forked(tmp_path):
    w = World(tmp_path, n=8)
    w.witness()
    ev = w.events()
    import dataclasses
    tampered = list(ev)
    tampered[3] = dataclasses.replace(ev[3], hash="e" * 64)
    rep = W.verify_tree_witnessed(tampered, w.cosigs, w.trust())
    assert any(p.startswith("FORKED") for p in rep.problems)


def test_junk_counts_for_nothing_and_the_reach_is_stated(tmp_path):
    w = World(tmp_path, n=5, witnesses=("w1", "w2"))
    w.witness("w1")
    w.append(4)
    w.witness("w2")
    ev = w.events()
    stranger = W.TreeWitness("w9", key("w9"), tmp_path / "w9",
                             clock=w.clock)
    stranger.provision_tree(w.tree_at(1))
    proof = W.tree_proof(ev, 1, len(ev))
    junk = [stranger.add_tree_checkpoint(w.tree_at(len(ev)), proof),
            W.TreeCosignature("w1", 1, "00" * 64, w.tree_at(len(ev)))]
    rep = W.verify_tree_witnessed(ev, w.cosigs + junk, w.trust(quorum=2))
    assert not rep.problems and len(rep.notes) == 2
    # a quorum of two reaches only as far as the lower of the two
    assert rep.witnessed_through == 4 and rep.unwitnessed == len(ev) - 5
    stale = W.verify_tree_witnessed(ev, w.cosigs, w.trust(quorum=2),
                                    max_lag=2)
    assert stale.stale and not stale.ok


def test_one_protocols_cosignature_is_never_the_others():
    tree = W.TreeCheckpoint("lab-log/" + "a" * 64, 1, "b" * 64)
    head = W.Checkpoint("lab-log/" + "a" * 64, 0, "b" * 64)
    assert W.tree_cosigned_message(tree, 7) != W.cosigned_message(head, 7)
    assert W.TREE_SCHEME != W.SCHEME


@pytest.mark.parametrize("rec", [
    {"origin": "a", "size": 0, "root": "b" * 64},
    {"origin": "a", "size": True, "root": "b" * 64},
    {"origin": "a\nb", "size": 1, "root": "b" * 64},
    {"origin": "a", "size": 1, "root": "B" * 64},
    {"origin": "a", "size": 1, "root": "b" * 64, "head": "c" * 64},
])
def test_a_malformed_tree_checkpoint_is_refused(rec):
    with pytest.raises(W.WitnessError):
        W.TreeCheckpoint.from_record(rec)


def test_a_tree_of_another_log_counts_for_nothing(tmp_path):
    """An authorized witness's tree of ANOTHER log, signed correctly, is a
    note: it says nothing about this one."""
    w = World(tmp_path, n=4)
    w.witness()
    ev = w.events()
    other = W.TreeCheckpoint("other-log/" + ev[0].hash, len(ev),
                             T.root(T.entries_of(ev)).hex())
    sig = w.witnesses["w1"]._tree_cosign(other)
    rep = W.verify_tree_witnessed(ev, [sig], w.trust())
    assert not rep.problems and rep.witnessed_through == -1
    assert any("another log" in n for n in rep.notes)


def test_a_trust_root_for_another_log_is_a_problem(tmp_path):
    """An origin is a name and its first record's hash: a trust root whose
    first record is not this log's is for another log."""
    w = World(tmp_path, n=4)
    w.witness()
    ev = w.events()
    wrong = W.TrustRoot(f"{NAME}/" + "0" * 64,
                        {"w1": w.witnesses["w1"].public_key})
    rep = W.verify_tree_witnessed(ev, w.cosigs, wrong)
    assert rep.problems and not rep.ok
