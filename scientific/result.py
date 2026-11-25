"""The ResultBundle: the one thing that crosses from computation into evidence.

A bundle says which code ran (model id, version, implementation digest), on
what (parameter digest, environment digest, seeds, solver configuration), what
it produced (named outputs, each a Quantity with its unit, or explicitly
FAILED / UNDEFINED with a reason), how the computation behaved (convergence
diagnostics), which of the model's declared invariants held (computed from
this run's arrays, not asserted), what it warned about, and where the large
data went (artefacts, by content digest).

What it deliberately does NOT carry:

* arrays. A field of 200 x 120 temperatures belongs in an artefact file,
  hashed; a bundle that inlines one is refused, because a bundle is written
  into an event log and the log is not a data store;
* a verdict about itself. Whether the result is accepted is a
  VerificationResult produced by something else, and an authority decision
  made by someone else. A bundle records invariants the producer computed;
  it cannot mark itself verified;
* a measurement claim. Its observation kind is SIMULATION_RESULT, fixed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .identity import IdentityError, digest, is_digest, require_digest
from .observation import ObservationKind
from .quantity import Quantity

SCHEMA_VERSION = 1

#: The longest numeric list a bundle may carry inline. Diagnostics such as a
#: three-mesh convergence series fit; a field does not.
MAX_INLINE_NUMBERS = 32


class BundleError(ValueError):
    pass


class OutputStatus(str, Enum):
    OK = "OK"
    FAILED = "FAILED"
    UNDEFINED = "UNDEFINED"


@dataclass(frozen=True)
class Output:
    name: str
    status: OutputStatus
    quantity: Quantity | None = None
    reason: str = ""

    def __post_init__(self):
        object.__setattr__(self, "status", OutputStatus(self.status))
        if not isinstance(self.name, str) or not self.name.strip():
            raise BundleError("an output needs a name")
        if self.status is OutputStatus.OK:
            if not isinstance(self.quantity, Quantity):
                raise BundleError(f"output {self.name}: OK needs a Quantity")
        else:
            if self.quantity is not None:
                raise BundleError(f"output {self.name}: a {self.status.value} "
                                  "output carries no value")
            if not self.reason.strip():
                raise BundleError(f"output {self.name}: say why it is "
                                  f"{self.status.value}")

    def to_record(self) -> dict:
        return {"name": self.name, "status": self.status.value,
                "quantity": (self.quantity.to_record()
                             if self.quantity else None),
                "reason": self.reason}

    @classmethod
    def from_record(cls, rec) -> "Output":
        q = rec.get("quantity")
        return cls(name=rec["name"], status=rec["status"],
                   quantity=Quantity.from_record(q) if q else None,
                   reason=rec.get("reason", ""))


@dataclass(frozen=True)
class InvariantResult:
    invariant_id: str
    holds: bool
    measured: Quantity
    criterion: str
    #: the bound the measurement is compared with, in measured's unit.
    threshold: Quantity | None
    derivation: str

    def __post_init__(self):
        if not isinstance(self.holds, bool):
            raise BundleError("holds must be a bool")
        if not isinstance(self.measured, Quantity):
            raise BundleError(f"invariant {self.invariant_id}: the measured "
                              "value is a Quantity -- an invariant nobody "
                              "measured did not hold, it was not checked")
        if not self.criterion.strip() or not self.derivation.strip():
            raise BundleError(f"invariant {self.invariant_id}: state the "
                              "criterion and where it comes from")

    def to_record(self) -> dict:
        return {"invariant_id": self.invariant_id, "holds": self.holds,
                "measured": self.measured.to_record(),
                "criterion": self.criterion,
                "threshold": (self.threshold.to_record()
                              if self.threshold else None),
                "derivation": self.derivation}

    @classmethod
    def from_record(cls, rec) -> "InvariantResult":
        t = rec.get("threshold")
        return cls(invariant_id=rec["invariant_id"], holds=rec["holds"],
                   measured=Quantity.from_record(rec["measured"]),
                   criterion=rec["criterion"],
                   threshold=Quantity.from_record(t) if t else None,
                   derivation=rec["derivation"])


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    digest: str
    media_type: str
    size_bytes: int
    description: str = ""

    def __post_init__(self):
        require_digest(f"artifact {self.name}", self.digest)
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise BundleError(f"artifact {self.name}: size_bytes")

    def to_record(self) -> dict:
        return {"name": self.name, "digest": self.digest,
                "media_type": self.media_type,
                "size_bytes": self.size_bytes,
                "description": self.description}


def _check_inline(obj, where: str) -> None:
    """Refuse arrays and non-JSON floats anywhere in a free-form mapping."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise BundleError(f"{where}: non-string key {k!r}")
            _check_inline(v, f"{where}.{k}")
    elif isinstance(obj, (list, tuple)):
        nums = sum(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in obj)
        if nums > MAX_INLINE_NUMBERS:
            raise BundleError(
                f"{where}: {nums} numbers inline; arrays go in an artefact "
                "referenced by digest, not into the bundle")
        for i, v in enumerate(obj):
            _check_inline(v, f"{where}[{i}]")
    elif isinstance(obj, float) and not math.isfinite(obj):
        raise BundleError(f"{where}: {obj!r} is not a JSON number")
    elif not (obj is None or isinstance(obj, (str, int, float, bool))):
        raise BundleError(f"{where}: {type(obj).__name__} is not JSON")


