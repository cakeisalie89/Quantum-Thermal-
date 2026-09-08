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

from . import safeio
from .canonical import digest

ACT_SECRET_GRANT = "secret.grant"
ACT_SECRET_ACCESS = "secret.access"
ACT_SECRET_PROVISION = "secret.provision"

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


# ---- external providers --------------------------------------------------
#
# WHY A PROVIDER AT ALL
#
# ``register`` takes a ``str`` that some caller already has. That is fine for
# a test and for a deployment that injects values itself, and it is the whole
# of the mechanism only if nothing outside the process ever holds a secret.
# Real deployments keep credentials somewhere else: a mounted file, an agent
# socket, a cloud secret manager. A provider is the seam where "somewhere
# else" is read, and the point of naming it is that the seam is ONE place
# with one set of refusals rather than a call to ``open()`` in whichever
# module needed a token first.
#
# WHAT A PROVIDER MAY AND MAY NOT DO
#
# It returns a ``bytearray`` and never a ``str``. The store zeroes what it
# holds; a value that has been through ``str`` cannot be zeroed at all (see
# the module docstring). The load path therefore never constructs one -- the
# bytes go from the read straight into the buffer the store will wipe. This
# does not make the value unrecoverable: :mod:`qta_agent.safeio` hands back
# an immutable ``bytes`` and that copy is not zeroable either. The claim is
# narrow and is the same one the module makes everywhere: fewer copies, for
# a bounded time, and no promise that the runtime kept none.
#
# It also fetches ONE NAMED SECRET. A provider that returned "everything in
# the file" would register whatever a hostile or careless source happened to
# contain, under ids the caller never asked for, and every later authority
# check would be over a set the caller did not choose.

#: A secret id a provider will look up. One path component, no separators, no
#: leading dot: the id is used as a FILENAME by the file provider, and an id
#: like ``../../etc/shadow`` must be refused by the naming rule rather than
#: relying on the path layer to catch it afterwards.
_PROVIDER_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: An upper bound on one secret. Larger than any credential and small enough
#: that a mis-pointed provider reads a bounded amount before refusing.
MAX_SECRET_BYTES = 64 * 1024


class ProviderError(SecretError):
    """A provider could not supply a value. Always fail-closed."""


class ProviderUnavailable(ProviderError):
    """The source itself could not be reached or opened.

    Distinct from :class:`ProviderRefused` on purpose. "The secret store is
    not mounted" and "the secret store says there is no such secret" are
    different operational facts, and collapsing them sends an operator to
    look in the wrong place.
    """


class ProviderRefused(ProviderError):
    """The source was reachable and did not yield a usable value."""


def _check_secret_bytes(secret_id: str, buf: bytearray) -> bytearray:
    """Every rule a value must satisfy, wherever it came from.

    Shared by ``register`` and by the provider load path so the two cannot
    drift: a value good enough to register by hand and a value good enough to
    load from a file are the same value.
    """
    if len(buf) < MIN_SECRET_LEN:
        raise ProviderRefused(
            f"secret {secret_id!r} is {len(buf)} byte(s); at least "
            f"{MIN_SECRET_LEN} are required, because a shorter value cannot "
            "be redacted from output without corrupting it")
    if 0 in buf:
        raise ProviderRefused(
            f"secret {secret_id!r} contains a NUL byte; it could not survive "
            "being passed to a process or a socket, and a value that is "
            "silently truncated later is worse than one refused now")
    try:
        bytes(buf).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderRefused(
            f"secret {secret_id!r} is not valid UTF-8 ({exc}); reveal() "
            "decodes as UTF-8, so this would fail at the moment of use "
            "rather than at the moment of loading") from None
    return buf


class SecretProvider:
    """The contract. Subclasses supply :meth:`fetch` and :meth:`describe`.

    Deliberately not a ``Protocol``: the store records ``describe()`` in the
    log as provenance, so a provider is a thing with an identity and not just
    a callable that happens to have the right shape.
    """

    #: Short, stable name for this kind of source. Recorded, not checked.
    kind = "abstract"

    def describe(self) -> dict:
        """Where values come from. Must contain no secret value."""
        raise NotImplementedError

    def fetch(self, secret_id: str) -> bytearray:
        """Return the value for ONE id, as a zeroable buffer, or raise."""
        raise NotImplementedError


