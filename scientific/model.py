"""What a scientific model declares, and the one way it is run.

The contract between the agent and the numerics, and the whole of it: the
agent must not know how a model computes, and a model must not know there is
an agent. A ScientificModel declares

* ``model_id`` / ``model_version`` -- what it is;
* ``implementation_modules`` -- the code that computes it, from which
  ``implementation_digest()`` is taken, fail-closed (``identity``);
* ``parameter_schema`` -- typed inputs with units and bounds; anything else
  is refused, not ignored;
* ``invariants`` -- properties every run's own arrays must satisfy, each
  computed by ``run`` into the bundle;
* ``independent_checks`` -- checks it supports that are NOT a second call
  into its own implementation (run elsewhere; see ``verification``);
* ``applicability`` -- the domain in which its answers mean anything;

and ``run(inputs) -> ResultBundle``. ``run`` computes. It writes no
authority event, decides nothing about acceptance, and cannot mark its own
output verified: the bundle has no field for that.

``run_model`` is the one entry point, and it checks the model against its
own declarations: the bundle must name this model, this implementation
digest and these validated parameters, and must carry a result for EVERY
declared invariant -- an invariant that was declared and silently not
computed is refused rather than read as having held.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .identity import digest, digest_bytes, implementation_digest
from .result import ResultBundle
from .run_identity import RunIdentity, environment_digest


class ModelError(ValueError):
    pass


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: str                       # "float" | "int" | "str"
    unit: str = ""                  # required for float / int
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple = ()
    default: object = None
    description: str = ""

    def __post_init__(self):
        if self.kind not in ("float", "int", "str"):
            raise ModelError(f"{self.name}: kind {self.kind!r}")
        if self.kind in ("float", "int") and not self.unit:
            raise ModelError(f"{self.name}: a numeric parameter needs a unit "
                             "('1' when dimensionless)")

    def coerce(self, value):
        if self.kind == "str":
            if not isinstance(value, str):
                raise ModelError(f"{self.name}: expected str, got {value!r}")
        elif self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ModelError(f"{self.name}: expected int, got {value!r}")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ModelError(f"{self.name}: expected a number, got "
                                 f"{value!r}")
            value = float(value)
            if not math.isfinite(value):
                raise ModelError(f"{self.name}: {value!r} is not finite")
        if self.choices and value not in self.choices:
            raise ModelError(f"{self.name}: {value!r} not in "
                             f"{list(self.choices)}")
        if self.minimum is not None and value < self.minimum:
            raise ModelError(f"{self.name}: {value!r} < {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ModelError(f"{self.name}: {value!r} > {self.maximum}")
        return value


@dataclass(frozen=True)
class ParameterSchema:
    parameters: tuple

    def validate(self, inputs: dict) -> dict:
        """Typed, bounded, complete: unknown keys refused, defaults filled,
        a missing parameter without a default refused."""
        if not isinstance(inputs, dict):
            raise ModelError("inputs are a mapping")
        known = {p.name: p for p in self.parameters}
        unknown = set(inputs) - set(known)
        if unknown:
            raise ModelError(f"unknown parameters {sorted(unknown)}; a typo "
                             "would otherwise run the default silently")
        out = {}
        for name, p in known.items():
            if name in inputs:
                out[name] = p.coerce(inputs[name])
            elif p.default is not None:
                out[name] = p.coerce(p.default)
            else:
                raise ModelError(f"missing parameter {name}")
        return out

    def to_record(self) -> list:
        return [{"name": p.name, "kind": p.kind, "unit": p.unit,
                 "minimum": p.minimum, "maximum": p.maximum,
                 "choices": list(p.choices), "default": p.default,
                 "description": p.description} for p in self.parameters]


@dataclass(frozen=True)
class InvariantSpec:
    invariant_id: str
    description: str
    criterion: str


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    description: str
    independence: str
    shared_components: tuple = ()
    limitations: tuple = ()


@dataclass(frozen=True)
class Applicability:
    statement: str
    bounds: dict = field(default_factory=dict)
    excludes: tuple = ()


@runtime_checkable
class ScientificModel(Protocol):
    model_id: str
    model_version: str
    implementation_modules: tuple
    parameter_schema: ParameterSchema
    invariants: tuple
    independent_checks: tuple
    applicability: Applicability

    def implementation_digest(self) -> str: ...

    def validate(self, inputs: dict) -> dict: ...

    def run(self, inputs: dict) -> ResultBundle: ...


class ModelBase:
    """Declarations -> digest and validation. Subclasses set the class
    attributes and implement ``run``; nothing else is inherited."""

    model_id: str = ""
    model_version: str = ""
    implementation_modules: tuple = ()
    parameter_schema: ParameterSchema = ParameterSchema(())
    invariants: tuple = ()
    independent_checks: tuple = ()
    applicability: Applicability = Applicability("")

    def implementation_digest(self) -> str:
        return implementation_digest(self.implementation_modules)

    def validate(self, inputs: dict) -> dict:
        return self.parameter_schema.validate(inputs)

    def run(self, inputs: dict) -> ResultBundle:
        raise NotImplementedError


def run_identity_for(model: ScientificModel, params: dict,
                     seeds: tuple = ()) -> RunIdentity:
    """The identity of running ``model`` on already-validated ``params``
    here: the one definition both a bundle's provenance and a reuse check
    use, so the two cannot disagree about what "the same run" means."""
    return RunIdentity(
        model_id=model.model_id, model_version=model.model_version,
        implementation_digest=model.implementation_digest(),
        parameter_digest=digest(params),
        environment_digest=environment_digest(), seeds=tuple(seeds))


def run_model(model: ScientificModel, inputs: dict) -> ResultBundle:
    """Validate, run, and hold the bundle to the model's declarations."""
    return run_model_with_artifacts(model, inputs)[0]


