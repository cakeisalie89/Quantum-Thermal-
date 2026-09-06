"""Capabilities: authority as an object, not a label.

WHY THIS IS NOT A PERMISSION FLAG

A boolean "may_write" answers the wrong question. The questions that matter are
*who*, *for which task*, *with which tool*, *over which paths*, and *until
when* -- and a flag answers none of them. Worse, a flag is ambient: once some
component holds it, every call that component makes inherits it, which is the
confused deputy in its purest form. A component that legitimately needs to
write one file ends up able to write any file, because the authority was
attached to the component rather than to the request.

A capability here is a specific, bounded, attributable grant. It names its
subject, its task, its tool and its scope, and it is checked against the
request being made -- not against the identity of whoever is making it. Holding
a capability for task T and tool X gives you nothing at all for task U or tool
Y, so a component that is tricked into acting for someone else's task fails
closed instead of succeeding on their behalf.

WHAT A CAPABILITY IS NOT

It is not authentication. This module cannot tell you that an actor is who they
say they are; ``subject`` is a name the issuer chose. What it gives you is that
a grant issued *to* that name cannot be used *as* another name, and that every
use is checkable against a record of what was granted.

It is not a secret. A capability's digest is derived from its fields, so
anyone who can read one can recompute it. Unforgeability comes from the issuing
record in the event log, not from the value being hard to guess: a capability
that was never issued does not appear in the log, and
:meth:`CapabilitySet.check` refuses it. Treating the digest as a bearer token
would be a mistake, and :func:`check` never does.

WHO MAY ISSUE, AND WHAT A DELEGATION MAY NOT WIDEN

For a long time this module answered "who granted this" and refused to answer
"who may grant". :meth:`CapabilityLedger.issuer_of` recorded the actor and
:meth:`CapabilitySet.check` deliberately did not consult it, which is
attribution rather than authorization -- and it meant any actor able to append
to the log could mint itself any grant it liked.

Two things close that, and one thing bounds it:

*The root issuer is explicit and unique.* The first grant on a log also
appends a :data:`ACT_ROOT` record naming its actor. From that position on,
:meth:`apply` REFUSES an issue record from any other actor -- at load time, in
both the ledger and any independent reconstruction, so a forged grant is a
verification failure rather than authority somebody cites.

*Authority flows downward only.* A subject holding a grant may
:meth:`CapabilityLedger.delegate` a WEAKER one to somebody else. Weaker is
checked, not asserted: same action, same task, same tool, scope a subset of
the parent's, expiry no later than the parent's. A delegation that widens any
of those is refused, and a delegation of a grant the actor does not hold is
refused before that. :meth:`CapabilitySet.check` then walks the chain, so a
revoked or expired parent takes its whole subtree with it -- revocation that
stopped at one node would leave the delegate holding authority its source no
longer has.

*The root itself is asserted, not authenticated.* Whoever writes first is the
root. Nothing in this repository can do better, because nothing here can prove
an actor is who it says it is; that needs an identity authority this build
deliberately does not contain. What the root DOES buy is that after it exists,
the set of actors who may mint is one, and every other attempt fails closed.

EXPIRY IS IN SEQUENCE NUMBERS, NOT WALL TIME

The log is ordered by ``seq`` and wall clocks move backwards. A capability that
expired "at 10:03" is a capability whose validity depends on whose clock you
ask; one that expires after seq 41 has the same answer for every reader of the
same log. Wall time is recorded for humans and never used for a decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import PurePosixPath
from typing import FrozenSet

from .canonical import digest, is_digest


class CapabilityError(Exception):
    """Base class. Every failure here is fail-closed."""


class CapabilityDenied(CapabilityError):
    """The grant does not authorize this request."""


class CapabilityRevoked(CapabilityDenied):
    """The grant was withdrawn before the request was made."""


class CapabilityExpired(CapabilityDenied):
    """The grant was valid, and is no longer."""


class CapabilityNotYetIssued(CapabilityDenied):
    """The grant exists in the log, but not yet at the position asked about.

    The other half of :class:`CapabilityExpired`. A grant has a window, and a
    window with only one end is a half-check: without this, a capability
    recorded at seq 90 answered "was this permitted at seq 20?" with yes.
    """


class NotTheIssuer(CapabilityError):
    """An actor that is not the root issuer tried to mint a grant.

    Not a :class:`CapabilityDenied`: nobody was refused the USE of a grant.
    A grant was refused existence, which is a different failure and belongs
    at load time rather than at check time.
    """


class BadDelegation(CapabilityError):
    """A delegation would widen the authority it derives from."""


class CapabilityUnknown(CapabilityDenied):
    """No such grant was ever issued."""


class Action(str, Enum):
    """What a grant permits. Deliberately coarse and deliberately closed.

    A new action is a deliberate widening of what the system can be asked to
    do, so adding one is an edit to this enum and not a string a caller can
    invent.
    """

    #: Run a registered tool. Scope names the tool's writable paths.
    EXECUTE_TOOL = "EXECUTE_TOOL"
    #: Write files under the scoped paths. Never implies EXECUTE_TOOL.
    WRITE_PATHS = "WRITE_PATHS"
    #: Read the named paths. Separate from writing on purpose -- a component
    #: that may write its output must not thereby be able to read the corpus.
    READ_PATHS = "READ_PATHS"


#: Sentinel meaning "this grant never expires on its own". Revocation still
#: applies; an unexpiring grant is not an unrevocable one.
NEVER_EXPIRES = -1

#: Event actions this module owns. Constants so a typo cannot create a second,
#: unread action.
ACT_ISSUE = "capability.issue"
ACT_REVOKE = "capability.revoke"
#: Establishes the one actor permitted to mint grants on this log. Appended
#: exactly once, alongside the first grant.
ACT_ROOT = "capability.root"

#: How deep a delegation chain may go. A bound, not a policy: chain walking is
#: linear in depth on every check, and an unbounded chain is a denial of
#: service that a delegate can create for everyone else.
MAX_DELEGATION_DEPTH = 8


def _normalise_scope(paths) -> tuple:
    """Repo-relative POSIX prefixes, sorted, validated, de-duplicated.

    Refuses absolute paths and traversal outright rather than normalising
    them: a scope is the definition of what a grant covers, and silently
    rewriting it changes what was granted.
    """
    out = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            raise CapabilityError(
                f"scope entry must be a non-empty str: {raw!r}")
        p = PurePosixPath(raw)
        if p.is_absolute():
            raise CapabilityError(
                f"scope entry {raw!r} is absolute; scopes are repository-"
                "relative so that a grant means the same thing on every host")
        if any(part in ("..", ".") for part in p.parts):
            raise CapabilityError(
                f"scope entry {raw!r} contains '..' or '.'; refused rather "
                "than normalised, because normalising changes the grant")
        if not p.parts:
            # PurePosixPath(".").parts is EMPTY, so the traversal check above
            # does not see it -- and "." is the parent of every relative path,
            # so such a scope would silently grant the entire repository. This
            # is the one scope value that looks narrow and is total.
            raise CapabilityError(
                f"scope entry {raw!r} names the repository root; a grant over "
                "everything is refused, because a scope that covers all paths "
                "is not a scope")
        out.add(p.as_posix())
    if not out:
        raise CapabilityError(
            "a capability with an empty scope grants nothing and is refused "
            "rather than issued; an empty allowlist is a mistake, "
            "not a policy")
    return tuple(sorted(out))


@dataclass(frozen=True)
class Capability:
    """One bounded grant. Immutable; its digest is its identity."""

    capability_id: str
    #: The actor this was granted TO. Not authenticated -- see module docs.
    subject: str
    action: Action
    #: The task this grant is confined to. A grant is never task-portable.
    task_id: str
    #: For EXECUTE_TOOL, the one tool this authorizes. Empty otherwise.
    tool_id: str
    #: Repo-relative path prefixes this grant covers.
    scope: tuple
    #: Log seq at which this was issued.
    issued_seq: int
    #: Last seq at which this is valid, or NEVER_EXPIRES.
    expires_after_seq: int
    #: The grant this was attenuated from, or "" for a root-issued grant.
    #: Part of body(), so a delegation cannot be re-parented without becoming
    #: a different capability.
    parent_id: str = ""
    #: Recorded for humans. Never consulted for a decision.
    issued_wall_time: float = 0.0

    def body(self) -> dict:
        return {
            "capability_id": self.capability_id,
            "subject": self.subject,
            "action": self.action.value,
            "task_id": self.task_id,
            "tool_id": self.tool_id,
            "scope": list(self.scope),
            "issued_seq": self.issued_seq,
            "expires_after_seq": self.expires_after_seq,
            "parent_id": self.parent_id,
        }

    def digest(self) -> str:
        """Content digest over the granting fields, excluding wall time."""
        return digest(self.body())

    def covers_path(self, rel: str) -> bool:
        """True when ``rel`` lies at or under one of the scope prefixes.

        Prefix matching is done on path COMPONENTS, not on strings.
        ``verification/stage10x`` starts with ``verification/stage10`` as text
        and is a different directory; a string prefix test would grant it.
        """
        try:
            target = PurePosixPath(rel)
        except (TypeError, ValueError):
            return False
        if target.is_absolute() or any(p in ("..", ".") for p in target.parts):
            return False
        for allowed in self.scope:
            a = PurePosixPath(allowed)
            if target == a or a in target.parents:
                return True
        return False

    def attenuation_of(self, parent: "Capability") -> str:
        """"" if this is no wider than ``parent``, else why it is wider.

        Returns a REASON rather than a bool because every caller of this
        reports the failure, and a bool would make each of them invent its
        own description of a rule that lives here.

        Scope containment is by prefix coverage rather than set membership:
        a child may narrow ``a/`` to ``a/b/``, which is not a subset of the
        parent's literal prefixes but is entirely inside what they cover.
        """
        if self.action is not parent.action:
            return (f"delegation grants {self.action.value} from a parent "
                    f"that grants {parent.action.value}; a delegation "
                    "attenuates authority, it does not translate it")
        if self.task_id != parent.task_id:
            return (f"delegation is for task {self.task_id!r} from a parent "
                    f"confined to {parent.task_id!r}; task confinement is the "
                    "boundary a delegation must not cross")
        if self.tool_id != parent.tool_id:
            return (f"delegation names tool {self.tool_id!r} and its parent "
                    f"names {parent.tool_id!r}")
        outside = [p for p in self.scope if not parent.covers_path(p)]
        if outside:
            return (f"delegation covers {sorted(outside)}, which its parent's "
                    f"scope {list(parent.scope)} does not; a child cannot "
                    "hand out what its parent was never given")
        if parent.expires_after_seq != NEVER_EXPIRES:
            if (self.expires_after_seq == NEVER_EXPIRES
                    or self.expires_after_seq > parent.expires_after_seq):
                return (f"delegation expires after seq "
                        f"{self.expires_after_seq} and its parent expires "
                        f"after {parent.expires_after_seq}; a child that "
                        "outlives its parent is authority that survives its "
                        "own source")
        if self.issued_seq < parent.issued_seq:
            return (f"delegation is issued at seq {self.issued_seq}, before "
                    f"its parent at {parent.issued_seq}; a grant cannot "
                    "derive from authority that did not exist yet")
        return ""


@dataclass(frozen=True)
class Request:
    """What is being attempted, described independently of who is asking.

    Separate from :class:`Capability` on purpose. The check compares a
    described request against a described grant; nothing in the check can
    consult the caller's identity, so a caller cannot be trusted into
    authority they were not granted.
    """

    actor: str
    action: Action
    task_id: str
    tool_id: str = ""
    paths: tuple = ()


@dataclass(frozen=True)
class CapabilitySet:
    """The grants in force, and the revocations against them.

    Both come from the event log, so "in force" is a statement about a
    verified, ordered history rather than about a mutable in-memory set.
    """

    issued: dict = field(default_factory=dict)
    revoked: FrozenSet[str] = frozenset()
    #: The log position the decision is being made at. Expiry is relative to
    #: this, so the same log answers identically for every reader.
    at_seq: int = 0

    def check(self, cap_id: str, req: Request) -> Capability:
        """Authorize ``req`` under grant ``cap_id``, or raise.

        Order matters and is deliberate: existence, then revocation, then
        the validity WINDOW (not yet issued, then expired), then subject,
        then task, then action, then tool, then paths.
        The message names the FIRST thing that was wrong, and the earlier
        checks are the ones an operator can act on -- "this grant was revoked"
        is actionable, "this path is not in scope" on a revoked grant is
        misleading.
        """
        cap = self.issued.get(cap_id)
        if cap is None:
            raise CapabilityUnknown(
                f"no capability {cap_id!r} was ever issued; a grant that does "
                "not appear in the log does not exist, whatever its holder "
                "presents")
        if cap_id in self.revoked:
            raise CapabilityRevoked(
                f"capability {cap_id!r} was revoked; it authorizes nothing "
                "from the moment the revocation was recorded")
        if self.at_seq < cap.issued_seq:
            # A grant has two ends. Only one was checked, so a capability
            # recorded at seq 90 authorized actions at seq 20 -- and the
            # question an auditor asks is precisely "was this permitted at
            # the time", which this answered with a grant that did not yet
            # exist.
            raise CapabilityNotYetIssued(
                f"capability {cap_id!r} was issued at seq {cap.issued_seq} "
                f"and the question is at seq {self.at_seq}; a grant does not "
                "reach backwards over what was already done")
        if (cap.expires_after_seq != NEVER_EXPIRES
                and self.at_seq > cap.expires_after_seq):
            raise CapabilityExpired(
                f"capability {cap_id!r} expired after seq "
                f"{cap.expires_after_seq}; the log is at {self.at_seq}")
        if cap.subject != req.actor:
            raise CapabilityDenied(
                f"capability {cap_id!r} was granted to {cap.subject!r}, not "
                f"{req.actor!r}; a grant is not a bearer token")
        if cap.task_id != req.task_id:
            raise CapabilityDenied(
                f"capability {cap_id!r} is confined to task {cap.task_id!r} "
                f"and cannot be used for {req.task_id!r}; this is what "
                "stops a "
                "component being tricked into acting on another task's behalf")
        if cap.action is not req.action:
            raise CapabilityDenied(
                f"capability {cap_id!r} permits {cap.action.value}, not "
                f"{req.action.value}")
        if cap.action is Action.EXECUTE_TOOL and cap.tool_id != req.tool_id:
            raise CapabilityDenied(
                f"capability {cap_id!r} permits tool {cap.tool_id!r}, not "
                f"{req.tool_id!r}")
        outside = [p for p in req.paths if not cap.covers_path(p)]
        if outside:
            raise CapabilityDenied(
                f"capability {cap_id!r} does not cover {sorted(outside)}; its "
                f"scope is {list(cap.scope)}. A grant is never widened by the "
                "request that needs it to be.")
        self._check_chain(cap)
        return cap

    def _check_chain(self, cap: Capability) -> None:
        """Every ancestor must still be live at ``at_seq``.

        Attenuation is verified once, when the delegation is recorded, and
        again by every reader that folds the log. What CANNOT be settled then
        is whether the parent is still in force NOW: revocation and expiry
        happen after issuance, by definition.

        So revoking a grant revokes its subtree. The alternative -- checking
        only the leaf -- would leave a delegate holding authority its source
        no longer has, which is precisely the state revocation exists to
        prevent, and it would be invisible because the leaf itself is
        untouched.
        """
        seen = {cap.capability_id}
        current = cap
        # MAX + 1 passes, not MAX. A chain of exactly MAX links needs MAX
        # passes to follow them and one more to SEE the root's empty
        # parent_id and return. Bounding the walk at MAX made the reader
        # stricter than the writer, so delegate() would happily mint a
        # legitimate grant at the limit that no check would ever accept --
        # authority that exists and cannot be used.
        for _ in range(MAX_DELEGATION_DEPTH + 1):
            parent_id = current.parent_id
            if not parent_id:
                return
            if parent_id in seen:
                # Not reachable through delegate(), which refuses to create
                # one. Reachable through a hand-written log, which is exactly
                # what a check is for: a cycle would otherwise spin here
                # until the depth bound, and a bound is not an answer.
                raise CapabilityDenied(
                    f"capability {cap.capability_id!r} sits on a delegation "
                    f"cycle through {parent_id!r}; authority that derives "
                    "from itself derives from nothing")
            seen.add(parent_id)
            parent = self.issued.get(parent_id)
            if parent is None:
                raise CapabilityUnknown(
                    f"capability {current.capability_id!r} was delegated from "
                    f"{parent_id!r}, which this log never issued; a chain "
                    "with a missing link grants nothing")
            if parent_id in self.revoked:
                raise CapabilityRevoked(
                    f"capability {cap.capability_id!r} derives from "
                    f"{parent_id!r}, which was revoked; revoking a grant "
                    "revokes what was delegated from it, or revocation would "
                    "leave the delegate holding what its source lost")
            if (parent.expires_after_seq != NEVER_EXPIRES
                    and self.at_seq > parent.expires_after_seq):
                raise CapabilityExpired(
                    f"capability {cap.capability_id!r} derives from "
                    f"{parent_id!r}, which expired after seq "
                    f"{parent.expires_after_seq}; the log is at {self.at_seq}")
            current = parent
        raise CapabilityDenied(
            f"capability {cap.capability_id!r} sits deeper than "
            f"{MAX_DELEGATION_DEPTH} delegations; the bound exists so that "
            "one delegate cannot make every check expensive for everyone")


def issue(*, capability_id: str, subject: str, action: Action, task_id: str,
          scope, issued_seq: int, tool_id: str = "",
          expires_after_seq: int = NEVER_EXPIRES, parent_id: str = "",
          issued_wall_time: float = 0.0) -> Capability:
    """Construct a grant, validating everything that cannot be fixed later."""
    if not capability_id or not isinstance(capability_id, str):
        raise CapabilityError("capability_id must be a non-empty str")
    if not subject or not isinstance(subject, str):
        raise CapabilityError("subject must be a non-empty str")
    if not task_id or not isinstance(task_id, str):
        raise CapabilityError(
            "task_id must be a non-empty str; a grant with no task is a grant "
            "with no boundary")
    if not isinstance(action, Action):
        raise CapabilityError(f"action must be an Action, got {action!r}")
    if action is Action.EXECUTE_TOOL and not tool_id:
        raise CapabilityError(
            "EXECUTE_TOOL requires a tool_id; a grant to run 'some tool' is "
            "a grant to run any tool")
    if action is not Action.EXECUTE_TOOL and tool_id:
        raise CapabilityError(
            f"{action.value} does not take a tool_id; carrying one would "
            "imply a tool authority this grant does not confer")
    if not isinstance(issued_seq, int) or isinstance(issued_seq, bool) \
            or issued_seq < 0:
        raise CapabilityError("issued_seq must be a non-negative int")
    if expires_after_seq != NEVER_EXPIRES:
        if (not isinstance(expires_after_seq, int)
                or isinstance(expires_after_seq, bool)):
            raise CapabilityError("expires_after_seq must be an int")
        if expires_after_seq < issued_seq:
            raise CapabilityError(
                f"capability would expire at {expires_after_seq}, before it "
                f"was issued at {issued_seq}; refusing to create a grant that "
                "was never valid")
    if parent_id is not None and not isinstance(parent_id, str):
        raise CapabilityError(f"parent_id must be a str, got {parent_id!r}")
    if parent_id == capability_id:
        raise CapabilityError(
            f"capability {capability_id!r} names itself as its parent; "
            "authority that derives from itself derives from nothing")
    return Capability(
        capability_id=capability_id, subject=subject, action=action,
        task_id=task_id, tool_id=tool_id, scope=_normalise_scope(scope),
        issued_seq=issued_seq, expires_after_seq=expires_after_seq,
        parent_id=parent_id or "",
        issued_wall_time=float(issued_wall_time))


def capability_from_record(rec: dict) -> Capability:
    """Rebuild a grant from a log payload, validating its shape.

    Used by the projection and by the independent reconstruction, so a
    malformed record fails the same way in both rather than being tolerated by
    one of them.
    """
    if not isinstance(rec, dict):
        raise CapabilityError(f"capability record is {type(rec).__name__}")
    try:
        action = Action(rec["action"])
    except (KeyError, ValueError) as exc:
        raise CapabilityError(f"bad capability action: {exc}") from exc
    try:
        return issue(
            capability_id=rec["capability_id"], subject=rec["subject"],
            action=action, task_id=rec["task_id"],
            tool_id=rec.get("tool_id", ""),
            scope=rec["scope"], issued_seq=rec["issued_seq"],
            expires_after_seq=rec.get("expires_after_seq", NEVER_EXPIRES),
            parent_id=rec.get("parent_id", ""),
            issued_wall_time=rec.get("issued_wall_time", 0.0))
    except KeyError as exc:
        raise CapabilityError(f"capability record missing {exc}") from exc


def digest_is_consistent(cap: Capability, claimed: str) -> bool:
    """Does ``claimed`` match the grant's content digest?

    Offered for records that carry a digest alongside the fields, so a reader
    can notice the two disagreeing. This is NOT an authorization check and
    must never be used as one -- see the module docstring on why a capability
    digest is not a bearer token.
    """
    return is_digest(claimed) and cap.digest() == claimed


class CapabilityLedger:
    """The grants in force, PROJECTED from the log rather than asserted.

    WHY THIS EXISTS SEPARATELY FROM :class:`CapabilitySet`

    ``CapabilitySet`` is a decision object: hand it grants and a position and
    it will authorize or refuse. It does not care where the grants came from,
    which is right for a checker and wrong for a system -- because a caller
    that assembles the set can put anything in it. The governed path did
    exactly that: it appended an issuance event AND separately built
    ``CapabilitySet(issued={cap_id: cap})`` from the same local variable. The
    log record was decorative; the executor was checking against whatever the
    caller had in hand.

    This ledger closes that. The in-force set is built by replaying issuance
    and revocation events, so a grant that was never recorded does not exist
    no matter what the caller holds, and a revoked one stops working without
    the caller having to remember.
    """

    def __init__(self, log):
        self.log = log
        self._issued: dict = {}
        self._issued_by: dict = {}
        self._revoked: set = set()
        self._root: str | None = None
        self._root_seq = -1
        self._at_seq = -1

    # ---- projection ----------------------------------------------------
    def load(self) -> "CapabilityLedger":
        self.log.verify().raise_if_bad()
        self._issued = {}
        self._issued_by = {}
        self._revoked = set()
        self._root = None
        self._root_seq = -1
        self._at_seq = -1
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        """Fold one event in. True when it was a capability event."""
        if ev.action == ACT_ROOT:
            issuer = ev.payload.get("issuer")
            if not isinstance(issuer, str) or not issuer:
                raise CapabilityError(
                    f"seq {ev.seq}: root record names no issuer")
            if issuer != ev.actor:
                # The actor of the record is who wrote it. A record that
                # anoints somebody ELSE is a third party assigning minting
                # authority, which is the thing this event exists to stop
                # being possible without a record of its own.
                raise NotTheIssuer(
                    f"seq {ev.seq}: {ev.actor!r} anointed {issuer!r} as root "
                    "issuer. The root is the actor that establishes it; "
                    "nominating a third party is a delegation, and "
                    "delegations are capabilities, not roots")
            if self._root is not None and self._root != issuer:
                raise NotTheIssuer(
                    f"seq {ev.seq}: {issuer!r} claims to be root issuer, but "
                    f"{self._root!r} already is. A log with two roots has no "
                    "root: every grant would be mintable by whichever one "
                    "the reader happened to consult")
            self._root = issuer
            self._root_seq = ev.seq
            self._at_seq = ev.seq
            return True
        if ev.action == ACT_ISSUE:
            cap = capability_from_record(ev.payload)
            existing = self._issued.get(cap.capability_id)
            if existing is not None:
                if existing.digest() != cap.digest():
                    raise CapabilityError(
                        f"seq {ev.seq}: capability {cap.capability_id!r} was "
                        "issued twice with different terms; two grants "
                        "sharing an id cannot be told apart by anything that "
                        "cites one")
                # Byte-identical: a replay of a grant already in force, which
                # a retried append can legitimately produce. Its start is the
                # FIRST record's, and that one was checked when it was read.
                self._at_seq = ev.seq
                return True
            if cap.issued_seq != ev.seq:
                # WHERE a grant starts is the log's to say, not the record's.
                # A capability appended at seq 90 claiming issued_seq 5 reads,
                # to every later question about seq 5..89, as authority that
                # was in force at the time. The window is only meaningful if
                # its start is the position the grant actually appeared at.
                raise CapabilityError(
                    f"seq {ev.seq}: capability {cap.capability_id!r} claims "
                    f"it was issued at seq {cap.issued_seq}. A grant is in "
                    "force from where it appears in the log; one that names "
                    "its own start could be backdated over anything already "
                    "done.")
            self._authorize_mint(ev, cap)
            self._issued[cap.capability_id] = cap
            # WHO granted it. Kept beside the grant rather than inside it,
            # because body() defines the capability's digest and its identity
            # must not depend on who recorded it. Not an authorization -- see
            # :meth:`issuer_of` -- but an auditor asking "where did this
            # authority come from" now has an answer that is not "the log".
            self._issued_by[cap.capability_id] = ev.actor
        elif ev.action == ACT_REVOKE:
            cap_id = ev.payload.get("capability_id")
            if not cap_id:
                raise CapabilityError(
                    f"seq {ev.seq}: revocation names no capability")
            self._revoked.add(cap_id)
        else:
            return False
        self._at_seq = ev.seq
        return True

    def _authorize_mint(self, ev, cap: Capability) -> None:
        """May ``ev.actor`` bring ``cap`` into existence?

        This is the check :meth:`issuer_of` deliberately was not. It runs at
        LOAD time, in the projection every reader builds, so a forged grant
        appended by an arbitrary actor is a verification failure rather than
        authority somebody cites -- and an independent reconstruction reaches
        the same verdict because it folds the same records through the same
        rule.

        Three cases, in order:

        * no root yet -- this record establishes one, and the ledger's writer
          appends the :data:`ACT_ROOT` event just before it. A log whose
          first grant carries no root is a log written by something other
          than this module, and it is refused.
        * a delegation -- the actor must be the parent's subject, and the
          child must be an attenuation. Verified here and not only at mint
          time, because the mint-time path is code and this is the record.
        * a root mint -- the actor must be the root.
        """
        if self._root is None:
            raise NotTheIssuer(
                f"seq {ev.seq}: capability {cap.capability_id!r} is the "
                "first grant on this log and nothing established a root "
                "issuer before it. Minting authority that answers to nobody "
                "is the state this check exists to end")
        if cap.parent_id:
            parent = self._issued.get(cap.parent_id)
            if parent is None:
                raise BadDelegation(
                    f"seq {ev.seq}: capability {cap.capability_id!r} is "
                    f"delegated from {cap.parent_id!r}, which this log has "
                    "not issued at this position")
            if ev.actor != parent.subject:
                raise NotTheIssuer(
                    f"seq {ev.seq}: {ev.actor!r} delegated "
                    f"{cap.parent_id!r}, which was granted to "
                    f"{parent.subject!r}. Delegating a grant you do not hold "
                    "is minting, wearing a chain for cover")
            why = cap.attenuation_of(parent)
            if why:
                raise BadDelegation(f"seq {ev.seq}: {why}")
            return
        if ev.actor != self._root:
            raise NotTheIssuer(
                f"seq {ev.seq}: {ev.actor!r} minted capability "
                f"{cap.capability_id!r}, and the root issuer on this log is "
                f"{self._root!r}. An actor that is neither the root nor "
                "holding the grant it delegates has no authority to create "
                "one")

    # ---- reads ---------------------------------------------------------
    def root_issuer(self) -> str | None:
        """The one actor permitted to mint non-delegated grants, if set.

        ``None`` on a log with no grants yet. Note what this is NOT: proof
        that the named actor is who it says it is. Whoever wrote first is the
        root, and nothing here can authenticate that -- see the module
        docstring. What it buys is that after the root exists, minting is one
        actor's, and every other attempt fails closed at load time.
        """
        return self._root

    def in_force(self, at_seq: int | None = None) -> CapabilitySet:
        """The set to check against, at a log position.

        Expiry is relative to ``at_seq``, so two readers of the same log at
        the same position reach the same verdict.
        """
        return CapabilitySet(
            issued=dict(self._issued), revoked=frozenset(self._revoked),
            at_seq=self._at_seq if at_seq is None else at_seq)

    def issued_ids(self) -> tuple:
        return tuple(sorted(self._issued))

    def issuer_of(self, capability_id: str) -> str | None:
        """The actor whose record granted ``capability_id``.

        Diagnostic, and deliberately not consulted by :meth:`CapabilitySet.
        check`. Nothing here constrains WHO may issue a grant -- there is no
        issuer authority in this build, and saying so is more useful than a
        check that reads like one. What this does give an auditor is the
        actor the grant is attributable to.
        """
        return self._issued_by.get(capability_id)

    def revoked_ids(self) -> tuple:
        return tuple(sorted(self._revoked))

    # ---- writes --------------------------------------------------------
    def anoint(self, *, actor: str) -> str:
        """Record ``actor`` as this log's root issuer. Once, ever.

        Idempotent for the same actor so a bootstrap that runs twice is not a
        failure; a DIFFERENT actor is refused, because a log with two roots
        has no root.
        """
        if self._root is not None:
            if self._root != actor:
                raise NotTheIssuer(
                    f"{actor!r} cannot become root issuer: {self._root!r} "
                    "already is, and minting authority is not shared")
            return self._root
        ev = self.log.append(actor=actor, action=ACT_ROOT, target=actor,
                             payload={"issuer": actor})
        self.apply(ev)
        return actor

    def issue(self, cap: Capability, *, actor: str) -> Capability:
        """Record a grant. It does not exist until this returns."""
        if not isinstance(cap, Capability):
            raise CapabilityError(f"expected a Capability, got {cap!r}")
        if cap.capability_id in self._issued:
            raise CapabilityError(
                f"capability {cap.capability_id!r} already exists; reusing an "
                "id would make two grants indistinguishable in the log")
        if self._root is None:
            # TRUST ON FIRST USE, WRITTEN DOWN. The first mint establishes
            # the root, as its own event, before the grant. Doing it here
            # rather than requiring a separate bootstrap call is what keeps
            # the rule enforceable: a caller cannot forget to establish a
            # root and leave the log in the state where anyone may mint.
            self.anoint(actor=actor)
        # Stamped, not accepted: the position a grant starts at is the log's
        # to decide, and a caller that could choose it could backdate one.
        cap = replace(cap, issued_seq=self.log.verify().head_seq + 1)
        ev = self.log.append(actor=actor, action=ACT_ISSUE,
                             target=cap.task_id,
                             payload={"task_id": cap.task_id, **cap.body()})
        self.apply(ev)
        return self._issued[cap.capability_id]

    def delegate(self, cap: Capability, *, parent_id: str,
                 actor: str) -> Capability:
        """Grant somebody a strictly weaker version of a grant you hold.

        ``actor`` must be the parent's subject. That is the whole point: a
        delegation is the holder passing on part of what it has, and an actor
        delegating a grant belonging to somebody else is minting with a chain
        for cover.

        The attenuation rules live on :meth:`Capability.attenuation_of` and
        are checked twice -- here, so the caller gets a useful error, and in
        :meth:`_authorize_mint`, so a record written by anything else is
        refused by every reader that folds the log.
        """
        if not isinstance(cap, Capability):
            raise CapabilityError(f"expected a Capability, got {cap!r}")
        parent = self._issued.get(parent_id)
        if parent is None:
            raise BadDelegation(
                f"no capability {parent_id!r} to delegate from")
        if parent_id in self._revoked:
            raise CapabilityRevoked(
                f"capability {parent_id!r} was revoked; a revoked grant "
                "cannot be the source of a new one")
        if actor != parent.subject:
            raise NotTheIssuer(
                f"{actor!r} cannot delegate {parent_id!r}, which was granted "
                f"to {parent.subject!r}")
        # Chain length of the CHILD, counted the same way check() walks it:
        # one link per parent_id followed. A root-issued grant is length 0.
        depth, walk = 1, parent
        while walk.parent_id:
            depth += 1
            walk = self._issued[walk.parent_id]
        if depth > MAX_DELEGATION_DEPTH:
            raise BadDelegation(
                f"delegating {parent_id!r} would make a chain {depth} deep, "
                f"past the {MAX_DELEGATION_DEPTH} bound. The bound exists so "
                "one delegate cannot make every check expensive for everyone")
        cap = replace(cap, parent_id=parent_id,
                      issued_seq=self.log.verify().head_seq + 1)
        why = cap.attenuation_of(parent)
        if why:
            raise BadDelegation(why)
        return self.issue(cap, actor=actor)

    def revoke(self, capability_id: str, *, actor: str,
               reason: str) -> None:
        """Withdraw a grant. Takes effect for every later check."""
        if capability_id not in self._issued:
            raise CapabilityError(
                f"no capability {capability_id!r} to revoke")
        ev = self.log.append(
            actor=actor, action=ACT_REVOKE, target=capability_id,
            payload={"capability_id": capability_id, "reason": reason})
        self.apply(ev)
