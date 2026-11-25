"""The container must contain every file the manifest governs.

`container_verify.sh` runs the manifest check inside the image. If
`.dockerignore` excludes a git-tracked file, that check cannot pass: the file
is listed in `final_manifest.json` but absent from the image, so it reports as
"missing file".

Hosted run 32618887858 failed exactly this way -- 18 mismatches, every one of
them under a directory `.dockerignore` excluded while the manifest governed
all 18.

There are two ways to make that green and only one of them is honest. Dropping
the files from the manifest would silence the check by shrinking what is
governed; including them in the image satisfies the check by actually shipping
what is governed. This test pins the second.
"""
from __future__ import annotations

import fnmatch
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _patterns() -> list:
    path = os.path.join(ROOT, ".dockerignore")
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.lstrip().startswith("#")]


def _governed() -> list:
    path = os.path.join(ROOT, "final_manifest.json")
    with open(path, encoding="utf-8") as fh:
        return [e["filename"] for e in json.load(fh)["files"]]


def _excluded_by(pattern: str, name: str) -> bool:
    if pattern.endswith("/"):
        return name.startswith(pattern)
    if "**" in pattern:
        base = pattern.split("**")[0]
        return name.startswith(base) and fnmatch.fnmatch(
            name, pattern.replace("**", "*"))
    if pattern.startswith("*"):
        return fnmatch.fnmatch(name, pattern)
    return name == pattern or name.startswith(pattern + "/")


def test_dockerignore_hides_no_manifest_governed_file():
    governed = _governed()
    assert governed, "manifest lists no files; the check would be vacuous"
    offenders = {}
    for pattern in _patterns():
        hit = [f for f in governed if _excluded_by(pattern, f)]
        if hit:
            offenders[pattern] = hit
    assert not offenders, (
        "these .dockerignore patterns exclude manifest-governed files, so the "
        "in-container manifest check cannot pass: "
        + "; ".join(f"{p} -> {len(v)} file(s), e.g. {v[0]}"
                    for p, v in offenders.items()))


def test_the_committed_learned_model_evidence_is_still_governed():
    """Guard the other half: a fix must not be to ungovern what the image
    would otherwise miss. The NF-1T evidence -- the dataset, the checkpoint
    bytes and their manifests -- is the largest governed tree, and the one a
    size-motivated exclusion would reach for first."""
    governed = set(_governed())
    for rel in ("docs/neural/dev/dataset.jsonl",
                "docs/neural/dev/dataset_manifest.json",
                "docs/neural/dev/checkpoint_final.safetensors",
                "docs/neural/dev/checkpoint_manifest.json"):
        assert rel in governed, (
            f"{rel} is not governed by the manifest; if it was dropped, the "
            "container check was made to pass by shrinking what is governed")


def test_the_check_would_notice_a_reintroduced_exclusion():
    """The matcher must actually match; a broken matcher passes everything."""
    governed = _governed()
    sample = next(f for f in governed if f.startswith("docs/neural/dev/"))
    assert _excluded_by("docs/neural/dev/", sample)
    assert _excluded_by("*.json", "final_manifest.json")
    assert not _excluded_by("verification/", sample)
