"""The instrument that tells a changed verdict from a changed digit.

Every refusal test here is paired with a positive control. A comparator that
refused everything would pass all the refusal tests and be worthless, and a
comparator that refused nothing would pass all the acceptance tests and be
worse than worthless -- it would license the sentence "no decision changed".

The comparator answers ONE question -- cross-environment decision stability
-- and every report says scientific equivalence is NOT ESTABLISHED.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from tools.cross_env_semantics import (
    DECISION, DECISION_DRIFT, DECISION_STABLE, DISCRETE, IDENTICAL, MEASURED,
    NONFINITE, NOT_NEEDED_BYTE_IDENTICAL, PRECISION, RESOLUTION_AMBIGUITY,
    SIGN_FLIP_BARE, SIGN_FLIP_BOUND, STRUCTURAL, STRUCTURAL_DRIFT,
    UNCLASSIFIED, UNCLASSIFIED_DIVERGENCE, ZERO_CROSSING_BARE,
    ZERO_CROSSING_BOUND, ScopeError, check_scope, compare, declared_scope,
    main, number, structured, summary_line,
)

ROOT = Path(__file__).resolve().parents[1]
B, R, E = "BELOW_RESOLUTION", "RESOLVED", "EXACT_ZERO"
EMPTY_INV: dict = {"columns": {}}


def _write(root: Path, name: str, payload):
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    if isinstance(payload, str):
        p.write_text(payload)
    else:
        p.write_text(json.dumps(payload, indent=2))
    return p


def _csv(rows) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def _pair(tmp_path, name, committed, other, *, inv=EMPTY_INV, extra=None):
    """One declared artefact on each side (plus ``extra`` identical ones)."""
    a, b = tmp_path / "committed", tmp_path / "other"
    _write(a, name, committed)
    _write(b, name, other)
    declared = {name}
    for n, v in (extra or {}).items():
        _write(a, n, v)
        _write(b, n, v)
        declared.add(n)
    return compare(b, a, declared=declared, exempt=frozenset(),
                   inventory=inv)


def _classes(report):
    return sorted(f["class"] for f in report["findings"])


# ---- tokens -----------------------------------------------------------------
def test_a_number_is_a_whole_token():
    assert number("1.25e-3") == ("float", 1.25e-3)
    assert number("-7") == ("int", -7)
    assert number("nan")[0] == "float"
    for text in ("model_v2", "1.0 K", "v1.2", "3e", "", " 1.0", "True"):
        assert number(text) is None, text
    assert number(True) is None and number(None) is None


def test_structured_cells_are_parsed_safely_not_evaluated():
    assert structured("{'ok': True, 'x': 1.5}") == {"ok": True, "x": 1.5}
    assert structured('{"a": [1, 2]}') == {"a": [1, 2]}
    assert structured("[__import__('os')]") is None
    assert structured("plain text") is None


# ---- decisions --------------------------------------------------------------
def test_a_changed_status_is_a_decision(tmp_path):
    r = _pair(tmp_path, "s.json", {"status": "CONDITIONAL"},
              {"status": "PASS"})
    assert _classes(r) == [DECISION] and r["status"] == DECISION_DRIFT


def test_a_changed_boolean_is_a_decision(tmp_path):
    r = _pair(tmp_path, "s.json", {"ok": True}, {"ok": False})
    assert _classes(r) == [DECISION]


def test_a_version_label_is_not_a_digit(tmp_path):
    """The hostile fixture the old digit-stripping comparator got wrong:
    ``model_v2`` minus its digits equals ``model_v3`` minus its digits."""
    r = _pair(tmp_path, "m.csv", _csv([["model"], ["model_v2"]]),
              _csv([["model"], ["model_v3"]]))
    assert _classes(r) == [DECISION]


def test_a_mode_label_is_a_decision(tmp_path):
    r = _pair(tmp_path, "m.csv", _csv([["mode"], ["Mode_D"]]),
              _csv([["mode"], ["Mode_C"]]))
    assert _classes(r) == [DECISION]


def test_text_that_carries_a_number_is_compared_as_text(tmp_path):
    r = _pair(tmp_path, "m.csv", _csv([["note"], ["theta=1.686e-10 (ok)"]]),
              _csv([["note"], ["theta=1.687e-10 (ok)"]]))
    assert _classes(r) == [DECISION]


def test_a_unit_or_a_resolution_class_that_changes_is_a_decision(tmp_path):
    r = _pair(tmp_path, "m.csv", _csv([["unit", "cls"], ["K", E]]),
              _csv([["unit", "cls"], ["mK", B]]))
    assert _classes(r) == [DECISION, DECISION]


def test_a_boolean_inside_a_structured_cell_is_a_decision(tmp_path):
    r = _pair(tmp_path, "m.csv", _csv([["d"], ["{'ok': True, 'x': 1.5}"]]),
              _csv([["d"], ["{'ok': False, 'x': 1.5}"]]))
    assert _classes(r) == [DECISION]


# ---- discrete, non-finite ---------------------------------------------------
def test_an_integer_that_moves_is_a_discrete_change(tmp_path):
    r = _pair(tmp_path, "c.csv", _csv([["n_failed"], ["12"]]),
              _csv([["n_failed"], ["13"]]))
    assert _classes(r) == [DISCRETE] and r["status"] == DECISION_DRIFT


def test_a_column_declared_exact_refuses_any_change(tmp_path):
    inv = {"columns": {"c.csv:x_m": {"basis": "COORDINATE", "why": "mesh"}}}
    r = _pair(tmp_path, "c.csv", _csv([["x_m"], ["3.536778e-05"]]),
              _csv([["x_m"], ["3.536779e-05"]]), inv=inv)
    assert _classes(r) == [DISCRETE]
    control = _pair(tmp_path / "ctl", "c.csv",
                    _csv([["x_m"], ["3.536778e-05"]]),
                    _csv([["x_m"], ["3.536779e-05"]]))
    assert _classes(control) == [PRECISION]


@pytest.mark.parametrize("before, after", [
    (1.5, float("nan")), (1.5, float("inf")), (float("inf"), float("-inf")),
    (float("nan"), 1.0)])
def test_nan_and_infinity_are_never_precision(tmp_path, before, after):
    r = _pair(tmp_path, "n.json", {"x": before}, {"x": after})
    assert _classes(r) == [NONFINITE]


def test_nan_on_both_sides_is_the_same_value(tmp_path):
    r = _pair(tmp_path, "n.json", {"x": float("nan"), "y": 1.0},
              {"x": float("nan"), "y": 1.0000001})
    assert _classes(r) == [PRECISION]


# ---- structure --------------------------------------------------------------
def test_a_key_on_one_side_only_is_refused_not_reported(tmp_path):
    r = _pair(tmp_path, "k.json", {"a": 1.0}, {"a": 1.0, "b": 2.0})
    assert _classes(r) == [STRUCTURAL] and r["status"] == STRUCTURAL_DRIFT


def test_a_list_that_changes_length_is_structural(tmp_path):
    r = _pair(tmp_path, "l.json", {"a": [1.0, 2.0]}, {"a": [1.0]})
    assert _classes(r) == [STRUCTURAL]


def test_a_changed_type_is_structural(tmp_path):
    r = _pair(tmp_path, "t.json", {"a": 1}, {"a": 1.0})
    assert _classes(r) == [STRUCTURAL]
    r = _pair(tmp_path / "2", "t.json", {"a": "1.0"}, {"a": "one"})
    assert _classes(r) == [STRUCTURAL]


def test_a_structured_cell_that_becomes_plain_text_is_structural(tmp_path):
    """A cell that carried a container on one side and a word on the other
    has changed what it IS, not what it says."""
    r = _pair(tmp_path, "m.csv", _csv([["d"], ["{'ok': True}"]]),
              _csv([["d"], ["ok"]]))
    assert _classes(r) == [STRUCTURAL]


def test_a_changed_csv_header_or_row_count_is_structural(tmp_path):
    r = _pair(tmp_path, "h.csv", _csv([["a", "b"], ["1.0", "2.0"]]),
              _csv([["a", "c"], ["1.0", "2.0"]]))
    assert _classes(r) == [STRUCTURAL]
    r = _pair(tmp_path / "2", "h.csv", _csv([["a"], ["1.0"], ["2.0"]]),
              _csv([["a"], ["1.0"]]))
    assert _classes(r) == [STRUCTURAL]


def test_a_missing_declared_artefact_is_structural(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "x.json", {"v": 1.0})
    _write(a, "y.json", {"v": 1.0})
    _write(b, "x.json", {"v": 1.0})
    r = compare(b, a, declared={"x.json", "y.json"}, exempt=frozenset(),
                inventory=EMPTY_INV)
    assert r["files"]["y.json"] == "MISSING_OTHER"
    assert r["status"] == STRUCTURAL_DRIFT


def test_a_foreign_artefact_is_structural(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "x.json", {"v": 1.0})
    _write(b, "x.json", {"v": 1.0})
    _write(b, "stray.json", {"v": 1.0})
    r = compare(b, a, declared={"x.json"}, exempt=frozenset(),
                inventory=EMPTY_INV)
    assert r["files"]["stray.json"] == "FOREIGN"
    assert r["status"] == STRUCTURAL_DRIFT


# ---- unclassified -----------------------------------------------------------
def test_a_differing_artefact_it_cannot_parse_is_unclassified(tmp_path):
    r = _pair(tmp_path, "d.mmd", "graph A-->B\n", "graph A-->C\n")
    assert _classes(r) == [UNCLASSIFIED]
    assert r["status"] == UNCLASSIFIED_DIVERGENCE


def test_bytes_that_do_not_parse_are_unclassified(tmp_path):
    r = _pair(tmp_path, "d.json", '{"a": 1}', '{"a": 1,')
    assert _classes(r) == [UNCLASSIFIED]


def test_a_duplicated_json_key_is_not_silently_resolved(tmp_path):
    r = _pair(tmp_path, "d.json", '{"a": 1}', '{"a": 1, "a": 2}')
    assert _classes(r) == [UNCLASSIFIED]


def test_the_same_value_in_different_text_is_not_guessed_at(tmp_path):
    r = _pair(tmp_path, "d.csv", _csv([["x"], ["1.0e3"]]),
              _csv([["x"], ["1000.0"]]))
    assert _classes(r) == [UNCLASSIFIED]


# ---- precision, and its control ---------------------------------------------
def test_a_moved_last_digit_is_precision_and_is_permitted(tmp_path):
    r = _pair(tmp_path, "p.csv", _csv([["q"], ["108740.08984348577"]]),
              _csv([["q"], ["108740.08984349713"]]))
    assert _classes(r) == [PRECISION]
    assert r["status"] == DECISION_STABLE
    assert r["max_precision_rel"] < 1e-12


def test_precision_inside_a_structured_cell_is_precision(tmp_path):
    r = _pair(tmp_path, "m.csv", _csv([["d"], ["{'ok': True, 'x': 1.5}"]]),
              _csv([["d"], ["{'ok': True, 'x': 1.5000001}"]]))
    assert _classes(r) == [PRECISION]


# ---- zero crossings and sign flips: bound or bare ---------------------------
def test_an_unbound_zero_crossing_is_bare(tmp_path):
    r = _pair(tmp_path, "z.csv", _csv([["q"], ["0.0"]]),
              _csv([["q"], ["1e-9"]]))
    assert _classes(r) == [ZERO_CROSSING_BARE]
    assert r["status"] == RESOLUTION_AMBIGUITY


WIDE = {"columns": {"w.csv:n": {"basis": "CARRIER", "resolution_from":
                                "n_resolution"}}}


def test_a_crossing_bound_below_resolution_on_both_sides_is_permitted(
        tmp_path):
    r = _pair(tmp_path, "w.csv", _csv([["n", "n_resolution"], ["0.0", B]]),
              _csv([["n", "n_resolution"], ["4e-9", B]]), inv=WIDE)
    assert _classes(r) == [ZERO_CROSSING_BOUND]
    assert r["status"] == DECISION_STABLE


def test_a_class_for_one_quantity_does_not_bind_another(tmp_path):
    """CASE 18: the resolution is quantity A's; the crossing is B's."""
    rows_a = [["n", "n_resolution", "m"], ["1.0", B, "0.0"]]
    rows_b = [["n", "n_resolution", "m"], ["1.0", B, "4e-9"]]
    r = _pair(tmp_path, "w.csv", _csv(rows_a), _csv(rows_b), inv=WIDE)
    assert _classes(r) == [ZERO_CROSSING_BARE]


def test_exact_zero_against_below_resolution_is_a_decision(tmp_path):
    """CASE 19: the class itself moved -- "absent by design" became "less
    than we can see" -- which is a changed claim, and the crossing it
    carries is not permitted either."""
    r = _pair(tmp_path, "w.csv", _csv([["n", "n_resolution"], ["0.0", E]]),
              _csv([["n", "n_resolution"], ["4e-9", B]]), inv=WIDE)
    assert _classes(r) == [DECISION, ZERO_CROSSING_BARE]


def test_a_class_that_says_resolved_does_not_permit_a_crossing(tmp_path):
    r = _pair(tmp_path, "w.csv", _csv([["n", "n_resolution"], ["0.0", R]]),
              _csv([["n", "n_resolution"], ["4e-9", R]]), inv=WIDE)
    assert _classes(r) == [ZERO_CROSSING_BARE]


FLOORED = {"columns": {"w.csv:n": {"basis": "CARRIER",
                                   "resolution_from": "n_resolution",
                                   "floor_from": "floor"},
                       "w.csv:floor": {"basis": "FLOOR", "why": "floor"}}}


def test_inside_a_bound_floor_the_crossing_is_permitted(tmp_path):
    """CASE 20: 0 against a tiny value, both classed BELOW_RESOLUTION, both
    inside the floor bound beside them."""
    hdr = ["n", "n_resolution", "floor"]
    r = _pair(tmp_path, "w.csv", _csv([hdr, ["0.0", B, "1e-6"]]),
              _csv([hdr, ["4e-9", B, "1e-6"]]), inv=FLOORED)
    assert _classes(r) == [ZERO_CROSSING_BOUND]


def test_a_value_outside_its_bound_floor_is_bare_whatever_the_class(
        tmp_path):
    hdr = ["n", "n_resolution", "floor"]
    r = _pair(tmp_path, "w.csv", _csv([hdr, ["0.0", B, "1e-6"]]),
              _csv([hdr, ["4e-3", B, "1e-6"]]), inv=FLOORED)
    assert _classes(r) == [ZERO_CROSSING_BARE]


@pytest.mark.parametrize("floor", ["0.0", "-1e-6", "inf", "nan", "wide"])
def test_a_floor_that_is_not_a_positive_number_authorises_nothing(tmp_path,
                                                                  floor):
    """An infinite floor would put every value inside it."""
    hdr = ["n", "n_resolution", "floor"]
    r = _pair(tmp_path, "w.csv", _csv([hdr, ["0.0", B, floor]]),
              _csv([hdr, ["4e-9", B, floor]]), inv=FLOORED)
    assert _classes(r) == [ZERO_CROSSING_BARE]


def test_a_sign_flip_is_permitted_only_inside_a_bound_class(tmp_path):
    bare = _pair(tmp_path, "s.csv", _csv([["q"], ["-1e-9"]]),
                 _csv([["q"], ["2e-9"]]))
    assert _classes(bare) == [SIGN_FLIP_BARE]
    bound = _pair(tmp_path / "b", "w.csv",
                  _csv([["n", "n_resolution"], ["-1e-9", B]]),
                  _csv([["n", "n_resolution"], ["2e-9", B]]), inv=WIDE)
    assert _classes(bound) == [SIGN_FLIP_BOUND]


LONG = {"columns": {}, "row_bindings": {"m.csv": {
    "key_column": "metric", "value_column": "value",
    "floor_row": "floor", "bindings": {"residual_m3": "residual_resolution"}}}}


def test_a_long_table_binds_by_row(tmp_path):
    def t(v):
        return _csv([["metric", "value"], ["residual_m3", v],
                     ["residual_resolution", B], ["floor", "1000.0"],
                     ["other_m3", v]])
    r = _pair(tmp_path, "m.csv", t("0.0"), t("4e-9"), inv=LONG)
    assert sorted(f["key"] for f in r["findings"]) == \
        ["r1[residual_m3]", "r4[other_m3]"]
    assert {f["key"]: f["class"] for f in r["findings"]} == {
        "r1[residual_m3]": ZERO_CROSSING_BOUND,
        "r4[other_m3]": ZERO_CROSSING_BARE}


JSONB = {"columns": {}, "json_bindings": {"s.json": {
    ".state.gas.*": ".state.gas_resolution.*",
    ".metrics.r": ".metrics.r_resolution"}}}


def test_a_json_leaf_binds_to_its_own_class(tmp_path):
    def doc(ch4, r):
        return {"state": {"gas": {"CH4": ch4, "H2": 1e11},
                          "gas_resolution": {"CH4": B, "H2": R},
                          "other": {"CH4": ch4}},
                "metrics": {"r": r, "r_resolution": B}}
    rep = _pair(tmp_path, "s.json", doc(0.0, 0.0), doc(4e-9, 4e-9),
                inv=JSONB)
    got = {f["key"]: f["class"] for f in rep["findings"]}
    assert got == {".state.gas.CH4": ZERO_CROSSING_BOUND,
                   ".metrics.r": ZERO_CROSSING_BOUND,
                   ".state.other.CH4": ZERO_CROSSING_BARE}, got


# ---- scope ------------------------------------------------------------------
def test_zero_files_compared_is_refused(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    r = compare(b, a, declared={"x.json"}, exempt=frozenset(),
                inventory=EMPTY_INV)
    with pytest.raises(ScopeError):
        check_scope(r)


def test_an_empty_declaration_is_refused(tmp_path):
    with pytest.raises(ScopeError, match="empty"):
        compare(tmp_path, tmp_path, declared=set(), exempt=frozenset(),
                inventory=EMPTY_INV)


def test_an_exemption_must_name_a_declared_artefact(tmp_path):
    with pytest.raises(ScopeError, match="undeclared"):
        compare(tmp_path, tmp_path, declared={"x.json"},
                exempt=frozenset({"y.json"}), inventory=EMPTY_INV)


def test_an_exempt_artefact_is_recorded_and_its_control_is_refused(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "x.json", {"status": "A"})
    _write(b, "x.json", {"status": "B"})
    _write(a, "y.json", {"v": 1.0})
    _write(b, "y.json", {"v": 1.0})
    r = compare(b, a, declared={"x.json", "y.json"},
                exempt=frozenset({"x.json"}), inventory=EMPTY_INV)
    assert r["exempted"] == ["x.json"] and r["status"] == \
        NOT_NEEDED_BYTE_IDENTICAL
    control = compare(b, a, declared={"x.json", "y.json"},
                      exempt=frozenset(), inventory=EMPTY_INV)
    assert control["status"] == DECISION_DRIFT


def test_identical_trees_say_what_they_establish(tmp_path):
    r = _pair(tmp_path, "i.json", {"v": 1.0}, {"v": 1.0})
    assert r["basis"] == IDENTICAL and r["files_differing"] == 0
    assert r["status"] == NOT_NEEDED_BYTE_IDENTICAL
    m = _pair(tmp_path / "m", "i.json", {"v": 1.0}, {"v": 1.1})
    assert m["basis"] == MEASURED


def test_every_report_says_scientific_equivalence_is_not_established(
        tmp_path):
    for r in (_pair(tmp_path, "a.json", {"v": 1.0}, {"v": 1.0}),
              _pair(tmp_path / "2", "a.json", {"v": 1.0}, {"v": 1.1})):
        assert r["scientific_equivalence"] == "NOT_ESTABLISHED"
        assert "SCIENTIFIC_EQUIVALENCE_STATUS=NOT_ESTABLISHED" in \
            summary_line(r)


# ---- precedence of the status -----------------------------------------------
def test_structural_outranks_decision_outranks_ambiguity(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "x.json", {"k": 1.0, "s": "A", "z": 0.0})
    _write(b, "x.json", {"k": 1.0, "s": "B", "z": 1e-9, "extra": 1})
    r = compare(b, a, declared={"x.json"}, exempt=frozenset(),
                inventory=EMPTY_INV)
    assert r["status"] == STRUCTURAL_DRIFT
    _write(b, "x.json", {"k": 1.0, "s": "B", "z": 1e-9})
    assert compare(b, a, declared={"x.json"}, exempt=frozenset(),
                   inventory=EMPTY_INV)["status"] == DECISION_DRIFT
    _write(b, "x.json", {"k": 1.0, "s": "A", "z": 1e-9})
    assert compare(b, a, declared={"x.json"}, exempt=frozenset(),
                   inventory=EMPTY_INV)["status"] == RESOLUTION_AMBIGUITY


# ---- the committed tree -----------------------------------------------------
def test_the_declared_scope_is_the_byte_gates_own(tmp_path):
    declared, exempt = declared_scope(ROOT)
    assert len(declared) >= 80
    assert exempt == {"deep_surrogate_readiness.json"}
    assert exempt <= declared


def _output_set_copy(tmp_path):
    """The committed canonical outputs, copied as a regeneration would lay
    them out: exactly the declared set, nothing else."""
    other = tmp_path / "other"
    other.mkdir()
    declared, _ = declared_scope(ROOT)
    for name in declared:
        (other / name).write_bytes((ROOT / name).read_bytes())
    return other


def test_the_committed_outputs_against_themselves_are_identical(tmp_path,
                                                                 capsys):
    assert main([str(_output_set_copy(tmp_path)), str(ROOT)]) == 0
    out = capsys.readouterr().out
    assert "CROSS_ENV_STATUS=NOT_NEEDED_BYTE_IDENTICAL" in out


def test_the_cli_exits_nonzero_on_drift(tmp_path, capsys):
    other = _output_set_copy(tmp_path)
    doc = json.loads((other / "coupled_mode_state_summary.json").read_text())
    doc["metrics"]["Mode_D_readiness_status"] = "FORECAST_READY_IF_MEASURED"
    (other / "coupled_mode_state_summary.json").write_text(json.dumps(doc))
    assert main([str(other), str(ROOT)]) == 1
    assert "CROSS_ENV_STATUS=DECISION_DRIFT" in capsys.readouterr().out
