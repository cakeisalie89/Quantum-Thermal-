#!/usr/bin/env python3
"""Deterministic stage-level driver for the canonical simulation.

MODEL-ONLY / FORECAST-ONLY. This driver is ADDITIVE runtime-resilience
tooling: it executes exactly the same four calls, in the same order, with
the same arguments and seed, as ``python3 qta_full_sim.py`` — but allows
them to be run one stage per process invocation with fail-closed
checkpoint markers, so the pipeline can complete under an external
per-command wall ceiling. The single-command ``python3 qta_full_sim.py``
remains the authoritative clean full-run release path; this driver exists
to produce the identical byte-for-byte output set when that path is
externally time-limited.

Stages (identical to qta_full_sim.py __main__):
  1  core     -> qta_full_sim.main(); qta_full_sim.print_engineering_fixes()
  2  layers   -> run_integrated_layers(profile='standard', output_dir=outputs, seed=42)
  3  three_d  -> run_3d_all(outputs, heavy=False)

Checkpoint rules (all enforced):
- a marker is written ONLY after its stage function returns successfully;
- the marker binds: driver+orchestrator source hashes, the full
  qta_multiphysics tree hash, interpreter/numpy/scipy/qutip versions,
  the stage's declared output files with their SHA-256 hashes;
- on start of any stage, existing markers are re-verified hash-by-hash
  and configuration-by-configuration; ANY difference fails closed
  (the stage reruns; nothing is merged across incompatible runs);
- marker writes are atomic (tmp file + os.replace);
- a stage killed mid-run leaves no marker and is never treated complete;
- final acceptance is external: the 88 governed outputs must compare
  byte-identical to the canonical roots (the driver never asserts
  scientific success itself).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"
CKDIR = ROOT / ".sim_stage_checkpoints"

# outputs each stage is responsible for (prefix match on outputs/ names),
# derived from the orchestrator's own save messages; used for marker
# hashing only — never to fabricate or trim the scientific output set.
STAGE_OUTPUT_PREFIXES = {
    "core": None,        # stage 1 writes into repo root (gate table etc.)
    "layers": None,      # design_*, nv_*, bayesian, integrated summary
    "three_d": None,     # remaining 3D families
}
STAGES = ("core", "layers", "three_d")


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(ROOT.joinpath("qta_multiphysics").rglob("*.py")):
        h.update(str(p.relative_to(ROOT)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _env_id() -> dict:
    import numpy
    import qutip
    import scipy
    return {"python": sys.version.split()[0], "numpy": numpy.__version__,
            "scipy": scipy.__version__, "qutip": qutip.__version__}


def _config_id() -> dict:
    return {"orchestrator_sha256": _sha_file(ROOT / "qta_full_sim.py"),
            "driver_sha256": _sha_file(Path(__file__)),
            "qta_multiphysics_tree_sha256": _tree_hash(),
            "environment": _env_id(),
            "argv_profile": "standard", "seed": 42, "heavy": False}


def _snapshot() -> dict:
    """Hashes of every file a stage may write: outputs/* plus the
    root-level canonical csv/json families (stage 'core' writes at
    root)."""
    files = {}
    if OUT.exists():
        for p in sorted(OUT.iterdir()):
            # deep_surrogate_readiness.json is by-design nondeterministic
            # and exempt from byte gating project-wide (checker policy);
            # binding it would spuriously invalidate valid checkpoints.
            if p.name == "deep_surrogate_readiness.json":
                continue
            if p.is_file():
                files["outputs/" + p.name] = _sha_file(p)
    for pat in ("*.csv", "*.json"):
        for p in sorted(ROOT.glob(pat)):
            # packaging metadata is not a stage output; binding it would
            # let routine manifest regeneration invalidate valid science
            # checkpoints (fail-closed in the wrong direction)
            if p.name in ("final_manifest.json",):
                continue
            if p.is_file():
                files[p.name] = _sha_file(p)
    return files


def _marker_path(stage: str) -> Path:
    return CKDIR / f"{stage}.checkpoint.json"


def _write_marker(stage: str, before: dict, wall_s: float) -> None:
    after = _snapshot()
    produced = {k: v for k, v in after.items()
                if k not in before or before[k] != v}
    marker = {"stage": stage, "config": _config_id(),
              "outputs_after_stage": after,
              "produced_or_updated": produced,
              "wall_s": round(wall_s, 2),
              "note": ("written only after successful completion; "
                       "fail-closed on any config or hash difference")}
    CKDIR.mkdir(exist_ok=True)
    tmp = _marker_path(stage).with_suffix(".tmp")
    tmp.write_text(json.dumps(marker, indent=1, sort_keys=True))
    os.replace(tmp, _marker_path(stage))


def _marker_valid(stage: str) -> bool:
    mp = _marker_path(stage)
    if not mp.exists():
        return False
    try:
        m = json.loads(mp.read_text())
    except Exception:
        print(f"  [checkpoint] {stage}: unreadable marker -> rerun")
        return False
    if m.get("config") != _config_id():
        print(f"  [checkpoint] {stage}: configuration/source/environment "
              "hash differs -> fail closed, rerun")
        return False
    for name, sha in m.get("outputs_after_stage", {}).items():
        p = (OUT / name[len("outputs/"):]) if name.startswith("outputs/") \
            else (ROOT / name)
        if not p.exists() or _sha_file(p) != sha:
            print(f"  [checkpoint] {stage}: output '{name}' missing or "
                  "hash-mismatched -> fail closed, rerun")
            return False
    return True


def _run_stage(stage: str) -> None:
    before = _snapshot()
    t0 = time.time()
    if stage == "core":
        import qta_full_sim as sim
        sim.main()
        sim.print_engineering_fixes()
    elif stage == "layers":
        from qta_multiphysics.integrated_layers import run_integrated_layers
        print("\n[integrated forecast layers: profile=standard, Mode C "
              "readiness enforced]")
        run_integrated_layers(profile="standard", output_dir=OUT, seed=42)
    elif stage == "three_d":
        from qta_multiphysics.runner_3d import run_3d_all
        run_3d_all(OUT, heavy=False)
    else:
        raise SystemExit(f"unknown stage '{stage}'")
    wall = time.time() - t0
    _write_marker(stage, before, wall)
    print(f"[stage '{stage}' complete in {wall:.1f} s; checkpoint written]")


def main(argv: list) -> int:
    want = argv[1:] or list(STAGES)
    bad = [w for w in want if w not in STAGES and w != "status"]
    if bad:
        print(f"unknown stage(s) {bad}; valid: {STAGES} or 'status'")
        return 2
    if want == ["status"]:
        for s in STAGES:
            print(f"  {s}: {'VALID' if _marker_valid(s) else 'absent/invalid'}")
        return 0
    last = max(STAGES.index(w) for w in want)
    for i, s in enumerate(STAGES):
        if i > last:
            break                      # later stages simply not reached
        if s not in want:              # earlier prerequisite
            if not _marker_valid(s):
                print(f"[prerequisite stage '{s}' has no valid "
                      "checkpoint -> refusing out-of-order execution]")
                return 3
            print(f"[stage '{s}': valid checkpoint verified -> skipped]")
            continue
        if _marker_valid(s):
            print(f"[stage '{s}': valid checkpoint verified -> skipped]")
            continue
        _run_stage(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
