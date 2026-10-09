#!/usr/bin/env python3
"""Mutations nobody wrote, so coverage stops depending on imagination.

WHY A HAND-WRITTEN MATRIX HAS A CEILING

Every mutation in ``tools/mutations/`` was written by somebody who had
already decided which lines were enforcement points. That is its strength --
each one names a specific defect and says what it would cost -- and it is
also its ceiling: a check the author did not think of is a check nothing
mutates, and the report says nothing about it either way. R51 named this
exactly: "coverage is as complete as the specification author's imagination
and no better."

WHAT THIS DOES INSTEAD

It walks a module's AST and applies mechanical operators wherever they fit,
with no idea what any of the code means:

    comparison    <  <=  >  >=  ==  !=      swapped for a neighbour
    boolean       and <-> or
    constant      True <-> False, a numeric literal +/- 1
    guard         `if <cond>:` -> `if False:`  (a check that stops checking)
    negation      `not X` -> `X`

Each candidate is emitted as the same find/replace a hand-written spec uses,
so the ordinary harness runs it and the ordinary rules apply: the anchor must
match exactly once, the replacement must change something, the source must be
restored byte-identical.

WHAT A SURVIVOR MEANS HERE, AND WHAT IT DOES NOT

A generated survivor is NOT automatically a defect. Plenty of mutations are
genuinely equivalent (a bound that no caller reaches, a comparison on a value
whose type makes both spellings identical), and a generated set is full of
them in a way a hand-written set is not. So this tool REPORTS survivors for a
person to triage and does not fail a build on them; what it fails on is a run
that generated nothing, which would report a clean sheet over an empty
sample.

USAGE

    python3 tools/generated_mutations.py qta_agent/capability.py \\
        --suite tests/test_agent_delegation.py --sample 20 --seed 7
    python3 tools/generated_mutations.py qta_agent/policy.py --emit out.json
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Comparison operators and what each is replaced by. Every swap changes a
#: boundary by one or inverts it, which is where off-by-one and
#: fail-open defects live.
_CMP = {
    ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="), ast.GtE: (">=", ">"),
    ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="),
}

#: Below this many candidates a sample says nothing about the module, and a
#: run that generated fewer is reporting on something other than what it was
#: pointed at.
MIN_CANDIDATES = 8


def _line(src_lines, node) -> str:
    return src_lines[node.lineno - 1]


def candidates(path: Path) -> list:
    """Every mechanical mutation that fits this module, as find/replace."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    out: list = []
    seen = set()

    def add(kind, node, old_text, new_text, why):
        line = _line(lines, node)
        if old_text not in line:
            return                      # spans lines; skip rather than guess
        mutated = line.replace(old_text, new_text, 1)
        if mutated == line:
            return
        # The anchor is the whole LINE, so a replacement is unambiguous even
        # when the same operator appears elsewhere in the file. A line that
        # is not unique is skipped: the harness would refuse it anyway, and
        # refusing here keeps the generated set clean.
        if src.count(line + "\n") != 1:
            return
        key = (node.lineno, kind, old_text, new_text)
        if key in seen:
            return
        seen.add(key)
        # A MUTATION THAT DOES NOT PARSE IS "KILLED" BY THE IMPORT FAILING,
        # WHICH TESTS NOTHING.
        #
        # This repository's defect ledger already carries four hand-written
        # mutations in that state, each counted as coverage while testing
        # nothing at all. A generator can produce them by the dozen -- drop
        # the `not` out of `if not x:` and the line still reads, drop it out
        # of a lambda body and it may not -- so every candidate is compiled
        # against the whole module before it is offered.
        try:
            ast.parse(src.replace(line + "\n", mutated + "\n", 1))
        except SyntaxError:
            return
        out.append({
            "name": f"G_{kind}_{node.lineno}_{len(out):03d}",
            "path": str(path.relative_to(ROOT)),
            "find": line,
            "replace": mutated,
            "rationale": f"line {node.lineno}: {why}",
        })

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = type(node.ops[0])
            if op in _CMP:
                old, new = _CMP[op]
                add("cmp", node, f" {old} ", f" {new} ",
                    f"comparison {old} becomes {new}: a boundary moves by "
                    "one, or a check inverts")
        elif isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                add("bool", node, " and ", " or ",
                    "and becomes or: a conjunction of required conditions "
                    "becomes satisfied by any one of them")
            else:
                add("bool", node, " or ", " and ",
                    "or becomes and: an alternative becomes a requirement")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            add("not", node, "not ", "",
                "a negation is dropped, so the branch fires on exactly the "
                "cases it was written to skip")
        elif isinstance(node, ast.If):
            seg = ast.get_source_segment(
                path.read_text(encoding="utf-8"), node.test)
            if seg and "\n" not in seg and seg not in ("False", "True"):
                add("guard", node, f"if {seg}", "if False",
                    "a guard stops guarding: the branch below it becomes "
                    "unreachable")
        elif isinstance(node, ast.Constant):
            if node.value is True:
                add("const", node, "True", "False", "True becomes False")
            elif node.value is False:
                add("const", node, "False", "True", "False becomes True")
            elif (isinstance(node.value, int)
                  and not isinstance(node.value, bool)
                  and 0 <= node.value <= 4096):
                add("const", node, str(node.value), str(node.value + 1),
                    f"the literal {node.value} becomes {node.value + 1}: a "
                    "bound, an index or a count is off by one")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("module", type=Path)
    ap.add_argument("--suite", action="append", default=[],
                    help="test suite to run (repeatable)")
    ap.add_argument("--sample", type=int, default=0,
                    help="run this many, chosen at random from the set")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--emit", type=Path,
                    help="write the whole generated spec here and stop")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    path = (args.module if args.module.is_absolute()
            else ROOT / args.module).resolve()
    if not path.is_file():
        raise SystemExit(f"no such module: {args.module}")
    try:
        path.relative_to(ROOT)
    except ValueError:
        # The spec's `path` is resolved against the repository root by the
        # harness, so a module outside it cannot be expressed at all.
        # Refused here with the reason rather than by a ValueError three
        # frames down about subpaths.
        raise SystemExit(
            f"{path} is outside {ROOT}; a mutation spec names a module by "
            "its repository-relative path, and one that has no such path "
            "cannot be run by the harness") from None

    found = candidates(path)
    print(f"{path.relative_to(ROOT)}: {len(found)} mechanical mutation(s) "
          "available")
    if len(found) < MIN_CANDIDATES:
        # A generator that produced almost nothing is reporting on something
        # other than the module it was pointed at -- an empty file, a parse
        # that silently failed, an operator table that stopped matching.
        print(f"  only {len(found)} candidate(s), below the "
              f"{MIN_CANDIDATES} floor: this is not a sample of that module")
        return 1

    chosen = found
    if args.sample and args.sample < len(found):
        chosen = random.Random(args.seed).sample(found, args.sample)
    spec = {
        "title": f"generated mutations over {path.relative_to(ROOT)}",
        "note": ("Produced mechanically by tools/generated_mutations.py, "
                 "with no knowledge of what any of this code means. A "
                 "survivor here is a finding to TRIAGE, not a defect: a "
                 "generated set contains genuinely equivalent mutations in "
                 "a way a hand-written one does not."),
        "suites": args.suite or ["tests/test_agent_substrate.py"],
        "mutations": chosen,
    }
    if args.emit:
        args.emit.write_text(json.dumps(spec, indent=2) + "\n",
                             encoding="utf-8")
        print(f"  wrote {len(chosen)} mutation(s) to {args.emit}")
        return 0

    with tempfile.TemporaryDirectory() as td:
        spec_path = Path(td) / "generated.json"
        spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        print(f"  running {len(chosen)} of them against "
              f"{', '.join(spec['suites'])}")
        proc = subprocess.run(
            [args.python, str(ROOT / "tools" / "mutation_matrix.py"),
             str(spec_path), "--python", args.python],
            cwd=str(ROOT), capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)

    # Survivors are reported and do not fail the run -- see the module
    # docstring. Anything else the harness refuses (an anchor that matched
    # nothing, a source not restored) still does.
    hard = ("ANCHOR DRIFT", "SOURCES NOT RESTORED", "POST-RUN BASELINE RED",
            "TESTS DAMAGED TRACKED FILES", "KILLED ONLY BY TIMEOUT")
    for phrase in hard:
        if phrase in proc.stdout:
            print(f"\nGENERATED RUN FAILED on: {phrase}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
