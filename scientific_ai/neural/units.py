"""The physical dimension of a unit, as this repository spells units.

``docs/unit_inventory.json`` (and every ``Quantity``) is the authority on
WHICH unit a quantity carries: a reviewed string such as ``K``, ``W/m^3``,
``1/(m^2 s)`` or ``W m^-2 K^-4``, or one of the reviewed non-units
``DIMENSIONLESS``, ``COUNT``, ``ORDINAL``, ``PER_ROW``. Nothing here decides
a unit. This module only reads one: it returns the SI base-dimension
exponents and the factor to coherent SI, so that a model can tell 0.01 K
from 0.01 Pa without trusting a column name -- the two carry the same
scalar and different dimensions, and the tokenizer embeds the dimension.

The grammar is exactly the repository's: at most one ``/``; each side a
space-separated product of factors, the denominator optionally
parenthesised; a factor is a symbol with an optional signed integer
exponent (``m^-2``); ``1`` stands for an empty numerator. A symbol is matched
exactly first (``Pa``, ``u``, ``m``), then as a decimal prefix on a unit
(``mK``, ``us``). An unknown symbol is refused, never guessed:
``tests/test_neural_units.py`` parses every unit in the inventory.

``PER_ROW`` names a long-format column whose unit belongs to the row: it has
no dimension of its own, so a feature cannot be built from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: The order of the exponent vector: length, mass, time, current,
#: temperature, amount, luminous intensity.
BASE = ("m", "kg", "s", "A", "K", "mol", "cd")

#: Dimension classes the tokenizer embeds. PHYSICAL covers every unit with
#: an SI form, including a genuine ratio written ``1``; the three non-units
#: are the inventory's reviewed vocabulary.
PHYSICAL = "PHYSICAL"
DIMENSIONLESS = "DIMENSIONLESS"
COUNT = "COUNT"
ORDINAL = "ORDINAL"
DIMENSION_CLASSES = (PHYSICAL, DIMENSIONLESS, COUNT, ORDINAL)
NON_UNITS = (DIMENSIONLESS, COUNT, ORDINAL)
#: Reviewed non-units that cannot describe a feature.
NOT_A_FEATURE_UNIT = ("PER_ROW",)


class UnitError(ValueError):
    """A unit this module cannot read -- refused, not guessed."""


def _dim(**exps) -> tuple:
    return tuple(int(exps.get(b, 0)) for b in BASE)


_ZERO = _dim()

#: symbol -> (exponents, factor to coherent SI). ``g`` is listed so that
#: ``kg`` parses as a prefixed gram with factor 1.
_UNITS = {
    "m": (_dim(m=1), 1.0),
    "g": (_dim(kg=1), 1e-3),
    "s": (_dim(s=1), 1.0),
    "A": (_dim(A=1), 1.0),
    "K": (_dim(K=1), 1.0),
    "mol": (_dim(mol=1), 1.0),
    "cd": (_dim(cd=1), 1.0),
    "Hz": (_dim(s=-1), 1.0),
    "N": (_dim(kg=1, m=1, s=-2), 1.0),
    "Pa": (_dim(kg=1, m=-1, s=-2), 1.0),
    "J": (_dim(kg=1, m=2, s=-2), 1.0),
    "W": (_dim(kg=1, m=2, s=-3), 1.0),
    "C": (_dim(A=1, s=1), 1.0),
    "V": (_dim(kg=1, m=2, s=-3, A=-1), 1.0),
    "eV": (_dim(kg=1, m=2, s=-2), 1.602176634e-19),
    # the unified atomic mass unit (CODATA 2018), as the inventory writes it
    "u": (_dim(kg=1), 1.66053906660e-27),
    # plane angle and information: SI-dimensionless, kept by name in the
    # feature's identity rather than in its exponents
    "rad": (_ZERO, 1.0),
    "nats": (_ZERO, 1.0),
    "percent": (_ZERO, 1e-2),
}

_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "c": 1e-2,
           "k": 1e3, "M": 1e6, "G": 1e9}

_FACTOR = re.compile(r"\A([A-Za-z]+)(?:\^(-?[0-9]+))?\Z")


@dataclass(frozen=True)
class Dimension:
    """What a unit means physically: its class, SI exponents, SI factor."""

    dimension_class: str
    exponents: tuple
    si_factor: float
    unit: str

    def to_dict(self) -> dict:
        return {"unit": self.unit, "dimension_class": self.dimension_class,
                "exponents": list(self.exponents),
                "si_factor": self.si_factor}

    @property
    def is_dimensionless(self) -> bool:
        return self.exponents == _ZERO


def _symbol(sym: str) -> tuple:
    if sym in _UNITS:
        return _UNITS[sym]
    if len(sym) > 1 and sym[0] in _PREFIX and sym[1:] in _UNITS \
            and sym[1:] not in ("u", "percent", "nats"):
        exps, f = _UNITS[sym[1:]]
        return exps, f * _PREFIX[sym[0]]
    raise UnitError(f"unknown unit symbol {sym!r}")


def _product(text: str) -> tuple:
    exps = [0] * len(BASE)
    factor = 1.0
    parts = text.split()
    if not parts:
        raise UnitError("an empty unit product")
    if parts == ["1"]:
        return tuple(exps), factor
    for part in parts:
        m = _FACTOR.match(part)
        if not m:
            raise UnitError(f"cannot read the factor {part!r}")
        sym, power = m.group(1), int(m.group(2) or 1)
        if power == 0:
            raise UnitError(f"{part!r}: a zero exponent is not a unit")
        e, f = _symbol(sym)
        for i, v in enumerate(e):
            exps[i] += v * power
        factor *= f ** power
    return tuple(exps), factor


def parse(unit: str) -> Dimension:
    """The dimension of ``unit``; :class:`UnitError` on anything else."""
    if not isinstance(unit, str) or not unit.strip():
        raise UnitError(f"a unit must be a non-empty string, got {unit!r}")
    u = unit.strip()
    if u in NON_UNITS:
        return Dimension(u, _ZERO, 1.0, u)
    if u in NOT_A_FEATURE_UNIT:
        raise UnitError(f"{u}: the unit belongs to the row, not to a "
                        "quantity a feature could carry")
    if u.count("/") > 1:
        raise UnitError(f"{u!r}: more than one '/' is ambiguous")
    if "/" in u:
        num, den = (s.strip() for s in u.split("/"))
        if den.startswith("(") != den.endswith(")"):
            raise UnitError(f"{u!r}: unbalanced parentheses")
        if den.startswith("("):
            den = den[1:-1].strip()
        if "(" in num or "(" in den or ")" in num or ")" in den:
            raise UnitError(f"{u!r}: nested or misplaced parentheses")
        ne, nf = _product(num)
        de, df = _product(den)
        exps = tuple(a - b for a, b in zip(ne, de))
        factor = nf / df
    else:
        if "(" in u or ")" in u:
            raise UnitError(f"{u!r}: parentheses only group a denominator")
        exps, factor = _product(u)
    return Dimension(PHYSICAL, exps, factor, u)


def same_dimension(a: str, b: str) -> bool:
    da, db = parse(a), parse(b)
    return (da.dimension_class, da.exponents) == (db.dimension_class,
                                                  db.exponents)
