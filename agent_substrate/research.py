"""Research records: objectives, hypotheses, observations and what became
of them (Tranche H, Gate C). None of them is authority.

WHAT IS RECORDED

``research.objective``
    A question an owner asks, with the limits the work on it runs under:
    steps and model calls. Registered by its owner, in its owner's name.
    It may state its QUESTION as data -- the prediction it asks about and
    the documented point (the run parameters) it asks it at. Then an
    observation of it is read only at that point, from that model, and it
    is CONCLUDED only on a hypothesis making exactly that prediction: an
    agent's hypothesis that is easier to support, or a run somewhere easier
    to win, answers a different question (D-2026-179). An objective that
    states none is answered by any decided hypothesis of it.
``research.hypothesis``
    A statement under investigation, with a STRUCTURED prediction: a named
    output of a named model, a comparison, a value and its unit. Proposed by
    an agent (the proposal is cited), recorded by the controller, never by
    the agent itself. It may depend on other hypotheses and observations,
    and it may not depend on one that is no longer current.
``research.observation``
    What a VERIFIED or PROMOTED scientific record's result bundle says, read
    from the bundle -- never from a model's account of it. An observation is
    only as current as the record it was read from.
``research.status``
    What became of a record. For a hypothesis, SUPPORTED, REFUTED and
    INCONCLUSIVE are DECIDED HERE, deterministically, by comparing the
    prediction with the observations it names, the numerical resolution
    included: a model cannot declare its own hypothesis supported, and
    neither can the controller. RETRACTED, SUPERSEDED and STALE end a
    record's currency. An objective is CONCLUDED only on a decided,
    current hypothesis, and becomes STALE when that hypothesis does.
``research.freshness``
    How long observations of one source stay current, for one objective:
    a typed policy (:mod:`agent_substrate.freshness`) recorded by the
    objective's OWNER, in the owner's name, with its reason. One per
    source, never rewritten. A source with none is HISTORICAL.
``research.step``
    One step of the bounded controller (:mod:`agent_substrate.research_loop`):
    which proposal it acted on, what it did, and how that ended. A durable
    progress record, so a controller that restarts knows where it was from
    the log rather than from its own memory.

WHAT "CURRENT" MEANS

A record is current while its status is OPEN, SUPPORTED, REFUTED or
INCONCLUSIVE (a hypothesis), CURRENT (an observation), or OPEN or CONCLUDED
(an objective). STALE, EXPIRED, RETRACTED, SUPERSEDED, ABSTAINED and
EXHAUSTED are not current. :meth:`ResearchLedger.propagate` carries the loss
of currency along every dependency: a record whose source is no longer
VERIFIED or PROMOTED makes its observation STALE, which makes the hypotheses
decided on it STALE, which makes the conclusions drawn from them STALE.
Nothing is rewritten. A new status record says what changed, and the old ones
still say what was believed and when.

FRESHNESS (Assurance F)

An observation stays a valid record of what was observed, always. Whether it
may still decide anything is judged, at the time a decision is recorded,
under its source's freshness policy (:mod:`agent_substrate.freshness`): a
verdict and a conclusion rest only on observations FRESH or HISTORICAL at
their own time, judged at the latest time the history has reached. When an
observation's freshness runs out, :meth:`ResearchLedger.propagate` appends
EXPIRED for it -- the observation is authentic and outdated, not withdrawn
-- and the existing propagation carries that to what rested on it. A
re-observation renews a decided verdict instead: the same verdict, decided
again on a new basis, is a move only when evidence the old basis held has
lapsed. :meth:`ResearchLedger.currency` derives what the evidence of an
objective is NOW, and who decided how that is judged.

WHAT THIS IS NOT

A hypothesis SUPPORTED here is supported by a VERIFIED simulation result
under its admission policy, within that result's stated resolution. That is
not a measurement of the world (``evidence_terms.Property.CURRENT_WORLD``),
and the record says so by what it cites.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import freshness as F
from .canonical import digest, digest_bytes, is_digest
from .events import DecidedWriter, judged_at, projection_load

ACT_OBJECTIVE = "research.objective"
ACT_HYPOTHESIS = "research.hypothesis"
ACT_OBSERVATION = "research.observation"
ACT_STATUS = "research.status"
ACT_STEP = "research.step"
ACT_FRESHNESS = "research.freshness"
ACTIONS = (ACT_OBJECTIVE, ACT_HYPOTHESIS, ACT_OBSERVATION, ACT_STATUS,
           ACT_STEP, ACT_FRESHNESS)
CONTROLLER_ID = "research-controller"

#: What a source record must be for an observation read from it to be
#: current.
STANDING = frozenset({"VERIFIED", "PROMOTED"})
COMPARATORS = ("<", "<=", ">", ">=")

#: status -> the statuses a record may move to from it, per kind.
HYPOTHESIS_MOVES = {
    "OPEN": {"SUPPORTED", "REFUTED", "INCONCLUSIVE", "RETRACTED",
             "SUPERSEDED", "STALE"},
    # A decided verdict may be decided AGAIN, the same way, on a new basis
    # -- a re-observation renewing it -- but only once evidence its basis
    # held has lapsed (see _status_problem).
    "INCONCLUSIVE": {"INCONCLUSIVE", "SUPPORTED", "REFUTED", "RETRACTED",
                     "SUPERSEDED", "STALE"},
    # A decided hypothesis that new evidence disagrees with becomes
    # INCONCLUSIVE, never the opposite verdict in one move: the disagreement
    # is a fact to record, not something to resolve by the latest arrival.
    "SUPPORTED": {"SUPPORTED", "INCONCLUSIVE", "RETRACTED", "SUPERSEDED",
                  "STALE"},
    "REFUTED": {"REFUTED", "INCONCLUSIVE", "RETRACTED", "SUPERSEDED",
                "STALE"},
    "RETRACTED": set(), "SUPERSEDED": set(), "STALE": set(),
}
#: STALE: the record it was read from no longer stands. EXPIRED: it still
#: does, and the observation is authentic, but its freshness ran out.
OBSERVATION_MOVES = {"CURRENT": {"STALE", "EXPIRED"}, "STALE": set(),
                     "EXPIRED": set()}
OBJECTIVE_MOVES = {
    "OPEN": {"CONCLUDED", "ABSTAINED", "EXHAUSTED", "BLOCKED"},
    "BLOCKED": {"OPEN", "ABSTAINED", "EXHAUSTED"},
    "CONCLUDED": {"STALE"},
    "ABSTAINED": set(), "EXHAUSTED": set(), "STALE": set(),
}
CURRENT = {"hypothesis": {"OPEN", "SUPPORTED", "REFUTED", "INCONCLUSIVE"},
           "observation": {"CURRENT"},
           "objective": {"OPEN", "BLOCKED", "CONCLUDED"}}
#: The hypothesis statuses only the deterministic evaluation may write.
EVALUATED = frozenset({"SUPPORTED", "REFUTED", "INCONCLUSIVE"})

_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_OBJECTIVE_KEYS = {"objective_id", "statement", "owner", "parent_id",
                   "limits"}
#: An objective that states its question as data carries it too; one that
#: does not keeps the original key set.
_QUESTION_KEYS = {"prediction", "parameters"}
_LIMIT_KEYS = {"max_steps", "max_inference_calls"}
_HYPOTHESIS_KEYS = {"hypothesis_id", "objective_id", "statement",
                    "prediction", "depends_on", "proposal_id", "proposed_by"}
_PREDICTION_KEYS = {"model_id", "quantity", "op", "value", "unit"}
_OBSERVATION_KEYS = {"observation_id", "objective_id", "record_id",
                     "result_bundle", "model_id", "values"}
#: An observation whose source says when it was observed carries that time
#: too. One whose source does not keeps the original key set, so every
#: record written before Assurance F reads exactly as it did.
_OBSERVATION_TIMED = _OBSERVATION_KEYS | {"observed_at"}
#: Keys an observation carries when its bundle has them: when it was
#: observed, and the digest of the parameters it was run at.
_OBSERVATION_OPTIONAL = {"observed_at", "parameter_digest"}
_STEP_KEYS = {"objective_id", "step", "proposal_id", "act", "outcome",
              "detail"}


class ResearchError(Exception):
    """A research record, or a history of them, refused. Fail closed."""


def _id(value, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ResearchError(f"{what} is not an id: {value!r}")
    return value


def _exact(obj, keys, what: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != set(keys):
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise ResearchError(f"{what} must have exactly {sorted(keys)}, "
                            f"got {got}")
    return obj


def _text(value, what: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise ResearchError(f"{what}: 1..{limit} characters")
    return value


def _count(value, what: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or \
            value < minimum:
        raise ResearchError(f"{what} must be an int >= {minimum}")
    return value


def check_prediction(pred) -> dict:
    _exact(pred, _PREDICTION_KEYS, "prediction")
    _id(pred["model_id"], "prediction.model_id")
    _id(pred["quantity"], "prediction.quantity")
    if pred["op"] not in COMPARATORS:
        raise ResearchError(f"prediction.op must be one of {COMPARATORS}")
    v = pred["value"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or \
            not math.isfinite(v):
        raise ResearchError("prediction.value must be a finite number")
    _text(pred["unit"], "prediction.unit", 64)
    return pred


def evaluate(prediction: dict, observations: list) -> tuple:
    """(status, reason) of ``prediction`` against ``observations``.

    The one evaluation, used by the ledger that records a verdict and by the
    replay that checks it. An observation counts only if it is of the model
    the prediction names, reports the quantity by NAME and in the same unit:
    two outputs in kelvin at different places are different quantities, and
    a value in another unit is not converted here. The comparison takes the
    output's resolution into account. A value whose resolution interval
    straddles the threshold is INCONCLUSIVE, not a coin toss.
    """
    pred = check_prediction(prediction)
    usable = []
    for obs in observations:
        if obs.get("model_id") != pred["model_id"]:
            continue
        got = obs.get("values", {}).get(pred["quantity"])
        if got is None:
            continue
        if got.get("unit") != pred["unit"]:
            return ("INCONCLUSIVE", f"{pred['quantity']} is reported in "
                    f"{got.get('unit')!r} and predicted in {pred['unit']!r};"
                    " this evaluation converts nothing")
        usable.append(got)
    if not usable:
        return ("INCONCLUSIVE", f"no observation of {pred['model_id']} "
                f"reports {pred['quantity']}")
    verdicts = set()
    for got in usable:
        lo = got["value"] - got["resolution"]
        hi = got["value"] + got["resolution"]
        t = pred["value"]
        holds = {"<": hi < t, "<=": hi <= t, ">": lo > t, ">=": lo >= t}
        fails = {"<": lo >= t, "<=": lo > t, ">": hi <= t, ">=": hi < t}
        op = pred["op"]
        verdicts.add("SUPPORTED" if holds[op] else
                     "REFUTED" if fails[op] else "INCONCLUSIVE")
    if len(verdicts) > 1:
        return ("INCONCLUSIVE", f"the observations disagree: "
                f"{sorted(verdicts)}")
    verdict = verdicts.pop()
    return (verdict, f"{pred['quantity']} {pred['op']} {pred['value']} "
            f"{pred['unit']}: {verdict.lower()} by "
            f"{len(usable)} observation(s), resolution included")


def _said(pred: dict) -> str:
    return (f"{pred.get('model_id')} {pred.get('quantity')} "
            f"{pred.get('op')} {pred.get('value')} {pred.get('unit')}")


def _off_question(objective: dict, obs: dict) -> str:
    """Why ``obs`` cannot be an observation of ``objective``'s question, or
    "": it is of another model, or of a run at another point."""
    q = objective.get("question")
    if q is None:
        return ""
    if obs["model_id"] != q["prediction"]["model_id"]:
        return (f"is of {obs['model_id']!r}; the objective asks about "
                f"{q['prediction']['model_id']!r}")
    if obs.get("parameter_digest") != digest(q["parameters"]):
        return ("was run at parameters other than the documented point the "
                "objective asks about")
    return ""


def values_of(bundle: dict) -> dict:
    """The OK outputs of a result bundle: name -> value, unit, resolution."""
    out = {}
    for o in bundle.get("outputs", ()):
        q = o.get("quantity") or {}
        if o.get("status") != "OK" or not isinstance(q.get("value"),
                                                     (int, float)):
            continue
        out[o["name"]] = {"value": q["value"], "unit": q.get("unit"),
                          "resolution": q.get("resolution") or 0.0}
    return out


@dataclass(frozen=True)
class _Draft:
    """The event a writer is about to append, as the reducer will see it."""

    seq: int
    actor: str
    wall_time: float


@dataclass
class _Rec:
    kind: str
    body: dict
    status: str
    seq: int
    basis: tuple = ()
    reason: str = ""
    history: list = field(default_factory=list)


class ResearchLedger(DecidedWriter):
    """The research records of one log, projected and written.

    ``evidence``, when given, is the store result bundles are read from; an
    observation's values are re-read from it on replay as well as when they
    are recorded. Without it, replay checks everything but the values.
    """

    def __init__(self, log, *, actor: str = CONTROLLER_ID, evidence=None,
                 clock: Callable = time.time):
        self.log = log
        self.actor = _id(actor, "controller actor")
        self.evidence = evidence
        self.clock = clock
        self._fold_refused: BaseException | None = None
        self._clear()

    def _clear(self) -> None:
        self._recs: dict = {}
        #: record_id -> {"state", "evidence"}: the authority records as the
        #: log leaves them. Read, not judged: the store judges them.
        self._sci: dict = {}
        #: proposal_id -> (agent_id, kind), from the ingress's receipts
        self._proposals: dict = {}
        #: objective_id -> {step: record}
        self._steps: dict = {}
        #: (objective_id, model_id) -> {"body", "seq", "by"}: the freshness
        #: policy of one source, and who recorded it where
        self._freshness: dict = {}
        #: (objective_id, model_id) -> observation ids, in recording order
        self._by_source: dict = {}
        #: observation_id -> seconds since the epoch its source gives, or None
        self._observed_at: dict = {}
        #: (action, target) -> the latest seq recorded for it: what an
        #: UNTIL_EVENT policy's event is looked up in. One entry per distinct
        #: pair on the log, the price of answering for a pair named later.
        self._last_seen: dict = {}
        #: the latest wall time folded (events.judged_at)
        self._latest_wall: float | None = None
        self._reset_fold()

    @projection_load
    def load(self) -> "ResearchLedger":
        report, events = self.log.read_verified()
        report.raise_if_bad()
        self._clear()
        for ev in events:
            self.apply(ev)
        self._set_fold_anchor()
        return self

    # ---- the fold -------------------------------------------------------
    def apply(self, ev) -> bool:
        self._folded(ev.seq)
        self._latest_wall = judged_at(self._latest_wall, ev.wall_time)
        folded = self._dispatch(ev)
        # After the record itself is judged, so a writer -- which judges
        # against the history before its record -- and a replay agree.
        self._last_seen[(ev.action, ev.target)] = ev.seq
        return folded

    def _dispatch(self, ev) -> bool:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        a = ev.action
        if a == "record.create":
            self._sci[p.get("record_id")] = {
                "state": "PROPOSED", "evidence": dict(p.get("evidence")
                                                      or {})}
            return False
        if a == "record.transition":
            cur = self._sci.get(p.get("record_id"))
            if cur is not None:
                cur["state"] = p.get("dst")
                cur["evidence"].update(p.get("evidence") or {})
            return False
        if a == "proposal.receive":
            raw = p.get("envelope")
            env: dict = raw if isinstance(raw, dict) else {}
            self._proposals.setdefault(ev.target, (env.get("agent_id"),
                                                   env.get("kind")))
            return False
        if a == ACT_OBJECTIVE:
            self._apply_objective(ev, p)
        elif a == ACT_HYPOTHESIS:
            self._apply_hypothesis(ev, p)
        elif a == ACT_OBSERVATION:
            self._apply_observation(ev, p)
        elif a == ACT_STATUS:
            self._apply_status(ev, p)
        elif a == ACT_STEP:
            self._apply_step(ev, p)
        elif a == ACT_FRESHNESS:
            self._apply_freshness(ev, p)
        else:
            return False
        return True

    # Each _apply_* checks a record and, with ``commit``, folds it. A writer
    # calls the same function with ``commit=False`` under the writer lock,
    # so what may be written and what replay accepts are one rule, not two
    # copies that can drift: a writer with a missing check would otherwise
    # append a record its own replay then refuses.
    def _new(self, ev, rid: str, kind: str, body: dict, status: str,
             commit: bool) -> None:
        if rid in self._recs:
            raise ResearchError(f"seq {ev.seq}: {rid!r} recorded twice")
        if commit:
            self._recs[rid] = _Rec(kind, body, status, ev.seq)

    def _apply_objective(self, ev, p: dict, commit: bool = True) -> None:
        raw = p.get("objective")
        b = _exact(raw, _OBJECTIVE_KEYS | ({"question"} if isinstance(
            raw, dict) and "question" in raw else set()), "objective")
        if "question" in b:
            q = _exact(b["question"], _QUESTION_KEYS, "objective.question")
            check_prediction(q["prediction"])
            if not isinstance(q["parameters"], dict):
                raise ResearchError("objective.question.parameters must be "
                                    "an object: the documented point")
        oid = _id(b["objective_id"], "objective_id")
        _text(b["statement"], "objective.statement")
        _id(b["owner"], "objective.owner")
        lim = _exact(b["limits"], _LIMIT_KEYS, "objective.limits")
        _count(lim["max_steps"], "max_steps")
        _count(lim["max_inference_calls"], "max_inference_calls")
        if ev.actor != b["owner"]:
            raise ResearchError(f"seq {ev.seq}: {ev.actor!r} registered an "
                                f"objective in {b['owner']!r}'s name; an "
                                "objective is its owner's question")
        if b["parent_id"] is not None:
            parent = self._recs.get(b["parent_id"])
            if parent is None or parent.kind != "objective":
                raise ResearchError(f"seq {ev.seq}: a subgoal of "
                                    f"{b['parent_id']!r}, which is no "
                                    "objective")
        self._new(ev, oid, "objective", b, "OPEN", commit)

    def current(self, rid: str) -> bool:
        r = self._recs.get(rid)
        return r is not None and r.status in CURRENT[r.kind]

    def _apply_hypothesis(self, ev, p: dict, commit: bool = True) -> None:
        b = _exact(p.get("hypothesis"), _HYPOTHESIS_KEYS, "hypothesis")
        hid = _id(b["hypothesis_id"], "hypothesis_id")
        _text(b["statement"], "hypothesis.statement")
        check_prediction(b["prediction"])
        obj = self._recs.get(b["objective_id"])
        if obj is None or obj.kind != "objective" or obj.status != "OPEN":
            raise ResearchError(f"seq {ev.seq}: a hypothesis for "
                                f"{b['objective_id']!r}, which is not an "
                                "open objective")
        if not isinstance(b["depends_on"], list) or \
                len(b["depends_on"]) > 32:
            raise ResearchError("hypothesis.depends_on: at most 32 ids")
        for dep in b["depends_on"]:
            if not self.current(dep):
                raise ResearchError(
                    f"seq {ev.seq}: {hid!r} depends on {dep!r}, which is "
                    "not current; a hypothesis built on a withdrawn or "
                    "stale one inherits nothing but the withdrawal")
        got = self._proposals.get(b["proposal_id"])
        if got is None or got != (b["proposed_by"], "HYPOTHESIS"):
            raise ResearchError(f"seq {ev.seq}: {hid!r} cites proposal "
                                f"{b['proposal_id']!r}, which the ingress "
                                f"did not receive as {b['proposed_by']!r}'s "
                                "HYPOTHESIS")
        if ev.actor == b["proposed_by"]:
            raise ResearchError(f"seq {ev.seq}: {ev.actor!r} recorded its own"
                                " hypothesis; an agent proposes, the "
                                "controller records")
        self._new(ev, hid, "hypothesis", b, "OPEN", commit)

    def _apply_observation(self, ev, p: dict, commit: bool = True) -> None:
        raw = p.get("observation")
        b = _exact(raw, _OBSERVATION_KEYS | (
            _OBSERVATION_OPTIONAL & set(raw) if isinstance(raw, dict)
            else set()), "observation")
        oid = _id(b["observation_id"], "observation_id")
        observed = self._observed_time(ev, b)
        obj = self._recs.get(b["objective_id"])
        if obj is None or obj.kind != "objective":
            raise ResearchError(f"seq {ev.seq}: an observation for "
                                f"{b['objective_id']!r}, which is no "
                                "objective")
        why = _off_question(obj.body, b)
        if why:
            raise ResearchError(f"seq {ev.seq}: {oid!r} {why}")
        sci = self._sci.get(b["record_id"])
        if sci is None or sci["state"] not in STANDING:
            raise ResearchError(
                f"seq {ev.seq}: {oid!r} is read from {b['record_id']!r}, "
                f"which is {(sci or {}).get('state')!r}; an observation is "
                "read only from a VERIFIED or PROMOTED result")
        if not is_digest(b["result_bundle"]) or \
                sci["evidence"].get("result_bundle") != b["result_bundle"]:
            raise ResearchError(f"seq {ev.seq}: {oid!r} names a bundle its "
                                "record does not cite")
        if self.evidence is not None:
            bundle = json.loads(self.evidence.get(b["result_bundle"]))
            if values_of(bundle) != b["values"] or \
                    bundle.get("model_id") != b["model_id"] or \
                    bundle.get("observed_at") != b.get("observed_at") or \
                    bundle.get("parameter_digest") != \
                    b.get("parameter_digest"):
                raise ResearchError(f"seq {ev.seq}: {oid!r} reports values "
                                    "or a time its bundle does not hold")
        self._new(ev, oid, "observation", b, "CURRENT", commit)
        if commit:
            self._observed_at[oid] = observed
            self._by_source.setdefault(
                (b["objective_id"], b["model_id"]), []).append(oid)

    def _observed_time(self, ev, b: dict) -> float | None:
        """When the source says it observed, or None. Never later than the
        history has reached: an observation from the future is a source's
        clock that is wrong, and a freshness judged on it would be too."""
        if "observed_at" not in b:
            return None
        try:
            t = F.instant(b["observed_at"])
        except F.FreshnessError as exc:
            raise ResearchError(f"seq {ev.seq}: {exc}") from None
        now = judged_at(self._latest_wall, ev.wall_time)
        if t > now:
            raise ResearchError(
                f"seq {ev.seq}: {b['observation_id']!r} was observed at {t}, "
                f"after the {now} the history has reached")
        return t

    def _apply_freshness(self, ev, p: dict, commit: bool = True) -> None:
        try:
            b = F.check_policy(p.get("freshness"))
        except F.FreshnessError as exc:
            raise ResearchError(f"seq {ev.seq}: {exc}") from None
        obj = self._recs.get(b["objective_id"])
        if obj is None or obj.kind != "objective":
            raise ResearchError(f"seq {ev.seq}: a freshness policy for "
                                f"{b['objective_id']!r}, which is no "
                                "objective")
        if ev.actor != obj.body["owner"]:
            raise ResearchError(
                f"seq {ev.seq}: {ev.actor!r} set how fresh "
                f"{b['objective_id']!r}'s evidence must be; that is its "
                f"owner's ({obj.body['owner']!r}) determination")
        key = (b["objective_id"], b["model_id"])
        if key in self._freshness:
            raise ResearchError(
                f"seq {ev.seq}: {b['model_id']!r} already has a freshness "
                f"policy for {b['objective_id']!r}, recorded at seq "
                f"{self._freshness[key]['seq']}; a policy is not rewritten")
        if any(f["body"]["policy_id"] == b["policy_id"]
               for f in self._freshness.values()):
            raise ResearchError(f"seq {ev.seq}: freshness policy "
                                f"{b['policy_id']!r} recorded twice")
        if commit:
            self._freshness[key] = {"body": b, "seq": ev.seq,
                                    "by": ev.actor}

    def _standing(self, oid: str, at: float) -> tuple:
        """``(Standing, why)`` of observation ``oid`` at ``at``, from the
        history folded so far. No fold: replay asks this mid-fold."""
        rec = self._recs[oid]
        b = rec.body
        source = (b["objective_id"], b["model_id"])
        pol = self._freshness.get(source)
        body = pol["body"] if pol else None
        event_seq = replaced = None
        if body is not None and body["kind"] == F.Kind.UNTIL_EVENT.value:
            seen = self._last_seen.get((body["params"]["action"],
                                        body["params"]["target"]))
            event_seq = seen if seen is not None and seen > rec.seq else None
        t = self._observed_at.get(oid)
        if body is not None and body["kind"] == F.Kind.PERIODIC.value and \
                t is not None:
            later = sorted((self._observed_at[o], o)
                           for o in self._by_source.get(source, ())
                           if self._observed_at.get(o) is not None
                           and self._observed_at[o] > t)
            replaced = later[0][1] if later else None
        sci = self._sci.get(b["record_id"]) or {}
        return F.standing(body, at=at, observed_at=t,
                          source_stands=sci.get("state") in STANDING,
                          event_seq=event_seq, replaced_by=replaced)

    def _usable(self, oid: str, at: float) -> bool:
        return self.current(oid) and \
            self._standing(oid, at)[0] in F.USABLE

    def _status_problem(self, rec: _Rec, status: str, basis: list,
                        at: float) -> str:
        """Why ``status`` may not be written for ``rec`` at time ``at``, or
        ''."""
        moves = {"hypothesis": HYPOTHESIS_MOVES,
                 "observation": OBSERVATION_MOVES,
                 "objective": OBJECTIVE_MOVES}[rec.kind]
        if status not in moves.get(rec.status, set()):
            return f"{rec.kind} {rec.status} -> {status} is not a move"
        if rec.kind == "hypothesis" and status in EVALUATED:
            obs = [self._recs[o].body for o in basis
                   if o in self._recs and self._recs[o].kind == "observation"
                   and self.current(o)]
            if len(obs) != len(basis) or not basis:
                return ("a verdict cites observations that are not all "
                        "current observations")
            verdict, _ = evaluate(rec.body["prediction"], obs)
            if verdict != status:
                return (f"the observations give {verdict}, not {status}; a "
                        "verdict is computed, never asserted")
            for o in basis:
                standing, why = self._standing(o, at)
                if standing not in F.USABLE:
                    return (f"a verdict rests on {o!r}, which is "
                            f"{standing.value} at {at}: {why}")
            if status == rec.status:
                if set(basis) == set(rec.basis):
                    return "decided again on the same basis records nothing"
                if all(self._usable(o, at) for o in rec.basis):
                    return ("a verdict is decided again only once evidence "
                            "it rested on has lapsed")
        if rec.kind == "objective" and status == "CONCLUDED":
            if len(basis) != 1 or not self.current(basis[0]) or \
                    self._recs[basis[0]].kind != "hypothesis" or \
                    self._recs[basis[0]].status not in ("SUPPORTED",
                                                        "REFUTED") or \
                    self._recs[basis[0]].body["objective_id"] != \
                    rec.body["objective_id"]:
                return ("a conclusion rests on one current, decided "
                        "hypothesis of this objective")
            asked = (rec.body.get("question") or {}).get("prediction")
            made = self._recs[basis[0]].body["prediction"]
            if asked is not None and made != asked:
                return (f"a conclusion answers the question the objective "
                        f"asks: {basis[0]!r} predicts {_said(made)}, and "
                        f"the objective asks whether {_said(asked)}")
            lapsed = [o for o in self._recs[basis[0]].basis
                      if not self._usable(o, at)]
            if lapsed:
                return (f"a conclusion rests on {basis[0]!r}, whose evidence "
                        f"{lapsed} is not usable at {at}")
        if status == "EXPIRED":
            standing, why = self._standing(rec.body["observation_id"], at)
            if standing not in F.LAPSED:
                return (f"it is {standing.value} at {at}, not lapsed: {why}")
        if status == "STALE" and not self._lost_currency(rec):
            return "nothing it depends on has lost its currency"
        return ""

    def _lost_currency(self, rec: _Rec) -> bool:
        if rec.kind == "observation":
            sci = self._sci.get(rec.body["record_id"]) or {}
            return sci.get("state") not in STANDING
        if rec.kind == "objective":
            # a conclusion stands only while its hypothesis is current AND
            # still decided the way it was concluded on
            return any(self._recs.get(d) is None or
                       self._recs[d].status not in ("SUPPORTED", "REFUTED")
                       for d in rec.basis)
        deps = list(rec.basis)
        if rec.kind == "hypothesis":
            deps += rec.body["depends_on"]
        return any(not self.current(d) for d in deps)

    def _apply_status(self, ev, p: dict, commit: bool = True) -> None:
        rid, status = p.get("target_id"), p.get("status")
        rec = self._recs.get(rid)
        if rec is None:
            raise ResearchError(f"seq {ev.seq}: a status for {rid!r}, which "
                                "is not recorded")
        basis = p.get("basis")
        if not isinstance(basis, list) or not all(isinstance(x, str)
                                                  for x in basis):
            raise ResearchError(f"seq {ev.seq}: basis must be a list of ids")
        if not isinstance(status, str):
            raise ResearchError(f"seq {ev.seq}: status {status!r}")
        why = self._status_problem(rec, status, basis,
                                   judged_at(self._latest_wall, ev.wall_time))
        if why:
            raise ResearchError(f"seq {ev.seq}: {rid!r}: {why}")
        if not commit:
            return
        rec.history.append((ev.seq, rec.status))
        rec.status = status
        if status in EVALUATED or status == "CONCLUDED":
            rec.basis = tuple(basis)
        rec.reason = str(p.get("reason", ""))

    def _apply_step(self, ev, p: dict, commit: bool = True) -> None:
        b = _exact(p.get("step"), _STEP_KEYS, "step")
        obj = self._recs.get(b["objective_id"])
        if obj is None or obj.kind != "objective":
            raise ResearchError(f"seq {ev.seq}: a step of no objective")
        steps = self._steps.get(b["objective_id"], {})
        n = b["step"]
        if isinstance(n, bool) or n != len(steps) + 1:
            raise ResearchError(f"seq {ev.seq}: step {n!r} where "
                                f"{len(steps) + 1} is next")
        if n > obj.body["limits"]["max_steps"]:
            raise ResearchError(f"seq {ev.seq}: step {n} is past the "
                                "objective's limit")
        if commit:
            self._steps.setdefault(b["objective_id"], {})[n] = b

    # ---- reads ----------------------------------------------------------
    def get(self, rid: str) -> _Rec:
        self._fold_new()
        try:
            return self._recs[rid]
        except KeyError:
            raise ResearchError(f"no research record {rid!r}") from None

    def of(self, objective_id: str, kind: str) -> tuple:
        self._fold_new()
        return tuple(sorted(
            (rid for rid, r in self._recs.items() if r.kind == kind and
             (rid == objective_id if kind == "objective"
              else r.body.get("objective_id") == objective_id))))

    def steps(self, objective_id: str) -> dict:
        self._fold_new()
        return dict(self._steps.get(objective_id, {}))

    def snapshot(self, objective_id: str) -> dict:
        """The research state of one objective, as plain data: what a
        controller shows a model, and what a restarted one starts from."""
        self._fold_new()
        obj = self._recs[objective_id]

        def row(rid):
            r = self._recs[rid]
            return {"id": rid, "status": r.status, "basis": list(r.basis),
                    **{k: v for k, v in r.body.items()
                       if k in ("statement", "prediction", "depends_on",
                                "model_id", "values", "record_id",
                                "proposed_by")}}

        return {"objective": {"id": objective_id, "status": obj.status,
                              "statement": obj.body["statement"],
                              "basis": list(obj.basis),
                              **({"question": obj.body["question"]}
                                 if "question" in obj.body else {})},
                "hypotheses": [row(h) for h in self.of(objective_id,
                                                       "hypothesis")],
                "observations": [row(o) for o in self.of(objective_id,
                                                         "observation")],
                "steps": len(self._steps.get(objective_id, {}))}

    # ---- writes ---------------------------------------------------------
    def register(self, *, objective_id: str, statement: str, owner: str,
                 max_steps: int, max_inference_calls: int,
                 parent_id: str | None = None,
                 question: dict | None = None) -> None:
        body = {"objective_id": objective_id, "statement": statement,
                "owner": owner, "parent_id": parent_id,
                "limits": {"max_steps": max_steps,
                           "max_inference_calls": max_inference_calls}}
        if question is not None:
            body["question"] = question

        def decide(head_seq: int) -> dict:
            now = self.clock()
            payload = {"objective": body}
            self._apply_objective(_Draft(head_seq + 1, owner, now),
                                  payload, commit=False)
            return {"actor": owner, "action": ACT_OBJECTIVE,
                    "target": objective_id, "payload": payload,
                    "wall_time": now}

        self._append_decided(decide)

    def hypothesize(self, *, hypothesis_id: str, objective_id: str,
                    statement: str, prediction: dict, depends_on: list,
                    proposal_id: str, proposed_by: str) -> None:
        body = {"hypothesis_id": hypothesis_id, "objective_id": objective_id,
                "statement": statement, "prediction": prediction,
                "depends_on": list(depends_on), "proposal_id": proposal_id,
                "proposed_by": proposed_by}

        def decide(head_seq: int) -> dict:
            now = self.clock()
            payload = {"hypothesis": body}
            self._apply_hypothesis(_Draft(head_seq + 1, self.actor, now),
                                   payload, commit=False)
            return {"actor": self.actor, "action": ACT_HYPOTHESIS,
                    "target": hypothesis_id, "payload": payload,
                    "wall_time": now}

        self._append_decided(decide)

    def observe(self, *, observation_id: str, objective_id: str,
                record_id: str, bundle_bytes: bytes) -> dict:
        """Record what a VERIFIED result's bundle says. The values -- and
        when it was observed, if the bundle says -- are read from the bundle
        here; nobody's summary of it is accepted."""
        bundle = json.loads(bundle_bytes)
        body = {"observation_id": observation_id,
                "objective_id": objective_id, "record_id": record_id,
                "result_bundle": digest_bytes(bundle_bytes),
                "model_id": bundle.get("model_id"),
                "values": values_of(bundle)}
        for key in sorted(_OBSERVATION_OPTIONAL):
            if key in bundle:
                body[key] = bundle[key]

        def decide(head_seq: int) -> dict:
            now = self.clock()
            payload = {"observation": body}
            self._apply_observation(_Draft(head_seq + 1, self.actor, now),
                                    payload, commit=False)
            return {"actor": self.actor, "action": ACT_OBSERVATION,
                    "target": observation_id, "payload": payload,
                    "wall_time": now}

        self._append_decided(decide)
        return body

    def set_status(self, target_id: str, status: str, *, reason: str,
                   basis=()) -> None:
        basis = list(basis)

        def decide(head_seq: int) -> dict:
            now = self.clock()
            payload = {"target_id": target_id, "status": status,
                       "reason": str(reason)[:500], "basis": basis}
            self._apply_status(_Draft(head_seq + 1, self.actor, now),
                               payload, commit=False)
            return {"actor": self.actor, "action": ACT_STATUS,
                    "target": target_id, "payload": payload,
                    "wall_time": now}

        self._append_decided(decide)

    def evaluate_hypothesis(self, hypothesis_id: str) -> str:
        """Decide a hypothesis from every observation of its objective that
        may decide anything now, and record the verdict if it changed -- or,
        unchanged, if evidence its basis held has lapsed and other evidence
        renews it. Returns it."""
        self._fold_new()
        rec = self._recs[hypothesis_id]
        renewed = self._renewal(rec, judged_at(self._latest_wall,
                                               self.clock()))
        if renewed is not None:
            obs_ids, verdict, reason = renewed
            self.set_status(hypothesis_id, verdict,
                            reason=f"re-observed: {reason}", basis=obs_ids)
            return verdict
        at = judged_at(self._latest_wall, self.clock())
        obs_ids = [o for o in self.of(rec.body["objective_id"], "observation")
                   if self._usable(o, at)]
        verdict, reason = evaluate(rec.body["prediction"],
                                   [self._recs[o].body for o in obs_ids])
        if rec.status in CURRENT["hypothesis"] and verdict != rec.status \
                and obs_ids:
            self.set_status(hypothesis_id, verdict, reason=reason,
                            basis=obs_ids)
        return verdict

    def _renewal(self, rec: _Rec, at: float) -> tuple | None:
        """``(basis, verdict, reason)`` when a decided verdict whose basis
        has lapsed is decided the same way by the evidence usable at
        ``at``; otherwise None."""
        if rec.kind != "hypothesis" or rec.status not in EVALUATED or \
                all(self._usable(o, at) for o in rec.basis):
            return None
        obs_ids = [o for o in self.of(rec.body["objective_id"], "observation")
                   if self._usable(o, at)]
        if not obs_ids or set(obs_ids) == set(rec.basis):
            return None
        verdict, reason = evaluate(rec.body["prediction"],
                                   [self._recs[o].body for o in obs_ids])
        return (obs_ids, verdict, reason) if verdict == rec.status else None

    def record_freshness(self, *, policy_id: str, objective_id: str,
                         model_id: str, kind: str, params: dict,
                         rationale: str, actor: str) -> None:
        """Record how long observations of ``model_id`` stay current for
        ``objective_id``. The objective's owner's determination, recorded in
        the owner's name, once."""
        body = {"policy_id": policy_id, "objective_id": objective_id,
                "model_id": model_id, "kind": kind, "params": dict(params),
                "rationale": rationale}

        def decide(head_seq: int) -> dict:
            now = self.clock()
            payload = {"freshness": body}
            self._apply_freshness(_Draft(head_seq + 1, actor, now), payload,
                                  commit=False)
            return {"actor": actor, "action": ACT_FRESHNESS,
                    "target": policy_id, "payload": payload,
                    "wall_time": now}

        self._append_decided(decide)

    def record_step(self, *, objective_id: str, step: int,
                    proposal_id: str | None, act: str, outcome: str,
                    detail: str = "") -> None:
        body = {"objective_id": objective_id, "step": step,
                "proposal_id": proposal_id, "act": act,
                "outcome": outcome, "detail": str(detail)[:500]}

        def decide(head_seq: int) -> dict:
            now = self.clock()
            payload = {"step": body}
            self._apply_step(_Draft(head_seq + 1, self.actor, now),
                             payload, commit=False)
            return {"actor": self.actor, "action": ACT_STEP,
                    "target": objective_id, "payload": payload,
                    "wall_time": now}

        self._append_decided(decide)

    def propagate(self) -> list:
        """Expire what has run out of freshness, renew what re-observation
        renews, and carry lost currency along every dependency, to a fixed
        point. Returns the ids made EXPIRED or STALE, in the order they
        were."""
        moved: list = []
        while True:
            self._fold_new()
            at = judged_at(self._latest_wall, self.clock())
            lapsed = sorted(
                o for o, r in self._recs.items()
                if r.kind == "observation" and r.status == "CURRENT"
                and self._standing(o, at)[0] in F.LAPSED)
            if lapsed:
                oid = lapsed[0]
                standing, why = self._standing(oid, at)
                self.set_status(oid, "EXPIRED", reason=f"{standing.value}: "
                                f"{why}{self._determined_by(oid)}")
                moved.append(oid)
                continue
            renewals = {
                h: ren for h, r in self._recs.items()
                if r.kind == "hypothesis"
                and not any(not self.current(d)
                            for d in r.body["depends_on"])
                and (ren := self._renewal(r, at)) is not None}
            if renewals:
                hid = min(renewals)
                obs_ids, verdict, reason = renewals[hid]
                self.set_status(hid, verdict, reason=f"re-observed: "
                                f"{reason}", basis=obs_ids)
                continue
            order = {"observation": 0, "hypothesis": 1, "objective": 2}
            todo = sorted((rid for rid, r in self._recs.items()
                           if r.status in CURRENT[r.kind]
                           and "STALE" in {"hypothesis": HYPOTHESIS_MOVES,
                                           "observation": OBSERVATION_MOVES,
                                           "objective": OBJECTIVE_MOVES}[
                                               r.kind].get(r.status, set())
                           and self._lost_currency(r)),
                          key=lambda rid: (order[self._recs[rid].kind], rid))
            if not todo:
                return moved
            rid = todo[0]
            self.set_status(rid, "STALE", reason="something it depends on "
                            "is no longer current")
            moved.append(rid)

    def _determined_by(self, oid: str) -> str:
        b = self._recs[oid].body
        pol = self._freshness.get((b["objective_id"], b["model_id"]))
        return "" if pol is None else \
            f"; policy recorded by {pol['by']} at seq {pol['seq']}"

    def currency(self, objective_id: str) -> dict:
        """What an objective's evidence is NOW, derived -- never written onto
        the observations, whose records keep saying what was observed.

        Every observation with its record status, its standing at the time
        judged, why, and the policy that judged it and who recorded that;
        every decided hypothesis with the weakest standing among the
        observations its verdict actually used. A verdict whose evidence is
        all FRESH is current under its owner's policies; one resting on
        HISTORICAL evidence says what was observed, not what holds; one
        resting on UNKNOWN or WITHDRAWN evidence is a record of a past
        belief."""
        self._fold_new()
        at = judged_at(self._latest_wall, self.clock())
        observations = {}
        for oid in self.of(objective_id, "observation"):
            r = self._recs[oid]
            standing, why = self._standing(oid, at)
            pol = self._freshness.get((objective_id, r.body["model_id"]))
            observations[oid] = {
                "status": r.status, "standing": standing.value, "why": why,
                "observed_at": r.body.get("observed_at"),
                "policy": None if pol is None else {
                    "policy_id": pol["body"]["policy_id"],
                    "kind": pol["body"]["kind"],
                    "recorded_by": pol["by"], "recorded_at_seq": pol["seq"]}}
        weakest = [F.Standing.WITHDRAWN, F.Standing.UNKNOWN,
                   F.Standing.SUPERSEDED, F.Standing.HISTORICAL,
                   F.Standing.FRESH]
        hypotheses = {}
        for hid in self.of(objective_id, "hypothesis"):
            r = self._recs[hid]
            pred = r.body["prediction"]
            used = [F.Standing(observations[o]["standing"]) for o in r.basis
                    if o in observations
                    and self._recs[o].body["model_id"] == pred["model_id"]
                    and pred["quantity"] in self._recs[o].body["values"]]
            hypotheses[hid] = {
                "status": r.status,
                "evidence": min(used, key=weakest.index).value
                if r.status in EVALUATED and used else None}
        return {"objective_id": objective_id, "at": at,
                "observations": observations, "hypotheses": hypotheses}
