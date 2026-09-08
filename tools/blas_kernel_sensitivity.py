#!/usr/bin/env python3
"""Measure how much of the 3D output set depends on the BLAS kernel.

DIAGNOSTIC ONLY. Nothing here moves a gate, a threshold or an authority
state, and it writes nothing into the canonical tree. MODEL-ONLY /
FORECAST-ONLY. PASS remains 0.

WHY THIS EXISTS

R59 asks whether the 3D outputs regenerate byte-identically in another
environment. For a long time the answer was "they did in one container and we
could not read the artifact for the other", which is a story about
availability rather than an answer about numerics.

The answer is that the divergence is a function of the BLAS kernel. OpenBLAS
ships DYNAMIC_ARCH: one library containing several hand-written kernels, one
of which is selected at load time from the host CPU's feature flags. Those
kernels differ in blocking, vectorisation width and accumulation order, so
they differ in the last bits of a floating-point reduction -- and a CSV of
sixteen-significant-digit numbers records the last bits.

``OPENBLAS_CORETYPE`` forces the selection. That turns "another environment"
from something you have to go and find into a variable, on one machine, with
one interpreter, one dependency set and one set of inputs -- so a difference
observed here can only be the kernel.

WHAT THE ANSWER LOOKS LIKE, measured on the sandbox that wrote this file
(Intel Xeon @ 2.10GHz, AVX-512, 4 cores, numpy 2.4.4 / scipy 1.17.1):

    unset (selects SkylakeX)   63/63 byte-identical
    SkylakeX                   63/63 byte-identical
    Haswell                    43/63 -- 20 files differ
    Zen (resolves to Haswell)  43/63 -- the same 20
    Nehalem                    41/63 -- 22 files differ

and a GitHub ubuntu-latest runner, at the same commit, reported 8 differing
files through package_consistency_check.py -- a set contained in the Nehalem
list, and a smaller divergence than Haswell's, which is what a nearby but
not identical kernel looks like.

WHAT IT DOES NOT ESTABLISH

Not which answer is correct. Every kernel here computes the same problem to
the same order of accuracy; none of them is the true one, and a byte
comparison cannot rank them. What it establishes is that the committed
canonical outputs are the outputs of ONE kernel, so byte-identity is a claim
about a declared environment rather than about the mathematics.

Usage:
    python3 tools/blas_kernel_sensitivity.py            # the default sweep
    python3 tools/blas_kernel_sensitivity.py --cores SkylakeX,Haswell
    python3 tools/blas_kernel_sensitivity.py --out docs/blas_sensitivity.json

Each kernel is a full regeneration of the 3D output set, so the default
sweep takes several minutes. It is not wired into the ordinary test run for
that reason; it is a committed instrument, re-runnable by anyone who doubts
the numbers above.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Kernels worth forcing. Chosen to span the vector widths OpenBLAS
#: distinguishes rather than to enumerate its whole table: AVX-512, AVX2, and
#: SSE-era. A kernel the host cannot execute is not selected -- OpenBLAS
#: falls back -- which is why the report records the core that was ACTUALLY
#: selected next to the one that was asked for.
DEFAULT_CORES = ("SkylakeX", "Haswell", "Nehalem")


def _run_collector(dest: Path, coretype: str | None) -> dict:
    """One regeneration under one forced kernel. Returns its summary lines."""
    env = dict(os.environ)
    env.pop("OPENBLAS_CORETYPE", None)
    if coretype is not None:
        env["OPENBLAS_CORETYPE"] = coretype
    proc = subprocess.run(
        [sys.executable, "analysis/collect_container_3d.py",
         str(dest), "--emit-summary"],
        cwd=ROOT, env=env, capture_output=True, text=True)
    out: dict = {"asked_for": coretype, "exit_status": proc.returncode}
    for line in proc.stdout.splitlines():
        if line.startswith("::QTA-3D-ENV:: "):
            env_rec = json.loads(line.split(" ", 1)[1])
            out["selected"] = env_rec.get("openblas_runtime_core")
            out["numpy"] = env_rec.get("numpy")
            out["scipy"] = env_rec.get("scipy")
        elif line.startswith("::QTA-3D-COMPARISON:: "):
            out["comparison"] = json.loads(line.split(" ", 1)[1])
    if "comparison" not in out:
        out["error"] = (proc.stderr or proc.stdout)[-600:]
    return out


def sweep(cores) -> dict:
    results = []
    for coretype in [None, *cores]:
        scratch = Path(tempfile.mkdtemp(prefix="qta-blas-"))
        try:
            results.append(_run_collector(scratch / "run", coretype))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return {"label": "MODEL_ONLY / FORECAST_ONLY / DIAGNOSTIC",
            "automatic_gate_effect": "NONE",
            "scientific_PASS_count": 0,
            "does_not_mean": (
                "a kernel that reproduces the committed bytes is not a more "
                "correct kernel. This measures which outputs depend on the "
                "BLAS kernel, not which answer is right"),
            "runs": results}


def problems(report: dict) -> list:
    """Reasons this sweep did not measure what it claims to.

    ANTI-VACUITY. A sweep in which every forced kernel silently fell back to
    the same one would report "no difference anywhere" -- a true sentence
    about a comparison that varied nothing, and exactly the shape this
    repository has shipped once already.
    """
    found = []
    runs = report.get("runs", [])
    if len(runs) < 2:
        found.append(f"only {len(runs)} run(s); nothing was compared")
    failed = [r for r in runs if "comparison" not in r]
    if failed:
        found.append(
            f"{len(failed)} run(s) produced no comparison: "
            f"{[r.get('asked_for') for r in failed]}")
    selected = {r.get("selected") for r in runs if r.get("selected")}
    if len(selected) < 2:
        found.append(
            f"every run selected the same kernel {sorted(selected)}, so "
            "forcing OPENBLAS_CORETYPE changed nothing and the sweep varied "
            "no variable")
    return found


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cores", default=",".join(DEFAULT_CORES),
                    help="comma-separated OPENBLAS_CORETYPE values to force")
    ap.add_argument("--out", help="write the report to this JSON file")
    args = ap.parse_args(argv[1:])

    report = sweep([c for c in args.cores.split(",") if c])
    for run in report["runs"]:
        asked = run.get("asked_for") or "(unset)"
        if "comparison" not in run:
            print(f"{asked:<12} FAILED: {run.get('error', '')[:200]}")
            continue
        c = run["comparison"]
        print(f"{asked:<12} selected={run.get('selected'):<10} "
              f"{c['identical']}/{c['regenerated']} identical, "
              f"{c['differing']} differing")
        for name in c["differing_files"]:
            print(f"             differs: {name}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"wrote {args.out}")

    found = problems(report)
    if found:
        print("\nTHIS SWEEP DID NOT MEASURE WHAT IT REPORTS ON:",
              file=sys.stderr)
        for p in found:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
