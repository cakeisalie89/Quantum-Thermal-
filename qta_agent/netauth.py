"""Network authority: egress as a bounded grant, never as an ambient power.

THE DEFAULT IS NO NETWORK

A process that can open a socket can open any socket. Nothing about "this
component only fetches the schema registry" is enforced by anything; it is a
sentence in a design document that the runtime has never read. So egress here
is a :class:`~qta_agent.capability.Capability`-shaped object: it names a
subject, a task, a tool, an expiry, and exactly which destinations it covers.
Anything not covered is refused, and a component holding a grant for one
destination holds nothing at all for another.

WHAT IS ACTUALLY ENFORCED, AND BY WHAT

Claiming isolation that is not there is worse than claiming none, so the three
layers are named separately and only two of them exist here:

policy layer (enforced by this module)
    :meth:`NetworkAuthority.authorize` refuses any request not covered by a
    live grant. Every caller that routes through it is bounded. A caller that
    does not route through it is not -- this layer binds cooperating code.

process layer (enforced by this module, within its limits)
    :func:`socket_guard` replaces ``socket.socket.connect`` for the duration
    of a block, so a connection opened by a LIBRARY the caller never inspected
    is checked too. This catches the realistic case -- a dependency that
    phones home -- and does not catch a determined in-process attacker, who
    can simply put the original method back. Said plainly because a guard that
    is described as stronger than it is will be relied on as if it were.

kernel layer (NOT provided)
    Network namespaces, seccomp and firewall rules are the only things that
    can stop a process from reaching the network. This module cannot create
    them, does not pretend to, and a deployment that needs real containment
    must add them outside the process.

HOST MATCHING IS BY LABEL, NEVER BY STRING

``example.com`` as a text prefix matches ``example.com.evil.net``; as a text
suffix it matches ``evil-example.com``. Both are the classic bypass and both
are refused here: a host matches a pattern only when their DNS LABELS match,
right-anchored, and ``*.example.com`` covers strict subdomains WITHOUT
covering the apex. Granting the apex and granting its subdomains are separate
decisions, so making one is not silently making the other.

PORTS AND SCHEMES ARE EXPLICIT

A URL with no port means the scheme's default port, which is a decision, so it
is made once, here, and recorded in the decision. A grant covers the schemes
it names and no others: ``https`` does not imply ``http``, because the whole
point of naming ``https`` was the transport.

MUTATION IS A SEPARATE GRANT

A GET is not automatically harmless -- it can exfiltrate through a query
string -- but a POST is categorically different, and a tool declaring itself
read-only must not be taken at its word. :data:`MUTATING_METHODS` must be
granted explicitly; a grant listing only safe methods cannot be widened by the
request that needs it to be.

DNS REBINDING, HONESTLY

Between "resolve the name" and "connect to the address" the answer can change.
This module closes that window only when the grant PINS the addresses it
permits: the process-layer guard then checks the address actually being
connected to. A grant that does not pin addresses cannot close it, so an
unpinned grant refuses at the socket layer unless its author has explicitly
said ``allow_unpinned_addresses``. That flag is a statement that the
deployment accepts the window, not a default that hides it.
"""
from __future__ import annotations

import contextlib
import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import PurePosixPath

# secrets precedes netauth in the layer order, so this direction is
# the permitted one. It is what makes the confused-deputy check a
# boundary rather than a helper nobody calls.
from .secrets import check_egress_composition

from .canonical import digest

#: Event actions. Constants so a typo cannot create an unread action.
ACT_NET_GRANT = "network.grant"
ACT_NET_REQUEST = "network.request"
ACT_NET_RESULT = "network.result"

#: Sentinel meaning "this grant does not expire on its own". Revocation still
#: applies; an unexpiring grant is not an unrevocable one.
NEVER_EXPIRES = -1

#: Default ports, applied once so that "no port" is a recorded decision rather
#: than an assumption each caller makes differently.
DEFAULT_PORTS = {"http": 80, "https": 443}

#: Methods that change something on the other side. Never implied.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Methods that only read. Not "harmless": a query string exfiltrates.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

METHODS = SAFE_METHODS | MUTATING_METHODS

#: Longer than any real hostname; refused rather than parsed.
MAX_HOST_LEN = 253
MAX_URL_LEN = 8192


class NetworkError(Exception):
    """Base class. Every failure here is fail-closed."""


class NetworkDenied(NetworkError):
    """The request is not covered by a live grant.

    Deliberately NOT an :class:`OSError`. Libraries retry on OSError, and a
    refusal that is retried looks like a flaky network instead of a policy
    decision -- which is how a denial ends up in a log nobody reads rather
    than in an exception somebody sees.
    """


