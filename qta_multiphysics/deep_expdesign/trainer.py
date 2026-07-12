"""Training pipeline: build the amortized dataset and fit the posterior model.

Each training point is (theta_i ~ prior, design_i ~ Mode-D sensing design
distribution, x_i ~ p(x | theta_i, design_i)) from the simulator adapter. The
density model is trained to estimate q(theta | x, design) over the design
distribution (amortized). Train/val/test are disjoint; standardizers are fit on
the training split only and reused everywhere (no leakage). Deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .config import DeepConfig
from . import likelihood_model as _lm
from . import transforms as _tf
from .simulator_adapter import simulate, observable_names
from .posterior_model import MDNPosterior


@dataclass
class AmortizedDataset:
    theta_t: np.ndarray          # (N, d_theta) transformed space
    design_feat_nat: np.ndarray  # (N, d_design) natural
    design_norm: np.ndarray      # (N, d_design) in [0,1]
    x: np.ndarray                # (N, k_obs) raw observables
    success: np.ndarray          # (N,) bool
    names: list                  # latent names
    transforms: list             # latent transforms
    obs_names: list
    split: dict


@dataclass
class TrainedPosterior:
    model: MDNPosterior
    std_x: _lm.Standardizer
    std_theta: _lm.Standardizer
    dataset: AmortizedDataset
    history: dict
    context_train: np.ndarray
    prior_lo: np.ndarray
    prior_hi: np.ndarray


def _split(n: int, fractions: tuple, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n)
    n_tr = int(round(fractions[0] * n)); n_va = int(round(fractions[1] * n))
    return {"train": np.sort(perm[:n_tr]), "val": np.sort(perm[n_tr:n_tr + n_va]),
            "test": np.sort(perm[n_tr + n_va:])}


def build_amortized_dataset(cfg: DeepConfig, *, repo_root: Optional[Path] = None) -> AmortizedDataset:
    """Generate the deterministic amortized (theta, design, x) dataset."""
    cfg.validate()
    names, lo, hi, transforms = _lm.trainable_prior_bounds(repo_root)
    d = len(names)
    N = cfg.n_dataset
    # prior sample in transformed space (uniform on [lo,hi])
    u = _tf  # alias
    rng_unit = np.random.default_rng(cfg.master_seed + 101)
    U = np.empty((N, d))
    for j in range(d):
        perm = rng_unit.permutation(N)
        U[:, j] = (perm + rng_unit.random(N)) / N          # LHS in [0,1)
    theta_t = lo + U * (hi - lo)
    designs, feat_nat = _lm.sample_designs(N, seed=cfg.master_seed + 202, sampler=cfg.sampler)
    design_norm = _lm.normalize_design_features(feat_nat)

    obs = observable_names()
    x = np.full((N, len(obs)), np.nan)
    success = np.zeros(N, dtype=bool)
    for i in range(N):
        nat = {names[j]: float(_tf.from_model(np.array(theta_t[i, j]), transforms[j]))
               for j in range(d)}
        resp = simulate(nat, designs[i], seed=cfg.master_seed + 7919 * (i + 1),
                        repo_root=repo_root)
        success[i] = resp.success
        if resp.success:
            x[i, :] = [resp.observables[k] for k in obs]
    split = _split(N, cfg.split_fractions, cfg.master_seed + 303)
    return AmortizedDataset(theta_t, feat_nat, design_norm, x, success, names,
                            transforms, obs, split)


def train_posterior(cfg: DeepConfig, *, repo_root: Optional[Path] = None,
                    dataset: Optional[AmortizedDataset] = None) -> TrainedPosterior:
    """Build the dataset (if needed) and fit the conditional density model."""
    ds = dataset or build_amortized_dataset(cfg, repo_root=repo_root)
    names, lo, hi, transforms = _lm.trainable_prior_bounds(repo_root)

    tr = ds.split["train"]
    ok = ds.success[tr]
    tr = tr[ok]                                            # train on successful sims
    # standardizers fit on TRAIN only
    std_x = _lm.Standardizer.fit(ds.x[tr])
    std_theta = _lm.Standardizer.fit(ds.theta_t[tr])

    def context(idx):
        return np.hstack([std_x.transform(ds.x[idx]), ds.design_norm[idx]])

    Xtr = context(tr)
    Ytr = std_theta.transform(ds.theta_t[tr])
    model = MDNPosterior(d_in=Xtr.shape[1], d_out=Ytr.shape[1],
                         n_mixture=cfg.n_mixture, hidden=cfg.hidden_units,
                         seed=cfg.master_seed)
    history = model.fit(Xtr, Ytr, steps=cfg.train_steps, lr=cfg.learning_rate,
                        weight_decay=cfg.weight_decay, seed=cfg.master_seed)
    return TrainedPosterior(model=model, std_x=std_x, std_theta=std_theta,
                            dataset=ds, history=history, context_train=Xtr,
                            prior_lo=lo, prior_hi=hi)
