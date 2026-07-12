"""Tests for the coupled 3D layer (channels, states, accounting, forecasts).

All checks are numerical/software self-consistency of the MODEL (MODEL-ONLY;
forecast-only; zero PASS; no measured data).
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.config import default_config
from qta_multiphysics.mesh_3d import Grid3DConfig, StructuredGrid3D
from qta_multiphysics.thermal_3d_transient import solve_thermal_3d
from qta_multiphysics.state_machine_3d import device_state, all_device_states
from qta_multiphysics.sources_3d import (microwave_nv_region_map_W,
                                         radiative_front_flux_W_m2)
from qta_multiphysics.heat_switch_3d import (HeatSwitchSpec,
                                             switch_leak_forecast_W,
                                             A1_LEAK_BOUND_W)
from qta_multiphysics.coupling_ledger_3d import (coupling_ledger,
                                                 ALLOWED_STATUSES)
from qta_multiphysics.species_accounting_3d import species_accounting_rows
from qta_multiphysics.mode_sequence_3d import run_mode_sequence_3d
from qta_multiphysics.verification_3d import run_verification_3d
from qta_multiphysics.falsification_3d import falsification_report
from qta_multiphysics.nv_eligibility_3d import nv_eligibility_forecast
from qta_multiphysics.convergence_3d import convergence_report
from qta_multiphysics.surface_coverage import (default_coverage_specs,
                                               evolve_coverage)

CFG = default_config()
G3 = Grid3DConfig(nx=8, ny=8, nz=10)
GRID = StructuredGrid3D(CFG, G3)

# one shared coupled sequence for the forecast tests (deterministic)
SEQ = run_mode_sequence_3d(CFG, G3, n_eval_b=9, n_eval_c=21)
VREP = run_verification_3d(CFG, G3)
SP = species_accounting_rows(CFG)


def test_mode_state_machine_table():
    """Device states follow the canonical DESIGN_SPECIFIED logic per mode."""
    st = {s.mode: s for s in all_device_states()}
    assert st["MODE_B"].laser_active and not st["MODE_B"].microwave_active
    assert st["MODE_B"].shutter_state == "open"
    assert st["MODE_B"].heat_switch_state == "OPEN"       # gate A1 protection
    assert not st["MODE_D"].laser_active and st["MODE_D"].microwave_active
    assert st["MODE_D"].shutter_state == "closed"
    assert st["MODE_C"].heat_switch_state == "CLOSED"     # recovery path
    assert st["MODE_D"].active_species == frozenset({"He3", "He4"})
    assert st["MODE_B"].active_species == frozenset({"C13_CH4"})


def test_microwave_map_zero_outside_mode_D():
    """MW drive -> zero NV-region map in Mode B; positive, conserved in D."""
    Qb, mb = microwave_nv_region_map_W(CFG, GRID, device_state("MODE_B"))
    assert float(np.abs(Qb).max()) == 0.0 and mb["active"] is False
    Qd, md = microwave_nv_region_map_W(CFG, GRID, device_state("MODE_D"))
    assert md["active"] is True and md["total_W"] > 0.0
    assert math.isclose(float(Qd.sum()), md["total_W"], rel_tol=1e-12)
    assert md["status"] == "REDUCED_ORDER"


def test_shutter_state_orders_radiative_load():
    """Open shutter admits more radiative load than closed (chain values)."""
    fx_open, mo = radiative_front_flux_W_m2(GRID, device_state("MODE_B"))
    fx_closed, mc = radiative_front_flux_W_m2(GRID, device_state("MODE_D"))
    assert mo["shutter_state"] == "open" and mc["shutter_state"] == "closed"
    assert fx_open > fx_closed > 0.0
    assert mo["status"] == mc["status"] == "REDUCED_ORDER"


def test_heat_switch_conductance_ordering_and_A1_bound():
    """CLOSED >> OPEN conductance; OPEN leak within the A1 forecast bound."""
    sw = HeatSwitchSpec()
    assert sw.G_closed_W_K > sw.G_open_W_K
    open_leak = switch_leak_forecast_W("OPEN", CFG.fridge.T_fridge_K)
    assert open_leak["within_A1_bound_in_model"] is True
    assert open_leak["leak_forecast_W"] < A1_LEAK_BOUND_W
    closed = switch_leak_forecast_W("CLOSED", CFG.fridge.T_fridge_K)
    assert closed["leak_forecast_W"] > open_leak["leak_forecast_W"]
    try:
        sw.conductance_W_K("HALF")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid switch state did not raise")


def test_extra_channels_conserve_energy():
    """A solve with MW map + front flux closes within the shared tolerance."""
    Qd, _ = microwave_nv_region_map_W(CFG, GRID, device_state("MODE_D"))
    fx, _ = radiative_front_flux_W_m2(GRID, device_state("MODE_D"))
    r = solve_thermal_3d(CFG, G3, source_scale=0.0, extra_volumetric_W=Qd,
                         extra_front_flux_W_m2=fx, n_eval=13)
    assert r.solver_status == "ok"
    assert abs(r.energy_residual()) < 0.05
    assert r.energy["front_flux_channel_J"] >= 0.0


def test_default_kwargs_bitwise_guard():
    """Zero-valued channel inputs reproduce the default solve bit-for-bit."""
    a = solve_thermal_3d(CFG, G3, n_eval=7)
    z = np.zeros((G3.nx, G3.ny, G3.nz))
    b = solve_thermal_3d(CFG, G3, n_eval=7, extra_volumetric_W=z,
                         extra_front_flux_W_m2=0.0)
    assert np.array_equal(a.T, b.T), "zero channels perturbed the solve"


def test_species_accounting_rows():
    """Ledger enforces roles, bounded coverage, canonical purge residual."""
    assert all(r["coverage_bounded_0_1"] != "false" for r in SP)
    d_he = [r for r in SP if r["phase"] == "MODE_D" and r["species"] == "He3"]
    assert d_he and float(d_he[0]["flux_in_per_m2_s"]) > 0.0
    b_he = [r for r in SP if r["phase"] == "MODE_B" and r["species"] == "He3"]
    assert b_he[0]["allowed_active"] == "false"
    resC = [r for r in SP if r["phase"] == "MODE_C"][0]
    assert float(resC["coverage_end"]) < 1e-6      # canonical purge forecast
    d_c13 = [r for r in SP if r["phase"] == "MODE_D" and r["species"] == "C13_CH4"]
    assert d_c13[0]["allowed_active"] == "false"


def test_purge_decay_matches_closed_form():
    """Zero-flux coverage decay matches theta0*exp(-(purge+cryotrap)t)."""
    specs = [s for s in default_coverage_specs() if s.name == "CH4"]
    th0, purge, t_end = 0.7, 5.0, 1.0
    cov, tt = evolve_coverage(specs, {"CH4": 0.0}, CFG.fridge.T_fridge_K,
                              t_end, theta0={"CH4": th0}, purge_1_s=purge,
                              n_eval=9)
    k = purge + specs[0].cryotrap_1_s    # desorption ~0 at 10 mK
    for th, t in zip(cov["CH4"], tt):
        ref = th0 * math.exp(-k * t)
        assert abs(th - ref) <= 1e-3 * th0 + 1e-9, (th, ref, t)


def test_falsification_statuses_and_injected_failure():
    """Statuses stay in the allowed set; an injected miss is reported, not hidden."""
    rep = falsification_report(CFG, SEQ, VREP, SP)
    allowed = {"DERIVED_CHECK", "CONDITIONAL", "NOT_IMPLEMENTED"}
    assert {c["status"] for c in rep["conditions"]} <= allowed
    assert rep["n_not_implemented_bounds"] >= 2
    # inject: absurdly low recovery threshold -> recovery falsified in model
    bad = run_mode_sequence_3d(CFG, G3, n_eval_b=7, n_eval_c=9,
                               threshold_K=1e-6, coupled_channels=False)
    rep2 = falsification_report(CFG, bad, VREP, SP)
    rec = [c for c in rep2["conditions"]
           if c["condition"] == "thermal_recovery_before_mode_D"][0]
    assert rec["falsified_in_model"] == "true" and rec["status"] == "CONDITIONAL"
    assert rep2["n_falsified_in_model"] >= 1


def test_nv_eligibility_forecast_blocks_honestly():
    """Eligibility is never PASS; blocks carry reasons; thermal block works."""
    el = nv_eligibility_forecast(CFG, SEQ, SP)
    assert el["eligibility_forecast"] in ("CONDITIONAL", "BLOCKED")
    assert el["can_PASS_now"] == "NO" and el["measured_in_this_system"] is False
    assert "vibration_amplitude" in el["blocking_reasons"]
    bad = run_mode_sequence_3d(CFG, G3, n_eval_b=7, n_eval_c=9,
                               threshold_K=1e-6, coupled_channels=False)
    el2 = nv_eligibility_forecast(CFG, bad, SP)
    assert el2["eligibility_forecast"] == "BLOCKED"
    assert "thermal_recovery" in el2["blocking_reasons"]


def test_convergence_report_schema():
    """Convergence check runs on a small pair and reports allowed statuses."""
    rep = convergence_report(CFG, ci=Grid3DConfig(8, 8, 10),
                             refined=Grid3DConfig(10, 10, 12))
    for key in ("mesh_check", "time_integration_check"):
        assert rep[key]["status"] in ("DERIVED_CHECK", "CONDITIONAL")
        assert "tolerance" in rep[key]
    assert "never" in rep["meaning"] or "verification" in rep["meaning"]


def test_coupling_ledger_vocabulary_and_arrows():
    """Ledger uses only the honesty vocabulary and covers the key arrows."""
    led = coupling_ledger()
    assert len(led) >= 15
    assert all(e["status"] in ALLOWED_STATUSES for e in led)
    joined = " ".join(e["source_domain"] + ">" + e["target_domain"] for e in led)
    for needle in ("laser", "Kapitza", "Mode B final", "microwave",
                   "heat-switch", "shutter", "vibration", "eligibility"):
        assert needle in joined, needle
    txt = str(led)
    assert '"PASS"' not in txt and "'PASS'" not in txt


def test_mode_d_hold_under_sensing_loads():
    """The coupled sequence forecasts the Mode-D hold probe below threshold."""
    assert SEQ.tD_hold is not None
    probe = float(SEQ.tD_hold.probe_timeseries_K()[-1])
    assert probe <= SEQ.threshold_K
    rows = SEQ.timeline_rows()
    assert any(r["phase"] == "MODE_D_HOLD" for r in rows)
    assert all("PASS" not in str(v) for r in rows for v in r.values())


def test_sensitivity_ranking_is_model_sensitivity():
    """OAT ranking: four rows, sorted by |normalized sensitivity|, explicitly
    labelled as model sensitivity (never experimental importance)."""
    from qta_multiphysics.sensitivity_3d import sensitivity_rows
    rows = sensitivity_rows(CFG)
    assert len(rows) == 4
    mags = [float(r["abs_normalized_sensitivity"]) for r in rows]
    assert mags == sorted(mags, reverse=True)
    assert [r["rank"] for r in rows] == ["1", "2", "3", "4"]
    assert all("model sensitivity" in r["meaning"] for r in rows)


ALL_TESTS = [
    test_mode_state_machine_table,
    test_microwave_map_zero_outside_mode_D,
    test_shutter_state_orders_radiative_load,
    test_heat_switch_conductance_ordering_and_A1_bound,
    test_extra_channels_conserve_energy,
    test_default_kwargs_bitwise_guard,
    test_species_accounting_rows,
    test_purge_decay_matches_closed_form,
    test_falsification_statuses_and_injected_failure,
    test_nv_eligibility_forecast_blocks_honestly,
    test_convergence_report_schema,
    test_coupling_ledger_vocabulary_and_arrows,
    test_mode_d_hold_under_sensing_loads,
    test_sensitivity_ranking_is_model_sensitivity,
]


def main() -> int:
    passed = failed = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(ALL_TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
