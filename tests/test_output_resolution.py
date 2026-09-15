"""An output states the resolution of the method that produced it.

gas_transport_profile.csv used to state 0.000000000e+00 for methane in all 120
Mode-C cells. The solve had not produced zero; it had produced noise between
-1.9e+01 and -1.8e-10 inside an integrator whose absolute tolerance is 1e3,
and np.clip turned the sign away. Helium's zero in the same file is exact --
no source, no initial content -- and the two were written identically, so
"residual methane at Mode D entry is zero" and "residual methane is below what
this solve can see" arrived at a reader as the same sentence.

The tests below are paired on purpose. Every refusal has a control that must
NOT refuse, and every "this is unresolved" has a sibling asserting something
IS resolved, because a mechanism that marks everything unresolved would pass a
one-sided test while saying nothing.

MODEL-ONLY / FORECAST-ONLY. No scientific value is asserted here.
"""
import csv
import math
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qta_multiphysics.numerics import (  # noqa: E402
    BELOW_RESOLUTION, EXACT_ZERO, OUT_OF_RANGE, RESOLVED,
    UndecidableComparison, UndeclaredResolution, decide_below,
    resolution_class, resolution_classes)
from qta_multiphysics.gas_transport_1d import (  # noqa: E402
    default_species, solve_gas_transport_1d)
from qta_multiphysics.surface_coverage import surface_coverage_1d  # noqa: E402

FLOOR = 1.0e3


# --------------------------------------------------------------- classifier

def test_a_value_below_the_floor_is_not_resolved():
    assert resolution_class(0.016, FLOOR) == BELOW_RESOLUTION


def test_a_value_at_or_above_the_floor_is_resolved():
    # The control. Without it, a classifier that answered BELOW_RESOLUTION for
    # everything would pass the test above.
    assert resolution_class(FLOOR, FLOOR) == RESOLVED
    assert resolution_class(5.2e11, FLOOR) == RESOLVED


def test_absence_by_design_is_an_exact_zero_not_an_unresolved_one():
    # The same number, classified differently, because the question is WHY it
    # is zero. Deciding by magnitude -- the proxy -- cannot tell these apart,
    # and it is the distinction the whole mechanism exists for: helium is
    # genuinely absent in Mode C, methane is merely invisible.
    assert resolution_class(0.0, FLOOR, trivially_zero=True) == EXACT_ZERO
    assert resolution_class(0.0, FLOOR, trivially_zero=False) == BELOW_RESOLUTION


def test_the_sign_of_noise_does_not_change_the_class():
    # R59 in one line. One runner's BLAS dispatch lands this at -18.9 and
    # another's at +0.016; the clip writes 0.0 for the first and 0.016 for the
    # second. The digits disagree. The statement about resolution must not.
    assert resolution_class(-18.9, FLOOR) == resolution_class(0.016, FLOOR)


def test_a_value_outside_the_physical_range_is_not_filed_as_noise():
    # A density of -5e5 is not a small number the solver could not see; it is
    # a number it should not have produced. Calling that BELOW_RESOLUTION
    # would file a solver problem as a precision footnote.
    assert resolution_class(-5.0e5, FLOOR, low=0.0) == OUT_OF_RANGE
    # ... and the control: just outside the range by less than the floor is
    # still noise, not an excursion.
    assert resolution_class(-1.0, FLOOR, low=0.0) == BELOW_RESOLUTION


def test_a_floor_that_says_nothing_is_refused():
    for bad in (0.0, -1.0, None, float("nan"), float("inf")):
        with pytest.raises(UndeclaredResolution):
            resolution_class(1.0, bad)
    # The control: a real floor does not refuse.
    assert resolution_class(1.0, FLOOR) == BELOW_RESOLUTION


def test_a_non_finite_value_is_refused():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(UndeclaredResolution):
            resolution_class(bad, FLOOR)


def test_the_elementwise_form_answers_per_cell():
    got = resolution_classes([5.0e11, 0.5, -2.0], FLOOR)
    assert got == [RESOLVED, BELOW_RESOLUTION, BELOW_RESOLUTION]


# ------------------------------------------------------------ decidability

