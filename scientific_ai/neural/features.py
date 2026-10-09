"""Feature and target schemas, training-only statistics, and the tokenizer.

A scientific record is not flattened into an anonymous vector. Each feature
keeps its identity, its unit and dimension, its role, whether it is present,
and its value in three complementary forms (``tokens``). The schema says
which features exist; a record that disagrees with it is refused.

WHAT THE TOKENIZER REFUSES, AND WHAT IT DOES INSTEAD

* a feature the schema does not declare, or a declared one left out of the
  record -- :class:`SchemaMismatch`. Absence is stated, not inferred: a
  missing value is ``None`` and the token says MISSING (only where the
  feature allows it);
* NaN -- :class:`MalformedInput`. NaN is not a missing marker; a record that
  means "absent" says ``None``;
* +-inf, a bool, a string, a non-integer for an integer feature --
  :class:`MalformedInput`;
* a value outside the feature's scientifically valid DOMAIN, or a
  non-positive value under a log transform -- :class:`ScientificallyInvalid`.
  The domain is physics (a pressure is not negative), not the training
  range: leaving the training range is extrapolation, which the OOD
  assessment reports rather than the tokenizer refusing;
* a value given with a unit of a different DIMENSION than the feature's --
  :class:`SchemaMismatch`. The same dimension in another unit (mK for K) is
  converted.

ORDER

Features are kept in canonical order (sorted by name) and every record is
read by name, so the token sequence does not depend on the order a record
lists its fields: the input is a SET of named quantities. The network adds
no positional encoding, so it cannot learn an order that is not there.

STATISTICS COME FROM THE TRAINING SPLIT

:meth:`Normalization.fit` takes the training rows and nothing else and
records the digest of the split it was fitted on; the dataset leakage check
refuses statistics whose digest is not the training split's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from scientific.identity import digest

from . import tokens, units

SCHEMA_VERSION = "scientific-feature-schema/1"
TRANSFORMS = ("linear", "log10_positive")
ROLES = ("INPUT", "CONDITION")


class SchemaError(ValueError):
    pass


class SchemaMismatch(SchemaError):
    """The record is not a record of this schema."""


class MalformedInput(SchemaError):
    """The value is not a number this pipeline accepts."""


class ScientificallyInvalid(SchemaError):
    """A number, but not a physically valid value of this feature."""


def _check_interval(name, lo, hi):
    for v in (lo, hi):
        if v is not None and (isinstance(v, bool) or not isinstance(
                v, (int, float)) or not math.isfinite(v)):
            raise SchemaError(f"{name}: bound {v!r} is not a finite number")
    if lo is not None and hi is not None and not lo < hi:
        raise SchemaError(f"{name}: lower {lo} must be < upper {hi}")


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    unit: str
    transform: str = "linear"
    role: str = "INPUT"
    lower: float | None = None
    upper: float | None = None
    missing_allowed: bool = False
    integer: bool = False
    description: str = ""

    def validate(self) -> "FeatureSpec":
        if not isinstance(self.name, str) or not self.name.strip():
            raise SchemaError("a feature needs a name")
        try:
            self.dimension
        except units.UnitError as exc:
            raise SchemaError(f"{self.name}: {exc}") from exc
        if self.transform not in TRANSFORMS:
            raise SchemaError(f"{self.name}: transform {self.transform!r}")
        if self.role not in ROLES:
            raise SchemaError(f"{self.name}: role {self.role!r}")
        _check_interval(self.name, self.lower, self.upper)
        if self.transform == "log10_positive" and (
                self.lower is None or self.lower <= 0):
            raise SchemaError(f"{self.name}: a log transform needs a "
                              "positive lower domain bound")
        if self.integer and self.dimension.dimension_class not in (
                units.COUNT, units.PHYSICAL) or (
                self.integer and not self.dimension.is_dimensionless):
            raise SchemaError(f"{self.name}: an integer feature is a COUNT "
                              "or dimensionless")
        return self

    @property
    def dimension(self) -> units.Dimension:
        return units.parse(self.unit)

    def to_dict(self) -> dict:
        return {"name": self.name, "unit": self.unit,
                "dimension": self.dimension.to_dict(),
                "transform": self.transform, "role": self.role,
                "lower": self.lower, "upper": self.upper,
                "missing_allowed": self.missing_allowed,
                "integer": self.integer, "description": self.description}


@dataclass(frozen=True)
class TargetSpec:
    name: str
    unit: str
    transform: str = "linear"
    lower: float | None = None
    upper: float | None = None
    description: str = ""

    def validate(self) -> "TargetSpec":
        if not isinstance(self.name, str) or not self.name.strip():
            raise SchemaError("a target needs a name")
        try:
            units.parse(self.unit)
        except units.UnitError as exc:
            raise SchemaError(f"{self.name}: {exc}") from exc
        if self.transform not in TRANSFORMS:
            raise SchemaError(f"{self.name}: transform {self.transform!r}")
        _check_interval(self.name, self.lower, self.upper)
        if self.transform == "log10_positive" and self.lower is not None \
                and self.lower <= 0:
            raise SchemaError(f"{self.name}: a log target is positive")
        return self

    def to_dict(self) -> dict:
        return {"name": self.name, "unit": self.unit,
                "dimension": units.parse(self.unit).to_dict(),
                "transform": self.transform, "lower": self.lower,
                "upper": self.upper, "description": self.description}


@dataclass(frozen=True)
class FeatureSchema:
    name: str
    features: tuple
    targets: tuple

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise SchemaError("a schema needs a name")
        if not self.features or not self.targets:
            raise SchemaError("a schema needs features and targets")
        for f in self.features:
            if not isinstance(f, FeatureSpec):
                raise SchemaError(f"{f!r} is not a FeatureSpec")
            f.validate()
        for t in self.targets:
            if not isinstance(t, TargetSpec):
                raise SchemaError(f"{t!r} is not a TargetSpec")
            t.validate()
        fn = [f.name for f in self.features]
        tn = [t.name for t in self.targets]
        if fn != sorted(fn) or len(set(fn)) != len(fn):
            raise SchemaError("features must be unique and in canonical "
                              "(sorted) order")
        if tn != sorted(tn) or len(set(tn)) != len(tn):
            raise SchemaError("targets must be unique and sorted")
        if set(fn) & set(tn):
            raise SchemaError(f"{sorted(set(fn) & set(tn))} are both a "
                              "feature and a target: target leakage by "
                              "construction")

    @property
    def feature_names(self) -> tuple:
        return tuple(f.name for f in self.features)

    @property
    def target_names(self) -> tuple:
        return tuple(t.name for t in self.targets)

    def feature_schema_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "name": self.name,
                "features": [f.to_dict() for f in self.features],
                "token_layout": {"value_channels": list(
                    tokens.VALUE_CHANNELS), "log_mag_scale":
                    tokens.LOG_MAG_SCALE, "validity": list(tokens.VALIDITY),
                    "contexts": list(tokens.CONTEXTS),
                    "dimension_classes": list(units.DIMENSION_CLASSES),
                    "base_dimensions": list(units.BASE)}}

    def target_schema_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "name": self.name,
                "targets": [t.to_dict() for t in self.targets]}

    def feature_digest(self) -> str:
        return digest(self.feature_schema_dict())

    def target_digest(self) -> str:
        return digest(self.target_schema_dict())


def _transform(kind: str, v: float) -> float:
    if kind == "linear":
        return v
    return math.log10(v)


def inverse_transform(kind: str, t):
    if kind == "linear":
        return t
    return np.power(10.0, t)


def _number(name: str, v, *, integer: bool) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, np.floating,
                                                 np.integer)):
        raise MalformedInput(f"{name}: {v!r} is not a number")
    x = float(v)
    if math.isnan(x):
        raise MalformedInput(f"{name}: NaN is not a missing marker; a "
                             "missing value is None")
    if math.isinf(x):
        raise MalformedInput(f"{name}: {x} is not a finite value")
    if integer and x != int(x):
        raise MalformedInput(f"{name}: {v!r} is not an integer")
    return x


def _in_unit(spec: FeatureSpec, raw) -> float:
    """The value in the FEATURE's stated unit."""
    if isinstance(raw, dict):
        if set(raw) != {"value", "unit"}:
            raise MalformedInput(f"{spec.name}: a unit-bearing value is "
                                 "{'value', 'unit'}")
        try:
            given = units.parse(raw["unit"])
        except units.UnitError as exc:
            raise SchemaMismatch(f"{spec.name}: {exc}") from exc
        want = spec.dimension
        if (given.dimension_class, given.exponents) != (
                want.dimension_class, want.exponents):
            raise SchemaMismatch(
                f"{spec.name}: {raw['unit']!r} has dimension "
                f"{given.exponents}, the feature {spec.unit!r} has "
                f"{want.exponents}")
        x = _number(spec.name, raw["value"], integer=False)
        x = x * given.si_factor / want.si_factor
        if spec.integer and x != int(x):
            raise MalformedInput(f"{spec.name}: {x!r} is not an integer")
        return x
    return _number(spec.name, raw, integer=spec.integer)


