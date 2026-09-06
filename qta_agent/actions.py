"""Every action name this package writes, and which subsystem owns it.

WHY A REGISTRY RATHER THAN EACH REDUCER GUESSING

The design premise is that ONE hash-chained log is the authority history.
Several reducers project it: the authority store reads ``record.*``, the
scheduler reads ``scheduler.*``, the policy store reads ``policy.*``, and so
on. Each of them meets every other subsystem's events on the way past.

Two behaviours were in tension, and both are right:

  * an event a reducer does not understand must NOT be silently skipped --
    that is how a future writer adds an authority-relevant event that older
    readers quietly drop, and the state they reconstruct is then confidently
    wrong;
  * an event belonging to another subsystem is not this reducer's business,
    and refusing it makes a shared log impossible -- which is what actually
    happened: the authority store raised on ``policy.publish`` and no two
    subsystems could use one log.

The distinction the reducers were missing is between FOREIGN and UNKNOWN. A
foreign action is one this package writes and another reducer owns: skip it.
An unknown action is one nothing here writes: refuse, because it is either a
newer schema this build cannot interpret or something that has no business in
the log.

ADDING AN ACTION IS AN EDIT TO THIS FILE

Deliberately. A new durable action is a new thing the system can be asked to
remember, and it should be visible in review rather than appearing as a
string somewhere.
"""
from __future__ import annotations

from typing import FrozenSet

#: action -> the module that owns and applies it.
OWNERS: dict = {
    # qta_agent.store -- the authority record state machine
    "record.create": "store",
    "record.transition": "store",
    "record.depend": "store",
    # qta_agent.governed_stage10 -- the durable task lifecycle
    "task.create": "governed_stage10",
    "task.transition": "governed_stage10",
    "task.execution": "governed_stage10",
    "task.evidence": "governed_stage10",
    # qta_agent.capability -- the ledger that makes a grant real
    "capability.issue": "capability",
    "capability.revoke": "capability",
    # qta_agent.policy
    "policy.publish": "policy",
    "policy.decision": "policy",
    # qta_agent.scheduler
    "scheduler.enqueue": "scheduler",
    "scheduler.transition": "scheduler",
    "scheduler.priority": "scheduler",
    # qta_agent.memory
    "memory.write": "memory",
    "memory.status": "memory",
    # qta_agent.agents
    "agent.register": "agents",
    "agent.retire": "agents",
    "agent.message": "agents",
    "agent.claim": "agents",
    "agent.escalation": "agents",
    "agent.escalation.answer": "agents",
    # qta_agent.netauth
    "network.grant": "netauth",
    "network.request": "netauth",
    "network.result": "netauth",
    # qta_agent.secrets
    "secret.grant": "secrets",
    "secret.access": "secrets",
    # qta_agent.context
    "context.build": "context",
    # qta_agent.readpath -- every governed read, permitted or refused
    "file.read": "readpath",
    # qta_agent.idempotency -- durable, owner-scoped request identity
    "idempotency.bind": "idempotency",
}

KNOWN: FrozenSet[str] = frozenset(OWNERS)

#: What a reducer should do with an action.
MINE = "MINE"
FOREIGN = "FOREIGN"
UNKNOWN = "UNKNOWN"


class UnknownAction(Exception):
    """An action no module in this package writes. Always fail closed."""


def owner(action: str) -> str | None:
    """Which module owns ``action``, or None if nothing here writes it."""
    return OWNERS.get(action)


def classify(action: str, *, mine) -> str:
    """MINE, FOREIGN or UNKNOWN, from one reducer's point of view.

    ``mine`` is the set of actions this reducer applies. An action in it is
    MINE; one owned by another module here is FOREIGN and may be skipped; and
    anything else is UNKNOWN, which is the case that must never be skipped.
    """
    if action in mine:
        return MINE
    if action in OWNERS:
        return FOREIGN
    return UNKNOWN


def require_known(action: str, *, mine, where: str = "") -> str:
    """Classify, raising :class:`UnknownAction` for anything unrecognised."""
    kind = classify(action, mine=mine)
    if kind is UNKNOWN or kind == UNKNOWN:
        prefix = f"{where}: " if where else ""
        raise UnknownAction(
            f"{prefix}unknown action {action!r}. No module in this package "
            "writes it, so it is either a record from a newer schema this "
            "build cannot interpret or something that does not belong in the "
            "log. Refusing to project a history with a hole in it: an "
            "authority-relevant event that older readers quietly drop makes "
            "the state they reconstruct confidently wrong.")
    return kind
