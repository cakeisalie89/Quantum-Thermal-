#!/usr/bin/env python3
"""Record what the scaling guards measured, so DRIFT has somewhere to show.

WHY A PASS/FAIL GUARD IS NOT ENOUGH

Every scaling guard in ``tests/test_agent_performance.py`` answers one
question: is this operation's shape still acceptable *today*. None of them
can answer the question that actually kills a system slowly -- is it getting
worse. An operation creeping from n^1.00 to n^1.30 over twenty commits
passes every single run and is a different system by the end, and the commit
that would have been worth arguing about is indistinguishable from the
nineteen that were not.

So the numbers are kept. Each recording appends one observation per guard,
against the commit it came from, to ``docs/performance_baseline.json``.

WHAT THIS FILE IS AND IS NOT

It is a series of measurements taken on whatever machine ran them, and the
absolute values are not comparable across machines -- which is why what is
recorded is a SHAPE (a fitted exponent, a ratio) rather than a duration. Two
machines a hundred times apart in speed should record the same exponent for
the same code.

It is not a continuous time series and does not pretend to be. It grows when
somebody runs this tool, which is a process fact stated here rather than a
guarantee implied elsewhere.

USAGE

    python3 tools/performance_baseline.py --record   # measure and append
    python3 tools/performance_baseline.py            # compare, do not write

Exit status is 0 when every guard is under its ceiling and no guard has
drifted past DRIFT_FACTOR of its historical median.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "performance_baseline.json"
SUITE = "tests/test_agent_performance.py"
SCHEMA_VERSION = 1

#: A new observation more than this multiple of the historical median is
#: reported as drift even when it is still under the guard's ceiling.
#:
#: Loose, on purpose. A shared runner is a noisy place and the point of this
#: number is to catch a trend, not to relitigate every run: a guard that
#: cries drift on ordinary variance is a guard people learn to ignore, which
#: is worse than not having it.
DRIFT_FACTOR = 1.6

#: Observations kept per guard. Old enough to show a trend, bounded so the
#: file stays reviewable.
MAX_HISTORY = 40


def _load() -> dict:
    if not BASELINE.is_file():
        return {"schema_version": SCHEMA_VERSION, "guards": {}}
    doc = json.loads(BASELINE.read_text(encoding="utf-8"))
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(
            f"{BASELINE} is schema v{doc.get('schema_version')} and this "
            f"tool writes v{SCHEMA_VERSION}; refusing to mix them")
    return doc


def _sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or "unknown"
    except Exception:                             # noqa: BLE001
        return "unknown"


def measure() -> list:
    """Run the guards and return what they measured."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "perf.jsonl"
        env = dict(os.environ, QTA_PERF_OUT=str(out))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", SUITE, "-q", "-p", "no:randomly"],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout[-4000:])
            raise SystemExit(
                "the performance suite failed; there is nothing to record "
                "about a run that did not pass")
        if not out.is_file():
            raise SystemExit(
                "the suite passed and recorded nothing. A recorder that "
                "writes an empty history and reports success is the vacuous "
                "result this repository already carries once")
        return [json.loads(ln) for ln in
                out.read_text(encoding="utf-8").splitlines() if ln.strip()]


def median(values) -> float:
    xs = sorted(values)
    n = len(xs)
    if not n:
        raise ValueError("no values")
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def report(doc: dict, seen: list) -> list:
    """Compare fresh measurements against the stored history."""
    problems = []
    for m in seen:
        guard = doc["guards"].get(m["guard"])
        if guard is None:
            print(f"  {m['guard']}: {m['value']} (new guard, no history)")
            continue
        if m["value"] >= m["ceiling"]:
            problems.append(
                f"{m['guard']}: {m['value']} is at or above its ceiling "
                f"{m['ceiling']}")
        past = [o["value"] for o in guard["observations"]]
        if past:
            mid = median(past)
            flag = ""
            if mid > 0 and m["value"] > mid * DRIFT_FACTOR:
                flag = "  <-- DRIFT"
                problems.append(
                    f"{m['guard']}: {m['value']} is more than "
                    f"{DRIFT_FACTOR}x the median of {len(past)} recorded "
                    f"observation(s) ({mid}). Still under the ceiling "
                    f"{m['ceiling']}, which is exactly the case a ceiling "
                    "cannot see")
            print(f"  {m['guard']}: {m['value']} "
                  f"(median {mid}, ceiling {m['ceiling']}){flag}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--record", action="store_true",
                    help="append this run's measurements to the baseline")
    args = ap.parse_args()

    doc = _load()
    print(f"measuring {SUITE} ...")
    seen = measure()
    if not seen:
        print("no guard published a measurement", file=sys.stderr)
        return 1
    print(f"{len(seen)} measurement(s):")
    problems = report(doc, seen)

    if args.record:
        sha = _sha()
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for m in seen:
            g = doc["guards"].setdefault(
                m["guard"], {"ceiling": m["ceiling"], "observations": []})
            g["ceiling"] = m["ceiling"]
            g["observations"].append(
                {"value": m["value"], "commit": sha, "recorded": when})
            g["observations"] = g["observations"][-MAX_HISTORY:]
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"recorded into {BASELINE.relative_to(ROOT)}")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
