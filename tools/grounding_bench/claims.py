"""The claim contract: V(C, S, E) -> SUPPORTED | CONTRADICTED | UNKNOWN |
UNVERIFIABLE.

A CLAIM is a statement an agent is about to assert, in a closed form:
``Claim(subject, predicate, value)``. Free text is not a claim; it is
something a claim might be extracted from, and the extraction is not
measured here.

S is authoritative state as an INDEPENDENT reader rebuilt it from the log
(:func:`agent_substrate.reconstruct.reconstruct`), with or without an
authenticator. E is the evidence store, which re-hashes what it returns.

The four answers, and what each is NOT:

SUPPORTED
    S holds the subject, nothing in the history around it was refused, and
    the claim is what S and E say. Not "true": S can only say what the log
    establishes, and a well-formed record nobody authenticated is in S.
CONTRADICTED
    S and E say something else. The claim is refused, and the value S holds
    is available to replace it.
UNKNOWN
    S holds nothing about the subject. Not "false": absence from the log is
    absence of a record, not a record of absence.
UNVERIFIABLE
    S holds the subject, but cannot vouch for it: a transition of it was
    refused on replay, its evidence is missing or no longer verifies, or the
    predicate is one S does not speak to.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

SUPPORTED, CONTRADICTED = "SUPPORTED", "CONTRADICTED"
UNKNOWN, UNVERIFIABLE = "UNKNOWN", "UNVERIFIABLE"
VERDICTS = (SUPPORTED, CONTRADICTED, UNKNOWN, UNVERIFIABLE)

#: The predicates a claim may use. Anything else is UNVERIFIABLE: a claim
#: about something the state does not record cannot be checked against it.
STATE, VALUE = "state", "value"
PREDICATES = (STATE, VALUE)

#: States in which a recorded result is admitted.
ADMITTED = ("VERIFIED", "PROMOTED")


@dataclass(frozen=True)
class Claim:
    subject: str
    predicate: str
    value: object

    def to_record(self) -> dict:
        return {"subject": self.subject, "predicate": self.predicate,
                "value": self.value}


@dataclass(frozen=True)
class Verdict:
    verdict: str
    why: str
    #: What S and E hold for the subject and predicate, where they hold
    #: one: what a CONTRADICTED claim is replaced by.
    held: object = None


_TOKEN = re.compile(r"[A-Za-z0-9_.:-]+")
_SEQ = re.compile(r"\bseq (\d+)\b")


def refused_subjects(state, targets: dict | None = None) -> set:
    """Subjects named by a transition the replay refused, or an anomaly: the
    history around them is not one the gates produced. Matched as whole
    tokens, so ``rec-1`` is not found inside ``rec-10``.

    ``targets`` maps a seq to the target of the event there. A refusal that
    names only a seq -- an authenticator's does, since it judges events and
    not records -- refuses that event's target too."""
    ids = set(state.records)
    out: set = set()
    for line in list(state.unauthorized) + list(state.anomalies):
        out |= ids & set(_TOKEN.findall(line))
        if targets:
            out |= ids & {targets.get(int(n)) for n in _SEQ.findall(line)}
    return out


def held_value(record: dict, evidence):
    """The value the record's verification report holds, or why there is
    none. Read through the evidence store, which verifies what it returns."""
    dg = (record.get("evidence") or {}).get("verification_report")
    if not dg:
        return None, "the record cites no verification report"
    if evidence is None or not evidence.contains(dg):
        return None, (f"its verification report {dg[:12]} is missing or no "
                      "longer verifies")
    try:
        doc = json.loads(evidence.get(dg))
    except ValueError:
        return None, "its verification report is not a JSON document"
    if doc.get("record_id") != record.get("record_id"):
        return None, "its verification report names another record"
    return doc.get("value"), ""


def verify(claim: Claim, state, evidence, *, targets: dict | None = None
           ) -> Verdict:
    """V(C, S, E). ``evidence`` is the store, which re-hashes what it
    returns -- or, in the ablation, a reader of the bytes as they lie, which
    does not. ``targets``: see :func:`refused_subjects`."""
    if claim.predicate not in PREDICATES:
        return Verdict(UNVERIFIABLE,
                       f"no record holds a {claim.predicate!r}")
    record = state.records.get(claim.subject)
    if record is None:
        return Verdict(UNKNOWN, f"no record {claim.subject!r}")
    if claim.subject in refused_subjects(state, targets):
        return Verdict(UNVERIFIABLE,
                       f"a transition of {claim.subject!r} was refused on "
                       "replay; its history is not one the gates produced")
    if claim.predicate == STATE:
        held = record.get("state")
        return (Verdict(SUPPORTED, f"{claim.subject} is {held}", held)
                if held == claim.value else
                Verdict(CONTRADICTED, f"{claim.subject} is {held}", held))
    # VALUE: only an admitted result holds one.
    if record.get("state") not in ADMITTED:
        return Verdict(UNVERIFIABLE,
                       f"{claim.subject} is {record.get('state')}; only an "
                       "admitted result holds a value")
    value, why = held_value(record, evidence)
    if why:
        return Verdict(UNVERIFIABLE, f"{claim.subject}: {why}")
    return (Verdict(SUPPORTED, f"{claim.subject} holds {value}", value)
            if value == claim.value else
            Verdict(CONTRADICTED, f"{claim.subject} holds {value}", value))
