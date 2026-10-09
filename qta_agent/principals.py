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

THE PRIMITIVE, AND WHO STANDS BEHIND IT. Ed25519 (RFC 8032), behind the
provider seam of :mod:`qta_agent.signature`. The only implementation here,
:mod:`qta_agent.ed25519`, is a REFERENCE: it verifies, and it signs only TEST
identities -- a :class:`Signer` holding a production key refuses to sign with
it. Production authentication (:func:`production_authenticator`) needs a
provider the deployment injects, which states VETTED and passes the seam's
conformance gate, AND a provisioned registry. Neither exists in this
repository: EXTERNALLY_BLOCKED, and nothing here pretends otherwise.

THE PROFILE. An authenticator given to a reader is an option; a history
declared ``AUTHENTICATED_REQUIRED`` (:func:`begin_history`) is not. Its
declaration is the first event, and :mod:`qta_agent.events` enforces it in
the verified-read primitives every reader reads through: without an
authenticator, no event of such a history is read. :data:`READER_COVERAGE`
says, reader by reader, what that means -- and the tests hold each line.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import signature
from .canonical import canonical_bytes
from .events import ACT_SECURITY_PROFILE, PROFILE_AUTHENTICATED_REQUIRED

SCHEME = "ed25519"
REGISTRY_SCHEMA = "qta-agent/key-registry/v1"
#: v2 carries each key's LIFECYCLE, in log positions rather than wall time:
#: when it began to speak for its principal, until when, whether it was
#: revoked and from where, and whether a compromise was declared and from
#: where. A v1 document is still read: every key valid from genesis, for
#: good, and never revoked -- which is what v1 said.
REGISTRY_SCHEMA_V2 = "qta-agent/key-registry/v2"
#: Domain separation: a signature made to attest an event is not a
#: signature for anything else, and nothing else's is one for this.
DOMAIN = b"qta-agent/event-attestation/v1\x00"
TEST_KEY_PREFIX = "test:"
_TEST_SEED = b"qta-agent/test-identity/v1\x00"

#: Where production keys come from: a key-registry document provisioned by
#: the deployment. ``None`` -- not configured, and deliberately so: no
#: production key is invented here.
PRODUCTION_KEYS = None
#: The sha256 of that document's canonical form, pinned by the DEPLOYMENT.
#: A registry is trusted because it is the one the deployment named, not
#: because it parses: a file beside the log is as writable as the log.
PRODUCTION_REGISTRY_DIGEST = None

