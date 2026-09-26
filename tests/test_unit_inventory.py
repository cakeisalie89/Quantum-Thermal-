"""A number published without its dimension is not the number it came from.

build_hdf5_mapping.py read a column's unit off the END OF ITS NAME. 91 of 162
numeric columns fell through the suffix table and were published as
"unresolved" -- including every heat-source density, every gas density and
every entropy in nats, all of which state their unit in their own name. Three
did not fall through and came out wrong: gradient_K_per_m was published as
METRES because it ends in "_m", and the two dose fluxes as SECONDS because
they end in "_s". A wrong unit is worse than none; a consumer believes it.

The tests below pair every refusal with a control, because a checker that
refuses everything would satisfy the refusals alone.

MODEL-ONLY / FORECAST-ONLY. No scientific value is asserted here.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import unit_inventory as ui  # noqa: E402
import build_hdf5_mapping as bhm  # noqa: E402


def _mapping():
    return json.loads(open(os.path.join(ROOT, "hdf5_output_mapping.json"),
                           encoding="utf-8").read())


def _declared():
    return json.loads(open(os.path.join(ROOT, "docs/unit_inventory.json"),
                           encoding="utf-8").read())


def _columns():
    return ui.governed_numeric_columns(_mapping())


def test_there_are_columns_to_reconcile():
    cols = _columns()
    assert len(cols) > 100, f"only {len(cols)} numeric columns enumerated"


def test_every_governed_numeric_column_is_declared():
    assert ui.reconcile(_columns(), _declared()["columns"]) == []


def test_no_column_is_published_without_a_dimension():
    bad = [f"{o['path']}:{c['name']}"
           for o in _mapping()["outputs"]
           for c in (o.get("columns") or [])
           if c.get("dtype") == "float64"
           and (not c.get("unit") or c["unit"] == "unresolved")]
    assert not bad, bad


def test_the_word_unresolved_is_gone_from_the_vocabulary():
    d = _declared()
    assert "unresolved" not in d["vocabulary"]
    assert not [k for k, v in d["columns"].items() if v["unit"] == "unresolved"]


@pytest.mark.parametrize("key,unit", [
    # The three the suffix rule got WRONG, pinned by value. "_m" and "_s" are
    # the last characters of these names and not their dimensions.
    ("distributed_thermal_profile.csv:gradient_K_per_m", "K/m"),
    ("gas_transport_2d_map.csv:dose_flux_open_m2_s", "1/(m^2 s)"),
    ("gas_transport_2d_map.csv:dose_flux_closed_m2_s", "1/(m^2 s)"),
    # ... and a sample of the ones it lost entirely, whose units are written
    # in their own names.
    ("distributed_thermal_profile.csv:Q_laser_W_m3", "W/m^3"),
    ("distributed_thermal_profile.csv:heat_flux_W_m2", "W/m^2"),
    ("gas_transport_profile.csv:n_CH4_modeC_1m3", "1/m^3"),
    ("expected_information_gain.csv:eig_nats", "nats"),
    ("failed_gate_samples.csv:Ts_mK", "mK"),
    ("failed_gate_samples.csv:eps_pct", "percent"),
    ("tau_c_sweep.csv:DeltaGamma_rads", "rad/s"),
])
def test_the_declared_dimension_is_the_one_published(key, unit):
    assert _declared()["columns"][key]["unit"] == unit
    src, col = key.split(":", 1)
    got = next(c["unit"] for o in _mapping()["outputs"] if o["path"] == src
               for c in o["columns"] if c["name"] == col)
    assert got == unit


def test_a_rank_scale_score_is_not_a_time_or_a_currency():
    # cost, duration and risk are {1,2,3} difficulty scores. A suffix rule
    # would have been free to call `duration` seconds, and ORDINAL is the word
    # that stops anyone reading it that way.
    cols = _declared()["columns"]
    for c in ("cost", "duration", "risk"):
        assert cols[f"experimental_design_candidates.csv:{c}"]["unit"] == "ORDINAL"
    assert "NOT currency or time" in _declared()["vocabulary"]["ORDINAL"]


def test_a_long_format_table_names_the_column_that_carries_its_unit():
    cols = _declared()["columns"]
    e = cols["measurement_comparison_rows.csv:measured"]
    assert e["unit"] == "PER_ROW" and e["unit_from"] == "units"
    e = cols["mesh_convergence_summary.csv:value"]
    assert e["unit"] == "PER_ROW" and e["unit_from"] == "metric"


# ------------------------------------------------------- the refusals

def test_an_undeclared_column_is_refused():
    declared = dict(_declared()["columns"])
    declared.pop("gas_transport_profile.csv:x_m")
    problems = ui.reconcile(_columns(), declared)
    assert any("no declared dimension" in p for p in problems), problems


def test_an_entry_for_a_column_that_no_longer_exists_is_refused():
    declared = dict(_declared()["columns"])
    declared["gone.csv:ghost"] = {"unit": "K"}
    problems = ui.reconcile(_columns(), declared)
    assert any("has moved on" in p for p in problems), problems


def test_a_dimension_of_unresolved_is_refused():
    declared = dict(_declared()["columns"])
    declared["gas_transport_profile.csv:x_m"] = {"unit": "unresolved"}
    problems = ui.reconcile(_columns(), declared)
    assert any("'unresolved'" in p for p in problems), problems


def test_a_per_row_entry_must_name_a_column_that_exists():
    declared = dict(_declared()["columns"])
    declared["mesh_convergence_summary.csv:value"] = {
        "unit": "PER_ROW", "unit_from": "no_such_column"}
    problems = ui.reconcile(_columns(), declared)
    assert any("not a column of" in p for p in problems), problems
    # ... and PER_ROW with no carrier at all.
    declared["mesh_convergence_summary.csv:value"] = {"unit": "PER_ROW"}
    problems = ui.reconcile(_columns(), declared)
    assert any("names no column" in p for p in problems), problems


def test_a_quantity_may_not_be_declared_a_pure_number():
    declared = dict(_declared()["columns"])
    declared["thermal_3d_hotspots.csv:T_peak_K"] = {"unit": "DIMENSIONLESS"}
    problems = ui.reconcile(_columns(), declared)
    assert any("pure number" in p for p in problems), problems
    # The control: a coverage fraction IS dimensionless and must not trip it.
    declared = dict(_declared()["columns"])
    assert declared["surface_coverage_2d_map.csv:theta_CH4"]["unit"] == "DIMENSIONLESS"
    assert ui.reconcile(_columns(), declared) == []


def test_a_reconciliation_over_nothing_is_refused():
    with pytest.raises(ui.ScopeError):
        ui.reconcile([], _declared()["columns"])
    with pytest.raises(ui.ScopeError):
        ui.reconcile(_columns(), {})
    ui.reconcile(_columns(), _declared()["columns"])  # the control


def test_the_mapping_builder_refuses_an_undeclared_column():
    inv = _declared()["columns"]
    with pytest.raises(bhm.UndeclaredUnit):
        bhm.unit_of("gas_transport_profile.csv", "a_column_nobody_declared", inv)
    # The control: a declared one answers, and answers correctly.
    assert bhm.unit_of("gas_transport_profile.csv", "x_m", inv) == "m"
    assert bhm.unit_of("mesh_convergence_summary.csv", "value",
                       inv) == "PER_ROW:metric"


def test_the_equivalence_report_divides_the_columns_it_gates():
    rep = json.loads(open(
        os.path.join(ROOT, "stage8_reports/hdf5_equivalence_report.json"),
        encoding="utf-8").read())
    assert "unresolved_unit_columns" not in rep, \
        "the field that counted 91 and gated nothing is still here"
    total = (rep["columns_with_a_physical_unit"]
             + rep["columns_declared_without_a_unit"])
    assert total == len(_columns()), (total, len(_columns()))
    assert rep["columns_with_a_physical_unit"] > 50
