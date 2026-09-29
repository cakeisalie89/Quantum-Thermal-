"""Ed25519 signatures (RFC 8032, section 5.1), standard library only.

Why it is here at all: the agent substrate imports nothing outside the
standard library, and the standard library has no public-key signature. The
algorithm is the RFC's own, point for point -- extended twisted Edwards
coordinates, SHA-512, the cofactored group of order ``Q`` -- and
``tests/test_actor_authentication.py`` holds it to the RFC's test vectors.

What it is NOT: a constant-time implementation. Python integers leak timing
through their size, so this is safe for VERIFYING (every input is public)
and for signing with the deterministic TEST identities, and it is not the
signer a production key should be held by. Production signing is part of
the key provisioning this repository leaves external (see
:mod:`qta_agent.principals`).
"""
from __future__ import annotations

import hashlib

#: The field prime and the group order.
P = 2 ** 255 - 19
Q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


_D = -121665 * _inv(121666) % P
_SQRT_M1 = pow(2, (P - 1) // 4, P)


def _recover_x(y: int, sign: int):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % P)
_IDENTITY = (0, 1, 1, 0)


def _add(p1, p2):
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % P
    c = 2 * p1[3] * p2[3] * _D % P
    d = 2 * p1[2] * p2[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s: int, pt):
    acc = _IDENTITY
    while s > 0:
        if s & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        s >>= 1
    return acc


def _equal(p1, p2) -> bool:
    return ((p1[0] * p2[2] - p2[0] * p1[2]) % P == 0
            and (p1[1] * p2[2] - p2[1] * p1[2]) % P == 0)


def _compress(pt) -> bytes:
    zi = _inv(pt[2])
    x, y = pt[0] * zi % P, pt[1] * zi % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(secret: bytes):
    if len(secret) != 32:
        raise ValueError("an Ed25519 secret key is 32 bytes")
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _hq(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % Q


def public_key(secret: bytes) -> bytes:
    """The 32-byte public key of a 32-byte secret."""
    a, _ = _expand(secret)
    return _compress(_mul(a, _G))


def sign(secret: bytes, message: bytes) -> bytes:
    """A 64-byte signature. Deterministic: no randomness to get wrong."""
    a, prefix = _expand(secret)
    pub = _compress(_mul(a, _G))
    r = _hq(prefix + message)
    rs = _compress(_mul(r, _G))
    s = (r + _hq(rs + pub + message) * a) % Q
    return rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a signature by ``public``'s secret over ``message``.
    Malformed input is False, never an exception: callers verify bytes an
    adversary may have written."""
    if not isinstance(public, (bytes, bytearray)) or len(public) != 32:
        return False
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
        return False
    a_pt = _decompress(bytes(public))
    r_pt = _decompress(bytes(signature[:32]))
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= Q:
        return False
    h = _hq(bytes(signature[:32]) + bytes(public) + message)
    return _equal(_mul(s, _G), _add(r_pt, _mul(h, a_pt)))
