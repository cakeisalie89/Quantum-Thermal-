"""QTA Bayesian experimental-design layer (forecast/model-only).

Direct expected-information-gain (mutual information) engine over the candidate
experiments parsed from FIRST_VALIDATION_EXPERIMENTS.md and reconciled with the
gate table, kill-gate ranking, and unknown parameters. Includes adaptive,
outcome-dependent next-experiment selection and a typed deep-surrogate interface
that is honestly NOT_IMPLEMENTED (automatic fallback to the direct estimator).
"""
from __future__ import annotations

from .model import (
    ParameterModel, ExperimentCandidate, load_candidates, enables_count,
)
from .engine import (
    eig_monte_carlo, compute_all_eig, rank_experiments, adaptive_policy,
    DeepEIGSurrogate, select_estimator_source,
)
from .runner import run_expdesign_layer, ci_run, standard_run

__all__ = [
    "ParameterModel", "ExperimentCandidate", "load_candidates", "enables_count",
    "eig_monte_carlo", "compute_all_eig", "rank_experiments", "adaptive_policy",
    "DeepEIGSurrogate", "select_estimator_source",
    "run_expdesign_layer", "ci_run", "standard_run",
]
