"""Merkle transparency commitments over the event log (Phase C).

The tree is held to the Certificate Transparency project's published
roots for RFC 6962's eight test leaves (RFC 9162 keeps the same tree hash):
an external reference, not a value this module produced. Every proof is
then checked both ways over every size up to 64: each inclusion and
consistency proof the module builds verifies, and each one offered for the
wrong leaf, the wrong position, the wrong size or the wrong root does not.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import transparency as T  # noqa: E402

#: RFC 6962's test leaves, as the Certificate Transparency project's
#: merkle tests state them, and the roots it publishes for their prefixes.
LEAVES = [b"", b"\x00", b"\x10", b"\x20\x21", b"\x30\x31",
          b"\x40\x41\x42\x43", bytes(range(0x50, 0x58)),
          bytes(range(0x60, 0x70))]
ROOTS = [
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]
D = [hashlib.sha256(str(i).encode()).digest() for i in range(64)]


@pytest.mark.parametrize("n", range(1, 9))
def test_the_published_roots_of_rfc6962s_test_leaves(n):
    assert T.root(LEAVES[:n]).hex() == ROOTS[n - 1]


def test_the_empty_tree_is_the_hash_of_nothing():
    assert T.root([]) == hashlib.sha256(b"").digest()


def test_a_leaf_and_a_node_are_never_the_same_hash():
    """Domain separation: the 64 bytes of two children, offered as one
    leaf, do not hash to their parent."""
    a, b = T.leaf_hash(D[0]), T.leaf_hash(D[1])
    assert T.leaf_hash(a + b) != T.node_hash(a, b)


def test_every_inclusion_proof_verifies_and_no_wrong_one_does():
    for n in range(1, 65):
        r = T.root(D[:n])
        for i in range(n):
            p = T.inclusion_proof(i, D[:n])
            assert T.verify_inclusion(i, n, T.leaf_hash(D[i]), p, r)
            assert not T.verify_inclusion(i, n, T.leaf_hash(D[i] + b"x"),
                                          p, r)
            if n > 1:
                j = (i + 1) % n
                assert not T.verify_inclusion(j, n, T.leaf_hash(D[i]), p, r)
            assert not T.verify_inclusion(i, n, T.leaf_hash(D[i]), p,
                                          r[:-1] + bytes([r[-1] ^ 1]))
            if p:
                assert not T.verify_inclusion(i, n, T.leaf_hash(D[i]),
                                              p[:-1], r)
                assert not T.verify_inclusion(i, n, T.leaf_hash(D[i]),
                                              p + [p[-1]], r)


def test_every_consistency_proof_verifies_and_no_wrong_one_does():
    for n in range(1, 65):
        rn = T.root(D[:n])
        for m in range(1, n + 1):
            rm = T.root(D[:m])
            p = T.consistency_proof(m, D[:n])
            assert T.verify_consistency(m, n, rm, rn, p)
            if m < n:
                other = T.root(D[:m - 1] + [b"forked"])
                assert not T.verify_consistency(m, n, other, rn, p)
                assert not T.verify_consistency(
                    m, n, rm, rn[:-1] + bytes([rn[-1] ^ 1]), p)


def test_a_root_does_not_fix_its_size_so_a_checkpoint_binds_both():
    """RFC 9162 takes the size as an input to verification: a root alone
    does not commit to it. Leaf 0's proof in a tree of three verifies as
    leaf 0 of a tree of four against the same root, because the two paths
    have one shape, and over the sizes to 64 this holds for 1,824 proofs of
    each kind. A checkpoint therefore states the size beside the root, and
    a cosignature covers both. A fork never verifies, at any size."""
    r3 = T.root(D[:3])
    assert T.verify_inclusion(0, 4, T.leaf_hash(D[0]), T.inclusion_proof(
        0, D[:3]), r3)
    assert T.verify_consistency(1, 3, T.root(D[:1]), T.root(D[:4]),
                                T.consistency_proof(1, D[:4]))


def test_proofs_are_logarithmic():
    for n in (2, 3, 64, 1000, 4097):
        entries = [hashlib.sha256(i.to_bytes(4, "big")).digest()
                   for i in range(n)]
        depth = (n - 1).bit_length()
        assert len(T.inclusion_proof(n // 2, entries)) <= depth
        assert len(T.consistency_proof(n // 3 or 1, entries)) <= depth + 1


@pytest.mark.parametrize("call", [
    lambda: T.inclusion_proof(3, D[:3]),
    lambda: T.inclusion_proof(-1, D[:3]),
    lambda: T.consistency_proof(0, D[:3]),
    lambda: T.consistency_proof(4, D[:3]),
])
def test_a_proof_of_what_is_not_there_is_refused(call):
    with pytest.raises(T.ProofError):
        call()


def test_a_proof_of_another_length_than_the_path_is_refused():
    """The path's length is fixed by the position and the size. A proof
    one hash short, offered against the root it does reach, and a proof
    one hash long, built so that it reaches the root, are both refused."""
    short = T.inclusion_proof(0, D[:2])
    assert not T.verify_inclusion(0, 3, T.leaf_hash(D[0]), short,
                                  T.root(D[:2]))
    longer = [T.leaf_hash(D[1])]
    assert not T.verify_inclusion(0, 1, T.leaf_hash(D[0]), longer,
                                  T.node_hash(T.leaf_hash(D[1]),
                                              T.leaf_hash(D[0])))


def test_equal_sizes_take_no_proof():
    r = T.root(D[:4])
    assert T.verify_consistency(4, 4, r, r, [])
    assert not T.verify_consistency(4, 4, r, r, [bytes(32)])


def test_a_position_outside_the_tree_never_verifies():
    """Where the RFC's loop alone would accept: one leaf, no proof, and the
    position past it or before it."""
    one = T.root(D[:1])
    assert not T.verify_inclusion(1, 1, T.leaf_hash(D[0]), [], one)
    assert not T.verify_inclusion(-1, 1, T.leaf_hash(D[0]), [], one)
    r = T.root(D[:4])
    assert not T.verify_inclusion(4, 4, T.leaf_hash(D[0]), [], r)
    assert not T.verify_inclusion(0, 0, T.leaf_hash(D[0]), [], T.root([]))
    assert not T.verify_consistency(0, 4, T.root([]), r, [])
    assert not T.verify_consistency(5, 4, r, r, [])


# ---- archived segments: a prefix's frontier ----------------------------------
@pytest.mark.parametrize("n", range(0, 9))
def test_a_frontier_folds_to_the_published_root(n):
    front = T.frontier(LEAVES[:n])
    assert len(front) == bin(n).count("1")
    want = ROOTS[n - 1] if n else hashlib.sha256(b"").hexdigest()
    assert T.frontier_root(front, n).hex() == want


def test_a_frontier_extended_is_the_frontier_of_the_whole():
    for n in range(0, 65):
        assert T.frontier_root(T.frontier(D[:n]), n) == T.root(D[:n])
        for a in range(0, n + 1):
            assert T.extend(T.frontier(D[:a]), a, D[a:n]) == \
                T.frontier(D[:n])


def test_every_segment_verifies_and_no_wrong_one_does():
    """Every contiguous segment of every tree up to 32 leaves verifies
    from its frontier and a consistency proof, and none verifies with a
    wrong root, reordered, one short, one entry altered, moved one place
    either way with that place's own frontier, or with a frontier hash
    bent."""
    for size in range(1, 33):
        r = T.root(D[:size])
        for a in range(size):
            fr = T.frontier(D[:a])
            for b in range(a + 1, size + 1):
                pr = T.consistency_proof(b, D[:size]) if b < size else []
                seg = D[a:b]
                assert T.verify_segment(a, seg, fr, size, r, pr)
                assert not T.verify_segment(a, seg, fr, size, r[::-1], pr)
                assert not T.verify_segment(a, seg[:-1] + [b"\x01" * 32],
                                            fr, size, r, pr)
                if b - a > 1:
                    assert not T.verify_segment(a, seg[::-1], fr, size, r,
                                                pr)
                    assert not T.verify_segment(a, seg[:-1], fr, size, r,
                                                pr)
                if a > 0:
                    earlier = T.consistency_proof(b - 1, D[:size])
                    assert not T.verify_segment(a - 1, seg,
                                                T.frontier(D[:a - 1]), size,
                                                r, earlier)
                    bent = [b"\x02" * 32] + fr[1:]
                    assert not T.verify_segment(a, seg, bent, size, r, pr)
                if b < size:
                    later = T.consistency_proof(b + 1, D[:size]) \
                        if b + 1 < size else []
                    assert not T.verify_segment(a + 1, seg,
                                                T.frontier(D[:a + 1]), size,
                                                r, later)


@pytest.mark.parametrize("front, size", [
    ([b"\x00" * 32], True), ([b"\x00" * 32], -1), ([b"\x00" * 32], 1.0),
    ([b"\x00" * 32], 0), ([], 1), ([b"\x00" * 31], 1), (["x" * 32], 1),
])
def test_a_frontier_that_is_not_one_is_refused(front, size):
    with pytest.raises(T.ProofError):
        T.frontier_root(front, size)
    with pytest.raises(T.ProofError):
        T.extend(front, size, [D[0]])


def test_a_segment_that_is_not_one_is_refused():
    r = T.root(D[:8])
    assert T.verify_segment(2, D[2:4], T.frontier(D[:2]), 8, r,
                            T.consistency_proof(4, D[:8]))
    # nothing is not a segment, though the frontier it stands on is true
    assert not T.verify_segment(2, [], T.frontier(D[:2]), 8, r,
                                T.consistency_proof(2, D[:8]))
    # a start that is not a number, and one that is a truth value
    assert not T.verify_segment("2", D[2:4], T.frontier(D[:2]), 8, r,
                                T.consistency_proof(4, D[:8]))
    assert not T.verify_segment(True, D[1:2], T.frontier(D[:1]), 8, r,
                                T.consistency_proof(2, D[:8]))
    # a segment past the tree's end, and a frontier not of its start
    assert not T.verify_segment(6, D[6:8] + [D[0]], T.frontier(D[:6]), 8,
                                r, [])
    assert not T.verify_segment(2, D[2:4], T.frontier(D[:3]), 8, r,
                                T.consistency_proof(4, D[:8]))
