"""Fail-closed validation of the Stage-10 adoption registry (Pydantic v2).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

``stack.json`` is a hand-editable document that the tests and the workflow
read, which makes it a trusted boundary in exactly the sense
``stage7_boundary_models.py`` uses the term — so it gets the same treatment:
a strict model, extras forbidden, statuses drawn from a closed vocabulary, and
validation that fails rather than degrades.

The rules that are worth enforcing here are the ones a careless edit would
otherwise break silently:

* ``status`` must be one of the three declared rungs, and the rung must be one
  the document itself defines in ``adoption_levels`` — a registry cannot
  invent a level in one place and not the other.
* ``automatic_gate_effect`` must be ``NONE`` and ``label`` must be the
  project's claim-boundary label, verbatim. A registry that quietly drops
  either is rejected.
* Element ids and ``doc_key`` anchors must be unique, and every element needs
  an authority, a boundary, and a verification command — a row with no way to
  check it is not an adoption record.
* A STAGED or DEFERRED element must list at least one open item. "Not adopted,
  nothing outstanding" is a contradiction, and catching it here is what keeps
  the ladder honest as it changes.

This module creates no competing authority: ``STACK.md`` and ``stack.json``
mirror the code, and the code remains the authority.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import AUTOMATIC_GATE_EFFECT, LABEL, STACK_STAGE
from .workspace import StrPath, repo_root

REGISTRY_FILENAME = "stack.json"
REGISTRY_SCHEMA_VERSION = "1.0.0"
#: ADOPTED_ADMISSION_MECHANISM_ONLY exists because "ADOPTED" was doing two
#: jobs. Selective Rust was listed ADOPTED on the strength of its bit-parity
#: admission rule being exercised and verified, which reads as an active Rust
#: backend -- while rust_kernel.py's own status record says no solver imports
#: the kernels. The governing rule and the tool it governs now have distinct
#: adoption states.
AdoptionStatus = Literal["ADOPTED", "ADOPTED_ADMISSION_MECHANISM_ONLY",
                         "STAGED", "DEFERRED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StackElement(_Strict):
    """One rung entry: what the element is, and how its claim is checked."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    doc_key: str = Field(min_length=1)
    status: AdoptionStatus
    since_stage: str = Field(min_length=1)
    authority: str = Field(min_length=1)
    owner_module: str = Field(min_length=1)
    optional_dependency: str | None = None
    boundary: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    open_items: list[str]

    @model_validator(mode="after")
    def _unadopted_elements_must_say_what_is_outstanding(self
                                                         ) -> "StackElement":
        if self.status != "ADOPTED" and not self.open_items:
            raise ValueError(
                f"{self.id}: status {self.status} with no open items — an "
                "element that is not adopted must record what is outstanding")
        return self


class StackRegistry(_Strict):
    """The whole adoption ladder, validated as one document."""

    schema_version: Literal["1.0.0"]
    stage: str = Field(min_length=1)
    label: str
    automatic_gate_effect: Literal["NONE"]
    owner: str = Field(min_length=1)
    adoption_levels: dict[str, str]
    invariants: list[str] = Field(min_length=1)
    elements: list[StackElement] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_registry_invariants(self) -> "StackRegistry":
        if self.label != LABEL:
            raise ValueError(
                f"label must be the project claim-boundary label {LABEL!r}")
        if self.automatic_gate_effect != AUTOMATIC_GATE_EFFECT:
            raise ValueError("automatic_gate_effect must be NONE")
        if self.stage != STACK_STAGE:
            raise ValueError(f"stage must be {STACK_STAGE!r}")
        ids = [e.id for e in self.elements]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate element ids")
        keys = [e.doc_key for e in self.elements]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate doc_key anchors")
        undefined = sorted({e.status for e in self.elements}
                           - set(self.adoption_levels))
        if undefined:
            raise ValueError(
                f"statuses not defined in adoption_levels: {undefined}")
        return self

    # ---- convenience accessors used by the tests and the workflow ----
    def by_id(self, element_id: str) -> StackElement:
        for element in self.elements:
            if element.id == element_id:
                return element
        raise KeyError(element_id)

    def with_status(self, status: AdoptionStatus) -> list[StackElement]:
        return [e for e in self.elements if e.status == status]

    def open_items(self) -> dict[str, list[str]]:
        return {e.id: list(e.open_items) for e in self.elements
                if e.open_items}


def registry_path(root: StrPath | None = None) -> Path:
    return (Path(root) if root is not None else repo_root()) / REGISTRY_FILENAME


def load_registry(path: StrPath | None = None) -> StackRegistry:
    """Load and validate ``stack.json``; raises on any violation."""
    target = Path(path) if path is not None else registry_path()
    return StackRegistry.model_validate(json.loads(target.read_text()))
