#!/usr/bin/env python3
"""How much of the history each authoritative read path reads, counted.

Phase 2C of the PR #17 audit (docs/audit/). A governed run is dominated by
a subprocess, so its wall time hides how much verification grows beneath
it: hashing three thousand records is microseconds against a one-second
run. This counts the work instead, by wrapping the event log's read paths
from outside, in this process only. Nothing in agent_substrate is changed
or configured by it.

WHAT IS COUNTED, PER READ SITE. A site is the first frame outside
``agent_substrate/events.py`` that asked for the read, as
``module:function``.

- ``full``: ``EventLog.read_verified`` calls, each a whole-chain verified
  read, with the records each returned and the size of the log it read.
- ``tail``: ``EventLog.advance`` and ``read_verified_from`` calls, the
  anchored O(new) reads, and the records they returned.
- ``anchor_at``: the slow anchor path, which reads the log again, with
  the records it parsed to reach the one it anchors on.
- ``hashed``: ``Event.recompute_hash`` calls, from any site.

WHAT IS RUN. ``tools/research_horizon.py``'s campaign to each size in
``--sizes``, in a fresh workspace each time. Its decisions are SCRIPTED
DECISIONS: no model is called, and nothing here measures one.

WHAT IT SAYS. For each size, the totals and the table by site. Across
sizes, each total's ratio to its value at the size before. When the size
doubles, work linear in the history goes up by about 2 and quadratic work
by about 4. A ratio is a measurement of this workload, not a bound.

    python tools/work_counts.py --sizes 15,30,60          # print
    python tools/work_counts.py --sizes 15,30,60 --write  # record
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_substrate import events as EV  # noqa: E402

RECORD = ROOT / "docs" / "audit" / "work_counts.json"
SCHEMA = "pr17-audit-work-counts/1"
KINDS = ("full", "tail", "anchor_at")


def _site() -> str:
    """The first frame outside the event log that asked for the read."""
    # [:-2]: this frame, and the wrapper in counting() that called it.
    for fr in reversed(traceback.extract_stack()[:-2]):
        f = fr.filename.replace("\\", "/")
        if f.endswith("agent_substrate/events.py") or f.endswith(
                "tools/work_counts.py"):
            continue
        if "/agent_substrate/" in f or "/tools/" in f:
            return f"{Path(f).stem}:{fr.name}"
    return "?"


class Counts:
    """Counters for one run, keyed by (kind, site)."""

    def __init__(self):
        self.calls: collections.Counter = collections.Counter()
        self.records: collections.Counter = collections.Counter()
        self.bytes: collections.Counter = collections.Counter()
        self.hashed = 0

    def totals(self) -> dict:
        out: dict = {"hashed": self.hashed}
        for kind in KINDS:
            out[f"{kind}_calls"] = sum(
                v for (k, _), v in self.calls.items() if k == kind)
            out[f"{kind}_records"] = sum(
                v for (k, _), v in self.records.items() if k == kind)
        out["full_bytes"] = sum(
            v for (k, _), v in self.bytes.items() if k == "full")
        return out

    def by_site(self) -> list:
        rows = []
        for (kind, site), n in self.calls.items():
            rows.append({"kind": kind, "site": site, "calls": n,
                         "records": self.records[(kind, site)],
                         "bytes": self.bytes[(kind, site)]})
        return sorted(rows, key=lambda r: (-r["records"], -r["calls"],
                                           r["kind"], r["site"]))


@contextlib.contextmanager
def counting(counts: Counts):
    """Wrap the read paths for the duration, and restore them after."""
    log_cls, ev_cls = EV.EventLog, EV.Event
    saved = {name: getattr(log_cls, name) for name in
             ("read_verified", "advance", "read_verified_from", "anchor_at")}
    saved_hash = ev_cls.recompute_hash

    def wrap(name, kind):
        real = saved[name]

        def run(self, *a, **k):
            site = _site()
            counts.calls[(kind, site)] += 1
            out = real(self, *a, **k)
            if kind == "full":
                counts.records[(kind, site)] += len(out[1])
                with contextlib.suppress(OSError):
                    counts.bytes[(kind, site)] += self.path.stat().st_size
            elif kind == "tail":
                got = out[0] if name == "advance" else out[1]
                counts.records[(kind, site)] += len(got)
            else:
                # anchor_at parses every line up to the one it anchors on
                counts.records[(kind, site)] += out.seq + 1
            return out
        return run

    def hashed(self):
        counts.hashed += 1
        return saved_hash(self)

    for name, kind in (("read_verified", "full"), ("advance", "tail"),
                       ("read_verified_from", "tail"),
                       ("anchor_at", "anchor_at")):
        setattr(log_cls, name, wrap(name, kind))
    setattr(ev_cls, "recompute_hash", hashed)
    try:
        yield counts
    finally:
        for name, fn in saved.items():
            setattr(log_cls, name, fn)
        setattr(ev_cls, "recompute_hash", saved_hash)


def measure(decisions: int) -> dict:
    """Run the campaign to ``decisions`` and count what it read."""
    import research_horizon as H
    campaign = H.Campaign(f"work-counts-{decisions}")
    counts = Counts()
    started = time.perf_counter()
    try:
        with counting(counts):
            campaign.run(decisions, invalidate_at=())
    finally:
        campaign.clean()
    return {"decisions": decisions,
            "seconds": round(time.perf_counter() - started, 1),
            "totals": counts.totals(), "by_site": counts.by_site()}


def growth(runs: list) -> list:
    """Each total's ratio to the run before it, with the size ratio."""
    out = []
    for before, after in zip(runs, runs[1:]):
        ratios = {}
        for key, now in after["totals"].items():
            was = before["totals"].get(key, 0)
            ratios[key] = None if not was else round(now / was, 2)
        out.append({"from": before["decisions"], "to": after["decisions"],
                    "size_ratio": round(after["decisions"]
                                        / before["decisions"], 2),
                    "ratios": ratios})
    return out


