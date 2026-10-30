#!/usr/bin/env python3
"""What one operation costs at several history sizes, measured five ways.

Phase 14 of the PR #17 audit (docs/audit/). Each operation runs in a FRESH
child process. The child first builds a history of N records with the
substrate's own writers, then runs the operation once. So no measurement
warms another's caches or shares its memory high-water mark. Around the
operation alone, each figure separately:

- ``wall_s``: ``time.perf_counter``.
- ``cpu_s``: user plus system time of the child (``resource.getrusage``).
  ``children_cpu_s`` is any subprocess the operation ran.
- ``read_bytes``: the change in ``rchar`` of ``/proc/self/io``. That is
  bytes asked of read(), page cache or disk, and None where the platform
  has no such file.
- ``disk_bytes``: the bytes under the history's directory after the
  operation.
- ``peak_alloc_bytes``: tracemalloc's peak, from a SECOND, traced child on
  an identical history, so that tracing does not distort the figures
  above. It is None for the campaign, whose traced run would cost as much
  again.
- ``max_rss_bytes``: the child's resident high-water mark. It is the whole
  process's, setup included, and is labelled so.

WHAT RUNS. The authority operations run on a history of N ``record.create``
records:

- ``append_warm`` and ``append_cold``: a writer that holds an anchor, and a
  fresh one;
- ``authority_transition``;
- ``projection_load`` (a full replay) and ``projection_update`` (a catch-up
  over ten new records);
- ``checkpoint_create`` and ``checkpoint_restore``;
- ``independent_reconstruction``.

Two more build their own histories:

- ``research_ledger_update``: N objectives, then one more;
- ``memory_retrieval``: N memories, then one recall.

``campaign`` is ``tools/research_horizon.py``'s scripted campaign to N
decisions. Each decision is a governed scientific run, with its task
transitions, so this is where those two are measured. Its decisions are
SCRIPTED DECISIONS: no model is called.

A measurement is of this host at this commit. Across sizes, each figure's
ratio to the size before says how it grows, and it is not a bound.

    python tools/op_costs.py                 # print
    python tools/op_costs.py --write         # record docs/audit/op_costs.json
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RECORD = ROOT / "docs" / "audit" / "op_costs.json"
SCHEMA = "pr17-audit-op-costs/1"
SIZES = (250, 500, 1000)
CAMPAIGN_SIZES = (4, 8, 16)
FIGURES = ("wall_s", "cpu_s", "children_cpu_s", "read_bytes", "disk_bytes",
           "peak_alloc_bytes", "max_rss_bytes")


def _rchar():
    """Bytes this process has asked of read() so far, or None."""
    try:
        with open("/proc/self/io", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("rchar:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _cpu(who: int) -> float:
    r = resource.getrusage(who)
    return r.ru_utime + r.ru_stime


def _disk(root: Path) -> int:
    total = 0
    for base, _, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------- set-ups
# Each returns the operation, a callable of no arguments. A set-up may also
# return a second callable giving the footprint, for an operation whose
# history lives outside the child's directory. Everything an operation
# needs is built here, before anything is measured.

def _authority(root: Path, n: int):
    from agent_substrate.events import EventLog
    from agent_substrate.store import AuthorityStore
    log = EventLog(root / "log.jsonl")
    store = AuthorityStore(log).load()
    for i in range(n):
        store.create(record_id=f"r{i}", kind="claim", proposer="p")
    return log, store


def _create_payload(i: int) -> dict:
    return {"record_id": f"x{i}", "kind": "claim", "proposer": "p"}


def setup(op: str, n: int, root: Path):
    from agent_substrate.authority import Role, State
    from agent_substrate.events import EventLog
    from agent_substrate.store import AuthorityStore

    if op == "append_warm":
        log, _ = _authority(root, n)
        return (lambda: log.append(actor="p", action="record.create",
                                   target="x0", payload=_create_payload(0)))
    if op == "append_cold":
        _authority(root, n)
        return (lambda: EventLog(root / "log.jsonl").append(
            actor="p", action="record.create", target="x0",
            payload=_create_payload(0)))
    if op == "authority_transition":
        _, store = _authority(root, n)
        return (lambda: store.transition(
            record_id=f"r{n - 1}", dst=State.UNDER_REVIEW, actor="v",
            role=Role.VERIFIER))
    if op == "projection_load":
        _authority(root, n)
        return lambda: AuthorityStore(EventLog(root / "log.jsonl")).load()
    if op == "projection_update":
        _, store = _authority(root, n)
        other = AuthorityStore(EventLog(root / "log.jsonl")).load()
        for i in range(10):
            other.create(record_id=f"x{i}", kind="claim", proposer="p")
        return lambda: store.catch_up(force=True)
    if op in ("checkpoint_create", "checkpoint_restore"):
        from agent_substrate.checkpoint import CheckpointStore
        from agent_substrate.evidence import EvidenceStore
        _, store = _authority(root, n)
        cps = CheckpointStore(root / "checkpoints")
        blobs = EvidenceStore(root / "blobs")
        if op == "checkpoint_create":
            return lambda: store.checkpoint(cps, blobs=blobs)
        store.checkpoint(cps, blobs=blobs)
        return (lambda: AuthorityStore.load_from(
            EventLog(root / "log.jsonl"), cps, blobs=blobs, evidence=blobs,
            require_checkpoint=True))
    if op == "independent_reconstruction":
        from agent_substrate.reconstruct import reconstruct
        _authority(root, n)
        return lambda: reconstruct(EventLog(root / "log.jsonl"))
    if op == "research_ledger_update":
        from agent_substrate.research import ResearchLedger
        led = ResearchLedger(EventLog(root / "log.jsonl")).load()

        def objective(i: int) -> None:
            led.register(objective_id=f"o{i}", statement=f"question {i}",
                         owner="owner", max_steps=4, max_inference_calls=4)
        for i in range(n):
            objective(i)
        return lambda: objective(n)
    if op == "memory_retrieval":
        from agent_substrate.memory import MemoryStore
        mem = MemoryStore(EventLog(root / "log.jsonl")).load()
        for i in range(n):
            mem.remember(memory_id=f"m{i}", author="a",
                         text=f"note {i} on topic {i % 17} with word{i % 31}")
        return lambda: mem.recall("topic 3 word5", k=5)
    if op == "campaign":
        import research_horizon as H
        campaign = H.Campaign(f"op-costs-{n}-{os.getpid()}")
        held: dict = {}

        def run() -> None:
            try:
                campaign.run(n, invalidate_at=())
                held["bytes"] = _disk(campaign.w.base)
            finally:
                campaign.clean()
        return run, lambda: held.get("bytes")
    raise SystemExit(f"unknown operation {op!r}")


OPERATIONS = ("append_warm", "append_cold", "authority_transition",
              "projection_load", "projection_update", "checkpoint_create",
              "checkpoint_restore", "independent_reconstruction",
              "research_ledger_update", "memory_retrieval", "campaign")


def child(op: str, n: int, traced: bool) -> dict:
    """Build, run once, measure: the body of one child process."""
    with tempfile.TemporaryDirectory(prefix="op-costs-") as tmp:
        root = Path(tmp)
        made = setup(op, n, root)
        run, footprint = (made if isinstance(made, tuple)
                          else (made, lambda: _disk(root)))
        r0, c0, k0 = _rchar(), _cpu(resource.RUSAGE_SELF), _cpu(
            resource.RUSAGE_CHILDREN)
        if traced:
            tracemalloc.start()
        t0 = time.perf_counter()
        run()
        wall = time.perf_counter() - t0
        peak = None
        if traced:
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        r1 = _rchar()
        out = {"op": op, "n": n, "wall_s": round(wall, 6),
               "cpu_s": round(_cpu(resource.RUSAGE_SELF) - c0, 6),
               "children_cpu_s": round(
                   _cpu(resource.RUSAGE_CHILDREN) - k0, 6),
               "read_bytes": None if r0 is None or r1 is None else r1 - r0,
               "disk_bytes": footprint(), "peak_alloc_bytes": peak,
               # ru_maxrss is in kilobytes on Linux
               "max_rss_bytes": resource.getrusage(
                   resource.RUSAGE_SELF).ru_maxrss * 1024}
    return out


def measure(op: str, n: int) -> dict:
    """One untraced child for the timings, one traced for the peak."""
    def spawn(traced: bool) -> dict:
        done = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", op,
             str(n), "--traced", "1" if traced else "0"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
            timeout=3600)
        if done.returncode != 0:
            raise SystemExit(f"{op} at {n}: the child failed\n"
                             f"{done.stdout[-2000:]}{done.stderr[-2000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])
    row = spawn(False)
    if op != "campaign":
        row["peak_alloc_bytes"] = spawn(True)["peak_alloc_bytes"]
    return row


def growth(rows: list) -> list:
    """For each operation, each figure's ratio to the size before."""
    out = []
    for op in dict.fromkeys(r["op"] for r in rows):
        mine = sorted((r for r in rows if r["op"] == op),
                      key=lambda r: r["n"])
        for before, after in zip(mine, mine[1:]):
            ratios = {}
            for f in FIGURES:
                was, now = before.get(f), after.get(f)
                ratios[f] = (round(now / was, 2) if was and now is not None
                             else None)
            out.append({"op": op, "from": before["n"], "to": after["n"],
                        "size_ratio": round(after["n"] / before["n"], 2),
                        "ratios": ratios})
    return out


