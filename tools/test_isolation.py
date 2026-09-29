#!/usr/bin/env python3
"""Can every test module be collected on its own?

DIAGNOSTIC + GATE. Nothing here moves a threshold, authors a gate state or
writes into the canonical tree. MODEL-ONLY / FORECAST-ONLY. PASS remains 0.

WHY THIS EXISTS

`tests/` has no `__init__.py`, so pytest puts `tests/` on `sys.path` and not
the repository root. A module importing `tools.something` or `qta_agent` at
top level therefore depends on some EARLIER module having inserted the root --
and 103 of 106 modules do insert it, each with its own copy of the line.

Three did not, and they passed anyway, every run, because the alphabet put a
module that inserts the path ahead of them. Run alone, all three failed to
collect. A suite whose green depends on collection order is reporting the
order as much as the code.

`conftest.py` at the repository root closes the mechanism: pytest imports it
before collecting anything, so the path is there whatever runs first. This
tool is what keeps it closed, and it checks the property rather than the
file -- a conftest that exists and does nothing would pass a grep.

WHAT IT REFUSES ON

A module that cannot be collected by itself. That is the whole contract.

THE SCOPE CHECK IS NOT DECORATION. "Every module collects alone" is trivially
true of no modules at all, which is what a moved directory or a changed glob
produces. The count is reported and a scope of zero is refused.

WHAT IT DOES NOT ESTABLISH

That the tests PASS in isolation -- only that they can be collected, which is
the import-order failure this was written for. A test whose assertions depend
on another module's side effects is a different defect and this does not look
for it.

Usage:
    python3 tools/test_isolation.py
    python3 tools/test_isolation.py --verbose
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


class ScopeError(RuntimeError):
    """Refusing a verdict drawn from an empty comparison."""


def modules() -> list[pathlib.Path]:
    return sorted(TESTS.glob("test_*.py"))


def collects_alone(path: pathlib.Path, timeout: int = 180):
    """``(ok, tail)`` for one module, from a real pytest collection."""
    r = subprocess.run(
        # Absolute, so the tool works against a scratch directory in a test
        # as well as against tests/ -- a checker whose own tests can only be
        # written against the live tree is one whose refusals go unexercised.
        [sys.executable, "-m", "pytest", str(path),
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    return r.returncode == 0, tail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    found = modules()
    try:
        if not found:
            raise ScopeError(
                f"no test module matched {TESTS}/test_*.py; 'every module "
                "collects alone' would be a statement about an empty set")
    except ScopeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    broken = []
    for p in found:
        ok, tail = collects_alone(p)
        if args.verbose:
            print(f"  {'ok  ' if ok else 'FAIL'} {p.name}")
        if not ok:
            broken.append((p.name, tail))

    print(f"{len(found) - len(broken)} of {len(found)} test modules can be "
          "collected on their own")
    if broken:
        print(f"\nREFUSED: {len(broken)} module(s) import only when something "
              "else has run first", file=sys.stderr)
        for name, tail in broken:
            print(f"  - {name}", file=sys.stderr)
            for line in tail:
                print(f"      {line[:120]}", file=sys.stderr)
        return 1
    print("collection order is not load-bearing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
