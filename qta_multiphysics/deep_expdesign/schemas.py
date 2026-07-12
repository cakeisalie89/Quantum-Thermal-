"""Typed schemas and the readiness state machine for the deep layer.

Everything the deep layer emits is described by a dataclass here with an
explicit ``SCHEMA_VERSION``. Provenance / claim-boundary fields
(``forecast_only``, ``measured_in_this_system``, ``simulation_generated``) are
first-class so no output can silently drop them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .config import SCHEMA_VERSION

# Mandatory provenance labels for any simulation-derived artifact.
SIMULATION_LABELS = {
    "simulation_generated": True,
    "forecast_only": True,
    "not_measured_in_this_system": True,
    "measured_in_this_system": False,
}


class ReadinessState:
    """Allowed readiness states. The system must *fail closed*: anything other
    than ``VALIDATED_SURROGATE`` means the surrogate does NOT control ordering."""
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    DATASET_GENERATED = "DATASET_GENERATED"
    TRAINED_NOT_VALIDATED = "TRAINED_NOT_VALIDATED"
    TRAINED_NOT_TRUSTED = "TRAINED_NOT_TRUSTED"
    VALIDATED_SURROGATE = "VALIDATED_SURROGATE"
    OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
    FALLBACK_DIRECT_MC = "FALLBACK_DIRECT_MC"

    ALL = (NOT_IMPLEMENTED, DATASET_GENERATED, TRAINED_NOT_VALIDATED,
           TRAINED_NOT_TRUSTED, VALIDATED_SURROGATE, OUT_OF_DISTRIBUTION,
           FALLBACK_DIRECT_MC)

    # Only this state permits the surrogate to control experiment ordering.
    TRUSTED = VALIDATED_SURROGATE

    @classmethod
    def controls_ordering(cls, state: str) -> bool:
        return state == cls.TRUSTED


@dataclass
class LatentParameter:
    """One coordinate of the joint latent physics vector."""
    name: str
    unit: str
    transform: str                 # 'linear' | 'log10'
    prior_family: str              # 'uniform' | 'none'
    prior_parameters: dict         # e.g. {"low":..., "high":...} in transformed space
    evidence_status: str           # 'UNKNOWN' | 'ASSUMED' | 'MEASURED_LITERATURE'
    source: str
    validity_range: Optional[list] # [lo, hi] in natural units, or None
    is_nuisance: bool
    is_trainable: bool
    source_required: bool = False  # True when no defensible range exists

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DesignVariable:
    """One coordinate of an experiment-design vector."""
    name: str
    kind: str                      # 'continuous' | 'categorical' | 'mode' | 'meta'
    unit: str
    domain: Optional[list]         # [lo, hi] or list of categories
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimulationResponse:
    """Strict response schema from the simulator adapter."""
    simulation_id: str
    parameter_vector: dict
    design_vector: dict
    observables: dict
    success: bool
    failure_reason: Optional[str]
    numerical_profile: dict
    seed: int
    source_commit: str
    forecast_only: bool = True
    measured_in_this_system: bool = False
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetSummary:
    """Compact, commit-safe dataset descriptor (no bulk arrays)."""
    schema_version: str
    n_total: int
    n_success: int
    n_failure: int
    n_train: int
    n_val: int
    n_test: int
    latent_names: list
    design_names: list
    observable_names: list
    sampler: str
    master_seed: int
    source_commit: str
    dataset_sha256: str
    split_disjoint: bool
    labels: dict = field(default_factory=lambda: dict(SIMULATION_LABELS))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MeasurementRecord:
    """Hardware-measurement ingestion schema. NO measured data exists in-repo;
    posterior updates from such records require ``data_source==MEASURED_IN_SYSTEM``
    and every required field present (incomplete records are rejected)."""
    measurement_id: str
    timestamp: str
    instrument: str
    calibration_record: str
    experiment_design: dict
    raw_data_reference: str
    processed_observable: dict
    uncertainty: dict
    systematic_uncertainty: dict
    operator: str
    temperature: float
    field: float
    coverage: float
    mode: str
    quality_flags: list
    data_source: str = "UNVERIFIED"   # must be set MEASURED_IN_SYSTEM to be used

    REQUIRED = ("measurement_id", "timestamp", "instrument", "calibration_record",
                "experiment_design", "raw_data_reference", "processed_observable",
                "uncertainty", "operator", "mode")

    def is_complete(self) -> bool:
        for f in self.REQUIRED:
            v = getattr(self, f, None)
            if v is None or (isinstance(v, (str, dict, list)) and len(v) == 0):
                return False
        return True

    def is_usable(self) -> bool:
        return self.is_complete() and self.data_source == "MEASURED_IN_SYSTEM"

    def to_dict(self) -> dict:
        return asdict(self)
