"""Direct Bayesian experimental-design engine.

Expected information gain (EIG) is the mutual information I(theta; data) between a
parameter and its measurement, equal to the expected KL divergence from prior to
posterior. It is estimated by a nested Monte-Carlo estimator with a batched
standard-error, so every EIG carries an uncertainty. Rankings combine EIG with
gate criticality, falsification value, predecessor structure, and cost/duration/
risk - they are NOT the gate count and NOT the raw EIG order.

A typed deep-surrogate interface is provided but is NOT_IMPLEMENTED; the engine
always falls back to the direct estimator and never lets an unvalidated surrogate
control ordering.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.special import logsumexp

from .model import ParameterModel, ExperimentCandidate, enables_count

# documented ranking weights (forecast modelling choices, not measurements)
W_EIG, W_CRIT, W_FALS, W_DEP = 0.30, 0.30, 0.30, 0.10
# Cost/duration/risk modulate the score but must not dominate criticality and
# falsification; small exponents keep a factor-3 difficulty to a ~1.6x penalty.
COST_EXP, DUR_EXP, RISK_EXP = 0.20, 0.10, 0.15


# --- nested Monte-Carlo EIG ---------------------------------------------------

def eig_monte_carlo(pm: ParameterModel, *, n_outer: int, n_inner: int,
                    seed: int, n_batches: int = 8) -> dict:
    """Estimate EIG = I(theta; d) (nats) with a batched standard error.

    Uniform prior on [lo, hi] (transformed space); Gaussian measurement
    d = theta + N(0, sigma^2). Nested MC: marginal p(d) via inner prior samples.
    """
    a, b, sig = pm.prior_lo, pm.prior_hi, pm.meas_sigma
    rng = np.random.default_rng(seed)
    norm = math.log(sig * math.sqrt(2 * math.pi))
    batch_eig = np.empty(n_batches)
    for k in range(n_batches):
        th_o = rng.uniform(a, b, n_outer)
        d = th_o + rng.normal(0.0, sig, n_outer)
        th_i = rng.uniform(a, b, n_inner)
        ll_joint = -0.5 * ((d - th_o) / sig) ** 2 - norm
        diff = (d[:, None] - th_i[None, :]) / sig
        ll_inner = -0.5 * diff ** 2 - norm
        ll_marg = logsumexp(ll_inner, axis=1) - math.log(n_inner)
        batch_eig[k] = float(np.mean(ll_joint - ll_marg))
    eig = float(np.mean(batch_eig))
    se = float(np.std(batch_eig, ddof=1) / math.sqrt(n_batches)) if n_batches > 1 else float("nan")
    prior_entropy = math.log(b - a)  # differential entropy of the uniform prior
    return {
        "eig_nats": eig, "eig_se": se,
        "prior_entropy_nats": prior_entropy,
        "expected_posterior_entropy_nats": prior_entropy - eig,
        "n_outer": n_outer, "n_inner": n_inner, "n_batches": n_batches,
        "method": "nested_monte_carlo_mutual_information",
    }


def compute_all_eig(cands: list, *, n_outer: int, n_inner: int, seed: int,
                    n_batches: int = 8) -> dict:
    out = {}
    for i, c in enumerate(sorted(cands, key=lambda x: x.experiment_id)):
        # distinct, deterministic per-experiment seed
        out[c.experiment_id] = eig_monte_carlo(
            c.parameter, n_outer=n_outer, n_inner=n_inner, seed=seed + 1009 * (i + 1),
            n_batches=n_batches)
    return out


# --- ranking ------------------------------------------------------------------

def _score_components(c: ExperimentCandidate, eig_nats: float, max_eig: float,
                      enables: int, max_enables: int) -> dict:
    eig_n = eig_nats / max_eig if max_eig > 0 else 0.0
    dep_n = enables / max_enables if max_enables > 0 else 0.0
    benefit = (W_EIG * eig_n + W_CRIT * c.criticality + W_FALS * c.falsification_value
               + W_DEP * dep_n)
    penalty = (c.cost ** COST_EXP) * (c.duration ** DUR_EXP) * (c.risk ** RISK_EXP)
    return {
        "eig_component": round(W_EIG * eig_n, 4),
        "criticality_component": round(W_CRIT * c.criticality, 4),
        "falsification_component": round(W_FALS * c.falsification_value, 4),
        "dependency_component": round(W_DEP * dep_n, 4),
        "benefit": benefit, "cost_penalty": penalty,
        "score": benefit / penalty,
    }


def rank_experiments(cands: list, eigs: dict) -> list:
    enab = enables_count(cands)
    max_eig = max((eigs[c.experiment_id]["eig_nats"] for c in cands), default=1.0)
    max_enab = max(enab.values(), default=1)
    rows = []
    for c in cands:
        comp = _score_components(c, eigs[c.experiment_id]["eig_nats"], max_eig,
                                 enab[c.experiment_id], max_enab)
        rows.append({"experiment_id": c.experiment_id, "label": c.label, **comp,
                     "source": "direct_monte_carlo"})
    # deterministic: sort by score desc, tie-break by id
    rows.sort(key=lambda r: (-r["score"], r["experiment_id"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


# --- adaptive, outcome-dependent planning -------------------------------------

def adaptive_policy(cands: list, eigs: dict, ranking: list) -> dict:
    by_id = {c.experiment_id: c for c in cands}
    enab = enables_count(cands)
    max_eig = max((eigs[c.experiment_id]["eig_nats"] for c in cands), default=1.0)
    max_enab = max(enab.values(), default=1)
    top_id = ranking[0]["experiment_id"]
    top = by_id[top_id]
    remaining = [c for c in cands if c.experiment_id != top_id]

    def base_score(c):
        return _score_components(c, eigs[c.experiment_id]["eig_nats"], max_eig,
                                 enab[c.experiment_id], max_enab)["score"]

    outcomes = {}
    plans = [("advance", False)]
    if top.parameter.falsify_below_transformed is not None:
        plans.append(("kill", True))
    for label, is_kill in plans:
        scores = {}
        for c in remaining:
            s = base_score(c)
            if is_kill:
                # a falsifying outcome on a premise-level experiment changes priorities:
                # dependents on the top experiment lose value; the rest of the Mode-D
                # sensing chain is de-prioritised pending redesign.
                if top_id in c.predecessors:
                    s *= 0.10
                if top.falsification_value >= 0.85 and c.required_mode == "D":
                    s *= 0.30
            scores[c.experiment_id] = round(s, 5)
        nxt = max(scores, key=lambda k: (scores[k], k)) if scores else None
        outcomes[label] = {
            "outcome_meaning": ("measured value supports the forecast (advance)"
                                if not is_kill else
                                "measured value below the falsification threshold (kill/redesign)"),
            "recommended_next_experiment": nxt,
            "remaining_scores": scores,
        }
    return {
        "_note": "Outcome-dependent next-experiment selection. The recommended next "
                 "experiment may differ between outcomes. Forecast-only.",
        "first_experiment": top_id,
        "first_experiment_parameter": top.parameter.name,
        "outcomes": outcomes,
        "next_changes_with_outcome": (
            len({o["recommended_next_experiment"] for o in outcomes.values()}) > 1),
    }


# --- deep surrogate (typed interface; honestly NOT_IMPLEMENTED) ---------------

class DeepEIGSurrogate:
    """Typed interface for a neural EIG/posterior surrogate. NOT IMPLEMENTED.

    A genuine implementation would be simulation-trained with fixed seeds and
    separate train/val/test splits, report calibration + predictive error, and be
    compared against the direct MC estimator before being allowed to control
    ordering. None of that exists here, so this is explicitly NOT_IMPLEMENTED and
    the engine falls back to direct MC.
    """
    status = "NOT_IMPLEMENTED"

    def __init__(self):
        self.trained = False

    def train(self, *args, **kwargs):
        raise NotImplementedError("DeepEIGSurrogate.train: neural surrogate not implemented")

    def predict_eig(self, *args, **kwargs):
        raise NotImplementedError("DeepEIGSurrogate.predict_eig: neural surrogate not implemented")

    def is_trustworthy(self) -> bool:
        return False

    def readiness(self) -> dict:
        return {
            "status": self.status,
            "reason": "No neural surrogate or neural posterior/likelihood estimator "
                      "has been trained or validated. Honest fallback to direct MC.",
            "fallback": "direct_monte_carlo",
            "simulation_trained": False,
            "train_val_test_split": None,
            "predictive_error": None,
            "calibration": None,
            "compared_against_direct_mc": False,
            "controls_experiment_ordering": False,
            "trust_threshold_note": "Would require predictive error below a documented "
                                    "threshold vs direct MC before controlling ordering.",
        }


def select_estimator_source(surrogate: Optional[DeepEIGSurrogate]) -> str:
    """Use the surrogate only if it exists AND is trustworthy; else direct MC."""
    if surrogate is not None and surrogate.is_trustworthy():
        return "deep_surrogate"
    return "direct_monte_carlo"
