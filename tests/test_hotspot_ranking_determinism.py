"""The hotspot rank must not be decided by digits the file does not print.

D-2026-49. A symmetric beam heats several cells to the same peak by
construction and the model does not compute them equal -- they differ around
1e-14 relative because each accumulates its reduction in a different order.
Ranking on the raw value ranked them by that rounding error, so a host with a
different BLAS kernel produced different coordinates with identical reported
temperatures.

These tests use a stand-in carrying only what `hotspot_rows` touches, so the
property can be stated directly instead of inferred from a four-minute
regeneration on two dispatches.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from qta_multiphysics.thermal_3d_transient import PEAK_FMT, Thermal3DResult

hotspot_rows = Thermal3DResult.hotspot_rows


def _stub(peaks):
    """A result whose cell i has peak temperature ``peaks[i]``."""
    n = len(peaks)
    return SimpleNamespace(
        T=np.array([[float(v)] for v in peaks]),
        t=np.array([0.0]),
        _shape=(n, 1, 1),
        grid=SimpleNamespace(xc=np.arange(n, dtype=float),
                             yc=np.array([0.0]), zc=np.array([0.0])),
    )


#: Four peaks that PRINT identically and are four distinct float64 values,
#: deliberately scrambled so "ascending index" and "ascending value" differ.
#: The magnitudes mirror the measured quartet: ~1e-14 relative apart.
_BASE = 13.73015220
_INDISTINGUISHABLE = [_BASE + 2e-13, _BASE, _BASE + 3e-13, _BASE + 1e-13]


def test_the_fixture_is_actually_indistinguishable_as_printed():
    """Guard. If formatting ever changed so these render differently, every
    test below would still pass while measuring nothing."""
    rendered = {f"{v:{PEAK_FMT}}" for v in _INDISTINGUISHABLE}
    assert len(rendered) == 1, rendered
    assert len(set(_INDISTINGUISHABLE)) == 4, "fixture must be 4 distinct floats"


def test_peaks_that_print_the_same_are_ranked_by_cell_index():
    rows = hotspot_rows(_stub(_INDISTINGUISHABLE), top_n=4)
    x = [float(r["x_m"]) for r in rows]
    assert x == [0.0, 1.0, 2.0, 3.0], (
        "cells whose reported peak is identical must rank by a fact about "
        f"the grid, not by rounding error; got {x}")


def test_the_ranking_still_follows_the_value_when_the_value_is_visible():
    """The control. Without it the test above passes on an implementation
    that ignores the temperatures entirely and always returns index order."""
    spread = [10.0, 40.0, 20.0, 30.0]
    rows = hotspot_rows(_stub(spread), top_n=4)
    assert [float(r["T_peak_K"]) for r in rows] == [40.0, 30.0, 20.0, 10.0]


def test_reordering_the_sub_printed_noise_does_not_reorder_the_output():
    """The property a second host exercises, stated directly: permuting
    differences that live below the reported resolution changes nothing."""
    a = hotspot_rows(_stub(_INDISTINGUISHABLE), top_n=4)
    shuffled = [_BASE + d for d in (1e-13, 3e-13, 0.0, 2e-13)]
    b = hotspot_rows(_stub(shuffled), top_n=4)
    assert [r["x_m"] for r in a] == [r["x_m"] for r in b]
    assert [r["T_peak_K"] for r in a] == [r["T_peak_K"] for r in b]


def test_the_reported_peak_and_the_sort_key_share_one_format():
    """PEAK_FMT is used for both, so a rank can never be decided by a digit
    the file does not show. Checked on the value the rows actually carry."""
    rows = hotspot_rows(_stub(_INDISTINGUISHABLE), top_n=1)
    assert rows[0]["T_peak_K"] == f"{_INDISTINGUISHABLE[0]:{PEAK_FMT}}"
