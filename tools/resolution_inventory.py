#!/usr/bin/env python3
"""What states the resolution of the method that produced each published number?

DIAGNOSTIC + GATE. Nothing here moves a threshold, authors a gate state or
writes into the canonical tree. MODEL-ONLY / FORECAST-ONLY. PASS remains 0.

WHY THIS EXISTS

D-2026-53 established that a serialised number must carry the resolution of
the method that produced it: a published 0.000000000e+00 means one thing when
the model determined the quantity to be nothing and another when it is one
accumulation order's underflow inside the integrator's own tolerance.

The only instrument reporting on that was the DECLARED/BARE split in
`tools/cross_env_semantics.py`, and it examines a column ONLY when the column
happens to CROSS ZERO between two environments. A quantity that is tiny but
nonzero on both machines is never looked at. So the discipline's apparent
coverage was a fact about which two machines had run, not about the package.

Measured over every governed numeric column instead of the ones a divergence
exposed, the answer was 19 of 162, in 4 artefacts of 41.

WHAT IT REFUSES ON

1. A declared column that is not a governed numeric column, or a governed
   numeric column with no declaration. Both directions, so an inventory
   describing a schema that has moved on is caught as readily as a new column
   nobody classified.
2. A CARRIER naming a column that is not in that artefact's header.
3. A CARRIER whose values are not all resolution classes. Naming a column is
   not carrying a class -- the same anti-proxy rule `claims_enforcement.py`
   applies to a pattern that names a claim without matching it.
4. WITHIN-ARTEFACT INCOMPLETENESS. In an artefact where at least one column
   declares a CARRIER, no column may be NO_FLOOR_DEFINED. The mechanism has
   reached that file: a floor exists and the solve computes the class, so a
   sibling quantity published without one is a gap that can be closed now,
   not a limit of the method. This is the rule that stops D-2026-53 from
   being closed on the example -- the residual -- while the maxima and the
   region means beside it in the same row stay bare.

WHAT IT DOES NOT REFUSE ON

The 120 columns whose producing method has no declared floor. Refusing those
would force a floor to be invented for every solver in the package tonight,
and a fabricated floor is worse than an absent one: it would make the
artefact state a resolution nobody derived. They are counted, named in
`--verbose`, and left open.

KNOWN LIMITATION, STATED RATHER THAN ROUNDED AWAY

A metric/value table declares per ROW, not per column -- the PER_ROW shape
D-2026-57 had to name. `coupled_mode_recovery_metrics.csv` carries
`Mode_D_residual_CH4_resolution` on a row of its own, and this inventory has
no way to say "some rows of this column carry a class". Those columns are
counted as NO_FLOOR_DEFINED, which understates them. Modelling per-row
carriers is the next refinement; counting them as declared without checking
which rows would be the overstatement, and that is the worse of the two.

THE SCOPE CHECK IS NOT DECORATION. "Every declaration checks out" is trivially
true of no declarations. The count is reported and a scope of zero is refused.

Usage:
    python3 tools/resolution_inventory.py
    python3 tools/resolution_inventory.py --verbose
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNITS = ROOT / "docs" / "unit_inventory.json"
INVENTORY = ROOT / "docs" / "resolution_inventory.json"

#: The vocabulary a carrier column may hold, from qta_multiphysics.numerics.
CLASSES = {"EXACT_ZERO", "RESOLVED", "BELOW_RESOLUTION", "OUT_OF_RANGE"}

BASES = {"CARRIER", "FLOOR", "COORDINATE", "EXACT_BY_CONSTRUCTION",
          "INPUT_CONSTANT", "NO_FLOOR_DEFINED"}


class ScopeError(RuntimeError):
    """Refusing a verdict drawn from an empty reconciliation."""


def governed_columns() -> set:
    """The subject, re-derived from the unit inventory rather than listed."""
    return set(json.loads(UNITS.read_text(encoding="utf-8"))["columns"])


def _column_values(src: str, col: str):
    """Every value of one column of a committed CSV, or None if unreadable."""
    path = ROOT / src
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or col not in rows[0]:
        return None
    return [r[col] for r in rows]


def reconcile(governed: set, declared: dict) -> list:
    if not governed:
        raise ScopeError(
            "the unit inventory yielded no governed numeric columns; a "
            "reconciliation over an empty set would report full coverage")
    if not declared:
        raise ScopeError(
            f"{INVENTORY.name} declares nothing; nothing was reconciled")

    problems = []
    for key in sorted(governed - set(declared)):
        problems.append(
            f"{key}: governed numeric column with no resolution basis; it is "
            "published without anything saying what its method could resolve, "
            "and without this file saying so either")
    for key in sorted(set(declared) - governed):
        problems.append(
            f"{key}: declared but no longer a governed numeric column; the "
            "inventory is describing a schema that has moved on")

    for key in sorted(governed & set(declared)):
        e = declared[key]
        basis = e.get("basis")
        if basis not in BASES:
            problems.append(f"{key}: basis {basis!r} is not one of "
                            f"{sorted(BASES)}")
            continue
        if basis != "CARRIER":
            if not e.get("why"):
                problems.append(
                    f"{key}: basis {basis} states no reason; a column exempted "
                    "from the discipline has to say why it is exempt")
            continue
        src, _ = key.split(":", 1)
        carrier = e.get("resolution_from")
        if not carrier:
            problems.append(f"{key}: CARRIER names no column")
            continue
        values = _column_values(src, carrier)
        if values is None:
            problems.append(
                f"{key}: CARRIER names {carrier!r}, which is not a readable "
                f"column of {src}")
            continue
        bad = sorted({v for v in values if v not in CLASSES})
        if bad:
            problems.append(
                f"{key}: CARRIER {carrier!r} holds {bad[:4]}, which are not "
                "resolution classes; naming a column is not carrying a class")

    # 4. within-artefact completeness
    by_src = collections.defaultdict(list)
    for key in governed & set(declared):
        by_src[key.split(":", 1)[0]].append(key)
    for src, keys in sorted(by_src.items()):
        if not any(declared[k].get("basis") == "CARRIER" for k in keys):
            continue
        for k in sorted(keys):
            if declared[k].get("basis") == "NO_FLOOR_DEFINED":
                problems.append(
                    f"{k}: NO_FLOOR_DEFINED in an artefact that already "
                    "declares a resolution for another of its columns. The "
                    "floor exists and the solve computes the class here, so "
                    "this is a quantity left bare beside one that is not")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true",
                    help="name every column with no floor defined")
    args = ap.parse_args(argv)

    governed = governed_columns()
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    declared = doc["columns"]
    try:
        problems = reconcile(governed, declared)
    except ScopeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    counts = collections.Counter(
        declared[k]["basis"] for k in sorted(governed & set(declared))
        if declared[k].get("basis") in BASES)
    carriers = counts["CARRIER"]
    open_gap = counts["NO_FLOOR_DEFINED"]
    artefacts = {k.split(":", 1)[0] for k in governed}
    reached = {k.split(":", 1)[0] for k in governed & set(declared)
               if declared[k].get("basis") == "CARRIER"}
    print(f"{len(governed)} governed numeric columns in {len(artefacts)} "
          f"artefacts: {carriers} carry a resolution class "
          f"({len(reached)} artefacts), {counts['EXACT_BY_CONSTRUCTION']} are "
          f"exact by construction, {counts['COORDINATE']} are coordinates, "
          f"{counts['FLOOR']} are floors, and {open_gap} have no floor defined")
    if args.verbose:
        for k in sorted(governed & set(declared)):
            if declared[k].get("basis") == "NO_FLOOR_DEFINED":
                print(f"  open: {k}")
    if problems:
        print(f"\nREFUSED: {len(problems)} problems", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("every governed column either states what its method resolves or "
          "says, in this file, that nothing does")
    return 0


if __name__ == "__main__":
    sys.exit(main())
