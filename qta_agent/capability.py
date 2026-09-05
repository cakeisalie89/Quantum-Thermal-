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

EXPIRY IS IN SEQUENCE NUMBERS, NOT WALL TIME

The log is ordered by ``seq`` and wall clocks move backwards. A capability that
expired "at 10:03" is a capability whose validity depends on whose clock you
ask; one that expires after seq 41 has the same answer for every reader of the
same log. Wall time is recorded for humans and never used for a decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
        expiry, then subject, then task, then action, then tool, then paths.
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
        return cap


def issue(*, capability_id: str, subject: str, action: Action, task_id: str,
          scope, issued_seq: int, tool_id: str = "",
          expires_after_seq: int = NEVER_EXPIRES,
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
    return Capability(
        capability_id=capability_id, subject=subject, action=action,
        task_id=task_id, tool_id=tool_id, scope=_normalise_scope(scope),
        issued_seq=issued_seq, expires_after_seq=expires_after_seq,
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
        self._revoked: set = set()
        self._at_seq = -1

    # ---- projection ----------------------------------------------------
    def load(self) -> "CapabilityLedger":
        self.log.verify().raise_if_bad()
        self._issued = {}
        self._revoked = set()
        self._at_seq = -1
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        """Fold one event in. True when it was a capability event."""
        if ev.action == ACT_ISSUE:
            cap = capability_from_record(ev.payload)
            existing = self._issued.get(cap.capability_id)
            if existing is not None and existing.digest() != cap.digest():
                raise CapabilityError(
                    f"seq {ev.seq}: capability {cap.capability_id!r} was "
                    "issued twice with different terms; two grants sharing "
                    "an id cannot be told apart by anything that cites one")
            self._issued[cap.capability_id] = cap
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

    # ---- reads ---------------------------------------------------------
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

    def revoked_ids(self) -> tuple:
        return tuple(sorted(self._revoked))

    # ---- writes --------------------------------------------------------
    def issue(self, cap: Capability, *, actor: str) -> Capability:
        """Record a grant. It does not exist until this returns."""
        if not isinstance(cap, Capability):
            raise CapabilityError(f"expected a Capability, got {cap!r}")
        if cap.capability_id in self._issued:
            raise CapabilityError(
                f"capability {cap.capability_id!r} already exists; reusing an "
                "id would make two grants indistinguishable in the log")
        ev = self.log.append(actor=actor, action=ACT_ISSUE,
                             target=cap.task_id,
                             payload={"task_id": cap.task_id, **cap.body()})
        self.apply(ev)
        return self._issued[cap.capability_id]

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
