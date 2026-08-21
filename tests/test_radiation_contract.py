"""§10 regression: the radiative-path producer/consumer field contract.

Two governed budget artifacts consumed ``radiation_paths`` through field names
the producer never emitted:

  * ``cryo_stack_3d_budget.csv`` used ``p.get("path", p.get("hop",
    "stage_hop"))`` and ``p.get("load_W", ...)`` -- every intercept row was
    written as "stage_hop" with an empty heat load.
  * ``radiation_paths_3d_budget.csv`` emitted its aggregate row with
    ``path``/``load_W`` while the per-path rows used ``path_name``/
    ``heat_load_W`` -- the total radiative load reaching 10 mK was blank.

These tests pin the declared contract and prove both consumers now carry real
values end to end.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics import cryo_stack_3d, radiation_paths_3d  # noqa: E402
from qta_multiphysics.radiation_paths import (  # noqa: E402
    RADIATION_METRIC_FIELDS, RADIATION_PATH_FIELDS, RadiationSchemaError,
    radiation_paths, validate_radiation_paths)


# ---------------------------------------------------------------- producer --

def test_producer_emits_exactly_the_declared_path_fields():
    paths, _ = radiation_paths()
    assert paths
    for p in paths:
        assert tuple(p) == RADIATION_PATH_FIELDS, tuple(p)


def test_producer_emits_the_declared_metric_fields():
    _, m = radiation_paths()
    for k in RADIATION_METRIC_FIELDS:
        assert k in m, k


def test_validator_accepts_the_real_producer_output():
    validate_radiation_paths(*radiation_paths())


# ---------------------------------------------------------------- negative --

def _expect_schema_error(paths, metrics, needle):
    try:
        validate_radiation_paths(paths, metrics)
    except RadiationSchemaError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
    else:
        raise AssertionError(f"schema drift accepted ({needle})")


def test_renamed_path_field_fails_closed():
    """The exact drift the consumers papered over with .get() aliases."""
    paths, m = radiation_paths()
    paths[0] = {("path" if k == "path_name" else k): v
                for k, v in paths[0].items()}
    _expect_schema_error(paths, m, "path_name")


def test_renamed_load_field_fails_closed():
    paths, m = radiation_paths()
    paths[0] = {("load_W" if k == "heat_load_W" else k): v
                for k, v in paths[0].items()}
    _expect_schema_error(paths, m, "heat_load_W")


def test_empty_path_name_fails_closed():
    paths, m = radiation_paths()
    paths[0]["path_name"] = "  "
    _expect_schema_error(paths, m, "empty path_name")


def test_non_finite_load_fails_closed():
    paths, m = radiation_paths()
    paths[0]["heat_load_W"] = float("nan")
    _expect_schema_error(paths, m, "non-finite")


def test_missing_total_fails_closed():
    paths, m = radiation_paths()
    del m["total_heat_load_to_10mK_W"]
    _expect_schema_error(paths, m, "total_heat_load_to_10mK_W")


def test_no_paths_fails_closed():
    _, m = radiation_paths()
    _expect_schema_error([], m, "no radiative paths")


# ------------------------------------------------- consumer: cryo_stack_3d --

def test_cryo_stack_intercept_rows_carry_real_names_and_loads():
    rows, _ = cryo_stack_3d.budget_rows()
    intercepts = [r for r in rows
                  if r["element"].startswith("radiative_intercept:")]
    assert len(intercepts) == 6, len(intercepts)
    for r in intercepts:
        name = r["element"].split(":", 1)[1]
        assert name and name != "stage_hop", r
        assert "->" in name, r
        assert r["forecast_load_W"] != "", f"blank governed heat load: {r}"
        assert math.isfinite(float(r["forecast_load_W"]))
        assert r["stage"] not in ("", "stage_hop"), r


def test_cryo_stack_intercept_loads_match_the_producer():
    paths, _ = radiation_paths()
    rows, _ = cryo_stack_3d.budget_rows()
    by_name = {r["element"].split(":", 1)[1]: r["forecast_load_W"]
               for r in rows if r["element"].startswith("radiative_intercept:")}
    for p in paths:
        assert by_name[p["path_name"]] == f'{float(p["heat_load_W"]):.9e}'


def test_cryo_stack_total_row_is_populated():
    rows, _ = cryo_stack_3d.budget_rows()
    tot = [r for r in rows if r["element"] == "radiative_total_to_10mK"]
    assert len(tot) == 1
    assert tot[0]["forecast_load_W"] != ""


# --------------------------------------------- consumer: radiation_paths_3d --

def test_budget_rows_all_share_one_schema():
    """No row may introduce a field the header will not carry."""
    rows, _ = radiation_paths_3d.budget_rows()
    for r in rows:
        assert tuple(r) == radiation_paths_3d.BUDGET_FIELDS, tuple(r)


def test_budget_total_row_carries_the_total():
    rows, metrics = radiation_paths_3d.budget_rows()
    tot = [r for r in rows
           if r["row_kind"] == radiation_paths_3d.ROW_KIND_TOTAL]
    assert len(tot) == 1, tot
    assert tot[0]["heat_load_W"] != "", "total radiative load written blank"
    assert float(tot[0]["heat_load_W"]) == float(
        f'{metrics["total_heat_load_to_10mK_W"]:.9e}')


def test_budget_total_is_distinguishable_from_paths():
    """A consumer must be able to exclude the aggregate from a per-hop sum."""
    rows, metrics = radiation_paths_3d.budget_rows()
    paths = [r for r in rows
             if r["row_kind"] == radiation_paths_3d.ROW_KIND_PATH]
    assert len(paths) == 6
    reaching = sum(float(r["heat_load_W"]) for r in paths
                   if str(r["reaches_10mK_region"]) == "True")
    assert math.isclose(reaching, metrics["total_heat_load_to_10mK_W"],
                        rel_tol=1e-9)


def test_budget_survives_csv_roundtrip_without_losing_the_total():
    """End-to-end §9 + §10: the writer must not blank the aggregate row."""
    import csv
    import tempfile
    from qta_multiphysics.exports import write_rows_csv
    rows, metrics = radiation_paths_3d.budget_rows()
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        write_rows_csv(path, rows)
        with open(path, newline="") as f:
            back = list(csv.DictReader(f))
    finally:
        os.unlink(path)
    tot = [r for r in back if r["row_kind"] == "TOTAL"]
    assert len(tot) == 1
    assert tot[0]["heat_load_W"] != "", "total blanked by the CSV writer"
    assert float(tot[0]["heat_load_W"]) > 0.0


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
