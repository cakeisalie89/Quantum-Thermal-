#!/usr/bin/env python3
"""Every governed numeric column carries a dimension somebody reviewed.

WHY THIS EXISTS

``build_hdf5_mapping.py`` resolved a column's unit by matching the END OF ITS
NAME against a thirty-entry suffix table. The last few characters of a name
are not a unit, and the measurement says so twice over:

  * **91 of 162** numeric columns fell through the table and were published
    into the archival HDF5 as ``unit: "unresolved"`` -- among them every
    heat-source density (``Q_laser_W_m3``), every gas density
    (``n_CH4_modeC_1m3``), every entropy in nats, and ``heat_flux_W_m2``.
    Each of those states its unit in its own name. The dimension was lost
    crossing a boundary that the source had already labelled;
  * **three did not fall through, and came out wrong.**
    ``gradient_K_per_m`` ends in ``_m`` and was published as **metres**.
    ``dose_flux_open_m2_s`` and ``dose_flux_closed_m2_s`` end in ``_s`` and
    were published as **seconds**. A consumer reading those attributes would
    have believed a temperature gradient was a length and a flux was a time --
    worse than no unit, because a wrong one is credible.

``validate_hdf5_equivalence.py`` counted the unresolved columns and gated
nothing, so the number sat in a report reading like a footnote.

WHAT IS REVIEWED RATHER THAN DERIVED

The dimension itself, in ``docs/unit_inventory.json``. There is no
``unresolved`` in that vocabulary: a column with no unit says WHY it has
none -- ``DIMENSIONLESS`` for a physical ratio, ``COUNT`` for a count or
index, ``ORDINAL`` for a rank-scale score, ``PER_ROW`` for a long-format
table whose unit belongs to the row. That distinction is the same one
D-2026-53 drew between a zero that is exact and a zero that is unresolved:
"I have no unit" and "nobody worked out the unit" look identical in a file
and mean opposite things. ``cost`` and ``duration`` are {1,2,3} difficulty
scores, not currency and not time, which is precisely the misreading a suffix
rule invites.

WHAT IS MEASURED

  * every governed numeric column has an entry, so a new column arrives
    undeclared and fails here rather than being published dimensionless;
  * no entry names a column that no longer exists;
  * no entry is ``unresolved`` or ``UNREVIEWED``;
  * a ``PER_ROW`` entry names a column that is ACTUALLY IN that CSV's header,
    so "the unit is in the row" is checked rather than asserted;
  * a column whose name ends in an unambiguous unit token may not be declared
    dimensionless. That is the direction of error that matters: publishing a
    quantity as a pure number.

A reconciliation over an empty column set is a refusal, not a pass.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "docs" / "unit_inventory.json"
MAPPING = ROOT / "hdf5_output_mapping.json"

NON_UNITS = {"DIMENSIONLESS", "COUNT", "ORDINAL", "PER_ROW"}

#: Suffixes that unambiguously name a physical dimension. Used ONLY to refuse
#: a dimensionless declaration on a column whose own name states a unit --
#: never to resolve one, which is the mistake this file exists to undo.
UNIT_TOKENS = (
    "_K", "_mK", "_Pa", "_J", "_W", "_Hz", "_s", "_us", "_ns", "_m",
    "_m2", "_m3", "_1m3", "_W_m3", "_W_m2", "_nats", "_rads", "_pct",
    "_percent", "_WK", "_K_per_m", "_m2_s", "_per_m2", "_per_s",
)


class ScopeError(RuntimeError):
    """The reconciliation had nothing to reconcile."""


def governed_numeric_columns(mapping: dict):
    """(source, column) for every float64 column in the governed mapping."""
    out = []
    for o in mapping["outputs"]:
        for c in (o.get("columns") or []):
            if c.get("dtype") == "float64":
                out.append((o["path"], c["name"]))
    return out


def _header(src: str):
    p = ROOT / src
    if not p.exists():
        return None
    with open(p, newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def reconcile(columns, declared):
    if not columns:
        raise ScopeError(
            "the governed mapping yielded no numeric columns; a "
            "reconciliation over an empty set would report full coverage")
    if not declared:
        raise ScopeError(
            f"{INVENTORY.name} declares nothing; nothing was reconciled")

    problems = []
    keys = {f"{s}:{c}" for s, c in columns}

    for k in sorted(keys - set(declared)):
        problems.append(
            f"{k}: governed numeric column with no declared dimension; it "
            "would be published into the archive carrying whatever the "
            "writer defaulted to")
    for k in sorted(set(declared) - keys):
        problems.append(
            f"{k}: declared but no longer a governed numeric column; the "
            "inventory is describing a schema that has moved on")

    for k in sorted(keys & set(declared)):
        e = declared[k]
        u = e.get("unit")
        if not u or u in ("unresolved", "UNREVIEWED"):
            problems.append(f"{k}: dimension is {u!r}")
            continue
        src, col = k.split(":", 1)
        if u == "PER_ROW":
            carrier = e.get("unit_from")
            if not carrier:
                problems.append(
                    f"{k}: PER_ROW names no column as carrying the unit")
                continue
            hdr = _header(src)
            if hdr is None:
                problems.append(f"{k}: PER_ROW, but {src} is not readable")
            elif carrier not in hdr:
                problems.append(
                    f"{k}: PER_ROW says the unit is in {carrier!r}, which is "
                    f"not a column of {src}")
        elif u in NON_UNITS:
            tok = next((t for t in sorted(UNIT_TOKENS, key=len, reverse=True)
                        if col.endswith(t)), None)
            if tok:
                problems.append(
                    f"{k}: declared {u} while its own name ends in {tok!r}; a "
                    "quantity published as a pure number")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    mapping = json.loads(MAPPING.read_text())
    declared = json.loads(INVENTORY.read_text())["columns"]
    columns = governed_numeric_columns(mapping)
    try:
        problems = reconcile(columns, declared)
    except ScopeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    kinds = {}
    for e in declared.values():
        kinds[e["unit"]] = kinds.get(e["unit"], 0) + 1
    real = sum(v for k, v in kinds.items() if k not in NON_UNITS)
    print(f"{len(columns)} governed numeric columns, every one declared: "
          f"{real} carry a physical unit, "
          f"{kinds.get('DIMENSIONLESS', 0)} are dimensionless, "
          f"{kinds.get('COUNT', 0)} are counts, "
          f"{kinds.get('ORDINAL', 0)} are ordinal scores, "
          f"{kinds.get('PER_ROW', 0)} carry the unit in the row")
    if args.verbose:
        for k in sorted(declared):
            e = declared[k]
            extra = f" (from {e['unit_from']})" if e.get("unit_from") else ""
            print(f"  {k}: {e['unit']}{extra}")
    if problems:
        print(f"\nREFUSED: {len(problems)} problems", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("no column is published without a dimension somebody reviewed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
