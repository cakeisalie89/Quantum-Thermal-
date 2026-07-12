"""Posterior-conditioned adaptive experiment-selection policy.

Generalises the previous binary advance/kill policy into a utility ranking that
combines expected information gain with gate criticality, falsification value,
dependency value, and forecast cost/duration/risk. Every utility component is
exposed (the ranking is never hidden inside the model), and the selected
experiment is accompanied by an explanation.

EIG is sourced from the deep surrogate ONLY when it is a trusted
``VALIDATED_SURROGATE``; otherwise the direct nested-Monte-Carlo EIG is used
(fail-closed). The posterior conditions the policy by down-weighting information
gain for parameters already well constrained by prior observations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# transparent, fixed utility weights (exposed in the output)
_WEIGHTS = {"eig": 0.35, "criticality": 0.25, "falsification": 0.20,
            "dependency": 0.10, "cost": 0.05, "duration": 0.03, "risk": 0.02}


@dataclass
class Candidate:
    experiment_id: str
    eig: float
    criticality: float = 0.5
    falsification: float = 0.5
    dependency: float = 0.5
    cost: float = 1.0
    duration: float = 1.0
    risk: float = 1.0


def _norm(vals: np.ndarray) -> np.ndarray:
    lo, hi = float(np.min(vals)), float(np.max(vals))
    if hi - lo < 1e-12:
        return np.full_like(vals, 0.5)
    return (vals - lo) / (hi - lo)


def rank_candidates(candidates: list, *, eig_source: str,
                    controls_ordering: bool) -> dict:
    """Rank candidates by transparent utility; return full component breakdown."""
    eig = _norm(np.array([c.eig for c in candidates]))
    crit = np.array([c.criticality for c in candidates])
    fals = np.array([c.falsification for c in candidates])
    dep = np.array([c.dependency for c in candidates])
    cost = _norm(np.array([c.cost for c in candidates]))
    dur = _norm(np.array([c.duration for c in candidates]))
    risk = _norm(np.array([c.risk for c in candidates]))
    w = _WEIGHTS
    utility = (w["eig"] * eig + w["criticality"] * crit + w["falsification"] * fals
               + w["dependency"] * dep - w["cost"] * cost - w["duration"] * dur
               - w["risk"] * risk)
    order = np.argsort(-utility)
    ranked = []
    for rank, i in enumerate(order):
        ranked.append({
            "rank": rank + 1, "experiment_id": candidates[i].experiment_id,
            "utility": float(utility[i]),
            "components": {
                "eig_normalized": float(eig[i]), "eig_raw": float(candidates[i].eig),
                "criticality": float(crit[i]), "falsification": float(fals[i]),
                "dependency": float(dep[i]), "cost_normalized": float(cost[i]),
                "duration_normalized": float(dur[i]), "risk_normalized": float(risk[i]),
            }})
    top = candidates[order[0]]
    explanation = (
        f"Selected {top.experiment_id}: highest combined utility "
        f"({utility[order[0]]:.3f}). EIG source = {eig_source} "
        f"({'trusted surrogate' if controls_ordering else 'direct Monte Carlo (deep layer untrusted)'}). "
        f"Dominant terms: EIG {w['eig']*eig[order[0]]:.3f}, "
        f"criticality {w['criticality']*crit[order[0]]:.3f}, "
        f"falsification {w['falsification']*fals[order[0]]:.3f}.")
    return {"weights": dict(w), "eig_source": eig_source,
            "controls_experiment_ordering": bool(controls_ordering),
            "ranking": ranked, "selected": top.experiment_id,
            "explanation": explanation}


def update_after_observation(candidates: list, constrained_gain: dict) -> list:
    """Down-weight EIG for experiments targeting already-constrained parameters.

    ``constrained_gain`` maps experiment_id -> multiplicative factor in [0,1]
    reflecting how much the posterior (after prior observations) has reduced the
    remaining information available from that experiment.
    """
    out = []
    for c in candidates:
        f = float(constrained_gain.get(c.experiment_id, 1.0))
        out.append(Candidate(c.experiment_id, c.eig * f, c.criticality,
                             c.falsification, c.dependency, c.cost, c.duration, c.risk))
    return out
