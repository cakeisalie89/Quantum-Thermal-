"""Authenticated actors: an event's actor, bound to a key the log does not
choose.

WHAT THIS CLOSES, AND WHAT IT DOES NOT

Every reader in this package re-authorizes a history against the state
machines, and the hash chain makes the history tamper-evident. Neither makes
it AUTHENTIC. An actor is a name: a writer able to append -- or to rewrite
the whole file and its head witness consistently -- can put a complete,
rule-abiding lifecycle under any names it likes, and no replay refuses it
(plan 9.7, "Actors are names, not keys").

This module is the seam that closes it: an ATTESTATION binds one event's
hash to a signature by a key, and a KEY REGISTRY -- supplied from outside
the log, because a key registered in the same log is chosen by the same
writer it is meant to check -- says which principal each key speaks for.
:func:`authenticate` refuses, for every event whose actor is required to
authenticate: a missing attestation, a key the registry does not hold, a
key registered to another principal (actor substitution, a wrong key), and
a signature that does not verify over the event's hash (a tampered payload
or signature, or a rewritten history). The hash is over the whole event
body -- position, previous hash, actor, action, target, payload -- so the
signature covers all of it.

WHERE ATTESTATIONS LIVE. Beside the log, not in it. The log refuses any
field its hash does not cover (an unhashed field could carry unverified
content next to a valid digest), and adding the signature to the hashed body
would change the canonical form every existing log was written in. An
attestation file needs no trust of its own: each line is checked by its
signature, and a line removed is an event unattested.

WHAT IS PENDING, AND EXTERNAL. No production key exists in this repository
and none is invented. :data:`PRODUCTION_KEYS` is not configured, and
:func:`production_registry` refuses rather than returning an empty registry
that would authenticate nothing and look as though it had. Deterministic TEST
identities exist for the tests (:func:`test_identity`); their key ids are
marked, and a registry built for production refuses them. Provisioning real
keys -- who holds them, where, how they rotate -- is a deployment decision.

THE PRIMITIVE. Ed25519 (RFC 8032), in :mod:`qta_agent.ed25519`, standard
library only and checked against the RFC's own vectors. It is not constant
time: fine for VERIFYING public data, not a signer to hold production keys
with. A production signer is part of the pending provisioning.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import ed25519
from .canonical import canonical_bytes

SCHEME = "ed25519"
REGISTRY_SCHEMA = "qta-agent/key-registry/v1"
#: Domain separation: a signature made to attest an event is not a
#: signature for anything else, and nothing else's is one for this.
DOMAIN = b"qta-agent/event-attestation/v1\x00"
TEST_KEY_PREFIX = "test:"
_TEST_SEED = b"qta-agent/test-identity/v1\x00"

#: Where production keys come from: a key-registry document provisioned by
#: the deployment. ``None`` -- not configured, and deliberately so: no
#: production key is invented here.
PRODUCTION_KEYS = None

# Finding kinds. Stable strings: tests and the second reader match on them.
MISSING = "MISSING"
UNKNOWN_KEY = "UNKNOWN_KEY"
WRONG_PRINCIPAL = "WRONG_PRINCIPAL"
BAD_SIGNATURE = "BAD_SIGNATURE"
NO_KEY = "NO_KEY"
DANGLING = "DANGLING"
MALFORMED = "MALFORMED"


class AuthenticationError(Exception):
    """A key, a registry or an attestation this module refuses."""


class NotConfigured(AuthenticationError):
    """No production key registry is provisioned."""


def key_id(public: bytes, *, test: bool = False) -> str:
    """A key's name, DERIVED from the key: a registry cannot give one key
    two names, or two keys one."""
    if len(public) != 32:
        raise AuthenticationError("an Ed25519 public key is 32 bytes")
    kid = f"{SCHEME}:{hashlib.sha256(public).hexdigest()[:32]}"
    return TEST_KEY_PREFIX + kid if test else kid


@dataclass(frozen=True)
class RegisteredKey:
    key_id: str
    principal: str
    public: bytes

    @property
    def is_test(self) -> bool:
        return self.key_id.startswith(TEST_KEY_PREFIX)


class KeyRegistry:
    """Which principal each key speaks for. Supplied from OUTSIDE the log:
    a key registered in the log it authenticates is chosen by whoever writes
    that log."""

    def __init__(self, keys, *, allow_test_keys: bool = False):
        by_id: dict = {}
        for k in keys:
            if not isinstance(k.principal, str) or not k.principal:
                raise AuthenticationError("a key must name its principal")
            if k.key_id != key_id(k.public, test=k.is_test):
                raise AuthenticationError(
                    f"{k.key_id!r} is not the id of the key it names; ids "
                    "are derived from keys, never chosen")
            if k.is_test and not allow_test_keys:
                raise AuthenticationError(
                    f"{k.key_id!r} is a TEST key and this registry was not "
                    "built for tests; a test identity's secret is derivable "
                    "from its name by anyone")
            if k.key_id in by_id:
                raise AuthenticationError(f"{k.key_id!r} registered twice")
            by_id[k.key_id] = k
        self._keys = by_id

    @property
    def principals(self) -> frozenset:
        return frozenset(k.principal for k in self._keys.values())

    def get(self, kid):
        return self._keys.get(kid) if isinstance(kid, str) else None

    def to_document(self) -> dict:
        return {"schema": REGISTRY_SCHEMA, "keys": [
            {"key_id": k.key_id, "principal": k.principal, "scheme": SCHEME,
             "public": k.public.hex()}
            for k in sorted(self._keys.values(), key=lambda k: k.key_id)]}

    @classmethod
    def from_document(cls, doc, *, allow_test_keys: bool = False):
        if not isinstance(doc, dict) or doc.get("schema") != REGISTRY_SCHEMA:
            raise AuthenticationError(
                f"not a {REGISTRY_SCHEMA} document")
        keys = []
        for entry in doc.get("keys") or ():
            if set(entry) != {"key_id", "principal", "scheme", "public"} \
                    or entry["scheme"] != SCHEME:
                raise AuthenticationError(f"unreadable key entry {entry!r}")
            try:
                public = bytes.fromhex(entry["public"])
            except (TypeError, ValueError) as exc:
                raise AuthenticationError(f"{entry['key_id']!r}: {exc}") \
                    from None
            keys.append(RegisteredKey(entry["key_id"], entry["principal"],
                                      public))
        return cls(keys, allow_test_keys=allow_test_keys)


def production_registry() -> KeyRegistry:
    """The deployment's registry. Refuses while none is provisioned rather
    than answering with an empty one, which would authenticate nothing and
    read as though it had checked."""
    if PRODUCTION_KEYS is None:
        raise NotConfigured(
            "no production key registry is provisioned: actor "
            "authentication is PENDING external key provisioning. Nothing "
            "in this repository holds a production key, and nothing will "
            "invent one")
    return KeyRegistry.from_document(
        json.loads(Path(PRODUCTION_KEYS).read_text(encoding="utf-8")))


def _message(event_hash: str) -> bytes:
    return DOMAIN + event_hash.encode("ascii")


@dataclass(frozen=True)
class Signer:
    """A principal's secret key. :func:`test_identity` makes the only ones
    this repository has."""

    principal: str
    secret: bytes = field(repr=False)
    test: bool = False

    @property
    def public(self) -> bytes:
        return ed25519.public_key(self.secret)

    @property
    def key_id(self) -> str:
        return key_id(self.public, test=self.test)

    def registered(self) -> RegisteredKey:
        return RegisteredKey(self.key_id, self.principal, self.public)

    def attest(self, event) -> dict:
        """Sign ``event``'s hash -- which covers its position, the previous
        hash, actor, action, target and payload."""
        if event.actor != self.principal:
            raise AuthenticationError(
                f"{self.principal!r} may attest only its own events; this "
                f"one names {event.actor!r}")
        return {"seq": event.seq, "event_hash": event.hash,
                "key_id": self.key_id,
                "sig": ed25519.sign(self.secret, _message(event.hash)).hex()}


def test_identity(principal: str) -> Signer:
    """A deterministic TEST identity: the same name, the same key, on every
    machine. Its secret is derivable from its name by anyone -- which is why
    its key id is marked, and a production registry refuses it."""
    return Signer(principal,
                  hashlib.sha256(_TEST_SEED + principal.encode()).digest(),
                  test=True)


class Attestations:
    """The attestation file beside a log: one JSON object per line. It needs
    no trust of its own -- each line is checked by its signature, and a line
    removed is an event unattested."""

    def __init__(self, path):
        self.path = Path(path)

    def add(self, attestation: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as fh:
            fh.write(canonical_bytes(attestation) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read(self) -> tuple:
        """``(by_event_hash, malformed_line_numbers)``."""
        by_hash: dict = {}
        malformed = []
        if not self.path.exists():
            return by_hash, malformed
        for n, line in enumerate(self.path.read_bytes().splitlines(), 1):
            try:
                att = json.loads(line)
            except ValueError:
                malformed.append(n)
                continue
            if not isinstance(att, dict) or set(att) != {
                    "seq", "event_hash", "key_id", "sig"}:
                malformed.append(n)
                continue
            by_hash.setdefault(att["event_hash"], []).append(att)
        return by_hash, malformed


def signed_append(log, attestations: Attestations, signer: Signer,
                  **fields):
    """Append as ``signer``'s principal and attest the event. The actor IS
    the signer: there is no parameter to name anybody else."""
    if "actor" in fields:
        raise AuthenticationError(
            "the actor of a signed append is the signer; it is not a "
            "parameter")
    ev = log.append(actor=signer.principal, **fields)
    attestations.add(signer.attest(ev))
    return ev


@dataclass
class AuthenticationReport:
    authenticated: int = 0
    #: ``(seq, kind, detail)``; seq is None for a finding about the file.
    findings: list = field(default_factory=list)
    #: Events refused: required to authenticate, and not.
    refused: set = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.findings

    def lines(self) -> list:
        return [f"seq {s}: {k}: {d}" if s is not None else f"{k}: {d}"
                for s, k, d in self.findings]


def authenticate(events, attestations: Attestations, registry: KeyRegistry,
                 *, require=None, complete: bool = True
                 ) -> AuthenticationReport:
    """Every event whose actor is required to authenticate -- ``require``, a
    set of principals, or every actor when None -- must carry an attestation
    by a key the registry holds FOR THAT ACTOR, whose signature verifies over
    the event's hash."""
    by_hash, malformed = attestations.read()
    out = AuthenticationReport()
    for n in malformed:
        out.findings.append((None, MALFORMED,
                             f"attestation line {n} is not an attestation"))
    seen = set()
    for ev in events:
        if require is not None and ev.actor not in require:
            continue
        atts = by_hash.get(ev.hash, [])
        seen.add(ev.hash)
        if ev.actor not in registry.principals:
            out.findings.append((ev.seq, NO_KEY,
                                 f"no key is registered for {ev.actor!r}"))
            out.refused.add(ev.seq)
            continue
        if not atts:
            out.findings.append((ev.seq, MISSING,
                                 f"no attestation of this event by "
                                 f"{ev.actor!r}"))
            out.refused.add(ev.seq)
            continue
        why = []
        for att in atts:
            key = registry.get(att.get("key_id"))
            if key is None:
                why.append((UNKNOWN_KEY, f"{att.get('key_id')!r} is not a "
                            "registered key"))
            elif key.principal != ev.actor:
                why.append((WRONG_PRINCIPAL, f"signed by a key of "
                            f"{key.principal!r}, and the event names "
                            f"{ev.actor!r}"))
            elif att.get("seq") != ev.seq or not _verifies(key, ev.hash,
                                                             att):
                why.append((BAD_SIGNATURE, f"{key.key_id!r} did not sign "
                            "this event"))
            else:
                why = None
                break
        if why is None:
            out.authenticated += 1
        else:
            out.findings.extend((ev.seq, k, d) for k, d in why)
            out.refused.add(ev.seq)
    # An attestation of no event in the history is a finding only when the
    # history given is the WHOLE history; a catch-up passes only the tail.
    for h in sorted(set(by_hash) - seen) if complete else ():
        if require is None or any(a.get("key_id") and registry.get(
                a["key_id"]) and registry.get(a["key_id"]).principal
                in require for a in by_hash[h]):
            out.findings.append((None, DANGLING,
                                 f"an attestation names event hash "
                                 f"{h[:16]}..., which is in no event of "
                                 "this history"))
    return out


def _verifies(key: RegisteredKey, event_hash: str, att: dict) -> bool:
    sig = att.get("sig")
    try:
        raw = bytes.fromhex(sig) if isinstance(sig, str) else None
    except ValueError:
        raw = None
    return raw is not None and ed25519.verify(key.public,
                                              _message(event_hash), raw)


@dataclass(frozen=True)
class Authenticator:
    """What a reader is given to authenticate a history: the attestation
    file, the registry, and who must authenticate."""

    attestations: Attestations
    registry: KeyRegistry
    require: frozenset | None = None

    def __call__(self, events, *, complete: bool = True
                 ) -> AuthenticationReport:
        return authenticate(events, self.attestations, self.registry,
                            require=self.require, complete=complete)
