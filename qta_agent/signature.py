"""Who signs, with what, and on whose assurance: the signature-provider seam.

WHY A SEAM

Actor authentication (D-2026-89) rests on one primitive, Ed25519. The only
implementation in this package is :mod:`qta_agent.ed25519`, a transcription
of RFC 8032 held to the RFC's own vectors. That makes it a REFERENCE: a good
oracle, and the right signer for deterministic TEST identities. It is not
enough assurance to become the production trust provider by default, and the
reason is not only that it is not constant time:

* implementation assurance -- one transcription, reviewed by its author, with
  no independent audit, no fuzzing history and no deployment record;
* encoding edge cases -- point decoding, non-canonical field elements and
  scalars are handled by the RFC's rules and tested, and are exactly where
  handwritten implementations have historically diverged from vetted ones;
* small-order points and the cofactor -- vetted libraries differ among
  THEMSELVES on small-order keys, and a transcription picks one behaviour
  without a reviewed reason;
* malleability -- ``s + Q`` is refused and tested; the class is wider;
* review surface -- field arithmetic that nobody else maintains.

A :class:`SignatureProvider` therefore states its SCHEME and its ASSURANCE.
``REFERENCE_ONLY`` may verify anything and sign only TEST identities.
``VETTED`` is what a deployment injects from OUTSIDE this package -- an
adapter over ``cryptography`` or libsodium, say -- because the substrate
imports nothing beyond the standard library and will not start. No vetted
provider is configured in this repository: production authentication refuses
without one (EXTERNALLY_BLOCKED), and choosing and declaring one is a
supply-chain decision for the deployment.

THE CONFORMANCE GATE

A provider is accepted for production only if it (a) says ``VETTED`` and (b)
agrees with RFC 8032's vectors and refuses what every conforming Ed25519
refuses: another message, a flipped bit, another key, a short signature, a
short key, and the malleable ``s + Q``. The gate checks behaviour, not the
label alone -- a provider claiming VETTED that verifies a malleable signature
is refused by name. Passing it says the provider behaves like Ed25519 on
these inputs; it does not certify the provider.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import ed25519

SCHEME = "ed25519"
REFERENCE_ONLY = "REFERENCE_ONLY"
VETTED = "VETTED"
ASSURANCES = frozenset({REFERENCE_ONLY, VETTED})

#: RFC 8032 section 7.1, TEST 1-3: secret, public, message, signature (hex).
RFC8032_VECTORS = (
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
     "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
)


class ProviderError(Exception):
    """A signature provider this package will not use for what was asked."""


@dataclass(frozen=True)
class ReferenceEd25519:
    """:mod:`qta_agent.ed25519` behind the provider interface. Verifies;
    signs only what the caller has marked a TEST identity."""

    provider_id: str = "qta-agent/reference-ed25519/v1"
    scheme: str = SCHEME
    assurance: str = REFERENCE_ONLY

    def public_key(self, secret: bytes) -> bytes:
        return ed25519.public_key(secret)

    def sign(self, secret: bytes, message: bytes) -> bytes:
        return ed25519.sign(secret, message)

    def verify(self, public: bytes, message: bytes,
               signature: bytes) -> bool:
        return ed25519.verify(public, message, signature)


REFERENCE = ReferenceEd25519()


def conformance_problems(provider) -> list:
    """How ``provider`` departs from Ed25519 on the RFC's vectors and on what
    every conforming implementation refuses. Empty means it behaved.

    Every call is guarded: a provider that RAISES on a malformed input rather
    than answering False is a finding too -- a verifier that throws is one a
    caller can mistake for a pass by catching too broadly.
    """
    problems: list = []

    def ask(what, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:                # noqa: BLE001 - reported
            problems.append(f"{what}: raised {type(exc).__name__}")
            return None

    for n, (sk, pk, msg, sig) in enumerate(RFC8032_VECTORS, 1):
        sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
        if ask(f"vector {n} public key", provider.public_key, sk) != pk:
            problems.append(f"vector {n}: public key differs from RFC 8032")
        if ask(f"vector {n} sign", provider.sign, sk, msg) != sig:
            problems.append(f"vector {n}: signature differs from RFC 8032")
        verified = ask(f"vector {n} verify", provider.verify, pk, msg, sig)
        if verified is not True:
            problems.append(f"vector {n}: the RFC's own signature does not "
                            "verify")
    sk, pk, _, sig = (bytes.fromhex(x) for x in RFC8032_VECTORS[1])
    msg = bytes.fromhex(RFC8032_VECTORS[1][2])
    other_pk = bytes.fromhex(RFC8032_VECTORS[0][1])
    s = int.from_bytes(sig[32:], "little") + ed25519.Q
    refusals = {
        "another message": (pk, msg + b"x", sig),
        "a flipped signature bit": (pk, msg, sig[:-1] + bytes([sig[-1] ^ 1])),
        "another key": (other_pk, msg, sig),
        "a short signature": (pk, msg, sig[:32]),
        "a short key": (pk[:31], msg, sig),
        "the malleable s + Q": (pk, msg,
                                sig[:32] + int.to_bytes(s, 32, "little")),
    }
    for what, args in refusals.items():
        if ask(f"verify with {what}", provider.verify, *args) is not False:
            problems.append(f"accepts {what}")
    return problems


def require_vetted(provider):
    """``provider``, if it may stand behind PRODUCTION authentication.

    Refuses a provider that does not say VETTED, one of another scheme, and
    one that says VETTED and does not behave like Ed25519.
    """
    assurance = getattr(provider, "assurance", None)
    if assurance not in ASSURANCES:
        raise ProviderError(f"{provider!r} states no known assurance")
    if assurance != VETTED:
        raise ProviderError(
            f"{getattr(provider, 'provider_id', provider)!r} is "
            f"{assurance}: a reference implementation is an oracle and a "
            "test-identity signer, not a production trust provider")
    if getattr(provider, "scheme", None) != SCHEME:
        raise ProviderError(f"{provider!r} is not an {SCHEME} provider")
    problems = conformance_problems(provider)
    if problems:
        raise ProviderError(
            f"{getattr(provider, 'provider_id', provider)!r} says VETTED "
            "and does not behave like Ed25519: " + "; ".join(problems))
    return provider
