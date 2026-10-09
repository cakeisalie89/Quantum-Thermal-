"""Whether two results are the same scientific result, quantity by quantity.

Byte identity is not correctness, and different bytes are not a different
result. Between those two facts sits the question this module answers, and
answers only under a declared contract: two ResultBundles of the same model
on the same parameters, possibly from different implementations or numerical
backends -- do they say the same thing, as far as each says anything?

THE RULES, AND WHERE EVERY NUMBER COMES FROM

* A contract (``docs/equivalence_contracts.json``) classifies EVERY output
  of a model: ``AT_DECLARED_RESOLUTION``, ``EXACT``, or ``EXCLUDED`` with a
  reason. It carries no tolerance: a number in a contract is refused, so no
  epsilon can be chosen after looking at a discrepancy.
* ``AT_DECLARED_RESOLUTION``: each side's value is within its own declared
  resolution ``r`` of the exact value, so two honest results differ by at
  most ``r_a + r_b``. That bound is the tolerance, and its provenance is the
  two quantities' own resolution classes and bases, which the report
  carries. A side that declares no resolution makes the quantity
  NOT_ESTABLISHED -- never equal, never different.
* ``EXACT``: defined values (``exact=True``) must be equal.
* Units must be identical; a unit difference is NOT_EQUIVALENT, not a
  conversion.
* A FAILED or UNDEFINED output on either side makes that quantity
  NOT_ESTABLISHED.
* Every invariant must be evaluated on both sides and hold on both. An
  invariant that holds on one side only is NOT_EQUIVALENT.
* A pair with no invariant evaluated at all is NOT_ESTABLISHED however
  close its numbers: nothing shows either computation behaved.
* Decision stability -- the same invariants holding on both -- is reported
  apart and never contributes to the equivalence status: that two results
  lead to the same decision does not make them the same result.

STATUSES

``BYTE_IDENTICAL`` (the records are equal), else
``EQUIVALENT_AT_DECLARED_RESOLUTION`` (every contracted quantity agrees
within its declared bounds and every invariant holds on both), else
``NOT_EQUIVALENT`` (at least one quantity or invariant demonstrably
differs), else ``NOT_ESTABLISHED`` (nothing differs demonstrably but
something could not be decided). A comparison of different models or
different parameters is refused: it is not a reproduction question.

Nothing here touches a legacy QTA comparison: those keep their own
``SCIENTIFIC_EQUIVALENCE_STATUS=NOT_ESTABLISHED`` until their methods declare
resolutions and an owner decides a policy for them.
"""
from __future__ import annotations

from typing import Any

import json
from dataclasses import dataclass
from pathlib import Path

from .identity import digest
from .quantity import Quantity
from .result import OutputStatus, ResultBundle

SCHEMA = "equivalence-contracts/1"
CONTRACTS = Path(__file__).resolve().parent.parent / "docs" / \
    "equivalence_contracts.json"

BYTE_IDENTICAL = "BYTE_IDENTICAL"
EQUIVALENT = "EQUIVALENT_AT_DECLARED_RESOLUTION"
NOT_EQUIVALENT = "NOT_EQUIVALENT"
NOT_ESTABLISHED = "NOT_ESTABLISHED"

AT_RESOLUTION = "AT_DECLARED_RESOLUTION"
EXACT = "EXACT"
EXCLUDED = "EXCLUDED"
RULES = (AT_RESOLUTION, EXACT, EXCLUDED)

DECISION_STABLE = "DECISION_STABLE"
DECISION_CHANGED = "DECISION_CHANGED"


class EquivalenceRefused(ValueError):
    pass