class FileSecretProvider(SecretProvider):
    """Secrets as files beneath one directory: ``<root>/<secret_id>``.

    The shape a container orchestrator already produces -- Kubernetes
    projected volumes, systemd credentials, a mounted tmpfs -- so integrating
    with one is a path, not an SDK.

    Reads go through :class:`qta_agent.safeio.ReadRoot`, which is the reason
    this class is short. Confinement to the root, refusal of symlinks, the
    aliased-file check and the size bound are that module's, already tested
    and already mutated against; re-implementing them here would be a second
    copy of the hard part.

    A trailing newline is stripped, once. Every editor and every ``echo``
    appends one, and a token that fails authentication because of an
    invisible byte is a bad afternoon; a value that genuinely ends in a
    newline is not expressible in this format, which is said here rather
    than discovered.
    """

    kind = "file"

    def __init__(self, root, *, max_bytes: int = MAX_SECRET_BYTES):
        self.root = root
        self.max_bytes = max_bytes
        self._open: safeio.ReadRoot | None = None

    def describe(self) -> dict:
        return {"kind": self.kind, "root": str(self.root)}

    def __enter__(self) -> "FileSecretProvider":
        self._open = safeio.ReadRoot(self.root,
                                     max_bytes=self.max_bytes).open()
        return self

    def __exit__(self, *exc) -> None:
        if self._open is not None:
            self._open.close()
            self._open = None

    def fetch(self, secret_id: str) -> bytearray:
        if not isinstance(secret_id, str) or not _PROVIDER_ID.match(secret_id):
            raise ProviderRefused(
                f"{secret_id!r} is not a usable secret id for a file "
                "provider: one component of [A-Za-z0-9._-], not starting "
                "with a dot, at most 64 characters. The id becomes a "
                "filename, and a name that can leave the directory is "
                "refused before the filesystem is asked anything")
        held = self._open is not None
        root = self._open if held else safeio.ReadRoot(
            self.root, max_bytes=self.max_bytes)
        try:
            if not held:
                root.open()
            try:
                res = root.read(secret_id)
            except FileNotFoundError as exc:
                raise ProviderRefused(
                    f"the file provider at {self.root} holds no secret "
                    f"{secret_id!r}") from exc
            except safeio.SafeIOError as exc:
                raise ProviderRefused(
                    f"the file provider refused {secret_id!r}: "
                    f"{exc.__class__.__name__}: {exc}") from exc
        except safeio.PathRefused as exc:
            # Raised by open(): the ROOT is wrong, which is a different
            # problem from a missing secret and is reported as one.
            raise ProviderUnavailable(
                f"the file provider root {self.root} is not usable: "
                f"{exc.__class__.__name__}: {exc}") from exc
        finally:
            if not held:
                root.close()
        buf = bytearray(res.data)
        if buf.endswith(b"\r\n"):
            del buf[-2:]
        elif buf.endswith(b"\n"):
            del buf[-1:]
        return _check_secret_bytes(secret_id, buf)


