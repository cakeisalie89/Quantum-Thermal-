"""NF-1T s.25-27, s.59: governed samples, splits, digests and leakage.

Most tests use SYNTHETIC samples built here (labelled as such, never
evidence); one generates a handful of real samples through the governed
source model to check provenance and regeneration.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scientific.identity import digest  # noqa: E402
from scientific_ai.neural import datasets as D  # noqa: E402
from scientific_ai.neural import features as F  # noqa: E402
from scientific_ai.neural import source_surface_adsorption as SRC  # noqa

SCHEMA = F.FeatureSchema(
    "synthetic", (F.FeatureSpec("a", "1", lower=-10, upper=10),
                  F.FeatureSpec("b", "K", lower=0.0, upper=1e3)),
    (F.TargetSpec("y", "1"),))


def sample(i: int, split=None, *, seed=3, a=None, y=None) -> D.Sample:
    inputs = {"a": float(i % 7) if a is None else a, "b": 10.0 + i}
    targets = {"y": 0.5 * i if y is None else y}
    prov = {"input_digest": digest(inputs), "target_digest": digest(targets),
            "scientific_configuration_digest": digest(inputs),
            "validation_status": "NOT_ASSESSED",
            "note": "SYNTHETIC test fixture, not evidence"}
    sp = split or D.assign_split(prov["scientific_configuration_digest"],
                                 seed)
    return D.Sample(f"s/{i:04d}", sp, inputs, targets, prov)


def corpus(n=200):
    return [sample(i) for i in range(n)]


def test_split_assignment_is_a_reproducible_function():
    a = corpus()
    b = corpus()
    assert [s.split for s in a] == [s.split for s in b]
    assert D.split_digests(a) == D.split_digests(b)
    # independent of order and of how many samples there are
    assert {s.sample_id: s.split for s in reversed(a)} == \
        {s.sample_id: s.split for s in a[:50]} | {
            s.sample_id: s.split for s in a[50:]}


@settings(max_examples=200, deadline=None, derandomize=True)
@given(st.text(min_size=1, max_size=20), st.integers(0, 2 ** 31))
def test_a_split_is_one_of_the_declared_ones(key, seed):
    assert D.assign_split(key, seed) in ("train", "validation", "test")


def test_split_fractions_are_respected_roughly_and_validated():
    s = [D.assign_split(f"k{i}", 1) for i in range(4000)]
    assert 0.66 < s.count("train") / 4000 < 0.74
    with pytest.raises(D.DatasetError):
        D.assign_split("k", 1, (("train", 0.5), ("test", 0.4)))


def test_the_dataset_digest_is_stable_and_binds_content_only():
    a = corpus()
    assert D.dataset_digest(SCHEMA, a) == D.dataset_digest(SCHEMA,
                                                           list(reversed(a)))
    b = list(a)
    b[3] = dataclasses.replace(b[3], provenance={**b[3].provenance,
                                                 "note": "another time"})
    assert D.dataset_digest(SCHEMA, a) == D.dataset_digest(SCHEMA, b)
    assert D.provenance_digest(a) != D.provenance_digest(b)
    c = list(a)
    c[3] = sample(3, a=6.5)
    assert D.dataset_digest(SCHEMA, a) != D.dataset_digest(SCHEMA, c)


def test_duplicates_and_overlap_across_splits_are_detected():
    base = corpus(40)
    dup = sample(1, split="test" if base[1].split != "test" else "train")
    dup = dataclasses.replace(dup, sample_id="s/dup")
    samples = base + [dup]
    n = D.fit_normalization(SCHEMA, samples)
    rep = D.leakage_report(SCHEMA, samples, n)
    failed = {c["check"] for c in rep["checks"] if not c["passed"]}
    assert {"exact_duplicate_inputs_across_splits",
            "provenance_family_across_splits",
            "duplicate_targets_across_splits"} <= failed
    assert not rep["passed"]


def test_a_clean_corpus_passes_every_leakage_check():
    samples = corpus()
    n = D.fit_normalization(SCHEMA, samples)
    rep = D.leakage_report(SCHEMA, samples, n)
    assert rep["passed"], [c for c in rep["checks"] if not c["passed"]]
    assert rep["remaining_risks"]


def test_statistics_from_anything_but_the_training_split_are_leakage():
    samples = corpus()
    test_rows = [s for s in samples if s.split == "test"]
    leaked = F.Normalization.fit(SCHEMA, [s.inputs for s in test_rows],
                                 [s.targets for s in test_rows],
                                 split_digest=D.split_digests(samples)[
                                     "test"])
    rep = D.leakage_report(SCHEMA, samples, leaked)
    bad = [c for c in rep["checks"]
           if c["check"] == "normalization_from_training_split_only"]
    assert bad and not bad[0]["passed"]
    good = D.fit_normalization(SCHEMA, samples)
    assert good.fitted_on == D.split_digests(samples)["train"]


def test_a_feature_that_copies_a_target_is_leakage():
    samples = [sample(i, y=float(i % 7)) for i in range(60)]
    n = D.fit_normalization(SCHEMA, samples)
    rep = D.leakage_report(SCHEMA, samples, n)
    bad = [c for c in rep["checks"]
           if c["check"] == "target_leakage_through_features"][0]
    assert not bad["passed"] and ["a", "y"] in bad["detail"][
        "feature_equal_to_target"]


def test_samples_must_match_their_digests_and_schema():
    s = sample(1)
    D.check_samples(SCHEMA, [s])
    with pytest.raises(D.DatasetError, match="input_digest"):
        D.check_samples(SCHEMA, [dataclasses.replace(s, inputs={"a": 9.0,
                                                                "b": 1.0})])
    with pytest.raises(D.DatasetError, match="twice"):
        D.check_samples(SCHEMA, [s, s])
    with pytest.raises(D.DatasetError, match="features"):
        D.check_samples(SCHEMA, [dataclasses.replace(s, inputs={"a": 1.0})])


def test_storage_round_trips_exactly_and_refuses_malformed_lines():
    a = corpus(30)
    raw = D.to_jsonl(a)
    back = D.from_jsonl(raw)
    assert D.to_jsonl(back) == raw
    assert D.dataset_digest(SCHEMA, back) == D.dataset_digest(SCHEMA, a)
    with pytest.raises(D.DatasetError):
        D.from_jsonl(b'{"sample_id": "x"}\n')
    with pytest.raises(D.DatasetError):
        D.from_jsonl(b'{"sample_id":"x","sample_id":"y","split":"t",'
                     b'"inputs":{},"targets":{},"provenance":{}}\n')


def test_the_ood_region_rule_is_disjoint_from_in_distribution():
    pts = SRC.design("in_distribution", 64, 1)
    ood = SRC.design("ood", 64, 2)
    assert not any(SRC.in_ood_region(p) for p in pts)
    assert all(SRC.in_ood_region(p) for p in ood)


def test_the_design_is_a_reproducible_function_of_its_seed():
    assert SRC.design("ood", 16, 5) == SRC.design("ood", 16, 5)
    assert SRC.design("ood", 16, 5) != SRC.design("ood", 16, 6)


def test_governed_samples_carry_complete_provenance_and_regenerate():
    kw = dict(dataset_id="probe", n_in=5, n_ood=2, design_seed=11,
              split_seed=3, source_commit={"unavailable": "test"},
              generated_at="2026-01-01T00:00:00+00:00", workers=1)
    a, ra = SRC.generate(**kw)
    b, rb = SRC.generate(**kw)
    assert not ra and not rb and len(a) == 7
    assert D.dataset_digest(SRC.SCHEMA, a) == D.dataset_digest(SRC.SCHEMA, b)
    D.check_samples(SRC.SCHEMA, a)
    need = {"input_digest", "target_digest", "source_dataset",
            "source_artifact", "source_commit", "generator_version",
            "scientific_configuration_digest", "backend_identity",
            "environment_identity", "schema_version",
            "generation_timestamp", "validation_status"}
    for s in a:
        assert need <= set(s.provenance)
        assert s.provenance["validation_status"] == "INVARIANTS_HOLD"
        assert s.provenance["source_artifact"]["stored"] is False
        assert s.provenance["source_commit"] == {"unavailable": "test"}
        assert (s.split == "ood") == SRC.in_ood_region(s.inputs)
