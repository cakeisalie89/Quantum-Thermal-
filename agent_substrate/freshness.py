"""Freshness: what an observation was is history; whether it still holds is
another question, answered at a stated time under a policy someone set
(Assurance F).

HISTORICAL VALIDITY AND CURRENT VALIDITY

An observation recorded from a VERIFIED result stays valid as a record of
what was observed -- or computed -- for as long as the log exists. Nothing
here changes its bytes, its values or the verdicts recorded on it. Whether
the condition it observed STILL holds is a different question, and its
answer depends on what was observed: a model's computed output does not
decay, a temperature reading does. So the answer is never written into the
observation. It is DERIVED, at a stated time, under the freshness policy
the objective's OWNER recorded for the observation's source, and the
research ledger appends a status record when the answer changes
(:mod:`agent_substrate.research`). The history keeps saying what was
believed and when.

THE POLICIES -- AND NO UNIVERSAL TTL

``HISTORICAL``
    Valid indefinitely as a record; it says nothing about now. The kind of a
    source the owner set no policy for, because a time limit nobody chose is
    a guess presented as a rule. A simulation's output is this.
``SOURCE_WITHDRAWAL``
    Current until its source stops standing; no clock.
``UNTIL_EVENT``
    Current until a record with a named action and target is appended after
    it (a recalibration, a new version of a policy).
``INTERVAL``
    Current for ``seconds`` after it was observed.
``PERIODIC``
    Current for ``seconds`` after it was observed, and only until a later
    observation of the same source replaces it: a series that must be
    re-observed.

Every kind is also ended by its source's withdrawal -- the record it was
read from leaving VERIFIED or PROMOTED -- which the ledger already carries
as STALE.

WHAT AN OBSERVATION IS, NOW (:class:`Standing`)

``FRESH``       within its policy at the time judged.
``HISTORICAL``  authentic; its policy claims nothing about now.
``UNKNOWN``     its freshness ran out. Nothing on record says the condition
                still holds, or that it does not: never "false".
``SUPERSEDED``  a later observation of its periodic series replaced it.
``WITHDRAWN``   its source no longer stands.

FRESH is not ``evidence_terms.Property.CURRENT_WORLD``. It says the owner's
policy holds the observation current at that time: an assumption about how
fast the world changes, stated and attributed, not a measurement of the
world now.

TIME

Judged at :func:`agent_substrate.events.judged_at`: a clock set back does
not make an expired observation fresh again (D-2026-168). An observation's
time is what its SOURCE says -- ``observed_at`` in the bundle, ISO 8601
with a UTC offset -- never what the recorder says. One with no time is
UNKNOWN under a policy that needs one, and one stamped later than the
history has reached is refused where it is recorded.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from enum import Enum


class Kind(str, Enum):
    HISTORICAL = "HISTORICAL"
    SOURCE_WITHDRAWAL = "SOURCE_WITHDRAWAL"
    UNTIL_EVENT = "UNTIL_EVENT"
    INTERVAL = "INTERVAL"
    PERIODIC = "PERIODIC"


class Standing(str, Enum):
    FRESH = "FRESH"
    HISTORICAL = "HISTORICAL"
    UNKNOWN = "UNKNOWN"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


#: What a decision may rest on: an observation within its policy, or one
#: whose policy claims nothing about now (and the decision then says no more
#: than its evidence did).
USABLE = frozenset({Standing.FRESH, Standing.HISTORICAL})
#: What an observation's EXPIRED status record must be justified by.
LAPSED = frozenset({Standing.UNKNOWN, Standing.SUPERSEDED})

#: kind -> the parameters it takes, exactly.
PARAMS = {Kind.HISTORICAL: frozenset(),
          Kind.SOURCE_WITHDRAWAL: frozenset(),
          Kind.UNTIL_EVENT: frozenset({"action", "target"}),
          Kind.INTERVAL: frozenset({"seconds"}),
          Kind.PERIODIC: frozenset({"seconds"})}
POLICY_KEYS = frozenset({"policy_id", "objective_id", "model_id", "kind",
                         "params", "rationale"})

_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
#: The one form of instant accepted: seconds, an optional fraction of up to
#: six digits, and ``Z`` or an offset. ``datetime.fromisoformat`` reads far
#: more (basic formats, week dates, fractional hours); the second reader
#: restates this form without it, so both read exactly the same strings.
_INSTANT = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?"
                      r"(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z")


class FreshnessError(ValueError):
    """A freshness policy or an observation time refused. Fail closed."""


def check_policy(body) -> dict:
    """``body`` if it is a well-formed policy, else :class:`FreshnessError`.

    Exactly the keys of :data:`POLICY_KEYS`; ``params`` exactly the keys its
    kind takes; a duration a positive, finite number of seconds; a reason
    stated, because a policy nobody explained is not one anybody can
    review."""
    if not isinstance(body, dict) or set(body) != POLICY_KEYS:
        got = sorted(body) if isinstance(body, dict) else type(body).__name__
        raise FreshnessError(f"a freshness policy has exactly "
                             f"{sorted(POLICY_KEYS)}, got {got}")
    for key in ("policy_id", "objective_id", "model_id"):
        if not isinstance(body[key], str) or not _ID.match(body[key]):
            raise FreshnessError(f"freshness.{key} is not an id: "
                                 f"{body[key]!r}")
    try:
        kind = Kind(body["kind"])
    except ValueError:
        raise FreshnessError(f"freshness.kind {body['kind']!r} is not one "
                             f"of {[k.value for k in Kind]}") from None
    params = body["params"]
    if not isinstance(params, dict) or set(params) != PARAMS[kind]:
        raise FreshnessError(f"a {kind.value} policy takes exactly "
                             f"{sorted(PARAMS[kind])} as params")
    if "seconds" in params:
        s = params["seconds"]
        if isinstance(s, bool) or not isinstance(s, (int, float)) or \
                not math.isfinite(s) or s <= 0:
            raise FreshnessError("freshness.params.seconds must be a "
                                 "positive, finite number")
    for key in ("action", "target"):
        if key in params and (not isinstance(params[key], str)
                              or not params[key].strip()):
            raise FreshnessError(f"freshness.params.{key} must name one")
    if not isinstance(body["rationale"], str) or \
            not 0 < len(body["rationale"].strip()) <= 500:
        raise FreshnessError("a freshness policy states why: 1..500 "
                             "characters of rationale")
    return body


def instant(text) -> float:
    """Seconds since the epoch of an ISO 8601 instant WITH its UTC offset,
    in the one form :data:`_INSTANT` accepts.

    A naive time is refused: two sources' naive times cannot be put in one
    order, and a freshness judged on one is judged on a guess."""
    if not isinstance(text, str) or not _INSTANT.match(text):
        raise FreshnessError(f"observed_at {text!r} is not an instant of "
                             "the form YYYY-MM-DDTHH:MM:SS[.ffffff] with Z "
                             "or a +HH:MM offset")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise FreshnessError(f"observed_at {text!r} is not an ISO 8601 "
                             "instant") from None
    if when.utcoffset() is None:
        raise FreshnessError(f"observed_at {text!r} has no UTC offset")
    return when.timestamp()


def standing(policy: dict | None, *, at: float, observed_at: float | None,
             source_stands: bool, event_seq: int | None = None,
             replaced_by: str | None = None) -> tuple:
    """``(Standing, why)`` of one observation at time ``at``.

    ``policy`` is the body of the freshness policy in force for its source,
    or None (HISTORICAL). ``event_seq`` is where an UNTIL_EVENT policy's
    event was recorded after the observation, if it was; ``replaced_by`` a
    later observation of the same source, if there is one. Pure: the caller
    says what the history holds, and this says what follows from it."""
    if not source_stands:
        return Standing.WITHDRAWN, "the record it was read from no longer " \
                                   "stands"
    kind = Kind.HISTORICAL if policy is None else Kind(policy["kind"])
    named = "no freshness policy" if policy is None else \
        f"policy {policy['policy_id']} ({kind.value})"
    if kind is Kind.HISTORICAL:
        return Standing.HISTORICAL, f"{named}: a record of what was " \
                                    "observed, claiming nothing about now"
    if kind is Kind.SOURCE_WITHDRAWAL:
        return Standing.FRESH, f"{named}: its source stands"
    if kind is Kind.UNTIL_EVENT:
        p = policy["params"]                   # type: ignore[index]
        if event_seq is not None:
            return Standing.UNKNOWN, (f"{named}: {p['action']} on "
                                      f"{p['target']} was recorded at seq "
                                      f"{event_seq}")
        return Standing.FRESH, f"{named}: {p['action']} on {p['target']} " \
                               "has not been recorded since"
    if observed_at is None:
        return Standing.UNKNOWN, f"{named}: its source does not say when " \
                                 "it was observed"
    if kind is Kind.PERIODIC and replaced_by is not None:
        return Standing.SUPERSEDED, f"{named}: re-observed as {replaced_by}"
    until = observed_at + policy["params"]["seconds"]  # type: ignore[index]
    if at > until:
        return Standing.UNKNOWN, (f"{named}: observed at {observed_at}, "
                                  f"current until {until}; the history has "
                                  f"reached {at}")
    return Standing.FRESH, f"{named}: observed at {observed_at}, current " \
                           f"until {until}"
