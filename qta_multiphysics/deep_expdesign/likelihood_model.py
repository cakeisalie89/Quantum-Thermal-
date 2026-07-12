"""Generative-model helpers for the deep layer's inference and EIG estimation.

Defines, for the simulator-adapter forward model, the trainable-latent prior (in
transformed space), a Mode-D sensing design sampler, the Gaussian observation
likelihood (fixed measurement noise), and a standardizer used to put contexts and
targets on a common scale for the density model. All deterministic.

The reduced forward map is model-only and forecast-only; this module does not
claim a high-fidelity physics likelihood.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import parameter_space as _ps
from . import transforms as _tf
from .design_space import ExperimentDesign
from .simulator_adapter import (clean_forward, observable_names, meas_sigma,
                                _temp_factor, _coverage_factor, _SIGMA_RAD_S)

# Mode-D sensing design knobs varied for amortized training (normalization bounds).
# (name, transform, lo, hi) in natural units; 'log10' bounds are in log space.
_DESIGN_FEATURES = [
    ("measurement_time", "log10", -6.0, -1.0),
    ("surface_temperature", "linear", 5e-3, 5e-1),
    ("he3_coverage_target", "linear", 0.0, 2.0),
    ("recovery_duration", "linear", 0.0, 500.0),
]


def design_feature_names() -> list:
    return [f[0] for f in _DESIGN_FEATURES]


def trainable_prior_bounds(repo_root: Optional[Path] = None):
    """Return (names, lo, hi) of the trainable latents in *transformed* space."""
    lat = _ps.build_latent_parameters(repo_root)
    tr = _ps.trainable_subspace(lat)
    names = [p.name for p in tr]
    lo = np.array([p.prior_parameters["low"] for p in tr])
    hi = np.array([p.prior_parameters["high"] for p in tr])
    transforms = [p.transform for p in tr]
    return names, lo, hi, transforms


def prior_logpdf_transformed(theta_t: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Uniform prior log-density in transformed space (per row)."""
    theta_t = np.atleast_2d(theta_t)
    inside = np.all((theta_t >= lo) & (theta_t <= hi), axis=1)
    vol = float(np.sum(np.log(hi - lo)))
    out = np.where(inside, -vol, -np.inf)
    return out


def sample_designs(n: int, seed: int, sampler: str = "lhs") -> tuple:
    """Sample n valid Mode-D sensing designs. Returns (designs, feat_natural)."""
    d = len(_DESIGN_FEATURES)
    u = _ps._unit_design(n, d, seed, sampler)          # (n,d) in [0,1)
    feats = np.empty((n, d))
    designs = []
    for i in range(n):
        vals = {}
        for j, (name, trans, lo, hi) in enumerate(_DESIGN_FEATURES):
            t = lo + u[i, j] * (hi - lo)
            nat = float(_tf.from_model(np.array(t), trans))
            vals[name] = nat
            feats[i, j] = nat
        designs.append(ExperimentDesign(
            experiment_id="amortized", pulse_sequence="hahn", mode="D",
            measurement_time=vals["measurement_time"],
            surface_temperature=vals["surface_temperature"],
            he3_coverage_target=vals["he3_coverage_target"],
            recovery_duration=vals["recovery_duration"]))
    return designs, feats


def normalize_design_features(feats_natural: np.ndarray) -> np.ndarray:
    """Map natural design features to [0,1] using the declared bounds."""
    feats_natural = np.atleast_2d(feats_natural)
    out = np.empty_like(feats_natural, dtype=float)
    for j, (_, trans, lo, hi) in enumerate(_DESIGN_FEATURES):
        t = _tf.to_model(feats_natural[:, j], trans)
        tlo = lo if trans == "linear" else lo
        thi = hi if trans == "linear" else hi
        out[:, j] = (t - tlo) / (thi - tlo)
    return out


def obs_sigma_vector() -> np.ndarray:
    """Per-observable measurement-noise std (fixed), in observable order."""
    ms = meas_sigma()
    return np.array([ms[k] for k in observable_names()])


