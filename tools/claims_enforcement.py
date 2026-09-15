#!/usr/bin/env python3
"""How much of the claims boundary is actually enforced.

WHY THIS EXISTS

CLAIMS_BOUNDARY.md calls itself "the strongest statement of position in the
entire package" and lists, under **Forbidden:**, the sentences the package may
not say. Measured for the first time, **19 of its 24 entries would have passed
package_consistency_check.py verbatim** -- pasted into README.md, nothing
would have refused them. The whole shielding list was unenforced, including

    "Mode B processing and Mode D sensing occur simultaneously."

which is the statement the mode-exclusive architecture exists to deny. The
five that were caught were caught incidentally, by patterns written to catch
stale RTB/JT module counts.

Nobody had claimed the list was enforced. Nothing had measured that it was
not, and a claims file whose entries are not enforced reads exactly like one
whose entries are.

WHAT THIS ESTABLISHES, AND WHAT IT CANNOT

One bounded question, per entry: **would this exact sentence be refused in a
live document?** A paraphrase is not caught. No string rule catches one, and
pretending otherwise would put a number on this page that means less than it
appears to.

So the coverage is re-derived rather than asserted. Naming a bullet in the
checker is not coverage: the pattern must MATCH the bullet as written, here,
now, and both directions are reconciled --

  * every **Forbidden:** bullet must be matched by some pattern in the
    checker, or it is reported uncovered;
  * every pattern must name a bullet that still exists, so the enforcement
    table cannot drift into describing a claims file that has moved on;
  * every pattern must match the bullet it names, which is the check that
    turns "this rule enforces that claim" from a label into a measurement.

Two entries of the claims boundary are deliberately NOT in the Forbidden list
-- rules about how to read a number rather than sentences anyone would write.
They sit in prose above it, because a list that must be enforced entry by
entry cannot carry entries that nothing can enforce without misreporting its
own coverage.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLAIMS = ROOT / "CLAIMS_BOUNDARY.md"
CHECKER = ROOT / "package_consistency_check.py"

#: Every list in the checker that can refuse a sentence in a live document.
#: The union is what "enforced" means here; a bullet caught by any of them is
#: caught. Kept as a union rather than duplicating patterns into one list,
#: because two copies of a rule drift and one of them keeps passing.
PATTERN_LISTS = ("FORBIDDEN_CLAIM_PATTERNS", "STALE_PATTERNS_8E")
SUBSTRING_LISTS = ("STALE_FORBIDDEN_IN_TEX_OR_PDF",
                   "STALE_FORBIDDEN_IN_STDOUT")


class ScopeError(RuntimeError):
    """The reconciliation had nothing to reconcile."""


def forbidden_bullets(text: str) -> list[str]:
    """The **Forbidden:** bullets of CLAIMS_BOUNDARY.md, unwrapped."""
    out: list[str] = []
    for block in re.finditer(
            r"\*\*Forbidden:\*\*\n(.*?)(?=\n\*\*Allowed:\*\*|\n\n[A-Z]|\n## )",
            text, re.S):
        cur = None
        for line in block.group(1).split("\n"):
            if line.strip().startswith("- "):
                if cur:
                    out.append(cur)
                cur = line.strip()[2:].strip()
            elif line.strip() and cur is not None:
                cur += " " + line.strip()
        if cur:
            out.append(cur)
    return [b.strip().strip('"').strip() for b in out]


def _literal_pairs(node):
    """(first, second) of every 2-tuple of string literals in a list node."""
    for elt in getattr(node, "elts", []):
        if not isinstance(elt, ast.Tuple) or len(elt.elts) != 2:
            continue
        a, b = elt.elts
        if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                and isinstance(b, ast.Constant) and isinstance(b.value, str):
            yield a.value, b.value


def enforcement(src: str):
    """Patterns and substrings the checker can refuse a sentence with.

    Read from the parse tree, not by importing: package_consistency_check.py
    runs its whole audit at import time.
    """
    tree = ast.parse(src)
    pats: list[tuple[str, str, str]] = []   # (regex, label, list name)
    subs: list[tuple[str, str]] = []        # (substring, list name)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = getattr(node.targets[0], "id", None)
        if name in PATTERN_LISTS:
            pats += [(rx, lab, name) for rx, lab in _literal_pairs(node.value)]
        elif name in SUBSTRING_LISTS:
            subs += [(e.value, name)
                     for e in getattr(node.value, "elts", [])
                     if isinstance(e, ast.Constant)
                     and isinstance(e.value, str)]
    return pats, subs


def reconcile(bullets, pats, subs):
    """Coverage, measured by matching -- never by a name."""
    if not bullets:
        raise ScopeError(
            "CLAIMS_BOUNDARY.md yielded no **Forbidden:** bullets; a "
            "reconciliation over an empty list would report full coverage")
    if not pats:
        raise ScopeError(
            "the checker yielded no enforcement patterns; nothing was "
            "reconciled")

    covered, uncovered, problems = {}, [], []
    for b in bullets:
        hits = [(lab, src) for rx, lab, src in pats if re.search(rx, b)]
        hits += [(x, src) for x, src in subs if x.lower() in b.lower()]
        if hits:
            covered[b] = hits
        else:
            uncovered.append(b)

    named = {lab for _, lab, src in pats if src == "FORBIDDEN_CLAIM_PATTERNS"}
    known = set(bullets)
    for lab in sorted(named - known):
        problems.append(
            f"FORBIDDEN_CLAIM_PATTERNS names {lab!r}, which is not a bullet "
            "in CLAIMS_BOUNDARY.md; the enforcement table is describing a "
            "claims file that has moved on")
    # The anti-proxy check: a pattern that names a bullet must MATCH it.
    for rx, lab, src in pats:
        if src != "FORBIDDEN_CLAIM_PATTERNS" or lab not in known:
            continue
        if not re.search(rx, lab):
            problems.append(
                f"pattern {rx!r} claims to enforce {lab!r} and does not "
                "match it; naming a claim is not enforcing it")
    for b in uncovered:
        problems.append(
            f"no pattern in the checker would refuse {b!r} in a live document")
    return covered, uncovered, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true",
                    help="print what catches each claim")
    args = ap.parse_args(argv)

    bullets = forbidden_bullets(CLAIMS.read_text(encoding="utf-8"))
    pats, subs = enforcement(CHECKER.read_text(encoding="utf-8"))
    try:
        covered, uncovered, problems = reconcile(bullets, pats, subs)
    except ScopeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    print(f"{len(covered)} of {len(bullets)} forbidden claims would be "
          f"refused verbatim in a live document "
          f"({len(pats)} patterns, {len(subs)} substrings)")
    if args.verbose:
        for b in bullets:
            hits = covered.get(b)
            mark = f"<- {hits[0][0]!r} ({hits[0][1]})" if hits else "UNCOVERED"
            print(f"  {b}\n      {mark}")
    if problems:
        print(f"\nREFUSED: {len(problems)} problems", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("every forbidden claim is matched by a rule that was re-derived, "
          "not named; paraphrases are outside what this establishes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
