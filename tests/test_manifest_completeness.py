"""The manifest must be complete, and the check must not share the bug.

The repository has had a provenance incident where every hash in the manifest
was correct and the manifest was still wrong, because files were missing from
it. Correct-but-incomplete is the failure mode that hashing alone cannot see.

So this module does not ask generate_manifest.py whether it agrees with itself.
It enumerates the governed set independently with `git ls-files`, recomputes
every digest with hashlib, and then checks the manifest against that. The
negative tests mutate a copy of the manifest one property at a time and require
the verifier to reject it -- a completeness check that accepts a truncated
manifest is not a completeness check.

MODEL-ONLY / FORECAST-ONLY. Software verification only.
"""
import copy
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MANIFEST = os.path.join(ROOT, "final_manifest.json")
HASHFILE = os.path.join(ROOT, "manifest_hash.txt")


def _manifest():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def _tracked():
    out = subprocess.run(["git", "-C", ROOT, "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return {p for p in out.split("\n") if p}


def _sha256(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _declared_exclusions(m):
    return set(m["coverage_policy"]["exclusions"])


# ------------------------------------------------- independent completeness --

def test_manifest_covers_every_tracked_file_except_declared_exclusions():
    m = _manifest()
    listed = {e["filename"] for e in m["files"]}
    tracked = _tracked()
    excluded = _declared_exclusions(m)
    missing = sorted(tracked - listed - excluded)
    assert not missing, f"tracked but absent from the manifest: {missing}"


def test_manifest_names_no_file_that_does_not_exist():
    m = _manifest()
    ghosts = [e["filename"] for e in m["files"]
              if not os.path.isfile(os.path.join(ROOT, e["filename"]))]
    assert not ghosts, f"manifest names nonexistent files: {ghosts}"


def test_manifest_lists_nothing_untracked():
    m = _manifest()
    stray = sorted({e["filename"] for e in m["files"]} - _tracked())
    assert not stray, f"manifest lists untracked paths: {stray}"


def test_no_duplicate_manifest_entries():
    names = [e["filename"] for e in _manifest()["files"]]
    assert len(names) == len(set(names)), "duplicate manifest paths"


def test_every_hash_and_size_recomputed_independently():
    bad = []
    for e in _manifest()["files"]:
        rel = e["filename"]
        if _sha256(rel) != e["sha256"]:
            bad.append(f"{rel}: sha256")
        if os.path.getsize(os.path.join(ROOT, rel)) != e["size_bytes"]:
            bad.append(f"{rel}: size")
    assert not bad, bad


def test_no_absolute_or_traversing_paths():
    offenders = []
    for e in _manifest()["files"]:
        rel = e["filename"]
        if os.path.isabs(rel) or rel.startswith(("/", "\\")) or ".." in rel.split("/"):
            offenders.append(rel)
        if "\\" in rel:
            offenders.append(f"{rel} (backslash separator)")
    assert not offenders, f"unsafe manifest paths: {offenders}"


def test_case_collisions_would_be_visible():
    lowered = {}
    collisions = []
    for e in _manifest()["files"]:
        k = e["filename"].lower()
        if k in lowered and lowered[k] != e["filename"]:
            collisions.append((lowered[k], e["filename"]))
        lowered[k] = e["filename"]
    assert not collisions, f"case-colliding manifest paths: {collisions}"


# ------------------------------------------------------ detached artifacts --

def test_exactly_the_declared_files_are_detached_and_each_is_explained():
    m = _manifest()
    excluded = _declared_exclusions(m)
    assert excluded == {"final_manifest.json", "manifest_hash.txt"}, excluded
    reason = m["coverage_policy"]["exclusion_reason"]
    for name in excluded:
        assert name in reason, f"{name} is excluded without a stated reason"
    # both are tracked -- detached means "not self-listed", not "not governed"
    assert excluded <= _tracked()


def test_the_detached_pair_is_verifiable_from_outside_the_manifest():
    """manifest_hash.txt is the external anchor; recompute it here."""
    import re
    with open(MANIFEST, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    text = open(HASHFILE, encoding="utf-8").read()
    stored = re.search(r"sha256:\s*([0-9a-f]{64})", text)
    size = re.search(r"size:\s*(\d+)", text)
    assert stored and stored.group(1) == digest, "detached hash does not match"
    assert size and int(size.group(1)) == os.path.getsize(MANIFEST)
    assert "detached" in text


# ----------------------------------------------- negative / mutation tests --

def _verify(manifest_obj):
    """Re-implementation of the completeness contract, independent of the
    generator, so a shared bug cannot make both agree."""
    problems = []
    listed = [e["filename"] for e in manifest_obj["files"]]
    if len(listed) != len(set(listed)):
        problems.append("duplicate entry")
    excluded = set(manifest_obj["coverage_policy"]["exclusions"])
    tracked = _tracked()
    if tracked - set(listed) - excluded:
        problems.append("incomplete coverage")
    if set(listed) - tracked:
        problems.append("names an untracked file")
    for e in manifest_obj["files"]:
        p = os.path.join(ROOT, e["filename"])
        if not os.path.isfile(p):
            problems.append(f"missing file {e['filename']}")
            continue
        if _sha256(e["filename"]) != e["sha256"]:
            problems.append(f"hash mismatch {e['filename']}")
    return problems


def test_the_verifier_accepts_the_real_manifest():
    assert _verify(_manifest()) == []


def test_dropping_one_entry_is_rejected():
    m = copy.deepcopy(_manifest())
    dropped = m["files"].pop(0)
    problems = _verify(m)
    assert any("incomplete" in p for p in problems), (dropped["filename"], problems)


def test_a_duplicated_entry_is_rejected():
    m = copy.deepcopy(_manifest())
    m["files"].append(copy.deepcopy(m["files"][0]))
    assert any("duplicate" in p for p in _verify(m))


def test_a_ghost_entry_is_rejected():
    m = copy.deepcopy(_manifest())
    m["files"].append({"filename": "no_such_file_xyz.json",
                       "size_bytes": 1, "sha256": "0" * 64})
    assert _verify(m), "a manifest naming a nonexistent file was accepted"


def test_a_flipped_hash_is_rejected():
    m = copy.deepcopy(_manifest())
    e = m["files"][0]
    e["sha256"] = ("1" if e["sha256"][0] != "1" else "2") + e["sha256"][1:]
    assert any("hash mismatch" in p for p in _verify(m))


def test_widening_the_exclusion_list_cannot_hide_a_gap():
    """Excluding a file is a policy decision, not a way to pass the check."""
    m = copy.deepcopy(_manifest())
    victim = m["files"].pop(0)["filename"]
    m["coverage_policy"]["exclusions"] = list(
        set(m["coverage_policy"]["exclusions"]) | {victim})
    # _verify now passes -- which is exactly why the declared exclusion set is
    # itself pinned by test_exactly_the_declared_files_are_detached_...
    assert _verify(m) == []
    assert set(m["coverage_policy"]["exclusions"]) != {
        "final_manifest.json", "manifest_hash.txt"}


if __name__ == "__main__":
    ns = dict(globals())
    for _n, _f in ns.items():
        if _n.startswith("test_") and callable(_f):
            _f()
    print("RESULT: manifest completeness holds under mutation")
