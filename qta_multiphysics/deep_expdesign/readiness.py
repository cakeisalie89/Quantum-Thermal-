"""Readiness state machine for the deep layer (fail-closed).

Computes the readiness state from concrete evidence (dependency availability,
dataset presence, training result, validation metrics vs trust thresholds, OOD
status). The system fails closed: any missing/failed evidence yields a state in
which the surrogate does NOT control ordering and the engine falls back to direct
Monte Carlo.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import DeepConfig, BACKEND, BACKEND_REASON, SCHEMA_VERSION
from .schemas import ReadinessState


@dataclass
class Evidence:
    dependency_ok: bool = True          # numpy/scipy backend available
    dataset_generated: bool = False
    trained: bool = False
    validation_ran: bool = False
    thresholds_passed: Optional[bool] = None   # None until validation runs
    ood_triggered: bool = False
    notes: str = ""


def compute_state(ev: Evidence) -> str:
    """Map evidence to exactly one readiness state (fail-closed)."""
    if not ev.dependency_ok:
        return ReadinessState.NOT_IMPLEMENTED
    if ev.ood_triggered:
        return ReadinessState.OUT_OF_DISTRIBUTION
    if not ev.trained:
        return (ReadinessState.DATASET_GENERATED if ev.dataset_generated
                else ReadinessState.NOT_IMPLEMENTED)
    if not ev.validation_ran:
        return ReadinessState.TRAINED_NOT_VALIDATED
    if ev.thresholds_passed is True:
        return ReadinessState.VALIDATED_SURROGATE
    return ReadinessState.TRAINED_NOT_TRUSTED


def readiness_record(ev: Evidence, cfg: DeepConfig,
                     metrics: Optional[dict] = None) -> dict:
    """Build the deterministic deep_surrogate_readiness payload (fail-closed)."""
    state = compute_state(ev)
    controls = ReadinessState.controls_ordering(state)
    fallback = "direct_monte_carlo"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": state,
        "controls_experiment_ordering": bool(controls),
        "fallback": fallback,
        "backend": BACKEND,
        "backend_reason": BACKEND_REASON,
        "dependency_ok": ev.dependency_ok,
        "dataset_generated": ev.dataset_generated,
        "trained": ev.trained,
        "validation_ran": ev.validation_ran,
        "thresholds_passed": ev.thresholds_passed,
        "ood_triggered": ev.ood_triggered,
        "trust_thresholds": cfg.thresholds.as_dict(),
        "validation_metrics": metrics or {},
        "notes": ev.notes,
        "forecast_only": True,
        "measured_in_this_system": False,
        "reference_estimator": "direct_nested_monte_carlo (qta_multiphysics.expdesign)",
        "claim_boundary": ("Numerical surrogate of a simulator; not experimental "
                           "validation of physics. No physical gate is created or "
                           "upgraded."),
    }
