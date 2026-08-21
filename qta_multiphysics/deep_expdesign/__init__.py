"""Deep (simulation-based) experimental-design layer for QTA (forecast-only).

Optional accelerated inference / EIG backend. The direct nested-Monte-Carlo EIG
in ``qta_multiphysics.expdesign`` remains the canonical reference and validation
authority. Fail-closed: a learned surrogate may influence experiment ordering
only after passing explicit calibration, predictive-error, reproducibility, and
out-of-distribution checks against the direct reference. Nothing here is a
measurement; no physical gate can be created or upgraded.
"""
from __future__ import annotations

from .config import (DeepConfig, TrustThresholds, ci_config, standard_config,
                     heavy_config, config_for, SCHEMA_VERSION, BACKEND)
from .schemas import (ReadinessState, LatentParameter, DesignVariable,
                      SimulationResponse, DatasetSummary, MeasurementRecord)
from .parameter_space import (build_latent_parameters, trainable_subspace, sample_prior)
from .design_space import (ExperimentDesign, design_variables, validate_design,
                           is_valid, validate_sequence, load_design_families,
                           load_interlocks)
from .simulator_adapter import simulate, clean_forward, observable_names, meas_sigma
from .dataset import generate_dataset, summarize, splits_disjoint, Dataset
from .readiness import Evidence, compute_state, readiness_record
from . import likelihood_model
from .posterior_model import MDNPosterior
from .trainer import build_amortized_dataset, train_posterior, TrainedPosterior, AmortizedDataset
from .eig_surrogate import learned_eig, learned_eig_batch
from .validation import (direct_mc_eig, validate_against_direct_mc, benchmark_designs,
                         ValidationResult)
from .calibration import run_sbc, CalibrationResult
from .ood import fit_ood, ood_report, OODModel
from .inference import (posterior_query, posterior_predictive,
                        trusted_posterior_query, trust_state,
                        SurrogateAuthorityDenied, TRUST_REQUIREMENTS,
                        TRUSTED, DENIED_OOD, DENIED_NOT_READY)
from .policy import rank_candidates, update_after_observation, Candidate
from .runner import run_deep_expdesign, run_deep_expdesign_full

__all__ = [
    "DeepConfig", "TrustThresholds", "ci_config", "standard_config", "heavy_config",
    "config_for", "SCHEMA_VERSION", "BACKEND", "ReadinessState", "LatentParameter",
    "DesignVariable", "SimulationResponse", "DatasetSummary", "MeasurementRecord",
    "build_latent_parameters", "trainable_subspace", "sample_prior", "ExperimentDesign",
    "design_variables", "validate_design", "is_valid", "validate_sequence",
    "load_design_families", "load_interlocks", "simulate", "clean_forward",
    "observable_names", "meas_sigma", "generate_dataset", "summarize", "splits_disjoint",
    "Dataset", "Evidence", "compute_state", "readiness_record", "likelihood_model",
    "MDNPosterior", "build_amortized_dataset", "train_posterior", "TrainedPosterior",
    "AmortizedDataset", "learned_eig", "learned_eig_batch", "direct_mc_eig",
    "validate_against_direct_mc", "benchmark_designs", "ValidationResult", "run_sbc",
    "CalibrationResult", "fit_ood", "ood_report", "OODModel", "posterior_query",
    "trusted_posterior_query", "trust_state", "SurrogateAuthorityDenied",
    "TRUST_REQUIREMENTS", "TRUSTED", "DENIED_OOD", "DENIED_NOT_READY",
    "posterior_predictive", "rank_candidates", "update_after_observation", "Candidate",
    "run_deep_expdesign", "run_deep_expdesign_full",
]
