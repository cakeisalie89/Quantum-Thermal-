"""§9 regression: CSV export must never silently drop governed fields.

``write_rows_csv`` derived its header from ``rows[0]`` alone. Any field that
first appeared in a later row was discarded without warning, which blanked the
total radiative load reaching 10 mK in a governed budget artifact. These tests
pin the fail-closed contract.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.exports import (  # noqa: E402
    CsvSchemaError, _union_fieldnames, write_rows_csv)


def _roundtrip(rows, fieldnames=None):
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        write_rows_csv(path, rows, fieldnames=fieldnames)
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    finally:
        os.unlink(path)


def test_later_row_only_field_is_preserved():
    """The exact §9 defect: a summary row whose keys differ from row 0."""
    rows = [{"a": 1, "b": 2}, {"c": 3}]
    out = _roundtrip(rows)
    assert out[1]["c"] == "3", f"later-row field lost: {out}"
    assert out[0]["a"] == "1"


def test_header_is_union_of_all_row_keys():
    rows = [{"a": 1}, {"b": 2}, {"c": 3}]
    assert _union_fieldnames(rows) == ["a", "b", "c"]


def test_union_order_is_first_seen_and_deterministic():
    """Byte-stability: re-deriving from unchanged input must not reorder."""
    rows = [{"z": 1, "a": 2}, {"m": 3, "z": 4}, {"a": 5}]
    assert _union_fieldnames(rows) == ["z", "a", "m"]
    assert _union_fieldnames(rows) == _union_fieldnames(list(rows))


def test_missing_key_in_some_row_is_blank_not_an_error():
    """Heterogeneous rows are legal; only *losing* a column is not."""
    out = _roundtrip([{"a": 1, "b": 2}, {"a": 9}])
    assert out[1]["b"] == ""
    assert out[1]["a"] == "9"


def test_declared_fieldnames_reject_undeclared_row_keys():
    """An explicit header is a contract, not a truncation instruction."""
    try:
        _roundtrip([{"a": 1}, {"a": 2, "surprise": 3}], fieldnames=["a"])
    except CsvSchemaError as e:
        assert "surprise" in str(e)
    else:
        raise AssertionError("undeclared field was silently discarded")


def test_declared_fieldnames_may_be_a_superset():
    out = _roundtrip([{"a": 1}], fieldnames=["a", "b"])
    assert out[0]["b"] == ""


def test_nested_container_fails_closed():
    for bad in ({"a": {"n": 1}}, {"a": [1, 2]}, {"a": (1, 2)}):
        try:
            _roundtrip([bad])
        except CsvSchemaError:
            pass
        else:
            raise AssertionError(f"nested container silently stringified: {bad}")


def test_empty_rows_behaviour_unchanged():
    """Pre-existing governed behaviour; not widened by this fix."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        write_rows_csv(path, [])
        with open(path) as f:
            assert f.read().strip() == "empty"
        write_rows_csv(path, [], fieldnames=["x", "y"])
        with open(path) as f:
            assert f.read().strip() == "x,y"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    ns = dict(globals())
    fails = 0
    for name, fn in sorted(ns.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if fails else 0)
