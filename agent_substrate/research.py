"""Research records: objectives, hypotheses, observations and what became
of them (Tranche H, Gate C). None of them is authority.

WHAT IS RECORDED

``research.objective``
    A question an owner asks, with the limits the work on it runs under:
    steps and model calls. Registered by its owner, in its owner's name.
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
``research.step``
    One step of the bounded controller (:mod:`agent_substrate.research_loop`):
    which proposal it acted on, what it did, and how that ended. A durable
    progress record, so a controller that restarts knows where it was from
    the log rather than from its own memory.

WHAT "CURRENT" MEANS

A record is current while its status is OPEN, SUPPORTED, REFUTED or
INCONCLUSIVE (a hypothesis), CURRENT (an observation), or OPEN or CONCLUDED
(an objective). STALE, RETRACTED, SUPERSEDED, ABSTAINED and EXHAUSTED are
not current. :meth:`ResearchLedger.propagate` carries the loss of currency
along every dependency: a record whose source is no longer VERIFIED or
PROMOTED makes its observation STALE, which makes the hypotheses decided on it
STALE, which makes the conclusions drawn from them STALE. Nothing is
rewritten. A new status record says what changed, and the old ones still say
what was believed and when.

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
from dataclasses import dataclass, field

from .canonical import digest_bytes, is_digest
from .events import DecidedWriter, projection_load

ACT_OBJECTIVE = "research.objective"
ACT_HYPOTHESIS = "research.hypothesis"
ACT_OBSERVATION = "research.observation"
ACT_STATUS = "research.status"
ACT_STEP = "research.step"
ACTIONS = (ACT_OBJECTIVE, ACT_HYPOTHESIS, ACT_OBSERVATION, ACT_STATUS,
           ACT_STEP)
CONTROLLER_ID = "research-controller"

#: What a source record must be for an observation read from it to be
#: current.
STANDING = frozenset({"VERIFIED", "PROMOTED"})
COMPARATORS = ("<", "<=", ">", ">=")

#: status -> the statuses a record may move to from it, per kind.
HYPOTHESIS_MOVES = {
    "OPEN": {"SUPPORTED", "REFUTED", "INCONCLUSIVE", "RETRACTED",
             "SUPERSEDED", "STALE"},
    "INCONCLUSIVE": {"SUPPORTED", "REFUTED", "RETRACTED", "SUPERSEDED",
                     "STALE"},
    # A decided hypothesis that new evidence disagrees with becomes
    # INCONCLUSIVE, never the opposite verdict in one move: the disagreement
    # is a fact to record, not something to resolve by the latest arrival.
    "SUPPORTED": {"INCONCLUSIVE", "RETRACTED", "SUPERSEDED", "STALE"},
    "REFUTED": {"INCONCLUSIVE", "RETRACTED", "SUPERSEDED", "STALE"},
    "RETRACTED": set(), "SUPERSEDED": set(), "STALE": set(),
}
OBSERVATION_MOVES = {"CURRENT": {"STALE"}, "STALE": set()}
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
_LIMIT_KEYS = {"max_steps", "max_inference_calls"}
_HYPOTHESIS_KEYS = {"hypothesis_id", "objective_id", "statement",
                    "prediction", "depends_on", "proposal_id", "proposed_by"}
_PREDICTION_KEYS = {"model_id", "quantity", "op", "value", "unit"}
_OBSERVATION_KEYS = {"observation_id", "objective_id", "record_id",
                     "result_bundle", "model_id", "values"}
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

    def __init__(self, log, *, actor: str = CONTROLLER_ID, evidence=None):
        self.log = log
        self.actor = _id(actor, "controller actor")
        self.evidence = evidence
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
        b = _exact(p.get("objective"), _OBJECTIVE_KEYS, "objective")
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
        b = _exact(p.get("observation"), _OBSERVATION_KEYS, "observation")
        oid = _id(b["observation_id"], "observation_id")
        obj = self._recs.get(b["objective_id"])
        if obj is None or obj.kind != "objective":
            raise ResearchError(f"seq {ev.seq}: an observation for "
                                f"{b['objective_id']!r}, which is no "
                                "objective")
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
                    bundle.get("model_id") != b["model_id"]:
                raise ResearchError(f"seq {ev.seq}: {oid!r} reports values "
                                    "its bundle does not hold")
        self._new(ev, oid, "observation", b, "CURRENT", commit)

    def _status_problem(self, rec: _Rec, status: str, basis: list) -> str:
        """Why ``status`` may not be written for ``rec``, or ''."""
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
        if rec.kind == "objective" and status == "CONCLUDED":
            if len(basis) != 1 or not self.current(basis[0]) or \
                    self._recs[basis[0]].kind != "hypothesis" or \
                    self._recs[basis[0]].status not in ("SUPPORTED",
                                                        "REFUTED") or \
                    self._recs[basis[0]].body["objective_id"] != \
                    rec.body["objective_id"]:
                return ("a conclusion rests on one current, decided "
                        "hypothesis of this objective")
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
        why = self._status_problem(rec, status, basis)
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
                                "model_id", "values", "record_id")}}

        return {"objective": {"id": objective_id, "status": obj.status,
                              "statement": obj.body["statement"],
                              "basis": list(obj.basis)},
                "hypotheses": [row(h) for h in self.of(objective_id,
                                                       "hypothesis")],
                "observations": [row(o) for o in self.of(objective_id,
                                                         "observation")],
                "steps": len(self._steps.get(objective_id, {}))}

    # ---- writes ---------------------------------------------------------
    def register(self, *, objective_id: str, statement: str, owner: str,
                 max_steps: int, max_inference_calls: int,
                 parent_id: str | None = None) -> None:
        body = {"objective_id": objective_id, "statement": statement,
                "owner": owner, "parent_id": parent_id,
                "limits": {"max_steps": max_steps,
                           "max_inference_calls": max_inference_calls}}

        def decide(head_seq: int) -> dict:
            payload = {"objective": body}
            self._apply_objective(_Draft(head_seq + 1, owner), payload,
                                  commit=False)
            return {"actor": owner, "action": ACT_OBJECTIVE,
                    "target": objective_id, "payload": payload}

        self._append_decided(decide)

    def hypothesize(self, *, hypothesis_id: str, objective_id: str,
                    statement: str, prediction: dict, depends_on: list,
                    proposal_id: str, proposed_by: str) -> None:
        body = {"hypothesis_id": hypothesis_id, "objective_id": objective_id,
                "statement": statement, "prediction": prediction,
                "depends_on": list(depends_on), "proposal_id": proposal_id,
                "proposed_by": proposed_by}

        def decide(head_seq: int) -> dict:
            payload = {"hypothesis": body}
            self._apply_hypothesis(_Draft(head_seq + 1, self.actor), payload,
                                   commit=False)
            return {"actor": self.actor, "action": ACT_HYPOTHESIS,
                    "target": hypothesis_id, "payload": payload}

        self._append_decided(decide)

    def observe(self, *, observation_id: str, objective_id: str,
                record_id: str, bundle_bytes: bytes) -> dict:
        """Record what a VERIFIED result's bundle says. The values are read
        from the bundle here; nobody's summary of it is accepted."""
        bundle = json.loads(bundle_bytes)
        body = {"observation_id": observation_id,
                "objective_id": objective_id, "record_id": record_id,
                "result_bundle": digest_bytes(bundle_bytes),
                "model_id": bundle.get("model_id"),
                "values": values_of(bundle)}

        def decide(head_seq: int) -> dict:
            payload = {"observation": body}
            self._apply_observation(_Draft(head_seq + 1, self.actor),
                                    payload, commit=False)
            return {"actor": self.actor, "action": ACT_OBSERVATION,
                    "target": observation_id, "payload": payload}

        self._append_decided(decide)
        return body

    def set_status(self, target_id: str, status: str, *, reason: str,
                   basis=()) -> None:
        basis = list(basis)

        def decide(head_seq: int) -> dict:
            payload = {"target_id": target_id, "status": status,
                       "reason": str(reason)[:500], "basis": basis}
            self._apply_status(_Draft(head_seq + 1, self.actor), payload,
                               commit=False)
            return {"actor": self.actor, "action": ACT_STATUS,
                    "target": target_id, "payload": payload}

        self._append_decided(decide)

    def evaluate_hypothesis(self, hypothesis_id: str) -> str:
        """Decide a hypothesis from every current observation of its
        objective, and record the verdict if it changed. Returns it."""
        self._fold_new()
        rec = self._recs[hypothesis_id]
        obs_ids = [o for o in self.of(rec.body["objective_id"], "observation")
                   if self.current(o)]
        verdict, reason = evaluate(rec.body["prediction"],
                                   [self._recs[o].body for o in obs_ids])
        if rec.status in CURRENT["hypothesis"] and verdict != rec.status \
                and obs_ids:
            self.set_status(hypothesis_id, verdict, reason=reason,
                            basis=obs_ids)
        return verdict

    def record_step(self, *, objective_id: str, step: int,
                    proposal_id: str | None, act: str, outcome: str,
                    detail: str = "") -> None:
        body = {"objective_id": objective_id, "step": step,
                "proposal_id": proposal_id, "act": act,
                "outcome": outcome, "detail": str(detail)[:500]}

        def decide(head_seq: int) -> dict:
            payload = {"step": body}
            self._apply_step(_Draft(head_seq + 1, self.actor), payload,
                             commit=False)
            return {"actor": self.actor, "action": ACT_STEP,
                    "target": objective_id, "payload": payload}

        self._append_decided(decide)

    def propagate(self) -> list:
        """Carry lost currency along every dependency, to a fixed point.
        Returns the ids made STALE, in the order they were."""
        moved: list = []
        while True:
            self._fold_new()
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
