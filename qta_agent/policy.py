"""Versioned policy: rules that can be read, digested, and dated.

WHY ``policy_id`` WAS NOT A POLICY

The authority state machine has always required an explicit ``policy_id`` on
promotion (I5). That closed one hole -- a promotion could not happen with
nothing named as its basis -- and left a larger one open: the id named
nothing. There was no document, no version, no rule set, and no record of
what the policy had said. "Which policy allowed this?" was answerable only as
a string; "what did that policy permit?" was not answerable at all.

This module makes the identity resolve to content:

  * a :class:`PolicyDocument` is an immutable, ordered set of rules with a
    version and a content digest;
  * publishing one is an event, so the sequence of policies is part of the
    same hash-chained history as the decisions they governed;
  * evaluating a request yields a :class:`Decision` that names the document
    digest, the rule that decided, and why -- so the reason survives the
    process that produced it.

DENY OVERRIDES, AND THE DEFAULT IS DENY

Evaluation collects every rule that matches the request. If any of them denies,
the request is denied and the first such rule is named. Otherwise, if any rule
allows, it is allowed. If nothing matches, it is denied.

Deny-overrides rather than first-match is deliberate. Under first-match, a
carve-out written to forbid something can be silently defeated by an earlier
broad grant, and the mistake is invisible because both rules are individually
correct. Under deny-overrides, adding a prohibition cannot be undone by
ordering.

NO EMPTY MATCH SETS

Every match field must be a non-empty tuple, and "any" must be spelled
:data:`ANY` rather than left blank. An empty field is refused at construction.
The asymmetry is the reason: an empty field on an ALLOW rule grants nothing
and is merely useless, while an empty field on a DENY rule forbids nothing and
quietly re-permits whatever the rule was written to stop. Refusing both keeps
a truncated or half-edited rule from being the dangerous one.

POLICY IS NOT RETROACTIVE

:meth:`PolicyStore.in_force_at` returns the document that was published at or
before a log position -- not the newest one. A decision made at seq 40 was
governed by the policy in force at seq 40, and re-evaluating it under today's
rules would answer a different question than the one an auditor is asking.
Todays's rules govern today's requests; that is what makes I5 mean anything.

AN ALLOW WITH AN UNDISCHARGED OBLIGATION IS NOT AN ALLOW

A boolean verdict answers "may this happen" and nothing else. Real control
planes rarely permit unconditionally: they permit *provided that* the result
is recorded, *provided that* the output is redacted, *provided that* the
grant is used once. Writing those conditions in the rule's ``reason`` string
makes them documentation; writing them as :data:`OBLIGATIONS` makes them
enforcement.

An ALLOW rule may carry obligations. A decision collects the obligations of
EVERY matching ALLOW rule -- not only the one named as deciding -- for the
same reason evaluation is deny-overrides: a condition attached by one rule
must not be defeated by another rule matching first. The decision starts with
all of them outstanding, and :meth:`Decision.raise_if_denied` refuses while
any remain. Discharging one requires naming it and citing a digest of what
discharged it, so "I did that" is a record rather than a claim.

:class:`PolicyObligationUnmet` is deliberately NOT a subclass of
:class:`PolicyDenied`. "You may not" and "you have not yet" are different
answers, and a caller that catches denial should not silently absorb the
second.

VERSIONS ARE GAP-FREE

A document's version must be exactly one more than the previous version of the
same ``policy_id``. Skipping a version would make "which policy governed seq
N" ambiguous in exactly the case where it matters -- when someone is looking
for the version that is missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .canonical import digest

#: Explicit wildcard. Must be written out; a blank field is refused.
ANY = "*"

#: Event actions. Constants so a typo cannot create a second, unread action.
ACT_POLICY_PUBLISH = "policy.publish"
ACT_POLICY_DECISION = "policy.decision"

#: A published document larger than this is refused. A policy nobody can read
#: is not inspectable, and an unbounded one is a memory bomb on every replay.
MAX_RULES = 512

#: Conditions an ALLOW may be made subject to. The set is CLOSED for the same
#: reason the boundary vocabulary is closed: an obligation nothing enforces is
#: worse than no obligation, because it reads like a control. Every name here
#: has a discharge site in this repository, and a rule naming anything else is
#: refused at construction rather than published and ignored.
OBLIGATIONS = {
    # The action's result must be written to the content-addressed evidence
    # store before the ALLOW is considered honoured.
    "record_evidence",
    # Any output that will be recorded must pass secret redaction first.
    "redact_output",
    # Every output the tool contract declared must have been collected and
    # digested; a zero exit that produced nothing is not a discharge.
    "verify_declared_outputs",
    # The decision authorizes exactly one action. Re-use is a fresh request.
    "single_use",
    # An effect that leaves this system must raise a human escalation, because
    # nothing here can compensate it automatically.
    "escalate_external_effect",
}


class PolicyError(Exception):
    """Base class. Every failure here is fail-closed."""


class PolicyDenied(PolicyError):
    """The policy in force does not permit this request."""


class UnknownPolicy(PolicyError):
    """No such policy was ever published, or none at the requested position."""


class PolicyVersionError(PolicyError):
    """A publication would break the version sequence."""


class PolicyObligationUnmet(PolicyError):
    """The rules permit this, and a condition they attached is outstanding.

    NOT a :class:`PolicyDenied`. A caller written to handle refusal would
    treat "not yet" as "never" and give up on work it is entitled to do; a
    caller written to handle refusal by escalating would escalate something
    that needs no human at all.
    """


class Effect(str, Enum):
    """What a matching rule does.

    ``str`` mixin so it serializes canonically.
    """

    ALLOW = "ALLOW"
    DENY = "DENY"


def _match_field(values, what: str) -> tuple:
    """Validate one match field: non-empty, all non-empty strings, sorted."""
    if isinstance(values, str):
        # A bare string would iterate character by character and produce a
        # rule matching single letters. Refused rather than wrapped, because
        # guessing the author's intent is how a rule ends up meaning
        # something nobody wrote.
        raise PolicyError(
            f"{what} must be a sequence of strings, not the bare string "
            f"{values!r}")
    try:
        items = list(values)
    except TypeError as exc:
        raise PolicyError(f"{what} is not iterable: {exc}") from exc
    # Type-check BEFORE sorting. ``sorted`` on a mixed list raises TypeError
    # about comparison, which names the wrong problem and sends whoever reads
    # it looking for an ordering bug.
    for v in items:
        if not isinstance(v, str) or not v:
            raise PolicyError(
                f"{what} entry must be a non-empty str, got {v!r}")
    out = tuple(sorted(set(items)))
    if not out:
        raise PolicyError(
            f"{what} is empty; write {ANY!r} if you mean 'any'. An empty "
            "match set on a DENY rule forbids nothing, which is the opposite "
            "of what such a rule is written for.")
    return out


@dataclass(frozen=True)
class PolicyRequest:
    """What is being asked, described independently of who is asking.

    Mirrors :class:`~qta_agent.capability.Request`: the evaluation compares a
    described request against described rules, and cannot consult the caller.
    """

    action: str
    subject: str
    role: str
    resource: str
    task_id: str = ""
    #: Diagnostic only. Never consulted by :meth:`Rule.matches` -- adding a
    #: match on free-form attributes would make the rule language open-ended,
    #: and an open-ended rule language is an interpreter with an audience.
    attributes: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        return {"action": self.action, "subject": self.subject,
                "role": self.role, "resource": self.resource,
                "task_id": self.task_id,
                "attributes": dict(sorted(self.attributes.items()))}


@dataclass(frozen=True)
class Rule:
    """One ordered, total, side-effect-free decision over a request."""

    rule_id: str
    effect: Effect
    actions: tuple
    subjects: tuple
    roles: tuple
    resources: tuple
    reason: str = ""
    #: Conditions attached to an ALLOW. Always empty on a DENY: a refusal
    #: cannot be made conditional on the refused party doing something.
    obligations: tuple = ()

    def matches(self, req: PolicyRequest) -> bool:
        """Every field must match. Conjunctive; there is no partial match."""
        return (_hit(self.actions, req.action)
                and _hit(self.subjects, req.subject)
                and _hit(self.roles, req.role)
                and _hit(self.resources, req.resource))

    def body(self) -> dict:
        return {"rule_id": self.rule_id, "effect": self.effect.value,
                "actions": list(self.actions), "subjects": list(self.subjects),
                "roles": list(self.roles), "resources": list(self.resources),
                "reason": self.reason, "obligations": list(self.obligations)}


def _hit(allowed: tuple, value: str) -> bool:
    return ANY in allowed or value in allowed


def _obligations(values, effect: Effect, rule_id: str) -> tuple:
    """Validate an obligation list: known names only, deduplicated, sorted.

    Sorted because the tuple reaches the document digest, and two rules that
    attach the same conditions in a different order are the same rule.
    """
    if values is None:
        return ()
    if isinstance(values, str):
        raise PolicyError(
            f"rule {rule_id!r}: obligations must be a sequence of names, not "
            f"the bare string {values!r}")
    try:
        items = list(values)
    except TypeError as exc:
        raise PolicyError(
            f"rule {rule_id!r}: obligations is not iterable: {exc}") from exc
    for v in items:
        if not isinstance(v, str) or not v:
            raise PolicyError(
                f"rule {rule_id!r}: obligation {v!r} is not a non-empty str")
        if v not in OBLIGATIONS:
            raise PolicyError(
                f"rule {rule_id!r}: unknown obligation {v!r}. The set is "
                f"closed to {sorted(OBLIGATIONS)}; an obligation with no "
                "discharge site would read like a control and enforce "
                "nothing")
    if items and effect is not Effect.ALLOW:
        raise PolicyError(
            f"rule {rule_id!r}: a DENY carries obligations "
            f"{sorted(set(items))}. A refusal cannot be made conditional on "
            "the refused party doing something; the request is already over")
    return tuple(sorted(set(items)))


def rule(*, rule_id: str, effect: Effect, actions, subjects, roles,
         resources, reason: str = "", obligations=()) -> Rule:
    """Construct a rule, validating everything that cannot be fixed later."""
    if not isinstance(rule_id, str) or not rule_id:
        raise PolicyError("rule_id must be a non-empty str")
    if not isinstance(effect, Effect):
        raise PolicyError(f"effect must be an Effect, got {effect!r}")
    return Rule(rule_id=rule_id, effect=effect,
                actions=_match_field(actions, "actions"),
                subjects=_match_field(subjects, "subjects"),
                roles=_match_field(roles, "roles"),
                resources=_match_field(resources, "resources"),
                reason=str(reason),
                obligations=_obligations(obligations, effect, rule_id))


@dataclass(frozen=True)
class PolicyDocument:
    """An immutable, versioned rule set. Its digest is its identity."""

    policy_id: str
    version: int
    rules: tuple
    description: str = ""

    def body(self) -> dict:
        return {"policy_id": self.policy_id, "version": self.version,
                "rules": [r.body() for r in self.rules],
                "description": self.description}

    def digest(self) -> str:
        return digest(self.body())

    @property
    def identity(self) -> str:
        """``policy_id@version``. Human-facing; the digest is the identity."""
        return f"{self.policy_id}@{self.version}"

    def evaluate(self, req: PolicyRequest) -> "Decision":
        """Deny-overrides evaluation. Total: always returns a Decision."""
        matched = [r for r in self.rules if r.matches(req)]
        denies = [r for r in matched if r.effect is Effect.DENY]
        if denies:
            r = denies[0]
            return Decision(
                allowed=False, policy_id=self.policy_id, version=self.version,
                policy_digest=self.digest(), rule_id=r.rule_id,
                effect=Effect.DENY, request=req.to_record(),
                reason=r.reason or f"denied by rule {r.rule_id}")
        allows = [r for r in matched if r.effect is Effect.ALLOW]
        if allows:
            r = allows[0]
            # Obligations accumulate across EVERY matching ALLOW, not only the
            # one named as deciding. Taking them from allows[0] alone would
            # let a broad unconditional grant defeat a narrower conditional
            # one purely by position -- the same defect deny-overrides exists
            # to prevent, reappearing one field to the right.
            attached = sorted({o for a in allows for o in a.obligations})
            return Decision(
                allowed=True, policy_id=self.policy_id, version=self.version,
                policy_digest=self.digest(), rule_id=r.rule_id,
                effect=Effect.ALLOW, request=req.to_record(),
                reason=r.reason or f"allowed by rule {r.rule_id}",
                obligations=tuple(attached),
                outstanding=tuple(attached))
        return Decision(
            allowed=False, policy_id=self.policy_id, version=self.version,
            policy_digest=self.digest(), rule_id=None, effect=Effect.DENY,
            request=req.to_record(),
            reason="no rule matched; the default is deny")


def document(*, policy_id: str, version: int, rules,
             description: str = "") -> PolicyDocument:
    """Construct a document, validating shape and rule-id uniqueness."""
    if not isinstance(policy_id, str) or not policy_id:
        raise PolicyError("policy_id must be a non-empty str")
    if (not isinstance(version, int) or isinstance(version, bool)
            or version < 1):
        raise PolicyError(
            f"version must be an int >= 1, got {version!r}; version 0 would "
            "be indistinguishable from 'unset'")
    rules = tuple(rules)
    if not rules:
        raise PolicyError(
            "a policy with no rules denies everything by default and is "
            "refused rather than published; an empty rule set is almost "
            "always a serialization failure, not an intent")
    if len(rules) > MAX_RULES:
        raise PolicyError(
            f"{len(rules)} rules exceeds the {MAX_RULES}-rule bound")
    seen = set()
    for r in rules:
        if not isinstance(r, Rule):
            raise PolicyError(f"rules must be Rule instances, got {r!r}")
        if r.rule_id in seen:
            raise PolicyError(
                f"duplicate rule_id {r.rule_id!r}; a decision naming it would "
                "not identify which rule decided")
        seen.add(r.rule_id)
    return PolicyDocument(policy_id=policy_id, version=version, rules=rules,
                          description=str(description))


def document_from_record(rec: dict) -> PolicyDocument:
    """Rebuild a document from a log payload, validating its shape.

    Used by the projection and by any independent reconstruction, so a
    malformed record fails identically in both rather than being tolerated by
    whichever one happens to be more forgiving.
    """
    if not isinstance(rec, dict):
        raise PolicyError(f"policy record is {type(rec).__name__}")
    try:
        raw_rules = rec["rules"]
        if not isinstance(raw_rules, list):
            raise PolicyError("policy record 'rules' must be a list")
        rules = []
        for rr in raw_rules:
            if not isinstance(rr, dict):
                raise PolicyError(
                    f"rule record is {type(rr).__name__}, not an object")
            unknown = set(rr) - {"rule_id", "effect", "actions", "subjects",
                                 "roles", "resources", "reason",
                                 "obligations"}
            if unknown:
                # An unrecognised field could be a condition this reader does
                # not evaluate. Applying the rule anyway would apply a
                # narrower rule than the author wrote.
                raise PolicyError(
                    f"rule {rr.get('rule_id')!r} carries unknown fields "
                    f"{sorted(unknown)}; refusing to evaluate a rule this "
                    "version does not fully understand")
            rules.append(rule(
                rule_id=rr["rule_id"], effect=Effect(rr["effect"]),
                actions=rr["actions"], subjects=rr["subjects"],
                roles=rr["roles"], resources=rr["resources"],
                reason=rr.get("reason", ""),
                obligations=rr.get("obligations", ())))
        return document(policy_id=rec["policy_id"], version=rec["version"],
                        rules=tuple(rules),
                        description=rec.get("description", ""))
    except KeyError as exc:
        raise PolicyError(f"policy record missing {exc}") from exc
    except ValueError as exc:
        raise PolicyError(f"policy record is invalid: {exc}") from exc


@dataclass(frozen=True)
class Decision:
    """Why a request was permitted or refused, in a form that outlives it.

    Carries the policy digest rather than only the id, so a reader can tell
    whether the document they are looking at is the one that decided. Two
    policies with the same id and version but different content are a
    tampering signature, and only the digest makes them distinguishable.
    """

    allowed: bool
    policy_id: str
    version: int
    policy_digest: str
    rule_id: str | None
    effect: Effect
    request: dict
    reason: str
    #: Every condition the matching ALLOW rules attached. Immutable: it is
    #: what the policy said, and discharging one does not change that.
    obligations: tuple = ()
    #: The subset not yet discharged. This is what gates the decision.
    outstanding: tuple = ()
    #: obligation name -> digest of whatever discharged it.
    discharges: tuple = ()
    #: Log position the decision was made at, set when it is recorded.
    at_seq: int = -1

    def to_record(self) -> dict:
        return {"allowed": self.allowed, "policy_id": self.policy_id,
                "version": self.version, "policy_digest": self.policy_digest,
                "rule_id": self.rule_id, "effect": self.effect.value,
                "request": self.request, "reason": self.reason,
                "obligations": list(self.obligations),
                "at_seq": self.at_seq}

    def discharge(self, name: str, *, evidence_digest: str) -> "Decision":
        """Record that ``name`` has been satisfied, citing what satisfied it.

        Returns a NEW decision rather than mutating: the recorded decision is
        what the policy said, and a discharge is something the caller did
        afterwards. Collapsing the two would make the log's copy disagree
        with the object in hand.

        The evidence digest is not verified here -- this module cannot know
        what an evidence digest for "redact_output" should look like. It is
        required so that a discharge names something, and so an auditor
        reading the trail has a thread to pull rather than a bare True.
        """
        if name not in self.obligations:
            raise PolicyError(
                f"{self.identity} attached no obligation {name!r} to this "
                f"decision (it attached {sorted(self.obligations) or 'none'});"
                " discharging one that was never required would let a caller "
                "satisfy an obligation by inventing it")
        if not isinstance(evidence_digest, str) or not evidence_digest:
            raise PolicyError(
                f"discharging {name!r} requires a non-empty evidence digest; "
                "a discharge that cites nothing is an assertion")
        if name not in self.outstanding:
            raise PolicyError(
                f"obligation {name!r} is already discharged by "
                f"{dict(self.discharges)[name][:12]}; discharging it twice "
                "would let one piece of evidence stand for two actions")
        return Decision(
            **{**self.__dict__,
               "outstanding": tuple(o for o in self.outstanding if o != name),
               "discharges": self.discharges + ((name, evidence_digest),)})

    def digest(self) -> str:
        """Content digest, EXCLUDING ``at_seq``.

        The same decision reached at two positions is the same decision; the
        position is where it was recorded, not part of what was decided.
        """
        rec = self.to_record()
        rec.pop("at_seq")
        return digest(rec)

    @property
    def identity(self) -> str:
        return f"{self.policy_id}@{self.version}"

    def raise_if_denied(self) -> "Decision":
        """Refuse if the RULES forbid this. Says nothing about obligations.

        Deliberately unchanged and deliberately weak. This is the question
        asked at authorization time, before the work has happened, when every
        obligation is outstanding because none of them COULD have been
        discharged yet. Strengthening it here would make the correct call
        site fail and teach every caller to route around it.

        The obligations are enforced by :meth:`completion_receipt`, which is
        structural rather than remembered: the success path cannot build its
        result without one.
        """
        if not self.allowed:
            raise PolicyDenied(
                f"{self.identity} denied {self.request.get('action')!r} on "
                f"{self.request.get('resource')!r} for "
                f"{self.request.get('subject')!r}: {self.reason}")
        return self

    def completion_receipt(self) -> dict:
        """Prove every attached condition was met, or refuse.

        WHY A RECEIPT AND NOT A CHECK

        An obligation enforced by a boolean method the caller must remember
        to call is enforced by the caller's memory. Deleting the call is
        invisible, and the guard that can be skipped is not a guard.

        So this returns something the success path NEEDS: the receipt is a
        field of the run's result, and the result cannot be constructed
        without asking for it. Removing a discharge does not remove a check
        somebody might not notice -- it makes the run raise on its way to
        reporting success.

        Returns ``{obligation: evidence_digest}``, sorted, so the reason each
        condition is considered met travels with the outcome instead of being
        an inference about control flow.
        """
        if not self.allowed:
            raise PolicyDenied(
                f"{self.identity} denied {self.request.get('action')!r}: "
                f"{self.reason}. A receipt for a refused request would "
                "certify work that was never authorized")
        if self.outstanding:
            raise PolicyObligationUnmet(
                f"{self.identity} allows {self.request.get('action')!r} on "
                f"{self.request.get('resource')!r} subject to "
                f"{sorted(self.obligations)}, and "
                f"{sorted(self.outstanding)} "
                f"{'is' if len(self.outstanding) == 1 else 'are'} still "
                "outstanding. An ALLOW whose conditions are unmet is not an "
                "ALLOW yet")
        return dict(sorted(self.discharges))


class PolicyStore:
    """Published policies as a projection of the log. Fail closed."""

    def __init__(self, log):
        self.log = log
        #: policy_id -> list of (seq, PolicyDocument), version-ordered.
        self._published: dict = {}
        self._loaded_through = -1

    # ---- projection ----------------------------------------------------
    def load(self) -> "PolicyStore":
        self.log.verify().raise_if_bad()
        self._published = {}
        self._loaded_through = -1
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        """Fold one event in. Returns True when it was a policy event.

        Returns rather than raising on a foreign action, because this store is
        one reducer among several over a shared log and must not claim that
        another subsystem's event is unknown.
        """
        if ev.action == ACT_POLICY_PUBLISH:
            doc = document_from_record(ev.payload["document"])
            claimed = ev.payload.get("policy_digest")
            if claimed != doc.digest():
                raise PolicyError(
                    f"seq {ev.seq}: policy record claims digest "
                    f"{str(claimed)[:12]} but its content hashes to "
                    f"{doc.digest()[:12]}")
            history = self._published.setdefault(doc.policy_id, [])
            expected = history[-1][1].version + 1 if history else 1
            if doc.version != expected:
                raise PolicyVersionError(
                    f"seq {ev.seq}: {doc.policy_id!r} version {doc.version} "
                    f"follows version {expected - 1}; versions must be "
                    "gap-free so 'which policy governed seq N' is answerable")
            history.append((ev.seq, doc))
            self._loaded_through = ev.seq
            return True
        if ev.action == ACT_POLICY_DECISION:
            self._recheck_decision(ev)
            self._loaded_through = ev.seq
            return True
        return False

    def _recheck_decision(self, ev) -> None:
        """Re-derive a recorded verdict from the rules that were in force.

        A decision record names its own verdict, its own rule and its own
        policy version. Folding it in unread meant the log could contain an
        ALLOW that no published rule would produce, and every reader
        downstream -- the audit index most of all -- would repeat it as the
        reason something was permitted.

        The record carries the whole request, so this is not a matter of
        trusting it more carefully: the verdict is recomputable. The document
        in force at this position decides, and the record is compared against
        what it says. That makes a forged ALLOW a load-time failure instead
        of a citation.
        """
        rec = ev.payload.get("decision")
        if not isinstance(rec, dict):
            raise PolicyError(
                f"seq {ev.seq}: decision record carries no decision")
        claimed = ev.payload.get("decision_digest")
        body = {k: v for k, v in rec.items() if k != "at_seq"}
        if claimed != digest(body):
            raise PolicyError(
                f"seq {ev.seq}: decision claims digest {str(claimed)[:12]} "
                f"but its content hashes to {digest(body)[:12]}")
        policy_id = rec.get("policy_id")
        try:
            doc = self.in_force_at(policy_id, ev.seq)
        except (UnknownPolicy, PolicyError) as exc:
            raise PolicyError(
                f"seq {ev.seq}: decision cites policy {policy_id!r}, which "
                f"nothing published by this position: {exc}") from None
        if doc.digest() != rec.get("policy_digest"):
            raise PolicyError(
                f"seq {ev.seq}: decision cites policy digest "
                f"{str(rec.get('policy_digest'))[:12]}, but {doc.identity} "
                f"in force here hashes to {doc.digest()[:12]}; two documents "
                "sharing an id and version are a tampering signature")
        req = rec.get("request")
        if not isinstance(req, dict):
            raise PolicyError(
                f"seq {ev.seq}: decision records no request, so its verdict "
                "cannot be re-derived and is only an assertion")
        try:
            replayed = doc.evaluate(PolicyRequest(
                action=req.get("action", ""), subject=req.get("subject", ""),
                role=req.get("role", ""), resource=req.get("resource", ""),
                task_id=req.get("task_id", ""),
                attributes=dict(req.get("attributes") or {})))
        except (TypeError, ValueError) as exc:
            raise PolicyError(
                f"seq {ev.seq}: decision carries a request this build cannot "
                f"evaluate: {exc}") from None
        for field_name, replayed_value in (
                ("allowed", replayed.allowed),
                ("effect", replayed.effect.value),
                ("rule_id", replayed.rule_id),
                # An ALLOW recorded with fewer obligations than the rules
                # attach is the forgery this check exists to catch, one field
                # further in: the verdict is right, the conditions on it are
                # not, and every reader downstream repeats the unconditional
                # version.
                ("obligations", list(replayed.obligations))):
            if rec.get(field_name, [] if field_name == "obligations"
                       else None) != replayed_value:
                raise PolicyError(
                    f"seq {ev.seq}: the record says {field_name}="
                    f"{rec.get(field_name)!r} for "
                    f"{req.get('action')!r} on {req.get('resource')!r}, but "
                    f"{doc.identity} decides {replayed_value!r}. A verdict "
                    "the rules do not produce did not come from them.")

    # ---- reads ---------------------------------------------------------
    def in_force(self, policy_id: str) -> PolicyDocument:
        """The newest published version of ``policy_id``."""
        history = self._published.get(policy_id)
        if not history:
            raise UnknownPolicy(
                f"no policy {policy_id!r} has been published; an unpublished "
                "policy authorizes nothing")
        return history[-1][1]

    def in_force_at(self, policy_id: str, at_seq: int) -> PolicyDocument:
        """The version published at or before ``at_seq``.

        This is what makes a historical decision re-checkable: an auditor
        asking why seq 40 was permitted gets the rules that were in force at
        seq 40, not the ones written afterwards.
        """
        if not isinstance(at_seq, int) or isinstance(at_seq, bool):
            raise PolicyError(f"at_seq must be an int, got {at_seq!r}")
        history = self._published.get(policy_id)
        if not history:
            raise UnknownPolicy(f"no policy {policy_id!r} has been published")
        candidates = [d for seq, d in history if seq <= at_seq]
        if not candidates:
            raise UnknownPolicy(
                f"policy {policy_id!r} was first published at seq "
                f"{history[0][0]}, after {at_seq}; nothing governed that "
                "position and the default is deny")
        return candidates[-1]

    def versions(self, policy_id: str) -> tuple:
        """``(seq, version, digest)`` for every publication, oldest first."""
        return tuple((seq, d.version, d.digest())
                     for seq, d in self._published.get(policy_id, ()))

    def policy_ids(self) -> tuple:
        return tuple(sorted(self._published))

    # ---- writes --------------------------------------------------------
    def publish(self, doc: PolicyDocument, *, actor: str) -> PolicyDocument:
        """Append a new version. Refuses gaps, reuse and silent edits."""
        if not isinstance(doc, PolicyDocument):
            raise PolicyError(f"expected a PolicyDocument, got {doc!r}")
        history = self._published.get(doc.policy_id, [])
        expected = history[-1][1].version + 1 if history else 1
        if doc.version != expected:
            raise PolicyVersionError(
                f"{doc.policy_id!r} is at version {expected - 1}; the next "
                f"publication must be version {expected}, not {doc.version}")
        ev = self.log.append(
            actor=actor, action=ACT_POLICY_PUBLISH, target=doc.policy_id,
            payload={"document": doc.body(), "policy_digest": doc.digest()})
        self.apply(ev)
        return doc

    def evaluate(self, policy_id: str, req: PolicyRequest, *,
                 at_seq: int | None = None) -> Decision:
        """Decide ``req`` under the policy in force. Never raises on denial."""
        doc = (self.in_force(policy_id) if at_seq is None
               else self.in_force_at(policy_id, at_seq))
        return doc.evaluate(req)

    def decide_and_record(self, policy_id: str, req: PolicyRequest, *,
                          actor: str, target: str = "") -> Decision:
        """Evaluate and append the decision, so the reason outlives the run.

        Denials are recorded too. A control plane that logs only what it
        permitted cannot answer "what did this agent try", which is the
        question an incident starts with.
        """
        decision = self.evaluate(policy_id, req)
        ev = self.log.append(
            actor=actor, action=ACT_POLICY_DECISION,
            target=target or req.resource,
            payload={"decision": decision.to_record(),
                     "decision_digest": decision.digest()})
        self.apply(ev)
        return Decision(**{**decision.__dict__, "at_seq": ev.seq})