def test_a_threshold_far_above_the_unresolved_band_still_decides():
    # This is the live case. The Mode-D residual term compares a methane
    # density that is pure noise against 1e12, nine orders of magnitude above
    # the floor -- so whatever the digits were, the comparison holds. The
    # guard has to permit that, or it would refuse a sound decision.
    assert decide_below(0.0, 1e12, FLOOR, value_class=BELOW_RESOLUTION,
                        what="residual CH4") is True


def test_a_threshold_inside_the_unresolved_band_is_refused():
    with pytest.raises(UndecidableComparison):
        decide_below(0.0, 1e2, FLOOR, value_class=BELOW_RESOLUTION,
                     what="residual CH4")


def test_a_resolved_value_is_compared_against_any_threshold():
    # The band only matters for values inside it.
    assert decide_below(5.0, 1e2, FLOOR, value_class=RESOLVED) is True
    assert decide_below(5.0e11, 1e2, FLOOR, value_class=RESOLVED) is False


def test_a_comparison_with_no_declared_floor_is_refused():
    with pytest.raises(UndeclaredResolution):
        decide_below(0.0, 1e12, 0.0, value_class=BELOW_RESOLUTION)


# ------------------------------------------------------------- the solves

@pytest.fixture(scope="module")
def gas():
    species = default_species()
    gB = solve_gas_transport_1d(mode="B", t_end=2.0)
    n_init = {s.name: gB.profile_final(s.name) for s in species}
    gC = solve_gas_transport_1d(mode="C", t_end=2.0, n_init=n_init)
    return gB, gC


def test_the_gas_solve_declares_its_own_floor(gas):
    _, gC = gas
    assert gC.resolution_floor_1m3 == FLOOR


def test_the_written_methane_zero_is_the_clip_not_the_model(gas):
    # The measurement the defect rests on: what the integrator returned, and
    # what reached the file.
    _, gC = gas
    raw = gC.raw_profile_final("CH4")
    written = gC.profile_final("CH4")
    assert np.all(raw < 0.0), "expected the Mode-C methane solve to be noise"
    assert np.all(np.abs(raw) < FLOOR), "expected that noise to be sub-tolerance"
    assert np.all(written == 0.0), "expected the clip to write exact zeros"


def test_methane_in_mode_C_is_marked_below_what_the_solve_can_see(gas):
    _, gC = gas
    assert set(gC.resolution_profile("CH4")) == {BELOW_RESOLUTION}
    assert gC.resolution_of_region_mean("CH4") == BELOW_RESOLUTION
    assert gC.cells_below_resolution("CH4") == 120


def test_helium_in_mode_C_is_absent_by_design_not_unresolved(gas):
    # Same 0.0 in the same file as methane's, and a different statement.
    _, gC = gas
    for name in ("He3", "He4"):
        assert gC.trivially_zero(name) is True
        assert set(gC.resolution_profile(name)) == {EXACT_ZERO}


def test_mode_B_methane_is_resolved(gas):
    # The control for the whole mechanism: it is not marking everything.
    gB, _ = gas
    assert set(gB.resolution_profile("CH4")) == {RESOLVED}
    assert gB.cells_below_resolution("CH4") == 0


def test_hydrogen_is_partly_resolved_and_partly_not(gas):
    # Both classes inside one column, which is the case a per-file verdict
    # would flatten.
    _, gC = gas
    classes = set(gC.resolution_profile("H2"))
    assert classes == {RESOLVED, BELOW_RESOLUTION}


def test_the_coverage_solve_declares_its_floor_and_its_absences():
    solveB = surface_coverage_1d({"CH4": 1e17, "H2": 1e16}, T_surface_K=20.0,
                                 t_end=1.0, mode="B")
    covB, _, specs = solveB
    thetaB = {k: float(v[-1]) for k, v in covB.items()}
    solveC = surface_coverage_1d({}, T_surface_K=0.01, t_end=2.0, mode="C",
                                 theta0=thetaB, purge_1_s=5.0)
    assert solveC.resolution_floor == 1e-12
    for name in ("He3", "He4"):
        assert solveC.resolution_final(name) == EXACT_ZERO
    # ... and the control: the species that are actually there are resolved.
    for name in ("CH4", "H2"):
        assert solveC.resolution_final(name) == RESOLVED


# ------------------------------------------------------------- the artifacts