class MalformedTarget(NetworkDenied):
    """The destination could not be parsed into something checkable."""


class Direction(str, Enum):
    """Which way the connection goes. Only egress is modelled today."""

    EGRESS = "EGRESS"


class AddressClass(str, Enum):
    """What kind of address a host resolves to. Each is granted separately."""

    PUBLIC = "PUBLIC"
    #: 127.0.0.0/8, ::1, and the names that alias them.
    LOOPBACK = "LOOPBACK"
    #: RFC1918, link-local, unique-local, CGNAT.
    PRIVATE = "PRIVATE"
    #: Multicast, reserved, unspecified.
    SPECIAL = "SPECIAL"


def classify_address(addr: str) -> AddressClass:
    """Classify a literal IP. Raises for anything that is not one.

    Order matters. ``ipaddress`` reports ``0.0.0.0`` as private (it is in
    ``0.0.0.0/8``), which is true and useless: the unspecified address is not
    somewhere a request goes, and calling it private would let a grant that
    permits private ranges cover it. The specific classifications are
    therefore tested first, and the broad ones last.
    """
    ip = ipaddress.ip_address(addr)
    if ip.is_unspecified:
        return AddressClass.SPECIAL
    if ip.is_loopback:
        return AddressClass.LOOPBACK
    if ip.is_multicast:
        return AddressClass.SPECIAL
    if ip.is_link_local or ip.is_private:
        return AddressClass.PRIVATE
    if ip.is_reserved:
        return AddressClass.SPECIAL
    return AddressClass.PUBLIC


def _labels(host: str) -> tuple:
    """DNS labels of ``host``, lowercased, trailing dot removed.

    The trailing dot matters: ``example.com.`` and ``example.com`` are the
    same name to a resolver and different strings to a comparison.
    """
    return tuple(host.rstrip(".").lower().split("."))


def host_matches(pattern: str, host: str) -> bool:
    """Label-wise, right-anchored host matching. Never a string compare.

    ``example.com``   matches only ``example.com``.
    ``*.example.com`` matches ``a.example.com`` and ``a.b.example.com``,
                      and does NOT match ``example.com`` itself.

    Both exclusions are the point: as text, ``example.com`` is a suffix of
    ``evil-example.com`` and a prefix of ``example.com.evil.net``.
    """
    if not isinstance(pattern, str) or not isinstance(host, str):
        return False
    if not pattern or not host:
        return False
    p = _labels(pattern)
    h = _labels(host)
    if p[0] == "*":
        stem = p[1:]
        if not stem:
            return False
        # Strictly longer: a wildcard covers subdomains, not the apex.
        return len(h) > len(stem) and h[-len(stem):] == stem
    return h == p


@dataclass(frozen=True)
class Target:
    """A destination, parsed once into the fields a decision is made on."""

    scheme: str
    host: str
    port: int
    path: str
    method: str
    direction: Direction = Direction.EGRESS
    #: Set when the host is a literal IP rather than a name.
    literal_address: str | None = None

    def to_record(self) -> dict:
        return {"scheme": self.scheme, "host": self.host, "port": self.port,
                "path": self.path, "method": self.method,
                "direction": self.direction.value,
                "literal_address": self.literal_address}

    @property
    def is_mutating(self) -> bool:
        return self.method in MUTATING_METHODS


def parse_target(url: str, *, method: str = "GET") -> Target:
    """Parse a URL into a :class:`Target`, refusing everything ambiguous.

    Refusals here are not pedantry. Each one is a documented way of getting a
    request past a check that reads the string differently from the client
    that will eventually make the request:

      * ``https://example.com@evil.com/`` -- userinfo makes the AUTHORITY look
        like the allowed host to a careless reader while the connection goes
        to ``evil.com``;
      * a missing or empty host, so ``host_matches`` has nothing to match;
      * a non-integer or out-of-range port;
      * a scheme this module has no default port for and that was not given
        one explicitly.
    """
    if not isinstance(url, str) or not url:
        raise MalformedTarget("target URL must be a non-empty str")
    if len(url) > MAX_URL_LEN:
        raise MalformedTarget(
            f"target URL is {len(url)} bytes, above the {MAX_URL_LEN} bound")
    if not isinstance(method, str):
        raise MalformedTarget(f"method must be a str, got {method!r}")
    method = method.upper()
    if method not in METHODS:
        raise MalformedTarget(
            f"method {method!r} is not one of {sorted(METHODS)}; an unknown "
            "method cannot be classified as safe or mutating, so it is "
            "refused rather than assumed safe")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise MalformedTarget(f"unparseable URL: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise MalformedTarget(
            "target URL has no scheme; a scheme-relative destination is "
            "decided by the client, not by the grant")
    if "@" in (parts.netloc or ""):
        raise MalformedTarget(
            "target URL carries userinfo before the host; "
            "'https://allowed.example@evil.test/' reads as the allowed host "
            "and connects to the other one")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise MalformedTarget(f"invalid host or port: {exc}") from exc
    if not host:
        raise MalformedTarget("target URL has no host")
    if len(host) > MAX_HOST_LEN:
        raise MalformedTarget(
            f"host is {len(host)} bytes, above the {MAX_HOST_LEN} bound")
    if port is None:
        port = DEFAULT_PORTS.get(scheme)
        if port is None:
            raise MalformedTarget(
                f"scheme {scheme!r} has no default port here and the URL "
                "gives none; an unstated port is a decision, and it is not "
                "one this layer will make silently")
    if not 1 <= port <= 65535:
        raise MalformedTarget(f"port {port} is outside 1-65535")

    literal = None
    try:
        ipaddress.ip_address(host)
        literal = host
    except ValueError:
        pass

    path = parts.path or "/"
    return Target(scheme=scheme, host=host.lower(), port=int(port), path=path,
                  method=method, literal_address=literal)


