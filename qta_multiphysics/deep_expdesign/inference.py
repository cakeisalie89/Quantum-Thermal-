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


#: A surrogate may control experiment ordering or provide trusted posterior
#: inference only when ALL of these hold. OOD detection existed but was only
#: ever used to write a report; an ordinary posterior_query() consulted none of
#: it, so an out-of-distribution query was answered by silent extrapolation and
#: the answer looked exactly like an in-distribution one.
TRUST_REQUIREMENTS = ("trained", "validation_ran", "thresholds_passed",
                      "calibration_passed", "in_distribution")

TRUSTED = "TRUSTED_SURROGATE"
DENIED_OOD = "DENIED_OUT_OF_DISTRIBUTION"
DENIED_NOT_READY = "DENIED_SURROGATE_NOT_READY"


class SurrogateAuthorityDenied(RuntimeError):
    """Trusted-surrogate authority was requested and refused."""


def trust_state(trained, readiness: dict | None = None, *,
                ood=None, context=None) -> dict:
    """Evaluate every trust requirement for one inference context.

    Returns a record rather than a bool so a denial says which requirement
    failed. ``readiness`` is the deep-layer readiness record
    (deep_surrogate_readiness.json); when it is absent nothing is assumed to
    have passed -- absence is denial, not a default grant.
    """
    r = readiness or {}
    checks = {
        "trained": bool(r.get("trained", False)),
        "validation_ran": bool(r.get("compared_against_direct_mc", False)),
        "thresholds_passed": bool(r.get("thresholds_passed", False)),
        "calibration_passed": bool((r.get("calibration") or {}).get("passed", False))
        if isinstance(r.get("calibration"), dict) else False,
        # Denied until an OOD model actually scores this context. Defaulting
        # this to True would let a caller obtain trusted authority simply by
        # not passing an OOD model, which is the bypass this gate exists to
        # close: the check must RUN, not merely exist.
        "in_distribution": False,
    }
    ood_detail = {"is_ood": None,
                  "reason": "no OOD model was supplied, so distribution "
                            "membership was never evaluated; trusted authority "
                            "requires the check to run"}
    if ood is not None and context is not None:
        score = ood.score(context)
        is_ood = bool(np.any(score["is_ood"]))
        checks["in_distribution"] = not is_ood
        ood_detail = {
            "is_ood": is_ood,
            "mahalanobis": [float(v) for v in np.atleast_1d(score["mahalanobis"])],
            "maha_threshold": float(score["maha_threshold"]),
            "in_box": [bool(v) for v in np.atleast_1d(score["in_box"])],
        }
    elif ood is not None or context is not None:
        # Half a check is no check.
        ood_detail = {"is_ood": None,
                      "reason": "OOD model and query context must be supplied "
                                "together; one without the other cannot decide "
                                "distribution membership"}
    failed = [k for k in TRUST_REQUIREMENTS if not checks[k]]
    if not failed:
        state = TRUSTED
    elif failed == ["in_distribution"]:
        state = DENIED_OOD
    else:
        state = DENIED_NOT_READY
    return {"state": state, "trusted": not failed, "checks": checks,
            "failed_requirements": failed, "ood": ood_detail,
            "requirements": list(TRUST_REQUIREMENTS)}


def trusted_posterior_query(trained, x_obs, design, *, readiness=None,
                            ood=None, n_samples: int = 2000, seed: int = 0,
                            fallback=None) -> dict:
    """Posterior inference that may only be trusted when authority is granted.

    On denial this does NOT extrapolate silently. It returns an explicit
    denial state, and uses ``fallback`` (the direct Monte-Carlo estimator) when
    one is supplied; otherwise it raises.
    """
    ctx = _context(trained, x_obs, design)
    trust = trust_state(trained, readiness, ood=ood, context=ctx)
    if trust["trusted"]:
        out = posterior_query(trained, x_obs, design,
                              n_samples=n_samples, seed=seed)
        out["surrogate_authority"] = trust
        return out
    if fallback is not None:
        out = dict(fallback(x_obs, design))
        out["surrogate_authority"] = trust
        out["estimator"] = "direct_monte_carlo_fallback"
        return out
    raise SurrogateAuthorityDenied(
        f"{trust['state']}: failed {trust['failed_requirements']}; "
        "no trusted posterior is available and no direct-MC fallback was "
        "supplied")


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
        "surrogate_authority": {
            "state": "NOT_EVALUATED",
            "note": "posterior_query is the raw sampling primitive and asserts "
                    "no trust; call trusted_posterior_query() for an answer "
                    "that is gated on readiness and distribution membership",
        },
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
