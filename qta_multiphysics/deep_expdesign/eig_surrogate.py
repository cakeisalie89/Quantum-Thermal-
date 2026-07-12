"""Learned expected-information-gain from the amortized posterior.

EIG(design) is estimated with the trained conditional density model via the
amortized-posterior mutual-information lower bound:

    EIG(d) ~= E_{theta~prior, x~p(x|theta,d)} [ log q(theta | x, d) - log p(theta) ]

evaluated with the learned q. This is fast (one forward pass per sample, no inner
Monte-Carlo loop). It is a *lower bound* on the true EIG and must be validated
against the direct nested-Monte-Carlo reference before it is trusted.
"""
from __future__ import annotations

import numpy as np

from . import likelihood_model as _lm
from . import transforms as _tf
from .simulator_adapter import observable_names
from .design_space import ExperimentDesign


def _design_norm(design: ExperimentDesign) -> np.ndarray:
    feat = np.array([[getattr(design, n) for n in _lm.design_feature_names()]])
    return _lm.normalize_design_features(feat)[0]


def learned_eig(trained, design: ExperimentDesign, *, n_samples: int = 600,
                seed: int = 0) -> float:
    """Amortized-posterior EIG estimate (nats) for one design."""
    names = trained.dataset.names
    transforms = trained.dataset.transforms
    lo, hi = trained.prior_lo, trained.prior_hi
    d = len(names)
    rng = np.random.default_rng(seed)
    # sample theta ~ prior (transformed, uniform)
    theta_t = lo + rng.random((n_samples, d)) * (hi - lo)
    # simulate x ~ p(x|theta,design) with measurement noise
    mean = _lm.forward_matrix(theta_t, design, names, transforms)
    sigma = _lm.obs_sigma_vector()
    x = mean + sigma * rng.standard_normal(mean.shape)
    # build context and evaluate log q in standardized theta space
    dn = np.tile(_design_norm(design), (n_samples, 1))
    ctx = np.hstack([trained.std_x.transform(x), dn])
    theta_std = trained.std_theta.transform(theta_t)
    logq_std = trained.model.log_prob(ctx, theta_std)
    # change of variables to transformed space; subtract uniform prior log-density
    log_jac = float(np.sum(np.log(trained.std_theta.std)))
    vol = float(np.sum(np.log(hi - lo)))                 # -log p(theta) = +vol
    eig = float(np.mean(logq_std) - log_jac + vol)
    return eig


def learned_eig_batch(trained, designs: list, *, n_samples: int = 600,
                      seed: int = 0) -> np.ndarray:
    return np.array([learned_eig(trained, d, n_samples=n_samples, seed=seed + i)
                     for i, d in enumerate(designs)])
