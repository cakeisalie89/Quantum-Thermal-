"""Several agents, and the separations that make more than one of them useful.

WHY TWO LABELS ON ONE CALL IS NOT TWO AGENTS

"Proposer" and "verifier" spelled as two strings passed to the same
uncontrolled function is one agent with a naming convention. The separation
only means something if the second party could have said no, and could have
said no for a reason the first party did not choose.

This module makes the parties addressable and the boundaries checkable:

  * every participating instance is REGISTERED, with the roles it may take;
  * an action names the instance that took it, and an instance may not act
    under another's identity;
  * messages between them are identified, attributed, ordered and
    append-only, with duplicate and stale delivery handled rather than
    assumed away;
  * a disagreement is a first-class outcome with a declared resolution rule,
    never "whichever wrote last".

HUMAN AUTHORITY IS A DIFFERENT KIND, NOT A STRONGER ROLE

Some decisions are not the agent's to make. Those are raised as an
:class:`Escalation`, and an escalation can only be answered by a principal
registered as :attr:`PrincipalKind.HUMAN`. No agent may answer one -- not a
more privileged agent, not one holding the REVIEWER role, not the one that
raised it.

WHAT THIS MODULE CANNOT DO, SAID PLAINLY

It cannot authenticate a human. ``PrincipalKind.HUMAN`` is a property of a
registration record, and the registration was made by somebody. What it gives
you is that the record exists, says who, is in the hash-chained log, and
cannot be created by an agent instance: only an already-registered human
principal, or the explicit out-of-band bootstrap, can register another human.
An installation that lets an agent perform the bootstrap has given the agent
human authority, and nothing in this file can prevent that -- it can only make
the moment it happened findable afterwards.

CONSEQUENTLY, THIS REPOSITORY HAS NO HUMAN DECISIONS

Nothing here fabricates one. An escalation raised in this repository stays
OPEN, and any workflow that depends on an answer stays blocked. That is the
correct behaviour for a mechanism whose input does not exist yet, and it is
why the escalation states are visible rather than implicit.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import FrozenSet

from .canonical import digest, is_digest

ACT_AGENT_REGISTER = "agent.register"
ACT_AGENT_RETIRE = "agent.retire"
ACT_MESSAGE = "agent.message"
ACT_CLAIM = "agent.claim"
class Notifier:
    """Where an escalation goes once it is durable. A contract, not a queue.

    WHY THIS IS AN INTERFACE AND NOT AN IMPLEMENTATION

    "Deliver to a person" is not a thing this repository can do. There is no
    address book, no mail transport, no pager, and inventing one would be a
    channel that looks like delivery and reaches nobody -- which is worse
    than the query-only state it replaced, because at least that state was
    visible as a gap.

    What CAN be done is to make delivery a named, replaceable seam with a
    stated contract, and to ship the sinks that are honest here: one that
    writes a line an operator or a CI job can actually see
    (:class:`StreamNotifier`), and one that records what it was handed so a
    test can assert delivery happened (:class:`RecordingNotifier`).

    THE CONTRACT

    * called AFTER the escalation is durable in the log, never before;
    * may raise: the caller survives it and records the failure. A sink that
      fails must not cost the escalation, because losing the record to save
      the notification is backwards;
    * must not write to the log, mutate the escalation, or answer it. A
      delivery channel that can answer is a channel that can decide, and an
      escalation exists precisely because the decision was not the software's
      to make.
    """

    def escalation_raised(self, escalation) -> None:
        raise NotImplementedError


class StreamNotifier(Notifier):
    """Write one line per escalation to a stream. The honest minimum.

    stderr by default, because that is where an operator running the
    workflow is already looking and where a CI job's log will keep it.
    """

    def __init__(self, stream=None):
        self._stream = stream

    def escalation_raised(self, escalation) -> None:
        import sys
        stream = self._stream if self._stream is not None else sys.stderr
        stream.write(
            f"ESCALATION {escalation.escalation_id} on task "
            f"{escalation.task_id}: {escalation.question} "
            f"[{'/'.join(escalation.options)}] raised by "
            f"{escalation.raised_by}\n")
        stream.flush()


class RecordingNotifier(Notifier):
    """Keep what it was handed, in order. For tests and for assertions."""

    def __init__(self):
        self.delivered: list = []

    def escalation_raised(self, escalation) -> None:
        self.delivered.append(escalation)


ACT_ESCALATION = "agent.escalation"
ACT_ESCALATION_ANSWER = "agent.escalation.answer"

#: Message bodies are carried by digest, not inline. A bus that stores bodies
#: is a second copy of everything anyone ever said, under the log's retention.
MAX_MESSAGE_SUBJECT = 200


class AgentError(Exception):
    """Base class. Every failure here is fail-closed."""


class IdentityError(AgentError):
    """The acting instance is not who it says, or may not take this role."""


class MessageError(AgentError):
    """The message cannot be accepted."""


class ConflictError(AgentError):
    """Two agents disagree and no declared rule resolves it."""


class EscalationError(AgentError):
    """The escalation cannot be raised or answered as asked."""


class PrincipalKind(str, Enum):
    """WHAT is acting. Distinct from which role it is taking."""

    AGENT = "AGENT"
    #: A person. Only a HUMAN may answer an escalation.
    HUMAN = "HUMAN"
    #: Automatic transitions with no author: expiry, invalidation.
    SYSTEM = "SYSTEM"


class AgentRole(str, Enum):
    """What an instance may do. An instance may hold several; each is named."""

    PROPOSER = "PROPOSER"
    EXECUTOR = "EXECUTOR"
    VERIFIER = "VERIFIER"
    AUDITOR = "AUDITOR"
    #: Judges a disagreement or answers an escalation. Held by humans here.
    REVIEWER = "REVIEWER"
    SCHEDULER = "SCHEDULER"


#: Roles that must not be held by the same instance for one task. Each pair is
#: a separation somebody could otherwise defeat by holding both.
INCOMPATIBLE: tuple = (
    (AgentRole.EXECUTOR, AgentRole.VERIFIER),
    (AgentRole.PROPOSER, AgentRole.VERIFIER),
    (AgentRole.PROPOSER, AgentRole.REVIEWER),
    (AgentRole.EXECUTOR, AgentRole.REVIEWER),
)


@dataclass(frozen=True)
class AgentIdentity:
    """One participating instance. Identity is the pair, not the name.

    ``agent_id`` names the software; ``instance_id`` names this run of it. Two
    runs of the same agent are two parties for the purpose of ordering and
    attribution, and one party for the purpose of separation of duties -- an
    agent that restarts has not become somebody else.
    """

    agent_id: str
    instance_id: str
    kind: PrincipalKind
    roles: FrozenSet[AgentRole]
    registered_seq: int = -1
    retired_seq: int = -1
    #: Who registered this identity. For a HUMAN, this is the chain of trust.
    registered_by: str = ""

    def body(self) -> dict:
        return {"agent_id": self.agent_id, "instance_id": self.instance_id,
                "kind": self.kind.value,
                "roles": sorted(r.value for r in self.roles),
                "registered_by": self.registered_by}

    def digest(self) -> str:
        return digest(self.body())

    def to_record(self) -> dict:
        rec = self.body()
        rec.update({"registered_seq": self.registered_seq,
                    "retired_seq": self.retired_seq})
        return rec

    def is_active(self, at_seq: int) -> bool:
        """Active STRICTLY BEFORE the retirement event.

        Retirement is not a lease. A lease says "valid through seq N" because
        its holder was promised that much time; a retirement says "as of seq
        N this party has left", and the event that records it is already
        after they have gone. Written with ``<=`` first, which let a retired
        instance act once more -- exactly once, at the moment it mattered.
        """
        return self.retired_seq < 0 or at_seq < self.retired_seq

    def may(self, role: AgentRole) -> bool:
        return role in self.roles


def identity(*, agent_id: str, instance_id: str, kind: PrincipalKind, roles,
             registered_by: str = "") -> AgentIdentity:
    """Construct an identity, refusing role combinations that defeat a
    separation."""
    for name, value in (("agent_id", agent_id), ("instance_id", instance_id)):
        if not isinstance(value, str) or not value:
            raise IdentityError(f"{name} must be a non-empty str")
    if not isinstance(kind, PrincipalKind):
        raise IdentityError(f"kind must be a PrincipalKind, got {kind!r}")
    role_set = frozenset(roles)
    if not role_set:
        raise IdentityError(
            "an identity with no roles can do nothing and is refused rather "
            "than registered; an empty role set is a mistake, not a policy")
    for r in role_set:
        if not isinstance(r, AgentRole):
            raise IdentityError(f"role must be an AgentRole, got {r!r}")
    for a, b in INCOMPATIBLE:
        if a in role_set and b in role_set:
            raise IdentityError(
                f"{instance_id!r} would hold both {a.value} and {b.value}. "
                "Holding both defeats the separation they exist to create: "
                "the second party could not have said no for a reason the "
                "first did not choose.")
    return AgentIdentity(agent_id=agent_id, instance_id=instance_id,
                         kind=kind, roles=role_set,
                         registered_by=registered_by)


def identity_from_record(rec: dict) -> AgentIdentity:
    if not isinstance(rec, dict):
        raise IdentityError(f"identity record is {type(rec).__name__}")
    known = {"agent_id", "instance_id", "kind", "roles", "registered_by",
             "registered_seq", "retired_seq"}
    unknown = set(rec) - known
    if unknown:
        raise IdentityError(
            f"identity record carries unknown fields {sorted(unknown)}; "
            "refusing to project an identity this version does not fully "
            "understand")
    try:
        return identity(
            agent_id=rec["agent_id"], instance_id=rec["instance_id"],
            kind=PrincipalKind(rec["kind"]),
            roles={AgentRole(r) for r in rec["roles"]},
            registered_by=rec.get("registered_by", ""))
    except (KeyError, ValueError) as exc:
        raise IdentityError(f"identity record is invalid: {exc}") from exc


@dataclass(frozen=True)
class Message:
    """One durable communication between instances."""

    message_id: str
    sender_instance: str
    recipient_agent: str
    task_id: str
    subject: str
    #: The body, by digest. Never inline -- see MAX_MESSAGE_SUBJECT.
    body_digest: str
    sent_seq: int = -1
    in_reply_to: str | None = None
    delivered_to: tuple = ()

    def to_record(self) -> dict:
        return {"message_id": self.message_id,
                "sender_instance": self.sender_instance,
                "recipient_agent": self.recipient_agent,
                "task_id": self.task_id, "subject": self.subject,
                "body_digest": self.body_digest, "sent_seq": self.sent_seq,
                "in_reply_to": self.in_reply_to,
                "delivered_to": list(self.delivered_to)}


def message_from_record(rec: dict) -> Message:
    """Rebuild a message from a log payload, validating its shape.

    A record carries JSON types: lists rather than tuples, strings rather
    than enums. ``Message(**rec)`` looks like it works and produces an object
    whose fields have the wrong types, which then fails somewhere else --
    which is how the escalation state ended up being a ``str`` that was asked
    for its ``.value``.
    """
    if not isinstance(rec, dict):
        raise MessageError(f"message record is {type(rec).__name__}")
    known = set(Message.__dataclass_fields__)
    unknown = set(rec) - known
    if unknown:
        raise MessageError(
            f"message record carries unknown fields {sorted(unknown)}; "
            "refusing to project a message this version does not fully "
            "understand")
    try:
        return Message(
            message_id=rec["message_id"],
            sender_instance=rec["sender_instance"],
            recipient_agent=rec["recipient_agent"], task_id=rec["task_id"],
            subject=rec["subject"], body_digest=rec["body_digest"],
            sent_seq=rec.get("sent_seq", -1),
            in_reply_to=rec.get("in_reply_to"),
            delivered_to=tuple(rec.get("delivered_to", ())))
    except (KeyError, TypeError) as exc:
        raise MessageError(f"message record is malformed: {exc}") from exc


class ConflictRule(str, Enum):
    """How a disagreement is resolved. Declared per subject; never defaulted.

    There is deliberately no LAST_WRITER_WINS. It is the rule people reach for
    and it makes the outcome depend on scheduling, which is the one input
    nobody chose.
    """

    #: A human must decide. The default for anything that matters.
    REQUIRE_HUMAN = "REQUIRE_HUMAN"
    #: Identical claims from N distinct instances carry it.
    REQUIRE_QUORUM = "REQUIRE_QUORUM"
    #: A named role's claim wins over other roles'.
    PREFER_ROLE = "PREFER_ROLE"


@dataclass(frozen=True)
class Claim:
    """One instance's assertion about one subject, by content digest."""

    claim_id: str
    task_id: str
    subject: str
    value_digest: str
    by_instance: str
    role: AgentRole
    claimed_seq: int = -1

    def to_record(self) -> dict:
        return {"claim_id": self.claim_id, "task_id": self.task_id,
                "subject": self.subject, "value_digest": self.value_digest,
                "by_instance": self.by_instance, "role": self.role.value,
                "claimed_seq": self.claimed_seq}


