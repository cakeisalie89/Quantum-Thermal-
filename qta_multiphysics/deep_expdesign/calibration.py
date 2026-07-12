"""Simulation-based calibration (coverage) for the conditional posterior.

For held-out (theta*, x, design) drawn from the same generative model, draw
posterior samples and check whether nominal central credible regions contain the
true parameter at approximately the nominal frequency. Reports empirical coverage,
coverage error, and per-parameter coverage. A posterior that is accurate on
average but badly miscalibrated will show large coverage error and must not be
trusted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import likelihood_model as _lm


@dataclass
class CalibrationResult:
    levels: list
    empirical_coverage: dict          # level -> overall empirical coverage
    coverage_error: dict              # level -> |empirical - nominal|
    per_param_coverage: dict          # level -> per-parameter coverage list
    max_coverage_error: float
    n_eval: int

    def to_dict(self) -> dict:
        return {"levels": self.levels,
                "empirical_coverage": {str(k): v for k, v in self.empirical_coverage.items()},
                "coverage_error": {str(k): v for k, v in self.coverage_error.items()},
                "per_param_coverage": {str(k): v for k, v in self.per_param_coverage.items()},
                "max_coverage_error": self.max_coverage_error, "n_eval": self.n_eval}


def run_sbc(trained, *, levels=(0.5, 0.8, 0.9, 0.95), n_post: int = 800,
            seed: int = 0) -> CalibrationResult:
    """Coverage-based SBC on the held-out test split."""
    ds = trained.dataset
    idx = ds.split["test"][ds.success[ds.split["test"]]]
    if len(idx) == 0:
        idx = ds.split["val"][ds.success[ds.split["val"]]]
    theta_true_t = ds.theta_t[idx]                    # transformed space
    ctx = np.hstack([trained.std_x.transform(ds.x[idx]), ds.design_norm[idx]])
    # posterior samples in standardized theta space -> transformed space
    samp_std = trained.model.sample(ctx, n=n_post, seed=seed)     # (N,n,d)
    samp_t = samp_std * trained.std_theta.std + trained.std_theta.mean
    N, _, d = samp_t.shape

    emp = {}; cov_err = {}; per = {}
    for lv in levels:
        lo_q = (1 - lv) / 2 * 100; hi_q = (1 + lv) / 2 * 100
        lo = np.percentile(samp_t, lo_q, axis=1)       # (N,d)
        hi = np.percentile(samp_t, hi_q, axis=1)
        inside = (theta_true_t >= lo) & (theta_true_t <= hi)   # (N,d)
        per[lv] = [float(inside[:, j].mean()) for j in range(d)]
        emp[lv] = float(inside.mean())
        cov_err[lv] = float(abs(emp[lv] - lv))
    return CalibrationResult(levels=list(levels), empirical_coverage=emp,
                             coverage_error=cov_err, per_param_coverage=per,
                             max_coverage_error=float(max(cov_err.values())),
                             n_eval=int(N))
