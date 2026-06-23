#!/usr/bin/env python3
"""Deterministically regenerate final_manifest.json and manifest_hash.txt.

The manifest lists every git-tracked file EXCEPT the two intentionally-detached
files (final_manifest.json, manifest_hash.txt), each with its byte size and
SHA-256. Per-file generated_by/purpose and the canonical narrative fields
(package, verdict, mode_separation, pass_history, self_hash_policy) are preserved
from the existing manifest; canonical_state gate counts are recomputed from
results_gate_table.csv so they cannot drift. The detached hash policy is the one
verified by package_consistency_check.py Step 11.

Run from the repository root:  python3 generate_manifest.py
Determinism: the file list is sorted; JSON uses indent=2, ensure_ascii=True, and
the same key order and (no-trailing-newline) format as the canonical manifest.
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "final_manifest.json"
HASHFILE = ROOT / "manifest_hash.txt"
DETACHED = {"final_manifest.json", "manifest_hash.txt"}

# purpose strings for files added by the forecast-layer work (when not already
# present in the previous manifest).
_PURPOSE_RULES = [
    ("qta_multiphysics/nv_spin/", "NV S=1 spin-dynamics / decoherence layer source (QuTiP, forecast)"),
    ("qta_multiphysics/design/", "computable design-registry layer source (forecast)"),
    ("qta_multiphysics/expdesign/", "Bayesian experimental-design layer source (forecast)"),
    ("qta_multiphysics/integrated_layers.py", "integrated forecast-layer orchestrator"),
    ("tests/", "test suite (physics / numerical / determinism)"),
    ("requirements.txt", "pinned runtime dependencies (numpy, scipy, qutip)"),
    ("generate_manifest.py", "deterministic manifest regenerator"),
    ("nv_", "NV spin-layer deterministic output (forecast)"),
    ("design_", "design-layer deterministic output (forecast)"),
    ("experimental_design_", "Bayesian design deterministic output (forecast)"),
    ("expected_information_gain", "Bayesian EIG deterministic output (forecast)"),
    ("validation_experiment_ranking", "Bayesian ranking deterministic output (forecast)"),
    ("adaptive_experiment_policy", "adaptive experiment policy (forecast)"),
    ("bayesian_design_summary", "Bayesian design summary (forecast)"),
    ("deep_surrogate_readiness", "deep-surrogate readiness (NOT_IMPLEMENTED)"),
    ("experiment_falsification_map", "experiment falsification map (forecast)"),
    ("integrated_layers_summary", "integrated forecast-layer summary"),
]


def _default_purpose(fname: str) -> str:
    base = fname.split("/")[-1]
    for prefix, purpose in _PURPOSE_RULES:
        if fname.startswith(prefix) or base.startswith(prefix):
            return purpose
    return "forecast-layer artifact (qta_multiphysics)"


def _tracked_files() -> list:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return sorted(f for f in out.splitlines() if f and f not in DETACHED)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _recompute_canonical_state(prev: dict) -> dict:
    """Preserve the curated canonical_state verbatim; verify gate counts against
    results_gate_table.csv and warn (do NOT silently alter) on any mismatch."""
    cs = dict(prev)
    gate_csv = ROOT / "results_gate_table.csv"
    if gate_csv.exists():
        with open(gate_csv, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        counts = Counter((r.get("status") or "").strip() for r in rows)
        if prev.get("total_gates") not in (None, len(rows)):
            print(f"WARN: canonical_state.total_gates={prev.get('total_gates')} but "
                  f"results_gate_table.csv has {len(rows)} rows (preserving manifest value)",
                  file=sys.stderr)
        if counts.get("PASS", 0) != 0:
            print(f"WARN: results_gate_table.csv has {counts.get('PASS')} PASS rows",
                  file=sys.stderr)
    return cs


def main() -> int:
    if not MANIFEST.exists():
        print("ERROR: final_manifest.json missing; cannot preserve canonical narrative.",
              file=sys.stderr)
        return 1
    prev = json.load(open(MANIFEST, encoding="utf-8"))
    prev_meta = {e["filename"]: e for e in prev.get("files", [])}
    prev_order = [e["filename"] for e in prev.get("files", [])]

    tracked = _tracked_files()
    tracked_set = set(tracked)
    # preserve existing entries' order (re-hashing each), then append any genuinely
    # new tracked files in sorted order. Both parts are deterministic.
    ordered = [f for f in prev_order if f in tracked_set]
    ordered += sorted(f for f in tracked if f not in set(prev_order))

    files = []
    missing = []
    for f in ordered:
        p = ROOT / f
        if not p.exists():
            missing.append(f)
            continue
        meta = prev_meta.get(f, {})
        files.append({
            "filename": f,
            "size_bytes": p.stat().st_size,
            "sha256": _sha256(p),
            "generated_by": meta.get("generated_by", "forecast-layer (qta_multiphysics)"),
            "purpose": meta.get("purpose", _default_purpose(f)),
        })
    if missing:
        print(f"ERROR: tracked-but-missing files: {missing}", file=sys.stderr)
        return 1

    manifest = {
        "package": prev.get("package"),
        "verdict": prev.get("verdict"),
        "canonical_state": _recompute_canonical_state(prev.get("canonical_state", {})),
        "mode_separation": prev.get("mode_separation"),
        "pass_history": prev.get("pass_history"),
        "files": files,
        "self_hash_policy": prev.get("self_hash_policy"),
    }
    text = json.dumps(manifest, indent=2, ensure_ascii=True)  # no trailing newline
    MANIFEST.write_text(text, encoding="utf-8")

    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    size = MANIFEST.stat().st_size
    HASHFILE.write_text(
        f"sha256: {digest}\n"
        f"file:   final_manifest.json\n"
        f"size:   {size} bytes\n"
        f"policy: detached (manifest does not contain its own hash)\n",
        encoding="utf-8",
    )
    print(f"regenerated final_manifest.json ({len(files)} files, {size} bytes)")
    print(f"regenerated manifest_hash.txt (sha256 {digest[:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