@dataclass(frozen=True)
class Resolution:
    """The outcome of comparing claims. Always explicit about which rule."""

    resolved: bool
    subject: str
    rule: ConflictRule | None
    value_digest: str | None
    reason: str
    claims: tuple = ()

    def to_record(self) -> dict:
        return {"resolved": self.resolved, "subject": self.subject,
                "rule": self.rule.value if self.rule else None,
                "value_digest": self.value_digest, "reason": self.reason,
                "claims": [c.claim_id for c in self.claims]}


class EscalationState(str, Enum):
    """Where a question for a human is."""

    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    #: The asker no longer needs it.
    WITHDRAWN = "WITHDRAWN"


@dataclass(frozen=True)
class Escalation:
    """A decision that is not the agent's to make."""

    escalation_id: str
    task_id: str
    question: str
    raised_by: str
    state: EscalationState = EscalationState.OPEN
    #: Options offered. A free-text answer is refused: an escalation whose
    #: answer cannot be checked against what was asked is a conversation.
    options: tuple = ()
    answer: str | None = None
    answered_by: str | None = None
    answer_reason: str = ""
    raised_seq: int = -1
    answered_seq: int = -1

    def to_record(self) -> dict:
        return {"escalation_id": self.escalation_id, "task_id": self.task_id,
                "question": self.question, "raised_by": self.raised_by,
                "state": self.state.value, "options": list(self.options),
                "answer": self.answer, "answered_by": self.answered_by,
                "answer_reason": self.answer_reason,
                "raised_seq": self.raised_seq,
                "answered_seq": self.answered_seq}


