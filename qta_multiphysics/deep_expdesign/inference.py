"""Posterior inference queries in natural parameter units.

Wraps the trained conditional density model to answer queries for a given
observation + design: posterior samples, means/medians, credible intervals,
covariance, and log-probability — all mapped back to natural parameter units.
Forecast-only; no measurement is involved.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from . import likelihood_model as _lm
from . import transforms as _tf
from .eig_surrogate import _design_norm
from .design_space import ExperimentDesign


def _context(trained, x_obs: np.ndarray, design: ExperimentDesign) -> np.ndarray:
    x_obs = np.atleast_2d(x_obs)
    dn = np.tile(_design_norm(design), (x_obs.shape[0], 1))
    return np.hstack([trained.std_x.transform(x_obs), dn])


def posterior_query(trained, x_obs: np.ndarray, design: ExperimentDesign, *,
                    n_samples: int = 2000, seed: int = 0) -> dict:
    """Return posterior summary in natural units for one observation."""
    names = trained.dataset.names
    transforms = trained.dataset.transforms
    ctx = _context(trained, x_obs, design)
    samp_std = trained.model.sample(ctx, n=n_samples, seed=seed)[0]      # (n,d)
    samp_t = samp_std * trained.std_theta.std + trained.std_theta.mean   # transformed
    # to natural units per coordinate
    samp_nat = np.empty_like(samp_t)
    for j in range(samp_t.shape[1]):
        samp_nat[:, j] = _tf.from_model(samp_t[:, j], transforms[j])

    def ci(level):
        lo = (1 - level) / 2 * 100; hi = (1 + level) / 2 * 100
        return [np.percentile(samp_nat, lo, axis=0).tolist(),
                np.percentile(samp_nat, hi, axis=0).tolist()]

    return {
        "parameter_names": list(names),
        "posterior_mean_natural": samp_nat.mean(0).tolist(),
        "posterior_median_natural": np.median(samp_nat, axis=0).tolist(),
        "credible_interval_90_natural": ci(0.90),
        "credible_interval_50_natural": ci(0.50),
        "covariance_transformed": np.cov(samp_t, rowvar=False).tolist(),
        "n_samples": int(n_samples),
        "forecast_only": True,
        "measured_in_this_system": False,
    }


def posterior_predictive(trained, design: ExperimentDesign, *, n: int = 500,
                         seed: int = 0) -> np.ndarray:
    """Posterior-predictive observable simulations under the prior+design."""
    names = trained.dataset.names; transforms = trained.dataset.transforms
    lo, hi = trained.prior_lo, trained.prior_hi
    rng = np.random.default_rng(seed)
    theta_t = lo + rng.random((n, len(names))) * (hi - lo)
    mean = _lm.forward_matrix(theta_t, design, names, transforms)
    sigma = _lm.obs_sigma_vector()
    return mean + sigma * rng.standard_normal(mean.shape)
