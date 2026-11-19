"""Model-facing tool contracts (Gate F): what a research agent is shown of a
model it may ask to run.

A contract is derived, never written: from the model's own declarations in
the catalog -- its applicability (the assumptions it is valid under, its
bounds, what it excludes), its parameter schema (name, kind, unit, bounds,
default), its invariants, the independent check that will be run on any
result, and that check's own statement of how independent it is, what it
shares with the model and what it cannot show. Nothing here is the model's
account of itself beyond what it declares to the governed path, and nothing
in a contract is evidence: a run is decided by the check and the reviewer.

The contract also says how many dimensional statements hold the model's
equations to its declared units (Assurance H); zero would mean UNCHECKED.
"""
from __future__ import annotations

from . import catalog
from .dimensional_statements import STATEMENTS
from .identity import digest

SCHEMA = "qt-model-contract/1"


class ContractError(ValueError):
    """A contract for a model or a check the catalog does not admit."""


def contract(model_id: str, check_id: str) -> dict:
    """The contract for running ``model_id``, checked by ``check_id``."""
    reg = catalog.models()
    versions = [v for m, v, _ in reg.entries() if m == model_id]
    if len(versions) != 1:
        raise ContractError(f"{model_id!r} is admitted at {len(versions)} "
                            "versions; a contract names exactly one")
    model = reg.lookup(model_id, versions[0])
    spec = next((c for c in model.independent_checks
                 if c.check_id == check_id), None)
    if spec is None:
        raise ContractError(f"{model_id} declares no independent check "
                            f"{check_id!r}")
    try:
        catalog.check(check_id)
    except KeyError as exc:
        raise ContractError(str(exc)) from exc
    app = model.applicability
    body = {
        "schema": SCHEMA,
        "model_id": model.model_id,
        "model_version": model.model_version,
        "implementation_digest": model.implementation_digest(),
        "applicability": {"statement": app.statement,
                          "bounds": dict(app.bounds),
                          "excludes": list(app.excludes)},
        "parameters": model.parameter_schema.to_record(),
        "invariants": [{"id": i.invariant_id, "statement": i.description,
                        "criterion": i.criterion}
                       for i in model.invariants],
        "independent_check": {
            "check_id": spec.check_id, "method": spec.description,
            "independence": spec.independence,
            "shared_components": list(spec.shared_components),
            "limitations": list(spec.limitations)},
        "dimensional_statements": len(STATEMENTS.get(model.model_id, ())),
        "decided_by": "the independent check and the governed reviewer, "
                      "never the requester's account of the run",
    }
    return {**body, "digest": digest(body)}


def contracts(checks: dict) -> list:
    """Every contract for ``checks`` (model id -> check id), in model-id
    order: what a deployment offers the research loop."""
    return [contract(m, c) for m, c in sorted(checks.items())]
