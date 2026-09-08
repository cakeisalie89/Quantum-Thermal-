#!/usr/bin/env python3
"""DIAGNOSTIC ONLY -- capture what this environment actually regenerates.

Not authoritative. Nothing here can move a gate, a threshold or an evidence
state; it only records bytes and an environment fingerprint so the hosted
container's 3D outputs can be compared against the committed canonical copies
and against a fresh local regeneration.

It exists because the investigation sandbox cannot build the declared image
(the registry denies layer blobs), so artifact set C can only be produced on a
hosted runner.

PROVENANCE. Regeneration writes into an EMPTY directory supplied by the
caller, never the repository root, so the captured bytes cannot be the
committed copies read back by accident. The inventory records that claim
explicitly rather than leaving it implied.

Usage:  python analysis/collect_container_3d.py <dest-dir> [--emit-summary]

``--emit-summary`` prints the environment fingerprint and the
byte-comparison against the committed copies as ::QTA-3D-*::
lines. That matters because the artifact route, while
authenticated and working, hands back a signed URL on a storage
host some egress policies refuse -- and a job log is served by
the logs API with no redirect at all.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# Running this as `python analysis/collect_container_3d.py` puts analysis/ on
# sys.path, not the repository root, so the package import below fails in ANY
# environment -- including the container, where -w /qta is not enough. Anchor
# on the script's own location rather than the working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The Monte Carlo sample count qta_full_sim.py passes to run_all. Mirrored
#: here rather than left to run_all's default, which is 60. See main().
CANONICAL_MC_SAMPLES = 30


def openblas_runtime_core() -> str:
    """The kernel OpenBLAS actually SELECTED, or a reason it is unknown.

    Read from the bundled library rather than from ``numpy.show_config``,
    which reports what the wheel was BUILT with. DYNAMIC_ARCH picks a kernel
    per CPU at load time, so the build string and the running kernel are
    different facts and only the second one explains a byte difference.

    Never raises. A fingerprint that cannot be taken is recorded as absent;
    a diagnostic that fails to collect is worse than one that says it could
    not.
    """
    import glob

    import numpy
    try:
        libdir = os.path.join(
            os.path.dirname(os.path.dirname(numpy.__file__)), "numpy.libs")
        libs = sorted(glob.glob(os.path.join(libdir, "*openblas*.so*")))
        if not libs:
            return "UNKNOWN: no bundled openblas found"
        handle = ctypes.CDLL(libs[0])
        for name in ("scipy_openblas_get_corename64_",
                     "openblas_get_corename64_", "openblas_get_corename"):
            fn = getattr(handle, name, None)
            if fn is not None:
                fn.restype = ctypes.c_char_p
                return fn().decode("ascii", "replace")
        return "UNKNOWN: library exports no corename symbol"
    except Exception as exc:              # diagnostic: record, never fail
        return f"UNKNOWN: {type(exc).__name__}: {exc}"


def fingerprint() -> dict:
    fp: dict = {"python": sys.version.split()[0],
                "platform": platform.platform(),
                "machine": platform.machine()}
    import numpy
    import scipy
    fp["numpy"] = numpy.__version__
    fp["scipy"] = scipy.__version__
    for mod in ("h5py", "qutip"):
        try:
            fp[mod] = __import__(mod).__version__
        except Exception as e:            # diagnostic: record, never fail
            fp[mod] = f"UNAVAILABLE: {type(e).__name__}"
    try:
        cfg = numpy.show_config(mode="dicts")
        dep = cfg.get("Build Dependencies", {})
        fp["blas"] = dep.get("blas", {})
        fp["lapack"] = dep.get("lapack", {})
        fp["simd"] = cfg.get("SIMD Extensions", {})
    except Exception as e:
        fp["numpy_show_config"] = f"UNAVAILABLE: {type(e).__name__}"
    # THE FIELD THIS COMPARISON ACTUALLY TURNS ON, and it was not being
    # recorded. ``numpy.show_config`` reports the BUILD configuration; the
    # string it prints here says "Haswell" while OpenBLAS DYNAMIC_ARCH
    # selects SkylakeX at RUNTIME on the same machine. A cross-environment
    # comparison whose fingerprint names the wrong kernel is a comparison
    # nobody can attribute, and the kernel is not a detail: forcing it, on
    # one machine with one interpreter and one set of inputs, moves this
    # collector's verdict from 63/63 identical to 20 files differing.
    fp["openblas_runtime_core"] = openblas_runtime_core()
    fp["OPENBLAS_CORETYPE"] = os.environ.get("OPENBLAS_CORETYPE")
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "PYTHONHASHSEED", "LANG", "LC_ALL",
                "TZ", "GITHUB_SHA", "GITHUB_REF", "GITHUB_WORKFLOW",
                "GITHUB_RUN_ID", "RUNNER_OS"):
        fp[var] = os.environ.get(var)
    cpu: dict = {"count": os.cpu_count()}
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("model name") and "model" not in cpu:
                cpu["model"] = line.split(":", 1)[1].strip()
            elif line.startswith("flags") and "flags" not in cpu:
                cpu["flags"] = sorted(line.split(":", 1)[1].split())
    except OSError as e:
        cpu["error"] = f"{type(e).__name__}"
    fp["cpu"] = cpu
    try:
        fp["uname"] = subprocess.run(
            ["uname", "-a"], capture_output=True, text=True,
            timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        fp["uname"] = f"UNAVAILABLE: {type(e).__name__}"
    return fp


def compare_with_committed(inventory: dict) -> dict:
    """Regenerated bytes against the committed copies, file by file.

    THE MEASUREMENT R59 IS ABOUT, and the collector never made it. It
    captured hashes into an artifact and left the comparison to whoever
    downloaded the zip -- so when the artifact's storage host turned out to
    be unreachable, the question "what did the 8-file divergence count
    measure" had no answer anywhere.

    Doing the comparison HERE, and printing it, puts the answer in the job
    log: a route that is authenticated, served by the logs API, and not
    subject to a signed-URL redirect to a host an egress policy may refuse.

    THE COMMITTED COPIES ARE AT THE REPOSITORY ROOT, and this compared
    against ``outputs/`` instead. Two things were wrong with that, and the
    second is worse than the first.

    ``outputs/`` is gitignored. On a fresh checkout it does not exist, so
    every regenerated file landed in ``not_committed``, nothing was compared,
    and the summary reported zero differences over zero comparisons. A
    hosted run said exactly that, and the anti-vacuity step in the workflow
    is what refused it.

    Worse: on a machine where ``outputs/`` DOES exist -- any machine that has
    run the pipeline or the package checker, which removes and recreates it
    -- ``outputs/`` is itself a REGENERATION. So the comparison passed by
    comparing a regeneration against a regeneration: two readings of the same
    computation, which cannot disagree about the thing R59 is asking. The
    "63 of 63 byte-identical" this collector reported was measured against
    the wrong side, and the answer it was supposed to give was never taken.
    """
    same, differ, missing = [], [], []
    for name, rec in sorted(inventory.items()):
        committed = REPO_ROOT / name
        if not committed.is_file():
            missing.append(name)
            continue
        got = hashlib.sha256(committed.read_bytes()).hexdigest()
        (same if got == rec["sha256"] else differ).append(name)
    return {"regenerated": len(inventory), "identical": len(same),
            "differing": len(differ), "not_committed": len(missing),
            "differing_files": differ, "not_committed_files": missing,
            "identical_files": same}


def emit_summary(fp: dict, comparison: dict) -> None:
    """One machine-readable line per fact, greppable out of a job log."""
    print("::QTA-3D-ENV:: " + json.dumps(
        {k: fp.get(k) for k in ("python", "platform", "machine", "numpy",
                                "scipy", "h5py", "qutip",
                                "openblas_runtime_core", "OPENBLAS_CORETYPE",
                                "blas", "lapack",
                                "simd", "OMP_NUM_THREADS",
                                "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                                "PYTHONHASHSEED", "LANG", "TZ",
                                "GITHUB_SHA", "RUNNER_OS")},
        sort_keys=True, default=str))
    print("::QTA-3D-CPU:: " + json.dumps(
        {"model": fp.get("cpu", {}).get("model"),
         "count": fp.get("cpu", {}).get("count"),
         "avx": sorted(f for f in fp.get("cpu", {}).get("flags", [])
                       if f.startswith(("avx", "sse4", "fma")))},
        sort_keys=True, default=str))
    print("::QTA-3D-COMPARISON:: " + json.dumps(
        {k: v for k, v in comparison.items()
         if k != "identical_files"}, sort_keys=True))
    for name in comparison["differing_files"]:
        print(f"::QTA-3D-DIFFERS:: {name}")
    for name in comparison["not_committed_files"]:
        print(f"::QTA-3D-NOT-COMMITTED:: {name}")
    print(f"::QTA-3D-VERDICT:: {verdict_for(comparison)}")


#: How many canonical 3D outputs a real comparison is expected to cover. A
#: run that compared far fewer examined a corner of the question and must not
#: report on the whole of it.
EXPECTED_CANONICAL = 60


def verdict_for(c: dict) -> str:
    """The verdict, from a FULL accounting rather than from one field.

    WHY THIS IS NOT `"IDENTICAL" if not differing else ...`

    That is what it was, and a hosted run printed

        ::QTA-3D-VERDICT:: IDENTICAL (0/63 byte-identical ...)

    while comparing nothing at all: the collector had been looking for the
    canonical copies under the wrong path, so all 63 landed in
    ``not_committed``, ``differing`` was zero because nothing was compared,
    and "no differences" read as "no differences found". The anti-vacuity
    check of the day asserted only that 63 files were REGENERATED, which was
    true and beside the point.

    So IDENTICAL now requires the arithmetic to close:

    * something was regenerated at all;
    * every regenerated file had a committed copy to compare against;
    * identical + differing accounts for all of them, with nothing lost;
    * the coverage is the size a real comparison has.

    Each failure gets its own verdict string, because "we compared nothing"
    and "we compared everything and it matched" must never be one word.
    """
    regenerated = c.get("regenerated", 0)
    identical = c.get("identical", 0)
    differing = c.get("differing", 0)
    missing = c.get("not_committed", 0)
    tail = (f"({identical}/{regenerated} byte-identical to the committed "
            "copies)")

    if regenerated <= 0:
        return ("VACUOUS: nothing was regenerated, so nothing was compared "
                "and this run establishes nothing")
    if missing:
        return (f"INCOMPARABLE: {missing} of {regenerated} regenerated "
                "file(s) have no committed copy to compare against. A "
                "comparison that skipped them cannot report on them, and "
                f"reporting the rest as a verdict would be misleading {tail}")
    if identical + differing != regenerated:
        return (f"ACCOUNTING ERROR: {identical} identical + {differing} "
                f"differing != {regenerated} regenerated; the comparison "
                "lost track of files and none of its numbers can be trusted")
    if regenerated < EXPECTED_CANONICAL:
        return (f"UNDER-COVERED: only {regenerated} file(s) were compared, "
                f"below the {EXPECTED_CANONICAL} a real cross-environment "
                f"comparison covers. This examined a corner of the question "
                f"{tail}")
    if differing:
        return f"DIVERGENT ({differing} file(s)) {tail}"
    return f"IDENTICAL {tail}"


def main(argv: list) -> int:
    emit = "--emit-summary" in argv
    argv = [a for a in argv if a != "--emit-summary"]
    if len(argv) != 2:
        print(__doc__)
        return 2
    dest = Path(argv[1])
    meta = dest / "meta"
    out = dest / "outputs"
    meta.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        print(f"REFUSING: {out} is not empty; captured bytes must be freshly "
              "generated, not pre-existing", file=sys.stderr)
        return 1

    fp = fingerprint()
    (meta / "fingerprint.json").write_text(
        json.dumps(fp, indent=1, sort_keys=True), encoding="utf-8")

    # Both canonical 3D producers, into the same empty directory, exactly as
    # qta_full_sim.py sequences them -- INCLUDING its arguments.
    #
    # ``mc_samples=30`` is not a choice made here. qta_full_sim.py passes it,
    # and run_all's DEFAULT is 60. Omitting it made this collector regenerate
    # multiphysics_summary.json with twice the Monte Carlo samples, so the
    # file differed from the committed copy in every distribution -- in an
    # environment where all 62 other files were byte-identical. A diagnostic
    # that does not reproduce the pipeline it is diagnosing manufactures the
    # divergence it was built to explain, and every conclusion drawn from it
    # is about the tool. tests/test_container_diagnostic_fidelity.py asserts
    # these arguments still match qta_full_sim.py.
    from qta_multiphysics.runner_3d import run_3d_all
    from qta_multiphysics.runner import run_all
    run_3d_all(out, heavy=False, verbose=False)
    run_all(out, mc_samples=CANONICAL_MC_SAMPLES, verbose=False)

    inventory = {}
    for path in sorted(out.iterdir()):
        if path.is_file():
            data = path.read_bytes()
            inventory[path.name] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data)}
    (meta / "hashes.json").write_text(json.dumps({
        "provenance": "freshly regenerated in this environment into an empty "
                      "directory; NOT the committed repository copies",
        "github_sha": os.environ.get("GITHUB_SHA"),
        "generators": ["qta_multiphysics.runner_3d.run_3d_all",
                       "qta_multiphysics.runner.run_all"],
        "files": inventory}, indent=1, sort_keys=True), encoding="utf-8")
    print(f"captured {len(inventory)} freshly regenerated files into {out}")

    comparison = compare_with_committed(inventory)
    (meta / "comparison.json").write_text(
        json.dumps(comparison, indent=1, sort_keys=True), encoding="utf-8")
    if emit:
        emit_summary(fp, comparison)
    # DIAGNOSTIC ONLY: a divergence is reported, never a failure. This tool
    # cannot move a gate, and exiting non-zero on a byte difference would
    # make it one.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
