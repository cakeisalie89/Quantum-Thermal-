"""Every quantity that crossed zero between two backends states its resolution.

D-2026-98. R59's remeasurement on the full canonical corpus, across seven
dispatch configurations of this host, found every zero crossing and traced
each to the quantity and the code that produced it:

  * ``energy_ledger_cumulative_3d.csv`` ``cumulative_dU_J`` after the first
    MODE_C: +9.114e-12 J and -9.114e-12 J cancelling to one ulp on one
    backend and to nothing on another. Bare: the ledger published no floor.
  * ``coupled_mode_state_summary.json`` ``.state.gasC_sample.CH4``: 0.0 and
    4.0e-09, both inside the gas integrator's 1e3 m^-3 floor. Bare -- but
    the comparator had called it DECLARED, because the FILE mentioned a
    resolution for a different key.
  * ``convergence_report_3d.json`` ``.time_integration_check.rel_change``:
    0.0 and 1.29e-16, one ulp of the probe, far below the tolerance either
    integration was run to.

Each now carries its class from the code that produced it, against a floor
that code establishes; nothing was rounded, clipped or hand-edited. And the
binding of a class to a quantity is declared quantity by quantity in
``docs/resolution_inventory.json`` and reconciled against the committed
artefacts, so "the file declares a resolution somewhere" authorises nothing.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import resolution_inventory as ri  # noqa: E402
from qta_multiphysics import campaign_state_3d as cs  # noqa: E402
from qta_multiphysics.convergence_3d import rel_change_resolution  # noqa: E402
from qta_multiphysics.numerics import (  # noqa: E402
    BELOW_RESOLUTION, EXACT_ZERO, RESOLVED)

U = 2.0 ** -53


# ---- the energy ledger ------------------------------------------------------
def _phase(src, snk, dU, res=None):
    res = src - snk - dU if res is None else res
    return {"integrated_source_energy_J": src, "boundary_sink_energy_J": snk,
            "internal_energy_change_J": dU, "residual_J": res,
            "rel_residual": res / max(abs(src), abs(snk), abs(dU), 1e-30)}


def _ledger(b, c, d=None, cycles=1):
    seq = SimpleNamespace(tB=SimpleNamespace(energy=b),
                          tC=SimpleNamespace(energy=c),
                          tD_hold=None if d is None
                          else SimpleNamespace(energy=d))
    st = SimpleNamespace(n_cycles=cycles, energy_rows=[], cumulative={})
    cs.attach_energy_ledger(st, seq)
    return st


def test_the_ledger_floor_is_what_its_balance_leaves_unexplained():
    assert cs.ledger_floor(2e-13, 0.0, 3) == 2e-13
    assert cs.ledger_floor(-2e-13, 0.0, 3) == 2e-13, "a magnitude"
    # a closure that lands on exactly zero still has the arithmetic's bound
    f = cs.ledger_floor(0.0, 1e-9, 2)
    assert f == 2 * U * 1e-9 and f > 0.0


def test_a_cancellation_to_one_ulp_and_to_nothing_are_the_same_statement():
    """The R59 crossing, rebuilt: B stores +x, C releases -x (to one ulp on
    one backend, exactly on another). Both are BELOW the ledger's floor --
    the energy its closures leave unexplained -- not one exact zero and one
    claim of stored energy."""
    x = 9.114286125e-12
    one_ulp = _ledger(_phase(1.5e-9, 1.5e-9 - x - 1.3e-14, x),
                      _phase(0.0, 8.9e-12, -x + 1.6155871e-27, -1.86e-13))
    exact = _ledger(_phase(1.5e-9, 1.5e-9 - x - 1.3e-14, x),
                    _phase(0.0, 8.9e-12, -x, -1.86e-13))
    for st in (one_ulp, exact):
        row = st.energy_rows[1]
        assert row["cumulative_dU_J_resolution"] == BELOW_RESOLUTION, row
        assert float(row["cumulative_resolution_floor_J"]) >= 1.86e-13
    assert one_ulp.energy_rows[1]["cumulative_dU_J"] != \
        exact.energy_rows[1]["cumulative_dU_J"], "the digits still differ"


def test_a_resolved_energy_is_resolved():
    st = _ledger(_phase(1.5e-9, 1.49e-9, 9.1e-12, 1.3e-14),
                 _phase(0.0, 8.9e-12, -9.1e-12, 1.9e-13))
    b = st.energy_rows[0]
    assert b["phase_dU_J_resolution"] == RESOLVED
    assert b["cumulative_dU_J_resolution"] == RESOLVED
    c = st.energy_rows[1]
    assert c["phase_source_J_resolution"] == BELOW_RESOLUTION, (
        "a source of 0 J against a closure residual of 1.9e-13 J")


def test_unexplained_energy_adds_in_magnitude_across_phases():
    """Signed residuals can cancel; what they fail to explain does not."""
    st = _ledger(_phase(1e-9, 1e-9, 0.0, 1e-13),
                 _phase(1e-9, 1e-9, 0.0, -1e-13))
    assert float(st.energy_rows[1]["cumulative_residual_J"]) == 0.0
    assert float(st.energy_rows[1]["cumulative_resolution_floor_J"]) >= 2e-13


def test_the_cumulative_class_is_against_the_cumulative_floor():
    """A dU resolved against its own phase's closure can be below what the
    ledger as a whole resolves once other phases' imbalance is summed in."""
    st = _ledger(_phase(1e-9, 1e-9, 0.0, 1e-13),
                 _phase(0.0, 0.0, 0.0, 1e-13),
                 _phase(1.3e-17, 1.6e-19, 1.25e-17, 3.7e-23))
    d = st.energy_rows[2]
    assert d["phase_dU_J_resolution"] == RESOLVED
    assert d["cumulative_dU_J_resolution"] == BELOW_RESOLUTION


