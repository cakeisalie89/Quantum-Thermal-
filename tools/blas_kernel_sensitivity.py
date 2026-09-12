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

#: The rows of the sweep, as ``(coretype, threads)``. ``None`` means "leave
#: it alone".
#:
#: THE THREAD ROW IS NOT DECORATION. Forcing the same kernel NAME on two
#: hosts did not make them agree: this sandbox at Haswell reproduced 43 of 63
#: committed files and a GitHub ubuntu-latest runner at Haswell reproduced
#: 40. Same kernel name, same interpreter, same dependency set, three files
#: apart -- so the kernel is a cause and is not the whole cause, and saying
#: "it is the kernel" without this row would have been a story that fitted
#: most of the data.
#:
#: OpenBLAS splits a reduction across threads and sums the partial results,
#: so the number of threads changes the accumulation order on its own. One
#: thread removes that variable.
#: numpy's OWN dispatch, which OPENBLAS_CORETYPE does not touch.
#:
#: This is the other half of the answer, and pinning the kernel alone hid it.
#: numpy compiles several versions of its element-wise loops and picks one
#: from the host's CPU features, entirely separately from BLAS. So this
#: sandbox at OPENBLAS_CORETYPE=Haswell still reproduced 43 of 63 committed
#: files while a GitHub runner at the same kernel reproduced 40 -- the BLAS
#: kernel matched and numpy's did not, because this machine has AVX-512 and
#: the runner does not.
#:
#: Dropping numpy to AVX2 as well reproduces the runner EXACTLY: 40 of 63,
#: and the same twenty-three files by name. That is what a complete
#: attribution looks like -- not "the numbers are close", but the same set.
AVX2_ONLY = "X86_V4 AVX512_ICL AVX512_SPR"

#: ``(coretype, threads, npy_disable)``. ``None`` means "leave it alone".
DEFAULT_ROWS = (
    (None, None, None),
    ("SkylakeX", None, None),
    ("Haswell", None, None),
    # Threads change the accumulation order in principle. Measured here, on
    # this workload, they change nothing -- which is worth a row precisely
    # because it rules a variable OUT rather than leaving it as a maybe.
    ("Haswell", 1, None),
    # THE ROW THAT REPRODUCES A DIFFERENT MACHINE.
    ("Haswell", None, AVX2_ONLY),
    ("Nehalem", None, None),
)


def _env(coretype: str | None, threads: int | None,
         npy_disable: str | None = None) -> dict:
    """The environment for one sweep row, with the variables it pins."""
    env = dict(os.environ)
    env.pop("OPENBLAS_CORETYPE", None)
    env.pop("NPY_DISABLE_CPU_FEATURES", None)
    if coretype is not None:
        env["OPENBLAS_CORETYPE"] = coretype
    if npy_disable is not None:
        env["NPY_DISABLE_CPU_FEATURES"] = npy_disable
    if threads is not None:
        for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                    "MKL_NUM_THREADS"):
            env[var] = str(threads)
    return env


def _probe(coretype: str | None) -> tuple:
    """Which kernel this host selects when asked for ``coretype``.

    A cheap import, not a regeneration. It answers the question that has to
    be asked BEFORE spending four minutes on a sweep row: can this CPU run
    the kernel at all?

    That distinction is the finding, not a detail. A GitHub ubuntu-latest
    runner asked for SkylakeX does not get SkylakeX -- it has no AVX-512 --
    and the sweep's first version recorded that as a FAILED run and went red.
    "The tool broke" and "this CPU cannot execute the kernel the committed
    outputs were produced with" are different statements, and only the second
    one is evidence.

    Returns ``(selected, error)``; exactly one is non-empty.
    """
    env = _env(coretype, None)
    code = ("import sys; sys.path.insert(0, %r);"
            "from analysis.collect_container_3d import openblas_runtime_core;"
            "print(openblas_runtime_core())" % str(ROOT))
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return "", (proc.stderr or proc.stdout).strip()[-300:] or (
            f"exit {proc.returncode} with no output")
    return proc.stdout.strip(), ""


def _run_collector(dest: Path, coretype: str | None,
                   threads: int | None = None,
                   npy_disable: str | None = None) -> dict:
    """One regeneration under one forced kernel. Returns its summary lines."""
    proc = subprocess.run(
        [sys.executable, "analysis/collect_container_3d.py",
         str(dest), "--emit-summary"],
        cwd=ROOT, env=_env(coretype, threads, npy_disable),
        capture_output=True, text=True)
    out: dict = {"asked_for": coretype, "threads": threads,
                 "npy_disable": npy_disable, "exit_status": proc.returncode}
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


def sweep(rows) -> dict:
    results = []
    for coretype, threads, npy_disable in rows:
        selected, error = _probe(coretype)
        if error or (coretype is not None and selected != coretype):
            # NOT A FAILURE. The host cannot run this kernel, which is a
            # fact about the host and is recorded as one. Skipping the
            # regeneration also saves four minutes per unsupported row.
            results.append({
                "asked_for": coretype, "threads": threads,
                "npy_disable": npy_disable, "unsupported": True,
                "selected": selected or None,
                "why": error or (
                    f"asked for {coretype}, this host selects "
                    f"{selected!r} instead -- the CPU does not support it"),
            })
            continue
        scratch = Path(tempfile.mkdtemp(prefix="qta-blas-"))
        try:
            results.append(
                _run_collector(scratch / "run", coretype, threads,
                               npy_disable))
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
    # A kernel this host cannot execute is an OBSERVATION, not a failed
    # measurement, and it does not count against the sweep. It is also the
    # single most useful row a hosted runner produces.
    measured = [r for r in runs if not r.get("unsupported")]
    if len(measured) < 2:
        found.append(
            f"only {len(measured)} run(s) measured anything; nothing was "
            "compared")
    failed = [r for r in measured if "comparison" not in r]
    if failed:
        found.append(
            f"{len(failed)} run(s) produced no comparison: "
            f"{[r.get('asked_for') for r in failed]}")
    selected = {r.get("selected") for r in measured if r.get("selected")}
    if len(selected) < 2:
        found.append(
            f"every run selected the same kernel {sorted(selected)}, so "
            "forcing OPENBLAS_CORETYPE changed nothing and the sweep varied "
            "no variable")
    return found


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cores", default="",
                    help="comma-separated OPENBLAS_CORETYPE values to force; "
                         "omit for the default sweep, which also pins "
                         "threads on one row")
    ap.add_argument("--out", help="write the report to this JSON file")
    args = ap.parse_args(argv[1:])

    rows = (DEFAULT_ROWS if not args.cores
            else tuple((c, None, None) for c in args.cores.split(",") if c))
    report = sweep(rows)
    for run in report["runs"]:
        asked = run.get("asked_for") or "(unset)"
        if run.get("threads") is not None:
            asked = f"{asked}@{run['threads']}t"
        if run.get("npy_disable"):
            asked = f"{asked}+avx2np"
        if run.get("unsupported"):
            print(f"{asked:<16} UNSUPPORTED ON THIS HOST: {run['why']}")
            continue
        if "comparison" not in run:
            print(f"{asked:<16} FAILED: {run.get('error', '')[:200]}")
            continue
        c = run["comparison"]
        print(f"{asked:<16} selected={run.get('selected'):<10} "
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
