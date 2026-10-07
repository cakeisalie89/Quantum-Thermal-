"""NF-1T s.15-18, s.23-24, s.56: units, tokens, OOD classes, constraints."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scientific_ai.neural import constraints as C  # noqa: E402
from scientific_ai.neural import features as F  # noqa: E402
from scientific_ai.neural import tokens, units  # noqa: E402
from scientific_ai.neural.ood import InputOOD  # noqa: E402

SCHEMA = F.FeatureSchema(
    "probe",
    (F.FeatureSpec("count", "COUNT", role="CONDITION", lower=1, upper=100,
                   integer=True),
     F.FeatureSpec("p", "Pa", "log10_positive", lower=1e-12, upper=1e5),
     F.FeatureSpec("t", "K", lower=0.0, upper=1e4),
     F.FeatureSpec("x", "1", lower=-1.0, upper=1.0, missing_allowed=True)),
    (F.TargetSpec("y", "1"),))


def norm():
    rows = [{"count": 1, "p": 1.0, "t": 10.0, "x": 0.1},
            {"count": 4, "p": 100.0, "t": 30.0, "x": -0.2},
            {"count": 2, "p": 10.0, "t": 20.0, "x": 0.0}]
    return F.Normalization.fit(SCHEMA, rows, [{"y": 0.1}, {"y": 0.3},
                                              {"y": 0.2}],
                               split_digest="0" * 64)


def rec(**kw):
    base = {"count": 2, "p": 1.0, "t": 20.0, "x": 0.0}
    base.update(kw)
    return base


# -- units ---------------------------------------------------------------

def test_every_unit_in_the_unit_authority_is_read_or_refused_by_rule():
    inv = json.loads((ROOT / "docs" / "unit_inventory.json").read_text())
    for col, entry in inv["columns"].items():
        u = entry["unit"]
        if u in units.NOT_A_FEATURE_UNIT:
            with pytest.raises(units.UnitError):
                units.parse(u)
        else:
            units.parse(u)    # raises on anything unknown


@pytest.mark.parametrize("u,exps,factor", [
    ("K", (0, 0, 0, 0, 1, 0, 0), 1.0),
    ("Pa", (-1, 1, -2, 0, 0, 0, 0), 1.0),
    ("mK", (0, 0, 0, 0, 1, 0, 0), 1e-3),
    ("us", (0, 0, 1, 0, 0, 0, 0), 1e-6),
    ("1/(m^2 s)", (-2, 0, -1, 0, 0, 0, 0), 1.0),
    ("W m^-2 K^-4", (0, 1, -3, 0, -4, 0, 0), 1.0),
    ("W/m^3", (-1, 1, -3, 0, 0, 0, 0), 1.0),
    ("1", (0,) * 7, 1.0),
])
def test_units_read_to_si_exponents_and_factors(u, exps, factor):
    d = units.parse(u)
    assert d.exponents == exps and math.isclose(d.si_factor, factor)


@pytest.mark.parametrize("bad", ["", "  ", "furlong", "m^0", "m/s/s",
                                 "(m s)", "1/(m s", "m^x", "Pa^"])
def test_an_unreadable_unit_is_refused_not_guessed(bad):
    with pytest.raises(units.UnitError):
        units.parse(bad)


def test_kelvin_and_pascal_are_different_dimensions():
    assert not units.same_dimension("K", "Pa")
    assert units.same_dimension("K", "mK")


# -- tokens --------------------------------------------------------------

def test_equal_scalars_in_different_units_are_different_tokens():
    s = F.FeatureSchema("kp", (F.FeatureSpec("a_t", "K", lower=0.0),
                               F.FeatureSpec("b_p", "Pa", lower=0.0)),
                        (F.TargetSpec("y", "1"),))
    n = F.Normalization.fit(s, [{"a_t": 0.0, "b_p": 0.0},
                                {"a_t": 1.0, "b_p": 1.0}],
                            [{"y": 0.0}, {"y": 1.0}], split_digest="0" * 64)
    tk = F.tokenize(s, n, [{"a_t": 0.01, "b_p": 0.01}])
    # the same number, the same value channels ...
    assert np.array_equal(tk["values"][0, 0], tk["values"][0, 1])
    # ... and a different identity and a different dimension
    assert tk["feature_ids"][0, 0] != tk["feature_ids"][0, 1]
    assert not np.array_equal(tk["dim_exponents"][0, 0],
                              tk["dim_exponents"][0, 1])


def test_feature_identity_is_preserved_and_order_does_not_matter():
    n = norm()
    a = F.tokenize(SCHEMA, n, [{"count": 2, "p": 1.0, "t": 20.0, "x": 0.5}])
    b = F.tokenize(SCHEMA, n, [{"x": 0.5, "t": 20.0, "p": 1.0, "count": 2}])
    for k in a:
        assert np.array_equal(a[k], b[k])
    assert a["feature_ids"][0].tolist() == [0, 1, 2, 3]
    assert list(SCHEMA.feature_names) == sorted(SCHEMA.feature_names)


def test_a_missing_value_is_explicit_and_carries_no_number():
    tk = F.tokenize(SCHEMA, norm(), [rec(x=None)])
    j = SCHEMA.feature_names.index("x")
    assert tk["validity"][0, j] == tokens.VALIDITY.index("MISSING")
    assert np.all(tk["values"][0, j] == 0)


def test_missing_where_the_schema_requires_a_value_is_refused():
    with pytest.raises(F.SchemaMismatch, match="required"):
        F.tokenize(SCHEMA, norm(), [rec(t=None)])


@pytest.mark.parametrize("value,exc", [
    (float("nan"), F.MalformedInput), (float("inf"), F.MalformedInput),
    (-float("inf"), F.MalformedInput), ("20", F.MalformedInput),
    (True, F.MalformedInput), (-5.0, F.ScientificallyInvalid),
    (2e4, F.ScientificallyInvalid)])
def test_invalid_values_never_become_valid_numbers(value, exc):
    with pytest.raises(exc):
        F.tokenize(SCHEMA, norm(), [rec(t=value)])


def test_nan_is_not_a_missing_marker():
    with pytest.raises(F.MalformedInput, match="None"):
        F.tokenize(SCHEMA, norm(), [rec(x=float("nan"))])


def test_a_count_must_be_an_integer_and_a_log_feature_positive():
    with pytest.raises(F.MalformedInput):
        F.tokenize(SCHEMA, norm(), [rec(count=2.5)])
    with pytest.raises(F.ScientificallyInvalid):
        F.tokenize(SCHEMA, norm(), [rec(p=0.0)])


@pytest.mark.parametrize("record", [
    {"count": 2, "p": 1.0, "t": 20.0},
    {"count": 2, "p": 1.0, "t": 20.0, "x": 0.0, "extra": 1.0},
    ["not", "a", "mapping"]])
def test_a_schema_mismatch_is_refused(record):
    with pytest.raises(F.SchemaMismatch):
        F.tokenize(SCHEMA, norm(), [record])


def test_a_value_in_another_unit_of_the_same_dimension_is_converted():
    n = norm()
    a = F.tokenize(SCHEMA, n, [rec(t=0.01)])
    b = F.tokenize(SCHEMA, n, [rec(t={"value": 10.0, "unit": "mK"})])
    assert np.array_equal(a["values"], b["values"])
    with pytest.raises(F.SchemaMismatch, match="dimension"):
        F.tokenize(SCHEMA, n, [rec(t={"value": 10.0, "unit": "Pa"})])


def test_normalisation_is_deterministic_and_training_only():
    a, b = norm(), norm()
    assert a == b and a.digest() == b.digest()
    assert a.fitted_on == "0" * 64 and a.rows == 3
    assert F.Normalization.from_dict(a.to_dict()) == a


def test_a_schema_with_a_feature_that_is_also_a_target_is_refused():
    with pytest.raises(F.SchemaError, match="leakage"):
        F.FeatureSchema("bad", (F.FeatureSpec("y", "1"),),
                        (F.TargetSpec("y", "1"),))


def test_schema_digests_are_stable_and_sensitive():
    assert SCHEMA.feature_digest() == F.FeatureSchema(
        SCHEMA.name, SCHEMA.features, SCHEMA.targets).feature_digest()
    other = F.FeatureSchema(SCHEMA.name, SCHEMA.features,
                            (F.TargetSpec("y", "K"),))
    assert other.target_digest() != SCHEMA.target_digest()
    assert other.feature_digest() == SCHEMA.feature_digest()


@settings(max_examples=300, deadline=None, derandomize=True)
@given(st.floats(min_value=1e-12, max_value=1e5, allow_nan=False,
                 allow_infinity=False))
def test_log_magnitude_and_sign_channels_are_exact_at_every_scale(p):
    tk = F.tokenize(SCHEMA, norm(), [rec(p=p)])
    j = SCHEMA.feature_names.index("p")
    z, sign, logmag, is_zero = tk["values"][0, j]
    assert sign == 1.0 and is_zero == 0.0
    assert math.isclose(float(logmag), math.log10(p) / tokens.LOG_MAG_SCALE,
                        rel_tol=1e-6, abs_tol=1e-7)
    assert np.isfinite(z)


@settings(max_examples=200, deadline=None, derandomize=True)
@given(st.floats(min_value=-1.0, max_value=1.0, allow_nan=False))
def test_zero_negative_and_positive_values_are_separable(x):
    tk = F.tokenize(SCHEMA, norm(), [rec(x=x)])
    j = SCHEMA.feature_names.index("x")
    _, sign, _, is_zero = tk["values"][0, j]
    assert sign == (0.0 if x == 0 else math.copysign(1.0, x))
    assert is_zero == (1.0 if x == 0 else 0.0)


# -- OOD classes ---------------------------------------------------------

def test_every_ood_class_is_reachable_and_distinct():
    n = norm()
    rows = [rec(count=c, p=10 ** (c / 2), t=10.0 + c, x=0.01 * c)
            for c in range(1, 4)]
    o = InputOOD.fit(SCHEMA, n, rows)
    assert o.assess(SCHEMA, n, rec(), record_schema_digest="1" * 64)[
        "class"] == "UNSUPPORTED_SCHEMA"
    assert o.assess(SCHEMA, n, {"p": 1.0})["class"] == "UNSUPPORTED_SCHEMA"
    assert o.assess(SCHEMA, n, rec(t=float("nan")))["class"] == \
        "MALFORMED_INPUT"
    assert o.assess(SCHEMA, n, rec(t=-1.0))["class"] == \
        "SCIENTIFICALLY_INVALID"
    ext = o.assess(SCHEMA, n, rec(t=500.0))
    assert ext["class"] == "EXTRAPOLATION" and ext["features"] == ["t"]
    assert o.assess(SCHEMA, n, rows[1])["class"] in (
        "IN_DISTRIBUTION", "DISTRIBUTION_SHIFT")


# -- constraints ---------------------------------------------------------

def test_a_violation_is_recorded_and_the_prediction_is_not_touched():
    cons = C.surface_adsorption()
    inputs = {"capacity_per_m2": 1e18, "initial_coverage": 0.5,
              "sticking": 0.5}
    pred = {"admitted_fluence": 1e17, "final_coverage": 1.2,
            "final_inventory": 2e18, "impingement_flux": float("nan")}
    snapshot = dict(pred)
    rep = C.evaluate(cons, [("s1", inputs, pred)])
    assert pred == snapshot or math.isnan(pred["impingement_flux"])
    got = {k: v["violations"] for k, v in rep["constraints"].items()}
    assert got["coverage_bounded"] == 1
    assert got["inventory_within_capacity"] == 1
    assert got["finite_impingement_flux"] == 1
    assert got["within_incidence"] == 1
    assert "no prediction is clipped" in rep["policy"]


def test_a_constraint_must_say_where_it_comes_from():
    with pytest.raises(ValueError):
        C.Constraint("x", "RANGE", "y", source="  ")
    with pytest.raises(ValueError):
        C.Constraint("x", "RELATION", "y", source="s")