# Finding kinds. Stable strings: tests and the second reader match on them.
MISSING = "MISSING"
UNKNOWN_KEY = "UNKNOWN_KEY"
WRONG_PRINCIPAL = "WRONG_PRINCIPAL"
BAD_SIGNATURE = "BAD_SIGNATURE"
NO_KEY = "NO_KEY"
DANGLING = "DANGLING"
MALFORMED = "MALFORMED"
KEY_NOT_YET_VALID = "KEY_NOT_YET_VALID"
KEY_EXPIRED = "KEY_EXPIRED"
KEY_REVOKED = "KEY_REVOKED"
KEY_COMPROMISED = "KEY_COMPROMISED"
#: An abort record naming an event the history COMMITTED: a prepare that
#: was never aborted by any honest writer, since a writer aborts only what
#: lies past the head it holds the lock on.
ABORTED_COMMITTED = "ABORTED_COMMITTED"


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
    """A key, whom it speaks for, and WHERE in a history it may.

    Positions, not times: an event's seq is part of what its signature
    covers and what every replay agrees on, so "was this key good for this
    event" has one answer on every machine. A revocation refuses the key
    FROM ``revoked_at_seq`` on and leaves earlier authorship standing -- a
    key retired is not a key stolen. Only a declared compromise
    (``compromised_from_seq``) reaches back, and only as far as declared.
    """

    key_id: str
    principal: str
    public: bytes
    valid_from_seq: int = 0
    valid_until_seq: int | None = None
    revoked_at_seq: int | None = None
    revocation_reason: str | None = None
    compromised_from_seq: int | None = None
    replacement_key: str | None = None

    @property
    def is_test(self) -> bool:
        return self.key_id.startswith(TEST_KEY_PREFIX)

    @property
    def status(self) -> str:
        if self.revoked_at_seq is not None:
            return "REVOKED"
        if self.valid_until_seq is not None:
            return "ROTATED"
        return "ACTIVE"

    def refusal_at(self, seq: int):
        """``(kind, detail)`` when this key may not sign event ``seq``."""
        if (self.compromised_from_seq is not None
                and seq >= self.compromised_from_seq):
            return (KEY_COMPROMISED,
                    f"{self.key_id!r} is declared compromised from seq "
                    f"{self.compromised_from_seq}")
        if self.revoked_at_seq is not None and seq >= self.revoked_at_seq:
            return (KEY_REVOKED,
                    f"{self.key_id!r} was revoked at seq "
                    f"{self.revoked_at_seq} ({self.revocation_reason})")
        if seq < self.valid_from_seq:
            return (KEY_NOT_YET_VALID,
                    f"{self.key_id!r} speaks from seq {self.valid_from_seq}")
        if self.valid_until_seq is not None and seq > self.valid_until_seq:
            return (KEY_EXPIRED,
                    f"{self.key_id!r} spoke until seq {self.valid_until_seq}"
                    + (f"; {self.replacement_key!r} replaces it"
                       if self.replacement_key else ""))
        return None


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
            _check_lifecycle(k)
            by_id[k.key_id] = k
        for k in by_id.values():
            if k.replacement_key is not None:
                nxt = by_id.get(k.replacement_key)
                if nxt is None or nxt.principal != k.principal:
                    raise AuthenticationError(
                        f"{k.key_id!r} names {k.replacement_key!r} as its "
                        "replacement, which is not a key of the same "
                        "principal in this registry")
        self._keys = by_id

    @property
    def principals(self) -> frozenset:
        return frozenset(k.principal for k in self._keys.values())

    def get(self, kid):
        return self._keys.get(kid) if isinstance(kid, str) else None

    def to_document(self, *, version: int = 1) -> dict:
        """The registry as a v2 document (keys sorted, lifecycle explicit),
        whose canonical digest is what a deployment pins."""
        return {"schema": REGISTRY_SCHEMA_V2, "registry_version": version,
                "keys": [
                    {"key_id": k.key_id, "principal": k.principal,
                     "scheme": SCHEME, "public": k.public.hex(),
                     "status": k.status,
                     "valid_from_seq": k.valid_from_seq,
                     "valid_until_seq": k.valid_until_seq,
                     "revoked_at_seq": k.revoked_at_seq,
                     "revocation_reason": k.revocation_reason,
                     "compromised_from_seq": k.compromised_from_seq,
                     "replacement_key": k.replacement_key}
                    for k in sorted(self._keys.values(),
                                    key=lambda k: k.key_id)]}

    @classmethod
    def from_bytes(cls, data: bytes, *, allow_test_keys: bool = False,
                   pinned_digest: str | None = None):
        """A registry from the bytes of its file. Every way those bytes can
        fail to be a document -- not UTF-8, not JSON, nested deep enough to
        exhaust the parser -- is a refusal, never a crash."""
        try:
            doc = json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise AuthenticationError(
                "the key registry is not a JSON document "
                f"({type(exc).__name__})") from None
        return cls.from_document(doc, allow_test_keys=allow_test_keys,
                                 pinned_digest=pinned_digest)

    @classmethod
    def from_document(cls, doc, *, allow_test_keys: bool = False,
                      pinned_digest: str | None = None):
        """Read a registry document. With ``pinned_digest`` -- what a
        deployment supplies -- a document whose canonical digest differs is
        refused before a single entry is believed: a registry is trusted
        because it is the one the deployment named."""
        if pinned_digest is not None:
            try:
                pinned = registry_digest(doc) == pinned_digest
            except (ValueError, TypeError, RecursionError):
                pinned = False
            if not pinned:
                raise AuthenticationError(
                    "the key registry is not the one the deployment pinned: "
                    "its digest differs")
        if not isinstance(doc, dict) or doc.get("schema") not in (
                REGISTRY_SCHEMA, REGISTRY_SCHEMA_V2):
            raise AuthenticationError(
                f"not a {REGISTRY_SCHEMA} or {REGISTRY_SCHEMA_V2} document")
        v2 = doc["schema"] == REGISTRY_SCHEMA_V2
        if v2 and (not isinstance(doc.get("registry_version"), int)
                   or isinstance(doc.get("registry_version"), bool)
                   or set(doc) != {"schema", "registry_version", "keys"}):
            raise AuthenticationError("unreadable v2 registry header")
        entries = doc.get("keys")
        if not isinstance(entries, list) or len(entries) > MAX_KEYS:
            raise AuthenticationError(
                f"'keys' must be a list of at most {MAX_KEYS} entries")
        keys = [_entry_key(entry, v2) for entry in entries]
        return cls(keys, allow_test_keys=allow_test_keys)


