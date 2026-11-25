"""Dimensions, exact scales and quantity kinds for the units this layer uses
(Assurance H).

WHAT IT ANSWERS

* What a unit string means: its SI dimension, as exponents of kg, m, s, A,
  K, mol and cd, and its exact scale to the coherent SI unit -- for the
  grammar the layer actually writes ("W m^-2 K^-1", "J/K", "kW/m^2", "1").
  A symbol it does not define is refused, never guessed at.
* Whether two units are dimensionally compatible, and the exact factor
  between them.
* Whether a product of declared quantities is dimensionless -- the check
  a model's governing equations must pass (:mod:`.dimensional_statements`).

WHAT IT KEEPS APART

Dimensional compatibility is not quantity identity. Heat capacity and
entropy are both J/K; energy and torque are both kg m^2 s^-2; a temperature
and a temperature difference are both K. :data:`KINDS` names the kinds this
layer uses, and :func:`convert` converts only within one. The Celsius scale
is affine: converting a value in degC needs to know whether it is a
temperature (an offset of 273.15) or a difference (none), and without that
it is refused.

WHAT IT DOES NOT DO

Symbolic algebra or calculus. Whether an expression equals another, or a
derivative was taken correctly, is not checked here or anywhere in this
repository: SymPy would be a new dependency (an owner decision), and a
derivation nothing checked stays UNCHECKED, never assumed. Nor is a
dimensionally consistent equation thereby a correct one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

BASE = ("kg", "m", "s", "A", "K", "mol", "cd")


class DimensionError(ValueError):
    """A unit nothing here defines, or a conversion that is not sound."""


@dataclass(frozen=True)
class Unit:
    """A unit as exponents of the SI base units, an exact scale to the
    coherent SI unit, and an offset (non-zero only for an affine scale)."""

    dims: tuple
    scale: Fraction
    offset: Fraction = Fraction(0)

    @property
    def affine(self) -> bool:
        return self.offset != 0

    def __mul__(self, other: "Unit") -> "Unit":
        if self.affine or other.affine:
            raise DimensionError("an affine unit (degC) does not multiply")
        return Unit(tuple(a + b for a, b in zip(self.dims, other.dims)),
                    self.scale * other.scale)

    def __pow__(self, n) -> "Unit":
        n = Fraction(n)
        if self.affine and n != 1:
            raise DimensionError("an affine unit (degC) has no powers")
        if n.denominator != 1 and self.scale != 1:
            raise DimensionError("a fractional power of a scaled unit has "
                                 "no exact scale")
        return Unit(tuple(d * n for d in self.dims),
                    self.scale ** int(n) if n.denominator == 1 else
                    Fraction(1), self.offset)


def _dims(**exp) -> tuple:
    return tuple(Fraction(exp.get(b, 0)) for b in BASE)


ONE = Unit(_dims(), Fraction(1))
#: The atomic mass constant, kg (CODATA 2018), carried exactly as written.
_U_KG = Fraction("1.66053906660e-27")

#: Every symbol this layer may write, and what it is. Extending it is a
#: reviewed act, as the FMI boundary's own table is.
SYMBOLS: dict = {
    "1": ONE,
    "kg": Unit(_dims(kg=1), Fraction(1)),
    "g": Unit(_dims(kg=1), Fraction(1, 1000)),
    "m": Unit(_dims(m=1), Fraction(1)),
    "s": Unit(_dims(s=1), Fraction(1)),
    "min": Unit(_dims(s=1), Fraction(60)),
    "h": Unit(_dims(s=1), Fraction(3600)),
    "A": Unit(_dims(A=1), Fraction(1)),
    "K": Unit(_dims(K=1), Fraction(1)),
    "degC": Unit(_dims(K=1), Fraction(1), Fraction("273.15")),
    "mol": Unit(_dims(mol=1), Fraction(1)),
    "cd": Unit(_dims(cd=1), Fraction(1)),
    "N": Unit(_dims(kg=1, m=1, s=-2), Fraction(1)),
    "J": Unit(_dims(kg=1, m=2, s=-2), Fraction(1)),
    "W": Unit(_dims(kg=1, m=2, s=-3), Fraction(1)),
    "Pa": Unit(_dims(kg=1, m=-1, s=-2), Fraction(1)),
    "Hz": Unit(_dims(s=-1), Fraction(1)),
    "u": Unit(_dims(kg=1), _U_KG),
}
#: Prefixes, on the symbols that take them. "u" alone is the atomic mass
#: unit; as a prefix it is micro, and only before a symbol that takes one.
PREFIXES = {"G": Fraction(10**9), "M": Fraction(10**6), "k": Fraction(1000),
            "c": Fraction(1, 100), "m": Fraction(1, 1000),
            "u": Fraction(1, 10**6), "n": Fraction(1, 10**9)}
_PREFIXABLE = frozenset({"m", "g", "s", "A", "K", "mol", "N", "J", "W", "Pa",
                         "Hz"})

_FACTOR = re.compile(r"([A-Za-z]+|1)(?:\^([+-]?\d+(?:/\d+)?))?")


def _symbol(sym: str) -> Unit:
    if sym in SYMBOLS:
        return SYMBOLS[sym]
    if len(sym) > 1 and sym[0] in PREFIXES and sym[1:] in _PREFIXABLE:
        base = SYMBOLS[sym[1:]]
        return Unit(base.dims, base.scale * PREFIXES[sym[0]])
    raise DimensionError(f"no unit {sym!r} is defined here")


def _term(text: str) -> Unit:
    out = ONE
    factors = [f for f in re.split(r"[ *]+", text.strip()) if f]
    if not factors:
        raise DimensionError("an empty unit term")
    for f in factors:
        m = _FACTOR.fullmatch(f)
        if m is None:
            raise DimensionError(f"{f!r} is not a unit factor")
        u = _symbol(m[1])
        if m[2] is not None:
            u = u ** Fraction(m[2])
        if len(factors) == 1 and m[2] is None:
            return u                     # a lone symbol keeps its offset
        out = out * u
    return out


def parse(unit: str) -> Unit:
    """The unit ``unit`` names: ``term`` or ``term/denominator``, where a
    term is factors separated by spaces or ``*``, each a symbol with an
    optional (possibly fractional) power, and a denominator is one factor
    or a parenthesized term."""
    if not isinstance(unit, str) or not unit.strip():
        raise DimensionError("a unit must be a non-empty string ('1' for "
                             "dimensionless)")
    num, sep, den = unit.partition("/")
    u = _term(num)
    if sep:
        den = den.strip()
        if den.startswith("(") and den.endswith(")"):
            den = den[1:-1]
        elif re.search(r"[ *]", den):
            raise DimensionError(f"{unit!r}: a denominator of several "
                                 "factors must be parenthesized")
        if "/" in den:
            raise DimensionError(f"{unit!r}: one '/' at most")
        u = u * (_term(den) ** -1)
    return u


def dimension(unit: str) -> dict:
    """The non-zero SI exponents of ``unit``, as the FMI BaseUnit writes
    them."""
    return {b: (int(e) if e.denominator == 1 else e)
            for b, e in zip(BASE, parse(unit).dims) if e}


def compatible(a: str, b: str) -> bool:
    return parse(a).dims == parse(b).dims


# --------------------------------------------------------- quantity kinds

@dataclass(frozen=True)
class Kind:
    name: str
    unit: str          # a unit of this kind, fixing its dimension
    #: whether a value of this kind sits on an affine scale (an absolute
    #: temperature does; a difference of temperatures does not)
    absolute: bool = False


#: The kinds this layer uses, including the pairs that share a dimension.
KINDS = {k.name: k for k in (
    Kind("temperature", "K", absolute=True),
    Kind("temperature_difference", "K"),
    Kind("time", "s"), Kind("length", "m"), Kind("mass", "kg"),
    Kind("energy", "J"), Kind("torque", "N m"),
    Kind("power", "W"), Kind("heat_flux", "W m^-2"),
    Kind("heat_capacity", "J K^-1"), Kind("entropy", "J K^-1"),
    Kind("thermal_conductance", "W K^-1"),
    Kind("thermal_conductivity", "W m^-1 K^-1"),
    Kind("heat_transfer_coefficient", "W m^-2 K^-1"),
    Kind("volumetric_heat_capacity", "J m^-3 K^-1"),
    Kind("volumetric_heat_source", "W m^-3"),
    Kind("pressure", "Pa"), Kind("areal_density", "m^-2"),
    Kind("dimensionless", "1"),
)}


def same_dimension_other_kinds(kind: str) -> list:
    """The kinds that share ``kind``'s dimension and are not it: what a
    dimension check alone cannot tell apart from it."""
    d = parse(KINDS[kind].unit).dims
    return sorted(k for k, v in KINDS.items()
                  if k != kind and parse(v.unit).dims == d)


def convert(value: float, frm: str, to: str, *, kind: str | None = None,
            to_kind: str | None = None) -> float:
    """``value`` in ``frm`` expressed in ``to``, exactly scaled.

    Refused when the dimensions differ, when the two sides are declared as
    different kinds (heat capacity is not entropy, whatever their units),
    and when an affine unit (degC) is involved and ``kind`` does not say
    whether the value is a temperature or a temperature difference."""
    a, b = parse(frm), parse(to)
    if a.dims != b.dims:
        raise DimensionError(f"{frm!r} and {to!r} differ in dimension")
    if kind is not None:
        if kind not in KINDS:
            raise DimensionError(f"no quantity kind {kind!r} is defined")
        if parse(KINDS[kind].unit).dims != a.dims:
            raise DimensionError(f"{frm!r} is not a unit of {kind}")
    if to_kind is not None and to_kind != kind:
        raise DimensionError(f"{kind} and {to_kind} are different kinds, "
                             "whatever their units")
    if a.affine or b.affine:
        if kind is None:
            raise DimensionError(
                f"{frm!r} -> {to!r} depends on whether the value is a "
                "temperature or a temperature difference; nothing says "
                "which")
        if KINDS[kind].absolute:
            si = (Fraction(value) + a.offset) * a.scale
            return float(si / b.scale - b.offset)
    return float(Fraction(value) * a.scale / b.scale)


def residual(product: dict, units: dict) -> dict:
    """The dimension left by the product of ``units[name] ** power`` over
    ``product``; empty when the product is dimensionless."""
    out = [Fraction(0)] * len(BASE)
    for name, power in product.items():
        u = parse(units[name])
        if u.affine:
            raise DimensionError(f"{name}: an affine unit cannot enter a "
                                 "product")
        out = [o + d * Fraction(power) for o, d in zip(out, u.dims)]
    return {b: e for b, e in zip(BASE, out) if e}
