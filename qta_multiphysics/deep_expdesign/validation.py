"""Direct nested-Monte-Carlo EIG reference and trust validation.

The deep EIG is compared against a *direct nested-Monte-Carlo* EIG computed on the
identical simulator-adapter generative model, using the same nested-MC algorithm
as the existing experimental-design engine (which remains unchanged and canonical
for the existing layer). For a Gaussian observation model with fixed noise,

    EIG(d) = H(x|d) - H(x|theta,d),   H(x|theta,d) = const,
    H(x|d) ~= -mean_i log[ mean_j N(x_i; f(theta_j,d), Sigma) ]   (nested MC)

Trust metrics are compared against the conservative thresholds in config. A
surrogate is trusted ONLY if every threshold passes.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from .config import DeepConfig, TrustThresholds
from . import likelihood_model as _lm
from .design_space import ExperimentDesign
from .eig_surrogate import learned_eig


def direct_mc_eig(design: ExperimentDesign, names, transforms, lo, hi, *,
                  n_outer: int, n_inner: int, n_batches: int, seed: int) -> tuple:
    """Direct nested-MC EIG (nats) for one design. Returns (mean, std_error)."""
    sigma = _lm.obs_sigma_vector()
    H_cond = _lm.gaussian_conditional_entropy(sigma)
    d = len(names)
    batch_eig = []
    for b in range(n_batches):
        rng = np.random.default_rng(seed + 1000 * b)
        theta_out = lo + rng.random((n_outer, d)) * (hi - lo)
        f_out = _lm.forward_matrix(theta_out, design, names, transforms)
        x_out = f_out + sigma * rng.standard_normal(f_out.shape)
        theta_in = lo + rng.random((n_inner, d)) * (hi - lo)
        f_in = _lm.forward_matrix(theta_in, design, names, transforms)  # (n_inner,k)
        # log marginal at each x_out via inner mixture over theta_in
        # log p(x) ~= logsumexp_j N(x; f_in_j, Sigma) - log(n_inner)
        logmarg = np.empty(n_outer)
        inv2s2 = 1.0 / (2.0 * sigma ** 2)
        norm = -np.sum(np.log(sigma)) - 0.5 * len(sigma) * np.log(2 * np.pi)
        for i in range(n_outer):
            d2 = np.sum((x_out[i][None, :] - f_in) ** 2 * inv2s2, axis=1)
            ll = norm - d2
            m = ll.max()
            logmarg[i] = m + np.log(np.mean(np.exp(ll - m)))
        H_marg = -np.mean(logmarg)
        batch_eig.append(H_marg - H_cond)
    batch_eig = np.array(batch_eig)
    return float(batch_eig.mean()), float(batch_eig.std(ddof=1) / np.sqrt(n_batches))


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b))
    ra = ra - ra.mean(); rb = rb - rb.mean()
    denom = np.sqrt(np.sum(ra ** 2) * np.sum(rb ** 2))
    return float(np.sum(ra * rb) / denom) if denom > 0 else 0.0


def _topk_agreement(a: np.ndarray, b: np.ndarray, k: int) -> float:
    ta = set(np.argsort(-a)[:k].tolist()); tb = set(np.argsort(-b)[:k].tolist())
    return len(ta & tb) / k


@dataclass
class ValidationResult:
    designs: list
    eig_deep: list
    eig_direct: list
    eig_direct_se: list
    mean_abs_error: float
    max_abs_error: float
    normalized_error: float
    spearman: float
    top1_agreement: float
    top3_agreement: float
    repeat_seed_eig_std: float
    thresholds_passed: bool
    per_threshold: dict

    def to_dict(self) -> dict:
        return asdict(self)


def validate_against_direct_mc(trained, designs: list, cfg: DeepConfig, *,
                               seed: int = 0) -> ValidationResult:
    """Compare deep EIG vs direct nested-MC EIG on a benchmark design set."""
    names = trained.dataset.names; transforms = trained.dataset.transforms
    lo, hi = trained.prior_lo, trained.prior_hi
    eig_deep, eig_dir, eig_se = [], [], []
    for i, dz in enumerate(designs):
        eig_deep.append(learned_eig(trained, dz, n_samples=cfg.eig_ref_outer, seed=seed + i))
        m, se = direct_mc_eig(dz, names, transforms, lo, hi, n_outer=cfg.eig_ref_outer,
                              n_inner=cfg.eig_ref_inner, n_batches=cfg.eig_ref_batches,
                              seed=seed + 50 * (i + 1))
        eig_dir.append(m); eig_se.append(se)
    eig_deep = np.array(eig_deep); eig_dir = np.array(eig_dir); eig_se = np.array(eig_se)

    abs_err = np.abs(eig_deep - eig_dir)
    norm_err = float(np.mean(abs_err / np.maximum(eig_se, 1e-9)))
    # repeat-seed stability of deep EIG predictions
    reps = []
    for r in range(cfg.n_repeat_seeds):
        reps.append([learned_eig(trained, dz, n_samples=cfg.eig_ref_outer, seed=10000 + 31 * r + i)
                     for i, dz in enumerate(designs)])
    repeat_std = float(np.mean(np.std(np.array(reps), axis=0)))

    sp = _spearman(eig_deep, eig_dir)
    t1 = _topk_agreement(eig_deep, eig_dir, 1)
    t3 = _topk_agreement(eig_deep, eig_dir, min(3, len(designs)))
    th = cfg.thresholds

    per = {
        "mean_abs_eig_error_nats": (float(abs_err.mean()), th.max_mean_abs_eig_error_nats,
                                    float(abs_err.mean()) <= th.max_mean_abs_eig_error_nats),
        "max_abs_eig_error_nats": (float(abs_err.max()), th.max_max_abs_eig_error_nats,
                                   float(abs_err.max()) <= th.max_max_abs_eig_error_nats),
        "normalized_eig_error": (norm_err, th.max_normalized_eig_error,
                                 norm_err <= th.max_normalized_eig_error),
        "spearman": (sp, th.min_spearman_rank_corr, sp >= th.min_spearman_rank_corr),
        "top1_agreement": (t1, th.min_top1_agreement, t1 >= th.min_top1_agreement),
        "top3_agreement": (t3, th.min_top3_agreement, t3 >= th.min_top3_agreement),
        "repeat_seed_eig_std_nats": (repeat_std, th.max_repeat_seed_eig_std_nats,
                                     repeat_std <= th.max_repeat_seed_eig_std_nats),
    }
    passed = all(v[2] for v in per.values())
    return ValidationResult(
        designs=[d.experiment_id for d in designs], eig_deep=eig_deep.tolist(),
        eig_direct=eig_dir.tolist(), eig_direct_se=eig_se.tolist(),
        mean_abs_error=float(abs_err.mean()), max_abs_error=float(abs_err.max()),
        normalized_error=norm_err, spearman=sp, top1_agreement=t1, top3_agreement=t3,
        repeat_seed_eig_std=repeat_std, thresholds_passed=passed, per_threshold=per)


def benchmark_designs(n: int = 6) -> list:
    """Deterministic benchmark design set spanning the sensing design space."""
    designs, _ = _lm.sample_designs(n, seed=777, sampler="sobol")
    for i, d in enumerate(designs):
        d.experiment_id = f"BENCH-{i+1}"
    return designs
