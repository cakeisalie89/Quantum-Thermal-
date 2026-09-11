#!/usr/bin/env python3
"""Did a DECISION change, or only a digit?

DIAGNOSTIC + GATE. Nothing here moves a threshold, authors a gate state or
writes into the canonical tree. MODEL-ONLY / FORECAST-ONLY. PASS remains 0.

WHY THIS EXISTS

R59 established that the committed outputs do not regenerate byte-identically
on a CPU with a different dispatch, and `tools/blas_kernel_sensitivity.py`
established why: OpenBLAS picks a kernel from the host's flags, numpy picks
its element-wise loops separately, and the two together reproduce a GitHub
runner exactly -- 40 of 63 files, the same 23 by name.

Both of those instruments count FILES. That is the wrong unit, and it is the
reason R59 could be explained for months without being answered. A byte
comparison emits the same output whether the last digit of a residual moved
or a gate flipped from CONDITIONAL to PASS. `package_consistency_check.py`
reporting "24 stale root copies" is compatible with both, so on its own it
licenses no statement about the science at all.

This tool asks the question the byte gate cannot, in the unit that matters:

    of everything that differs between two environments,
    how much of it is a DECISION?

A decision is a non-numeric token: a status, a verdict, a boolean, a label, a
gate id. Those are what the package asserts. A float that moved in its last
places is a serialisation artifact of one accumulation order and asserts
nothing -- but the two are indistinguishable to `cmp`.

WHAT IT REFUSES ON

Exactly one thing: a non-numeric token that is not identical across the two
environments. That is unambiguously a changed claim, and it is the only class
here that is unambiguous. Everything else is measured and reported, never
refused -- a gate that goes red for benign reasons is how R59 became
background noise, and reproducing that in a new file would be a poor trade.

THE SCOPE CHECK IS NOT DECORATION. "No decision changed" is trivially true of
an empty comparison, and an empty comparison is exactly what a renamed
directory or a moved output set produces. The report carries the number of
files and leaves actually compared and the gate refuses a scope of zero, so
the headline can never be satisfied by having looked at nothing.

WHAT IT DOES NOT ESTABLISH

Not that the committed numbers are the correct ones. Every dispatch computes
the same problem to the same order of accuracy and a comparison cannot rank
them. Not that decisions are host-independent in general: they are invariant
HERE, and the report's own `decision_basis` records that most thresholds in
this package currently read REQUIRES_MEASUREMENT, so a gate cannot flip on a
number that is not yet compared to anything. That is a structural reason, not
a safety margin, and it stops holding the day hardware supplies a threshold.

Usage:
    python3 tools/cross_env_semantics.py <other-dir> [<committed-dir>]
    python3 tools/cross_env_semantics.py outputs/ . --json report.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A numeric token as these outputs serialise them. Deliberately broad: it
#: has to find floats INSIDE a repr'd dict that a CSV cell carries as text,
#: which is how `multiphysics_verification_summary.csv` stores the derived
#: booleans next to the quantity they were derived from.
NUMBER = re.compile(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?")

#: Names whose root copy and regenerated copy are DIFFERENT ARTIFACTS rather
#: than one artifact as two environments produced it. `deep_surrogate_
#: readiness.json` at the root is the deep layer's authoritative record
#: (TRAINED_NOT_TRUSTED); the direct expdesign engine emits a NOT_IMPLEMENTED
#: stub into its own run directory. Comparing those two reports a DECISION
#: change on EVERY host, which is a fact about the pipeline's layout and not
#: about the environment -- and a gate that refuses everywhere is the failure
#: mode this file was written to end, not to reproduce.
#:
#: `outputs/` is not a mirror of the root; MANIFEST_BOUNDARY.md says so and
#: this is what that means in practice. `package_consistency_check.py`
#: carries the same exemption for the same reason. It is duplicated rather
#: than imported because that module runs its entire check at import time,
#: and the duplicate has an owner in
#: `test_the_exemption_matches_the_one_the_byte_gate_already_uses`.
REGEN_EXEMPT = frozenset({"deep_surrogate_readiness.json"})

#: Divergence classes, most serious first.
DECISION = "DECISION"            # a non-numeric token changed: a changed claim
ZERO_CROSSING = "ZERO_CROSSING"  # exactly 0 on one side, nonzero on the other
SIGN_FLIP = "SIGN_FLIP"          # both nonzero, opposite signs
PRECISION = "PRECISION"          # both nonzero, same sign


class ScopeError(RuntimeError):
    """The comparison did not measure what it reports on.

    A raise, not an ``assert``: `python -O` deletes asserts, and an
    enforcement point that a flag removes is not an enforcement point.
    """


def leaves(path: Path) -> dict:
    """Every leaf of a CSV or JSON document, addressed by a stable key.

    CSV cells are addressed positionally and JSON leaves by their path, so a
    key means the same thing in both environments as long as the shape is the
    same -- and a shape that is NOT the same shows up as a key present on one
    side only, which the caller reports rather than silently skipping.
    """
    if path.suffix == ".csv":
        out = {}
        with path.open(newline="") as fh:
            for i, row in enumerate(csv.reader(fh)):
                for j, cell in enumerate(row):
                    out[f"r{i}c{j}"] = cell
        return out

    def walk(obj, prefix):
        if isinstance(obj, dict):
            for k in sorted(obj):
                yield from walk(obj[k], f"{prefix}.{k}")
        elif isinstance(obj, list):
            for k, v in enumerate(obj):
                yield from walk(v, f"{prefix}[{k}]")
        else:
            yield prefix, obj
    return dict(walk(json.loads(path.read_text()), ""))


def classify(before: str, after: str) -> list:
    """Classify one leaf's divergence into zero or more findings.

    The split that does the work: strip the numeric tokens out of both sides
    and compare what is LEFT. If the residue differs, a word changed -- a
    status, a boolean, a unit, a label -- and that is a changed claim
    regardless of what the numbers did. If the residue matches, the leaf
    differs only in its numbers, and each number is then classified on its
    own.

    Doing it in that order matters. Comparing the strings whole would call
    `'status: OK'` -> `'status: BAD'` and `'1.0000001'` -> `'1.0000002'` the
    same kind of event, which is precisely the conflation this file exists to
    undo.
    """
    b, a = str(before), str(after)
    b_nums, a_nums = NUMBER.findall(b), NUMBER.findall(a)

    if NUMBER.sub("#", b) != NUMBER.sub("#", a) or len(b_nums) != len(a_nums):
        return [(DECISION, b[:120], a[:120], None)]

    found = []
    for sb, sa in zip(b_nums, a_nums):
        if sb == sa:
            continue
        try:
            fb, fa = float(sb), float(sa)
        except ValueError:                      # pragma: no cover - defensive
            found.append((DECISION, sb, sa, None))
            continue
        if not (math.isfinite(fb) and math.isfinite(fa)):
            # A number that became inf or NaN is not a precision event.
            found.append((DECISION, sb, sa, None))
        elif fb == 0.0 or fa == 0.0:
            found.append((ZERO_CROSSING, sb, sa, None))
        elif (fb < 0) != (fa < 0):
            found.append((SIGN_FLIP, sb, sa, None))
        else:
            rel = abs(fb - fa) / max(abs(fb), abs(fa))
            found.append((PRECISION, sb, sa, rel))
    return found


def compare(other: Path, committed: Path,
            exempt: frozenset = REGEN_EXEMPT) -> dict:
    """Compare every output present in both trees. Returns the report."""
    findings = []
    files_compared = files_differing = leaves_compared = 0
    shape_changes = []
    exempted = []

    for gen in sorted(other.iterdir()):
        if not gen.is_file() or gen.suffix not in (".csv", ".json"):
            continue
        com = committed / gen.name
        if not com.is_file():
            continue
        if gen.name in exempt:
            # Recorded, not silently dropped: an exemption nobody can see is
            # indistinguishable from a file the comparison forgot.
            exempted.append(gen.name)
            continue
        files_compared += 1
        if gen.read_bytes() == com.read_bytes():
            continue
        files_differing += 1
        A, B = leaves(com), leaves(gen)
        only = (set(A) ^ set(B))
        if only:
            shape_changes.append({"file": gen.name,
                                  "n_keys_on_one_side": len(only),
                                  "examples": sorted(only)[:5]})
        for key in sorted(set(A) & set(B)):
            leaves_compared += 1
            if A[key] == B[key]:
                continue
            for kind, b, a, rel in classify(A[key], B[key]):
                findings.append({"file": gen.name, "key": key, "class": kind,
                                 "committed": b, "other": a, "rel": rel})

    counts = {k: sum(1 for f in findings if f["class"] == k)
              for k in (DECISION, ZERO_CROSSING, SIGN_FLIP, PRECISION)}
    precisions = [f["rel"] for f in findings if f["class"] == PRECISION]
    return {
        "exempted": exempted,
        "files_compared": files_compared,
        "files_differing": files_differing,
        "leaves_compared": leaves_compared,
        "counts": counts,
        "shape_changes": shape_changes,
        "max_precision_rel": max(precisions) if precisions else 0.0,
        "findings": findings,
    }


def check_scope(report: dict) -> None:
    """Refuse a verdict drawn from an empty comparison."""
    if report["files_compared"] == 0:
        raise ScopeError(
            "compared 0 files: no output in the other tree has a committed "
            "counterpart by name. 'no decision changed' would be a statement "
            "about an empty set.")
    if report["leaves_compared"] == 0 and report["files_differing"]:
        raise ScopeError(
            f"{report['files_differing']} file(s) differ but 0 leaves were "
            "compared: the documents do not share a shape, so nothing was "
            "actually put side by side.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("other", type=Path,
                   help="tree regenerated in the other environment")
    p.add_argument("committed", type=Path, nargs="?", default=ROOT,
                   help="tree holding the committed canonical copies")
    p.add_argument("--json", type=Path, help="write the full report here")
    args = p.parse_args(argv)

    report = compare(args.other, args.committed)
    try:
        check_scope(report)
    except ScopeError as exc:
        print(f"SCOPE REFUSED: {exc}", file=sys.stderr)
        return 2

    c = report["counts"]
    print(f"compared {report['files_compared']} file(s); "
          f"{report['files_differing']} differ byte-for-byte; "
          f"{report['leaves_compared']} leaves put side by side")
    if report["exempted"]:
        print(f"  exempt (root copy and regenerated copy are different "
              f"artifacts): {', '.join(report['exempted'])}")
    print()
    print(f"  DECISION      {c[DECISION]:5d}   a status, verdict, boolean or "
          "label that is not the same")
    print(f"  ZERO_CROSSING {c[ZERO_CROSSING]:5d}   exactly zero on one side, "
          "nonzero on the other")
    print(f"  SIGN_FLIP     {c[SIGN_FLIP]:5d}   opposite signs, neither zero")
    print(f"  PRECISION     {c[PRECISION]:5d}   same sign, "
          f"largest relative difference {report['max_precision_rel']:.3e}")

    for s in report["shape_changes"]:
        print(f"  ! {s['file']}: {s['n_keys_on_one_side']} key(s) on one side "
              f"only, e.g. {s['examples']}")

    if c[ZERO_CROSSING]:
        print("\nZERO_CROSSING is not a precision event. A published "
              "0.000000000e+00 states that the model determined the quantity "
              "to be exactly nothing; the other environment's nonzero value "
              "says the zero was one accumulation order's underflow. Both are "
              "the same physics and only one of them is a claim of "
              "exactness.")
        for f in [f for f in report["findings"]
                  if f["class"] == ZERO_CROSSING][:10]:
            print(f"    {f['file']} [{f['key']}]: {f['committed']} -> "
                  f"{f['other']}")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"\nfull report: {args.json}")

    if c[DECISION]:
        print(f"\nREFUSED: {c[DECISION]} decision-bearing token(s) differ "
              "between the two environments. A verdict is not a rounding "
              "artifact.", file=sys.stderr)
        for f in [f for f in report["findings"]
                  if f["class"] == DECISION][:20]:
            print(f"    {f['file']} [{f['key']}]: {f['committed']!r} -> "
                  f"{f['other']!r}", file=sys.stderr)
        return 1

    print("\nNo decision-bearing token differs between the two environments.")
    return 0


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