def report(sizes: list) -> dict:
    runs = [measure(n) for n in sizes]
    return {"schema": SCHEMA,
            "what": "counts of the event log's read paths under "
                    "tools/research_horizon.py's scripted campaign; "
                    "SCRIPTED DECISIONS, no model; counts, and this host's "
                    "seconds as a measurement",
            "sizes": sizes, "runs": runs, "growth": growth(runs)}


def _print(doc: dict) -> None:
    for run in doc["runs"]:
        t = run["totals"]
        print(f"decisions {run['decisions']:>5}: {run['seconds']:7.1f}s  "
              f"full {t['full_calls']:>5} reads, {t['full_records']:>9} "
              f"records  tail {t['tail_calls']:>6} reads, "
              f"{t['tail_records']:>7} records  anchor_at "
              f"{t['anchor_at_calls']:>4}  hashed {t['hashed']:>9}")
    for g in doc["growth"]:
        r = g["ratios"]
        print(f"{g['from']} -> {g['to']} (x{g['size_ratio']}): full records "
              f"x{r['full_records']}, full reads x{r['full_calls']}, tail "
              f"records x{r['tail_records']}, hashed x{r['hashed']}")
    last = doc["runs"][-1]
    print(f"\nby site at {last['decisions']} decisions:")
    for row in last["by_site"][:15]:
        print(f"  {row['kind']:9s} {row['calls']:6d} calls "
              f"{row['records']:9d} records  {row['site']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sizes", default="15,30,60",
                    help="comma-separated campaign sizes, in decisions")
    ap.add_argument("--write", action="store_true",
                    help=f"record the result in {RECORD.relative_to(ROOT)}")
    args = ap.parse_args(argv)
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    if not sizes or any(n < 1 for n in sizes):
        raise SystemExit("--sizes needs positive campaign sizes")
    doc = report(sizes)
    _print(doc)
    if args.write:
        RECORD.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"wrote {RECORD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