def checked_value(spec: FeatureSpec, raw):
    """The validated value in the feature's unit, or None when MISSING."""
    if raw is None:
        if not spec.missing_allowed:
            raise SchemaMismatch(f"{spec.name}: a value is required")
        return None
    x = _in_unit(spec, raw)
    if spec.lower is not None and x < spec.lower or \
            spec.upper is not None and x > spec.upper:
        raise ScientificallyInvalid(
            f"{spec.name}: {x!r} lies outside its valid domain "
            f"[{spec.lower}, {spec.upper}] {spec.unit}")
    if spec.transform == "log10_positive" and not x > 0:
        raise ScientificallyInvalid(f"{spec.name}: {x!r} is not positive")
    return x


def checked_record(schema: FeatureSchema, record: dict) -> dict:
    if not isinstance(record, dict):
        raise SchemaMismatch("a record is a mapping of feature name to value")
    keys = set(record)
    want = set(schema.feature_names)
    if keys != want:
        raise SchemaMismatch(f"record fields differ from the schema: "
                             f"unknown {sorted(keys - want)}, absent "
                             f"{sorted(want - keys)}")
    return {f.name: checked_value(f, record[f.name])
            for f in schema.features}


@dataclass(frozen=True)
class Normalization:
    """Per-feature and per-target location and scale in transform space,
    fitted on the training split ONLY."""

    feature_mean: tuple
    feature_scale: tuple
    feature_min: tuple
    feature_max: tuple
    target_mean: tuple
    target_scale: tuple
    fitted_on: str            # the training split's digest
    rows: int

    @classmethod
    def fit(cls, schema: FeatureSchema, train_inputs: list,
            train_targets: list, *, split_digest: str) -> "Normalization":
        if not train_inputs or len(train_inputs) != len(train_targets):
            raise SchemaError("fit needs the training rows, inputs and "
                              "targets alike")
        fm, fs, lo, hi = [], [], [], []
        for f in schema.features:
            vals = [_transform(f.transform, r[f.name] * f.dimension
                               .si_factor)
                    for r in train_inputs if r[f.name] is not None]
            if not vals:
                raise SchemaError(f"{f.name}: no training value to fit on")
            m, s = _mean_scale(vals)
            fm.append(m)
            fs.append(s)
            raw = [r[f.name] for r in train_inputs if r[f.name] is not None]
            lo.append(min(raw))
            hi.append(max(raw))
        tm, ts = [], []
        for t in schema.targets:
            vals = [_transform(t.transform, r[t.name])
                    for r in train_targets]
            m, s = _mean_scale(vals)
            tm.append(m)
            ts.append(s)
        return cls(tuple(fm), tuple(fs), tuple(lo), tuple(hi), tuple(tm),
                   tuple(ts), split_digest, len(train_inputs))

    def to_dict(self) -> dict:
        return {"method": "mean and population standard deviation in "
                          "transform space (math.fsum); a constant "
                          "feature keeps scale 1.0",
                "feature_mean": list(self.feature_mean),
                "feature_scale": list(self.feature_scale),
                "feature_training_min": list(self.feature_min),
                "feature_training_max": list(self.feature_max),
                "target_mean": list(self.target_mean),
                "target_scale": list(self.target_scale),
                "fitted_on_split_digest": self.fitted_on,
                "rows": self.rows}

    @classmethod
    def from_dict(cls, d: dict) -> "Normalization":
        return cls(tuple(d["feature_mean"]), tuple(d["feature_scale"]),
                   tuple(d["feature_training_min"]),
                   tuple(d["feature_training_max"]),
                   tuple(d["target_mean"]), tuple(d["target_scale"]),
                   d["fitted_on_split_digest"], d["rows"])

    def digest(self) -> str:
        return digest(self.to_dict())


