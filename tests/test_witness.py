"""Independent witnesses to the log's head (Assurance E).

The limitation they address, M-3, is reproduced first: a log truncated or
rewritten together with its own head witness verifies. Then the protocol,
from the witness's side (it cosigns only an extension of what it cosigned)
and from the verifier's (a log that disagrees with what an authorized
witness cosigned is TRUNCATED or FORKED; junk cosignatures count for
nothing; the reach of the witnessing is stated, and STALE past a bound).

Every key here is a TEST key, signed by the REFERENCE_ONLY provider, and
every witness runs in this process: a fixture of the protocol, not an
independent deployment of it.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import witness as W  # noqa: E402
from agent_substrate.events import ChainState, EventLog  # noqa: E402

NAME = "lab-log"


def key(label: str) -> bytes:
    """A deterministic TEST secret."""
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
            w: W.Witness(w, key(w), tmp / "witness" / w, clock=self.clock)
            for w in witnesses}
        for wit in self.witnesses.values():
            wit.provision(self.checkpoint_at(0))
        self.cosigs: list = []

    def append(self, n: int) -> None:
        for i in range(n):
            self.log.append(actor="writer", action="note.fixture",
                            target=f"t{i}", payload={"i": i})

    def events(self):
        report, events = self.log.read_verified()
        report.raise_if_bad()
        return events

    def checkpoint_at(self, seq: int) -> W.Checkpoint:
        ev = self.events()
        return W.Checkpoint(W.origin_of(NAME, ev), seq, ev[seq].hash)

    def witness(self, wid="w1", upto=None):
        """Ask ``wid`` to cosign the head (or ``upto``), from where it is."""
        ev = self.events()
        upto = ev[-1].seq if upto is None else upto
        wit = self.witnesses[wid]
        last = wit._load(W.origin_of(NAME, ev)).seq
        c = wit.add_checkpoint(self.checkpoint_at(upto),
                               W.proof_records(ev, last, upto))
        self.cosigs.append(c)
        return c

    def root(self, quorum=1, ids=None):
        ids = ids or list(self.witnesses)
        return W.TrustRoot(origin=W.origin_of(NAME, self.events()),
                           witnesses={i: self.witnesses[i].public_key
                                      for i in ids}, quorum=quorum)

    def verify(self, **kw):
        _, ev = self.log.read_verified()
        root = kw.pop("root", None) or self.root()
        return W.verify_witnessed(ev, kw.pop("cosigs", self.cosigs), root,
                                  **kw)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


# ------------------------------------------------- M-3, before and after

def _truncate_with_its_witness(log: EventLog, keep: int) -> None:
    """The attack M-3 describes: cut the log AND rewrite the head witness
    beside it to agree."""
    lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
    log.path.write_text("".join(lines[:keep]), encoding="utf-8")
    last = json.loads(lines[keep - 1])
    log._write_head(ChainState(last["seq"], last["hash"]))


def test_m3_reproduced_a_log_cut_with_its_own_witness_verifies(w):
    w.witness()
    w.append(3)
    _truncate_with_its_witness(w.log, 4)
    report, events = w.log.read_verified()
    assert report.ok and events[-1].seq == 3      # consistent with itself


def test_m3_closed_an_independent_cosignature_shows_the_cut(w):
    w.witness()                                   # cosigned seq 4
    _truncate_with_its_witness(w.log, 3)
    rep = w.verify()
    assert not rep.ok
    assert any(p.startswith("TRUNCATED: w1 cosigned seq 4") for p in
               rep.problems)


def test_m3_closed_even_when_the_holder_drops_the_cosignatures(w):
    """The verifier asks the witness itself, where the log's holder cannot
    reach: deleting the stored cosignatures hides nothing."""
    w.witness()
    _truncate_with_its_witness(w.log, 3)
    asked = [wit.latest(W.origin_of(NAME, w.events()[:1]))
             for wit in w.witnesses.values()]
    rep = w.verify(cosigs=asked)
    assert any(p.startswith("TRUNCATED") for p in rep.problems)


def _rewrite(log: EventLog, seq: int) -> None:
    """Change record ``seq`` and re-chain everything after it, head witness
    included: a rewrite every local check accepts."""
    from agent_substrate.canonical import digest
    recs = [json.loads(x) for x in
            log.path.read_text(encoding="utf-8").splitlines()]
    prev = recs[seq - 1]["hash"]
    for r in recs[seq:]:
        if r["seq"] == seq:
            r["payload"] = {"i": "rewritten"}
        r["prev_hash"] = prev
        body = {k: v for k, v in r.items() if k != "hash"}
        r["hash"] = digest(body)
        prev = r["hash"]
    log.path.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                                for r in recs), encoding="utf-8")
    log._write_head(ChainState(recs[-1]["seq"], recs[-1]["hash"]))


def test_a_rewritten_history_verifies_locally_and_is_forked_to_a_witness(w):
    w.witness()
    _rewrite(w.log, 2)
    report, _ = w.log.read_verified()
    assert report.ok                              # M-3's other half
    rep = w.verify()
    assert any(p.startswith("FORKED: w1 cosigned") for p in rep.problems)
    # and the witness refuses to cosign the rewritten history forward
    w.append(1)
    with pytest.raises(W.Fork):
        w.witness()


def test_control_an_honest_log_verifies_witnessed_to_its_head(w):
    w.witness()
    w.append(2)
    w.witness()
    rep = w.verify()
    assert rep.ok and rep.witnessed_through == 6 and rep.unwitnessed == 0
    assert rep.problems == [] and rep.notes == []


# -------------------------------------------- the witness: monotonic, linked

def test_a_witness_refuses_to_roll_back(w):
    w.witness()
    with pytest.raises(W.Rollback):
        w.witnesses["w1"].add_checkpoint(w.checkpoint_at(2), [])


def test_a_witness_refuses_another_head_at_a_position_it_cosigned(w):
    w.witness()
    cp = w.checkpoint_at(4)
    other = W.Checkpoint(cp.origin, 4, "e" * 64)
    with pytest.raises(W.Fork):
        w.witnesses["w1"].add_checkpoint(other, [])
    # the same head again is the same answer: a lost reply costs nothing
    again = w.witnesses["w1"].add_checkpoint(cp, [])
    assert again.checkpoint == cp


@pytest.mark.parametrize("damage, why", [
    (lambda p: p[:-1], "needs exactly"),
    (lambda p: p + p[-1:], "needs exactly"),
    (lambda p: [dict(p[0], payload={"i": "x"})] + p[1:], "does not hash"),
    (lambda p: [dict(p[0], seq=p[0]["seq"] + 1)] + p[1:], "numbered"),
    (lambda p: p[1:] + p[:1], "numbered"),
    (lambda p: [dict(p[0], extra=1)] + p[1:], "not a log record"),
])
def test_a_witness_refuses_records_that_do_not_extend_its_head(
        w, damage, why):
    ev = w.events()
    w.append(3)
    ev = w.events()
    proof = W.proof_records(ev, 0, ev[-1].seq)
    with pytest.raises(W.WitnessError, match=why):
        w.witnesses["w1"].add_checkpoint(w.checkpoint_at(ev[-1].seq),
                                         damage(proof))
    # nothing was recorded: the honest proof still works from seq 0
    c = w.witnesses["w1"].add_checkpoint(w.checkpoint_at(ev[-1].seq), proof)
    assert c.checkpoint.seq == ev[-1].seq


def test_a_witness_refuses_a_head_the_records_do_not_reach(w):
    w.append(2)
    ev = w.events()
    cp = W.Checkpoint(w.checkpoint_at(0).origin, ev[-1].seq, "f" * 64)
    with pytest.raises(W.WitnessError, match="end at"):
        w.witnesses["w1"].add_checkpoint(cp, W.proof_records(ev, 0,
                                                             ev[-1].seq))


def test_a_witness_that_lost_its_state_trusts_nothing_it_is_shown(w):
    w.witness()
    shutil.rmtree(w.tmp / "witness" / "w1")
    (w.tmp / "witness" / "w1").mkdir()
    with pytest.raises(W.WitnessError, match="re-provisioning is the "):
        w.witness()
    # re-provisioning is the owner's act, from the log's first record
    w.witnesses["w1"].provision(w.checkpoint_at(0))
    assert w.witness().checkpoint.seq == 4


def test_a_witness_is_provisioned_once_and_only_at_the_first_record(w):
    with pytest.raises(W.WitnessError, match="already witnesses"):
        w.witnesses["w1"].provision(w.checkpoint_at(0))
    fresh = W.Witness("w9", key("w9"), w.tmp / "witness" / "w9")
    with pytest.raises(W.WitnessError, match="first record"):
        fresh.provision(w.checkpoint_at(3))


def test_a_witness_refuses_a_log_it_was_not_provisioned_for(tmp_path, w):
    other = World(tmp_path / "other")
    ev = other.events()
    with pytest.raises(W.WitnessError, match="holds no state"):
        w.witnesses["w1"].add_checkpoint(other.checkpoint_at(4),
                                         W.proof_records(ev, 0, 4))


def test_a_witness_persists_before_it_answers(w, monkeypatch):
    """A crash after the state is written and before the cosignature is
    returned leaves a witness that cosigned nothing it forgot: asked again,
    it signs the same checkpoint."""
    wit = w.witnesses["w1"]
    real = wit._cosign

    def crash(cp):
        raise RuntimeError("killed before replying")

    monkeypatch.setattr(wit, "_cosign", crash)
    with pytest.raises(RuntimeError):
        w.witness()
    monkeypatch.setattr(wit, "_cosign", real)
    assert wit._load(w.checkpoint_at(0).origin).seq == 4
    assert wit.add_checkpoint(w.checkpoint_at(4), []).checkpoint.seq == 4


# ---------------------------------------- the verifier: what counts, and reach

def test_junk_cosignatures_count_for_nothing(w, tmp_path):
    good = w.witness()
    stranger = W.Witness("w-unknown", key("w-unknown"), tmp_path / "s")
    stranger.provision(w.checkpoint_at(0))
    ev = w.events()
    foreign = stranger.add_checkpoint(w.checkpoint_at(4),
                                      W.proof_records(ev, 0, 4))
    forged = W.Cosignature("w1", good.time, "00" * 64, good.checkpoint)
    moved = W.Cosignature("w1", good.time, good.signature,
                          W.Checkpoint(good.checkpoint.origin, 3,
                                       ev[3].hash))
    retimed = W.Cosignature("w1", good.time + 1, good.signature,
                            good.checkpoint)
    rep = w.verify(cosigs=[foreign, forged, moved, retimed])
    assert rep.witnessed_through == -1 and not rep.ok
    assert len(rep.notes) == 4 and rep.problems == []
    # junk beside a good cosignature changes nothing
    rep = w.verify(cosigs=[foreign, forged, moved, retimed, good])
    assert rep.ok and rep.witnessed_through == 4


def test_an_authorized_witness_s_cosignature_of_another_log_counts_nothing(
        tmp_path, w):
    """w1 witnesses two logs. What it cosigned for the other one is
    neither evidence for this log nor a fork of it."""
    other = World(tmp_path / "other")
    wit = w.witnesses["w1"]
    wit.provision(other.checkpoint_at(0))
    ev = other.events()
    elsewhere = wit.add_checkpoint(other.checkpoint_at(4),
                                   W.proof_records(ev, 0, 4))
    rep = w.verify(cosigs=[elsewhere])
    assert rep.problems == [] and rep.witnessed_through == -1
    assert rep.notes == ["w1: cosigned another log; not counted"]


def test_a_trust_root_for_another_log_counts_nothing(tmp_path, w):
    w.witness()
    other = World(tmp_path / "other")
    rep = w.verify(root=W.TrustRoot(
        origin=W.origin_of(NAME, other.events()),
        witnesses={"w1": w.witnesses["w1"].public_key}))
    assert not rep.ok and "another log" in rep.problems[0]


def test_the_reach_is_stated_and_stale_past_a_bound(w):
    w.witness()
    w.append(20)
    rep = w.verify()
    assert rep.ok and (rep.witnessed_through, rep.unwitnessed) == (4, 20)
    assert not w.verify(max_lag=10).ok
    assert w.verify(max_lag=20).ok
    cosigned = w.clock.t
    assert not w.verify(max_age_s=3600, now=cosigned + 7200).ok
    assert w.verify(max_age_s=3600, now=cosigned + 3000).ok


def test_a_quorum_is_reached_by_distinct_witnesses_only(tmp_path):
    w = World(tmp_path, witnesses=("w1", "w2", "w3"))
    w.witness("w1")
    w.append(1)
    w.witness("w1")                    # two cosignatures, one witness
    assert len({c.signature for c in w.cosigs}) == 2
    assert w.verify(root=w.root(quorum=2)).witnessed_through == -1
    w.append(1)
    w.witness("w2")
    rep = w.verify(root=w.root(quorum=2))
    # w1 reached 5, w2 reached 6: two of them vouch through 5
    assert rep.ok and rep.witnessed_through == 5 and rep.unwitnessed == 1
    with pytest.raises(W.WitnessError, match="quorum"):
        w.root(quorum=4)


@settings(max_examples=60, deadline=None, derandomize=True, database=None)
@given(n=st.integers(3, 12), cut=st.integers(1, 12), tamper=st.integers(1, 12),
       witnessed=st.integers(0, 12))
def test_detection_is_exactly_the_witnessed_prefix(tmp_path_factory, n, cut,
                                                   tamper, witnessed):
    """Whatever is cut or rewritten AT OR BELOW a cosigned position is
    detected; what happened after it is not, and the report says how many
    records that is."""
    w = World(tmp_path_factory.mktemp("p"), n=n)
    head = n - 1
    witnessed = min(witnessed, head)
    if witnessed:
        w.witness(upto=witnessed)
    keep = min(cut, n)
    if tamper < keep:
        _rewrite(w.log, tamper)
    if keep < n:
        _truncate_with_its_witness(w.log, keep)
    rep = w.verify()
    harmed = keep - 1 < witnessed or (tamper < keep and tamper <= witnessed)
    if witnessed:
        assert (not rep.ok) == harmed, (rep.problems, keep, tamper)
    else:
        assert rep.witnessed_through == -1 and not rep.ok