def _no_numbers(obj, where: str) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        raise EquivalenceRefused(
            f"{where}: a contract carries no numbers -- a tolerance comes "
            "from the quantities' declared resolutions, never from here")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _no_numbers(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_numbers(v, f"{where}[{i}]")


def load_contracts(path: Path = CONTRACTS) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise EquivalenceRefused(f"schema {doc.get('schema')!r} is not "
                                 f"{SCHEMA}")
    _no_numbers(doc.get("models"), "models")
    for mid, c in (doc.get("models") or {}).items():
        outs = c.get("outputs")
        if not isinstance(outs, dict) or not outs:
            raise EquivalenceRefused(f"{mid}: classify every output")
        for name, rule in outs.items():
            r = rule.get("rule") if isinstance(rule, dict) else None
            if r not in RULES:
                raise EquivalenceRefused(f"{mid}.{name}: rule {r!r} is not "
                                         f"one of {RULES}")
            if r == EXCLUDED and not str(rule.get("reason", "")).strip():
                raise EquivalenceRefused(f"{mid}.{name}: an exclusion says "
                                         "why")
        if not str(c.get("provenance", "")).strip():
            raise EquivalenceRefused(f"{mid}: say where the contract's "
                                     "rules come from")
    return doc


@dataclass(frozen=True)
class QuantityVerdict:
    name: str
    rule: str
    status: str
    detail: str
    difference: float | None = None
    bound: float | None = None
    bound_provenance: str = ""

    def to_record(self) -> dict:
        return {"name": self.name, "rule": self.rule, "status": self.status,
                "detail": self.detail, "difference": self.difference,
                "bound": self.bound, "bound_provenance":
                    self.bound_provenance}


def _bound(q: Quantity):
    if q.exact:
        return 0.0, "exact by construction"
    if q.resolution is None:
        return None, "no declared resolution"
    return float(q.resolution), (f"{q.resolution_class.value}: "
                                 f"{q.resolution_basis}")


def _quantity(name: str, rule: str, a, b) -> QuantityVerdict:
    if a.status is not OutputStatus.OK or b.status is not OutputStatus.OK:
        return QuantityVerdict(name, rule, NOT_ESTABLISHED,
                               f"output status {a.status.value} / "
                               f"{b.status.value}")
    qa, qb = a.quantity, b.quantity
    if qa.unit != qb.unit:
        return QuantityVerdict(name, rule, NOT_EQUIVALENT,
                               f"units differ: {qa.unit!r} vs {qb.unit!r}")
    diff = abs(qa.value - qb.value)
    if rule == EXACT:
        if not (qa.exact and qb.exact):
            return QuantityVerdict(name, rule, NOT_ESTABLISHED,
                                   "EXACT needs both values defined "
                                   "(exact=True)", diff)
        return QuantityVerdict(name, rule,
                               EQUIVALENT if qa.value == qb.value
                               else NOT_EQUIVALENT, "defined values", diff,
                               0.0, "exact by construction")
    ra, pa = _bound(qa)
    rb, pb = _bound(qb)
    if ra is None or rb is None:
        return QuantityVerdict(name, rule, NOT_ESTABLISHED,
                               f"resolution: {pa} / {pb}", diff)
    bound = ra + rb
    return QuantityVerdict(
        name, rule, EQUIVALENT if diff <= bound else NOT_EQUIVALENT,
        "each side within its declared resolution of the exact value",
        diff, bound, f"r_a ({pa}) + r_b ({pb})")


def _combine(statuses) -> str:
    statuses = list(statuses)
    if NOT_EQUIVALENT in statuses:
        return NOT_EQUIVALENT
    if NOT_ESTABLISHED in statuses or not statuses:
        return NOT_ESTABLISHED
    return EQUIVALENT


def compare(a: ResultBundle, b: ResultBundle, contracts: dict | None = None
            ) -> dict:
    """The equivalence report for two bundles of one model on one input."""
    if a.model_id != b.model_id or a.model_version != b.model_version:
        raise EquivalenceRefused(
            f"different models ({a.model_id}@{a.model_version} vs "
            f"{b.model_id}@{b.model_version}) are not a reproduction "
            "question")
    if a.parameter_digest != b.parameter_digest:
        raise EquivalenceRefused("different parameters are not a "
                                 "reproduction question")
    doc = contracts if contracts is not None else load_contracts()
    contract = (doc.get("models") or {}).get(a.model_id)
    report: dict[str, Any] = {
        "schema": "equivalence-report/1", "model_id": a.model_id,
              "model_version": a.model_version,
              "parameter_digest": a.parameter_digest,
              "bundles": [a.digest(), b.digest()],
              "implementations": [a.implementation_digest,
                                  b.implementation_digest],
              "environments": [a.environment_digest, b.environment_digest]}
    if a.to_record() == b.to_record():
        report.update(status=BYTE_IDENTICAL, quantities=[], invariants=[],
                      decision=DECISION_STABLE,
                      note="the records are equal; nothing to resolve")
        report["digest"] = digest({k: v for k, v in report.items()})
        return report
    if contract is None:
        raise EquivalenceRefused(f"no equivalence contract for "
                                 f"{a.model_id}; without one nothing says "
                                 "what 'the same result' means for it")
    names_a = {o.name for o in a.outputs}
    names_b = {o.name for o in b.outputs}
    rules = contract["outputs"]
    verdicts = []
    for name in sorted(names_a | names_b):
        rule = (rules.get(name) or {}).get("rule")
        if name not in names_a or name not in names_b:
            verdicts.append(QuantityVerdict(name, rule or "UNCLASSIFIED",
                                            NOT_EQUIVALENT,
                                            "present on one side only"))
        elif rule is None:
            verdicts.append(QuantityVerdict(name, "UNCLASSIFIED",
                                            NOT_ESTABLISHED,
                                            "the contract does not "
                                            "classify this output"))
        elif rule == EXCLUDED:
            verdicts.append(QuantityVerdict(name, rule, EXCLUDED,
                                            rules[name]["reason"]))
        else:
            verdicts.append(_quantity(name, rule, a.output(name),
                                      b.output(name)))
    ids = sorted({i.invariant_id for i in a.invariants}
                 | {i.invariant_id for i in b.invariants})
    inv = []
    for iid in ids:
        try:
            ha, hb = a.invariant(iid).holds, b.invariant(iid).holds
        except KeyError:
            inv.append({"invariant": iid, "status": NOT_EQUIVALENT,
                        "detail": "evaluated on one side only"})
            continue
        inv.append({"invariant": iid, "holds": [ha, hb],
                    "status": EQUIVALENT if ha and hb
                    else NOT_EQUIVALENT})
    decision = (DECISION_STABLE if all(
        "holds" in x and x["holds"][0] == x["holds"][1] for x in inv)
        else DECISION_CHANGED)
    status = _combine([v.status for v in verdicts if v.status != EXCLUDED]
                      + [x["status"] for x in inv])
    if not ids and status == EQUIVALENT:
        status = NOT_ESTABLISHED
    report.update(status=status,
                  quantities=[v.to_record() for v in verdicts],
                  invariants=inv, decision=decision,
                  decision_note="reported apart: a stable decision is not "
                                "equivalence",
                  contract_provenance=contract["provenance"])
    report["digest"] = digest({k: v for k, v in report.items()})
    return report
