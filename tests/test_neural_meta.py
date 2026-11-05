"""NF-1T s.31-32, s.49-52, s.61: the flagship without its weights, and
what may be said about it."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neural_support import require_jax  # noqa: E402

jax = require_jax()

from scientific.identity import digest  # noqa: E402
from scientific_ai.neural import (accounting, claims,  # noqa: E402
                                  documents, family, manifests, solver)
from scientific_ai.neural.model import meta  # noqa: E402

FLAGSHIP_TRAINABLE = 996_509_217_800


@pytest.fixture(scope="module")
def flag():
    return solver.config_from(family.solve_flagship())


@pytest.fixture(scope="module")
def report(flag):
    return meta.validate(flag)


def test_the_flagship_resolves_without_allocating_its_weights(report):
    g = report["allocation_guard"]
    assert report["result"] == "PASS"
    assert g["every_leaf_abstract"] is True
    assert g["live_arrays_after"] == g["live_arrays_before"]
    assert g["live_bytes_after"] == g["live_bytes_before"]
    assert g["peak_rss_growth_bytes"] < g["peak_rss_limit_bytes"]
    assert g["would_be_parameter_bytes"] > 1.9e12
    assert report["allocation_mode"] == "meta"


def test_the_flagship_counts_are_exact_and_in_range(report, flag):
    assert report["counts_equal"] is True
    assert report["abstract_trainable"] == FLAGSHIP_TRAINABLE
    assert report["abstract_by_category"] == \
        accounting.count(flag).by_category
    assert report["abstract_dtypes"] == ["bfloat16"]


def test_the_flagship_routing_and_heads_resolve(report, flag):
    r, f = report["routing"], report["forward"]
    assert r["legal"] and r["moe_layers_traced"] == 64 == \
        r["moe_layers_declared"]
    assert r["top_k"] == 12 and r["num_experts"] == 64
    assert r["index_dtype"] == ["int32"]
    assert f["traced"] and f["heads_resolve"]
    assert f["mean"] == [2, len(flag.heads)]


def test_a_real_flagship_allocation_is_refused_before_anything_exists(flag):
    before = len(jax.live_arrays())
    with pytest.raises(meta.AllocationRefused, match="budget"):
        meta.materialize(flag, jax.random.key(0))
    assert len(jax.live_arrays()) <= before + 1   # at most the key itself


def test_large_allocation_needs_both_the_flag_and_the_environment(
        monkeypatch):
    c = family.development()
    with pytest.raises(meta.AllocationRefused):
        meta.materialize(c, jax.random.key(0), max_bytes=1)
    with pytest.raises(meta.AllocationRefused):
        meta.materialize(c, jax.random.key(0), max_bytes=1, allow_large=True)
    monkeypatch.setenv(meta.ALLOW_ENV, "1")
    with pytest.raises(meta.AllocationRefused):
        meta.materialize(c, jax.random.key(0), max_bytes=1)
    p, _ = meta.materialize(c, jax.random.key(0), max_bytes=1,
                            allow_large=True)
    assert p["layers"]


def test_a_member_within_budget_allocates_and_needs_no_refusal():
    rep = meta.validate(family.development())
    assert rep["result"] == "PASS" and rep["real_allocation_refused"] is None


@pytest.mark.parametrize("rung", [name for name, *_ in family.ladder()])
def test_every_rung_of_the_ladder_meta_validates(rung):
    rec = dict(family.solve_ladder())[rung]
    rep = meta.validate(solver.config_from(rec))
    assert rep["result"] == "PASS" and rep["counts_equal"]


def manifest(flag, evidence=(), members=()):
    return documents.model_manifest(flag, solve=family.solve_flagship(),
                                    evidence=evidence,
                                    family_members=members,
                                    source_commit="0" * 40)


def test_the_flagship_manifest_is_well_formed_and_claims_its_evidence(
        flag, report):
    m = manifest(flag, [report])
    assert manifests.problems(m) == []
    assert m["trainable_parameters"] == FLAGSHIP_TRAINABLE
    t = m["claim_status"]["claims"]
    for c in ("ARCHITECTURE_DEFINED", "ARCHITECTURE_PARAMETER_VERIFIED",
              "ARCHITECTURE_META_VALIDATED"):
        assert t[c]["holds"], c
    for c in ("DEVELOPMENT_MODEL_TRAINED", "LARGE_MODEL_TRAINED",
              "SCIENTIFIC_PERFORMANCE_ESTABLISHED",
              "DISTRIBUTED_HARDWARE_VALIDATED"):
        assert not t[c]["holds"], c
    assert m["claim_status"]["status"] == "META_VALIDATED"
    assert "has not been trained" in m["claim_status"]["plain_language"]


def _training(subject: str, state="COMPLETED") -> dict:
    """A structurally valid training manifest bound to ``subject``."""
    doc = {k: None for k in manifests.KEYS[manifests.TRAINING_MANIFEST]}
    doc.update({"schema": manifests.TRAINING_MANIFEST, "run_id": "r",
                "model_configuration_digest": subject,
                "dataset_digest": "1" * 64, "feature_schema_digest": "2" *
                64, "target_schema_digest": "3" * 64,
                "normalization_digest": "4" * 64,
                "resume_semantics": "FRESH_TRAINING",
                "completion_state": state, "parameter_count": 431_784,
                "reproducibility": {"claimed": []}, "checkpoints": [],
                "loss_history": [], "steps": {}})
    assert manifests.problems(doc) == []
    return doc


def test_the_flagship_is_never_trained_without_a_run_of_the_flagship(flag,
                                                                     report):
    dev = family.development().digest()
    dev_run = _training(dev)
    m = manifest(flag, [report, dev_run], members=(dev,))
    t = m["claim_status"]["claims"]
    assert t["DEVELOPMENT_MODEL_TRAINED"]["scope"] == "FAMILY_MEMBER"
    assert t["DEVELOPMENT_MODEL_TRAINED"]["member"] == dev
    assert not t["LARGE_MODEL_TRAINED"]["holds"]
    hist = [h["to"] for h in m["claim_status"]["status_history"]]
    assert "TRAINED" not in hist and "EVALUATED" not in hist
    # without naming the member, the dev run says nothing about the flagship
    m2 = manifest(flag, [report, dev_run])
    assert not m2["claim_status"]["claims"]["DEVELOPMENT_MODEL_TRAINED"][
        "holds"]


def test_an_interrupted_run_is_not_a_trained_model(flag, report):
    c = claims.evaluate(flag.digest(), [report, _training(flag.digest(),
                                                          "INTERRUPTED")])
    assert not c["DEVELOPMENT_MODEL_TRAINED"]["holds"]
    assert not c["LARGE_MODEL_TRAINED"]["holds"]


def test_a_tampered_meta_report_verifies_nothing(flag, report):
    bad = copy.deepcopy(report)
    bad["abstract_by_category"]["expert"] += 1
    m = manifest(flag, [bad])
    t = m["claim_status"]["claims"]
    assert not t["ARCHITECTURE_PARAMETER_VERIFIED"]["holds"]
    bad2 = copy.deepcopy(report)
    bad2["allocation_guard"]["passed"] = False
    t2 = manifest(flag, [bad2])["claim_status"]["claims"]
    assert not t2["ARCHITECTURE_META_VALIDATED"]["holds"]


def test_a_manifest_whose_counts_lie_defines_but_does_not_verify(flag,
                                                                 report):
    m = manifest(flag, [report])
    m["trainable_parameters"] += 1
    m.pop("claim_status")
    m["claim_status"] = {}
    c = claims.evaluate(flag.digest(), [m, report])
    assert c["ARCHITECTURE_DEFINED"]["holds"]
    assert not c["ARCHITECTURE_PARAMETER_VERIFIED"]["holds"]


def test_status_moves_only_along_declared_edges():
    assert claims.allowed(None, "ARCHITECTURE_DEFINED")
    assert claims.allowed("META_VALIDATED", "TRAINED")
    assert not claims.allowed("ARCHITECTURE_DEFINED", "TRAINED")
    assert not claims.allowed("REJECTED", "EVALUATED")
    assert not claims.allowed("INVALIDATED", "ACCEPTED_FOR_LIMITED_USE")
    assert claims.TRANSITIONS["REJECTED"] == ()
    for decided in claims.DECIDED_OUTSIDE:
        assert decided in claims.STATUSES


def test_no_derived_history_takes_an_edge_decided_outside(flag, report):
    m = manifest(flag, [report])
    for h in m["claim_status"]["status_history"]:
        assert h["to"] not in claims.DECIDED_OUTSIDE


def test_the_manifest_identity_excludes_its_own_claims(flag, report):
    m = manifest(flag, [report])
    ident = claims.manifest_identity(m)
    m2 = dict(m)
    m2["claim_status"] = {"anything": True}
    assert claims.manifest_identity(m2) == ident != digest(m)


def test_a_trained_development_model_is_not_a_large_model():
    dev = family.development()
    rep = meta.validate(dev)
    c = claims.evaluate(dev.digest(), [rep, _training(dev.digest())])
    assert c["DEVELOPMENT_MODEL_TRAINED"]["holds"]
    assert c["DEVELOPMENT_MODEL_TRAINED"]["scope"] == "SUBJECT"
    assert not c["LARGE_MODEL_TRAINED"]["holds"]


def test_plain_language_names_the_member_that_was_trained(flag, report):
    dev = family.development().digest()
    m = manifest(flag, [report, _training(dev)], members=(dev,))
    text = m["claim_status"]["plain_language"]
    assert "development-scale member" in text
    assert "this configuration has not been trained" in text


#: What a subject with real training evidence must never be said to be.
UNALLOCATED = ("have not been allocated", "has not been allocated",
               "never been allocated", "not been allocated")


def test_a_trained_subject_is_never_said_to_be_unallocated():
    """The development member was meta-validated AND then allocated and
    trained: both are said, in that order, and nothing says its weights
    were never allocated (NF-1T closure)."""
    dev = family.development()
    rep = meta.validate(dev)
    m = documents.model_manifest(dev, solve=None,
                                 evidence=[rep, _training(dev.digest())],
                                 source_commit="0" * 40)
    text = m["claim_status"]["plain_language"]
    assert "ARCHITECTURE_META_VALIDATED" in [
        c for c, v in m["claim_status"]["claims"].items() if v["holds"]]
    assert "first validated by zero-allocation (abstract) construction" \
        in text
    assert "allocated and trained end to end" in text
    assert not any(u in text for u in UNALLOCATED), text


def test_the_flagship_still_says_its_real_weights_were_never_allocated(
        flag, report):
    dev = family.development().digest()
    m = manifest(flag, [report, _training(dev)], members=(dev,))
    text = m["claim_status"]["plain_language"]
    assert "its real weights have not been allocated" in text
    assert "this configuration has not been trained" in text
    assert "allocated and trained" not in text


def test_the_committed_manifests_say_what_happened_to_each_subject():
    root = Path(__file__).resolve().parents[1] / "docs" / "neural"
    import json
    dev = json.loads((root / "development_model_manifest.json").read_text())
    flag = json.loads((root / "flagship_model_manifest.json").read_text())
    dtext = dev["claim_status"]["plain_language"]
    ftext = flag["claim_status"]["plain_language"]
    assert not any(u in dtext for u in UNALLOCATED), dtext
    assert "allocated and trained end to end" in dtext
    assert "reloaded to identical outputs" in dtext
    assert "its real weights have not been allocated" in ftext
    assert "this configuration has not been trained" in ftext


def test_an_evaluation_that_drops_the_learned_semantics_is_malformed():
    doc = {k: None for k in manifests.KEYS[manifests.EVALUATION_REPORT]}
    doc.update({"schema": manifests.EVALUATION_REPORT,
                "model_config_digest": "1" * 64,
                "checkpoint_digest": "2" * 64,
                "checkpoint_manifest_digest": "3" * 64,
                "dataset_digest": "4" * 64, "normalization_digest": "5" * 64,
                "semantics": list(manifests.PREDICTION_SEMANTICS)})
    assert manifests.problems(doc) == []
    doc["semantics"] = ["LEARNED_PREDICTION", "VERIFIED"]
    assert any("semantics" in p for p in manifests.problems(doc))


def test_network_init_has_its_own_guard_independent_of_materialize(
        monkeypatch):
    """Exercised on the DEVELOPMENT member with the limit lowered to a few
    bytes, so that a broken guard allocates kilobytes, never terabytes."""
    from scientific_ai.neural.model import network
    c = family.development()
    monkeypatch.setattr(network, "INIT_LIMIT_BYTES", 16)
    with pytest.raises(network.RealAllocationRefused, match="approval"):
        network.init(c, jax.random.key(0))
    need = network._real_bytes(c)
    with pytest.raises(network.RealAllocationRefused):
        network.init(c, jax.random.key(0), approved_bytes=need - 1)
    p, _ = network.init(c, jax.random.key(0), approved_bytes=need)
    assert p["layers"]
    # an abstract trace is never refused: nothing is allocated under it
    jax.eval_shape(lambda k: network.init(c, k), jax.random.key(0))