def _normalise_hosts(hosts) -> tuple:
    out = set()
    for raw in _seq(hosts, "hosts"):
        if not isinstance(raw, str) or not raw:
            raise NetworkError(
                f"host pattern must be a non-empty str: {raw!r}")
        h = raw.strip().lower().rstrip(".")
        if not h:
            raise NetworkError(f"host pattern {raw!r} is empty after cleanup")
        if h == "*":
            raise NetworkError(
                "'*' as a host pattern grants the entire internet; if that is "
                "genuinely intended it must be written as an explicit list of "
                "what it covers, because a grant over everything is not a "
                "grant")
        labels = h.split(".")
        if any(not part for part in labels):
            raise NetworkError(
                f"host pattern {raw!r} has an empty label; '..' and a leading "
                "dot mean different things to different parsers")
        if "*" in labels[1:]:
            raise NetworkError(
                f"host pattern {raw!r} has a wildcard that is not the "
                "leftmost label; interior wildcards are refused because their "
                "meaning differs between implementations")
        out.add(h)
    if not out:
        raise NetworkError(
            "a grant with no hosts permits nothing and is refused rather than "
            "issued; an empty allowlist is a mistake, not a policy")
    return tuple(sorted(out))


def _normalise_paths(paths) -> tuple:
    """Path prefixes, matched by component. ``()`` means the whole path."""
    out = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith("/"):
            raise NetworkError(
                f"path prefix {raw!r} must be an absolute path beginning '/'")
        p = PurePosixPath(raw)
        if any(part in ("..", ".") for part in p.parts):
            raise NetworkError(
                f"path prefix {raw!r} contains '..' or '.'; refused rather "
                "than normalised, because normalising changes the grant")
        out.add(p.as_posix())
    return tuple(sorted(out))


@dataclass(frozen=True)
class EgressGrant:
    """One bounded permission to reach the network. Digest is its identity."""

    grant_id: str
    subject: str
    task_id: str
    tool_id: str
    schemes: tuple
    hosts: tuple
    ports: tuple
    methods: tuple
    #: Path prefixes; empty means the whole path space of the granted hosts.
    paths: tuple = ()
    #: Address classes this grant permits. PUBLIC only, by default: reaching
    #: loopback or a private range from a governed tool is a different act
    #: with different consequences, so it is a different decision.
    address_classes: tuple = (AddressClass.PUBLIC.value,)
    #: Literal addresses this grant pins. When present, the process-layer
    #: guard checks the address actually connected to against this set, which
    #: is what closes the rebinding window.
    addresses: tuple = ()
    #: Explicit acceptance that, with no pinned addresses, the resolve-then-
    #: connect window is open. Never a default.
    allow_unpinned_addresses: bool = False
    issued_seq: int = 0
    expires_after_seq: int = NEVER_EXPIRES
    direction: Direction = Direction.EGRESS

    def body(self) -> dict:
        return {"grant_id": self.grant_id, "subject": self.subject,
                "task_id": self.task_id, "tool_id": self.tool_id,
                "schemes": list(self.schemes), "hosts": list(self.hosts),
                "ports": list(self.ports), "methods": list(self.methods),
                "paths": list(self.paths),
                "address_classes": list(self.address_classes),
                "addresses": list(self.addresses),
                "allow_unpinned_addresses": self.allow_unpinned_addresses,
                "issued_seq": self.issued_seq,
                "expires_after_seq": self.expires_after_seq,
                "direction": self.direction.value}

    def digest(self) -> str:
        return digest(self.body())

    def covers_host(self, host: str) -> bool:
        return any(host_matches(p, host) for p in self.hosts)

    def covers_path(self, path: str) -> bool:
        """Path prefixes match by COMPONENT.

        ``/v1`` does not cover ``/v10``.
        """
        if not self.paths:
            return True
        try:
            target = PurePosixPath(path or "/")
        except (TypeError, ValueError):
            return False
        # ".." only. ``PurePosixPath`` drops a "." segment from ``parts``,
        # so testing for it would be a condition that can never be true --
        # and it need not be: "/v1/./x" resolves inside "/v1" under every
        # reading, so both readings stay within the grant. ".." is the one
        # that climbs OUT, and PurePosixPath keeps it, which is why the
        # component-prefix test below is not sufficient on its own:
        # "/v1" is genuinely among the parents of "/v1/../admin".
        if ".." in target.parts:
            return False
        for allowed in self.paths:
            a = PurePosixPath(allowed)
            if target == a or a in target.parents:
                return True
        return False


