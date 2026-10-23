"""Semantic claim verification (section 6.3): what the evidence says about a
claim, kept apart from whether the evidence is intact and whether it was
admitted.

THREE QUESTIONS, ANSWERED SEPARATELY AND NEVER MERGED

citation
    Does each observation the claim cites exist, and do the result bytes it
    was read from still re-hash and still say what the observation recorded?
    A forged or altered citation is caught here, and says nothing about
    whether the claim is true.
admission
    Is each observation read from a result that is admitted now -- its
    authority record VERIFIED or PROMOTED (and, for a scientific result,
    admitted by this reader) -- current in the research ledger, and usable
    under its owner's freshness policy? Evidence that is not admitted is not
    weighed, however well it is cited.
semantics
    Does what the ADMITTED evidence says support what the claim says? One of
    five answers:

    SUPPORTED
        every admitted observation that reports the quantity says so.
    CONTRADICTED
        every one says otherwise.
    INSUFFICIENT_EVIDENCE
        none reports it, or an output's resolution straddles the claim: the
        evidence could decide it and does not.
    UNVERIFIABLE
        nothing this system holds could decide it as stated: the text is
        not a claim the closed grammar reads, or it is in a unit the
        evidence is not in (nothing is converted).
    CONFLICTING_EVIDENCE
        admitted observations decide it both ways.

    The semantic answer is judged against EVERY admitted observation of the
    objective, not only the ones the claim chose to cite: a claim cannot
    avoid a contradicting measurement by leaving it out.

TWO WAYS TO ANSWER THE THIRD QUESTION

DETERMINISTIC
    The claim is read, by a closed grammar and nothing else, into the form a
    research prediction takes -- one named output of one model, a
    comparison, a number and a unit -- and judged by
    :func:`agent_substrate.research.evaluate`, the evaluation the research
    ledger records verdicts with, one observation at a time. A sentence the
    grammar does not read in FULL is not read at all: "below 350 K, so the
    design is safe" is not "below 350 K".
ASSESSED
    Text the grammar does not read may be given to a checker -- a model, or
    a heuristic. Its answer is a MODEL_OUTPUT opinion: reported, never an
    established property (:func:`agent_substrate.trust_labels.establish`
    refuses an ASSESSED check), and read by nothing that decides.

WHAT THIS IS NOT

An authority. Nothing in the substrate calls it to decide anything; a
SUPPORTED claim permits nothing and promotes nothing. Its label carries
SEMANTICALLY_SUPPORTED only when the deterministic route said SUPPORTED and
every citation verified -- and even then it says the admitted evidence
supports the claim, not that the world is so (CURRENT_WORLD is never
established here).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum

from .evidence_terms import Property
from .freshness import USABLE, Standing
from .research import COMPARATORS, evaluate, values_of
from .result_rules import ADMITTED
from .trust_labels import Label, Origin, establish


class Verdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNVERIFIABLE = "UNVERIFIABLE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class Basis(str, Enum):
    #: the closed grammar read the claim and research.evaluate judged it
    DETERMINISTIC = "DETERMINISTIC"
    #: a checker's opinion: fallible, non-authoritative, never established
    ASSESSED = "ASSESSED"
    #: nothing judged it
    NONE = "NONE"


#: The registered checks (evidence_terms.CHECKS) this module answers with.
JUDGE = "agent_substrate.claim_check:judge"
ASSESS = "agent_substrate.claim_check:assess_with"
BYTES = "agent_substrate.evidence:EvidenceStore.contains"


# ------------------------------------------------------- the closed grammar

@dataclass(frozen=True)
class Structured:
    """A claim in the form research predictions take; ``op`` is one of
    research.COMPARATORS or "=" (the value lies within the output's
    resolution)."""

    quantity: str
    model_id: str
    op: str
    value: float
    unit: str


_PHRASES = (
    ("at most", "<="), ("no more than", "<="), ("at least", ">="),
    ("no less than", ">="), ("less than", "<"), ("below", "<"),
    ("under", "<"), ("greater than", ">"), ("more than", ">"),
    ("above", ">"), ("over", ">"), ("equal to", "="), ("<=", "<="),
    (">=", ">="), ("<", "<"), (">", ">"), ("=", "="),
)
_ID = r"[A-Za-z][A-Za-z0-9_.-]*"
#: The phrases as alternatives, any run of whitespace between their words.
_CMP = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p, _ in _PHRASES)
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_GRAMMAR = re.compile(
    rf"\s*(?:the\s+)?(?P<quantity>{_ID})\s+(?:of|from)\s+(?P<model>{_ID})\s+"
    rf"(?:(?:is|was|stays|stayed|remains|remained)\s+)?"
    rf"(?:(?P<cmp>{_CMP})\s+)?"
    rf"(?P<value>{_NUM})\s*(?P<unit>[^\s.,;:]+)\s*\.?\s*",
    re.IGNORECASE)


def read_claim(text: str) -> Structured | None:
    """The structured claim ``text`` states, or None. The WHOLE text must be
    one sentence of the grammar; anything more -- a reason, a consequence,
    a hedge -- and nothing is read."""
    if not isinstance(text, str):
        return None
    m = _GRAMMAR.fullmatch(text)
    if m is None:
        return None
    value = float(m["value"])
    if not math.isfinite(value):
        return None
    phrase = " ".join((m["cmp"] or "=").lower().split())
    op = dict(_PHRASES)[phrase]
    return Structured(quantity=m["quantity"], model_id=m["model"], op=op,
                      value=value, unit=m["unit"])


def judge(claim: Structured, observations: list) -> tuple:
    """``(Verdict, why, used)``: ``claim`` against admitted ``observations``
    (research observation bodies). ``used`` names the observations that
    report the quantity."""
    usable = [o for o in observations
              if o.get("model_id") == claim.model_id
              and claim.quantity in (o.get("values") or {})]
    if not usable:
        return (Verdict.INSUFFICIENT_EVIDENCE,
                f"no admitted observation of {claim.model_id} reports "
                f"{claim.quantity}", ())
    used = tuple(o["observation_id"] for o in usable)
    units = {o["values"][claim.quantity].get("unit") for o in usable}
    if units != {claim.unit}:
        return (Verdict.UNVERIFIABLE,
                f"{claim.quantity} is reported in {sorted(map(str, units))} "
                f"and claimed in {claim.unit!r}; nothing is converted", used)
    each = set()
    for o in usable:
        if claim.op == "=":
            got = o["values"][claim.quantity]
            lo = got["value"] - got["resolution"]
            hi = got["value"] + got["resolution"]
            each.add("SUPPORTED" if lo <= claim.value <= hi else "REFUTED")
        else:
            each.add(evaluate({"model_id": claim.model_id,
                               "quantity": claim.quantity, "op": claim.op,
                               "value": claim.value, "unit": claim.unit},
                              [o])[0])
    if {"SUPPORTED", "REFUTED"} <= each:
        return (Verdict.CONFLICTING_EVIDENCE,
                f"admitted observations decide it both ways ({len(used)})",
                used)
    if each == {"SUPPORTED"}:
        return (Verdict.SUPPORTED,
                f"{len(used)} admitted observation(s) support it, "
                "resolution included", used)
    if each == {"REFUTED"}:
        return (Verdict.CONTRADICTED,
                f"{len(used)} admitted observation(s) contradict it, "
                "resolution included", used)
    return (Verdict.INSUFFICIENT_EVIDENCE,
            "an output's resolution straddles the claimed value", used)


def assess_with(checker, text: str, observations: list) -> tuple:
    """``(Verdict, why)`` from ``checker(text, observations)`` -- a model's
    or a heuristic's opinion, ASSESSED. An answer outside the five is
    UNVERIFIABLE, and so is a checker that fails: an opinion that was not
    given is not a verdict."""
    try:
        answer, why = checker(text, [dict(o) for o in observations])
        return Verdict(answer), f"assessed: {str(why)[:300]}"
    except (ValueError, TypeError) as exc:
        return Verdict.UNVERIFIABLE, f"the checker gave no usable answer " \
                                     f"({type(exc).__name__})"


# ------------------------------------------------------------- the report

@dataclass(frozen=True)
class Report:
    text: str
    structured: Structured | None
    #: citation integrity: INTACT, BROKEN or NONE (nothing cited)
    citation: str
    citation_problems: tuple
    #: observation id -> "ADMITTED" or why it is not
    admission: dict
    verdict: Verdict
    basis: Basis
    why: str
    #: the admitted observations the verdict was judged on
    used: tuple
    #: admitted observations that decided the verdict and were not cited
    uncited: tuple
    label: Label = field(compare=False)

    @property
    def established(self) -> bool:
        return Property.SEMANTICALLY_SUPPORTED in self.label.properties


def check(text: str, *, cites=(), objective_id: str, ledger, store,
          evidence, checker=None, claimant: Origin = Origin.MODEL_OUTPUT,
          source: str = "claimant") -> Report:
    """Answer the three questions for ``text``, which cites the research
    observations ``cites``, under ``objective_id``.

    ``ledger`` is the research ledger, ``store`` the authority store,
    ``evidence`` the evidence store the bundles are held in. ``checker`` is
    consulted only for text the grammar does not read, and its answer is
    ASSESSED."""
    cites = tuple(cites)
    known = set(ledger.of(objective_id, "observation"))
    problems = []
    for oid in cites:
        if oid not in known:
            problems.append(f"{oid}: no observation of {objective_id}")
            continue
        body = ledger.get(oid).body
        dg = body["result_bundle"]
        if not evidence.contains(dg):
            problems.append(f"{oid}: its result bytes are missing or no "
                            "longer re-hash")
        elif values_of(json.loads(evidence.get(dg))) != body["values"]:
            problems.append(f"{oid}: its result bytes say something other "
                            "than the observation recorded")
    citation = "NONE" if not cites else "BROKEN" if problems else "INTACT"

    admission, admitted = admitted_evidence(objective_id, ledger=ledger,
                                            store=store)

    structured = read_claim(text)
    if structured is not None:
        verdict, why, used = judge(structured, admitted)
        basis = Basis.DETERMINISTIC
    elif checker is not None:
        verdict, why = assess_with(checker, text, admitted)
        used, basis = (), Basis.ASSESSED
    else:
        verdict, why, used, basis = (
            Verdict.UNVERIFIABLE, "the grammar does not read it, and no "
            "checker was asked", (), Basis.NONE)
    uncited = tuple(o for o in used if o not in cites)

    label = Label(origin=claimant, source=source)
    if citation == "INTACT":
        label = establish(label, Property.CITED_BYTES_VERIFY, check=BYTES,
                          ref=",".join(cites))
    if basis is Basis.DETERMINISTIC and verdict is Verdict.SUPPORTED and \
            citation == "INTACT":
        label = establish(label, Property.SEMANTICALLY_SUPPORTED,
                          check=JUDGE, ref=",".join(used))
    return Report(text=text, structured=structured, citation=citation,
                  citation_problems=tuple(problems), admission=admission,
                  verdict=verdict, basis=basis, why=why, used=used,
                  uncited=uncited, label=label)


def admitted_evidence(objective_id: str, *, ledger, store) -> tuple:
    """``(admission, admitted)``: for every observation of the objective,
    "ADMITTED" or why not; and the bodies of the admitted ones, in id
    order. Judged at the time the ledger's currency is judged."""
    currency = ledger.currency(objective_id)["observations"]
    admission, admitted = {}, []
    for oid in sorted(ledger.of(objective_id, "observation")):
        why = _admission_problem(ledger, oid, currency[oid], store)
        admission[oid] = why or "ADMITTED"
        if not why:
            admitted.append(dict(ledger.get(oid).body))
    return admission, admitted


def _admission_problem(ledger, oid: str, currency: dict, store) -> str:
    """Why observation ``oid`` is not admitted evidence now, or ''.

    The research ledger answers for the observation's status and standing
    -- WITHDRAWN once the record it was read from no longer stands. The
    authority store answers for what the ledger does not re-derive: whether
    a scientific result's admission holds HERE (its verification report may
    since have been lost, D-2026-150)."""
    if not ledger.current(oid):
        return f"its research status is {currency['status']}"
    if Standing(currency["standing"]) not in USABLE:
        return f"its standing is {currency['standing']}: {currency['why']}"
    record = store.get(ledger.get(oid).body["record_id"])
    if record.admission not in (None, ADMITTED):
        return (f"its record {record.record_id} is not admitted here "
                f"({record.admission})")
    return ""


__all__ = ["Verdict", "Basis", "Structured", "Report", "read_claim",
           "judge", "assess_with", "check", "admitted_evidence",
           "COMPARATORS"]
