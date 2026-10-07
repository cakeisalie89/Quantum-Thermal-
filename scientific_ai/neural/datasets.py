"""Governed training data: samples that know where they came from.

A sample is the inputs a governed computation was given, the outputs it
produced, the split it belongs to, and its provenance. Nothing here
fabricates a value: a sample is whatever the source computation returned,
and a provenance field the source cannot supply is
``manifests.unavailable(<why>)``.

TWO DIGESTS, ON PURPOSE

* ``dataset_digest`` binds CONTENT: the two schema digests and, for every
  sample in sample_id order, its id, split, inputs and targets. Regenerated
  on the same backend from the same design, it is the same digest.
* ``provenance_digest`` binds the per-sample provenance, which includes the
  generation time and the environment, and so changes when the same content
  is generated again.

SPLITS ARE A FUNCTION, NOT A DRAW

A sample's split is ``assign_split(family_key, seed)``: the sha256 of the
seed and the sample's PROVENANCE FAMILY (every sample sharing a
configuration digest is one family) mapped onto the declared fractions.
Membership never depends on order, on the number of samples, or on a random
state, so it is reproducible and cannot be silently regenerated: the
manifest records each split's digest, and a split that no longer hashes to
it is a different split. An OUT-OF-DISTRIBUTION split is not drawn at all:
its samples come from a separately declared region of input space
(extrapolation), so it is never "the hard part of a random split".

LEAKAGE (directive s.27): :func:`leakage_report` checks what can be
checked, reports what cannot, and the manifest carries the report.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from scientific.identity import digest

from .features import FeatureSchema, Normalization

SPLITS = ("train", "validation", "test", "ood")
DEFAULT_FRACTIONS = (("train", 0.70), ("validation", 0.15), ("test", 0.15))
VALIDATION_STATUSES = ("INVARIANTS_HOLD", "INVARIANT_FAILED",
                       "NOT_ASSESSED")


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class Sample:
    sample_id: str
    split: str
    inputs: dict
    targets: dict
    provenance: dict

    def content(self) -> dict:
        return {"sample_id": self.sample_id, "split": self.split,
                "inputs": self.inputs, "targets": self.targets}

    def to_record(self) -> dict:
        return {**self.content(), "provenance": self.provenance}


def assign_split(family_key: str, seed: int,
                 fractions=DEFAULT_FRACTIONS) -> str:
    if not isinstance(family_key, str) or not family_key:
        raise DatasetError("a split needs the sample's family key")
    total = sum(f for _, f in fractions)
    if abs(total - 1.0) > 1e-12 or any(f <= 0 for _, f in fractions):
        raise DatasetError(f"split fractions must be positive and sum to "
                           f"1: {fractions}")
    h = hashlib.sha256(f"{seed}:{family_key}".encode()).digest()
    u = int.from_bytes(h[:8], "big") / 2 ** 64
    acc = 0.0
    for name, f in fractions:
        acc += f
        if u < acc:
            return name
    return fractions[-1][0]


def family_key(sample: Sample) -> str:
    return sample.provenance.get("scientific_configuration_digest") \
        or sample.provenance["input_digest"]


def dataset_digest(schema: FeatureSchema, samples) -> str:
    ordered = sorted(samples, key=lambda s: s.sample_id)
    return digest({"feature_schema_digest": schema.feature_digest(),
                   "target_schema_digest": schema.target_digest(),
                   "samples": [s.content() for s in ordered]})


def provenance_digest(samples) -> str:
    return digest([[s.sample_id, s.provenance]
                   for s in sorted(samples, key=lambda s: s.sample_id)])


def split_members(samples) -> dict:
    out = {name: [] for name in SPLITS}
    for s in samples:
        if s.split not in out:
            raise DatasetError(f"{s.sample_id}: unknown split {s.split!r}")
        out[s.split].append(s)
    return out


def split_digests(samples) -> dict:
    return {name: digest(sorted(s.sample_id for s in members))
            for name, members in split_members(samples).items()}


def check_samples(schema: FeatureSchema, samples) -> None:
    """Every sample is a record of ``schema``; ids are unique."""
    ids = set()
    for s in samples:
        if s.sample_id in ids:
            raise DatasetError(f"sample id {s.sample_id!r} twice")
        ids.add(s.sample_id)
        if set(s.inputs) != set(schema.feature_names):
            raise DatasetError(f"{s.sample_id}: inputs are not the "
                               "schema's features")
        if set(s.targets) != set(schema.target_names):
            raise DatasetError(f"{s.sample_id}: targets are not the "
                               "schema's targets")
        p = s.provenance
        if p.get("input_digest") != digest(s.inputs):
            raise DatasetError(f"{s.sample_id}: input_digest does not "
                               "match its inputs")
        if p.get("target_digest") != digest(s.targets):
            raise DatasetError(f"{s.sample_id}: target_digest does not "
                               "match its targets")
        if p.get("validation_status") not in VALIDATION_STATUSES:
            raise DatasetError(f"{s.sample_id}: validation_status")


def fit_normalization(schema: FeatureSchema, samples) -> Normalization:
    train = split_members(samples)["train"]
    if not train:
        raise DatasetError("no training split to fit statistics on")
    return Normalization.fit(schema, [s.inputs for s in train],
                             [s.targets for s in train],
                             split_digest=split_digests(samples)["train"])


def _check(name, passed, detail) -> dict:
    return {"check": name, "passed": bool(passed), "detail": detail}


def leakage_report(schema: FeatureSchema, samples,
                   norm: Normalization, *, ood_rule=None) -> dict:
    """Every leakage check (directive s.27) and its result. ``ood_rule``:
    a function inputs -> bool that is True exactly for the OOD region."""
    by = split_members(samples)
    checks = []

    def crossing(key_fn, name, what):
        seen: dict = {}
        bad = []
        for split, members in by.items():
            for s in members:
                k = key_fn(s)
                if k in seen and seen[k][0] != split:
                    bad.append([seen[k][1], s.sample_id])
                seen.setdefault(k, (split, s.sample_id))
        checks.append(_check(name, not bad, {
            "what": what, "crossings": len(bad), "examples": bad[:5]}))

    crossing(lambda s: s.provenance["input_digest"],
             "exact_duplicate_inputs_across_splits",
             "the same input vector in two splits")
    crossing(family_key, "provenance_family_across_splits",
             "two samples of one scientific configuration in two splits")
    crossing(lambda s: s.provenance["target_digest"],
             "duplicate_targets_across_splits",
             "identical outputs under different sample ids in two splits")
    train_digest = split_digests(samples)["train"]
    checks.append(_check(
        "normalization_from_training_split_only",
        norm.fitted_on == train_digest and norm.rows == len(by["train"]),
        {"fitted_on": norm.fitted_on, "train_split_digest": train_digest,
         "rows": norm.rows, "train_rows": len(by["train"])}))
    copies = []
    for f in schema.feature_names:
        for t in schema.target_names:
            if samples and all(s.inputs[f] == s.targets[t]
                               for s in samples):
                copies.append([f, t])
    checks.append(_check(
        "target_leakage_through_features",
        not copies and not set(schema.feature_names)
        & set(schema.target_names),
        {"feature_equal_to_target": copies}))
    ids = [s.sample_id for s in samples]
    checks.append(_check(
        "seed_correlated_design_points",
        len(ids) == len(set(ids)) and len(
            {s.provenance["input_digest"] for s in samples}) == len(ids),
        {"what": "every design point is drawn once and every sample has "
                 "its own inputs; a split is a hash of the configuration, "
                 "independent of the design stream's order"}))
    if ood_rule is not None:
        wrong = [s.sample_id for s in samples
                 if bool(ood_rule(s.inputs)) != (s.split == "ood")]
        checks.append(_check("ood_region_disjoint", not wrong, {
            "misplaced": len(wrong), "examples": wrong[:5]}))
    checks.append({"check": "future_information", "passed": True,
                   "detail": {"status": "NOT_APPLICABLE",
                              "why": "samples are independent "
                                     "steady computations, not a time "
                                     "series; no sample is computed from "
                                     "another"}})
    return {
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "remaining_risks": [
            "the source is a smooth closed-form law: a test point can sit "
            "close to a training point in input space, so in-distribution "
            "test error measures interpolation, not generalisation -- the "
            "OOD split is the extrapolation test",
            "one computational source and one implementation: errors "
            "shared by every sample (a wrong constant in the law) are "
            "invisible to any split",
            "duplicates are detected exactly, not approximately; two "
            "configurations equal to 1e-15 are different samples"],
    }


def to_jsonl(samples) -> bytes:
    """Canonical storage: one sorted-key JSON object per line, sample_id
    order, floats as Python repr (round-trip exact)."""
    lines = [json.dumps(s.to_record(), sort_keys=True,
                        separators=(",", ":"), allow_nan=False)
             for s in sorted(samples, key=lambda s: s.sample_id)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def from_jsonl(raw: bytes) -> list:
    out = []

    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise DatasetError("duplicate key in a sample record")
        return dict(pairs)

    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        rec = json.loads(line, object_pairs_hook=no_dupes)
        if set(rec) != {"sample_id", "split", "inputs", "targets",
                        "provenance"}:
            raise DatasetError(f"line {n}: not a sample record")
        out.append(Sample(rec["sample_id"], rec["split"], rec["inputs"],
                          rec["targets"], rec["provenance"]))
    return out
