"""Every governed CSV must have one stable, declared, non-empty header.

Two defects motivated this. radiation_paths_3d_budget.csv lost its TOTAL row's
values because the shared writer took the header from rows[0]. Then
failed_gate_samples.csv turned out to write a different header depending on
whether the Monte-Carlo run produced any failures, so a governed artifact's
schema depended on a stochastic outcome.

Both were found one file at a time. This checks the property across every
tracked CSV instead: a header exists, it is unique and non-blank, every data
row has exactly the declared width, and the file parses under strict settings.
It also pins the two schemas that were previously wrong, so a regression there
is named rather than merely counted.

MODEL-ONLY / FORECAST-ONLY. No scientific value is asserted here.
"""
import csv
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _tracked_csvs():
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "*.csv"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split() if not p.startswith("attic/")]


def _read(rel):
    with open(os.path.join(ROOT, rel), newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return rows


#: Schemas that were previously wrong, pinned by name so a regression is
#: identified rather than merely counted. Field order is part of the contract.
PINNED_SCHEMAS = {
    "failed_gate_samples.csv": [
        "tc_us", "Ge_WK", "Cc", "T2s_us", "ea", "Ts_mK", "SNR", "eps_pct",
        "dominant_failure", "g_d10", "g_d3", "g_d13", "g_d18"],
    "radiation_paths_3d_budget.csv": [
        "path_name", "source_stage", "sink_stage", "heat_load_W",
        "attenuation_factor", "shutter_state", "baffle_state",
        "reaches_10mK_region", "row_kind", "resolution",
        "three_d_view_factors", "label"],
}


def test_there_are_governed_csvs_to_check():
    assert len(_tracked_csvs()) > 50, "CSV enumeration looks wrong"


def test_every_governed_csv_has_a_non_empty_header():
    bad = []
    for rel in _tracked_csvs():
        rows = _read(rel)
        if not rows:
            bad.append(f"{rel}: file is empty (no header)")
            continue
        header = rows[0]
        if not header or all(not c.strip() for c in header):
            bad.append(f"{rel}: blank header")
    assert not bad, bad


def test_no_governed_csv_declares_a_duplicate_or_blank_column():
    bad = []
    for rel in _tracked_csvs():
        rows = _read(rel)
        if not rows:
            continue
        header = rows[0]
        if any(not c.strip() for c in header):
            bad.append(f"{rel}: blank column name in {header}")
        seen = [c for c in header]
        if len(seen) != len(set(seen)):
            dupes = sorted({c for c in seen if seen.count(c) > 1})
            bad.append(f"{rel}: duplicate columns {dupes}")
    assert not bad, bad


def test_every_row_matches_the_declared_width():
    """A ragged row means the header is not the contract it claims to be."""
    bad = []
    for rel in _tracked_csvs():
        rows = _read(rel)
        if len(rows) < 2:
            continue
        width = len(rows[0])
        for i, r in enumerate(rows[1:], start=2):
            if not r:                      # tolerate a trailing blank line
                continue
            if len(r) != width:
                bad.append(f"{rel}:{i} has {len(r)} fields, header declares {width}")
                break
    assert not bad, bad


def test_no_governed_csv_carries_the_old_placeholder_schema():
    """`empty` was the shared writer's invented column for a zero-row table."""
    bad = []
    for rel in _tracked_csvs():
        rows = _read(rel)
        if rows and [c.strip() for c in rows[0]] == ["empty"]:
            bad.append(rel)
    assert not bad, f"artifacts still carrying the placeholder schema: {bad}"


def test_the_previously_broken_schemas_are_pinned():
    for rel, expected in PINNED_SCHEMAS.items():
        rows = _read(rel)
        assert rows, f"{rel} is empty"
        assert rows[0] == expected, (
            f"{rel} header drifted\n  got:      {rows[0]}\n  expected: {expected}")


def test_the_radiation_budget_total_row_carries_its_total():
    """The regression that started this: a TOTAL row with a blank heat load."""
    with open(os.path.join(ROOT, "radiation_paths_3d_budget.csv"),
              newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    totals = [r for r in rows if r["row_kind"] == "TOTAL"]
    assert len(totals) == 1, f"expected exactly one TOTAL row, got {len(totals)}"
    t = totals[0]
    assert t["heat_load_W"].strip(), "TOTAL row lost its heat load again"
    assert float(t["heat_load_W"]) > 0.0
    paths = [r for r in rows if r["row_kind"] == "PATH"]
    assert paths, "no PATH rows"
    for r in paths:
        assert r["heat_load_W"].strip(), f"PATH row {r['path_name']} lost its load"


def test_governed_csvs_use_deterministic_line_endings():
    """Mixed endings make byte-identity depend on the writing platform."""
    bad = []
    for rel in _tracked_csvs():
        raw = open(os.path.join(ROOT, rel), "rb").read()
        if not raw:
            continue
        crlf = raw.count(b"\r\n")
        lf = raw.count(b"\n") - crlf
        if crlf and lf:
            bad.append(f"{rel}: {crlf} CRLF and {lf} LF endings")
        if raw.count(b"\r") != crlf:
            bad.append(f"{rel}: bare CR present")
    assert not bad, bad


def test_the_declared_writer_rejects_an_undeclared_field():
    """The shared writer must fail closed, not truncate."""
    import tempfile
    from qta_multiphysics.exports import CsvSchemaError, write_rows_csv
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        try:
            write_rows_csv(path, [{"a": 1, "b": 2}], fieldnames=["a"])
        except CsvSchemaError as e:
            assert "b" in str(e)
        else:
            raise AssertionError("an undeclared field was silently discarded")
    finally:
        os.unlink(path)


def test_the_shared_writer_keeps_a_later_row_only_field():
    import tempfile
    from qta_multiphysics.exports import write_rows_csv
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        write_rows_csv(path, [{"a": 1}, {"a": 2, "b": 3}])
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert "b" in rows[0], "a later row's field vanished from the header"
        assert rows[1]["b"] == "3"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    ns = dict(globals())
    for _n, _f in ns.items():
        if _n.startswith("test_") and callable(_f):
            _f()
    print("RESULT: governed CSV schema contracts hold")