def _mean_scale(vals) -> tuple:
    n = len(vals)
    m = math.fsum(vals) / n
    var = math.fsum((v - m) ** 2 for v in vals) / n
    s = math.sqrt(var)
    return m, (s if s > 0 else 1.0)


def tokenize(schema: FeatureSchema, norm: Normalization,
             records: list) -> dict:
    """Token arrays for ``records`` (each checked). Shapes: B records,
    F features in canonical order."""
    b, nf = len(records), len(schema.features)
    if b == 0:
        raise SchemaError("nothing to tokenize")
    feat = np.tile(np.arange(nf, dtype=np.int32), (b, 1))
    ctx = np.empty((b, nf), dtype=np.int32)
    valid = np.zeros((b, nf), dtype=np.int32)
    dims = np.empty((b, nf, tokens.N_BASE_DIMS), dtype=np.float32)
    dcls = np.empty((b, nf), dtype=np.int32)
    vals = np.zeros((b, nf, len(tokens.VALUE_CHANNELS)), dtype=np.float32)
    missing = tokens.VALIDITY.index("MISSING")
    for j, f in enumerate(schema.features):
        ctx[:, j] = tokens.CONTEXTS.index(f.role)
        dims[:, j, :] = f.dimension.exponents
        dcls[:, j] = units.DIMENSION_CLASSES.index(
            f.dimension.dimension_class)
    for i, rec in enumerate(records):
        checked = checked_record(schema, rec)
        for j, f in enumerate(schema.features):
            x = checked[f.name]
            if x is None:
                valid[i, j] = missing
                continue
            si = x * f.dimension.si_factor
            z = (_transform(f.transform, si) - norm.feature_mean[j]) \
                / norm.feature_scale[j]
            sign = 0.0 if si == 0 else math.copysign(1.0, si)
            log_mag = 0.0 if si == 0 else math.log10(abs(si)) \
                / tokens.LOG_MAG_SCALE
            vals[i, j] = (z, sign, log_mag, 1.0 if si == 0 else 0.0)
    return {"feature_ids": feat, "context_ids": ctx, "validity": valid,
            "dim_exponents": dims, "dim_class": dcls, "values": vals}


def encode_targets(schema: FeatureSchema, norm: Normalization,
                   rows: list) -> np.ndarray:
    out = np.empty((len(rows), len(schema.targets)), dtype=np.float64)
    for i, r in enumerate(rows):
        for j, t in enumerate(schema.targets):
            v = _number(t.name, r[t.name], integer=False)
            if t.transform == "log10_positive" and not v > 0:
                raise ScientificallyInvalid(f"{t.name}: {v!r} is not "
                                            "positive")
            out[i, j] = (_transform(t.transform, v) - norm.target_mean[j]) \
                / norm.target_scale[j]
    return out


def decode_targets(schema: FeatureSchema, norm: Normalization,
                   z: np.ndarray) -> np.ndarray:
    """Standardised transform space back to each target's unit."""
    z = np.asarray(z, dtype=np.float64)
    out = np.empty_like(z)
    for j, t in enumerate(schema.targets):
        tval = z[..., j] * norm.target_scale[j] + norm.target_mean[j]
        out[..., j] = inverse_transform(t.transform, tval)
    return out
