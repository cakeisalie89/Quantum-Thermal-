"""What kind of thing a number is: measured, simulated, or derived.

"A simulation must never silently become a measurement" is enforced as a
type rule, not a naming convention. An Observation's kind is fixed when it
is made; there is no relabelling. A derived observation's kind is checked
against the kinds of everything it was derived from, so the lineage decides
what it may claim:

* RAW_OBSERVATION is never derived from anything;
* PROCESSED_OBSERVATION comes only from measured inputs (RAW or PROCESSED);
* anything with a SIMULATION_RESULT or SYNTHETIC_OBSERVATION anywhere among
  its inputs can be a SIMULATION_RESULT, a SYNTHETIC_OBSERVATION (a
  simulated measurement) or a DERIVED_STATISTIC -- never RAW or PROCESSED;
* a CALIBRATED_PARAMETER must cite at least one measured input and say how
  the calibration was done: a parameter fitted only to simulations is a
  simulation output, however it is labelled;
* ``measured`` is true only for RAW and PROCESSED, and a DERIVED_STATISTIC
  inherits nothing: a mean of measurements is a statistic OF measurements,
  and says so through ``derived_from``.

The inputs are named by (observation_id, kind), so the rule is checkable
from the record alone. That the named inputs exist, and have the kinds the
record says, is the evidence store's question; this answers whether the
claimed lineage permits the claimed kind.

WHEN, IN WHAT ORDER, AND WHICH BYTES

This is the framework's generic measurement boundary: what the hardware-era
ingestion layer (``qta_multiphysics/measurement_ingest_3d.py``, retired)
did for one apparatus's quantities, without its mode alignment, gate
vocabulary or hard-coded refusals. An observation carries its unit and
resolution (the Quantity), its source, its transformation, its uncertainty
and calibration provenance, and:

* ``time`` -- when it was acquired or produced, an ISO 8601 instant WITH its
  UTC offset. A naive time is refused: two instruments' naive times cannot
  be put in one order;
* ``sequence`` -- its position in an ordered series (a scan index, a shot
  number) when order matters and no clock gives it;
* ``digest()`` -- the content digest of its whole record, so a citation
  names these values, this lineage and this time, and nothing else.

``from_record`` reads a record back exactly: a key it does not know, or one
missing, is refused rather than defaulted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .identity import digest as _digest
from .quantity import Quantity


class ObservationKind(str, Enum):
    RAW_OBSERVATION = "RAW_OBSERVATION"
    PROCESSED_OBSERVATION = "PROCESSED_OBSERVATION"
    SYNTHETIC_OBSERVATION = "SYNTHETIC_OBSERVATION"
    SIMULATION_RESULT = "SIMULATION_RESULT"
    DERIVED_STATISTIC = "DERIVED_STATISTIC"
    CALIBRATED_PARAMETER = "CALIBRATED_PARAMETER"


K = ObservationKind
MEASURED = frozenset({K.RAW_OBSERVATION, K.PROCESSED_OBSERVATION})
SIMULATED = frozenset({K.SIMULATION_RESULT, K.SYNTHETIC_OBSERVATION})

#: input kind -> the kinds an observation derived from it may have.
ALLOWED = {
    K.RAW_OBSERVATION: frozenset({K.PROCESSED_OBSERVATION,
                                  K.DERIVED_STATISTIC,
                                  K.CALIBRATED_PARAMETER}),
    K.PROCESSED_OBSERVATION: frozenset({K.PROCESSED_OBSERVATION,
                                        K.DERIVED_STATISTIC,
                                        K.CALIBRATED_PARAMETER}),
    K.SYNTHETIC_OBSERVATION: frozenset({K.SYNTHETIC_OBSERVATION,
                                        K.DERIVED_STATISTIC}),
    K.SIMULATION_RESULT: frozenset({K.SIMULATION_RESULT,
                                    K.SYNTHETIC_OBSERVATION,
                                    K.DERIVED_STATISTIC,
                                    K.CALIBRATED_PARAMETER}),
    K.DERIVED_STATISTIC: frozenset({K.DERIVED_STATISTIC,
                                    K.SIMULATION_RESULT,
                                    K.CALIBRATED_PARAMETER}),
    K.CALIBRATED_PARAMETER: frozenset({K.CALIBRATED_PARAMETER,
                                       K.SIMULATION_RESULT,
                                       K.DERIVED_STATISTIC}),
}


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class Observation:
    observation_id: str
    kind: ObservationKind
    quantity: Quantity
    #: instrument, dataset, or model id@version -- who or what produced it.
    source: str
    transformation: str = ""
    #: ((observation_id, kind), ...) of the inputs it was derived from.
    derived_from: tuple = ()
    calibration: str = ""
    uncertainty_provenance: str = ""
    #: ISO 8601 with a UTC offset; "" when not stated.
    time: str = ""
    #: position in an ordered series; None when not stated.
    sequence: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "kind", ObservationKind(self.kind))
        if self.time:
            try:
                when = datetime.fromisoformat(self.time)
            except (TypeError, ValueError) as exc:
                raise ObservationError(
                    f"time {self.time!r} is not an ISO 8601 instant") from exc
            if when.utcoffset() is None:
                raise ObservationError(
                    f"time {self.time!r} has no UTC offset; a naive time "
                    "cannot be ordered against another instrument's")
        if self.sequence is not None and (
                isinstance(self.sequence, bool)
                or not isinstance(self.sequence, int) or self.sequence < 0):
            raise ObservationError(f"sequence {self.sequence!r} is not a "
                                   "non-negative integer")
        if not isinstance(self.observation_id, str) or \
                not self.observation_id.strip():
            raise ObservationError("an observation needs an id")
        if not isinstance(self.quantity, Quantity):
            raise ObservationError("an observation's value is a Quantity")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ObservationError("an observation must name its source")
        inputs = tuple((str(i), ObservationKind(k))
                       for i, k in self.derived_from)
        object.__setattr__(self, "derived_from", inputs)
        kind = self.kind
        if kind is K.RAW_OBSERVATION:
            if inputs or self.transformation:
                raise ObservationError(
                    "a RAW_OBSERVATION is not derived from anything; one "
                    "that is has been processed or simulated")
            return
        if not inputs:
            if kind is K.SIMULATION_RESULT:
                return          # a model run from parameters alone
            raise ObservationError(
                f"a {kind.value} must name what it was derived from")
        if not self.transformation.strip():
            raise ObservationError("a derived observation must say how it "
                                   "was derived (transformation)")
        for oid, ik in inputs:
            if kind not in ALLOWED[ik]:
                why = ("; a simulation never becomes a measurement"
                       if ik in SIMULATED else "")
                raise ObservationError(f"{kind.value} cannot be derived "
                                       f"from {oid} ({ik.value}){why}")
        if kind is K.CALIBRATED_PARAMETER:
            if not any(ik in MEASURED for _, ik in inputs):
                raise ObservationError(
                    "a CALIBRATED_PARAMETER must cite a measured input; one "
                    "fitted only to simulations is a simulation output")
            if not self.calibration.strip():
                raise ObservationError("a CALIBRATED_PARAMETER must state "
                                       "its calibration provenance")

    @property
    def measured(self) -> bool:
        return self.kind in MEASURED

    def to_record(self) -> dict:
        return {"observation_id": self.observation_id,
                "kind": self.kind.value,
                "quantity": self.quantity.to_record(),
                "source": self.source,
                "transformation": self.transformation,
                "derived_from": [[i, k.value] for i, k in self.derived_from],
                "calibration": self.calibration,
                "uncertainty_provenance": self.uncertainty_provenance,
                "time": self.time, "sequence": self.sequence}

    def digest(self) -> str:
        """The content digest of the whole record."""
        return _digest(self.to_record())

    @classmethod
    def from_record(cls, rec: dict) -> "Observation":
        if not isinstance(rec, dict) or set(rec) != _RECORD_KEYS:
            got = set(rec) if isinstance(rec, dict) else rec
            raise ObservationError(f"an observation record has exactly the "
                                   f"keys {sorted(_RECORD_KEYS)}; got {got}")
        return cls(observation_id=rec["observation_id"], kind=rec["kind"],
                   quantity=Quantity.from_record(rec["quantity"]),
                   source=rec["source"],
                   transformation=rec["transformation"],
                   derived_from=tuple((i, k) for i, k in rec["derived_from"]),
                   calibration=rec["calibration"],
                   uncertainty_provenance=rec["uncertainty_provenance"],
                   time=rec["time"], sequence=rec["sequence"])


_RECORD_KEYS = frozenset({
    "observation_id", "kind", "quantity", "source", "transformation",
    "derived_from", "calibration", "uncertainty_provenance", "time",
    "sequence"})
