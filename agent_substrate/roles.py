"""Who may hold a role: grants rooted outside the log (AUD-011, AUD-017).

WHAT THIS CLOSES

A role was whatever the request said it was. :func:`authority.check` held a
transition's role to the roles its edge permits; nothing held the actor to
the role. Anyone able to append could verify and promote a claim, and an
identity registered nowhere could take every role except proposer
(AUD-011). The agent directory did not close the gap: it admits an AGENT
holding any roles, from any registrar. Issuing a secret grant or an egress
grant had no issuer authority at all (AUD-017).

THE ROOT: A ROLE REGISTRY. It is supplied from outside the log and pinned by
its digest, as the key registry is (:mod:`agent_substrate.principals`), for
the same reason: a grant recorded in the log it governs is chosen by whoever
writes that log. Each grant names a principal, a role, a scope and the
positions it holds for.

A reader given a registry asks it before every act a role governs: live,
before the record is written, and again on every replay. The independent
reader (:mod:`agent_substrate.reconstruct`) restates these rules rather than
importing them.

A SIGNATURE IS NOT AN AUTHORIZATION. Under ``AUTHENTICATED_REQUIRED``, the
authenticator binds an event's actor to a key. This module binds the actor
to what it may do. An authenticated principal that holds no grant is refused
exactly as an unauthenticated name is.

Without authentication, a name is only a name. A writer who can append under
any name can append under a holder's, and the registry then constrains only
what passes through the gates. A deployment that relies on roles needs both
the registry and authentication.

POSITIONS, NOT TIMES. As for keys: an event's seq is part of what its record
hashes and what every replay agrees on, so "did this grant hold for this
act" has one answer on every machine. A grant holds from ``valid_from_seq``
through ``valid_until_seq``. A revocation refuses the grant FROM
``revoked_at_seq`` on, and leaves earlier acts standing. A registry that
names a position the history has already passed is a declared reach-back,
as a key's declared compromise is: every reader given it refuses what
follows that position, and says so.

SCOPE. Each grant ranges over values in a namespace:

- ``kind:<record kind>`` for proposing, verifying and promoting a record;
- ``tool:<tool id>`` for a governed task's parties;
- ``policy:<policy id>``, ``secret:<secret id>`` and ``egress:<host>`` for
  the issuing roles;
- ``approval:<what>`` and ``release:<channel>``.

A scope entry is an exact value, ``<namespace>:*`` or ``*``.

DELEGATION: EXPLICIT, AND ONLY ONCE. A registry grant marked
``may_delegate`` lets its holder pass a narrower copy to another principal,
in the log (``role.delegate``). The copy has the same role, a scope inside
the parent's, and an end no later than the parent's. It is never made to
the delegator itself, and it is never passed on: a delegation does not
delegate.

Only PROPOSER, EXECUTOR, VERIFIER and PROMOTER may be delegated. The issuing
roles, policy ownership, human approval and release authority are the
root's to give. A delegation holds only while its parent holds, and its
delegator revokes it in the log (``role.revoke``) from that position on.

WHAT IS PENDING, AND EXTERNAL. No production registry exists here, and none
is invented. :func:`production_role_registry` refuses until a deployment
provisions a registry and pins its digest. Who holds each role in production
is an owner decision.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from .events import DecidedWriter, projection_load
from .principals import registry_digest

SCHEMA = "harness/role-registry/v1"

PROPOSER = "PROPOSER"
EXECUTOR = "EXECUTOR"
VERIFIER = "VERIFIER"
PROMOTER = "PROMOTER"
POLICY_OWNER = "POLICY_OWNER"
SECRET_ISSUER = "SECRET_ISSUER"
EGRESS_ISSUER = "EGRESS_ISSUER"
HUMAN_APPROVER = "HUMAN_APPROVER"
RELEASE_AUTHORITY = "RELEASE_AUTHORITY"

ROLES: tuple = (PROPOSER, EXECUTOR, VERIFIER, PROMOTER, POLICY_OWNER,
                SECRET_ISSUER, EGRESS_ISSUER, HUMAN_APPROVER,
                RELEASE_AUTHORITY)
#: The roles a registry grant may let its holder delegate, once.
DELEGABLE = frozenset({PROPOSER, EXECUTOR, VERIFIER, PROMOTER})
NAMESPACES: tuple = ("kind", "tool", "policy", "secret", "egress",
                     "approval", "release")
ANY = "*"

#: The registry roles that may stand behind each role a transition of the
#: authority state machine claims (:class:`agent_substrate.authority.Role`).
#: SYSTEM is granted to nobody. Under a registry, an automatic transition is
#: made by a principal holding VERIFIER or PROMOTER over the record's kind.
AUTHORITY_ROLES = {"PROPOSER": (PROPOSER,), "VERIFIER": (VERIFIER,),
                   "PROMOTER": (PROMOTER,), "SYSTEM": (VERIFIER, PROMOTER)}

ACT_DELEGATE = "role.delegate"
ACT_REVOKE = "role.revoke"
ACTIONS = frozenset({ACT_DELEGATE, ACT_REVOKE})

#: Why a principal may not act in a role. One answer per case, and the same
#: answer from every reader.
NOT_GRANTED = "NOT_GRANTED"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
NOT_YET_VALID = "NOT_YET_VALID"
EXPIRED = "EXPIRED"
REVOKED = "REVOKED"
REFUSALS: tuple = (NOT_GRANTED, OUT_OF_SCOPE, NOT_YET_VALID, EXPIRED,
                   REVOKED)

#: Where a deployment's registry lives, and the digest it pinned. Neither is
#: set here.
PRODUCTION_ROLES = None
PRODUCTION_ROLE_REGISTRY_DIGEST = None

#: Bounds on a registry document: it is read from a file, and a file is
#: attacker-shaped input until it has been checked.
MAX_GRANTS = 4096
_MAX_NAME = 256
_MAX_SCOPE = 256
_GRANT_FIELDS = frozenset({
    "grant_id", "principal", "role", "scope", "valid_from_seq",
    "valid_until_seq", "revoked_at_seq", "revocation_reason",
    "may_delegate"})
_HEADER = frozenset({"schema", "registry_version", "key_registry", "grants"})
_DELEGATE_FIELDS = frozenset({"delegation_id", "parent", "role",
                              "principal", "scope", "valid_until_seq"})
_REVOKE_FIELDS = frozenset({"delegation_id", "reason"})


class RoleError(Exception):
    """A registry, or a role record, that cannot be believed."""


class RoleNotConfigured(RoleError):
    """No production registry is provisioned."""


class RoleRefused(RoleError):
    """An act whose actor holds no grant admitting it."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _is_name(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_NAME


