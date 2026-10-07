"""Is this input one the model has any business answering? (directive s.23)

Six answers, decided in this order, for one record:

``UNSUPPORTED_SCHEMA``     the record was built for another feature schema
                           (digest differs), or names other features;
``MALFORMED_INPUT``        a value is not an acceptable number (NaN, inf,
                           a string, a non-integer count);
``SCIENTIFICALLY_INVALID`` a number outside the feature's physical domain;
``EXTRAPOLATION``          inside the domain, outside the TRAINING range of
                           at least one feature (named);
``DISTRIBUTION_SHIFT``     inside every training range, but the joint
                           standardised vector is farther (Mahalanobis)
                           from the training cloud than the
                           ``quantile`` of the training points themselves;
``IN_DISTRIBUTION``        none of the above.

A random train/test split cannot show extrapolation, which is why the
dataset carries a separately generated OOD region and the evaluation
reports both. This module only classifies; the evaluation reports how
prediction error degrades across the classes.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .features import (FeatureSchema, MalformedInput, Normalization,
                       SchemaMismatch, ScientificallyInvalid, checked_record,
                       tokenize)

CLASSES = ("UNSUPPORTED_SCHEMA", "MALFORMED_INPUT", "SCIENTIFICALLY_INVALID",
           "EXTRAPOLATION", "DISTRIBUTION_SHIFT", "IN_DISTRIBUTION")


@dataclass(frozen=True)
class InputOOD:
    schema_digest: str
    mean: tuple
    precision: tuple          # inverse covariance, row-major
    threshold: float
    quantile: float
    ridge: float

    @classmethod
    def fit(cls, schema: FeatureSchema, norm: Normalization,
            train_inputs: list, *, quantile: float = 0.99,
            ridge: float = 1e-6) -> "InputOOD":
        z = _z(schema, norm, train_inputs)
        mu = z.mean(axis=0)
        cov = np.cov(z, rowvar=False) + ridge * np.eye(z.shape[1])
        prec = np.linalg.inv(cov)
        d2 = _d2(z, mu, prec)
        thr = float(np.quantile(d2, quantile))
        return cls(schema.feature_digest(), tuple(mu.tolist()),
                   tuple(prec.reshape(-1).tolist()), thr, quantile, ridge)

    def to_dict(self) -> dict:
        return {"method": "Mahalanobis distance of the standardised input "
                          "vector to the training split, threshold at "
                          "its training quantile",
                "schema_digest": self.schema_digest,
                "mean": list(self.mean), "precision": list(self.precision),
                "threshold": self.threshold, "quantile": self.quantile,
                "ridge": self.ridge}

    def assess(self, schema: FeatureSchema, norm: Normalization, record,
               *, record_schema_digest: str | None = None) -> dict:
        if record_schema_digest is not None and \
                record_schema_digest != self.schema_digest:
            return {"class": "UNSUPPORTED_SCHEMA",
                    "why": "the record names another feature schema"}
        try:
            checked = checked_record(schema, record)
        except SchemaMismatch as exc:
            return {"class": "UNSUPPORTED_SCHEMA", "why": str(exc)}
        except MalformedInput as exc:
            return {"class": "MALFORMED_INPUT", "why": str(exc)}
        except ScientificallyInvalid as exc:
            return {"class": "SCIENTIFICALLY_INVALID", "why": str(exc)}
        outside = [f.name for j, f in enumerate(schema.features)
                   if checked[f.name] is not None and not (
                       norm.feature_min[j] <= checked[f.name]
                       <= norm.feature_max[j])]
        if outside:
            return {"class": "EXTRAPOLATION", "features": outside}
        z = _z(schema, norm, [record])
        n = len(self.mean)
        d2 = float(_d2(z, np.asarray(self.mean),
                       np.asarray(self.precision).reshape(n, n))[0])
        if d2 > self.threshold:
            return {"class": "DISTRIBUTION_SHIFT", "mahalanobis_sq": d2,
                    "threshold": self.threshold}
        return {"class": "IN_DISTRIBUTION", "mahalanobis_sq": d2}


def _z(schema, norm, records) -> np.ndarray:
    return tokenize(schema, norm, records)["values"][:, :, 0].astype(
        np.float64)


def _d2(z, mu, prec) -> np.ndarray:
    c = z - mu
    return np.einsum("ni,ij,nj->n", c, prec, c)
