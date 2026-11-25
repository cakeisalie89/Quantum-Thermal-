"""A quorum of tree witnesses against a split view (Phase E, AUD-018).

A log operator shows each witness whatever history it likes. It wins if two
forks of one log are each witnessed by a quorum: two readers then accept
two different pasts. ``docs/formal/WitnessQuorum.tla`` (protocol 7) states
when that cannot happen -- two quorums share more witnesses than can be
faulty -- and TLC refutes the protocol without each of its rules. These
tests hold the code to the model with real witnesses:

* the trees the model's witness accepts are exactly the trees a
  :class:`TreeWitness` accepts, for every pair the model ranges over;
* a quorum that is not a majority let two groups of HONEST witnesses
  witness two forks (AUD-018, reproduced), and is now refused;
* a witness provisioned again after losing its state cosigns a fork of
  what it forgot, as TLC's counterexample says, so it must be counted
  faulty: the split view it enables needs one fault more than the quorum
  tolerates.

Every key is a TEST key and every witness runs in this process.
"""
from __future__ import annotations

import hashlib
import itertools
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import witness as W  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402

NAME = "lab-log"
#: The trees protocol 7 ranges over: the first record "g", then up to two
#: more, each "a" or "b" (Values and MaxLen in tools/formal_check.py).
TREES = [("g",) + rest for k in range(3)
         for rest in itertools.product("ab", repeat=k)]


def key(label: str) -> bytes:
    return hashlib.sha256(f"test-witness-key:{label}".encode()).digest()


class Histories:
    """One real log per tree. Records are written with fixed ids and times,
    so two trees with a common prefix have the same records there."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.events: dict = {}
        for tree in TREES:
            log = EventLog(tmp / "logs" / "-".join(tree) / "log.jsonl")
            for i, value in enumerate(tree):
                log.append(actor="operator", action="note.fixture",
                           target=f"r{i}", payload={"value": value},
                           event_id=f"{i}-" + "-".join(tree[:i + 1]),
                           wall_time=1_800_000_000.0 + i)
            report, events = log.read_verified()
            report.raise_if_bad()
            self.events[tree] = events
        self.origin = W.origin_of(NAME, self.events[("g",)])

    def checkpoint(self, tree) -> W.TreeCheckpoint:
        return W.tree_checkpoint_of(NAME, self.events[tree])

    def proof(self, held, tree) -> list:
        """The operator's best proof that ``tree`` extends ``held``: the
        consistency proof within ``tree``'s own log."""
        if len(tree) <= len(held):
            return []
        return W.tree_proof(self.events[tree], len(held), len(tree))

    def witness(self, wid: str, where: Path | None = None) -> W.TreeWitness:
        wit = W.TreeWitness(wid, key(wid), where or self.tmp / "w" / wid)
        wit.provision_tree(self.checkpoint(("g",)))
        return wit

    def show(self, wit: W.TreeWitness, held, tree):
        return wit.add_tree_checkpoint(self.checkpoint(tree),
                                       self.proof(held, tree))

    def trust(self, ids, quorum: int) -> W.TrustRoot:
        return W.TrustRoot(self.origin, {i: W.TreeWitness(
            i, key(i), self.tmp / "keys" / i).public_key for i in ids},
            quorum=quorum)


@pytest.fixture(scope="module")
def H(tmp_path_factory):
    return Histories(tmp_path_factory.mktemp("histories"))


def _is_prefix(p, q) -> bool:
    return len(p) <= len(q) and q[:len(p)] == p


# ---- the model's witness is the code's ----------------------------------------
def test_a_tree_witness_accepts_exactly_the_trees_the_model_s_does(H,
                                                                   tmp_path):
    """For each pair (held, shown) of the model's trees, a real witness
    holding ``held`` cosigns ``shown`` if and only if ``held`` is a prefix
    of it: the model's Accepts, in its correct variant."""
    seen = 0
    for held, shown in itertools.product(TREES, TREES):
        wit = H.witness("w1", tmp_path / f"{'-'.join(held)}_{'-'.join(shown)}")
        if held != ("g",):
            H.show(wit, ("g",), held)
        try:
            H.show(wit, held, shown)
            accepted = True
        except W.WitnessError:
            accepted = False
        assert accepted == _is_prefix(held, shown), (held, shown)
        # and what it holds afterwards is what the model's witness holds
        want = shown if accepted else held
        assert wit._load_tree(H.origin) == H.checkpoint(want)
        seen += 1
    assert seen == len(TREES) ** 2 == 49


