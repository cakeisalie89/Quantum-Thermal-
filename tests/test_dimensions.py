"""Dimensions, exact scales, quantity kinds and the dimensional statements of
each model's equations (Assurance H).

What is held:

* every unit the scientific layer writes parses, and agrees with the FMI
  boundary's own SI table where both define it;
* conversion is exact, round-trips, and is refused across dimensions,
  across kinds that share a dimension, and on the Celsius scale without
  knowing a temperature from a difference;
* every statement each model's equations make holds for the units the model
  declares -- and a unit declared wrong breaks the statement it enters;
* an unknown symbol is refused, never guessed at.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scientific import dimensional_statements as S  # noqa: E402
from scientific import dimensions as D  # noqa: E402
from scientific.fmi_boundary import SI_DEFINITIONS  # noqa: E402
from scientific.model import ParameterSchema  # noqa: E402
from scientific.models.slab_transient import SlabTransientModel  # noqa: E402
from scientific.models.surface_adsorption import (  # noqa: E402
    SurfaceAdsorptionModel,
)
from scientific.models.thermal_rc2 import ThermalRC2Model  # noqa: E402

MODELS = (SlabTransientModel(), ThermalRC2Model(), SurfaceAdsorptionModel())


# ---------------------------------------------------------------- the units

@pytest.mark.parametrize("unit, dims", [
    ("1", {}), ("K", {"K": 1}), ("s", {"s": 1}), ("m^-2", {"m": -2}),
    ("W m^-1 K^-1", {"kg": 1, "m": 1, "s": -3, "K": -1}),
    ("W/(m*K)", {"kg": 1, "m": 1, "s": -3, "K": -1}),
    ("J m^-3 K^-1", {"kg": 1, "m": -1, "s": -2, "K": -1}),
    ("Pa", {"kg": 1, "m": -1, "s": -2}), ("u", {"kg": 1}),
    ("kW/m^2", {"kg": 1, "s": -3}), ("mol", {"mol": 1}),
])
def test_each_unit_means_what_si_says(unit, dims):
    assert D.dimension(unit) == dims


def test_the_fmi_boundary_s_table_agrees():
    """Two statements of the same units, made separately: the FMI
    boundary's BaseUnit table and this module's grammar."""
    for unit, exponents in SI_DEFINITIONS.items():
        assert D.dimension(unit) == exponents, unit


def test_every_unit_a_model_declares_parses():
    for m in MODELS:
        for p in m.parameter_schema.parameters:
            D.parse(p.unit)


@pytest.mark.parametrize("bad", ["furlongs", "kkg", "W/m K", "m^x", "",
                                 "W//m", "degC^2", "degC m"])
def test_what_is_not_defined_is_refused(bad):
    with pytest.raises(D.DimensionError):
        D.parse(bad)


def test_prefixes_and_the_symbols_they_could_be_mistaken_for():
    assert D.parse("u").scale == Fraction("1.66053906660e-27")   # amu
    assert D.parse("um").scale == Fraction(1, 10**6)            # micrometre
    assert D.parse("mol").dims == D.parse("mol").dims
    assert D.parse("min").scale == 60                           # minute
    assert D.parse("mm").scale == Fraction(1, 1000)


# --------------------------------------------------------------- conversion

def test_a_scale_is_exact():
    assert D.convert(1.2, "kW/m^2", "W m^-2") == 1200.0
    assert D.convert(340000.0, "mK", "K") == 340.0
    assert D.convert(1.0, "h", "s") == 3600.0


def test_celsius_needs_to_know_a_temperature_from_a_difference():
    with pytest.raises(D.DimensionError, match="temperature difference"):
        D.convert(40.0, "degC", "K")
    assert D.convert(40.0, "degC", "K", kind="temperature") == 313.15
    assert D.convert(5.0, "degC", "K", kind="temperature_difference") == 5.0


def test_a_shared_dimension_is_not_a_shared_kind():
    """Heat capacity and entropy are both J/K, energy and torque both
    kg m^2 s^-2, a temperature and a difference both K: compatible in
    dimension, and not interchangeable."""
    assert D.same_dimension_other_kinds("heat_capacity") == ["entropy"]
    assert D.same_dimension_other_kinds("energy") == ["torque"]
    assert D.same_dimension_other_kinds("temperature") == [
        "temperature_difference"]
    with pytest.raises(D.DimensionError, match="different kinds"):
        D.convert(1.0, "J/K", "J K^-1", kind="heat_capacity",
                  to_kind="entropy")
    with pytest.raises(D.DimensionError, match="not a unit of"):
        D.convert(1.0, "W", "W", kind="energy")


def test_a_different_dimension_is_refused():
    with pytest.raises(D.DimensionError, match="differ in dimension"):
        D.convert(1.0, "W", "J")


SCALED = st.sampled_from([("kW", "W"), ("mK", "K"), ("h", "min"),
                          ("kW/m^2", "W m^-2"), ("mm", "um"), ("g", "kg"),
                          ("J/K", "kJ K^-1")])


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(SCALED, st.floats(-1e6, 1e6, allow_nan=False))
def test_a_conversion_and_its_inverse_return_the_value(pair, x):
    a, b = pair
    back = D.convert(D.convert(x, a, b), b, a)
    assert back == pytest.approx(x, rel=1e-12, abs=1e-300)


# ------------------------------------------------ the equations' statements

def test_every_model_s_equations_hold_for_its_declared_units():
    for m in MODELS:
        assert S.problems(m) == [], m.model_id
    assert all(n > 0 for n in S.coverage(MODELS).values())


def _declared(model, name, unit):
    """``model`` with parameter ``name`` declared in ``unit``."""
    params = tuple(replace(p, unit=unit) if p.name == name else p
                   for p in model.parameter_schema.parameters)
    clone = type(model)()
    clone.parameter_schema = ParameterSchema(params)
    return clone


@pytest.mark.parametrize("model, name, wrong, statement", [
    (MODELS[0], "k_W_m_K", "W m^-2 K^-1", "storage balances conduction"),
    (MODELS[0], "h_W_m2_K", "W m^-1 K^-1", "conduction balances convection"),
    (MODELS[1], "C1_J_K", "J K^-2", "node 1 stores what is delivered"),
    (MODELS[1], "G2a_W_K", "W", "both conductances"),
    (MODELS[2], "pressure_Pa", "W", "ideal-gas number density"),
    (MODELS[2], "mass_amu", "m", "impingement flux"),
])
def test_a_unit_declared_wrong_breaks_the_statement_it_enters(
        model, name, wrong, statement):
    found = S.problems(_declared(model, name, wrong))
    assert any(statement in f for f in found), found


def test_a_statement_names_nothing_the_model_does_not_declare():
    for m in MODELS:
        units = S.units_of(m)
        for title, product, _ in S.STATEMENTS[m.model_id]:
            assert set(product) <= set(units), (m.model_id, title)


def test_what_a_pass_does_not_claim_is_said():
    doc = S.__doc__ or ""
    assert "UNCHECKED" in doc and "can be wrong" in doc
    assert "SymPy" in (D.__doc__ or "") and "UNCHECKED" in (D.__doc__ or "")