class MappingSecretProvider(SecretProvider):
    """An in-memory provider, for deployments that inject values themselves.

    Not a test double: a process that already received its credentials --
    from an init container, an operator, a parent that read them once -- has
    a legitimate need to hand them to the store through the same seam, with
    the same refusals and the same recorded provenance, rather than through
    a second path that skips both.
    """

    kind = "mapping"

    def __init__(self, values: dict, *, source: str = "in-process"):
        self._values = {k: bytearray(v.encode("utf-8")
                                     if isinstance(v, str) else v)
                        for k, v in values.items()}
        self.source = source

    def describe(self) -> dict:
        return {"kind": self.kind, "source": self.source}

    def fetch(self, secret_id: str) -> bytearray:
        buf = self._values.get(secret_id)
        if buf is None:
            raise ProviderRefused(
                f"the mapping provider {self.source!r} holds no secret "
                f"{secret_id!r}")
        return _check_secret_bytes(secret_id, bytearray(buf))


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
        if not isinstance(value, str):
            raise SecretError(
                f"secret {secret_id!r} must be a str, got "
                f"{type(value).__name__}")
        self._check_not_registered(secret_id)
        self._values[secret_id] = _check_secret_bytes(
            secret_id, bytearray(value.encode("utf-8")))
        return SecretRef(secret_id)

    def _check_not_registered(self, secret_id: str) -> None:
        if secret_id in self._values:
            raise SecretError(
                f"secret {secret_id!r} is already registered; replacing it "
                "silently would leave holders resolving a different value "
                "than the one they were granted")

    def provision(self, provider: SecretProvider, secret_id: str, *,
                  actor: str = "deployment") -> SecretRef:
        """Load ONE secret from an external provider and register it.

        Returns the reference. The value goes provider -> store and is never
        returned, never logged and never digested; what is recorded is the
        id, the provider's ``describe()`` and who asked.

        No digest, for the reason the module records everywhere else: a
        digest of a credential is an offline guessing oracle, and this
        repository would be the one publishing it.
        """
        if not isinstance(provider, SecretProvider):
            raise ProviderError(
                f"expected a SecretProvider, got {type(provider).__name__}; "
                "the provenance recorded for a provisioned secret is the "
                "provider's own description, so it has to be one")
        if not isinstance(secret_id, str) or not secret_id:
            raise SecretError("secret_id must be a non-empty str")
        # BEFORE the fetch. Reading a value that cannot be stored would put
        # a credential in this process for nothing.
        self._check_not_registered(secret_id)
        buf = provider.fetch(secret_id)
        if not isinstance(buf, bytearray):
            raise ProviderError(
                f"provider {provider.kind!r} returned "
                f"{type(buf).__name__} for {secret_id!r}; a provider returns "
                "a bytearray, because a str cannot be zeroed")
        _check_secret_bytes(secret_id, buf)
        self._values[secret_id] = buf
        if self.log is not None:
            desc = provider.describe()
            try:
                # The description is about to be written to the log, and the
                # redactor can only answer this question with the value in
                # hand -- so the value is registered first and dropped again
                # if the answer is bad. A provider whose "source" field is
                # the credential is not hypothetical: a URL with the token
                # in it is the ordinary way people configure these.
                self.assert_clean(desc, what="provider description")
            except SecretError:
                self.forget(secret_id)
                raise
            ev = self.log.append(
                actor=actor, action=ACT_SECRET_PROVISION, target=secret_id,
                payload={"secret_id": secret_id, "provider": desc,
                         "bytes": len(buf)})
            self._at_seq = ev.seq
        return SecretRef(secret_id)

    def provision_all(self, provider: SecretProvider, secret_ids, *,
                      actor: str = "deployment") -> tuple:
        """Provision every id in ``secret_ids``, or none of them.

        Two rules that look fussy and are not:

        EMPTY IS A REFUSAL. A provisioning step that provisioned nothing and
        reported success is this repository's oldest defect, written down in
        the ledger twice. A caller with nothing to load should not be calling
        this.

        ALL OR NOTHING. Half a credential set is the state where a run starts,
        does some of its work, and fails at the one call that needed the
        secret that was missing. Anything already registered by this call is
        forgotten before the failure is raised.
        """
        ids = tuple(secret_ids)
        if not ids:
            raise ProviderError(
                "provision_all was given no secret ids. Provisioning nothing "
                "and returning success is a vacuous result, not an empty "
                "success")
        if len(set(ids)) != len(ids):
            raise ProviderError(
                f"duplicate secret ids in {list(ids)}; the second load would "
                "fail as already-registered, which reads as a provider fault "
                "rather than a caller mistake")
        done = []
        try:
            for sid in ids:
                done.append(self.provision(provider, sid, actor=actor))
        except Exception:
            for ref in done:
                self.forget(ref.secret_id)
            raise
        return tuple(done)

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