# ---- AUD-018: a quorum that is not a majority ---------------------------------
def test_a_minority_quorum_let_honest_witnesses_witness_two_forks(H,
                                                                 tmp_path):
    """Reproduced: four honest witnesses, a quorum of two. The operator
    shows w1 and w2 one fork and w3 and w4 the other, and each reader holds
    a quorum of cosignatures for its own past. The trust root that allowed
    this is now refused; with a majority neither fork is witnessed."""
    fork_a, fork_b = ("g", "a"), ("g", "b")
    cosigs = {fork_a: [], fork_b: []}
    for wid, fork in (("w1", fork_a), ("w2", fork_a), ("w3", fork_b),
                      ("w4", fork_b)):
        cosigs[fork].append(H.show(H.witness(wid, tmp_path / wid),
                                   ("g",), fork))
    ids = ("w1", "w2", "w3", "w4")
    with pytest.raises(W.WitnessError, match="not a majority"):
        H.trust(ids, quorum=2)
    # the trust root as it was accepted before AUD-018
    before = H.trust(ids, quorum=3)
    object.__setattr__(before, "quorum", 2)
    for fork in (fork_a, fork_b):
        rep = W.verify_tree_witnessed(H.events[fork], cosigs[fork], before)
        assert rep.ok and rep.witnessed_through == 1, rep.problems
    # with a majority, neither fork is witnessed by honest witnesses alone
    for fork in (fork_a, fork_b):
        rep = W.verify_tree_witnessed(H.events[fork], cosigs[fork],
                                      H.trust(ids, quorum=3))
        assert rep.witnessed_through == -1 and not rep.problems


@pytest.mark.parametrize("n, quorum, tolerates", [
    (1, 1, 0), (2, 2, 1), (3, 2, 0), (3, 3, 2), (4, 3, 1), (5, 3, 0),
    (5, 4, 2), (7, 5, 2),
])
def test_how_many_faulty_witnesses_a_quorum_tolerates(H, n, quorum,
                                                      tolerates):
    ids = [f"w{i}" for i in range(1, n + 1)]
    assert H.trust(ids, quorum).tolerates == tolerates
    if 2 * (quorum - 1) > n:
        assert H.trust(ids, quorum - 1).tolerates == tolerates - 2


@pytest.mark.parametrize("n, quorum", [(2, 1), (4, 2), (6, 3), (5, 2)])
def test_every_quorum_of_half_or_fewer_is_refused(H, n, quorum):
    with pytest.raises(W.WitnessError, match="not a majority"):
        H.trust([f"w{i}" for i in range(1, n + 1)], quorum)


# ---- a witness that lost its state --------------------------------------------
def test_a_reprovisioned_witness_cosigns_a_fork_of_what_it_forgot(H,
                                                                  tmp_path):
    """TLC's counterexample to the loss_uncounted variant, replayed with
    real witnesses. w1 cosigns one fork, loses its state, is provisioned
    again and cosigns the other; w2 and w3 each cosign one fork; w4 is
    faulty and cosigns both. Each reader holds a quorum of three for its
    own past. It took two faulty witnesses, and a quorum of three of four
    tolerates one: the code cannot see a lost state, so the trust root's
    owner must count it."""
    fork_a, fork_b = ("g", "a"), ("g", "b")
    w1 = H.witness("w1", tmp_path / "w1")
    old = H.show(w1, ("g",), fork_a)
    shutil.rmtree(tmp_path / "w1")
    (tmp_path / "w1").mkdir()
    with pytest.raises(W.WitnessError, match="holds no tree"):
        H.show(w1, ("g",), fork_b)
    w1.provision_tree(H.checkpoint(("g",)))
    new = H.show(w1, ("g",), fork_b)        # the hazard: it does not know
    a = [old, H.show(H.witness("w2", tmp_path / "w2"), ("g",), fork_a),
         H.show(H.witness("w4", tmp_path / "w4a"), ("g",), fork_a)]
    b = [new, H.show(H.witness("w3", tmp_path / "w3"), ("g",), fork_b),
         H.show(H.witness("w4", tmp_path / "w4b"), ("g",), fork_b)]
    root = H.trust(("w1", "w2", "w3", "w4"), quorum=3)
    for fork, cs in ((fork_a, a), (fork_b, b)):
        rep = W.verify_tree_witnessed(H.events[fork], cs, root)
        assert rep.ok and rep.witnessed_through == 1, rep.problems
    faulty = {"w1", "w4"}            # w1 forgot; w4 cosigned anything
    assert len(faulty) > root.tolerates == 1
    # one fault fewer, and the split view is gone: without w4's second
    # cosignature the other fork has two of the three it needs
    rep = W.verify_tree_witnessed(H.events[fork_b], b[:2], root)
    assert rep.witnessed_through == -1
