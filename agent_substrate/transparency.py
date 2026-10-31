"""Merkle transparency commitments over the event log (RFC 9162, 2.1).

THE LIMIT THIS ADDRESSES. A witness (:mod:`witness`) learns that a new head
extends the last one it cosigned by being shown every record in between:
for a hash chain those records ARE the consistency proof, so the proof is
as long as the history added since. A reader who wants to know that one
record belongs to a witnessed history must hold the history up to it. A
Merkle tree over the same records answers both in O(log n) hashes:

* an INCLUSION proof, that the record at position i is the i-th leaf of the
  tree a root commits to;
* a CONSISTENCY proof, that the tree of the first m records is a prefix of
  the tree of the first n.

THE TREE. RFC 9162's Merkle Tree Hash with its domain separation, over the
log's event hashes in order: a leaf is SHA-256(0x00 || h), with h the 32
bytes of the event's hash, and an interior node SHA-256(0x01 || left ||
right). The split is at the largest power of two below the size, so the
tree of the first m leaves is a subtree-for-subtree prefix of every larger
one. The chain stays exactly as it is: the tree is computed from a verified
chain and adds a commitment, not an authority.

WHAT A ROOT DOES NOT ESTABLISH. That anything in the records is true: a
root binds the records, it does not judge them. Nor its own size: RFC
9162's verification takes the size as an input, and a proof for one size
can verify against the same root at another of the same shape. A
checkpoint states the size beside the root, and is what binds them. Nor
where the root came from: a root handed out with its own proof is the
producer's word, and an inclusion proof against it shows only that the
producer can make the two agree. A root establishes a history when it was
obtained independently of the producer, as a witness cosigns it.
"""
from __future__ import annotations

import hashlib

EMPTY_ROOT = hashlib.sha256(b"").digest()


class ProofError(ValueError):
    """A proof that does not prove what it is offered for."""


def leaf_hash(entry: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + entry).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """The largest power of two strictly below ``n`` (n >= 2)."""
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def root(entries: list) -> bytes:
    """MTH(D[n]) of RFC 9162 section 2.1.1."""
    n = len(entries)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaf_hash(entries[0])
    k = _split(n)
    return node_hash(root(entries[:k]), root(entries[k:]))


def inclusion_proof(index: int, entries: list) -> list:
    """PATH(m, D[n]) of RFC 9162 section 2.1.3.1."""
    n = len(entries)
    if not 0 <= index < n:
        raise ProofError(f"no leaf {index} in a tree of {n}")
    if n == 1:
        return []
    k = _split(n)
    if index < k:
        return inclusion_proof(index, entries[:k]) + [root(entries[k:])]
    return inclusion_proof(index - k, entries[k:]) + [root(entries[:k])]


def _subproof(m: int, entries: list, whole: bool) -> list:
    n = len(entries)
    if m == n:
        return [] if whole else [root(entries)]
    k = _split(n)
    if m <= k:
        return _subproof(m, entries[:k], whole) + [root(entries[k:])]
    return _subproof(m - k, entries[k:], False) + [root(entries[:k])]


def consistency_proof(m: int, entries: list) -> list:
    """PROOF(m, D[n]) of RFC 9162 section 2.1.4.1, for 0 < m <= n."""
    if not 0 < m <= len(entries):
        raise ProofError(f"no consistency proof from {m} to {len(entries)}")
    return _subproof(m, entries, True)


def verify_inclusion(index: int, size: int, leaf: bytes, proof: list,
                     expected_root: bytes) -> bool:
    """RFC 9162 section 2.1.3.2."""
    if not 0 <= index < size:
        return False
    fn, sn, r = index, size - 1, leaf
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == expected_root


def verify_consistency(first: int, second: int, first_root: bytes,
                       second_root: bytes, proof: list) -> bool:
    """RFC 9162 section 2.1.4.2, for 0 < first <= second."""
    if not 0 < first <= second:
        return False
    if first == second:
        return not proof and first_root == second_root
    path = list(proof)
    if first & (first - 1) == 0:
        path = [first_root] + path
    if not path:
        return False
    fn, sn = first - 1, second - 1
    while fn & 1 and fn > 0:
        fn >>= 1
        sn >>= 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    return fr == first_root and sr == second_root and sn == 0


def entries_of(events) -> list:
    """The tree's entries for a verified log: each event's hash, as bytes."""
    return [bytes.fromhex(e.hash) for e in events]
