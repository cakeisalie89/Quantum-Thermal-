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
    # agent_substrate.store -- the authority record state machine
    "record.create": "store",
    "record.transition": "store",
    "record.depend": "store",
    #: The digest of a projection snapshot, at the log position it describes.
    #: Written so a checkpoint's claim about state lives INSIDE the hash
    #: chain rather than only in a file anyone can rewrite. See D-2026-30.
    "checkpoint.state": "store",
    # agent_substrate.governed_tasks -- the durable task lifecycle
    "task.create": "governed_tasks",
    "task.transition": "governed_tasks",
    "task.execution": "governed_tasks",
    "task.evidence": "governed_tasks",
    "task.compensation": "governed_tasks",
    "task.separate_verification": "governed_tasks",
    "task.reexecution": "governed_tasks",
    # agent_substrate.capability -- the ledger that makes a grant real
    "capability.issue": "capability",
    "capability.revoke": "capability",
    "capability.root": "capability",
    # agent_substrate.policy
    "policy.publish": "policy",
    "policy.decision": "policy",
    # D-2026-148: a policy's owner withdraws it, and from then on it
    # authorizes nothing
    "policy.withdraw": "policy",
    # agent_substrate.scheduler
    "scheduler.enqueue": "scheduler",
    "scheduler.transition": "scheduler",
    "scheduler.priority": "scheduler",
    "scheduler.lease_renew": "scheduler",
    # agent_substrate.memory
    "memory.write": "memory",
    "memory.status": "memory",
    # agent_substrate.agents
    "agent.register": "agents",
    "agent.retire": "agents",
    "agent.message": "agents",
    "agent.claim": "agents",
    "agent.escalation": "agents",
    "agent.escalation.answer": "agents",
    # agent_substrate.netauth
    "network.grant": "netauth",
    "network.service": "netauth",
    "network.request": "netauth",
    "network.result": "netauth",
    # agent_substrate.secrets
    "secret.grant": "secrets",
    "secret.access": "secrets",
    # Missing until follow-up A: a provisioning record made every reducer
    # on the log refuse the whole history as UNKNOWN (D-2026-85).
    "secret.provision": "secrets",
    # agent_substrate.context
    "context.build": "context",
    # agent_substrate.readpath -- every governed read, permitted or refused
    "file.read": "readpath",
    # agent_substrate.idempotency -- durable, owner-scoped request identity
    "idempotency.bind": "idempotency",
    # agent_substrate.proposals -- a proposal received at the ingress: input,
    # NON_AUTHORITATIVE, recorded before anything runs
    "proposal.receive": "proposals",
    # agent_substrate.events -- the security profile a history is written
    # under, declared by its first event and enforced by the read primitives
    # themselves; every reducer meets it at seq 0 and passes it by (D-2026-92)
    "history.security_profile": "events",
    # agent_substrate.store -- D-2026-148: promotions on this history name a
    # policy published on it by an authority it names, and in force
    "authority.policy_binding": "store",
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
