"""External scientific constraints a learned prediction is checked against.

A constraint comes from OUTSIDE the model -- a declared invariant of the
computation that produced the training data, a unit, a physical domain --
and says where it came from. It is evaluated on predictions and the inputs
that produced them, and every violation is RECORDED: nothing here clips,
rounds or replaces a prediction. A violation rate is a finding about the
model.

Kinds

``FINITE``       the predicted value is finite;
``RANGE``        lower <= value <= upper (either bound optional);
``RELATION``     a declared inequality between predictions and inputs,
                 with the margin by which it fails reported.

No legacy machine state appears here; the surface-adsorption constraints
are that model's own declared invariants (``scientific/models/
surface_adsorption.py``: bounded, capture_only, within_incidence).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

KINDS = ("FINITE", "RANGE", "RELATION")


@dataclass(frozen=True)
class Constraint:
    constraint_id: str
    kind: str
    target: str
    source: str
    lower: float | None = None
    upper: float | None = None
    #: RELATION: (inputs, predictions) -> margin; >= 0 holds, < 0 fails.
    relation: Callable | None = None
    statement: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"{self.constraint_id}: kind {self.kind!r}")
        if not self.source.strip():
            raise ValueError(f"{self.constraint_id}: a constraint says "
                             "where it comes from")
        if self.kind == "RELATION" and self.relation is None:
            raise ValueError(f"{self.constraint_id}: a RELATION needs one")


def _margin(c: Constraint, inputs: dict, pred: dict) -> float:
    v = pred[c.target]
    if c.kind == "FINITE":
        return 0.0 if math.isfinite(v) else -math.inf
    if not math.isfinite(v):
        return -math.inf
    if c.kind == "RANGE":
        m = math.inf
        if c.lower is not None:
            m = min(m, v - c.lower)
        if c.upper is not None:
            m = min(m, c.upper - v)
        return m
    return float(c.relation(inputs, pred))


def evaluate(constraints, rows) -> dict:
    """``rows``: (sample_id, inputs, predictions-in-physical-units)."""
    out = {}
    n = 0
    for c in constraints:
        out[c.constraint_id] = {"kind": c.kind, "target": c.target,
                                "source": c.source,
                                "statement": c.statement, "evaluated": 0,
                                "violations": 0, "worst_margin": None,
                                "examples": []}
    for sid, inputs, pred in rows:
        n += 1
        for c in constraints:
            rec = out[c.constraint_id]
            m = _margin(c, inputs, pred)
            rec["evaluated"] += 1
            if m < 0:
                rec["violations"] += 1
                if len(rec["examples"]) < 5:
                    rec["examples"].append(
                        [sid, None if not math.isfinite(m) else m])
            w = rec["worst_margin"]
            if math.isfinite(m) and (w is None or m < w):
                rec["worst_margin"] = m
    for rec in out.values():
        rec["violation_rate"] = (rec["violations"] / rec["evaluated"]
                                 if rec["evaluated"] else None)
    return {"policy": "violations are recorded; no prediction is clipped "
                      "or replaced", "rows": n, "constraints": out}


def surface_adsorption() -> tuple:
    src = "surface.langmuir_capture@1.0.0 declared invariant"
    rel = []
    for t in ("admitted_fluence", "final_coverage", "final_inventory",
              "impingement_flux"):
        rel.append(Constraint(f"finite_{t}", "FINITE", t,
                              "every published number is finite"))
    rel.append(Constraint(
        "coverage_bounded", "RANGE", "final_coverage", f"{src} 'bounded'",
        lower=0.0, upper=1.0, statement="0 <= N / N_cap <= 1"))
    rel.append(Constraint(
        "inventory_within_capacity", "RELATION", "final_inventory",
        f"{src} 'bounded'",
        relation=lambda x, p: x["capacity_per_m2"] - p["final_inventory"],
        statement="final_inventory <= capacity_per_m2"))
    rel.append(Constraint(
        "capture_only", "RELATION", "final_inventory",
        f"{src} 'capture_only'",
        relation=lambda x, p: p["final_inventory"]
        - x["initial_coverage"] * x["capacity_per_m2"],
        statement="no desorption: final_inventory >= initial_coverage * "
                  "capacity_per_m2"))
    rel.append(Constraint(
        "within_incidence", "RELATION", "final_inventory",
        f"{src} 'within_incidence'",
        relation=lambda x, p: x["sticking"] * p["admitted_fluence"]
        - (p["final_inventory"]
           - x["initial_coverage"] * x["capacity_per_m2"]),
        statement="captured <= sticking * admitted fluence"))
    return tuple(rel)
