#!/usr/bin/env python3
"""Run a declarative mutation matrix against a test suite.

WHY THIS IS A COMMITTED TOOL AND NOT A SCRATCH SCRIPT

`authorities.json` claims that every enforcement point in `qta_agent/` is
verified by mutation testing. A claim of that kind is only as good as the
ability to re-run it, so the harness and the mutation specifications live in
the repository and are reviewable like anything else.

WHAT A MUTATION MATRIX ACTUALLY PROVES

Coverage says a line executed. It does not say anything would have noticed if
the line were deleted. Each mutation here disables exactly one check; the
suite must then fail. A mutation that SURVIVES means that check is
unprotected -- nothing distinguishes a build with it from a build without it.

THE TRAP THIS HARNESS IS BUILT TO AVOID

A mutation matrix run against a RED baseline proves nothing: all N mutations
"fail the suite", every one for the pre-existing reason, and the report reads
as a perfect score. The baseline is therefore checked first and the run
refuses to start otherwise.

Two lesser versions of the same trap are also closed:

  * An anchor that no longer matches its source is an ERROR, not a skip. A
    silently skipped mutation is a mutation that tested nothing while
    appearing in the report.
  * Sources are restored in a ``finally`` and then re-hashed. "Restored
    byte-identical" is verified rather than asserted.

USAGE

    python3 tools/mutation_matrix.py tools/mutations/agent_substrate.json

Exit status is 0 only when the baseline was green, every anchor matched, every
mutation was killed, and every source was restored byte-identical.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Backstop only. A mutation whose kill mechanism is the harness timing out is
#: a badly written TEST, not a well-caught mutation: it costs the timeout in
#: wall clock every run and reports nothing about which check was lost. The
#: tests are expected to bound their own blocking calls -- see the ``deadline``
#: helper in tests/test_agent_evidence.py, which turns a blocked FIFO read into
#: a five-second failure. This value exists so a mutation nobody anticipated
#: cannot stall CI indefinitely; the agent suites run in single-digit seconds.
SUITE_TIMEOUT_S = 300


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_suite(suites: list, python: str) -> tuple:
    """Return (returncode, [failed test names]). ``-x`` stops at the first."""
    proc = subprocess.run(
        [python, "-m", "pytest", *suites, "-q", "--no-header", "-x",
         "--tb=no", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=SUITE_TIMEOUT_S)
    failed = [line.split("::")[-1].split(" ")[0]
              for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    return proc.returncode, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", type=Path, help="mutation specification (JSON)")
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter used to run the suite")
    ap.add_argument("--list", action="store_true",
                    help="print the mutations and exit without running them")
    args = ap.parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    suites = spec["suites"]
    mutations = spec["mutations"]
    name_w = max(len(m["name"]) for m in mutations)

    if args.list:
        for m in mutations:
            print(f"{m['name']:{name_w}s}  {m['path']}  -- {m['rationale']}")
        return 0

    print(f"{spec['title']}\n{'=' * len(spec['title'])}")
    print(f"{len(mutations)} mutations over {len(suites)} suite(s)\n")

    # --- 1. the baseline must be green ------------------------------------
    rc, failed = run_suite(suites, args.python)
    if rc != 0:
        print("BASELINE RED -- mutation results would be meaningless.")
        print("Every mutation would 'fail the suite' for this pre-existing")
        print(f"reason and the report would read as a perfect score: {failed}")
        return 2
    print("baseline green; mutating\n")

    paths = sorted({m["path"] for m in mutations})
    original = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    before = {p: _sha(ROOT / p) for p in paths}

    results: dict = {}
    try:
        for m in mutations:
            name, path = m["name"], m["path"]
            src = original[path]
            found = src.count(m["find"])
            if found != 1:
                print(f"{name:{name_w}s}  ANCHOR DRIFT  matched {found}x in "
                      f"{path}")
                results[name] = "ANCHOR_DRIFT"
                continue

            (ROOT / path).write_text(src.replace(m["find"], m["replace"]),
                                     encoding="utf-8")
            try:
                rc, failed = run_suite(suites, args.python)
            except subprocess.TimeoutExpired:
                # Counted as killed -- the suite did not go green -- but
                # flagged, because a test that bounds its own blocking calls
                # would have said WHICH check was lost, in seconds.
                print(f"{name:{name_w}s}  KILLED (TIMEOUT) <-- no test bounds "
                      f"this; it cost {SUITE_TIMEOUT_S}s of wall clock")
                results[name] = "KILLED_BY_TIMEOUT"
                continue
            finally:
                (ROOT / path).write_text(src, encoding="utf-8")

            if rc != 0:
                by = f"  by {failed[0]}" if failed else ""
                print(f"{name:{name_w}s}  KILLED      {by}")
                results[name] = "KILLED"
            else:
                print(f"{name:{name_w}s}  SURVIVED    <-- nothing detects "
                      f"this: {m['rationale']}")
                results[name] = "SURVIVED"
    finally:
        for p, src in original.items():
            (ROOT / p).write_text(src, encoding="utf-8")

    # --- 2. restoration is verified, not assumed --------------------------
    drifted = [p for p in paths if _sha(ROOT / p) != before[p]]
    survived = [k for k, v in results.items() if v == "SURVIVED"]
    anchors = [k for k, v in results.items() if v == "ANCHOR_DRIFT"]
    timeouts = [k for k, v in results.items() if v == "KILLED_BY_TIMEOUT"]
    killed = sum(1 for v in results.values()
                 if v in ("KILLED", "KILLED_BY_TIMEOUT"))

    print()
    print(f"killed:   {killed}/{len(mutations)}")
    if timeouts:
        print(f"killed only by timeout (write a bounded test): {timeouts}")
    if survived:
        print(f"SURVIVED: {survived}")
    if anchors:
        print(f"ANCHOR DRIFT (tested nothing): {anchors}")
    if drifted:
        print(f"SOURCES NOT RESTORED: {drifted}")
    else:
        print("all sources restored byte-identical (verified by re-hashing)")

    return 0 if not (survived or anchors or drifted) else 1


if __name__ == "__main__":
    raise SystemExit(main())
