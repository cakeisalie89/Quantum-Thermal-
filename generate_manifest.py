#!/usr/bin/env python3
"""Deterministically regenerate final_manifest.json and manifest_hash.txt.

COVERAGE BOUNDARY (the thing this file is the authority for).

The manifest answers "what bytes are in this repository, and what were their
hashes" — it is a PROVENANCE record, not an AUTHORITY record. Those are
different questions and the project answers them in different places:

  * provenance  — final_manifest.json: every git-tracked file except the two
    intentionally-detached ones. Inclusion here says only "these bytes were
    present and this was their SHA-256". It confers no scientific standing.
  * authority   — AUTHORITIES.md / authorities.json: which module owns which
    concept. That is where "is this governed?" is answered.

The two are deliberately not the same set. `attic/delivery_artifacts/` is
described in README.md as "not part of the governed project" and yet is fully
hashed here, and always has been — precisely because a file can be preserved
and recorded without being authoritative. Do not add exclusions to make a
manifest diff smaller: an unhashed tracked file is an unrecorded byte, which
is the failure mode this file exists to prevent.

The manifest lists every git-tracked file EXCEPT the two intentionally-detached
files (final_manifest.json, manifest_hash.txt), each with its byte size and
SHA-256. Per-file generated_by/purpose and the canonical narrative fields
(package, verdict, mode_separation, pass_history, self_hash_policy) are preserved
from the existing manifest; canonical_state gate counts are recomputed from
results_gate_table.csv so they cannot drift. The detached hash policy is the one
verified by package_consistency_check.py Step 11.

Run from the repository root:  python3 generate_manifest.py
Verify without writing:        python3 generate_manifest.py --check
Determinism: JSON uses indent=2, ensure_ascii=True, and
the same key order and (no-trailing-newline) format as the canonical manifest.
Ordering: existing entries keep their position, new tracked files are appended
in sorted order (_manifest_order). The list as a whole is NOT sorted.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "final_manifest.json"
HASHFILE = ROOT / "manifest_hash.txt"
DETACHED = {"final_manifest.json", "manifest_hash.txt"}

# Machine-readable statement of the boundary above, emitted into the manifest
# so a reader of the artifact does not have to come here to learn its scope.
COVERAGE_POLICY = {
    "schema_version": "1.0.0",
    "scope": "every git-tracked file in this repository",
    "record_type": "PROVENANCE",
    "means": "these bytes were present at this SHA-256",
    "does_not_mean": "that the file is a governed scientific authority, a "
                     "release artifact, or an execution input",
    "authority_register": "AUTHORITIES.md / authorities.json answer 'what "
                          "is governed'; this manifest answers 'what bytes "
                          "exist'",
    "exclusions": sorted(DETACHED),
    "exclusion_reason": "final_manifest.json cannot hash itself; "
                        "manifest_hash.txt is its detached hash, written "
                        "after the manifest is finalized",
    "non_exclusions": "attic/delivery_artifacts/ is documented as outside "
                      "the governed project yet is fully hashed here — "
                      "preservation and authority are separate concepts",
    "drift_check": "python3 generate_manifest.py --check",
}

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


def _untracked_but_not_ignored() -> list:
    """Files git can see, does not track, and has not been told to ignore.

    THE TRAP THIS CLOSES

    The manifest is built from ``git ls-files``, which sees TRACKED files only.
    Regenerating it while a new file is still untracked produces a manifest
    that is correct at that instant and stale the moment the file is added --
    and the staleness surfaces in CI, on a runner, after the commit, as
    "tracked but not listed".

    That has happened repeatedly in this repository, in three different tools,
    always the same way: a local check reads the tracked set, the new file is
    not in it yet, everything looks fine. So the check reports them rather
    than leaving the next person to rediscover it. A file that is genuinely
    scratch belongs in ``.gitignore``, which is a decision someone makes once
    instead of a surprise everyone meets.
    """
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return sorted(line[3:].strip() for line in out.splitlines()
                  if line.startswith("?? "))


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _manifest_order(prev_order, tracked_set) -> list:
    """The documented ordering policy, in one place.

    Existing entries keep their position (so a regeneration produces a
    reviewable diff rather than a reshuffle), then genuinely new tracked files
    are appended in sorted order. Both halves are deterministic. This is NOT a
    globally sorted list, and the module docstring used to claim it was.
    """
    prev_set = set(prev_order)
    ordered = [f for f in prev_order if f in tracked_set]
    ordered += sorted(f for f in tracked_set if f not in prev_set)
    return ordered


#: canonical_state fields that are DERIVED from results_gate_table.csv and are
#: therefore recomputed on every write and re-verified by --check. Everything
#: else in canonical_state is curated narrative with no generator, and is
#: preserved verbatim.
DERIVED_STATE_FIELDS = ("total_gates", "PASS", "CONDITIONAL", "BLOCKED",
                        "UNKNOWN", "DERIVED_CHECK")


def _gate_state() -> dict | None:
    """Recompute the derived canonical_state fields from the gate table.

    Returns None when results_gate_table.csv is absent (a fixture repository),
    so callers can skip semantic verification rather than invent counts.
    """
    gate_csv = ROOT / "results_gate_table.csv"
    if not gate_csv.exists():
        return None
    with open(gate_csv, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    counts = Counter((r.get("status") or "").strip() for r in rows)
    state = {"total_gates": len(rows)}
    for f in DERIVED_STATE_FIELDS[1:]:
        state[f] = counts.get(f, 0)
    return state


def _recompute_canonical_state(prev: dict) -> dict:
    """Recompute the derived gate counts from results_gate_table.csv.

    These fields used to be preserved verbatim with a warning on mismatch,
    which meant a stale count could survive every regeneration and every
    --check. A warning is not verification: the counts are derived data and
    are now actually derived. Curated narrative fields are untouched.
    """
    cs = dict(prev)
    derived = _gate_state()
    if derived is None:
        return cs
    for field, value in derived.items():
        if prev.get(field) not in (None, value):
            print(f"NOTE: canonical_state.{field} {prev.get(field)} -> {value} "
                  f"(recomputed from results_gate_table.csv)", file=sys.stderr)
        cs[field] = value
    return cs


def check() -> int:
    """Verify the committed manifest against the working tree; write nothing.

    This is the guard whose absence let the manifest drift: the committed
    manifest can be internally consistent (every hash it lists is correct) and
    still be wrong, because it silently omits tracked files that were added
    after it was last written. Membership is checked in BOTH directions.
    """
    if not MANIFEST.exists() or not HASHFILE.exists():
        print("MANIFEST DRIFT: final_manifest.json or manifest_hash.txt "
              "missing", file=sys.stderr)
        return 1
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    listed = {e["filename"] for e in manifest.get("files", [])}
    tracked = set(_tracked_files())

    problems = []
    for f in sorted(tracked - listed):
        problems.append(f"tracked but not listed: {f}")
    for f in sorted(listed - tracked):
        problems.append(f"listed but not tracked: {f}")
    for f in sorted(listed & DETACHED):
        problems.append(f"detached file must not be listed: {f}")

    for entry in manifest.get("files", []):
        path = ROOT / entry["filename"]
        if not path.exists():
            problems.append("listed but absent from the tree: "
                            f"{entry['filename']}")
            continue
        if _sha256(path) != entry["sha256"]:
            problems.append(f"sha256 mismatch: {entry['filename']}")
        if path.stat().st_size != entry["size_bytes"]:
            problems.append(f"size mismatch: {entry['filename']}")

    stored = re.search(r"sha256:\s*([0-9a-fA-F]{64})", HASHFILE.read_text())
    if not stored:
        problems.append("manifest_hash.txt has no sha256 line")
    elif stored.group(1) != _sha256(MANIFEST):
        problems.append("manifest_hash.txt does not match final_manifest.json")

    # ---- semantic verification (distinct from byte coverage above) ----
    # Byte coverage answers "are these the bytes"; this answers "does the
    # manifest's narrative still describe them". A derived count that drifts
    # from its source is a defect, not a warning.
    semantic = []
    derived = _gate_state()
    if derived is not None:
        cs = manifest.get("canonical_state", {})
        for field, value in derived.items():
            if cs.get(field) != value:
                semantic.append(
                    f"canonical_state.{field}={cs.get(field)!r} but "
                    f"results_gate_table.csv gives {value!r}")
        if derived.get("PASS", 0) != 0:
            semantic.append(
                f"results_gate_table.csv contains {derived['PASS']} PASS rows; "
                "the package is forecast-only and PASS must be 0")
    listed_order = [e["filename"] for e in manifest.get("files", [])]
    if listed_order != _manifest_order(listed_order, tracked):
        semantic.append("file ordering does not match the documented policy "
                        "(existing order preserved, then new tracked files "
                        "appended in sorted order)")
    problems.extend(semantic)

    # Reported last, and as a problem rather than a note: a manifest
    # regenerated over a tree with untracked files is stale the moment they
    # are committed, and the failure surfaces on a runner rather than here.
    for f in _untracked_but_not_ignored():
        problems.append(
            f"untracked and not ignored: {f} -- it is absent from the "
            "manifest now and would be required in it the moment it is "
            "committed. Stage it before regenerating, or add it to "
            ".gitignore.")

    if problems:
        print(f"MANIFEST DRIFT ({len(problems)} problem(s)):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("run: python3 generate_manifest.py", file=sys.stderr)
        return 1
    print(f"manifest in sync ({len(listed)} files; "
          f"{len(DETACHED)} detached by policy)")
    return 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        return check()
    if not MANIFEST.exists():
        print("ERROR: final_manifest.json missing; cannot preserve canonical narrative.",
              file=sys.stderr)
        return 1
    prev = json.load(open(MANIFEST, encoding="utf-8"))
    prev_meta = {e["filename"]: e for e in prev.get("files", [])}
    prev_order = [e["filename"] for e in prev.get("files", [])]

    tracked = _tracked_files()
    ordered = _manifest_order(prev_order, set(tracked))

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
        "coverage_policy": COVERAGE_POLICY,
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
