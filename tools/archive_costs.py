#!/usr/bin/env python3
"""What an archived segment costs to check, against the log it came from.

For each size, a log of real records (a valid chain, each record its
canonical line, written in one pass rather than through ``append`` so that
building 100,000 records takes seconds, not an fsync each) is read verified
from its first record, sealed into segments, and checked three ways:

* the whole log, from genesis, by its own reader (the baseline);
* the whole archive, from genesis, against the head's tree;
* one segment alone (the first, the middle, the last) against the head's
  tree: its own bytes, its frontier and a consistency proof.

Printed as JSON: bytes, hashes and wall time, on the host it runs on. The
times are this machine's and vary between runs; the bytes and the hash
counts do not.

    python tools/archive_costs.py --sizes 1000,10000,100000 --every 1000
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import archive as A  # noqa: E402
from agent_substrate import transparency as T  # noqa: E402
from agent_substrate.canonical import (  # noqa: E402
    CANONICAL_FORM_VERSION, ZERO_DIGEST, canonical_bytes, digest,
)
from agent_substrate.events import (  # noqa: E402
    PROFILE_UNAUTHENTICATED_LEGACY, EventLog,
)


def _write_log(path: Path, n: int) -> None:
    prev = ZERO_DIGEST
    with path.open("wb") as fh:
        for i in range(n):
            body = {"seq": i, "event_id": f"e{i:08d}",
                    "wall_time": 1_800_000_000.0 + i, "actor": "writer",
                    "action": "note.fixture", "target": f"t{i}",
                    "payload": {"i": i}, "prev_hash": prev,
                    "canonical_form_version": CANONICAL_FORM_VERSION}
            prev = digest(body)
            fh.write(canonical_bytes(dict(body, hash=prev)) + b"\n")


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, round(time.perf_counter() - t0, 4)


def measure(n: int, every: int, tmp: Path) -> dict:
    path = tmp / f"log{n}.jsonl"
    _write_log(path, n)
    log = EventLog(path)
    (report, events), t_full = _timed(log.read_verified)
    report.raise_if_bad()
    (manifest, blobs), t_seal = _timed(lambda: A.seal(log, every))
    head = {"size": n, "root": T.root(T.entries_of(events)).hex()}
    _, t_archive = _timed(lambda: A.verify_archive(
        manifest, blobs.__getitem__, proof=[], **head))
    segs = manifest["segments"]
    one = {}
    for label, rec in (("first", segs[0]), ("middle", segs[len(segs) // 2]),
                       ("last", segs[-1])):
        proof = [h.hex() for h in T.consistency_proof(
            rec["end"], T.entries_of(events))] if rec["end"] < n else []
        _, t = _timed(lambda: A.verify_segment(
            rec, blobs[rec["digest"]], proof=proof,
            profile=PROFILE_UNAUTHENTICATED_LEGACY, **head))
        one[label] = {"records": rec["end"] - rec["start"],
                      "bytes": len(blobs[rec["digest"]]),
                      "frontier_hashes": len(rec["frontier"]),
                      "proof_hashes": len(proof), "seconds": t}
    return {"records": n, "log_bytes": path.stat().st_size,
            "segments": len(segs),
            "manifest_bytes": len(json.dumps(manifest).encode()),
            "full_verified_read_seconds": t_full,
            "seal_seconds": t_seal,
            "verify_archive_seconds": t_archive,
            "verify_one_segment": one}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", default="1000,10000,100000")
    ap.add_argument("--every", type=int, default=1000)
    args = ap.parse_args(argv)
    sizes = [int(s) for s in args.sizes.split(",")]
    with tempfile.TemporaryDirectory() as tmp:
        rows = [measure(n, args.every, Path(tmp)) for n in sizes]
    print(json.dumps({"every": args.every, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