def escalation_from_record(rec: dict) -> Escalation:
    """Rebuild an escalation from a log payload, validating its shape."""
    if not isinstance(rec, dict):
        raise EscalationError(f"escalation record is {type(rec).__name__}")
    known = set(Escalation.__dataclass_fields__)
    unknown = set(rec) - known
    if unknown:
        raise EscalationError(
            f"escalation record carries unknown fields {sorted(unknown)}; "
            "refusing to project an escalation this version does not fully "
            "understand")
    try:
        return Escalation(
            escalation_id=rec["escalation_id"], task_id=rec["task_id"],
            question=rec["question"], raised_by=rec["raised_by"],
            state=EscalationState(rec.get("state", "OPEN")),
            options=tuple(rec.get("options", ())),
            answer=rec.get("answer"), answered_by=rec.get("answered_by"),
            answer_reason=rec.get("answer_reason", ""),
            raised_seq=rec.get("raised_seq", -1),
            answered_seq=rec.get("answered_seq", -1))
    except (KeyError, TypeError, ValueError) as exc:
        raise EscalationError(
            f"escalation record is malformed: {exc}") from exc


class AgentDirectory:
    """Who is participating, what they may do, and everything they said.

    A projection of the log, like everything else here: an instance that is
    not in the log did not participate, whatever it claims about itself.
    """

    def __init__(self, log, *, notifier=None):
        self.log = log
        self._identities: dict = {}
        self._messages: dict = {}
        self._claims: dict = {}
        self._escalations: dict = {}
        #: Where a raised escalation is DELIVERED, or None to file only.
        #: See :class:`Notifier` for what a sink must promise.
        self.notifier = notifier
        #: ``(escalation_id, error)`` for notifications the sink refused.
        self._undelivered: list = []
        self._at_seq = -1

    # ---- projection ----------------------------------------------------
    def load(self) -> "AgentDirectory":
        self.log.verify().raise_if_bad()
        self._identities = {}
        self._messages = {}
        self._claims = {}
        self._escalations = {}
        self._at_seq = -1
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        p = ev.payload
        if ev.action == ACT_AGENT_REGISTER:
            ident = identity_from_record(p["identity"])
            if ident.instance_id in self._identities:
                raise IdentityError(
                    f"seq {ev.seq}: instance {ident.instance_id!r} registered "
                    "twice; a second registration could quietly widen the "
                    "roles the first one was given")
            # THE REGISTRAR IS THE EVENT'S ACTOR, AND THE HUMAN RULE IS
            # ENFORCED HERE TOO.
            #
            # register() refuses to let a non-human register a HUMAN, and
            # says why: "one step from answering its own escalations". That
            # refusal lived on the write path alone, so it was advice. A
            # hostile agent appended the identical record, replay accepted a
            # HUMAN principal with registered_by='' -- and then answered its
            # own escalation through the invented confederate, satisfying
            # both halves of the human gate it was supposed to be stopped by.
            #
            # registered_by comes from ev.actor rather than the payload for
            # the same reason the executor comes from the execution record:
            # a field the registrant writes cannot establish who admitted it.
            self._check_may_register(ident, by=ev.actor, seq=ev.seq)
            claimed_by = p["identity"].get("registered_by", "")
            if claimed_by and claimed_by != ev.actor:
                raise IdentityError(
                    f"seq {ev.seq}: the record says {ident.instance_id!r} was "
                    f"registered by {claimed_by!r}, but it was appended by "
                    f"{ev.actor!r}. Who admitted a principal is the log's to "
                    "say, not the record's.")
            self._identities[ident.instance_id] = replace(
                ident, registered_by=ev.actor, registered_seq=ev.seq)
        elif ev.action == ACT_AGENT_RETIRE:
            iid = p["instance_id"]
            if iid not in self._identities:
                # A raw KeyError here leaks the projection's internals and
                # skips the domain error every other refusal in this module
                # raises. An authority API fails on purpose or not at all.
                raise IdentityError(
                    f"seq {ev.seq}: retirement names {iid!r}, which was "
                    "never registered; a record about a principal that does "
                    "not exist is not a fact about this system")
            self._identities[iid] = replace(self._identities[iid],
                                            retired_seq=ev.seq)
        elif ev.action == ACT_MESSAGE:
            msg = message_from_record(p["message"])
            if msg.message_id in self._messages:
                # Duplicate delivery is expected and is a no-op, not an
                # error: a retrying sender must not be able to change what it
                # said by saying it again.
                first = self._messages[msg.message_id]
                if first.body_digest != msg.body_digest:
                    raise MessageError(
                        f"seq {ev.seq}: message {msg.message_id!r} was "
                        "already sent with a different body; a redelivery "
                        "that changes the content is a rewrite")
                return True
            self._messages[msg.message_id] = replace(msg, sent_seq=ev.seq)
        elif ev.action == ACT_CLAIM:
            # WHO MADE THE CLAIM IS THE ACTOR, AND THE ROLE IS CHECKED.
            #
            # claim() calls require(by_instance, role) -- registered, active,
            # and actually holding the role. The replay did neither and took
            # both fields from the payload, so a forged claim could be
            # attributed to any instance in any role. Claims are what conflict
            # detection compares, so an attributable-to-anyone claim is a way
            # to manufacture or suppress a disagreement between two
            # "independent" parties.
            claimed_by = p["by_instance"]
            if claimed_by != ev.actor:
                raise ConflictError(
                    f"seq {ev.seq}: claim {p['claim_id']!r} says it was made "
                    f"by {claimed_by!r} but was appended by {ev.actor!r}; a "
                    "claim is attributed to the instance that recorded it")
            role = AgentRole(p["role"])
            self.require(ev.actor, role, at_seq=ev.seq)
            claim = Claim(claim_id=p["claim_id"], task_id=p["task_id"],
                          subject=p["subject"],
                          value_digest=p["value_digest"],
                          by_instance=ev.actor,
                          role=role, claimed_seq=ev.seq)
            if claim.claim_id in self._claims:
                raise ConflictError(
                    f"seq {ev.seq}: claim {claim.claim_id!r} recorded twice")
            self._claims[claim.claim_id] = claim
        elif ev.action == ACT_ESCALATION:
            esc = escalation_from_record(p["escalation"])
            if esc.escalation_id in self._escalations:
                raise EscalationError(
                    f"seq {ev.seq}: escalation {esc.escalation_id!r} raised "
                    "twice")
            self._escalations[esc.escalation_id] = replace(
                esc, raised_seq=ev.seq)
        elif ev.action == ACT_ESCALATION_ANSWER:
            eid = p["escalation_id"]
            # RE-AUTHORIZE ON REPLAY. This used to assign the record's own
            # fields, so one appended line let an AGENT answer its own
            # escalation: the HUMAN-principal check and the not-the-asker
            # check both live on the write path, and a reducer that trusts
            # the payload makes both advisory.
            #
            # "No arrangement of roles substitutes for a person" is the
            # strongest claim this module makes. It cannot rest on a field
            # the answering party writes.
            cur = self._escalations.get(eid)
            if cur is None:
                raise EscalationError(
                    f"seq {ev.seq}: answer for escalation {eid!r}, which "
                    "this history never opened")
            if cur.state is not EscalationState.OPEN:
                raise EscalationError(
                    f"seq {ev.seq}: escalation {eid!r} is "
                    f"{cur.state.value}; answering it again would rewrite a "
                    "decision that was already recorded")
            dst = EscalationState(p["state"])
            if dst is EscalationState.WITHDRAWN:
                # A withdrawal is a different act from an answer: the ASKER
                # says they no longer need the decision, and no human is
                # claimed to have made one. Only the asker may do it.
                if ev.actor != cur.raised_by:
                    raise EscalationError(
                        f"seq {ev.seq}: escalation {eid!r} was raised by "
                        f"{cur.raised_by!r}; {ev.actor!r} may not withdraw "
                        "it")
                self._escalations[eid] = replace(
                    cur, state=dst, answer_reason=p.get("reason", ""),
                    answered_seq=ev.seq)
                self._at_seq = ev.seq
                return True
            answered_by = p.get("answered_by")
            ident = self._identities.get(answered_by)
            if ident is None:
                raise EscalationError(
                    f"seq {ev.seq}: {answered_by!r} answered escalation "
                    f"{eid!r} and is not a registered principal")
            if ident.kind is not PrincipalKind.HUMAN:
                raise EscalationError(
                    f"seq {ev.seq}: {answered_by!r} is a "
                    f"{ident.kind.value} principal and may not answer an "
                    "escalation. An escalation exists because the decision "
                    "was not the agent's to make; holding a role does not "
                    "change what kind of thing is deciding.")
            if answered_by == cur.raised_by:
                raise EscalationError(
                    f"seq {ev.seq}: {answered_by!r} raised escalation "
                    f"{eid!r} and may not also answer it")
            if p.get("answer") not in cur.options:
                raise EscalationError(
                    f"seq {ev.seq}: answer {p.get('answer')!r} is not one of "
                    f"{list(cur.options)}; an answer that cannot be checked "
                    "against what was asked is a note, not a decision")
            self._escalations[eid] = replace(
                cur, state=EscalationState(p["state"]),
                answer=p.get("answer"), answered_by=answered_by,
                answer_reason=p.get("reason", ""), answered_seq=ev.seq)
        else:
            return False
        self._at_seq = ev.seq
        return True

    # ---- identity ------------------------------------------------------
    def _check_may_register(self, ident: AgentIdentity, *, by: str,
                            seq: int | None = None) -> None:
        """May ``by`` admit ``ident``? Or raise.

        ONE function, called by both the write path and the replay. They had
        this rule in only one of the two places, and the gap was exploitable
        end to end -- so the rule lives here and neither caller carries a
        copy that could drift.

        Registering a HUMAN principal requires an already-registered human,
        or the explicit bootstrap sentinel. An agent that could register a
        human could grant itself human authority in one step, which would
        make every escalation in the system answerable by the thing it was
        protecting against.
        """
        if ident.kind is not PrincipalKind.HUMAN:
            return
        if by == BOOTSTRAP:
            return
        registrar = self._identities.get(by)
        if registrar is None or registrar.kind is not PrincipalKind.HUMAN:
            where = f"seq {seq}: " if seq is not None else ""
            raise IdentityError(
                f"{where}{by!r} may not register a HUMAN principal: only an "
                "already-registered human, or the explicit "
                f"{BOOTSTRAP!r} bootstrap, can. An agent that could "
                "do this would be one step from answering its own "
                "escalations.")

    def register(self, ident: AgentIdentity, *, by: str) -> AgentIdentity:
        """Record a participating instance. See :meth:`_check_may_register`."""
        self._check_may_register(ident, by=by)
        ident = replace(ident, registered_by=by)
        ev = self.log.append(actor=by, action=ACT_AGENT_REGISTER,
                             target=ident.instance_id,
                             payload={"identity": ident.to_record()})
        self.apply(ev)
        return self.get(ident.instance_id)

    def retire(self, instance_id: str, *, by: str, reason: str) -> None:
        self.get(instance_id)
        ev = self.log.append(actor=by, action=ACT_AGENT_RETIRE,
                             target=instance_id,
                             payload={"instance_id": instance_id,
                                      "reason": reason})
        self.apply(ev)

    def get(self, instance_id: str) -> AgentIdentity:
        try:
            return self._identities[instance_id]
        except KeyError:
            raise IdentityError(
                f"instance {instance_id!r} is not registered; an instance "
                "that is not in the log did not participate, whatever it "
                "claims about itself") from None

    def require(self, instance_id: str, role: AgentRole, *,
                at_seq: int | None = None) -> AgentIdentity:
        """The gate. Raises unless this instance may take this role now."""
        ident = self.get(instance_id)
        seq = self._at_seq if at_seq is None else at_seq
        if not ident.is_active(seq):
            raise IdentityError(
                f"instance {instance_id!r} was retired after seq "
                f"{ident.retired_seq}; the log is at {seq}")
        if not ident.may(role):
            raise IdentityError(
                f"instance {instance_id!r} holds "
                f"{sorted(r.value for r in ident.roles)} and may not act as "
                f"{role.value}")
        return ident

    def instances(self) -> tuple:
        return tuple(sorted(self._identities.values(),
                            key=lambda i: i.instance_id))

    # ---- messages ------------------------------------------------------
    def send(self, *, message_id: str, sender_instance: str,
             recipient_agent: str, task_id: str, subject: str,
             body_digest: str, in_reply_to: str | None = None) -> Message:
        """Send a message. Duplicates are no-ops; rewrites are refused."""
        self.get(sender_instance)
        if not is_digest(body_digest):
            raise MessageError(
                "a message body is carried by digest; a bus that stored "
                "bodies would be a second copy of everything anyone said")
        if len(subject) > MAX_MESSAGE_SUBJECT:
            raise MessageError(
                f"subject is {len(subject)} chars, above "
                f"{MAX_MESSAGE_SUBJECT}")
        if in_reply_to is not None and in_reply_to not in self._messages:
            raise MessageError(
                f"message {message_id!r} replies to {in_reply_to!r}, which "
                "was never sent")
        existing = self._messages.get(message_id)
        if existing is not None:
            if existing.body_digest != body_digest:
                raise MessageError(
                    f"message {message_id!r} was already sent with a "
                    "different body; a resend that changes the content is a "
                    "rewrite, and the bus is append-only")
            return existing
        msg = Message(message_id=message_id, sender_instance=sender_instance,
                      recipient_agent=recipient_agent, task_id=task_id,
                      subject=subject, body_digest=body_digest,
                      in_reply_to=in_reply_to)
        ev = self.log.append(actor=sender_instance, action=ACT_MESSAGE,
                             target=recipient_agent,
                             payload={"message": msg.to_record()})
        self.apply(ev)
        return self._messages[message_id]

    def inbox(self, agent_id: str, *, task_id: str | None = None) -> tuple:
        """Messages for an agent, in log order. Ordering is the log's."""
        return tuple(sorted(
            (m for m in self._messages.values()
             if m.recipient_agent == agent_id
             and (task_id is None or m.task_id == task_id)),
            key=lambda m: m.sent_seq))

    def deliverable(self, agent_id: str, instance_id: str, *,
                    at_seq: int | None = None,
                    superseded_tasks: frozenset = frozenset()) -> tuple:
        """Messages this instance may still act on, and why the rest cannot.

        Returns ``(deliverable, refused)`` where ``refused`` is a tuple of
        ``(message, reason)``. Refusals are RETURNED rather than raised
        because a bus that throws on the first stale message stops delivering
        the good ones behind it.
        """
        seq = self._at_seq if at_seq is None else at_seq
        ident = self._identities.get(instance_id)
        ok: list = []
        refused: list = []
        for msg in self.inbox(agent_id):
            if ident is None:
                refused.append((msg, "recipient instance is not registered"))
            elif not ident.is_active(seq):
                refused.append((msg, (
                    f"recipient was retired after seq {ident.retired_seq}; "
                    "a message to a party that has left is not delivered to "
                    "whoever replaced them")))
            elif msg.task_id in superseded_tasks:
                refused.append((msg, (
                    f"task {msg.task_id!r} was superseded; acting on this "
                    "would apply a decision to work that no longer exists")))
            else:
                ok.append(msg)
        return tuple(ok), tuple(refused)

    # ---- claims and conflict -------------------------------------------
    def claim(self, *, claim_id: str, task_id: str, subject: str,
              value_digest: str, by_instance: str, role: AgentRole) -> Claim:
        self.require(by_instance, role)
        if not is_digest(value_digest):
            raise ConflictError(
                "a claim names its value by digest, so two claims can be "
                "compared without comparing prose")
        ev = self.log.append(
            actor=by_instance, action=ACT_CLAIM, target=task_id,
            payload={"claim_id": claim_id, "task_id": task_id,
                     "subject": subject, "value_digest": value_digest,
                     "by_instance": by_instance, "role": role.value})
        self.apply(ev)
        return self._claims[claim_id]

    def claims_about(self, task_id: str, subject: str) -> tuple:
        return tuple(sorted(
            (c for c in self._claims.values()
             if c.task_id == task_id and c.subject == subject),
            key=lambda c: (c.claimed_seq, c.claim_id)))

    def resolve(self, *, task_id: str, subject: str, rule: ConflictRule,
                quorum: int = 2,
                prefer: AgentRole | None = None) -> Resolution:
        """Compare claims under a DECLARED rule. Never last-writer-wins."""
        # Validated FIRST, before the agreement shortcut. A rule missing the
        # parameter it needs is malformed whether or not the data happens to
        # agree today, and finding that out only on the day they disagree is
        # finding it out in the worst place.
        if rule is ConflictRule.REQUIRE_QUORUM:
            if (not isinstance(quorum, int) or isinstance(quorum, bool)
                    or quorum < 2):
                raise ConflictError("quorum must be an int >= 2")
        if rule is ConflictRule.PREFER_ROLE and not isinstance(prefer,
                                                              AgentRole):
            raise ConflictError(
                "PREFER_ROLE needs the role it prefers; a preference with no "
                "subject is not a rule")

        claims = self.claims_about(task_id, subject)
        if not claims:
            return Resolution(False, subject, rule, None,
                              "no claims to resolve", ())
        values = {c.value_digest for c in claims}
        if len(values) == 1:
            return Resolution(
                True, subject, rule, next(iter(values)),
                f"{len(claims)} claim(s) agree", claims)

        if rule is ConflictRule.REQUIRE_QUORUM:
            by_value: dict = {}
            for c in claims:
                by_value.setdefault(c.value_digest, set()).add(c.by_instance)
            winners = sorted(v for v, who in by_value.items()
                             if len(who) >= quorum)
            if len(winners) == 1:
                return Resolution(
                    True, subject, rule, winners[0],
                    f"{quorum} distinct instances agree", claims)
            if len(winners) > 1:
                raise ConflictError(
                    f"{subject!r}: {len(winners)} values each reached quorum "
                    f"{quorum}; a rule that can select two answers has not "
                    "resolved anything")
            return Resolution(
                False, subject, rule, None,
                f"no value reached quorum {quorum}", claims)

        if rule is ConflictRule.PREFER_ROLE:
            preferred = [c for c in claims if c.role is prefer]
            pvalues = {c.value_digest for c in preferred}
            if len(pvalues) == 1:
                return Resolution(
                    True, subject, rule, next(iter(pvalues)),
                    f"the {prefer.value} claim decides", claims)
            if len(pvalues) > 1:
                return Resolution(
                    False, subject, rule, None,
                    f"{len(pvalues)} {prefer.value} instances disagree with "
                    "each other; the preference cannot break a tie inside "
                    "the preferred role", claims)
            return Resolution(
                False, subject, rule, None,
                f"no {prefer.value} claim exists", claims)

        return Resolution(
            False, subject, rule, None,
            f"{len(values)} conflicting values; REQUIRE_HUMAN means this "
            "waits for a person, and no agent may substitute for one",
            claims)

    # ---- escalation ----------------------------------------------------
    def escalate(self, *, escalation_id: str, task_id: str, question: str,
                 raised_by: str, options: tuple) -> Escalation:
        """Raise a decision that is not the agent's to make, and DELIVER it.

        Raising used to end at the log. An open escalation was visible to
        anyone who ran a query and to nobody who did not, which for a thing
        that blocks work until a person acts means it is not raised, it is
        filed. The difference matters most in exactly the case escalation
        exists for: nobody is looking.

        So a registered :class:`Notifier` is called for every escalation.
        Delivery is best-effort BY CONSTRUCTION and the code says so rather
        than pretending: a sink that raises must not undo an escalation that
        is already durable in the log, because losing the record to save the
        notification is backwards. Failures are recorded on the notifier and
        reported by :meth:`undelivered`, so "nobody was told" is a question
        with an answer.
        """
        self.get(raised_by)
        if not question.strip():
            raise EscalationError("an escalation must ask something")
        opts = tuple(dict.fromkeys(options))
        if len(opts) < 2:
            raise EscalationError(
                "an escalation must offer at least two options; a question "
                "with one answer is a notification, and it should not block "
                "anything")
        esc = Escalation(escalation_id=escalation_id, task_id=task_id,
                         question=question, raised_by=raised_by,
                         options=opts)
        ev = self.log.append(actor=raised_by, action=ACT_ESCALATION,
                             target=task_id,
                             payload={"escalation": esc.to_record()})
        self.apply(ev)
        raised = self._escalations[escalation_id]
        # DURABLE FIRST, DELIVERED SECOND, and never the other way round.
        self._notify(raised)
        return raised

    def _notify(self, esc: Escalation) -> None:
        """Hand the escalation to the sink, surviving anything it does."""
        if self.notifier is None:
            return
        try:
            self.notifier.escalation_raised(esc)
        except Exception as exc:                        # noqa: BLE001
            # Deliberately broad. A sink is somebody else's code writing to
            # somebody else's system, and there is no exception it could
            # raise that would make discarding a recorded escalation the
            # right answer.
            self._undelivered.append((esc.escalation_id, repr(exc)))

    def undelivered(self) -> tuple:
        """``(escalation_id, error)`` for every notification that failed.

        A caller that never asks gets the old behaviour, which is why this
        exists as a query rather than as a raise: the escalation is already
        safe, and it is the OPERATOR who needs to know their channel is
        broken.
        """
        return tuple(self._undelivered)

    def answer(self, *, escalation_id: str, answered_by: str, answer: str,
               reason: str) -> Escalation:
        """Answer an escalation. HUMAN principals only, and never the asker.

        The kind check is the whole point. An agent holding REVIEWER is still
        an agent, and an escalation exists precisely because the decision was
        not the agent's to make -- so no arrangement of roles substitutes for
        a person.
        """
        esc = self.escalation(escalation_id)
        if esc.state is not EscalationState.OPEN:
            raise EscalationError(
                f"escalation {escalation_id!r} is {esc.state.value}; "
                "answering it again would rewrite a decision that was already "
                "recorded")
        ident = self.get(answered_by)
        if ident.kind is not PrincipalKind.HUMAN:
            raise EscalationError(
                f"{answered_by!r} is a {ident.kind.value} principal and may "
                "not answer an escalation. An escalation exists because the "
                "decision was not the agent's to make; holding REVIEWER does "
                "not change what kind of thing is deciding.")
        if answered_by == esc.raised_by:
            raise EscalationError(
                f"{answered_by!r} raised this escalation and may not also "
                "answer it")
        if answer not in esc.options:
            raise EscalationError(
                f"answer {answer!r} is not one of {list(esc.options)}; an "
                "answer that cannot be checked against what was asked is a "
                "conversation, not a decision")
        if not reason.strip():
            raise EscalationError(
                "an answered escalation must carry a reason; the reason is "
                "what a later reader needs and the answer alone never gives")
        ev = self.log.append(
            actor=answered_by, action=ACT_ESCALATION_ANSWER,
            target=esc.task_id,
            payload={"escalation_id": escalation_id,
                     "state": EscalationState.ANSWERED.value,
                     "answer": answer, "answered_by": answered_by,
                     "reason": reason})
        self.apply(ev)
        return self._escalations[escalation_id]

    def withdraw(self, *, escalation_id: str, by: str,
                 reason: str) -> Escalation:
        esc = self.escalation(escalation_id)
        if esc.raised_by != by:
            raise EscalationError(
                f"escalation {escalation_id!r} was raised by "
                f"{esc.raised_by!r}; {by!r} may not withdraw it")
        if esc.state is not EscalationState.OPEN:
            raise EscalationError(
                f"escalation {escalation_id!r} is {esc.state.value}")
        ev = self.log.append(
            actor=by, action=ACT_ESCALATION_ANSWER, target=esc.task_id,
            payload={"escalation_id": escalation_id,
                     "state": EscalationState.WITHDRAWN.value,
                     "reason": reason})
        self.apply(ev)
        return self._escalations[escalation_id]

    def escalation(self, escalation_id: str) -> Escalation:
        try:
            return self._escalations[escalation_id]
        except KeyError:
            raise EscalationError(
                f"no escalation {escalation_id!r}") from None

    def open_escalations(self, *, task_id: str | None = None) -> tuple:
        return tuple(sorted(
            (e for e in self._escalations.values()
             if e.state is EscalationState.OPEN
             and (task_id is None or e.task_id == task_id)),
            key=lambda e: e.escalation_id))

    def is_blocked(self, task_id: str) -> bool:
        """True while any escalation for this task is unanswered.

        A workflow that consults this waits for a person. In THIS repository
        it waits forever, because no human decision exists here and none is
        fabricated -- which is the correct behaviour for a mechanism whose
        input does not exist yet.
        """
        return bool(self.open_escalations(task_id=task_id))


