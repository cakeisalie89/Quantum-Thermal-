#!/usr/bin/env python3
"""Deterministically regenerate final_manifest.json and manifest_hash.txt.

COVERAGE BOUNDARY (the thing this file is the authority for).

The manifest answers "what bytes are in this repository, and what were their
hashes" -- it is a PROVENANCE record, not an AUTHORITY record. Those are
different questions and the project answers them in different places:

  * provenance -- final_manifest.json: every git-tracked file except the two
    intentionally-detached ones. Inclusion here says only "these bytes were
    present and this was their SHA-256". It confers no scientific standing.
  * authority  -- AUTHORITIES.md / authorities.json: which module owns which
    concept. That is where "is this governed?" is answered.

Do not add exclusions to make a manifest diff smaller: an unhashed tracked
file is an unrecorded byte, which is the failure mode this file exists to
prevent.

Each entry is a path, its byte size and its SHA-256, and nothing else: a
narrative field with no generator is a claim nothing re-derives. Entries are
in sorted path order, so a regeneration is a reviewable diff -- an added
file appears where its name sorts, a removed one disappears.

Run from the repository root:  python3 generate_manifest.py
Verify without writing:        python3 generate_manifest.py --check
Determinism: JSON with indent=2, ensure_ascii=True and no trailing newline;
no timestamps and no host paths.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "final_manifest.json"
HASHFILE = ROOT / "manifest_hash.txt"
DETACHED = {"final_manifest.json", "manifest_hash.txt"}

SCHEMA = "harness-manifest/2"
PACKAGE = "scientific-ai-harness"
SELF_HASH_POLICY = ("DETACHED MANIFEST HASHING. final_manifest.json cannot "
                    "contain its own hash; manifest_hash.txt records it and "
                    "is not listed in files[].")

# Machine-readable statement of the boundary above, emitted into the manifest
# so a reader of the artifact does not have to come here to learn its scope.
COVERAGE_POLICY = {
    "schema_version": "2.0.0",
    "scope": "every git-tracked file in this repository",
    "record_type": "PROVENANCE",
    "means": "these bytes were present at this SHA-256",
    "does_not_mean": "that the file is a governed scientific authority, a "
                     "release artifact, or evidence that anything in it is "
                     "correct",
    "authority_register": "AUTHORITIES.md / authorities.json answer 'what "
                          "is governed'; this manifest answers 'what bytes "
                          "exist'",
    "exclusions": sorted(DETACHED),
    "exclusion_reason": "final_manifest.json cannot hash itself; "
                        "manifest_hash.txt is its detached hash, written "
                        "after the manifest is finalized",
    "ordering": "sorted by path",
    "drift_check": "python3 generate_manifest.py --check",
}


def _tracked_files() -> list:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return sorted(f for f in out.splitlines() if f and f not in DETACHED)


def _untracked_but_not_ignored() -> list:
    """Files git can see, does not track, and has not been told to ignore.

    THE TRAP THIS CLOSES

    The manifest is built from ``git ls-files``, which sees TRACKED files
    only. Regenerating it while a new file is still untracked produces a
    manifest that is correct at that instant and stale the moment the file
    is added -- and the staleness surfaces in CI, on a runner, after the
    commit, as "tracked but not listed". So the check reports them. A file
    that is genuinely scratch belongs in ``.gitignore``, which is a decision
    someone makes once instead of a surprise everyone meets.
    """
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return sorted(line[3:].strip() for line in out.splitlines()
                  if line.startswith("?? "))


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check() -> int:
    """Verify the committed manifest against the working tree; write nothing.

    The committed manifest can be internally consistent (every hash it lists
    is correct) and still be wrong, because it silently omits tracked files
    added after it was last written. Membership is checked in BOTH
    directions.
    """
    if not MANIFEST.exists() or not HASHFILE.exists():
        print("MANIFEST DRIFT: final_manifest.json or manifest_hash.txt "
              "missing", file=sys.stderr)
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = manifest.get("files", [])
    listed = {e["filename"] for e in entries}
    tracked = set(_tracked_files())

    problems = []
    for key, want in (("schema", SCHEMA), ("package", PACKAGE),
                      ("coverage_policy", COVERAGE_POLICY),
                      ("self_hash_policy", SELF_HASH_POLICY)):
        if manifest.get(key) != want:
            problems.append(f"{key} is not the generator's")
    extra = sorted(set(manifest) - {"schema", "package", "coverage_policy",
                                    "files", "self_hash_policy"})
    if extra:
        problems.append(f"fields no generator writes: {extra}")
    for f in sorted(tracked - listed):
        problems.append(f"tracked but not listed: {f}")
    for f in sorted(listed - tracked):
        problems.append(f"listed but not tracked: {f}")
    for f in sorted(listed & DETACHED):
        problems.append(f"detached file must not be listed: {f}")
    if len(listed) != len(entries):
        problems.append("a file is listed more than once")

    for entry in entries:
        if set(entry) != {"filename", "size_bytes", "sha256"}:
            problems.append(f"entry fields are not the generator's: "
                            f"{entry.get('filename')}")
        path = ROOT / entry["filename"]
        if not path.exists():
            problems.append("listed but absent from the tree: "
                            f"{entry['filename']}")
            continue
        if _sha256(path) != entry["sha256"]:
            problems.append(f"sha256 mismatch: {entry['filename']}")
        if path.stat().st_size != entry["size_bytes"]:
            problems.append(f"size mismatch: {entry['filename']}")
    names = [e["filename"] for e in entries]
    if names != sorted(names):
        problems.append("files are not in sorted path order")

    stored = re.search(r"sha256:\s*([0-9a-fA-F]{64})", HASHFILE.read_text())
    if not stored:
        problems.append("manifest_hash.txt has no sha256 line")
    elif stored.group(1) != _sha256(MANIFEST):
        problems.append("manifest_hash.txt does not match final_manifest.json")

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
    # WHAT "IN SYNC" DOES NOT MEAN, said out loud because it misled someone.
    # This compares the manifest against the tracked tree. It says nothing
    # about whether two DERIVED artifacts agree with each other: the manifest
    # is last in the regeneration chain and hashes whatever it finds,
    # including a stale link earlier in the chain (D-2026-35).
    print("  (coverage and hashes only -- it does not check that derived "
          "artifacts agree with their sources; after changing a governed "
          "output run the regeneration order in MANIFEST_BOUNDARY.md and "
          "then the test suite)")
    return 0


def build() -> dict:
    files = []
    missing = []
    for f in _tracked_files():
        p = ROOT / f
        if not p.exists():
            missing.append(f)
            continue
        files.append({"filename": f, "size_bytes": p.stat().st_size,
                      "sha256": _sha256(p)})
    if missing:
        raise FileNotFoundError(f"tracked-but-missing files: {missing}")
    return {"schema": SCHEMA, "package": PACKAGE,
            "coverage_policy": COVERAGE_POLICY, "files": files,
            "self_hash_policy": SELF_HASH_POLICY}


def main() -> int:
    if "--check" in sys.argv[1:]:
        return check()
    try:
        manifest = build()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(manifest, indent=2, ensure_ascii=True)
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
    print(f"regenerated final_manifest.json ({len(manifest['files'])} files, "
          f"{size} bytes)")
    print(f"regenerated manifest_hash.txt (sha256 {digest[:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
