"""Out-of-distribution detection for the deep layer (fail-closed).

Fits a training-support description (per-dimension box + Mahalanobis distance on
the training context distribution). A query context that falls outside the
training box quantile or beyond a Mahalanobis threshold is flagged OOD, in which
case the deep model must not be used and the engine falls back to direct MC.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class OODModel:
    lo: np.ndarray
    hi: np.ndarray
    mean: np.ndarray
    cov_inv: np.ndarray
    maha_threshold: float

    def score(self, ctx: np.ndarray) -> dict:
        ctx = np.atleast_2d(ctx)
        in_box = np.all((ctx >= self.lo) & (ctx <= self.hi), axis=1)
        delta = ctx - self.mean
        maha = np.sqrt(np.einsum("ni,ij,nj->n", delta, self.cov_inv, delta))
        is_ood = (~in_box) | (maha > self.maha_threshold)
        return {"in_box": in_box, "mahalanobis": maha, "is_ood": is_ood,
                "maha_threshold": self.maha_threshold}


def fit_ood(context_train: np.ndarray, *, quantile: float = 0.99,
            maha_factor: float = 1.5) -> OODModel:
    """Fit the OOD detector on the training context matrix."""
    A = np.atleast_2d(context_train)
    lo = A.min(0); hi = A.max(0)
    mean = A.mean(0)
    cov = np.cov(A, rowvar=False)
    cov = np.atleast_2d(cov) + 1e-6 * np.eye(A.shape[1])
    cov_inv = np.linalg.pinv(cov)
    delta = A - mean
    maha_train = np.sqrt(np.einsum("ni,ij,nj->n", delta, cov_inv, delta))
    thresh = float(np.quantile(maha_train, quantile) * maha_factor)
    return OODModel(lo=lo, hi=hi, mean=mean, cov_inv=cov_inv, maha_threshold=thresh)


def ood_report(ood: OODModel, contexts: dict) -> dict:
    """Score several named context matrices; return a JSON-able report."""
    out = {"maha_threshold": ood.maha_threshold, "checks": {}}
    for name, ctx in contexts.items():
        s = ood.score(ctx)
        out["checks"][name] = {
            "n": int(np.atleast_2d(ctx).shape[0]),
            "frac_in_box": float(np.mean(s["in_box"])),
            "frac_ood": float(np.mean(s["is_ood"])),
            "mahalanobis_mean": float(np.mean(s["mahalanobis"])),
            "mahalanobis_max": float(np.max(s["mahalanobis"])),
        }
    return out
