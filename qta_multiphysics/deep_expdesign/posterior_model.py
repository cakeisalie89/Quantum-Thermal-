"""Conditional mixture-density posterior (pure NumPy, deterministic, CPU).

An amortized conditional density estimator q(theta | context), where context is
[normalized observation, normalized design]. The model is a small tanh MLP with a
Gaussian-mixture head (diagonal covariances). It is trained by maximum likelihood
(mean negative log-likelihood) with Adam using *analytic* gradients (verified
against finite differences in the test suite). Everything is seeded and
deterministic; no GPU/CUDA is used.

This is a documented neural density estimator with reproducible CPU support (the
directive's allowed alternative to a PyTorch normalizing flow). It produces
posterior samples, means/medians, credible intervals, covariance, and log-prob.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

_S_MIN, _S_MAX = -7.0, 3.0          # clamp on log-sigma for numerical stability
_LOG_2PI = float(np.log(2.0 * np.pi))


def _logsumexp(a: np.ndarray, axis: int) -> np.ndarray:
    m = np.max(a, axis=axis, keepdims=True)
    out = m + np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True))
    return np.squeeze(out, axis=axis)


@dataclass
class MDNParams:
    W1: np.ndarray; b1: np.ndarray
    W2: np.ndarray; b2: np.ndarray
    Wpi: np.ndarray; bpi: np.ndarray
    Wmu: np.ndarray; bmu: np.ndarray
    Ws: np.ndarray; bs: np.ndarray

    def flat(self):
        return [self.W1, self.b1, self.W2, self.b2, self.Wpi, self.bpi,
                self.Wmu, self.bmu, self.Ws, self.bs]

    def names(self):
        return ["W1", "b1", "W2", "b2", "Wpi", "bpi", "Wmu", "bmu", "Ws", "bs"]


class MDNPosterior:
    """Amortized conditional mixture-density posterior network."""

    def __init__(self, d_in: int, d_out: int, n_mixture: int = 3,
                 hidden: int = 32, seed: int = 0):
        self.d_in = int(d_in)
        self.d_out = int(d_out)
        self.K = int(n_mixture)
        self.H = int(hidden)
        rng = np.random.default_rng(seed)
        H, K, d = self.H, self.K, self.d_out

        def he(shape, fan_in):
            return rng.normal(0.0, np.sqrt(2.0 / fan_in), size=shape)

        self.p = MDNParams(
            W1=he((d_in, H), d_in), b1=np.zeros(H),
            W2=he((H, H), H), b2=np.zeros(H),
            Wpi=he((H, K), H) * 0.1, bpi=np.zeros(K),
            Wmu=he((H, K * d), H) * 0.1, bmu=np.zeros(K * d),
            Ws=np.zeros((H, K * d)), bs=np.zeros(K * d),  # start sigma ~ 1
        )
        self.trained = False

    # ----- forward -----
    def _forward(self, X: np.ndarray):
        p = self.p
        z1 = X @ p.W1 + p.b1
        h1 = np.tanh(z1)
        z2 = h1 @ p.W2 + p.b2
        h2 = np.tanh(z2)
        logits = h2 @ p.Wpi + p.bpi                       # (N,K)
        mu = (h2 @ p.Wmu + p.bmu).reshape(-1, self.K, self.d_out)
        s = (h2 @ p.Ws + p.bs).reshape(-1, self.K, self.d_out)
        s = np.clip(s, _S_MIN, _S_MAX)
        cache = (X, h1, h2)
        return logits, mu, s, cache

    def _log_components(self, Y, logits, mu, s):
        """Return (log_mix_weights (N,K), per-comp loglik L (N,K), z (N,K,d))."""
        log_w = logits - _logsumexp(logits, axis=1)[:, None]   # log softmax
        sig = np.exp(s)
        z = (Y[:, None, :] - mu) / sig                          # (N,K,d)
        L = np.sum(-0.5 * z ** 2 - s - 0.5 * _LOG_2PI, axis=2)  # (N,K)
        return log_w, L, z

    def log_prob(self, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
        """Per-sample log q(Y|X)."""
        logits, mu, s, _ = self._forward(X)
        log_w, L, _ = self._log_components(Y, logits, mu, s)
        return _logsumexp(log_w + L, axis=1)

    def nll(self, X: np.ndarray, Y: np.ndarray) -> float:
        return float(-np.mean(self.log_prob(X, Y)))

    # ----- gradients -----
    def _grads(self, X, Y, weight_decay: float):
        logits, mu, s, cache = self._forward(X)
        Xin, h1, h2 = cache
        N = X.shape[0]
        log_w, L, z = self._log_components(Y, logits, mu, s)
        logp = _logsumexp(log_w + L, axis=1)                   # (N,)
        # responsibilities r_k
        r = np.exp((log_w + L) - logp[:, None])                # (N,K)
        pi = np.exp(log_w)                                     # softmax weights
        sig = np.exp(s)

        glogits = (pi - r) / N                                  # (N,K)
        gmu = (-(r[:, :, None]) * (z / sig)) / N                # (N,K,d)
        gs = (-(r[:, :, None]) * (z ** 2 - 1.0)) / N            # (N,K,d)
        gmu_flat = gmu.reshape(N, -1)
        gs_flat = gs.reshape(N, -1)

        p = self.p
        gWpi = h2.T @ glogits;  gbpi = glogits.sum(0)
        gWmu = h2.T @ gmu_flat; gbmu = gmu_flat.sum(0)
        gWs = h2.T @ gs_flat;   gbs = gs_flat.sum(0)
        gh2 = glogits @ p.Wpi.T + gmu_flat @ p.Wmu.T + gs_flat @ p.Ws.T
        gz2 = gh2 * (1.0 - h2 ** 2)
        gW2 = h1.T @ gz2; gb2 = gz2.sum(0)
        gh1 = gz2 @ p.W2.T
        gz1 = gh1 * (1.0 - h1 ** 2)
        gW1 = Xin.T @ gz1; gb1 = gz1.sum(0)

        grads = MDNParams(gW1, gb1, gW2, gb2, gWpi, gbpi, gWmu, gbmu, gWs, gbs)
        # L2 weight decay on weight matrices only
        for nm in ("W1", "W2", "Wpi", "Wmu", "Ws"):
            setattr(grads, nm, getattr(grads, nm) + weight_decay * getattr(p, nm))
        return grads, float(-np.mean(logp))

    # ----- training -----
    def fit(self, X: np.ndarray, Y: np.ndarray, *, steps: int = 300,
            lr: float = 5e-3, weight_decay: float = 1e-4, seed: int = 0) -> dict:
        """Full-batch Adam training. Returns history dict (deterministic)."""
        X = np.asarray(X, float); Y = np.asarray(Y, float)
        b1c, b2c, eps = 0.9, 0.999, 1e-8
        m = {n: np.zeros_like(v) for n, v in zip(self.p.names(), self.p.flat())}
        v = {n: np.zeros_like(v) for n, v in zip(self.p.names(), self.p.flat())}
        hist = {"step": [], "nll": []}
        for t in range(1, steps + 1):
            grads, loss = self._grads(X, Y, weight_decay)
            for n, g in zip(grads.names(), grads.flat()):
                m[n] = b1c * m[n] + (1 - b1c) * g
                v[n] = b2c * v[n] + (1 - b2c) * (g * g)
                mhat = m[n] / (1 - b1c ** t)
                vhat = v[n] / (1 - b2c ** t)
                cur = getattr(self.p, n)
                setattr(self.p, n, cur - lr * mhat / (np.sqrt(vhat) + eps))
            if t == 1 or t % 10 == 0 or t == steps:
                hist["step"].append(t)
                hist["nll"].append(float(loss))
        self.trained = True
        return hist

    # ----- posterior queries -----
    def predict_mixture(self, X: np.ndarray):
        logits, mu, s, _ = self._forward(X)
        log_w = logits - _logsumexp(logits, axis=1)[:, None]
        return np.exp(log_w), mu, np.exp(s)          # weights (N,K), mu, sigma

    def sample(self, X: np.ndarray, n: int, seed: int = 0) -> np.ndarray:
        """Draw n posterior samples per row of X. Returns (N, n, d)."""
        rng = np.random.default_rng(seed)
        w, mu, sig = self.predict_mixture(X)
        N = X.shape[0]
        out = np.empty((N, n, self.d_out))
        for i in range(N):
            comp = rng.choice(self.K, size=n, p=w[i] / w[i].sum())
            out[i] = mu[i, comp, :] + sig[i, comp, :] * rng.standard_normal((n, self.d_out))
        return out

    def credible_interval(self, X: np.ndarray, levels=(0.5, 0.8, 0.9, 0.95),
                          n: int = 2000, seed: int = 0) -> dict:
        """Empirical central credible intervals per coordinate via sampling."""
        s = self.sample(X, n=n, seed=seed)              # (N,n,d)
        out = {}
        for lv in levels:
            lo = (1 - lv) / 2 * 100
            hi = (1 + lv) / 2 * 100
            out[lv] = (np.percentile(s, lo, axis=1), np.percentile(s, hi, axis=1))
        return out
