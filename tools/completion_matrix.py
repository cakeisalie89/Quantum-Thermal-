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
  * a BLOCKED row must name its missing input and what would unblock it;
  * a residual gap must not claim a subsystem "does not exist" when a module
    of that name is sitting in the tree -- the staleness check, added after a
    dozen rows were found still describing subsystems that had been built
    weeks earlier;
  * a row above PARTIALLY_IMPLEMENTED must list at least one residual gap or
    be COMPLETE, because a row with neither is claiming perfection quietly;
  * a row claiming property_tests, fuzzing or differential coverage must name
    a file that exists and actually contains that kind of testing.

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
import re
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
            elif pc.startswith(("tests/", "test_")) or "/tests/" in pc:
                # THE defect this project has hit twice, in two subsystems:
                # a correct, thoroughly tested function whose only callers
                # were its own tests, and a result field populated by
                # nothing. A row whose production caller IS a test is
                # claiming production integration it does not have, and a
                # test file is the easiest thing in the tree to point at.
                problems.append(
                    f"{rid}: production_caller {pc} is a test. A defence "
                    "nothing but its own tests invokes is indistinguishable "
                    "from no defence; name the real caller or say the row "
                    "has none")
            elif impl:
                text = (ROOT / pc).read_text(encoding="utf-8",
                                             errors="replace")
                # A QUALIFIED reference, not a bare stem. Matching "memory"
                # or "tools" against a whole file is true of almost any
                # document, so the old check passed a README as a production
                # caller -- a guard that cannot fail is not a guard. Its own
                # test caught this.
                mods = {Path(q).stem for q in impl}
                forms = set()
                for m in mods:
                    # "import <stem>" covers every import spelling that
                    # actually pulls the module in, including
                    # "from qta_multiphysics.stack import rag_index as R",
                    # while still being false of ordinary prose.
                    forms.update({f"qta_agent.{m}", f"qta_agent/{m}",
                                  f"import {m}", f".{m} import"})
                forms.update(impl)
                if not any(f in text for f in forms):
                    problems.append(
                        f"{rid}: production_caller {pc} does not reference "
                        f"any of {sorted(mods)} in a form that would import "
                        "or run it")

        # STALENESS. A gap describing a subsystem as absent, when a module
        # of that name is in the tree, is documentation that has stopped
        # being true -- and a matrix nobody re-reads drifts exactly this way.
        # Twelve rows were found in that state at once, still saying "the
        # scheduler does not exist yet" months after it did.
        modules = {q.stem for q in (ROOT / "qta_agent").glob("*.py")}
        for gap in row.get("residual_gaps", []):
            low = gap.lower()
            for phrase in ("does not exist yet", "do not exist yet",
                           "does not exist", "no scheduler",
                           "no policy engine", "is skeletal",
                           "has no production caller"):
                if phrase not in low:
                    continue
                named = {m for m in modules
                         if len(m) > 4 and m in low}
                if named:
                    problems.append(
                        f"{rid}: a residual gap says {phrase!r} while "
                        f"qta_agent/{sorted(named)[0]}.py exists; refresh the "
                        "row rather than leaving documentation that has "
                        "stopped being true")

        # A row above PARTIALLY_IMPLEMENTED with no gaps and no COMPLETE
        # claim is claiming perfection without saying so.
        if (cls not in BLOCKED and cls != COMPLETE
                and _rank(cls) > _rank("PARTIALLY_IMPLEMENTED")
                and not row.get("residual_gaps")):
            problems.append(
                f"{rid}: {cls} with no residual gaps listed. Either the row "
                f"is {COMPLETE}, or something is missing and should be said")

        # Claimed coverage must be the kind of coverage it claims to be.
        #
        # USAGE, not a mention. This matched the bare word "hypothesis"
        # anywhere in the file, so a docstring sentence like "the rule
        # Hypothesis found" satisfied a property-testing claim -- and did,
        # the moment one was written into a suite that has no property tests
        # at all. A marker a comment can supply is not evidence of coverage.
        for field, markers, what in (
            ("property_tests",
             ("@given", "from hypothesis import", "import hypothesis",
              "rulebasedstatemachine"),
             "property-based testing"),
            ("fuzzing", ("fuzz",), "fuzzing"),
        ):
            value = row.get(field)
            names = value if isinstance(value, list) else (
                [value] if value and value.lower() != "none" else [])
            for name in names:
                rel = name.split(" ", 1)[0]
                path = ROOT / rel
                if not path.is_file():
                    continue          # prose, not a path; nothing to check
                body = path.read_text(encoding="utf-8",
                                      errors="replace").lower()
                if not any(m in body for m in markers):
                    problems.append(
                        f"{rid}: {field} names {rel}, which contains no "
                        f"{what}")

        # A hosted-CI claim must cite a RUN, not a mood. "green", "passing"
        # and "should be fine" are all things this field has been tempted to
        # say; a run id is a thing somebody can open.
        hosted = row.get("hosted_ci") or ""
        if hosted and hosted.lower() not in ("none", "n/a"):
            if not re.search(r"\b\d{8,}\b", hosted):
                problems.append(
                    f"{rid}: hosted_ci says {hosted[:60]!r} but names no run "
                    "id. A hosted claim with no run behind it is the one "
                    "kind of evidence a reader cannot check for themselves")

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
