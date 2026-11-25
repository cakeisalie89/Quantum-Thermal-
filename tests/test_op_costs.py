"""The per-operation cost instrument (tools/op_costs.py) reports what it says.

It measures from outside, in child processes, so what it must get right is
that every figure is there and is taken around the operation, and that the
traced run is the one the peak comes from. Each is held here at sizes
small enough to run in a second.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import op_costs as OC  # noqa: E402


def test_a_child_reports_every_figure_around_the_operation():
    row = OC.child("append_cold", 5, traced=False)
    assert set(OC.FIGURES) <= set(row)
    assert row["op"] == "append_cold" and row["n"] == 5
    assert row["wall_s"] >= 0 and row["cpu_s"] >= 0
    assert row["disk_bytes"] > 0, "the history it built has a footprint"
    assert row["peak_alloc_bytes"] is None, "untraced runs report no peak"
    if row["read_bytes"] is not None:
        # a cold append verifies the whole log before it writes
        assert row["read_bytes"] >= (row["disk_bytes"] // 2)


def test_the_peak_comes_from_a_traced_run():
    assert OC.child("memory_retrieval", 5, traced=True)[
        "peak_alloc_bytes"] > 0
    row = OC.measure("append_warm", 3)
    assert row["peak_alloc_bytes"] is not None and row["wall_s"] >= 0


def test_growth_is_each_figure_against_the_size_before():
    def row(n, wall):
        return {"op": "x", "n": n, "wall_s": wall, "cpu_s": None,
                "children_cpu_s": 0.0, "read_bytes": 100 * n,
                "disk_bytes": n, "peak_alloc_bytes": None,
                "max_rss_bytes": 1}
    g = OC.growth([row(40, 4.0), row(10, 1.0), row(20, 2.0)])
    assert [(x["from"], x["to"]) for x in g] == [(10, 20), (20, 40)]
    assert [x["ratios"]["wall_s"] for x in g] == [2.0, 2.0]
    assert g[0]["ratios"]["cpu_s"] is None
    assert g[0]["ratios"]["children_cpu_s"] is None, "nothing to divide by"


def test_an_unknown_operation_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        OC.setup("nothing", 3, tmp_path)
    with pytest.raises(SystemExit):
        OC.main(["--ops", "nothing"])