#: The one value that may register the first human principal. Spelled out so
#: that granting human authority is greppable, and so that an installation
#: which lets an agent do it has made a visible choice rather than a quiet one.
BOOTSTRAP = "out-of-band-bootstrap"


@dataclass(frozen=True)
class SeparationCheck:
    """Whether one instance may take a second role on a task it touched.

    Kept as a value rather than a boolean so a refusal carries its reason
    into the record that refused it.
    """

    allowed: bool
    reason: str
    conflicting_role: AgentRole | None = None


def check_separation(directory: AgentDirectory, *, instance_id: str,
                     taking: AgentRole, already: dict) -> SeparationCheck:
    """Refuse a second role that would defeat a separation on THIS task.

    ``already`` maps role -> instance that has taken it for the task so far.
    Same-instance is what matters: two runs of one agent are one party here,
    because an agent that restarts has not become somebody else.
    """
    ident = directory.get(instance_id)
    if not ident.may(taking):
        return SeparationCheck(
            False, f"{instance_id!r} does not hold {taking.value}")
    for a, b in INCOMPATIBLE:
        for first, second in ((a, b), (b, a)):
            if taking is not second:
                continue
            holder = already.get(first)
            if holder is None:
                continue
            if _same_agent(directory, holder, instance_id):
                return SeparationCheck(
                    False,
                    (f"{instance_id!r} already took {first.value} on this "
                     f"task and may not also take {second.value}; an agent "
                     "that checks its own work has not checked anything"),
                    first)
    return SeparationCheck(True, f"{instance_id!r} may take {taking.value}")


def _same_agent(directory: AgentDirectory, a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        return directory.get(a).agent_id == directory.get(b).agent_id
    except IdentityError:
        # An unregistered party cannot be shown to be different, so it is
        # treated as the same. Fail closed.
        return True
