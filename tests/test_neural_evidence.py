"""NF-1T s.57-60: the committed learned-model evidence re-derives.

``docs/neural/`` holds what ``tools/neural.py all`` wrote at a clean commit:
the governed development dataset, the training, resume and checkpoint
manifests, the final checkpoint's bytes, the evaluation, the architecture
manifests and meta validations, the distributed report and the claims.
Nothing here trusts those files: ``verify`` re-solves the budget, re-traces
the ~1T configuration abstractly, re-checks every digest link, reloads the
checkpoint and recomputes the claims in a fresh authority history -- the
same command the CI runs. The tests below then pin what the evidence must
never say.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))

from neural_support import require_jax  # noqa: E402

jax = require_jax()

import neural  # noqa: E402

from scientific_ai.neural import manifests  # noqa: E402


def _read(key):
    return json.loads((ROOT / neural.F[key]).read_text(encoding="utf-8"))


def test_the_committed_evidence_re_derives(capsys):
    rc = neural.cmd_verify(None)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "neural evidence: PASS" in out


def test_every_evidence_document_is_well_formed():
    for key in ("dataset_manifest", "training", "training_resume_a",
                "training_resume_b", "checkpoint", "checkpoint_resume_a",
                "checkpoint_resume_b", "evaluation", "distributed",
                "dev_meta", "dev_manifest", "flagship_meta",
                "flagship_manifest"):
        assert manifests.problems(_read(key)) == [], key


def test_the_evidence_was_generated_at_a_commit():
    for key in ("training", "flagship_manifest", "dev_manifest"):
        sc = _read(key)["source_commit"]
        assert isinstance(sc, str) and len(sc) == 40, (key, sc)


def test_the_checkpoint_bytes_are_the_recorded_ones():
    raw = (ROOT / neural.F["checkpoint_bytes"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == \
        _read("checkpoint")["checkpoint_digest"]


def test_the_dataset_file_is_the_manifest_s():
    dman = _read("dataset_manifest")
    lines = (ROOT / neural.F["dataset"]).read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == dman["sample_count"]


def test_the_flagship_is_counted_meta_validated_and_never_trained():
    man = _read("flagship_manifest")
    assert 950_000_000_000 <= man["trainable_parameters"] \
        <= 1_050_000_000_000
    assert 150_000_000_000 <= man["estimated_active_parameters_per_token"] \
        <= 250_000_000_000
    held = {c for c, v in man["claim_status"]["claims"].items()
            if v["holds"] and v["scope"] == "SUBJECT"}
    assert {"ARCHITECTURE_PARAMETER_VERIFIED",
            "ARCHITECTURE_META_VALIDATED"} <= held
    assert not held & {"DEVELOPMENT_MODEL_TRAINED", "LARGE_MODEL_TRAINED",
                       "DISTRIBUTED_HARDWARE_VALIDATED",
                       "SCIENTIFIC_PERFORMANCE_ESTABLISHED"}
    meta = _read("flagship_meta")
    assert meta["real_allocation_refused"] is True
    assert meta["allocation_guard"]["live_arrays_after"] == 0


def test_no_learned_record_was_accepted():
    claims = _read("claims")
    assert claims["acceptance_attempt"]["refused"] is True
    assert claims["semantics"] == list(manifests.PREDICTION_SEMANTICS)


@pytest.mark.parametrize("key", ["evaluation"])
def test_every_learned_output_is_a_non_authoritative_prediction(key):
    assert _read(key)["semantics"] == list(manifests.PREDICTION_SEMANTICS)