def production_authenticator(attestations: "Attestations", *, provider,
                             require=None) -> "Authenticator":
    """The authenticator a PRODUCTION reader is given: the provisioned
    registry, and a provider that states VETTED and passes the conformance
    gate. Refuses while either is missing -- both are, here -- rather than
    falling back to the reference implementation or to an empty registry."""
    registry = production_registry()
    return Authenticator(attestations, registry, require,
                         signature.require_vetted(provider))


#: Bounds on a registry document: it is read from a file, and a file is
#: attacker-shaped input until it has been checked.
MAX_KEYS = 4096
_MAX_NAME = 256
_V1_FIELDS = frozenset({"key_id", "principal", "scheme", "public"})
_V2_FIELDS = _V1_FIELDS | {
    "status", "valid_from_seq", "valid_until_seq", "revoked_at_seq",
    "revocation_reason", "compromised_from_seq", "replacement_key"}


def registry_digest(doc) -> str:
    """The digest a deployment pins: sha256 of the canonical form."""
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def _seq_or_none(value, what: str):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AuthenticationError(f"{what} must be a seq (int >= 0) or null")
    return value


def _name(value, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_NAME:
        raise AuthenticationError(
            f"{what} must be a non-empty string of at most {_MAX_NAME} "
            "characters")
    return value


def _entry_key(entry, v2: bool) -> RegisteredKey:
    """One registry entry, every field typed before anything uses it."""
    if not isinstance(entry, dict) or set(entry) != (
            _V2_FIELDS if v2 else _V1_FIELDS):
        raise AuthenticationError("unreadable key entry")
    kid = _name(entry["key_id"], "key_id")
    if entry["scheme"] != SCHEME:
        raise AuthenticationError(f"{kid!r}: scheme is not {SCHEME}")
    pub = entry["public"]
    if not _canonical_hex(pub, 64):
        raise AuthenticationError(
            f"{kid!r}: public must be 64 lowercase hex digits")
    public = bytes.fromhex(pub)
    life = {}
    if v2:
        life = {
            "valid_from_seq": _seq_or_none(entry["valid_from_seq"],
                                           "valid_from_seq") or 0,
            "valid_until_seq": _seq_or_none(entry["valid_until_seq"],
                                            "valid_until_seq"),
            "revoked_at_seq": _seq_or_none(entry["revoked_at_seq"],
                                           "revoked_at_seq"),
            "revocation_reason": (None if entry["revocation_reason"] is None
                                  else _name(entry["revocation_reason"],
                                             "revocation_reason")),
            "compromised_from_seq": _seq_or_none(
                entry["compromised_from_seq"], "compromised_from_seq"),
            "replacement_key": (None if entry["replacement_key"] is None
                                else _name(entry["replacement_key"],
                                           "replacement_key")),
        }
    key = RegisteredKey(kid, _name(entry["principal"], "principal"), public,
                        **life)
    if v2 and entry["status"] != key.status:
        raise AuthenticationError(
            f"{kid!r}: status {entry['status']!r} contradicts its lifecycle "
            f"fields, which say {key.status}")
    return key


def _check_lifecycle(k: RegisteredKey) -> None:
    if k.valid_until_seq is not None and k.valid_until_seq < k.valid_from_seq:
        raise AuthenticationError(f"{k.key_id!r} expires before it begins")
    if (k.revoked_at_seq is None) != (k.revocation_reason is None):
        raise AuthenticationError(
            f"{k.key_id!r}: a revocation names both where and why")


def production_registry() -> KeyRegistry:
    """The deployment's registry, at the digest the deployment pinned.
    Refuses while either is unprovisioned rather than answering with an
    empty registry, which would authenticate nothing and read as though it
    had checked, or with an unpinned one, which would trust whatever file
    was there."""
    if PRODUCTION_KEYS is None or PRODUCTION_REGISTRY_DIGEST is None:
        raise NotConfigured(
            "no production key registry is provisioned: actor "
            "authentication is PENDING external key provisioning. Nothing "
            "in this repository holds a production key, and nothing will "
            "invent one")
    return KeyRegistry.from_bytes(Path(PRODUCTION_KEYS).read_bytes(),
                                  pinned_digest=PRODUCTION_REGISTRY_DIGEST)


def _message(event_hash: str) -> bytes:
    return DOMAIN + event_hash.encode("ascii")


@dataclass(frozen=True)
class Signer:
    """A principal's secret key. :func:`test_identity` makes the only ones
    this repository has."""

    principal: str
    secret: bytes = field(repr=False)
    test: bool = False
    provider: signature.SignatureProvider = field(
        default=signature.REFERENCE, repr=False)

    @property
    def public(self) -> bytes:
        return self.provider.public_key(self.secret)

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
        if (getattr(self.provider, "assurance", None) != signature.VETTED
                and not self.test):
            raise AuthenticationError(
                f"{getattr(self.provider, 'provider_id', self.provider)!r} "
                "is not a VETTED provider, and it signs only TEST "
                "identities: a production key is not held by a reference "
                "implementation")
        return {"seq": event.seq, "event_hash": event.hash,
                "key_id": self.key_id,
                "sig": self.provider.sign(self.secret,
                                          _message(event.hash)).hex()}


def test_identity(principal: str) -> Signer:
    """A deterministic TEST identity: the same name, the same key, on every
    machine. Its secret is derivable from its name by anyone -- which is why
    its key id is marked, and a production registry refuses it."""
    return Signer(principal,
                  hashlib.sha256(_TEST_SEED + principal.encode()).digest(),
                  test=True)


@dataclass
class AttestationFile:
    """One read of an attestation file."""
    #: event hash -> attestations naming it, each fully typed.
    by_hash: dict = field(default_factory=dict)
    #: line numbers that are neither an attestation nor an abort.
    malformed: list = field(default_factory=list)
    #: event hashes whose PREPARED attestation a writer aborted.
    aborted: set = field(default_factory=set)
    #: True when the last line is a partial write -- a prepare torn by a
    #: crash, never a committed attestation (the event is written only
    #: after its attestation is complete and durable).
    torn_tail: bool = False


_ATT_FIELDS = frozenset({"seq", "event_hash", "key_id", "sig"})
_ABORT_FIELDS = frozenset({"abort", "seq"})
#: An Ed25519 signature, as its signer writes it: 64 bytes, lowercase hex.
_SIG_HEX = 128
_LOWER_HEX = re.compile(r"[0-9a-f]*")


def _canonical_hex(value, length: int) -> bool:
    """Exactly ``length`` LOWERCASE hex digits -- the one encoding a signer
    writes. ``bytes.fromhex`` also takes uppercase and spaces, so without
    this one signature has many records, and a record no signer wrote
    would authenticate as though one had."""
    return (isinstance(value, str) and len(value) == length
            and _LOWER_HEX.fullmatch(value) is not None)


def _typed_attestation(att) -> bool:
    return (isinstance(att, dict) and set(att) == _ATT_FIELDS
            and isinstance(att["seq"], int)
            and not isinstance(att["seq"], bool) and att["seq"] >= 0
            and _canonical_hex(att["event_hash"], 64)
            and isinstance(att["key_id"], str)
            and 0 < len(att["key_id"]) <= _MAX_NAME
            and _canonical_hex(att["sig"], _SIG_HEX))


def _typed_abort(rec) -> bool:
    return (isinstance(rec, dict) and set(rec) == _ABORT_FIELDS
            and _canonical_hex(rec["abort"], 64)
            and isinstance(rec["seq"], int)
            and not isinstance(rec["seq"], bool) and rec["seq"] >= 0)


class Attestations:
    """The attestation file beside a log. It needs no trust of its own --
    each attestation is checked by its signature, and a line removed is an
    event unattested.

    HOW A SIGNED APPEND SURVIVES A CRASH. Two file appends are not atomic,
    and nothing here pretends they are. What is arranged instead is the
    ORDER: under the log's writer lock, the attestation of the exact record
    about to be written is made durable FIRST (:meth:`prepare`), then the
    record. So a crash leaves one of four states, and each has one meaning:

    * nothing written -- the append did not happen;
    * a torn last attestation line -- a prepare that did not finish; read as
      pending, and truncated by the next prepare;
    * a complete attestation of an event one past the history -- a prepare
      whose record never landed; PENDING, not a finding (a reader may see
      exactly this while another process holds the lock between the two
      writes), and ABORTED by the next prepare, which appends an abort
      record for it before its own attestation;
    * the record and its attestation -- a committed, authenticated event.

    What cannot arise is the one that would lose a committed event's
    authentication: a record durable without its attestation.
    """

    def __init__(self, path):
        self.path = Path(path)

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as fh:
            fh.write(canonical_bytes(record) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())

    def add(self, attestation: dict) -> None:
        """Append an attestation of an event ALREADY in the log. The signed
        write paths use :meth:`prepare`; this remains for attesting after
        the fact, which is only ever a repair."""
        self._append(attestation)

    def prepare(self, attestation: dict) -> None:
        """Make ``attestation`` durable BEFORE the record it attests is
        written. The caller holds the log's writer lock, so the head is
        ``attestation['seq'] - 1`` and nothing at or past the new seq can
        have been committed: a torn tail is truncated, and every earlier
        prepare at or past this seq is aborted, before this one is added."""
        self._repair_torn_tail()
        seen = self.read()
        at = attestation["seq"]
        stale = sorted({(a["seq"], h) for h, atts in seen.by_hash.items()
                        for a in atts
                        if a["seq"] >= at and h not in seen.aborted})
        for seq, h in stale:
            self._append({"abort": h, "seq": seq})
        self._append(attestation)

    def _repair_torn_tail(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        with self.path.open("r+b") as fh:
            fh.truncate(data.rfind(b"\n") + 1)
            fh.flush()
            os.fsync(fh.fileno())

    def read(self) -> AttestationFile:
        out = AttestationFile()
        if not self.path.exists():
            return out
        data = self.path.read_bytes()
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        elif lines:
            out.torn_tail = True        # a partial final write: a prepare
            lines.pop()
        for n, line in enumerate(lines, 1):
            try:
                rec = json.loads(line)
            except (ValueError, RecursionError):
                # RecursionError: nesting deep enough to exhaust the parser
                # is a malformed line, not a crash of every reader.
                out.malformed.append(n)
                continue
            if _typed_abort(rec):
                out.aborted.add(rec["abort"])
            elif _typed_attestation(rec):
                out.by_hash.setdefault(rec["event_hash"], []).append(rec)
            else:
                out.malformed.append(n)
        return out


def signed_append(log, attestations: Attestations, signer: Signer, *,
                  registry: "KeyRegistry | None" = None, **fields):
    """Append as ``signer``'s principal and attest the event. The actor IS
    the signer: there is no parameter to name anybody else.

    The key must be one ``registry`` holds for the signer AND one that may
    sign at the record's seq -- checked under the writer lock, before a byte
    is written. That is a different question from the reader's. A key
    revoked at seq 10 still authenticates seq 5 for every reader, for ever;
    it may not sign seq 11, and a writer holding it is refused NOW rather
    than leaving an event every reader will refuse later. ``registry``
    defaults to the one the log's authenticator reads with; a log with
    neither is refused, because nothing could then say whether the key may
    sign."""
    if "actor" in fields:
        raise AuthenticationError(
            "the actor of a signed append is the signer; it is not a "
            "parameter")
    return log.append(actor=signer.principal,
                      before_write=_preparer(attestations, signer,
                                             _write_registry(log, registry)),
                      **fields)


def _write_registry(log, registry):
    """The registry that decides whether a key may sign here: the one given,
    else the one the log authenticates with. Neither is refused, by
    :func:`may_sign`, before anything is written."""
    if registry is None:
        registry = getattr(getattr(log, "authenticator", None), "registry",
                           None)
    return registry


def may_sign(registry: "KeyRegistry", signer: Signer, seq: int) -> None:
    """Refuse unless ``signer``'s key may sign the event at ``seq`` NOW.

    The WRITER's question. The reader's -- was this key good for event N
    when N was written -- is :meth:`RegisteredKey.refusal_at` on N, and its
    answer for an old event does not change when the key is later revoked or
    rotated; this one does, for every event from the revocation on."""
    if not isinstance(registry, KeyRegistry):
        raise AuthenticationError(
            "a signed append needs the registry that says whether this key "
            "may sign the next event: pass registry=, or open the log with "
            "an authenticator")
    key = registry.get(signer.key_id)
    if key is None:
        raise AuthenticationError(
            f"{UNKNOWN_KEY}: {signer.key_id!r} is not in the registry; it "
            f"may not sign seq {seq}")
    if key.principal != signer.principal:
        raise AuthenticationError(
            f"{WRONG_PRINCIPAL}: {signer.key_id!r} is registered for "
            f"{key.principal!r}, not {signer.principal!r}")
    why = key.refusal_at(seq)
    if why is not None:
        raise AuthenticationError(
            f"{why[0]}: {why[1]}; it may not sign seq {seq}, and nothing "
            "was written")


def _preparer(attestations: Attestations, signer: Signer,
              registry: "KeyRegistry"):
    """What the log calls with the exact record, under its writer lock,
    before writing it: check the key may sign THIS seq, sign it, and make
    the attestation durable first."""
    def prepare(ev):
        may_sign(registry, signer, ev.seq)
        attestations.prepare(signer.attest(ev))
    return prepare


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
                 *, require=None, complete: bool = True,
                 provider=signature.REFERENCE) -> AuthenticationReport:
    """Every event whose actor is required to authenticate -- ``require``, a
    set of principals, or every actor when None -- must carry an attestation
    by a key the registry holds FOR THAT ACTOR, whose signature verifies over
    the event's hash."""
    events = list(events)
    file = attestations.read()
    by_hash, malformed = file.by_hash, file.malformed
    out = AuthenticationReport()
    for n in malformed:
        out.findings.append((None, MALFORMED,
                             f"attestation line {n} is not an attestation"))
    committed = {ev.hash for ev in events}
    for h in sorted(file.aborted & committed):
        out.findings.append((None, ABORTED_COMMITTED,
                             f"an abort names committed event {h[:16]}...; "
                             "no writer aborts what it committed"))
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
        why: list[tuple[str, str]] = []
        authenticated = False
        for att in atts:
            key = registry.get(att.get("key_id"))
            if key is None:
                why.append((UNKNOWN_KEY, f"{att.get('key_id')!r} is not a "
                            "registered key"))
            elif key.principal != ev.actor:
                why.append((WRONG_PRINCIPAL, f"signed by a key of "
                            f"{key.principal!r}, and the event names "
                            f"{ev.actor!r}"))
            elif key.refusal_at(ev.seq) is not None:
                why.append(key.refusal_at(ev.seq))
            elif att.get("seq") != ev.seq or not _verifies(
                    key, ev.hash, att, provider):
                why.append((BAD_SIGNATURE, f"{key.key_id!r} did not sign "
                            "this event"))
            else:
                authenticated = True
                break
        if authenticated:
            out.authenticated += 1
        else:
            out.findings.extend((ev.seq, k, d) for k, d in why)
            out.refused.add(ev.seq)
    # An attestation of no event in the history is a finding only when the
    # history given is the WHOLE history; a catch-up passes only the tail.
    # Not a finding either way: an ABORTED prepare, and the one PENDING
    # prepare one past the head -- another writer may be between its two
    # writes this instant, or have crashed there.
    head = events[-1].seq if events else -1
    pending = {h for h, atts in by_hash.items()
               if h not in seen and h not in file.aborted
               and all(a["seq"] == head + 1 for a in atts)}
    if len(pending) > 1:
        pending = set()         # one prepare can be in flight, never two
    for h in sorted(set(by_hash) - seen - file.aborted - pending
                    ) if complete else ():
        if require is None or any(a.get("key_id") and registry.get(
                a["key_id"]) and registry.get(a["key_id"]).principal
                in require for a in by_hash[h]):
            out.findings.append((None, DANGLING,
                                 f"an attestation names event hash "
                                 f"{h[:16]}..., which is in no event of "
                                 "this history"))
    return out


def _verifies(key: RegisteredKey, event_hash: str, att: dict,
              provider) -> bool:
    sig = att.get("sig")
    try:
        raw = bytes.fromhex(sig) if isinstance(sig, str) else None
    except ValueError:
        raw = None
    return raw is not None and provider.verify(key.public,
                                               _message(event_hash), raw)


@dataclass(frozen=True)
class Authenticator:
    """What a reader is given to authenticate a history: the attestation
    file, the registry, and who must authenticate."""

    attestations: Attestations
    registry: KeyRegistry
    require: frozenset | None = None
    provider: object = signature.REFERENCE

    def __call__(self, events, *, complete: bool = True
                 ) -> AuthenticationReport:
        return authenticate(events, self.attestations, self.registry,
                            require=self.require, complete=complete,
                            provider=self.provider)


def begin_history(log, attestations: Attestations, signer: Signer, *,
                  profile: str = PROFILE_AUTHENTICATED_REQUIRED,
                  registry: "KeyRegistry | None" = None):
    """Declare ``log``'s security profile as its FIRST event, attested by
    ``signer``. Refused on a log that already has a history: a profile is
    what a history is written under, not something it switches to. The key
    must be allowed to sign seq 0, as for :func:`signed_append`."""
    registry = _write_registry(log, registry)
    def decide(head_seq):
        if head_seq != -1:
            raise AuthenticationError(
                "a security profile is declared by a history's first event; "
                f"this log already reaches seq {head_seq}")
        return {"actor": signer.principal, "action": ACT_SECURITY_PROFILE,
                "target": "history", "payload": {"profile": profile}}
    return log.append_decided(decide,
                              before_write=_preparer(attestations, signer,
                                                     registry))


#: WHO AUTHENTICATES, reader by reader -- the coverage the actor-
#: authentication directive asks to be kept rather than assumed. Every
#: reader in ``qta_agent`` reads the log through ``read_verified`` or
#: ``read_verified_from`` on the EventLog it is GIVEN, so each is GATED: it
#: authenticates every event when that log carries an authenticator, and
#: reads nothing of an AUTHENTICATED_REQUIRED history when it does not.
#: Two also give their own per-event verdict. A tool that builds its own
#: EventLog from a path has no way yet to be handed an authenticator, so it
#: can only refuse a REQUIRED history. ``tests/test_authenticated_history
#: .py`` exercises every entry.
GATED = "GATED"
GATED_AND_OWN_VERDICT = "GATED_AND_OWN_VERDICT"
REFUSES_REQUIRED_ONLY = "REFUSES_REQUIRED_ONLY"
NOT_BUILT = "NOT_BUILT"
READER_COVERAGE = {
    "qta_agent.store.AuthorityStore": GATED_AND_OWN_VERDICT,
    "qta_agent.reconstruct": GATED_AND_OWN_VERDICT,
    "qta_agent.governed_stage10.GovernedStage10": GATED,
    "qta_agent.governed_model.GovernedOrigins": GATED,
    "qta_agent.scheduler.Scheduler": GATED,
    "qta_agent.memory.MemoryStore": GATED,
    "qta_agent.policy.PolicyStore": GATED,
    "qta_agent.audit.AuditIndex": GATED,
    "qta_agent.agents.AgentDirectory": GATED,
    "qta_agent.capability.CapabilityLedger": GATED,
    "qta_agent.idempotency.IdempotencyLedger": GATED,
    "qta_agent.netauth.NetworkAuthority": GATED,
    "qta_agent.secrets.SecretStore": GATED,
    "qta_agent.checkpoint.CheckpointStore": GATED,
    # The AI proposal ingress's projection of its receipts: read through
    # read_verified, and it refuses a receipt whose digest or target is not
    # its envelope's.
    "qta_agent.proposals.received": GATED_AND_OWN_VERDICT,
    "tools/audit_log.py": REFUSES_REQUIRED_ONLY,
    "tools/generic_consistency.py": REFUSES_REQUIRED_ONLY,
    "tools/independent_verify.py": REFUSES_REQUIRED_ONLY,
    "hypothesis lifecycle": NOT_BUILT,
}