def _seq(value, what: str):
    """A sequence, refusing the bare string that iterates character by
    character."""
    if isinstance(value, (str, bytes)):
        raise NetworkError(
            f"{what} must be a sequence of values, not the bare string "
            f"{value!r}; iterating it would produce one entry per character")
    try:
        return list(value)
    except TypeError as exc:
        raise NetworkError(f"{what} is not iterable: {exc}") from exc


def _lower_str(value, what: str, *, fold: bool = True) -> str:
    """A non-empty string, checked before anything is done to it."""
    if not isinstance(value, str) or not value:
        raise NetworkError(
            f"{what} must be a non-empty str, got "
            f"{type(value).__name__}: {value!r}")
    return value.lower() if fold else value


def grant(*, grant_id: str, subject: str, task_id: str, tool_id: str,
          schemes, hosts, ports, methods, paths=(),
          address_classes=(AddressClass.PUBLIC,), addresses=(),
          allow_unpinned_addresses: bool = False, issued_seq: int = 0,
          expires_after_seq: int = NEVER_EXPIRES) -> EgressGrant:
    """Construct a grant, validating everything that cannot be fixed later."""
    for name, value in (("grant_id", grant_id), ("subject", subject),
                        ("task_id", task_id), ("tool_id", tool_id)):
        if not isinstance(value, str) or not value:
            raise NetworkError(
                f"{name} must be a non-empty str; a grant with no {name} is a "
                "grant with no boundary")
    # Type-checked BEFORE being transformed. Written the other way round
    # first, and a fuzz campaign found it: a record whose "schemes" is a list
    # of lists raised AttributeError from .lower() rather than NetworkError,
    # so the refusal happened by accident and a caller catching NetworkError
    # did not catch it.
    schemes = tuple(sorted({_lower_str(v, "scheme") for v in _seq(schemes,
                                                                 "schemes")}))
    if not schemes:
        raise NetworkError("a grant must name at least one scheme")
    for s in schemes:
        if s not in DEFAULT_PORTS:
            raise NetworkError(
                f"scheme {s!r} is not one this layer can reason about "
                f"({sorted(DEFAULT_PORTS)}); refusing rather than passing an "
                "unknown transport through unchecked")
    # Checked BEFORE set() and sorted(): an unhashable element raises
    # TypeError from inside the builtin, and a caller catching NetworkError
    # does not catch that.
    raw_ports = _seq(ports, "ports")
    for p in raw_ports:
        if isinstance(p, bool) or not isinstance(p, int) \
                or not 1 <= p <= 65535:
            raise NetworkError(f"port {p!r} is not an integer in 1-65535")
    ports = tuple(sorted(set(raw_ports)))
    if not ports:
        raise NetworkError(
            "a grant must name its ports; 'the default port' is a decision "
            "that belongs in the grant, not in whatever client runs later")
    methods = tuple(sorted({_lower_str(v, "method").upper()
                            for v in _seq(methods, "methods")}))
    if not methods:
        raise NetworkError("a grant must name at least one method")
    unknown = [m for m in methods if m not in METHODS]
    if unknown:
        raise NetworkError(
            f"methods {unknown} are not in {sorted(METHODS)}; an unknown "
            "method cannot be classified as safe or mutating")
    raw_classes = _seq(address_classes, "address_classes")
    classes = tuple(sorted({
        c.value if isinstance(c, AddressClass)
        else _lower_str(c, "address class", fold=False)
        for c in raw_classes}))
    if not classes:
        raise NetworkError("a grant must name at least one address class")
    for c in classes:
        if c not in {a.value for a in AddressClass}:
            raise NetworkError(f"unknown address class {c!r}")
    raw_addrs = _seq(addresses, "addresses")
    for a in raw_addrs:
        if not isinstance(a, str):
            raise NetworkError(
                f"pinned address must be a str, got {type(a).__name__}: {a!r}")
        try:
            ipaddress.ip_address(a)
        except ValueError as exc:
            raise NetworkError(
                f"pinned address {a!r} is not an IP address: {exc}") from exc
    addrs = tuple(sorted(set(raw_addrs)))
    if not isinstance(issued_seq, int) or isinstance(issued_seq, bool) \
            or issued_seq < 0:
        raise NetworkError("issued_seq must be a non-negative int")
    if expires_after_seq != NEVER_EXPIRES:
        if (not isinstance(expires_after_seq, int)
                or isinstance(expires_after_seq, bool)):
            raise NetworkError("expires_after_seq must be an int")
        if expires_after_seq < issued_seq:
            raise NetworkError(
                f"grant would expire at {expires_after_seq}, before it was "
                f"issued at {issued_seq}; refusing to create a grant that was "
                "never valid")
    return EgressGrant(
        grant_id=grant_id, subject=subject, task_id=task_id, tool_id=tool_id,
        schemes=schemes, hosts=_normalise_hosts(hosts), ports=ports,
        methods=methods, paths=_normalise_paths(paths),
        address_classes=classes, addresses=addrs,
        allow_unpinned_addresses=bool(allow_unpinned_addresses),
        issued_seq=issued_seq, expires_after_seq=expires_after_seq)


