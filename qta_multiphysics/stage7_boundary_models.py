"""Stage-7 trusted-boundary validation models (Pydantic v2).

MODEL-ONLY / FORECAST-ONLY. These models validate identifiers, statuses,
configuration and provenance records at trusted boundaries. They create
no competing authority: canonical vocabularies are loaded from the
project's authoritative files, and matrix-update requests are ALSO passed
through the existing Stage-5 validator (`validate_matrix_update_request`),
which remains the governance authority. No model here touches scientific
state, gates, parameters, or canonical outputs.

Fail-closed rules: unknown identifiers reject; NaN/infinity reject where
physically invalid; negatives reject for non-negative quantities; UNKNOWN
stays UNKNOWN (never coerced to zero); missing measured values never
become assumed values; ``automatic_application`` must be False;
requesters cannot review their own requests.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Literal, Optional

from pydantic import (BaseModel, ConfigDict, Field, field_validator,
                      model_validator)

MODES = ("MODE_A", "MODE_B", "MODE_C", "MODE_D")
SPECIES = ("C13_CH4", "He3", "He4", "H2_residual")
GATE_STATUSES = ("CONDITIONAL", "BLOCKED", "UNKNOWN", "DERIVED_CHECK",
                 "PASS")   # vocabulary; the PASS *count* is asserted zero
EXPERIMENT_STATUSES = ("DESIGNED", "PLAYBOOK_READY",
                       "BLOCKED_BY_DEPENDENCY")
_SPECIES_MODE = {"C13_CH4": "MODE_B", "He3": "MODE_D", "He4": "MODE_D"}


def _registry_ids() -> set:
    reg = json.loads(Path("experiment_registry.json").read_text())
    return {e["experiment_id"] for e in reg["experiments"]}


def _gate_ids() -> set:
    return {r["gate_id"] for r in
            csv.DictReader(open("results_gate_table.csv"))}


def _matrix_keys() -> set:
    return {r["item"] for r in csv.DictReader(open("validation_matrix.csv"))}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModeId(_Strict):
    mode: Literal["MODE_A", "MODE_B", "MODE_C", "MODE_D"]


class DeviceStateId(_Strict):
    mode: Literal["MODE_A", "MODE_B", "MODE_C", "MODE_D"]
    device_state: str = Field(min_length=1)


class SpeciesAssignment(_Strict):
    """Active-species assignment; enforces canonical separation."""
    species: Literal["C13_CH4", "He3", "He4"]
    mode: Literal["MODE_A", "MODE_B", "MODE_C", "MODE_D"]
    active: bool

    @model_validator(mode="after")
    def _separation(self) -> "SpeciesAssignment":
        if self.active and _SPECIES_MODE[self.species] != self.mode:
            raise ValueError(
                f"{self.species} may only be active in "
                f"{_SPECIES_MODE[self.species]} (canonical separation)")
        return self


class SimultaneityGuard(_Strict):
    """Processing and sensing can never be simultaneously active."""
    mode_b_processing_active: bool
    mode_d_sensing_active: bool

    @model_validator(mode="after")
    def _exclusive(self) -> "SimultaneityGuard":
        if self.mode_b_processing_active and self.mode_d_sensing_active:
            raise ValueError("simultaneous Mode-B processing and Mode-D "
                             "sensing is forbidden")
        return self


class GateStatus(_Strict):
    gate_id: str
    status: Literal["CONDITIONAL", "BLOCKED", "UNKNOWN", "DERIVED_CHECK",
                    "PASS"]

    @field_validator("gate_id")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in _gate_ids():
            raise ValueError(f"unknown gate_id '{v}' (fail-closed)")
        return v


class ExperimentStatus(_Strict):
    experiment_id: str
    status: Literal["DESIGNED", "PLAYBOOK_READY", "BLOCKED_BY_DEPENDENCY"]

    @field_validator("experiment_id")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in _registry_ids():
            raise ValueError(f"unknown experiment_id '{v}' (fail-closed)")
        return v


class NonNegativeQuantity(_Strict):
    """Physical scalar with unit; rejects NaN/inf/negative."""
    name: str
    value: float
    unit: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _finite_nonneg(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN/infinity rejected (physically invalid)")
        if v < 0:
            raise ValueError("negative value rejected for non-negative "
                             "quantity")
        return v


class BoundedScalar(_Strict):
    """Range-checked physical scalar (e.g. temperatures, pressures)."""
    name: str
    value: float
    unit: str
    lower: float
    upper: float

    @model_validator(mode="after")
    def _in_range(self) -> "BoundedScalar":
        v = self.value
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN/infinity rejected")
        if not (self.lower <= v <= self.upper):
            raise ValueError(f"{self.name}={v} outside "
                             f"[{self.lower}, {self.upper}] {self.unit}")
        return self


class UnknownOrValue(_Strict):
    """UNKNOWN is preserved as UNKNOWN — never coerced to zero."""
    value: Optional[float] = None
    status: Literal["KNOWN", "UNKNOWN"]

    @model_validator(mode="after")
    def _consistent(self) -> "UnknownOrValue":
        if self.status == "UNKNOWN" and self.value is not None:
            raise ValueError("UNKNOWN must not carry a numeric value")
        if self.status == "KNOWN":
            if self.value is None:
                raise ValueError("KNOWN requires a value")
            if math.isnan(self.value) or math.isinf(self.value):
                raise ValueError("NaN/infinity rejected")
        return self


class MatrixUpdateRequestModel(_Strict):
    """Mirrors the Stage-6 JSON schema; the Stage-5 validator remains the
    governance authority — call ``validated_by_governance()``."""
    request_id: str
    item: str
    current_status: str
    proposed_status: str
    experiment_ids: list
    dossier_refs: list
    raw_data_refs: list
    calibration_refs: list
    uncertainty_refs: list
    review_ids: list
    requester: str
    request_date: str
    rationale: str
    claim_boundary_confirmation: str
    automatic_application: Literal[False]
    schema_version: str

    @model_validator(mode="after")
    def _rules(self) -> "MatrixUpdateRequestModel":
        if self.requester in self.review_ids:
            raise ValueError("requester may not be a reviewer")
        if self.item not in _matrix_keys():
            raise ValueError(f"unknown matrix item '{self.item}'")
        unknown = [e for e in self.experiment_ids
                   if e not in _registry_ids()]
        if unknown:
            raise ValueError(f"unknown experiment_ids {unknown}")
        return self

    def validated_by_governance(self) -> tuple:
        from qta_multiphysics.hardware_governance_3d import (
            validate_matrix_update_request)
        return validate_matrix_update_request(self.model_dump())


class PathConfig(_Strict):
    source_root: str
    workspace: str

    @model_validator(mode="after")
    def _distinct(self) -> "PathConfig":
        if Path(self.source_root).resolve() == \
                Path(self.workspace).resolve():
            raise ValueError("workspace must not equal the source root")
        return self


class WorkflowConfig(_Strict):
    sim_command: str = "python3 qta_full_sim.py"
    checker_command: str = "python3 package_consistency_check.py"
    manuscript_command: str = "python3 manuscript_consistency_check.py"
    governed_output_exempt: tuple = ("deep_surrogate_readiness.json",)


class OutputMetadata(_Strict):
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)


class EnvironmentMetadata(_Strict):
    python_version: str
    platform: str
    numpy: str
    scipy: str
    qutip: str
    locale: str
    timezone: str


class DependencyProvenance(_Strict):
    package: str
    version: str
    dependency_class: Literal["runtime", "dev", "workflow"]
    reason: str
    behavior_sensitive: bool
    version_authority: str


class ReproducibilityRunRecord(_Strict):
    path_name: Literal["direct", "snakemake", "container"]
    command: str
    exit_status: int
    output_count: int
    matches: int
    mismatches: int
    note: str = ""


class ManifestVerificationResult(_Strict):
    entries: int
    mismatches: int
    detached_hash_ok: bool

    @model_validator(mode="after")
    def _clean(self) -> "ManifestVerificationResult":
        if self.mismatches != 0 or not self.detached_hash_ok:
            raise ValueError("manifest verification must be clean")
        return self