@dataclass(frozen=True)
class ResultBundle:
    model_id: str
    model_version: str
    implementation_digest: str
    parameter_digest: str
    environment_digest: str
    parameters: dict
    outputs: tuple
    invariants: tuple
    convergence: dict
    seeds: tuple = ()
    solver_config: dict = field(default_factory=dict)
    warnings: tuple = ()
    artifacts: tuple = ()
    provenance: dict = field(default_factory=dict)
    observation_kind: ObservationKind = ObservationKind.SIMULATION_RESULT

    def __post_init__(self):
        if ObservationKind(self.observation_kind) is not \
                ObservationKind.SIMULATION_RESULT:
            raise BundleError("a model's result is a SIMULATION_RESULT; it "
                              "cannot be issued as any other kind")
        for name in ("implementation_digest", "parameter_digest",
                     "environment_digest"):
            try:
                require_digest(name, getattr(self, name))
            except IdentityError as exc:
                raise BundleError(str(exc)) from exc
        if not self.model_id or not self.model_version:
            raise BundleError("model id and version are required")
        if digest(self.parameters) != self.parameter_digest:
            raise BundleError("parameter_digest is not the digest of the "
                              "parameters this bundle carries")
        for coll, typ in ((self.outputs, Output),
                          (self.invariants, InvariantResult),
                          (self.artifacts, ArtifactRef)):
            if not isinstance(coll, tuple) or \
                    not all(isinstance(x, typ) for x in coll):
                raise BundleError(f"expected a tuple of {typ.__name__}")
        if not self.outputs:
            raise BundleError("a bundle with no outputs reports nothing")
        names = [o.name for o in self.outputs]
        if len(names) != len(set(names)):
            raise BundleError("output names repeat")
        ids = [i.invariant_id for i in self.invariants]
        if len(ids) != len(set(ids)):
            raise BundleError("invariant ids repeat")
        for key in ("parameters", "convergence", "solver_config",
                    "provenance"):
            _check_inline(getattr(self, key), key)
        _check_inline(list(self.seeds), "seeds")
        if not all(isinstance(w, str) for w in self.warnings):
            raise BundleError("warnings are strings")

    # -- reading ------------------------------------------------------------

    def output(self, name: str) -> Output:
        for o in self.outputs:
            if o.name == name:
                return o
        raise KeyError(name)

    def invariant(self, invariant_id: str) -> InvariantResult:
        for i in self.invariants:
            if i.invariant_id == invariant_id:
                return i
        raise KeyError(invariant_id)

    @property
    def all_invariants_hold(self) -> bool:
        return bool(self.invariants) and all(i.holds for i in self.invariants)

    # -- serialization ------------------------------------------------------

    def to_record(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "observation_kind": ObservationKind(self.observation_kind).value,
            "model_id": self.model_id, "model_version": self.model_version,
            "implementation_digest": self.implementation_digest,
            "parameter_digest": self.parameter_digest,
            "environment_digest": self.environment_digest,
            "parameters": self.parameters,
            "seeds": list(self.seeds),
            "solver_config": self.solver_config,
            "outputs": [o.to_record() for o in self.outputs],
            "invariants": [i.to_record() for i in self.invariants],
            "convergence": self.convergence,
            "warnings": list(self.warnings),
            "artifacts": [a.to_record() for a in self.artifacts],
            "provenance": self.provenance,
        }

    def digest(self) -> str:
        return digest(self.to_record())

    @classmethod
    def from_record(cls, rec: dict) -> "ResultBundle":
        if not isinstance(rec, dict):
            raise BundleError("a bundle record is a mapping")
        if rec.get("schema_version") != SCHEMA_VERSION:
            raise BundleError(f"schema_version {rec.get('schema_version')!r}"
                              f" is not {SCHEMA_VERSION}; refused rather "
                              "than read under a guess")
        expected = set(cls(**_minimal()).to_record())
        if set(rec) != expected:
            raise BundleError(f"bundle keys differ: extra "
                              f"{sorted(set(rec) - expected)}, missing "
                              f"{sorted(expected - set(rec))}")
        return cls(
            model_id=rec["model_id"], model_version=rec["model_version"],
            implementation_digest=rec["implementation_digest"],
            parameter_digest=rec["parameter_digest"],
            environment_digest=rec["environment_digest"],
            parameters=rec["parameters"], seeds=tuple(rec["seeds"]),
            solver_config=rec["solver_config"],
            outputs=tuple(Output.from_record(o) for o in rec["outputs"]),
            invariants=tuple(InvariantResult.from_record(i)
                             for i in rec["invariants"]),
            convergence=rec["convergence"],
            warnings=tuple(rec["warnings"]),
            artifacts=tuple(ArtifactRef(**a) for a in rec["artifacts"]),
            provenance=rec["provenance"],
            observation_kind=rec["observation_kind"])


def _minimal() -> dict:
    """The smallest valid bundle, used only to derive the key set."""
    z = "0" * 64
    return {"model_id": "m", "model_version": "0",
            "implementation_digest": z, "parameter_digest": digest({}),
            "environment_digest": z, "parameters": {},
            "outputs": (Output("x", OutputStatus.UNDEFINED, reason="-"),),
            "invariants": (), "convergence": {}}


__all__ = ["ArtifactRef", "BundleError", "InvariantResult", "Output",
           "OutputStatus", "ResultBundle", "is_digest"]