MARKED_FILES = {
    "gas_transport_profile.csv": ("n_{sp}_modeC_1m3", "resolution_{sp}_modeC",
                                  "gas_transport_metrics.csv",
                                  "resolution_floor_1m3"),
    "surface_coverage_profile.csv": ("theta_{sp}_modeC",
                                     "resolution_{sp}_modeC",
                                     "surface_coverage_metrics.csv",
                                     "resolution_floor_theta"),
}


def _rows(name):
    with open(os.path.join(ROOT, name), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.mark.parametrize("profile", sorted(MARKED_FILES))
def test_every_committed_value_states_what_the_solve_resolved(profile):
    vfmt, mfmt, mfile, fcol = MARKED_FILES[profile]
    floors = {r["species"]: float(r[fcol]) for r in _rows(mfile)}
    assert floors, f"{mfile} declares no resolution floor"
    rows = _rows(profile)
    checked = 0
    for sp, floor in floors.items():
        vc, mc = vfmt.format(sp=sp), mfmt.format(sp=sp)
        assert vc in rows[0], f"{profile}: no column {vc}"
        assert mc in rows[0], f"{profile}: {vc} is stated with no {mc}"
        assert floor > 0.0, f"{mfile}: {sp} declares floor {floor!r}"
        for i, r in enumerate(rows, 1):
            v, m = float(r[vc]), r[mc]
            checked += 1
            assert m in (RESOLVED, BELOW_RESOLUTION, EXACT_ZERO, OUT_OF_RANGE)
            if m == RESOLVED:
                assert abs(v) >= floor, f"{profile}:{i} {vc}={v} marked RESOLVED"
            if m == EXACT_ZERO:
                assert v == 0.0, f"{profile}:{i} {vc}={v} marked EXACT_ZERO"
    # Anti-vacuity: a loop that compared nothing proves nothing.
    assert checked > 100, f"{profile}: only {checked} value/marking pairs"


@pytest.mark.parametrize("profile", sorted(MARKED_FILES))
def test_the_marking_carries_information(profile):
    # If every cell in a file said the same thing the column would be
    # decoration. Both files contain species that are absent by design and
    # species that are present, so both must distinguish them.
    _, mfmt, mfile, _ = MARKED_FILES[profile]
    rows = _rows(profile)
    seen = set()
    for sp in (r["species"] for r in _rows(mfile)):
        mc = mfmt.format(sp=sp)
        seen.update(r[mc] for r in rows)
    assert len(seen) >= 2, f"{profile}: every cell marked {seen}"


def test_the_committed_methane_column_is_stated_as_unresolved():
    # Pins the finding itself. If a future change makes this column resolved,
    # that is a real improvement and this test should be updated deliberately
    # rather than drift.
    rows = _rows("gas_transport_profile.csv")
    assert {r["resolution_CH4_modeC"] for r in rows} == {BELOW_RESOLUTION}
    assert {r["n_CH4_modeC_1m3"] for r in rows} == {"0.000000000e+00"}
    metrics = {r["species"]: r for r in _rows("gas_transport_metrics.csv")}
    assert metrics["CH4"]["residual_mode_D_resolution"] == BELOW_RESOLUTION
    assert metrics["CH4"]["cells_below_resolution_modeC"] == "120"
    # ... beside the species whose zero IS exact, in the same file.
    assert metrics["He3"]["residual_mode_D_resolution"] == EXACT_ZERO
    assert metrics["He4"]["residual_mode_D_resolution"] == EXACT_ZERO
    # ... and the one that is genuinely measured by this model.
    assert metrics["H2"]["residual_mode_D_resolution"] == RESOLVED
    assert float(metrics["H2"]["residual_mode_D_density_m3"]) > FLOOR


def test_the_floor_in_the_artifact_is_the_floor_the_solver_used():
    # The number in the file and the number the code classifies against are
    # the same number, or the marking describes a solve nobody ran.
    from qta_multiphysics.gas_transport_1d import solve_gas_transport_1d as s
    import inspect
    declared = {float(r["resolution_floor_1m3"])
                for r in _rows("gas_transport_metrics.csv")}
    assert len(declared) == 1
    assert declared == {float(inspect.signature(s).parameters["atol"].default)}
    from qta_multiphysics.surface_coverage import COVERAGE_ATOL
    cov = {float(r["resolution_floor_theta"])
           for r in _rows("surface_coverage_metrics.csv")}
    assert cov == {COVERAGE_ATOL}


def test_no_marking_column_is_silently_all_nan():
    # math is imported for this: a column of unparseable values would make
    # every numeric assertion above vacuously true.
    rows = _rows("gas_transport_profile.csv")
    vals = [float(r["n_H2_modeC_1m3"]) for r in rows]
    assert all(math.isfinite(v) for v in vals)
    assert max(vals) > FLOOR


# --------------------------------- the clip-provenance reconciliation

sys.path.insert(0, os.path.join(ROOT, "tools"))
import clip_provenance as cp  # noqa: E402

_GOOD = {
    "qta_multiphysics/x.py:f": {
        "moves": True, "reaches_output": ["x.csv"],
        "resolution_declared": "x_floor", "test": "tests/test_x.py::t"},
    "qta_multiphysics/y.py:g": {
        "moves": False, "reaches_output": [],
        "resolution_declared": None, "test": None},
}
_MEASURED = {
    "qta_multiphysics/x.py:f": {"elements": 10, "elements_moved": 3},
    "qta_multiphysics/y.py:g": {"elements": 10, "elements_moved": 0},
}


def test_the_clip_inventory_accepts_a_measurement_it_describes():
    # The control for every refusal below.
    assert cp.check(_MEASURED, {"sites": _GOOD}) == []


def test_a_clip_the_inventory_does_not_know_about_is_reported():
    measured = dict(_MEASURED)
    measured["qta_multiphysics/z.py:h"] = {"elements": 4, "elements_moved": 4}
    problems = cp.check(measured, {"sites": _GOOD})
    assert any("not in the inventory" in p for p in problems), problems


def test_an_inventory_entry_the_run_never_reaches_is_reported():
    sites = dict(_GOOD)
    sites["qta_multiphysics/gone.py:old"] = dict(_GOOD["qta_multiphysics/y.py:g"])
    problems = cp.check(_MEASURED, {"sites": sites})
    assert any("past version of the code" in p for p in problems), problems


def test_the_inventory_may_not_disagree_with_the_measurement():
    # In both directions: a site recorded as quiet that moves, and a site
    # recorded as moving that does not.
    sites = dict(_GOOD)
    sites["qta_multiphysics/y.py:g"] = dict(sites["qta_multiphysics/y.py:g"],
                                            moves=True)
    assert any("moves=True" in p for p in cp.check(_MEASURED, {"sites": sites}))
    sites = dict(_GOOD)
    sites["qta_multiphysics/x.py:f"] = dict(sites["qta_multiphysics/x.py:f"],
                                            moves=False)
    assert any("moves=False" in p for p in cp.check(_MEASURED, {"sites": sites}))


def test_a_moving_clip_that_reaches_a_file_must_name_what_states_the_floor():
    sites = dict(_GOOD)
    sites["qta_multiphysics/x.py:f"] = dict(sites["qta_multiphysics/x.py:f"],
                                            resolution_declared=None)
    problems = cp.check(_MEASURED, {"sites": sites})
    assert any("states the resolution" in p for p in problems), problems


def test_a_moving_clip_must_name_a_regression_test():
    sites = dict(_GOOD)
    sites["qta_multiphysics/x.py:f"] = dict(sites["qta_multiphysics/x.py:f"],
                                            test=None)
    problems = cp.check(_MEASURED, {"sites": sites})
    assert any("names no regression test" in p for p in problems), problems


def test_the_committed_inventory_agrees_with_the_committed_judgements():
    import json
    d = json.loads(open(os.path.join(ROOT, "docs/clip_provenance.json"),
                        encoding="utf-8").read())
    sites = d["sites"]
    assert len(sites) >= 10, f"only {len(sites)} clip sites recorded"
    moving = {k: v for k, v in sites.items() if v["moves"]}
    # Measured, not assumed: exactly the two gas-transport clips move, and
    # nine other sites in the tree were observed and move nothing. A one-sided
    # inventory naming only the movers could not say that.
    assert set(moving) == {
        "qta_multiphysics/gas_transport_1d.py:rhs",
        "qta_multiphysics/gas_transport_1d.py:solve_gas_transport_1d",
    }, sorted(moving)
    for k, v in sites.items():
        assert v["what"] != "UNREVIEWED", f"{k} is unreviewed"
        if v["moves"]:
            assert v["resolution_declared"] and v["test"], k