def run_model_with_artifacts(model: ScientificModel, inputs: dict) -> tuple:
    """``(bundle, {artifact name: bytes})``, every artefact reference in the
    bundle matched by bytes of that digest and size, and nothing extra."""
    if not isinstance(model, ScientificModel):
        raise ModelError(f"{model!r} does not implement ScientificModel")
    params = model.validate(inputs)
    impl = model.implementation_digest()
    runner = getattr(model, "run_with_artifacts", None)
    bundle, payloads = runner(params) if runner else (model.run(params), {})
    if not isinstance(bundle, ResultBundle):
        raise ModelError("run() must return a ResultBundle")
    if (bundle.model_id, bundle.model_version) != (model.model_id,
                                                   model.model_version):
        raise ModelError(f"bundle names {bundle.model_id}@"
                         f"{bundle.model_version}, not the model that ran")
    if bundle.implementation_digest != impl:
        raise ModelError("bundle's implementation digest is not the "
                         "running code's")
    if bundle.parameter_digest != digest(params):
        raise ModelError("bundle's parameters are not the validated inputs")
    declared = {i.invariant_id for i in model.invariants}
    computed = {i.invariant_id for i in bundle.invariants}
    if declared - computed:
        raise ModelError(f"declared invariants not computed: "
                         f"{sorted(declared - computed)}")
    if computed - declared:
        raise ModelError(f"undeclared invariants reported: "
                         f"{sorted(computed - declared)}")
    refs = {a.name: a for a in bundle.artifacts}
    if set(refs) != set(payloads):
        raise ModelError(f"artefacts referenced {sorted(refs)} but bytes "
                         f"given for {sorted(payloads)}")
    for name, ref in refs.items():
        data = payloads[name]
        if digest_bytes(data) != ref.digest or len(data) != ref.size_bytes:
            raise ModelError(f"artefact {name}: the bytes are not the ones "
                             "the bundle's reference names")
    return bundle, payloads
