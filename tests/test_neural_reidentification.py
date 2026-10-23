"""The NF-1T re-identification record holds against the evidence, both sides.

``docs/neural/reidentification_record.json`` says the development dataset
was regenerated from a source closure whose only change was wording, that
its dataset digest did not move, that every sample differs only in four
provenance fields, and that retraining reproduced the checkpoint byte for
byte. Each of those is re-derived here rather than read: the AFTER side
from the committed evidence and the code as it is, the BEFORE side from git
history at the commit the record names. A record that is only read is a
claim on trust.

The history half needs the commit to be fetched. Without it the half skips,
unless ``HARNESS_HISTORY_REQUIRED=1`` (the full-suite CI job, which fetches
history) turns the skip into a failure.

Software verification only: identical bytes say the wording changed
nothing the model computes or learns from, never that the model is right.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECORD = ROOT / "docs" / "neural" / "reidentification_record.json"
DEV = ROOT / "docs" / "neural" / "dev"
PROVENANCE_ONLY = {"provenance/generation_timestamp",
                   "provenance/generator_version/implementation_digest",
                   "provenance/source_artifact/digest",
                   "provenance/source_commit"}


def _rec() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def _manifest() -> dict:
    return json.loads((DEV / "dataset_manifest.json").read_text(
        encoding="utf-8"))


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _at(commit: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _history(commit: str) -> None:
    r = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e",
                        f"{commit}^{{commit}}"], capture_output=True)
    if r.returncode != 0:
        if os.environ.get("HARNESS_HISTORY_REQUIRED") == "1":
            pytest.fail(f"commit {commit} is not in this checkout, and "
                        "HARNESS_HISTORY_REQUIRED=1")
        pytest.skip(f"commit {commit} not fetched (shallow checkout)")


def test_the_record_is_labelled_as_provenance_only():
    rec = _rec()
    assert rec["schema"] == "nf1t-reidentification/1"
    assert "NOT_EVIDENCE_OF_CORRECTNESS" in rec["label"]
    assert "NON_AUTHORITATIVE" in rec["does_not_mean"]


def test_the_after_side_is_the_committed_evidence_and_the_code():
    from scientific.models.surface_adsorption import SurfaceAdsorptionModel
    from scientific import catalog
    rec, man = _rec(), _manifest()
    assert rec["generator"]["after"] == man["source_identifiers"][0]
    m = SurfaceAdsorptionModel()
    digest = catalog.models().digest_of(m.model_id, m.model_version)
    assert rec["generator"]["after"].endswith("#" + digest), (
        "the record names a generator the code no longer is")
    assert rec["dataset_digest"]["after"] == man["dataset_digest"]
    raw = (DEV / "dataset.jsonl").read_bytes()
    assert rec["dataset_storage_sha256"]["after"] == _sha(raw) \
        == man["storage"]["sha256"]
    ckpt = (DEV / "checkpoint_final.safetensors").read_bytes()
    assert rec["checkpoint_sha256"]["after"] == _sha(ckpt)


def test_what_the_record_says_did_not_move_did_not():
    rec = _rec()
    assert rec["dataset_digest"]["before"] == rec["dataset_digest"]["after"]
    assert rec["checkpoint_sha256"]["before"] == \
        rec["checkpoint_sha256"]["after"]
    assert rec["generator"]["before"] != rec["generator"]["after"], (
        "a re-identification whose generator did not change re-identified "
        "nothing")
    assert set(rec["fields_that_differ_in_every_sample"]) == PROVENANCE_ONLY
    assert rec["fields_that_differ_in_any_sample_otherwise"] == []


def test_the_before_side_is_what_history_holds():
    rec = _rec()
    commit = rec["previous_evidence_commit"]
    _history(commit)
    man = json.loads(_at(commit, "docs/neural/dev/dataset_manifest.json"))
    assert rec["generator"]["before"] == man["source_identifiers"][0]
    assert rec["dataset_digest"]["before"] == man["dataset_digest"]
    assert rec["dataset_storage_sha256"]["before"] == man["storage"]["sha256"]
    ckpt = _at(commit, "docs/neural/dev/checkpoint_final.safetensors")
    assert rec["checkpoint_sha256"]["before"] == _sha(ckpt)


def _leaves(x, p=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _leaves(v, f"{p}/{k}" if p else k)
    else:
        yield p, x


def test_every_sample_differs_only_in_provenance():
    """Re-derived over all samples, not read from the record."""
    rec = _rec()
    commit = rec["previous_evidence_commit"]
    _history(commit)
    old = _at(commit, "docs/neural/dev/dataset.jsonl").decode().splitlines()
    new = (DEV / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(old) == len(new) == rec["samples_compared"]
    everywhere = None
    for a, b in zip(old, new):
        la, lb = dict(_leaves(json.loads(a))), dict(_leaves(json.loads(b)))
        assert la.keys() == lb.keys()
        differ = {k for k in la if la[k] != lb[k]}
        assert differ <= PROVENANCE_ONLY, sorted(differ - PROVENANCE_ONLY)
        everywhere = differ if everywhere is None else everywhere & differ
    assert everywhere == PROVENANCE_ONLY
