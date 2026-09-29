"""A published number with its unit, its resolution, and what zero means.

D-2026-69, made generic. The cumulative energy ledger published
``1.615587134e-27`` J on one machine and ``0.000000000e+00`` on another, and
the running sum's forward error bound at that row is 8.1e-27 J: both values
are below what the method resolves, so they are the SAME result, and the file
said nothing that would let a reader know. A comparison of the digits then
reported a zero crossing -- a finding about noise.

So a Quantity keeps apart what a bare float merges:

* ``value`` and ``unit``;
* ``resolution`` -- the numerical floor of the method that produced the
  value, in ``unit``, with ``resolution_class`` saying what KIND of floor it
  is and ``resolution_basis`` saying where the number came from. A stated
  class without a number, or a number without a basis, is refused;
* ``reporting_digits`` -- how many significant digits are written, which is a
  formatting decision and not a precision claim;
* ``uncertainty_class`` -- a class, never a single uncertainty float standing
  in for all of them; ``NOT_ASSESSED`` is the honest default;
* ``exact`` -- a value that is defined rather than computed (an insulated
  boundary's flux is 0 by construction). Only an exact value can be an
  EXACT_ZERO; a computed value inside its resolution is BELOW_RESOLUTION
  whatever digits it happens to print.

Nothing here decides a tolerance. ``indistinguishable`` compares two values
at the coarser of their stated resolutions and refuses to answer when either
states none.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class ResolutionClass(str, Enum):
    #: ~ one machine epsilon of a single evaluation, relative to its value.
    SINGLE_EVALUATION = "SINGLE_EVALUATION"
    #: the forward error bound of a reduction (n * eps * sum |t_i| for a sum).
    ACCUMULATED_BOUND = "ACCUMULATED_BOUND"
    #: an integrator's rtol / atol applied to this value.
    SOLVER_TOLERANCE = "SOLVER_TOLERANCE"
    #: an estimate from refining a discretization (mesh, time step).
    DISCRETIZATION_ESTIMATE = "DISCRETIZATION_ESTIMATE"
    NOT_STATED = "NOT_STATED"


class UncertaintyClass(str, Enum):
    NUMERICAL = "NUMERICAL"
    PARAMETRIC = "PARAMETRIC"
    MODEL_FORM = "MODEL_FORM"
    MEASUREMENT = "MEASUREMENT"
    NOT_ASSESSED = "NOT_ASSESSED"


class ZeroState(str, Enum):
    EXACT_ZERO = "EXACT_ZERO"
    BELOW_RESOLUTION = "BELOW_RESOLUTION"
    RESOLVED = "RESOLVED"
    #: no resolution was stated, so whether it is zero cannot be said.
    RESOLUTION_NOT_STATED = "RESOLUTION_NOT_STATED"


class QuantityError(ValueError):
    pass


_FIELDS = ("value", "unit", "resolution", "resolution_class",
           "resolution_basis", "reporting_digits", "uncertainty_class",
           "exact")


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str
    resolution: float | None = None
    resolution_class: ResolutionClass = ResolutionClass.NOT_STATED
    resolution_basis: str = ""
    reporting_digits: int | None = None
    uncertainty_class: UncertaintyClass = UncertaintyClass.NOT_ASSESSED
    exact: bool = False

    def __post_init__(self):
        if isinstance(self.value, bool) or not isinstance(
                self.value, (int, float)):
            raise QuantityError(f"value must be a number: {self.value!r}")
        if not math.isfinite(self.value):
            raise QuantityError(f"value must be finite: {self.value!r}")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise QuantityError("a quantity needs a unit ('1' for "
                                "dimensionless); an empty unit is a missing "
                                "one")
        object.__setattr__(self, "resolution_class",
                           ResolutionClass(self.resolution_class))
        object.__setattr__(self, "uncertainty_class",
                           UncertaintyClass(self.uncertainty_class))
        r = self.resolution
        if r is not None:
            if isinstance(r, bool) or not isinstance(r, (int, float)) \
                    or not math.isfinite(r) or r < 0:
                raise QuantityError(f"resolution must be a finite number "
                                    f">= 0: {r!r}")
            if self.resolution_class is ResolutionClass.NOT_STATED:
                raise QuantityError("a resolution without its class is a "
                                    "number nobody can interpret")
            if not self.resolution_basis.strip():
                raise QuantityError("a resolution must say where it came "
                                    "from (resolution_basis)")
        elif self.resolution_class is not ResolutionClass.NOT_STATED:
            raise QuantityError(f"resolution class "
                                f"{self.resolution_class.value} stated with "
                                "no resolution")
        d = self.reporting_digits
        if d is not None and (isinstance(d, bool) or not isinstance(d, int)
                              or not 1 <= d <= 17):
            raise QuantityError(f"reporting_digits must be 1..17: {d!r}")
        if not isinstance(self.exact, bool):
            raise QuantityError("exact must be a bool")
        if self.exact and r not in (None, 0, 0.0):
            raise QuantityError("an exact value has no numerical floor")

    def zero_state(self) -> ZeroState:
        if self.exact:
            return (ZeroState.EXACT_ZERO if self.value == 0
                    else ZeroState.RESOLVED)
        if self.resolution is None:
            return ZeroState.RESOLUTION_NOT_STATED
        if abs(self.value) <= self.resolution:
            return ZeroState.BELOW_RESOLUTION
        return ZeroState.RESOLVED

    def formatted(self) -> str:
        """The value as it may be published: a below-resolution value is
        written as its bound, never as digits that look like a result."""
        state = self.zero_state()
        if state is ZeroState.BELOW_RESOLUTION:
            return (f"|x| <= {self.resolution:.3g} {self.unit} "
                    f"(below resolution, {self.resolution_class.value})")
        if state is ZeroState.EXACT_ZERO:
            return f"0 {self.unit} (exact)"
        digits = self.reporting_digits or 17
        return f"{self.value:.{digits - 1}e} {self.unit}"

    def to_record(self) -> dict:
        return {"value": self.value, "unit": self.unit,
                "resolution": self.resolution,
                "resolution_class": self.resolution_class.value,
                "resolution_basis": self.resolution_basis,
                "reporting_digits": self.reporting_digits,
                "uncertainty_class": self.uncertainty_class.value,
                "exact": self.exact,
                "zero_state": self.zero_state().value}

    @classmethod
    def from_record(cls, rec: dict) -> "Quantity":
        if not isinstance(rec, dict):
            raise QuantityError("a quantity record must be a mapping")
        extra = set(rec) - set(_FIELDS) - {"zero_state"}
        missing = set(_FIELDS) - set(rec)
        if extra or missing:
            raise QuantityError(f"quantity record keys: extra "
                                f"{sorted(extra)}, missing {sorted(missing)}")
        q = cls(**{k: rec[k] for k in _FIELDS})
        if "zero_state" in rec and rec["zero_state"] != q.zero_state().value:
            raise QuantityError(
                f"record says {rec['zero_state']}, the values say "
                f"{q.zero_state().value}")
        return q


def indistinguishable(a: Quantity, b: Quantity) -> bool:
    """True when ``a`` and ``b`` agree within the coarser stated resolution.

    Refuses (raises) on different units, and on a quantity that states no
    resolution: "they agree" needs a scale, and inventing one is exactly the
    tolerance-widening this is here to prevent.
    """
    if a.unit != b.unit:
        raise QuantityError(f"units differ: {a.unit!r} vs {b.unit!r}")
    ra = 0.0 if a.exact else a.resolution
    rb = 0.0 if b.exact else b.resolution
    if ra is None or rb is None:
        raise QuantityError("a quantity with no stated resolution cannot be "
                            "compared at its resolution")
    return abs(a.value - b.value) <= max(ra, rb)