def test_the_campaign_summary_carries_the_final_classes():
    st = _ledger(_phase(1.5e-9, 1.49e-9, 9.1e-12, 1.3e-14),
                 _phase(0.0, 8.9e-12, -9.1e-12, 1.9e-13))
    for k in ("source_J", "sink_J", "dU_J"):
        assert st.cumulative[f"{k}_resolution"] == \
            st.energy_rows[-1][f"cumulative_{k}_resolution"]
    assert st.cumulative["resolution_floor_J"] == float(
        st.energy_rows[-1]["cumulative_resolution_floor_J"])


def test_the_committed_ledger_states_its_resolution_row_by_row():
    with (ROOT / "energy_ledger_cumulative_3d.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    crossing = rows[1]
    assert (crossing["cycle"], crossing["phase"]) == ("1", "MODE_C")
    assert crossing["cumulative_dU_J_resolution"] == BELOW_RESOLUTION
    for r in rows:
        for scope in ("phase", "cumulative"):
            floor = float(r[f"{scope}_resolution_floor_J"])
            assert floor > 0.0
            for q in ("source_J", "sink_J", "dU_J"):
                v, cls = float(r[f"{scope}_{q}"]), r[f"{scope}_{q}_resolution"]
                assert cls == (RESOLVED if abs(v) >= floor
                               else BELOW_RESOLUTION), (r["cycle"],
                                                        r["phase"], q)


# ---- the coupled-mode state -------------------------------------------------
@pytest.fixture(scope="module")
def coupled():
    from qta_multiphysics.config import default_config
    from qta_multiphysics.coupled_mode_solver import run_coupled
    metrics, state, _ = run_coupled(default_config())
    return metrics, state


@pytest.mark.parametrize("obj", ["gasB_sample", "gasC_sample", "thetaB",
                                 "thetaC"])
def test_every_species_value_travels_with_its_class(coupled, obj):
    _, state = coupled
    classes = state[f"{obj}_resolution"]
    assert set(classes) == set(state[obj]), "species by species"
    assert set(classes.values()) <= {EXACT_ZERO, RESOLVED, BELOW_RESOLUTION}


def test_the_crossing_species_is_below_resolution_in_both_copies(coupled):
    metrics, state = coupled
    assert state["gasC_sample_resolution"]["CH4"] == BELOW_RESOLUTION
    assert state["gasC_sample_resolution"]["CH4"] == \
        metrics["Mode_C_cleanup_residual_CH4_resolution"]


def test_each_mode_is_classified_by_its_own_solve(coupled):
    """Mode B's methane is resolved, Mode C's is not: a class copied across
    solves would say the same of both."""
    _, state = coupled
    assert state["gasB_sample_resolution"]["CH4"] == RESOLVED
    assert state["gasC_sample_resolution"]["CH4"] == BELOW_RESOLUTION
    assert state["gasB_sample_resolution"]["He3"] == EXACT_ZERO


# ---- the convergence report -------------------------------------------------
def test_a_relative_change_below_the_tightest_tolerance_is_unresolved():
    assert rel_change_resolution(0.0, 1e-6, 1e-7) == (1e-7, BELOW_RESOLUTION)
    assert rel_change_resolution(1.29e-16, 1e-6, 1e-7) == \
        (1e-7, BELOW_RESOLUTION)
    assert rel_change_resolution(5.6e-3, 1e-6, 1e-6) == (1e-6, RESOLVED)


def test_the_floor_is_the_tightest_of_the_solves_compared():
    """A change of 5e-7 between a 1e-6 solve and a 1e-7 solve is larger than
    what the tighter one resolves: RESOLVED, not absorbed by the looser."""
    assert rel_change_resolution(5e-7, 1e-6, 1e-7) == (1e-7, RESOLVED)


def test_the_committed_report_classifies_its_own_numbers():
    doc = json.loads((ROOT / "convergence_report_3d.json").read_text())
    t = doc["time_integration_check"]
    assert (t["rel_change_resolution_floor"], t["rel_change_resolution"]) == \
        rel_change_resolution(t["rel_change"], t["base_rtol"],
                              t["tightened_rtol"])
    m = doc["mesh_check"]
    for q in ("probe", "hotspot"):
        assert m[f"{q}_rel_change_resolution"] == rel_change_resolution(
            m[f"{q}_rel_change"], t["base_rtol"])[1]


# ---- bindings, quantity by quantity -----------------------------------------
INV = json.loads((ROOT / "docs" / "resolution_inventory.json").read_text())


def test_the_committed_bindings_reconcile():
    assert ri.reconcile_bindings(INV) == []


def test_every_crossing_r59_found_is_bound_to_its_own_class():
    bound = [
        ("coupled_mode_state_summary.json", ".state.gasC_sample.CH4",
         ".state.gasC_sample_resolution.CH4"),
        ("coupled_mode_state_summary.json",
         ".metrics.Mode_D_residual_CH4_density_m3",
         ".metrics.Mode_D_residual_CH4_resolution"),
        ("multiphysics_summary.json",
         ".coupled_metrics.Mode_C_cleanup_residual_CH4_m3",
         ".coupled_metrics.Mode_C_cleanup_residual_CH4_resolution"),
        ("convergence_report_3d.json", ".time_integration_check.rel_change",
         ".time_integration_check.rel_change_resolution"),
    ]
    for src, leaf, carrier in bound:
        assert ri.json_carrier(INV, src, leaf) == carrier
    assert ri.row_carrier(INV, "coupled_mode_recovery_metrics.csv",
                          "Mode_D_residual_CH4_density_m3") == (
        "Mode_D_residual_CH4_resolution", "gas_resolution_floor_1m3")
    e = ri.column_entry(INV, "energy_ledger_cumulative_3d.csv",
                        "cumulative_dU_J")
    assert (e["resolution_from"], e["floor_from"]) == (
        "cumulative_dU_J_resolution", "cumulative_resolution_floor_J")


def test_a_class_for_one_quantity_binds_nothing_else():
    """The file-level proxy this replaces: coupled_mode_state_summary.json
    declared a resolution for its metrics, and a crossing in its state was
    called declared because of it."""
    src = "coupled_mode_state_summary.json"
    assert ri.json_carrier(INV, src, ".state.ready_terms.drift_ok") is None
    assert ri.json_carrier(INV, src, ".state.microwave.total_input_power_W") \
        is None
    assert ri.json_carrier(INV, src, ".metrics.Mode_B_peak_T_K") is None
    assert ri.json_carrier(INV, "gas_transport_metrics.csv", ".x") is None
    assert ri.row_carrier(INV, "coupled_mode_recovery_metrics.csv",
                          "Mode_B_peak_T_K") is None


def test_a_pattern_binds_each_member_to_the_member_of_the_same_name():
    inv = {"json_bindings": {"f.json": {".s.gas.*": ".s.gas_resolution.*"}}}
    assert ri.json_carrier(inv, "f.json", ".s.gas.CH4") == \
        ".s.gas_resolution.CH4"
    assert ri.json_carrier(inv, "f.json", ".s.gas.H2") == \
        ".s.gas_resolution.H2"
    assert ri.json_carrier(inv, "f.json", ".s.other.CH4") is None


def _reconcile(tmp_path, doc, bindings):
    (tmp_path / "a.json").write_text(json.dumps(doc))
    return ri.reconcile_bindings({"json_bindings": {"a.json": bindings}},
                                 root=tmp_path)


def test_a_binding_to_a_path_that_is_not_there_is_refused(tmp_path):
    p = _reconcile(tmp_path, {"m": {"x": 0.0}}, {".m.x": ".m.x_resolution"})
    assert p and "does not have" in p[0]


def test_a_carrier_that_is_not_a_class_is_refused(tmp_path):
    p = _reconcile(tmp_path, {"m": {"x": 0.0, "x_resolution": "fine"}},
                   {".m.x": ".m.x_resolution"})
    assert p and "not a resolution class" in p[0]


def test_a_bound_quantity_that_is_not_a_number_is_refused(tmp_path):
    p = _reconcile(tmp_path, {"m": {"x": "ok", "x_resolution": RESOLVED}},
                   {".m.x": ".m.x_resolution"})
    assert p and "not a number" in p[0]


def test_parallel_objects_must_have_the_same_members(tmp_path):
    p = _reconcile(tmp_path, {"g": {"CH4": 0.0, "H2": 1.0},
                              "g_resolution": {"CH4": BELOW_RESOLUTION}},
                   {".g.*": ".g_resolution.*"})
    assert any("same members" in x for x in p), p


def test_a_pattern_bound_to_a_single_leaf_is_refused(tmp_path):
    p = _reconcile(tmp_path, {"g": {"CH4": 0.0}, "c": RESOLVED},
                   {".g.*": ".c"})
    assert p and "pattern to a single leaf" in p[0]


def test_a_floor_that_is_not_a_declared_floor_is_refused():
    governed = {"energy_ledger_cumulative_3d.csv:cumulative_dU_J"}
    declared = {"energy_ledger_cumulative_3d.csv:cumulative_dU_J": {
        "basis": "CARRIER", "resolution_from": "cumulative_dU_J_resolution",
        "floor_from": "cumulative_residual_J"}}
    problems = ri.reconcile(governed, declared)
    assert any("floor_from" in p for p in problems), problems


def test_a_long_table_binding_to_a_row_that_is_not_a_class_is_refused(
        tmp_path):
    (tmp_path / "m.csv").write_text(
        "metric,value\nx_m3,0.0\nx_resolution,maybe\n")
    p = ri.reconcile_bindings({"row_bindings": {"m.csv": {
        "key_column": "metric", "value_column": "value",
        "bindings": {"x_m3": "x_resolution"}}}}, root=tmp_path)
    assert p and "not a resolution class" in p[0]


def test_leaf_paths_are_parsed_not_split():
    assert ri.path_segments(".a.b[2].c") == ["a", "b", 2, "c"]
    for bad in ("a.b", ".a..b", ".a[x]", ""):
        with pytest.raises(ValueError):
            ri.path_segments(bad)
