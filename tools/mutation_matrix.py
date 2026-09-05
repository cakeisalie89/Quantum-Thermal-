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
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Written before the first mutation, removed on a clean exit. Its presence at
#: startup means a previous run died without restoring, so the sources on disk
#: may still be mutated.
#:
#: This exists because it happened. A run was killed by a sandbox timeout
#: before the signal handler below was added, and left `os.setsid()` replaced
#: by `pass` in the execution layer. The check afterwards grepped for the
#: OTHER mutations' markers and reported the tree clean, and the files were
#: new and untracked so git could not contradict it. The result was a process
#: group that was never created, so every timeout killed the CALLER's process
#: group -- the test runner terminated itself, repeatedly, and the cause looked
#: like a kernel or sandbox problem for as long as the leftover went unnoticed.
#:
#: A sidecar turns that silent state into a refusal to start.
RECOVERY = ROOT / ".mutation-recovery.json"

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


def _dirty_tracked_files() -> list:
    """Tracked files currently modified in the working tree, repo-relative.

    Deliberately git rather than a hash snapshot: git already knows the full
    tracked set, so this notices damage to a file the harness never touched
    and never listed.
    """
    proc = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain",
                           "--untracked-files=no"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return sorted(line[3:].strip() for line in proc.stdout.splitlines()
                  if line.strip())


def _restore(paths: list) -> None:
    if paths:
        subprocess.run(["git", "-C", str(ROOT), "checkout", "HEAD", "--",
                        *paths], capture_output=True, text=True)


def run_suite(suites: list, python: str) -> tuple:
    """Return (returncode, [failed test names]). ``-x`` stops at the first."""
    # start_new_session puts the suite in ITS OWN process group. Without it a
    # mutant that signals "its" process group signals the HARNESS too -- which
    # is not hypothetical: mutating away the execution layer's `setsid` makes
    # every timeout in the suite kill the caller's group, and the caller is
    # this harness. The run then dies looking like an infrastructure problem
    # rather than like the mutation doing exactly what it was written to do.
    proc = subprocess.run(
        [python, "-m", "pytest", *suites, "-q", "--no-header", "-x",
         "--tb=no", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=SUITE_TIMEOUT_S,
        start_new_session=True)
    failed = [line.split("::")[-1].split(" ")[0]
              for line in proc.stdout.splitlines()
              if line.startswith("FAILED")]
    return proc.returncode, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", type=Path, nargs="?",
                    help="mutation specification (JSON)")
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter used to run the suite")
    ap.add_argument("--list", action="store_true",
                    help="print the mutations and exit without running them")
    ap.add_argument("--recover", action="store_true",
                    help="restore sources from a previous run's recovery file")
    args = ap.parse_args()

    if args.recover:
        if not RECOVERY.exists():
            print("nothing to recover; no interrupted run recorded")
            return 0
        saved = json.loads(RECOVERY.read_text(encoding="utf-8"))
        for rel, text in saved.items():
            path = ROOT / rel
            if path.read_text(encoding="utf-8") != text:
                print(f"  restored {rel}")
                path.write_text(text, encoding="utf-8")
            else:
                print(f"  already clean: {rel}")
        RECOVERY.unlink()
        print("sources restored from the interrupted run")
        return 0

    if args.spec is None:
        ap.error("a mutation specification is required unless --recover")
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

    # A mutation that disables a safety guard lets the SUITE damage files the
    # harness never touched. Verifying only that the mutated sources were
    # restored misses that entirely -- and it happened: removing the Stage-10
    # write guard let a test replace a 261-line governed README with one line
    # of forged text, and the corrupted file was nearly committed alongside a
    # manifest regenerated over it. The tracked working tree is therefore
    # checked after every mutation, not just the files being mutated.
    baseline_dirty = set(_dirty_tracked_files())
    collateral: dict = {}

    if RECOVERY.exists():
        print(f"REFUSING TO START: {RECOVERY.name} exists, so a previous run "
              "died without restoring its sources. They may still be mutated "
              "-- and a leftover mutation is a DISABLED SAFETY GUARD that no "
              "test is currently reporting.")
        print("Restore them with:")
        print(f"    python3 {Path(__file__).name} --recover")
        return 3
    RECOVERY.write_text(json.dumps(original, indent=2), encoding="utf-8")

    # SIGTERM does not run `finally`, so a harness killed by a timeout or a
    # CI cancellation would leave a MUTATED source on disk -- a disabled safety
    # guard, committed by whoever ran `git add -A` next. Turning the signal
    # into an exception makes the restore path run. This came up for real: a
    # run was SIGTERMed mid-matrix and the sources survived only because the
    # kill happened to land between mutations.
    def _restore_and_die(signum, frame):
        for rel, text in original.items():
            (ROOT / rel).write_text(text, encoding="utf-8")
        print(f"\ninterrupted by signal {signum}; sources restored")
        raise SystemExit(130)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _restore_and_die)
        except (ValueError, OSError):          # pragma: no cover - platform
            pass

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
                damaged = [f for f in _dirty_tracked_files()
                           if f not in baseline_dirty and f != path]
                if damaged:
                    collateral[name] = damaged
                    _restore(damaged)

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
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):      # pragma: no cover - platform
                pass
        RECOVERY.unlink(missing_ok=True)

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
    if collateral:
        print("TESTS DAMAGED TRACKED FILES under mutation (restored, but the "
              "test is unsafe -- it must undo its own writes in a finally):")
        for name, files in sorted(collateral.items()):
            print(f"  {name}: {files}")
    if drifted:
        print(f"SOURCES NOT RESTORED: {drifted}")
    else:
        print("all sources restored byte-identical (verified by re-hashing)")

    still_dirty = [f for f in _dirty_tracked_files()
                   if f not in baseline_dirty]
    if still_dirty:
        print(f"WORKING TREE LEFT DIRTY: {still_dirty}")
    return 0 if not (survived or anchors or drifted or collateral
                     or still_dirty) else 1


if __name__ == "__main__":
    raise SystemExit(main())