def _is_seq(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0


def _pattern(value) -> str:
    """One scope entry, typed: ``*``, ``<namespace>:*`` or
    ``<namespace>:<value>``."""
    if value == ANY:
        return value
    ns, sep, rest = value.partition(":") if _is_name(value) else ("", "", "")
    if not sep or ns not in NAMESPACES or not rest:
        raise RoleError(
            f"scope entry {value!r} is not '*', '<namespace>:*' or "
            f"'<namespace>:<value>' over {list(NAMESPACES)}")
    return value


def scope_of(entries) -> tuple:
    """A scope: a non-empty list of entries, as a sorted tuple."""
    if (not isinstance(entries, (list, tuple)) or not entries
            or len(entries) > _MAX_SCOPE):
        raise RoleError(
            f"a scope is a non-empty list of at most {_MAX_SCOPE} entries")
    return tuple(sorted({_pattern(e) for e in entries}))


def covers(scope, value: str) -> bool:
    """True when ``scope`` admits ``value``, itself
    ``<namespace>:<value>``."""
    ns = value.partition(":")[0]
    return any(p == ANY or p == value or p == f"{ns}:*" for p in scope)


def within(child, parent) -> bool:
    """True when ``parent`` admits every entry of ``child``. A delegation
    may narrow its parent's scope, and may never widen it."""
    for c in child:
        if c == ANY:
            if ANY not in parent:
                return False
        elif c.endswith(":*"):
            if ANY not in parent and c not in parent:
                return False
        elif not covers(parent, c):
            return False
    return True


@dataclass(frozen=True)
class RoleGrant:
    """One grant of the registry: who holds which role, over what, and from
    and to which positions."""

    grant_id: str
    principal: str
    role: str
    scope: tuple
    valid_from_seq: int = 0
    valid_until_seq: int | None = None
    revoked_at_seq: int | None = None
    revocation_reason: str | None = None
    may_delegate: bool = False

    def lapse_at(self, seq: int):
        """``(kind, detail)`` when this grant does not hold at ``seq``."""
        if self.revoked_at_seq is not None and seq >= self.revoked_at_seq:
            return (REVOKED,
                    f"grant {self.grant_id!r} was revoked at seq "
                    f"{self.revoked_at_seq} ({self.revocation_reason})")
        if seq < self.valid_from_seq:
            return (NOT_YET_VALID,
                    f"grant {self.grant_id!r} holds from seq "
                    f"{self.valid_from_seq}")
        if self.valid_until_seq is not None and seq > self.valid_until_seq:
            return (EXPIRED,
                    f"grant {self.grant_id!r} held until seq "
                    f"{self.valid_until_seq}")
        return None

    def to_entry(self) -> dict:
        return {"grant_id": self.grant_id, "principal": self.principal,
                "role": self.role, "scope": list(self.scope),
                "valid_from_seq": self.valid_from_seq,
                "valid_until_seq": self.valid_until_seq,
                "revoked_at_seq": self.revoked_at_seq,
                "revocation_reason": self.revocation_reason,
                "may_delegate": self.may_delegate}


@dataclass(frozen=True)
class Delegation:
    """A narrower copy of a registry grant, made by its holder in the log.
    It holds after the position that made it, and only while its parent
    holds."""

    delegation_id: str
    parent: str
    delegator: str
    principal: str
    role: str
    scope: tuple
    seq: int
    valid_until_seq: int | None = None
    revoked_at_seq: int | None = None

    def lapse_at(self, seq: int, parent: RoleGrant):
        if self.revoked_at_seq is not None and seq >= self.revoked_at_seq:
            return (REVOKED,
                    f"delegation {self.delegation_id!r} was revoked at seq "
                    f"{self.revoked_at_seq}")
        if seq <= self.seq:
            return (NOT_YET_VALID,
                    f"delegation {self.delegation_id!r} was made at seq "
                    f"{self.seq}")
        if self.valid_until_seq is not None and seq > self.valid_until_seq:
            return (EXPIRED,
                    f"delegation {self.delegation_id!r} held until seq "
                    f"{self.valid_until_seq}")
        lapse = parent.lapse_at(seq)
        if lapse is not None:
            return (lapse[0],
                    f"delegation {self.delegation_id!r} stands on "
                    f"{lapse[1]}")
        return None

    def to_row(self) -> dict:
        return {"delegation_id": self.delegation_id, "parent": self.parent,
                "delegator": self.delegator, "principal": self.principal,
                "role": self.role, "scope": list(self.scope),
                "seq": self.seq, "valid_until_seq": self.valid_until_seq,
                "revoked_at_seq": self.revoked_at_seq}


def _grant(entry, principals) -> RoleGrant:
    """One registry entry, every field typed before anything uses it."""
    if not isinstance(entry, dict) or set(entry) != _GRANT_FIELDS:
        raise RoleError(f"unreadable grant entry; its fields are "
                        f"{sorted(_GRANT_FIELDS)}")
    gid = entry["grant_id"]
    if not _is_name(gid):
        raise RoleError("a grant names its id")
    if not _is_name(entry["principal"]):
        raise RoleError(f"grant {gid!r} names its principal")
    if entry["role"] not in ROLES:
        raise RoleError(f"grant {gid!r}: {entry['role']!r} is not one of "
                        f"{list(ROLES)}")
    for f in ("valid_until_seq", "revoked_at_seq"):
        if entry[f] is not None and not _is_seq(entry[f]):
            raise RoleError(f"grant {gid!r}: {f} is a seq or null")
    if not _is_seq(entry["valid_from_seq"]):
        raise RoleError(f"grant {gid!r}: valid_from_seq is a seq")
    reason = entry["revocation_reason"]
    if reason is not None and not _is_name(reason):
        raise RoleError(f"grant {gid!r}: a revocation reason is a string")
    if not isinstance(entry["may_delegate"], bool):
        raise RoleError(f"grant {gid!r}: may_delegate is true or false")
    g = RoleGrant(grant_id=gid, principal=entry["principal"],
                  role=entry["role"], scope=scope_of(entry["scope"]),
                  valid_from_seq=entry["valid_from_seq"],
                  valid_until_seq=entry["valid_until_seq"],
                  revoked_at_seq=entry["revoked_at_seq"],
                  revocation_reason=reason,
                  may_delegate=entry["may_delegate"])
    _check(g, principals)
    return g


def _check(g: RoleGrant, principals) -> None:
    if (g.valid_until_seq is not None
            and g.valid_until_seq < g.valid_from_seq):
        raise RoleError(f"grant {g.grant_id!r} ends before it begins")
    if (g.revoked_at_seq is None) != (g.revocation_reason is None):
        raise RoleError(
            f"grant {g.grant_id!r}: a revocation names both where and why")
    if g.may_delegate and g.role not in DELEGABLE:
        raise RoleError(
            f"grant {g.grant_id!r}: {g.role} is the root's to give, and may "
            f"not be delegated; only {sorted(DELEGABLE)} may")
    if principals is not None and g.principal not in principals:
        raise RoleError(
            f"grant {g.grant_id!r} names {g.principal!r}, who holds no key "
            "in the key registry this role registry is bound to; a role "
            "binds an authenticated identity")


class RoleRegistry:
    """Who holds which role. Supplied from OUTSIDE the log: a grant recorded
    in the log it governs is chosen by whoever writes that log.

    ``principals`` (a key registry's :attr:`principals`) binds every grant to
    an identity that can authenticate; a grant naming anyone else is refused
    before any grant is believed. ``key_registry`` is the digest of the key
    registry document the deployment pinned. A document written against
    another key registry is refused."""

    def __init__(self, grants, *, principals=None, key_registry=None,
                 version: int = 1):
        self.principals = (None if principals is None
                           else frozenset(principals))
        by_id: dict = {}
        for g in grants:
            if not isinstance(g, RoleGrant):
                raise RoleError(f"expected a RoleGrant, got {g!r}")
            g = replace(g, scope=scope_of(g.scope))
            _check(g, self.principals)
            if g.grant_id in by_id:
                raise RoleError(f"grant {g.grant_id!r} registered twice")
            by_id[g.grant_id] = g
        if len(by_id) > MAX_GRANTS:
            raise RoleError(f"a registry holds at most {MAX_GRANTS} grants")
        self._grants = by_id
        self.key_registry = key_registry
        self.version = version
        self._source_digest: str | None = None

    @property
    def digest(self) -> str:
        """The digest a deployment pins: of the document this registry was
        read from, or, for one built in code, of :meth:`to_document`."""
        return self._source_digest or registry_digest(self.to_document())

    def get(self, grant_id):
        return self._grants.get(grant_id) if isinstance(grant_id, str) \
            else None

    def grants(self) -> tuple:
        return tuple(self._grants[k] for k in sorted(self._grants))

    def held_by(self, principal: str, role: str) -> tuple:
        """``principal``'s grants of ``role``, by grant id."""
        return tuple(g for g in self.grants()
                     if g.principal == principal and g.role == role)

    def to_document(self) -> dict:
        return {"schema": SCHEMA, "registry_version": self.version,
                "key_registry": self.key_registry,
                "grants": [g.to_entry() for g in self.grants()]}

    @classmethod
    def from_document(cls, doc, *, pinned_digest: str | None = None,
                      principals=None, key_registry: str | None = None):
        """Read a registry document. With ``pinned_digest``, which is what a
        deployment supplies, a document whose canonical digest differs is
        refused before a single grant is believed."""
        try:
            got = registry_digest(doc)
        except (ValueError, TypeError, RecursionError):
            raise RoleError("the role registry is not a canonical JSON "
                            "document") from None
        if pinned_digest is not None and got != pinned_digest:
            raise RoleError("the role registry is not the one the "
                            "deployment pinned: its digest differs")
        if (not isinstance(doc, dict) or set(doc) != _HEADER
                or doc["schema"] != SCHEMA):
            raise RoleError(f"not a {SCHEMA} document")
        version = doc["registry_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise RoleError("unreadable registry_version")
        bound = doc["key_registry"]
        if bound is not None and not _is_name(bound):
            raise RoleError("key_registry is a digest or null")
        if key_registry is not None and bound != key_registry:
            raise RoleError(
                "the role registry was written against another key registry "
                f"({bound!r}) than the one this deployment pinned "
                f"({key_registry!r})")
        entries = doc["grants"]
        if not isinstance(entries, list) or len(entries) > MAX_GRANTS:
            raise RoleError(f"'grants' is a list of at most {MAX_GRANTS}")
        reg = cls([_grant(e, principals) for e in entries],
                  principals=principals, key_registry=bound,
                  version=version)
        reg._source_digest = got
        return reg

    @classmethod
    def from_bytes(cls, data: bytes, **kw):
        """A registry from its file's bytes. Every way those bytes can fail
        to be a document is a refusal, never a crash."""
        try:
            doc = json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise RoleError("the role registry is not a JSON document "
                            f"({type(exc).__name__})") from None
        return cls.from_document(doc, **kw)


def production_role_registry(*, principals=None,
                             key_registry: str | None = None
                             ) -> RoleRegistry:
    """The deployment's registry, at the digest the deployment pinned.
    Refuses while either is unprovisioned, rather than answering with an
    empty registry (which would refuse every act and look like a policy) or
    an unpinned one (which would trust whatever file was there)."""
    if PRODUCTION_ROLES is None or PRODUCTION_ROLE_REGISTRY_DIGEST is None:
        raise RoleNotConfigured(
            "no production role registry is provisioned. Who may propose, "
            "execute, verify, promote, own a policy, issue a secret or an "
            "egress grant, approve as a person or release is PENDING an "
            "owner decision. Nothing in this repository grants a "
            "production role, and nothing will invent one")
    return RoleRegistry.from_bytes(
        Path(PRODUCTION_ROLES).read_bytes(),
        pinned_digest=PRODUCTION_ROLE_REGISTRY_DIGEST,
        principals=principals, key_registry=key_registry)


class RoleView:
    """A registry, and the delegations a history made under it, folded to a
    position. The one question every enforcing reader asks it is
    :meth:`refusal`."""

    def __init__(self, registry: RoleRegistry):
        if not isinstance(registry, RoleRegistry):
            raise RoleError(f"expected a RoleRegistry, got {registry!r}")
        self.registry = registry
        self.delegations: dict = {}

    def refusal(self, principal, role: str, value: str, seq: int):
        """``(kind, detail)`` when ``principal`` may not act as ``role`` over
        ``value`` at ``seq``. None when a grant or a delegation it holds
        admits it.

        A principal's grants are asked in a fixed order: the registry's by
        grant id, then its delegations by id. The first refusal met is the
        answer, so every reader gives the same one.

        A delegation made at or after ``seq`` is not held at ``seq``, and a
        revocation is judged by its position. So a view folded past ``seq``
        answers exactly as one folded to it would."""
        held = [(g, None) for g in self.registry.held_by(principal, role)]
        held += [(d, self.registry.get(d.parent))
                 for _, d in sorted(self.delegations.items())
                 if d.principal == principal and d.role == role
                 and d.seq < seq]
        if not held:
            return (NOT_GRANTED, f"{principal!r} holds no {role} grant")
        first = None
        for g, parent in held:
            if isinstance(g, Delegation) and parent is None:
                # Folded under a registry that held its parent, read under
                # one that does not: it holds nothing.
                lapse = (REVOKED, f"delegation {g.delegation_id!r} stands on "
                                  f"grant {g.parent!r}, which the registry "
                                  "no longer holds")
            elif parent is None:
                lapse = g.lapse_at(seq)
            else:
                lapse = g.lapse_at(seq, parent)
            if lapse is None:
                if covers(g.scope, value):
                    return None
                name = g.grant_id if parent is None else g.delegation_id
                lapse = (OUT_OF_SCOPE,
                         f"{role} {name!r} ranges over {list(g.scope)}, "
                         f"not {value!r}")
            first = first or lapse
        return first

    def require(self, principal, roles, value: str, seq: int, *,
                what: str = "") -> None:
        """Raise :class:`RoleRefused` unless ``principal`` holds one of
        ``roles`` over ``value`` at ``seq``. The refusal is the first role's,
        in the order given."""
        first = (NOT_GRANTED, "no role was named")
        for i, role in enumerate((roles,) if isinstance(roles, str)
                                 else roles):
            r = self.refusal(principal, role, value, seq)
            if r is None:
                return
            if i == 0:
                first = r
        raise RoleRefused(first[0], f"{what}{first[1]}")

    # ---- delegation, folded from the log --------------------------------
    def delegation_refusal(self, actor, p, seq: int) -> str | None:
        """Why a ``role.delegate`` record by ``actor`` at ``seq`` is refused,
        or None. The live writer and every replay ask this one function."""
        if not isinstance(p, dict) or set(p) != _DELEGATE_FIELDS:
            return ("a delegation names exactly "
                    f"{sorted(_DELEGATE_FIELDS)}")
        did = p["delegation_id"]
        if not _is_name(did):
            return "a delegation names its id"
        if did in self.delegations:
            return f"delegation {did!r} was already made"
        parent = self.registry.get(p["parent"])
        if parent is None:
            return (f"delegation {did!r} names {p['parent']!r}, which is no "
                    "grant of the registry; a delegation does not delegate")
        if actor != parent.principal:
            return (f"{actor!r} delegates grant {parent.grant_id!r}, which "
                    f"{parent.principal!r} holds; only a grant's holder "
                    "delegates it")
        if not parent.may_delegate:
            return f"grant {parent.grant_id!r} may not be delegated"
        lapse = parent.lapse_at(seq)
        if lapse is not None:
            return (f"grant {parent.grant_id!r} does not hold at seq {seq}: "
                    f"{lapse[1]}")
        if p["role"] != parent.role:
            return (f"delegation {did!r} names {p['role']!r}; its parent "
                    f"grants {parent.role}")
        principal = p["principal"]
        if not _is_name(principal):
            return f"delegation {did!r} names its principal"
        if principal == actor:
            return (f"{actor!r} delegates {parent.role} to itself; no "
                    "principal authorizes itself")
        if (self.registry.principals is not None
                and principal not in self.registry.principals):
            return (f"{principal!r} holds no key in the key registry this "
                    "role registry is bound to")
        try:
            scope = scope_of(p["scope"])
        except RoleError as exc:
            return f"delegation {did!r}: {exc}"
        if list(scope) != p["scope"]:
            return f"delegation {did!r}: its scope is sorted and unique"
        if not within(scope, parent.scope):
            return (f"delegation {did!r} ranges over {list(scope)}, wider "
                    f"than grant {parent.grant_id!r} "
                    f"({list(parent.scope)})")
        until = p["valid_until_seq"]
        if until is not None and not _is_seq(until):
            return f"delegation {did!r}: valid_until_seq is a seq or null"
        if parent.valid_until_seq is not None and (
                until is None or until > parent.valid_until_seq):
            return (f"delegation {did!r} would outlast grant "
                    f"{parent.grant_id!r}, which holds until seq "
                    f"{parent.valid_until_seq}")
        return None

    def revocation_refusal(self, actor, p, seq: int) -> str | None:
        if not isinstance(p, dict) or set(p) != _REVOKE_FIELDS:
            return f"a revocation names exactly {sorted(_REVOKE_FIELDS)}"
        did = p["delegation_id"]
        d = self.delegations.get(did) if isinstance(did, str) else None
        if d is None:
            return (f"a revocation names delegation {did!r}, which this "
                    "history never made")
        if actor != d.delegator:
            return (f"{actor!r} revokes delegation {did!r}, made by "
                    f"{d.delegator!r}; a delegation is withdrawn by whoever "
                    "made it")
        if d.revoked_at_seq is not None:
            return (f"delegation {did!r} was revoked at seq "
                    f"{d.revoked_at_seq}")
        if not _is_name(p["reason"]):
            return "a revocation says why"
        return None

    def fold(self, ev) -> bool:
        """Fold one event. True when it was a role record. Raises
        :class:`RoleError` on one these rules refuse: presence in the log
        is not authority."""
        if ev.action == ACT_DELEGATE:
            why = self.delegation_refusal(ev.actor, ev.payload, ev.seq)
            if why is not None:
                raise RoleError(f"seq {ev.seq}: {why}")
            p = ev.payload
            self.delegations[p["delegation_id"]] = Delegation(
                delegation_id=p["delegation_id"], parent=p["parent"],
                delegator=ev.actor, principal=p["principal"],
                role=p["role"], scope=tuple(p["scope"]), seq=ev.seq,
                valid_until_seq=p["valid_until_seq"])
            return True
        if ev.action == ACT_REVOKE:
            why = self.revocation_refusal(ev.actor, ev.payload, ev.seq)
            if why is not None:
                raise RoleError(f"seq {ev.seq}: {why}")
            did = ev.payload["delegation_id"]
            self.delegations[did] = replace(self.delegations[did],
                                            revoked_at_seq=ev.seq)
            return True
        return False

    # ---- snapshots ------------------------------------------------------
    def export(self) -> dict:
        """What a snapshot holds: the registry it was folded under, by
        digest, and the delegations, by id."""
        return {"registry": self.registry.digest,
                "delegations": [d.to_row() for _, d in
                                sorted(self.delegations.items())]}

    def restore(self, row) -> None:
        """Take a snapshot's delegations, or refuse. A snapshot folded under
        another registry is refused rather than believed: what it holds was
        decided under grants this reader was not given."""
        if not isinstance(row, dict) or set(row) != {"registry",
                                                     "delegations"}:
            raise RoleError("the snapshot's role view is malformed")
        if row["registry"] != self.registry.digest:
            raise RoleError(
                "the snapshot was folded under role registry "
                f"{str(row['registry'])[:12]}, and this reader holds "
                f"{self.registry.digest[:12]}")
        rows = row["delegations"]
        if not isinstance(rows, list):
            raise RoleError("the snapshot's delegations are malformed")
        out: dict = {}
        for r in rows:
            try:
                d = Delegation(**{**r, "scope": scope_of(r["scope"])})
            except (TypeError, KeyError) as exc:
                raise RoleError(
                    f"a snapshot delegation is malformed: {exc}") from None
            if not (_is_name(d.delegation_id) and _is_name(d.principal)
                    and _is_name(d.delegator) and d.role in DELEGABLE
                    and _is_seq(d.seq)):
                raise RoleError("a snapshot delegation is malformed")
            out[d.delegation_id] = d
        self.delegations = out


class RoleDelegations(DecidedWriter):
    """Writes delegations and their revocations. Each is decided under the
    writer lock, against the log as it stands, before its record exists."""

    def __init__(self, log, registry: RoleRegistry):
        self.log = log
        self.view = RoleView(registry)
        self._reset_fold()

    @projection_load
    def load(self) -> "RoleDelegations":
        report, events = self.log.read_verified()
        report.raise_if_bad()
        self.view = RoleView(self.view.registry)
        self._reset_fold()
        for ev in events:
            self.apply(ev)
        self._set_fold_anchor(report)
        return self

    def apply(self, ev) -> bool:
        self._folded(ev)
        return self.view.fold(ev)

    def delegate(self, *, actor: str, delegation_id: str, parent: str,
                 principal: str, scope, valid_until_seq: int | None = None
                 ) -> Delegation:
        try:
            entries = sorted(set(scope))
        except TypeError:
            entries = scope         # refused below, by the rule that reads it

        def decide(head_seq: int) -> dict:
            g = self.view.registry.get(parent)
            payload = {"delegation_id": delegation_id, "parent": parent,
                       "role": None if g is None else g.role,
                       "principal": principal, "scope": entries,
                       "valid_until_seq": valid_until_seq}
            why = self.view.delegation_refusal(actor, payload, head_seq + 1)
            if why is not None:
                raise RoleError(why)
            return dict(actor=actor, action=ACT_DELEGATE,
                        target=delegation_id, payload=payload)

        self._append_decided(decide)
        return self.view.delegations[delegation_id]

    def revoke(self, *, actor: str, delegation_id: str, reason: str) -> None:
        def decide(head_seq: int) -> dict:
            payload = {"delegation_id": delegation_id, "reason": reason}
            why = self.view.revocation_refusal(actor, payload, head_seq + 1)
            if why is not None:
                raise RoleError(why)
            return dict(actor=actor, action=ACT_REVOKE,
                        target=delegation_id, payload=payload)

        self._append_decided(decide)
