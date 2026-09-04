#!/usr/bin/env python3
"""Validate and render the §21-§59 completion matrix.

WHY THIS IS EXECUTABLE

A completion matrix is a self-assessment, and a self-assessment nobody checks
drifts toward optimism one edit at a time. Every claim a row makes that CAN be
checked mechanically IS checked here:

  * every path named in ``implementation`` exists;
  * every path named in ``tests`` exists;
  * every mutation spec named in ``mutation_tests`` exists AND actually
    mutates at least one of the row's implementation paths -- so a row cannot
    borrow another subsystem's mutation coverage;
  * a row claiming a ``production_caller`` names a real file that really
    imports the implementation;
  * classifications above PARTIALLY_IMPLEMENTED require tests;
  * COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT additionally requires
    mutation coverage and an empty ``residual_gaps``;
  * a BLOCKED row must name its missing input and what would unblock it.

What cannot be checked mechanically is whether a row's prose is honest. That
is what review is for, which is why the matrix is committed rather than
printed.

Usage:
    python3 tools/completion_matrix.py            # validate, print summary
    python3 tools/completion_matrix.py --table    # full table
    python3 tools/completion_matrix.py --open     # only unfinished rows
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "completion_matrix.json"

#: Ordered weakest to strongest. Order is meaningful: `>=` comparisons below
#: rely on it, and a new class must be inserted at its true strength.
CLASSES = (
    "ABSENT",
    "PLACEHOLDER",
    "SKELETAL",
    "PARTIALLY_IMPLEMENTED",
    "LOCALLY_MATURE_BUT_UNINTEGRATED",
    "INTEGRATED_BUT_INCOMPLETELY_VERIFIED",
    "DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS",
    "COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT",
)
BLOCKED = ("EXTERNALLY_BLOCKED", "EPISTEMICALLY_BLOCKED")
COMPLETE = "COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT"

#: Fields every row must carry, from the directive's §62 list.
REQUIRED = (
    "id", "requirement", "classification", "implementation", "callers",
    "production_caller", "persistent_state", "authority_owner", "evidence",
    "provenance", "failure_semantics", "recovery", "retry", "idempotency",
    "cancellation", "concurrency", "security_boundary", "tests",
    "property_tests", "mutation_tests", "fuzzing", "differential",
    "hosted_ci", "residual_gaps", "blocker",
)


def load() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _rank(cls: str) -> int:
    return CLASSES.index(cls) if cls in CLASSES else -1


def validate(doc: dict) -> list:
    problems: list = []
    rows = doc.get("rows")
    if not isinstance(rows, list) or not rows:
        return ["matrix has no rows"]

    seen = set()
    for row in rows:
        rid = row.get("id", "<no id>")
        for field in REQUIRED:
            if field not in row:
                problems.append(f"{rid}: missing field {field!r}")
        if rid in seen:
            problems.append(f"{rid}: duplicate row id")
        seen.add(rid)

        cls = row.get("classification")
        if cls not in CLASSES and cls not in BLOCKED:
            problems.append(f"{rid}: unknown classification {cls!r}")
            continue

        for field in ("implementation", "tests"):
            for rel in row.get(field, []):
                if not (ROOT / rel).exists():
                    problems.append(
                        f"{rid}: {field} path does not exist: {rel}")

        # A row may not borrow another subsystem's mutation coverage.
        impl = set(row.get("implementation", []))
        for spec_rel in row.get("mutation_tests", []):
            spec_path = ROOT / spec_rel
            if not spec_path.exists():
                problems.append(f"{rid}: mutation spec missing: {spec_rel}")
                continue
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                problems.append(f"{rid}: mutation spec unparseable: {exc}")
                continue
            mutated = {m.get("path") for m in spec.get("mutations", [])}
            if impl and not (mutated & impl):
                problems.append(
                    f"{rid}: cites {spec_rel} but that spec mutates "
                    f"{sorted(mutated)}, none of this row's implementation "
                    f"{sorted(impl)}")

        pc = row.get("production_caller")
        if pc:
            if not (ROOT / pc).exists():
                problems.append(f"{rid}: production_caller missing: {pc}")
            elif impl:
                text = (ROOT / pc).read_text(encoding="utf-8",
                                             errors="replace")
                mods = {Path(p).stem for p in impl}
                if not any(m in text for m in mods):
                    problems.append(
                        f"{rid}: production_caller {pc} does not "
                        f"reference any "
                        f"of {sorted(mods)}")

        if cls in BLOCKED:
            if not row.get("blocker"):
                problems.append(f"{rid}: {cls} requires a blocker")
            elif not row["blocker"].get("unblocked_by"):
                problems.append(
                    f"{rid}: blocker must say what would unblock it")
        else:
            weak = _rank("PARTIALLY_IMPLEMENTED")
            if _rank(cls) > weak and not row["tests"]:
                problems.append(f"{rid}: {cls} claimed with no tests")
            if cls == COMPLETE:
                if not row["mutation_tests"]:
                    problems.append(
                        f"{rid}: {COMPLETE} claimed with no mutation coverage")
                if row["residual_gaps"]:
                    problems.append(
                        f"{rid}: {COMPLETE} claimed with residual "
                        f"gaps listed: "
                        f"{row['residual_gaps']}")
    return problems


def summarise(doc: dict) -> dict:
    counts: dict = {}
    for row in doc["rows"]:
        cls = row["classification"]
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", action="store_true", help="print every row")
    ap.add_argument("--open", dest="open_only", action="store_true",
                    help="print only rows below the completion bar")
    args = ap.parse_args()

    doc = load()
    problems = validate(doc)

    rows = doc["rows"]
    if args.table or args.open_only:
        width = max(len(r["id"]) for r in rows)
        for row in rows:
            cls = row["classification"]
            if args.open_only and (cls == COMPLETE or cls in BLOCKED):
                continue
            print(f"{row['id']:{width}s}  {cls:48s} {row['requirement'][:60]}")
        print()

    counts = summarise(doc)
    total = len(rows)
    for cls in CLASSES + BLOCKED:
        if counts.get(cls):
            print(f"  {counts[cls]:3d}  {cls}")
    done = counts.get(COMPLETE, 0)
    blocked = sum(counts.get(c, 0) for c in BLOCKED)
    print(f"\n{done}/{total} complete, {blocked} blocked, "
          f"{total - done - blocked} open")

    if problems:
        print(f"\nMATRIX INVALID ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("matrix self-consistent; every mechanically checkable claim holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
