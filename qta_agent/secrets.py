"""Secrets: references that travel, values that do not.

WHY AN ENVIRONMENT VARIABLE IS NOT A SECRET MECHANISM

``os.environ`` is inherited by every child process, readable by every line of
every dependency, and printed by any diagnostic that dumps the environment. A
secret held that way is not scoped to a task, a tool or a purpose; it is scoped
to the process, which is to say it is not scoped at all. The first thing this
module does is separate the two things that get conflated:

reference
    :class:`SecretRef` names a secret. It is safe to log, store, put in a task
    record, cite in provenance and hand to a component that will never see the
    value. It IS the thing that flows through the system.

value
    Reachable only through :meth:`SecretStore.resolve`, only under a grant that
    names the actor, task, tool and purpose, only inside a bounded block, and
    only via an explicit :meth:`Secret.reveal`. It does not flow anywhere.

ACCIDENTAL DISCLOSURE IS THE COMMON CASE

Deliberate exfiltration is rare; an f-string in an error path is not. So
:class:`Secret` renders as ``<secret:api-token>`` under ``str``, ``repr`` and
``format``, and the only way to obtain the characters is to ask for them by
name. A log line that interpolates a secret prints the placeholder, which is
both harmless and a visible signal that the call site should be holding a
reference instead.

REDACTION IS AT THE SURFACE, NOT AT THE CALL SITE

A rule that every caller must remember to redact is a rule that holds until the
first caller who does not. :class:`Redactor` walks whole structures --
subprocess output, exception text, JSON, audit answers -- and replaces every
known secret value wherever it appears.

WHAT REDACTION CANNOT DO, STATED PLAINLY

It finds the value, and the encodings named in :data:`ENCODINGS`: base64,
URL-encoding, hex, and the JSON-escaped form. It does NOT find a secret that
has been hashed, encrypted, compressed, reversed, split across two fields or
re-encoded some other way. A component that wants to leak a secret past this
can. Redaction is a defence against accident and against careless output, and
this paragraph exists so nobody plans as though it were more.

LIFETIME, AND THE LIMIT PYTHON IMPOSES

Values are held as :class:`bytearray` and zeroed when the block ends, so the
plaintext does not sit in the store between uses. What cannot be promised is
that no copy remains: ``str`` is immutable, the interpreter may have interned
or copied it, and a garbage collector moves things. The zeroing is real and
bounded; the guarantee people usually assume from it is not available in this
runtime, so it is not claimed.

AUTHORITY IS RE-CHECKED, NOT CAPTURED

A :class:`Secret` holds no value of its own and re-checks its grant on every
:meth:`Secret.reveal`. A grant revoked between one call and the next stops
working at the next call, rather than after whichever in-memory object happens
to still be alive.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
from dataclasses import dataclass, replace
from typing import FrozenSet

from .canonical import digest

ACT_SECRET_GRANT = "secret.grant"
ACT_SECRET_ACCESS = "secret.access"

#: Sentinel meaning "does not expire on its own". Revocation still applies.
NEVER_EXPIRES = -1

#: Purpose meaning "any purpose this grant's tool needs". Must be written out;
#: an empty purpose set is refused, for the reason given in the policy module.
ANY_PURPOSE = "*"

#: Encodings :class:`Redactor` recognises. Enumerated rather than open-ended,
#: because a redactor that claims to find "any encoding" is claiming to solve
#: a problem nobody has solved.
ENCODINGS = ("literal", "base64", "base64url", "url", "hex", "json")

#: A secret shorter than this is not redacted by value: two characters occur
#: in ordinary text, and replacing them would corrupt output while protecting
#: nothing. Such a secret is a configuration error, and is refused instead.
MIN_SECRET_LEN = 8

_PLACEHOLDER = "<redacted:{}>"


class SecretError(Exception):
    """Base class. Every failure here is fail-closed."""


class SecretDenied(SecretError):
    """No live grant authorizes this access."""


class UnknownSecret(SecretDenied):
    """No such secret is registered."""


class SecretExpired(SecretDenied):
    """The grant was valid, and is no longer."""


class SecretNotYetIssued(SecretDenied):
    """The grant exists, and did not yet at the position being asked about.

    The other end of :class:`SecretExpired`. See the identical pair in
    :mod:`qta_agent.capability`: a window checked at one end is a half-check,
    and the end that was missing is the one an incident review depends on.
    """


class SecretRevoked(SecretDenied):
    """The grant was withdrawn."""


@dataclass(frozen=True)
class SecretRef:
    """A name for a secret. Safe everywhere the value is not.

    Carries no value and no digest OF the value: a digest would let anyone
    holding it confirm a guess offline, which turns a low-entropy secret into
    a solved one. What identifies the secret is the id its issuer chose.
    """

    secret_id: str

    def __post_init__(self):
        if not isinstance(self.secret_id, str) or not self.secret_id:
            raise SecretError("secret_id must be a non-empty str")

    def __str__(self) -> str:
        return f"<secretref:{self.secret_id}>"

    __repr__ = __str__

    def to_record(self) -> dict:
        return {"secret_ref": self.secret_id}


@dataclass(frozen=True)
class SecretGrant:
    """Permission for one subject to resolve one secret, for named purposes."""

    grant_id: str
    subject: str
    task_id: str
    tool_id: str
    secret_id: str
    purposes: tuple
    issued_seq: int = 0
    expires_after_seq: int = NEVER_EXPIRES

    def body(self) -> dict:
        return {"grant_id": self.grant_id, "subject": self.subject,
                "task_id": self.task_id, "tool_id": self.tool_id,
                "secret_id": self.secret_id, "purposes": list(self.purposes),
                "issued_seq": self.issued_seq,
                "expires_after_seq": self.expires_after_seq}

    def digest(self) -> str:
        return digest(self.body())

    def covers_purpose(self, purpose: str) -> bool:
        return ANY_PURPOSE in self.purposes or purpose in self.purposes


def grant(*, grant_id: str, subject: str, task_id: str, tool_id: str,
          secret_id: str, purposes, issued_seq: int = 0,
          expires_after_seq: int = NEVER_EXPIRES) -> SecretGrant:
    """Construct a grant, validating everything that cannot be fixed later."""
    for name, value in (("grant_id", grant_id), ("subject", subject),
                        ("task_id", task_id), ("tool_id", tool_id),
                        ("secret_id", secret_id)):
        if not isinstance(value, str) or not value:
            raise SecretError(
                f"{name} must be a non-empty str; a grant with no {name} is a "
                "grant with no boundary")
    if isinstance(purposes, str):
        raise SecretError(
            f"purposes must be a sequence of strings, not the bare string "
            f"{purposes!r}")
    ps = tuple(sorted({p for p in purposes}))
    if not ps:
        raise SecretError(
            f"a grant with no purposes authorizes nothing and is refused; "
            f"write {ANY_PURPOSE!r} if any purpose is genuinely intended")
    for p in ps:
        if not isinstance(p, str) or not p:
            raise SecretError(f"purpose {p!r} must be a non-empty str")
    if not isinstance(issued_seq, int) or isinstance(issued_seq, bool) \
            or issued_seq < 0:
        raise SecretError("issued_seq must be a non-negative int")
    if expires_after_seq != NEVER_EXPIRES:
        if (not isinstance(expires_after_seq, int)
                or isinstance(expires_after_seq, bool)):
            raise SecretError("expires_after_seq must be an int")
        if expires_after_seq < issued_seq:
            raise SecretError(
                f"grant would expire at {expires_after_seq}, before it was "
                f"issued at {issued_seq}; refusing to create a grant that was "
                "never valid")
    return SecretGrant(grant_id=grant_id, subject=subject, task_id=task_id,
                       tool_id=tool_id, secret_id=secret_id, purposes=ps,
                       issued_seq=issued_seq,
                       expires_after_seq=expires_after_seq)


class Secret:
    """A handle to a value. Renders as a placeholder; reveals only on demand.

    The handle stores no plaintext. Every :meth:`reveal` goes back to the
    store and re-checks the grant, so a revocation takes effect at the next
    use rather than whenever the last handle happens to be collected.
    """

    __slots__ = ("_store", "_grant_id", "_secret_id", "_actor", "_task_id",
                 "_tool_id", "_purpose")

    def __init__(self, store, grant_id, secret_id, actor, task_id, tool_id,
                 purpose):
        self._store = store
        self._grant_id = grant_id
        self._secret_id = secret_id
        self._actor = actor
        self._task_id = task_id
        self._tool_id = tool_id
        self._purpose = purpose

    @property
    def secret_id(self) -> str:
        return self._secret_id

    @property
    def ref(self) -> SecretRef:
        return SecretRef(self._secret_id)

    def reveal(self) -> str:
        """The characters. Re-authorizes first; raises rather than returning
        an empty string on refusal.

        This checks the SECRET grant and nothing else. When the value is
        about to be sent somewhere, use :meth:`reveal_for`, which checks the
        pairing -- holding a credential and holding egress are two grants,
        and using one on the other is a third thing neither implies.
        """
        return self._store._reveal(
            grant_id=self._grant_id, secret_id=self._secret_id,
            actor=self._actor, task_id=self._task_id, tool_id=self._tool_id,
            purpose=self._purpose)

    def reveal_for(self, decision) -> str:
        """The characters, for one authorized destination. THE COMPOSED
        OPERATION.

        check_egress_composition existed, was well tested, and had no caller
        outside its own tests -- so the confused-deputy defence was a
        function rather than a boundary. This is where it lives now: the
        pairing is checked BEFORE the value is produced, so a refusal
        happens without the plaintext ever existing.

        Prefer this wherever a secret is destined for a network call. The
        value-based check in NetworkAuthority.authorize is the backstop for
        code that does not.
        """
        check_egress_composition(self._store._grants[self._grant_id],
                                 decision)
        return self.reveal()

    # Every stringification path leads to the placeholder. __format__ matters
    # as much as __str__: f"{secret}" goes through format(), and a class that
    # only overrides __str__ leaks through an f-string with a format spec.
    def __str__(self) -> str:
        return f"<secret:{self._secret_id}>"

    __repr__ = __str__

    def __format__(self, spec: str) -> str:
        return str(self)

    def __bytes__(self) -> bytes:
        raise SecretError(
            "refusing to convert a Secret to bytes implicitly; call "
            ".reveal().encode() at the point of use so the disclosure is "
            "visible in the code")


class Redactor:
    """Replaces known secret values wherever they appear in output.

    Holds the values it must find, which is unavoidable: a redactor that
    cannot recognise a secret cannot remove it. What it does not do is hand
    them out -- there is no accessor, and the class renders no value in its
    own repr.
    """

    __slots__ = ("_patterns",)

    def __init__(self, secrets: dict | None = None):
        self._patterns: list = []
        for sid, value in sorted((secrets or {}).items()):
            self.add(sid, value)

    def add(self, secret_id: str, value: str) -> None:
        if not isinstance(value, str) or len(value) < MIN_SECRET_LEN:
            raise SecretError(
                f"secret {secret_id!r} is shorter than {MIN_SECRET_LEN} "
                "characters; such a value occurs in ordinary text, so "
                "redacting it would corrupt output while protecting nothing")
        for form in _encoded_forms(value):
            self._patterns.append((form, secret_id))
        # Longest first, so a secret that contains another is replaced whole
        # rather than leaving a fragment of the outer one behind.
        self._patterns.sort(key=lambda pair: len(pair[0]), reverse=True)

    def __repr__(self) -> str:
        return f"<Redactor: {len(self._patterns)} pattern(s)>"

    def text(self, value: str) -> str:
        if not isinstance(value, str):
            return value
        for form, sid in self._patterns:
            if form and form in value:
                value = value.replace(form, _PLACEHOLDER.format(sid))
        return value

    def __call__(self, obj):
        return self.walk(obj)

    def walk(self, obj):
        """Redact recursively through the containers output actually uses."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, bytes):
            try:
                return self.text(obj.decode("utf-8", "surrogateescape")) \
                    .encode("utf-8", "surrogateescape")
            except (UnicodeDecodeError, UnicodeEncodeError):
                return obj
        if isinstance(obj, dict):
            return {self.walk(k): self.walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            out = [self.walk(v) for v in obj]
            return type(obj)(out) if isinstance(obj, list) else tuple(out)
        if isinstance(obj, BaseException):
            return type(obj)(self.text(str(obj)))
        return obj

    def environment(self, env: dict) -> dict:
        """Redact an environment mapping by VALUE, not by key name.

        Key-name redaction is the version that misses ``FOO=<the token>``,
        which is exactly how a secret reaches a diagnostic dump.
        """
        return {k: self.text(v) if isinstance(v, str) else v
                for k, v in env.items()}

    def contains_secret(self, obj) -> bool:
        """True when anything in ``obj`` still carries a known secret.

        The assertion form. Used by tests and by output surfaces that would
        rather fail than emit something they cannot prove is clean.
        """
        return self.walk(obj) != obj


def _encoded_forms(value: str) -> tuple:
    """The forms of ``value`` this redactor recognises. See ENCODINGS."""
    raw = value.encode("utf-8")
    forms = [value]
    try:
        forms.append(base64.b64encode(raw).decode("ascii"))
        forms.append(base64.b64encode(raw).decode("ascii").rstrip("="))
        forms.append(base64.urlsafe_b64encode(raw).decode("ascii"))
        forms.append(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
        forms.append(binascii.hexlify(raw).decode("ascii"))
    except (binascii.Error, ValueError):      # pragma: no cover - defensive
        pass
    forms.append(urllib.parse.quote(value, safe=""))
    forms.append(urllib.parse.quote_plus(value))
    # json.dumps escapes quotes and backslashes; a secret containing either
    # appears differently inside serialized output than it does in the raw.
    forms.append(json.dumps(value)[1:-1])
    seen: set = set()
    out = []
    for f in forms:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return tuple(out)


class SecretStore:
    """Registered secrets, the grants over them, and every access recorded.

    The store is the only thing that ever holds a plaintext value, and it
    holds it as a ``bytearray`` it can zero. Nothing else in the package
    receives one except inside an explicit :meth:`Secret.reveal`.
    """

    def __init__(self, log=None):
        self.log = log
        self._values: dict = {}
        self._grants: dict = {}
        self._revoked: set = set()
        self._at_seq = 0
        self._accesses: list = []

    # ---- registration --------------------------------------------------
    def register(self, secret_id: str, value: str) -> SecretRef:
        """Register a value. Returns the REFERENCE; the value stays here."""
        if not isinstance(secret_id, str) or not secret_id:
            raise SecretError("secret_id must be a non-empty str")
        if not isinstance(value, str) or len(value) < MIN_SECRET_LEN:
            raise SecretError(
                f"secret {secret_id!r} must be at least {MIN_SECRET_LEN} "
                "characters; a shorter value cannot be redacted from output "
                "without corrupting it")
        if secret_id in self._values:
            raise SecretError(
                f"secret {secret_id!r} is already registered; replacing it "
                "silently would leave holders resolving a different value "
                "than the one they were granted")
        self._values[secret_id] = bytearray(value.encode("utf-8"))
        return SecretRef(secret_id)

    def forget(self, secret_id: str) -> None:
        """Zero and drop a value. The grants remain, and stop resolving."""
        buf = self._values.pop(secret_id, None)
        if buf is not None:
            for i in range(len(buf)):
                buf[i] = 0

    def secret_ids(self) -> tuple:
        return tuple(sorted(self._values))

    # ---- grants --------------------------------------------------------
    def issue(self, g: SecretGrant, *, actor: str) -> SecretGrant:
        if not isinstance(g, SecretGrant):
            raise SecretError(f"expected a SecretGrant, got {g!r}")
        if g.grant_id in self._grants:
            raise SecretError(f"secret grant {g.grant_id!r} already exists")
        if g.secret_id not in self._values:
            raise UnknownSecret(
                f"no secret {g.secret_id!r} is registered; a grant over "
                "something that does not exist would look like authority")
        if self.log is not None:
            # Stamped from the log, like the capability and egress ledgers:
            # where a grant begins is not the caller's to choose.
            g = replace(g, issued_seq=self.log.verify().head_seq + 1)
        self._grants[g.grant_id] = g
        if self.log is not None:
            # The grant BODY, which names ids and purposes and no value.
            ev = self.log.append(
                actor=actor, action=ACT_SECRET_GRANT, target=g.task_id,
                payload={"grant": g.body(), "grant_digest": g.digest()})
            self._at_seq = ev.seq
        return g

    def revoke(self, grant_id: str, *, actor: str, reason: str) -> None:
        if grant_id not in self._grants:
            raise SecretError(f"no secret grant {grant_id!r} to revoke")
        self._revoked.add(grant_id)
        if self.log is not None:
            ev = self.log.append(
                actor=actor, action=ACT_SECRET_GRANT, target=grant_id,
                payload={"grant_id": grant_id, "revoke": True,
                         "reason": reason})
            self._at_seq = ev.seq

    def set_position(self, at_seq: int) -> None:
        if not isinstance(at_seq, int) or isinstance(at_seq, bool):
            raise SecretError(f"at_seq must be an int, got {at_seq!r}")
        self._at_seq = at_seq

    # ---- resolution ----------------------------------------------------
    def resolve(self, ref: SecretRef, *, grant_id: str, actor: str,
                task_id: str, tool_id: str, purpose: str) -> Secret:
        """Return a handle, having checked that this access is permitted.

        Returns a :class:`Secret`, never a string. The caller that genuinely
        needs the characters asks for them, and that call is greppable.
        """
        if not isinstance(ref, SecretRef):
            raise SecretError(
                f"expected a SecretRef, got {type(ref).__name__}; passing a "
                "raw value here would defeat the separation this module is")
        self._authorize(grant_id=grant_id, secret_id=ref.secret_id,
                        actor=actor, task_id=task_id, tool_id=tool_id,
                        purpose=purpose)
        return Secret(self, grant_id, ref.secret_id, actor, task_id, tool_id,
                      purpose)

    def _authorize(self, *, grant_id, secret_id, actor, task_id, tool_id,
                   purpose) -> SecretGrant:
        g = self._grants.get(grant_id)
        if g is None:
            raise SecretDenied(
                f"no secret grant {grant_id!r} was ever issued")
        if grant_id in self._revoked:
            raise SecretRevoked(
                f"secret grant {grant_id!r} was revoked; it authorizes "
                "nothing from the moment the revocation was recorded")
        if self._at_seq < g.issued_seq:
            raise SecretNotYetIssued(
                f"secret grant {grant_id!r} was issued at seq {g.issued_seq} "
                f"and the log is at {self._at_seq}; a grant does not reach "
                "backwards over a secret that was already read")
        if (g.expires_after_seq != NEVER_EXPIRES
                and self._at_seq > g.expires_after_seq):
            raise SecretExpired(
                f"secret grant {grant_id!r} expired after seq "
                f"{g.expires_after_seq}; the log is at {self._at_seq}")
        if g.secret_id != secret_id:
            raise SecretDenied(
                f"secret grant {grant_id!r} covers {g.secret_id!r}, not "
                f"{secret_id!r}")
        if g.subject != actor:
            raise SecretDenied(
                f"secret grant {grant_id!r} was granted to {g.subject!r}, "
                f"not {actor!r}; a grant is not a bearer token")
        if g.task_id != task_id:
            raise SecretDenied(
                f"secret grant {grant_id!r} is confined to task "
                f"{g.task_id!r} and cannot be used for {task_id!r}")
        if g.tool_id != tool_id:
            raise SecretDenied(
                f"secret grant {grant_id!r} permits tool {g.tool_id!r}, not "
                f"{tool_id!r}")
        if not g.covers_purpose(purpose):
            raise SecretDenied(
                f"secret grant {grant_id!r} permits purposes "
                f"{list(g.purposes)}, not {purpose!r}. This is the check that "
                "stops a credential issued for one destination being used "
                "for another.")
        if secret_id not in self._values:
            raise UnknownSecret(
                f"secret {secret_id!r} is no longer held; the grant is live "
                "and the value is gone, which is a refusal rather than an "
                "empty string")
        return g

    def _reveal(self, *, grant_id, secret_id, actor, task_id, tool_id,
                purpose) -> str:
        g = self._authorize(grant_id=grant_id, secret_id=secret_id,
                            actor=actor, task_id=task_id, tool_id=tool_id,
                            purpose=purpose)
        self._accesses.append(
            {"grant_id": grant_id, "secret_id": secret_id, "actor": actor,
             "task_id": task_id, "tool_id": tool_id, "purpose": purpose})
        if self.log is not None:
            # Identity and purpose. No value, and no digest of one.
            ev = self.log.append(
                actor=actor, action=ACT_SECRET_ACCESS, target=task_id,
                payload={"secret_id": secret_id, "grant_id": grant_id,
                         "grant_digest": g.digest(), "tool_id": tool_id,
                         "purpose": purpose})
            self._at_seq = ev.seq
        return bytes(self._values[secret_id]).decode("utf-8")

    def accesses(self) -> tuple:
        """Every resolution performed, for tests and for audit."""
        return tuple(dict(a) for a in self._accesses)

    # ---- output surfaces -----------------------------------------------
    def grants_in_force(self) -> tuple:
        """Every secret grant this store currently holds, revocations aside.

        Exposed so the egress boundary can ask which pairings are authorized
        without reaching into private state.
        """
        return tuple(sorted(
            (g for gid, g in self._grants.items()
             if gid not in self._revoked),
            key=lambda g: g.grant_id))

    def redactor(self) -> Redactor:
        """A redactor over every value this store currently holds."""
        r = Redactor()
        for sid, buf in sorted(self._values.items()):
            r.add(sid, bytes(buf).decode("utf-8"))
        return r

    def assert_clean(self, obj, *, what: str = "output") -> None:
        """Raise if ``obj`` still carries a known secret.

        For surfaces that would rather fail than emit something they cannot
        prove is clean -- an event payload, an evidence blob, a manifest.
        """
        if self.redactor().contains_secret(obj):
            raise SecretError(
                f"refusing to emit {what}: it contains a registered secret "
                "value. Redact it, or carry a SecretRef instead of the value.")


# ---- composition: secret + network --------------------------------------
def egress_purpose(host: str) -> str:
    """The purpose string naming egress to one host.

    A convention rather than a mechanism, and the mechanism is that a grant
    must NAME it. A credential granted for ``egress:api.example.com`` does not
    resolve for ``egress:evil.test``, so possessing a secret grant and a
    network grant separately does not compose into permission to send the one
    to the other.
    """
    if not isinstance(host, str) or not host:
        raise SecretError("egress purpose needs a host")
    return f"egress:{host.strip().lower().rstrip('.')}"


def check_egress_composition(g: SecretGrant, decision) -> None:
    """Refuse a secret whose purpose does not name the authorized host.

    The confused deputy in this system looks like: a component legitimately
    holds a credential for service A and legitimately holds egress to service
    B, and something persuades it to send the first to the second. Neither
    grant is violated on its own, which is why the check has to be on the
    COMBINATION rather than on either half.
    """
    if decision is None or not getattr(decision, "allowed", False):
        raise SecretDenied(
            "refusing to pair a secret with an unauthorized destination")
    host = (decision.request or {}).get("target", {}).get("host")
    if not host:
        raise SecretDenied(
            "the network decision names no host, so the pairing cannot be "
            "checked; refusing rather than assuming they match")
    wanted = egress_purpose(host)
    if not g.covers_purpose(wanted):
        raise SecretDenied(
            f"secret grant {g.grant_id!r} permits purposes "
            f"{list(g.purposes)}; sending it to {host} would need "
            f"{wanted!r}. Holding a credential and holding egress are two "
            "grants; using one on the other is a third thing, and it is not "
            "implied by either.")


#: Key names that look like secrets. Used only to WARN about a structure that
#: carries a value under a suspicious name; the real defence is value-based
#: redaction, because the leak that matters is the one under an innocent name.
_SUSPICIOUS_KEYS: FrozenSet[str] = frozenset({
    "token", "secret", "password", "passwd", "api key", "apikey",
    "authorization", "bearer", "private key", "credential", "passphrase",
    "session id", "cookie", "access key", "client secret",
})

_KEY_SEPARATORS = re.compile(r"[_\-.:/]+")


def looks_like_a_secret_key(key: object) -> bool:
    """Does this key NAME suggest a secret? Advisory, never authoritative.

    Separators are flattened to spaces first, because ``bearer_token`` is one
    word to a regex with ``\\b`` boundaries -- ``_`` is a word character, and
    the pattern that "obviously" matched it did not.

    Matching is then on whole words and adjacent word pairs rather than on
    substrings. Substring matching flags ``tokenizer_name``, and a heuristic
    that cries wolf on ordinary field names is one people switch off.
    """
    words = _KEY_SEPARATORS.sub(" ", str(key)).lower().split()
    if not words:
        return False
    singles = set(words)
    pairs = {f"{a} {b}" for a, b in zip(words, words[1:])}
    return bool((singles | pairs) & _SUSPICIOUS_KEYS)