_COH_CACHE = {}


def _coherence_grid(seq_for: str, measurement_time: float, n_omega: int):
    """Cached (grid, coherence) over the tau_c prior range for a fixed design."""
    key = (seq_for, round(float(measurement_time), 15), int(n_omega))
    if key not in _COH_CACHE:
        from qta_multiphysics.nv_spin.noise import filter_function_coherence
        from .simulator_adapter import _SIGMA_RAD_S
        grid = np.linspace(-6.5, -1.5, 80)               # log10(tau_c), wider than prior
        cg = np.array([filter_function_coherence(
            sequence=seq_for, tau_c_s=float(10.0 ** lt), sigma_rad_s=_SIGMA_RAD_S,
            total_time_s=float(measurement_time), n_omega=n_omega) for lt in grid])
        _COH_CACHE[key] = (grid, cg)
    return _COH_CACHE[key]


def forward_matrix(theta_t: np.ndarray, design: ExperimentDesign,
                   names: list, transforms: list) -> np.ndarray:
    """Clean forward observable for a batch of transformed-space thetas (n,k_obs).

    Vectorized: for a fixed design the coherence depends only on tau_c, so it is
    evaluated on a cached grid over log10(tau_c) and interpolated (matching
    ``clean_forward`` to interpolation accuracy); the other observables are exact
    closed-form maps.
    """
    from .simulator_adapter import _temp_factor, _coverage_factor, _N_OMEGA
    theta_t = np.atleast_2d(theta_t)
    n = theta_t.shape[0]
    obs = observable_names()
    oi = {nm: k for k, nm in enumerate(obs)}
    idx = {nm: j for j, nm in enumerate(names)}
    nat = {nm: _tf.from_model(theta_t[:, idx[nm]], transforms[idx[nm]]) for nm in names}
    out = np.empty((n, len(obs)))

    seq = (design.pulse_sequence or "hahn").lower()
    seq_for = seq if seq in ("ramsey", "hahn", "xy8") else "hahn"
    ltau = theta_t[:, idx["tau_c"]]                       # log10(tau_c)
    grid, cg = _coherence_grid(seq_for, design.measurement_time, _N_OMEGA)
    out[:, oi["coherence_at_tmeas"]] = np.interp(ltau, grid, cg)
    out[:, oi["odmr_contrast"]] = nat["nv_contrast_10mK"] * _temp_factor(design.surface_temperature)
    out[:, oi["he_contrast"]] = nat["he3_he4_contrast"] * _coverage_factor(design.he3_coverage_target)
    out[:, oi["thermal_recovery_obs"]] = np.log10(np.maximum(
        1e-12, nat["thermal_recovery_time"] * (1.0 + design.recovery_duration / 1.0e3)))
    out[:, oi["isolation_leak_obs"]] = np.log10(np.maximum(1e-12, nat["mode_isolation_leak"]))
    return out


def gaussian_loglik(x: np.ndarray, mean: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """Diagonal-Gaussian log-likelihood, summed over observables (per row)."""
    x = np.atleast_2d(x); mean = np.atleast_2d(mean)
    z = (x - mean) / sigma
    return np.sum(-0.5 * z ** 2 - np.log(sigma) - 0.5 * np.log(2 * np.pi), axis=1)


def gaussian_conditional_entropy(sigma: np.ndarray) -> float:
    """H(x|theta) for fixed diagonal Gaussian noise (constant in design)."""
    return float(np.sum(0.5 * np.log(2 * np.pi * np.e * sigma ** 2)))


@dataclass
class Standardizer:
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def fit(cls, A: np.ndarray) -> "Standardizer":
        A = np.atleast_2d(A)
        mu = A.mean(0)
        sd = A.std(0)
        sd = np.where(sd < 1e-12, 1.0, sd)
        return cls(mu, sd)

    def transform(self, A: np.ndarray) -> np.ndarray:
        return (np.atleast_2d(A) - self.mean) / self.std

    def inverse(self, A: np.ndarray) -> np.ndarray:
        return np.atleast_2d(A) * self.std + self.mean