def grant_from_record(rec: dict) -> EgressGrant:
    """Rebuild a grant from a log payload, validating its shape."""
    if not isinstance(rec, dict):
        raise NetworkError(f"grant record is {type(rec).__name__}")
    known = set(EgressGrant.__dataclass_fields__)
    unknown = set(rec) - known
    if unknown:
        raise NetworkError(
            f"grant record carries unknown fields {sorted(unknown)}; refusing "
            "to project a grant this version does not fully understand")
    try:
        return grant(
            grant_id=rec["grant_id"], subject=rec["subject"],
            task_id=rec["task_id"], tool_id=rec["tool_id"],
            schemes=rec["schemes"], hosts=rec["hosts"], ports=rec["ports"],
            methods=rec["methods"], paths=rec.get("paths", ()),
            address_classes=rec.get("address_classes",
                                    (AddressClass.PUBLIC.value,)),
            addresses=rec.get("addresses", ()),
            allow_unpinned_addresses=rec.get("allow_unpinned_addresses",
                                             False),
            issued_seq=rec.get("issued_seq", 0),
            expires_after_seq=rec.get("expires_after_seq", NEVER_EXPIRES))
    except KeyError as exc:
        raise NetworkError(f"grant record missing {exc}") from exc


def _refuse_secret_pairing(decision, body, secrets):
    """None if the body may go to this destination, else a refusing decision.

    Value-based, deliberately: the leak that matters is a credential under an
    innocent name, so this asks what the bytes ARE rather than what they are
    called. A body carrying no registered secret is not this function's
    business, and a store that was not supplied cannot be consulted.
    """
    if body is None or secrets is None:
        return None
    try:
        carries = secrets.redactor().contains_secret(body)
    except Exception:                        # noqa: BLE001 - never a leak
        # A store that cannot answer must not turn into permission. It also
        # must not raise out of a function documented as total.
        return replace(decision, allowed=False,
                       reason=("the secret store could not be consulted "
                               "about this body; refusing rather than "
                               "assuming it carries nothing"))
    if not carries:
        return None
    host = decision.request.get("target", {}).get("host", "")
    for g in sorted(secrets.grants_in_force(),
                    key=lambda x: x.grant_id):
        try:
            check_egress_composition(g, decision)
        except Exception:                    # noqa: BLE001 - try the next
            continue
        return None                          # one grant authorizes the pair
    return replace(decision, allowed=False,
                   reason=(f"the body carries a registered secret and no "
                           f"secret grant names egress to {host!r}; holding "
                           "a credential and holding egress are two grants, "
                           "and using one on the other is a third thing"))


@dataclass(frozen=True)
class NetworkRequest:
    """What is being attempted, described independently of who is asking."""

    actor: str
    task_id: str
    tool_id: str
    target: Target
    #: The address the caller resolved the host to, when it has one. Recorded
    #: and checked; never guessed at.
    resolved_address: str | None = None

    def to_record(self) -> dict:
        return {"actor": self.actor, "task_id": self.task_id,
                "tool_id": self.tool_id, "target": self.target.to_record(),
                "resolved_address": self.resolved_address}