def report(sizes, campaign_sizes, ops) -> dict:
    rows = []
    for op in ops:
        for n in (campaign_sizes if op == "campaign" else sizes):
            rows.append(measure(op, n))
            print(f"{op:27s} n={n:<5d} wall {rows[-1]['wall_s']:9.4f}s "
                  f"cpu {rows[-1]['cpu_s']:8.4f}s", file=sys.stderr)
    return {"schema": SCHEMA,
            "what": "one operation per fresh child process, after the "
                    "child built its history; wall, cpu, read bytes, disk "
                    "and peak allocation measured separately; this host's "
                    "figures, at one commit; the campaign runs SCRIPTED "
                    "DECISIONS, no model",
            "host": {"python": sys.version.split()[0],
                     "platform": sys.platform,
                     "cpus": os.cpu_count()},
            "sizes": list(sizes), "campaign_sizes": list(campaign_sizes),
            "rows": rows, "growth": growth(rows)}


def _ints(text: str) -> tuple:
    out = tuple(int(x) for x in text.split(",") if x.strip())
    if not out or any(v < 1 for v in out):
        raise SystemExit("sizes must be positive integers")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--child", nargs=2, metavar=("OP", "N"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--traced", default="0", help=argparse.SUPPRESS)
    ap.add_argument("--sizes", default=",".join(map(str, SIZES)))
    ap.add_argument("--campaign-sizes",
                    default=",".join(map(str, CAMPAIGN_SIZES)))
    ap.add_argument("--ops", default=",".join(OPERATIONS))
    ap.add_argument("--write", action="store_true",
                    help=f"record the result in {RECORD.relative_to(ROOT)}")
    args = ap.parse_args(argv)
    if args.child:
        op, n = args.child[0], int(args.child[1])
        print(json.dumps(child(op, n, args.traced == "1"), sort_keys=True))
        return 0
    ops = tuple(o for o in args.ops.split(",") if o)
    unknown = sorted(set(ops) - set(OPERATIONS))
    if unknown:
        raise SystemExit(f"unknown operations {unknown}")
    doc = report(_ints(args.sizes), _ints(args.campaign_sizes), ops)
    for g in doc["growth"]:
        r = g["ratios"]
        print(f"{g['op']:27s} {g['from']}->{g['to']} (x{g['size_ratio']}): "
              f"wall x{r['wall_s']} cpu x{r['cpu_s']} read "
              f"x{r['read_bytes']} peak x{r['peak_alloc_bytes']}")
    if args.write:
        RECORD.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"wrote {RECORD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
