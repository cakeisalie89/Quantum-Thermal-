"""Joint latent physics-parameter space for the deep layer.

The joint latent vector is assembled from *existing* repository sources only:
the experimental-design parameter models (authoritative ranges/transforms), the
assumed- and measured-parameter registries, and provenance. No authoritative
numerical prior is invented here. Parameters listed by the directive for which
the repository provides no defensible range are recorded with
``value=null / status=UNKNOWN / trainable=false / source_required=true`` and are
excluded from the trainable subspace.

Default priors are independent (uniform in transformed space). Correlated priors
are only constructed when explicitly configured with evidence; absent that, the
default is independence, as required.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

from .schemas import LatentParameter
from . import transforms as _tf

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Directive-mandated joint latent coordinates. The value/range come from the
# repo where available; otherwise they are flagged UNKNOWN + source_required.
# Mapping notes: nv_contrast_10mK and the design parameter C_contr_10mK refer to
# the same 10 mK ODMR contrast unknown; he3_surface_coverage maps to the
# h2_coverage-style log coverage model only as a transform template, but is kept
# UNKNOWN unless the registry provides a coverage range (it does not), so it is
# non-trainable. We never silently borrow an unrelated range.
_DIRECTIVE_LATENTS = [
    "tau_c", "B_rms", "he3_he4_contrast", "surface_spin_density",
    "he3_surface_coverage", "surface_diffusion_coefficient", "sticking_probability",
    "residence_time", "nv_depth", "nv_contrast_10mK", "background_surface_spin_density",
    "thermal_recovery_time", "mode_isolation_leak",
]

# Which directive latents are backed by an authoritative design-model range.
# (Keys are directive names; values are expdesign _PARAM_MODELS keys.)
_BACKED_BY_PARAM_MODEL = {
    "tau_c": "tau_c",
    "he3_he4_contrast": "he3_he4_contrast",
    "nv_contrast_10mK": "C_contr_10mK",
    "thermal_recovery_time": "thermal_recovery_time",
    "mode_isolation_leak": "mode_isolation_leak",
}

_NUISANCE = {"B_rms", "background_surface_spin_density", "residence_time"}


def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_latent_parameters(repo_root: Optional[Path] = None) -> list:
    """Assemble the joint latent vector from existing registries.

    Returns a list of :class:`LatentParameter`. Trainable iff a defensible range
    exists; otherwise UNKNOWN / non-trainable / source_required.
    """
    root = Path(repo_root) if repo_root else _REPO_ROOT
    # authoritative ranges/transforms from the existing design models
    from qta_multiphysics.expdesign.model import _PARAM_MODELS  # reuse, do not redefine

    assumed_path = root / "assumed_parameters.json"
    assumed = _load_json(assumed_path) if assumed_path.exists() else {}

    out = []
    for name in _DIRECTIVE_LATENTS:
        if name in _BACKED_BY_PARAM_MODEL:
            pm = _PARAM_MODELS[_BACKED_BY_PARAM_MODEL[name]]
            lo_t, hi_t = pm.prior_lo, pm.prior_hi
            nat_lo = float(_tf.from_model(np.array(lo_t), pm.transform))
            nat_hi = float(_tf.from_model(np.array(hi_t), pm.transform))
            out.append(LatentParameter(
                name=name, unit=pm.unit, transform=pm.transform,
                prior_family="uniform",
                prior_parameters={"low": lo_t, "high": hi_t, "space": "transformed"},
                evidence_status=pm.status,
                source=f"qta_multiphysics.expdesign.model._PARAM_MODELS[{_BACKED_BY_PARAM_MODEL[name]!r}]",
                validity_range=[nat_lo, nat_hi],
                is_nuisance=name in _NUISANCE, is_trainable=True,
                source_required=False,
            ))
        else:
            # No defensible range in the repository -> fail-closed UNKNOWN.
            out.append(LatentParameter(
                name=name, unit="unknown", transform="linear",
                prior_family="none", prior_parameters={},
                evidence_status="UNKNOWN",
                source="no defensible range in repository registries",
                validity_range=None,
                is_nuisance=name in _NUISANCE, is_trainable=False,
                source_required=True,
            ))
    return out


def trainable_subspace(latents: list) -> list:
    """Return only the trainable latent parameters (defensible range present)."""
    return [p for p in latents if p.is_trainable]


def sample_prior(latents: list, n: int, seed: int, sampler: str = "lhs") -> dict:
    """Deterministically sample the *trainable* latent subspace from its priors.

    Returns dict with: 'names' (list), 'natural' (n x d array, natural units),
    'model' (n x d array, model space). Non-trainable params are excluded.
    Sampler is Latin-hypercube ('lhs') or Sobol-like ('sobol'); both deterministic.
    """
    tr = trainable_subspace(latents)
    d = len(tr)
    if d == 0:
        return {"names": [], "natural": np.zeros((n, 0)), "model": np.zeros((n, 0))}
    u = _unit_design(n, d, seed, sampler)  # n x d in [0,1)
    model = np.empty((n, d))
    natural = np.empty((n, d))
    for j, p in enumerate(tr):
        lo = p.prior_parameters["low"]
        hi = p.prior_parameters["high"]
        z = lo + u[:, j] * (hi - lo)            # uniform in transformed space
        model[:, j] = z
        natural[:, j] = _tf.from_model(z, p.transform)
    return {"names": [p.name for p in tr], "natural": natural, "model": model}


def _unit_design(n: int, d: int, seed: int, sampler: str) -> np.ndarray:
    """Deterministic [0,1)^d design of n points (LHS or scrambled van der Corput)."""
    rng = np.random.default_rng(seed)
    if sampler == "lhs":
        out = np.empty((n, d))
        for j in range(d):
            perm = rng.permutation(n)
            out[:, j] = (perm + rng.random(n)) / n
        return out
    if sampler == "sobol":
        # dependency-free, deterministic radical-inverse (van der Corput) per dim
        bases = _first_primes(d)
        out = np.empty((n, d))
        for j in range(d):
            out[:, j] = [_radical_inverse(i + 1, bases[j]) for i in range(n)]
        # deterministic per-seed scramble
        out = (out + rng.random(d)[None, :]) % 1.0
        return out
    raise ValueError(f"unknown sampler {sampler!r}")


def _radical_inverse(i: int, base: int) -> float:
    f = 1.0
    r = 0.0
    while i > 0:
        f /= base
        r += f * (i % base)
        i //= base
    return r


def _first_primes(k: int) -> list:
    primes = []
    cand = 2
    while len(primes) < k:
        if all(cand % p for p in primes):
            primes.append(cand)
        cand += 1
    return primes