@dataclass(frozen=True)
class NetworkDecision:
    """Why egress was permitted or refused, in a form that outlives it."""

    allowed: bool
    reason: str
    grant_id: str | None = None
    grant_digest: str | None = None
    request: dict = field(default_factory=dict)
    #: Addresses the connection is confined to, when the grant pins any.
    pinned_addresses: tuple = ()

    def to_record(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "grant_id": self.grant_id, "grant_digest": self.grant_digest,
                "request": self.request,
                "pinned_addresses": list(self.pinned_addresses)}

    def raise_if_denied(self) -> "NetworkDecision":
        if not self.allowed:
            raise NetworkDenied(self.reason)
        return self


class NetworkAuthority:
    """The set of egress grants in force, and the decisions made under them.

    Grants and revocations come from the event log when one is attached, so
    "in force" is a statement about a verified, ordered history rather than
    about a mutable in-memory set.
    """

    def __init__(self, log=None):
        self.log = log
        self._grants: dict = {}
        self._revoked: set = set()
        self._at_seq = 0

    # ---- projection ----------------------------------------------------
    def load(self) -> "NetworkAuthority":
        if self.log is None:
            return self
        self.log.verify().raise_if_bad()
        self._grants = {}
        self._revoked = set()
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        if ev.action == ACT_NET_GRANT:
            p = ev.payload
            if p.get("revoke"):
                self._revoked.add(p["grant_id"])
            else:
                g = grant_from_record(p["grant"])
                claimed = p.get("grant_digest")
                if claimed != g.digest():
                    raise NetworkError(
                        f"seq {ev.seq}: egress grant claims digest "
                        f"{str(claimed)[:12]} but hashes to "
                        f"{g.digest()[:12]}")
                existing = self._grants.get(g.grant_id)
                if existing is not None:
                    if existing.digest() != g.digest():
                        # It USED to overwrite. issue() refuses a duplicate
                        # id, so this path is only reachable by a record that
                        # did not go through it -- and it silently widened a
                        # grant already in force.
                        raise NetworkError(
                            f"seq {ev.seq}: egress grant {g.grant_id!r} was "
                            "issued twice with different terms; the second "
                            "record would replace authority already in force")
                    self._at_seq = ev.seq       # a replay of the same grant
                    return True
                if g.issued_seq != ev.seq:
                    # The digest binds the CONTENT of the grant, which
                    # includes the start it names -- so a self-consistent
                    # record can still be backdated. Where a grant begins is
                    # the log's to say. See the same check in capability.py.
                    raise NetworkError(
                        f"seq {ev.seq}: egress grant {g.grant_id!r} claims "
                        f"it was issued at seq {g.issued_seq}; a grant is in "
                        "force from where it appears in the log")
                self._grants[g.grant_id] = g
            self._at_seq = ev.seq
            return True
        if ev.action in (ACT_NET_REQUEST, ACT_NET_RESULT):
            self._at_seq = ev.seq
            return True
        return False

    # ---- writes --------------------------------------------------------
    def issue(self, g: EgressGrant, *, actor: str) -> EgressGrant:
        if not isinstance(g, EgressGrant):
            raise NetworkError(f"expected an EgressGrant, got {g!r}")
        if g.grant_id in self._grants:
            raise NetworkError(
                f"egress grant {g.grant_id!r} already exists; reusing an id "
                "would make two different grants indistinguishable in the log")
        if self.log is not None:
            # Stamped, not accepted: a caller that could choose the start
            # could grant egress over traffic that already happened.
            g = replace(g, issued_seq=self.log.verify().head_seq + 1)
            ev = self.log.append(
                actor=actor, action=ACT_NET_GRANT, target=g.task_id,
                payload={"grant": g.body(), "grant_digest": g.digest(),
                         "grant_id": g.grant_id})
            self.apply(ev)
        else:
            self._grants[g.grant_id] = g
        return g

    def revoke(self, grant_id: str, *, actor: str, reason: str) -> None:
        if grant_id not in self._grants:
            raise NetworkError(f"no egress grant {grant_id!r} to revoke")
        if self.log is not None:
            ev = self.log.append(
                actor=actor, action=ACT_NET_GRANT, target=grant_id,
                payload={"grant_id": grant_id, "revoke": True,
                         "reason": reason})
            self.apply(ev)
        else:
            self._revoked.add(grant_id)

    def set_position(self, at_seq: int) -> None:
        """Set the log position expiry is measured against."""
        if not isinstance(at_seq, int) or isinstance(at_seq, bool):
            raise NetworkError(f"at_seq must be an int, got {at_seq!r}")
        self._at_seq = at_seq

    # ---- the decision --------------------------------------------------
    def authorize(self, req: NetworkRequest, *,
                  grant_id: str | None = None,
                  body=None, secrets=None) -> NetworkDecision:
        """Decide one egress request. Never raises on denial; total.

        With no ``grant_id`` the request is checked against every live grant
        and the first that covers it decides. That is a convenience for
        callers holding several, not a widening: a request covered by none is
        still denied, and each grant is checked in full.

        THE CONFUSED DEPUTY, CHECKED BY VALUE
        -------------------------------------
        Pass ``body`` and a ``secrets`` store and the decision also asks
        whether the bytes about to leave carry a credential this destination
        may not receive. Neither grant is violated on its own -- that is what
        makes the pairing the thing worth checking, and what makes checking
        either half insufficient.

        This is the BACKSTOP for the composed operation
        (:meth:`Secret.reveal_for`), and it is the stronger of the two
        because it does not depend on the caller having chosen the safe
        method: it looks at what is actually in the body. It is still
        mediation -- a component that builds its own socket never reaches
        this function at all.
        """
        candidates = ([grant_id] if grant_id is not None
                      else sorted(self._grants))
        rec = req.to_record()
        if not candidates:
            return NetworkDecision(
                False, "no egress grant exists; the default is no network",
                request=rec)
        last = "no egress grant covers this request"
        for gid in candidates:
            ok, why = self._covers(gid, req)
            if ok:
                g = self._grants[gid]
                decision = NetworkDecision(
                    True, why, grant_id=gid, grant_digest=g.digest(),
                    request=rec, pinned_addresses=g.addresses)
                refusal = _refuse_secret_pairing(decision, body, secrets)
                return refusal if refusal is not None else decision
            last = why
        return NetworkDecision(False, last, request=rec)

    def _covers(self, gid: str, req: NetworkRequest) -> tuple:
        """Check one grant against one request. Order is deliberate."""
        g = self._grants.get(gid)
        t = req.target
        if g is None:
            return False, (f"no egress grant {gid!r} was ever issued; a grant "
                           "that does not appear in the log does not exist")
        if gid in self._revoked:
            return False, f"egress grant {gid!r} was revoked"
        if self._at_seq < g.issued_seq:
            # The other end of the window. Without it a grant recorded later
            # answers "was this egress permitted at seq N" for an N before it
            # existed.
            return False, (f"egress grant {gid!r} was issued at seq "
                           f"{g.issued_seq} and the log is at {self._at_seq}; "
                           "a grant does not reach backwards over traffic "
                           "that already happened")
        if (g.expires_after_seq != NEVER_EXPIRES
                and self._at_seq > g.expires_after_seq):
            return False, (f"egress grant {gid!r} expired after seq "
                           f"{g.expires_after_seq}; the log is at "
                           f"{self._at_seq}")
        if g.subject != req.actor:
            return False, (f"egress grant {gid!r} was granted to "
                           f"{g.subject!r}, not {req.actor!r}")
        if g.task_id != req.task_id:
            return False, (f"egress grant {gid!r} is confined to task "
                           f"{g.task_id!r} and cannot be used for "
                           f"{req.task_id!r}")
        if g.tool_id != req.tool_id:
            return False, (f"egress grant {gid!r} permits tool "
                           f"{g.tool_id!r}, not {req.tool_id!r}")
        if g.direction is not t.direction:
            return False, (f"egress grant {gid!r} permits "
                           f"{g.direction.value}, not {t.direction.value}")
        if t.scheme not in g.schemes:
            return False, (f"egress grant {gid!r} permits schemes "
                           f"{list(g.schemes)}, not {t.scheme!r}")
        if not g.covers_host(t.host):
            return False, (f"egress grant {gid!r} does not cover host "
                           f"{t.host!r}; its hosts are {list(g.hosts)}")
        if t.port not in g.ports:
            return False, (f"egress grant {gid!r} permits ports "
                           f"{list(g.ports)}, not {t.port}")
        if t.method not in g.methods:
            extra = (" (a mutating method is never implied by a safe one)"
                     if t.is_mutating else "")
            return False, (f"egress grant {gid!r} permits methods "
                           f"{list(g.methods)}, not {t.method!r}{extra}")
        if not g.covers_path(t.path):
            return False, (f"egress grant {gid!r} does not cover path "
                           f"{t.path!r}; its prefixes are {list(g.paths)}")
        addr = req.resolved_address or t.literal_address
        if addr is not None:
            try:
                cls = classify_address(addr)
            except ValueError:
                return False, f"resolved address {addr!r} is not an IP address"
            if cls.value not in g.address_classes:
                return False, (
                    f"egress grant {gid!r} permits address classes "
                    f"{list(g.address_classes)}; {addr} is {cls.value}")
            if g.addresses and addr not in g.addresses:
                return False, (
                    f"egress grant {gid!r} pins {list(g.addresses)}; the host "
                    f"resolved to {addr}, which is the signature of a name "
                    "whose answer changed between the check and the connect")
        return True, (f"egress grant {gid!r} covers {t.method} "
                      f"{t.scheme}://{t.host}:{t.port}{t.path}")

    def record(self, req: NetworkRequest, decision: NetworkDecision, *,
               actor: str) -> NetworkDecision:
        """Append the attempt and its verdict. Denials too.

        Records identity and destination, never payloads or headers: an audit
        trail that carries request bodies is a second copy of every secret the
        request contained.
        """
        if self.log is None:
            return decision
        self.log.append(actor=actor, action=ACT_NET_REQUEST,
                        target=req.task_id,
                        payload={"request": req.to_record(),
                                 "decision": decision.to_record()})
        return decision

    def record_result(self, req: NetworkRequest, *, actor: str,
                      outcome: str, status: int | None = None,
                      response_digest: str | None = None) -> None:
        """Record what happened, by classification rather than by content."""
        self.log.append(
            actor=actor, action=ACT_NET_RESULT, target=req.task_id,
            payload={"target": req.target.to_record(), "outcome": outcome,
                     "status": status, "response_digest": response_digest})


