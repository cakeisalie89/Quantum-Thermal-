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

Usage:  python analysis/collect_container_3d.py <destination-dir>
"""
from __future__ import annotations

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


def main(argv: list) -> int:
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

    (meta / "fingerprint.json").write_text(
        json.dumps(fingerprint(), indent=1, sort_keys=True), encoding="utf-8")

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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
