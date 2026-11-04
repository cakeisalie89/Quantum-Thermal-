"""Keys no secret stands behind, and encodings that are no point (AUD-026).

FOUND IN REVIEW (Stage 5). The key registry checked a public key only for
64 lowercase hex digits, and neither Ed25519 verifier refused a key of
SMALL ORDER -- one of the eight points with [8]A = 0. Under such a key no
secret is needed: [S]B = R + [k]A holds for R = [S]B whenever [k]A is the
identity, so anyone could sign as the principal registered with it, and a
principal could register one to make its own records deniable. And the two
implementations disagreed on one encoding: the producer
(``agent_substrate/ed25519.py``, transcribing the RFC's reference code)
tested x^2 against zero before reducing it, so y = p - 1 with the sign bit
set decoded as the point (0, -1), which RFC 8032 5.1.3 step 4 refuses and
the restated verifier (``tools/verify_assurance.py``) did refuse.

NOW. Both registries refuse a key that is not the canonical encoding of a
point, or is of small order; both verifiers refuse every signature under
one; both decoders refuse the non-canonical encoding. Every case below puts
the same bytes to both implementations and holds them to one verdict.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import ed25519 as E  # noqa: E402
from agent_substrate import principals as pr  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "verify_assurance_ed25519", ROOT / "tools" / "verify_assurance.py")
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)


def _torsion() -> dict:
    """The eight points of small order, FOUND rather than remembered: [L]P
    for points P read off a hash, until eight distinct ones are seen."""
    found: dict = {}
    i = 0
    while len(found) < 8:
        y = int.from_bytes(hashlib.sha256(b"torsion-%d" % i).digest(),
                           "little") % E.P
        i += 1
        x = E._recover_x(y, 0)
        if x is None:
            continue
        t = E._mul(E.Q, (x, y, 1, x * y % E.P))
        found[E._compress(t)] = t
    return found


TORSION = _torsion()
SMALL_ORDER = sorted(TORSION)
#: libsodium's list of the same eight encodings, to show the search found
#: what everyone means by them
KNOWN = {bytes.fromhex(h) for h in (
    "0100000000000000000000000000000000000000000000000000000000000000",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0000000000000000000000000000000000000000000000000000000000000080",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85")}
#: encodings that are no point: x = 0 with the sign bit set (the order-2
#: point and the identity), and y at or past p
NON_CANONICAL = {
    "(0, -1) with the sign bit": int.to_bytes((E.P - 1) | (1 << 255), 32,
                                              "little"),
    "the identity with the sign bit": int.to_bytes(1 | (1 << 255), 32,
                                                   "little"),
    "y = p": int.to_bytes(E.P, 32, "little"),
    "y = p + 1": int.to_bytes(E.P + 1, 32, "little"),
}


#: the order-2 point (0, -1), canonically encoded
ORDER_TWO = bytes.fromhex(
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f")


def _forged(public: bytes, message: bytes, point=None) -> bytes:
    """A signature made with no secret: R = [S]B, for an S whose challenge
    k makes [k]A the identity. Valid under the cofactorless equation for
    any key of small order, honest arithmetic and all. ``point`` is the
    point an encoding that is no point would be taken for."""
    pt = point or E._decompress(public)
    for s in range(1, 10_000):
        r = E._compress(E._mul(s, E._G))
        k = int.from_bytes(hashlib.sha512(r + public + message).digest(),
                           "little") % E.Q
        if E._equal(E._mul(k, pt), E._IDENTITY):
            return r + int.to_bytes(s, 32, "little")
    raise AssertionError("no challenge annihilated the key")


def _registry_doc(public: bytes) -> dict:
    alice = pr.test_identity("alice")
    doc = pr.KeyRegistry([alice.registered()],
                         allow_test_keys=True).to_document()
    entry = dict(doc["keys"][0])
    entry.update(public=public.hex(), key_id=pr.key_id(public),
                 principal="mallory")
    doc["keys"].append(entry)
    return doc


def test_the_search_finds_the_eight_points_everyone_names():
    assert set(SMALL_ORDER) == KNOWN
    for pt in TORSION.values():
        assert E._small_order(pt)


@pytest.mark.parametrize("public", SMALL_ORDER, ids=lambda b: b.hex()[:8])
def test_a_small_order_key_lets_anyone_sign_and_is_refused_everywhere(
        public):
    message = b"a record anyone could have attested"
    sig = _forged(public, message)
    # the forgery is real: the bare equation holds for it
    a, r = E._decompress(public), E._decompress(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    k = E._hq(sig[:32] + public + message)
    assert E._equal(E._mul(s, E._G), E._add(r, E._mul(k, a)))
    # and neither verifier takes it
    assert E.verify(public, message, sig) is False
    assert V.ed25519_verify(public, message, sig) is False
    # nor does either registry hold the key
    assert "small order" in E.public_key_problem(public)
    with pytest.raises(pr.AuthenticationError, match="small order"):
        pr.KeyRegistry([pr.RegisteredKey(pr.key_id(public), "mallory",
                                         public)])
    with pytest.raises(pr.AuthenticationError, match="small order"):
        pr.KeyRegistry.from_document(_registry_doc(public),
                                     allow_test_keys=True)
    with pytest.raises(V.Refused):
        V.read_key_registry(_registry_doc(public), allow_test_keys=True)


@pytest.mark.parametrize("name", sorted(NON_CANONICAL))
def test_an_encoding_that_is_no_point_is_refused_by_both(name):
    raw = NON_CANONICAL[name]
    assert E._decompress(raw) is None
    assert V._ed_point(raw) is None
    assert "canonical" in E.public_key_problem(raw)
    with pytest.raises(V.Refused):
        V.read_key_registry(_registry_doc(raw), allow_test_keys=True)
    alice = pr.test_identity("alice")
    message = b"m"
    sig = alice.provider.sign(alice.secret, message)
    assert E.verify(raw, message, sig) is False
    assert V.ed25519_verify(raw, message, sig) is False
    # as R, too: the honest S under a non-point R is no signature
    bent = raw + sig[32:]
    assert E.verify(alice.public, message, bent) is False
    assert V.ed25519_verify(alice.public, message, bent) is False


def test_the_two_verifiers_agree_on_every_crafted_input():
    """The disagreement the review found, closed: the same bytes, one
    verdict, for every small-order key and non-canonical encoding as key or
    as R, with forged and honest signatures alike."""
    alice = pr.test_identity("alice")
    message = b"agreement"
    honest = alice.provider.sign(alice.secret, message)
    keys = [*SMALL_ORDER, *NON_CANONICAL.values(), alice.public]
    for public in keys:
        sigs = [honest, honest[:32] + bytes(32)]
        if public in TORSION:
            sigs.append(_forged(public, message))
        if public == NON_CANONICAL["(0, -1) with the sign bit"]:
            # the producer once took this for (0, -1) and the restated
            # verifier did not: a forgery under it is where they parted
            sigs.append(_forged(public, message, TORSION[ORDER_TWO]))
        sigs += [raw + honest[32:] for raw in NON_CANONICAL.values()]
        for sig in sigs:
            assert E.verify(public, message, sig) == \
                V.ed25519_verify(public, message, sig), (public.hex(),
                                                         sig.hex())


def test_genuine_keys_are_unaffected():
    for n in range(32):
        who = pr.test_identity(f"party-{n}")
        assert E.public_key_problem(who.public) is None
        msg = b"x" * n
        sig = who.provider.sign(who.secret, msg)
        assert E.verify(who.public, msg, sig)
        assert V.ed25519_verify(who.public, msg, sig)