# ---- the process layer --------------------------------------------------
class GuardedConnection(NetworkDenied):
    """A socket connect was refused by the process-layer guard."""


@contextlib.contextmanager
def socket_guard(authority: NetworkAuthority, *, actor: str, task_id: str,
                 tool_id: str, allowed: "NetworkDecision | None" = None):
    """Refuse socket connections that no live grant permits, for a block.

    This is the layer that catches a DEPENDENCY reaching the network -- the
    realistic case, and the one a policy check at the call site cannot see. It
    is not containment: code running inside the block can restore the original
    method, and this docstring says so rather than leaving a reader to assume
    otherwise.

    ``allowed`` is the decision that authorized the intended request. When it
    pins addresses, only those are permitted; when it does not, the connection
    is refused unless the grant behind it explicitly accepted the unpinned
    window. That is the resolve-then-connect gap, closed where it can be and
    declared where it cannot.
    """
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def _check(address) -> None:
        host, port = _address_parts(address)
        if allowed is not None and allowed.allowed:
            if allowed.pinned_addresses:
                if host not in allowed.pinned_addresses:
                    raise GuardedConnection(
                        f"connect to {host}:{port} is outside the addresses "
                        f"this request pinned "
                        f"({list(allowed.pinned_addresses)}); a name that "
                        "resolves differently at connect time than at check "
                        "time is the rebinding case, not a retry")
                return
            g = authority._grants.get(allowed.grant_id)
            if g is not None and not g.allow_unpinned_addresses:
                raise GuardedConnection(
                    f"connect to {host}:{port} cannot be checked: the grant "
                    "pins no addresses and has not accepted the "
                    "resolve-then-connect window. Pin addresses, or set "
                    "allow_unpinned_addresses on the grant to say the "
                    "deployment accepts it.")
            return
        req = NetworkRequest(
            actor=actor, task_id=task_id, tool_id=tool_id,
            target=Target(scheme="https", host=host, port=port, path="/",
                          method="GET",
                          literal_address=_maybe_ip(host)),
            resolved_address=_maybe_ip(host))
        decision = authority.authorize(req)
        if not decision.allowed:
            raise GuardedConnection(
                f"connect to {host}:{port} was not authorized: "
                f"{decision.reason}")

    def guarded_connect(self, address):
        _check(address)
        return original(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return original_ex(self, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = original
        socket.socket.connect_ex = original_ex


def _address_parts(address) -> tuple:
    """(host, port) from whatever ``connect`` was handed. Fail closed."""
    if isinstance(address, (bytes, str)):
        # A UNIX-domain path. Not egress, but not something this guard can
        # reason about either, so it is refused rather than waved through.
        raise GuardedConnection(
            f"connect to a non-inet address ({address!r}) is refused; this "
            "guard reasons about host and port, and cannot classify anything "
            "else")
    try:
        host, port = address[0], address[1]
    except (TypeError, IndexError, KeyError) as exc:
        raise GuardedConnection(
            f"connect address {address!r} is not (host, port): {exc}") from exc
    return str(host), int(port)


def _maybe_ip(host: str) -> str | None:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return None
