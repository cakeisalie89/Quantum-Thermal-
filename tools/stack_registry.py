"""Fail-closed validation of the stack registry, ``stack.json`` (Pydantic v2).

``stack.json`` is a hand-editable document the tests, the workflow and the
derived harness status read, which makes it a trusted boundary: a strict
model, extras forbidden, statuses from a closed vocabulary, validation that
fails rather than degrades. The rules worth enforcing are the ones a
careless edit would otherwise break silently:

* ``status`` is one of the declared rungs, and the rung is one the document
  itself defines in ``adoption_levels``;
* ``authority_effect`` is ``NONE``: no stack element decides authority, and a
  registry that says otherwise is rejected;
* element ids and ``doc_key`` anchors are unique, and every element names an
  authority, a boundary and a verification -- a row with no way to check it
  is not an adoption record;
* an element that is not settled (STAGED, DEFERRED) lists what is
  outstanding: "not adopted, nothing outstanding" is a contradiction;
* a RETIRED element says why and when, and is not also a live element.

The code remains the authority; ``STACK.md`` and ``stack.json`` mirror it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_FILENAME = "stack.json"
REGISTRY_SCHEMA_VERSION = "2.0.0"
LABEL = "SOFTWARE_STACK_REGISTRY: what is in use and how it is checked; " \
        "not evidence and not authority"

AdoptionStatus = Literal["ADOPTED", "STAGED", "DEFERRED", "RESOLVED"]
#: Settled rungs need not list open items; anything else must.
SETTLED = frozenset({"ADOPTED", "RESOLVED"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StackElement(_Strict):
    """One element: what it is, what governs it, and how it is checked."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    doc_key: str = Field(min_length=1)
    status: AdoptionStatus
    authority: str = Field(min_length=1)
    owner_module: str = Field(min_length=1)
    optional_dependency: str | None = None
    boundary: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    open_items: list[str]

    @model_validator(mode="after")
    def _unsettled_elements_must_say_what_is_outstanding(
            self) -> "StackElement":
        if self.status not in SETTLED and not self.open_items:
            raise ValueError(
                f"{self.id}: status {self.status} with no open items -- an "
                "element that is not settled must record what is "
                "outstanding")
        return self


class RetiredElement(_Strict):
    """An element that left the stack, and why. Kept so the registry's
    history is legible without the element being live."""

    id: str = Field(min_length=1)
    retired_at: str = Field(min_length=7)
    why: str = Field(min_length=1)


class StackRegistry(_Strict):
    """The whole stack, validated as one document."""

    schema_version: Literal["2.0.0"]
    label: str
    authority_effect: Literal["NONE"]
    owner: str = Field(min_length=1)
    adoption_levels: dict[str, str]
    invariants: list[str] = Field(min_length=1)
    elements: list[StackElement] = Field(min_length=1)
    retired: list[RetiredElement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_registry_invariants(self) -> "StackRegistry":
        if self.label != LABEL:
            raise ValueError(f"label must be {LABEL!r}")
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
        both = sorted(set(ids) & {r.id for r in self.retired})
        if both:
            raise ValueError(f"retired and live at once: {both}")
        return self

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


def registry_path(root: Path | None = None) -> Path:
    return (root if root is not None else ROOT) / REGISTRY_FILENAME


def load_registry(path: Path | None = None) -> StackRegistry:
    """Load and validate ``stack.json``; raises on any violation."""
    target = Path(path) if path is not None else registry_path()
    return StackRegistry.model_validate(json.loads(target.read_text()))
