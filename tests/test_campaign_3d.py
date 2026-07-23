"""Campaign-continuity tests (Stage 2). MODEL-ONLY / FORECAST-ONLY.

Covers the approved list: linear-regime analytical benchmark, capture
monotonicity, saturation bounds, species/mode gating, mass+energy
bookkeeping, three-cycle deterministic equality, per-cycle interlock
refusal persistence, forced-saturation falsification, zero-PASS scan,
single-cycle regression (canon untouched).
"""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qta_multiphysics.config import default_config                # noqa: E402
from qta_multiphysics.mesh_3d import Grid3DConfig                 # noqa: E402
from qta_multiphysics.mode_sequence_3d import run_mode_sequence_3d  # noqa: E402
from qta_multiphysics.species_accounting_3d import species_accounting_rows  # noqa: E402
from qta_multiphysics.vibration_transfer import vibration_transfer  # noqa: E402
from qta_multiphysics.verification_3d import run_verification_3d  # noqa: E402
from qta_multiphysics.falsification_3d import falsification_report  # noqa: E402
from qta_multiphysics.machine_fsm import run_nominal_lifecycle    # noqa: E402
from qta_multiphysics.cryopanel_dynamics_3d import (              # noqa: E402
    PanelInventory, new_panel_set, advance_phase, phase_fluxes_per_m2_s,
    SITES_PER_M2, N_ML_CAP)
from qta_multiphysics.campaign_state_3d import (                  # noqa: E402
    build_campaign, attach_energy_ledger, energy_conservation_ok,
    mass_bookkeeping_rows)

CFG = default_config()
SEQ = run_mode_sequence_3d(CFG, Grid3DConfig(8, 8, 10),
                           n_eval_b=7, n_eval_c=17)
SP = species_accounting_rows(CFG)
_, VM = vibration_transfer()


def _campaign(n=3):
    st = build_campaign(CFG, SEQ, SP, VM, n)
    attach_energy_ledger(st, SEQ)
    return st


def test_linear_regime_analytical_benchmark():
    p = PanelInventory("T", "C13_CH4")
    F, dt = 1.0e15, 1.0e-3
    p.capture_window(F, dt)
    lin = p.sticking * F * dt
    rel = abs(p.N_per_m2 - lin) / lin
    # exact solution deviates from the linear form by ~ s F dt / (2 Ncap)
    curv = p.sticking * F * dt / (2.0 * SITES_PER_M2)
    assert rel < 1e-6 and abs(rel - curv) / curv < 0.2, (rel, curv)


def test_inventory_monotonic_during_capture():
    p = PanelInventory("T", "C13_CH4")
    prev = 0.0
    for _ in range(5):
        p.capture_window(1.0e18, 1.0e-3)
        assert p.N_per_m2 >= prev
        prev = p.N_per_m2


def test_saturation_bounds():
    p = PanelInventory("T", "C13_CH4")
    p.capture_window(1.0e25, 1.0e6)          # absurdly deep exposure
    assert 0.0 <= p.monolayer_fraction <= 1.0
    assert abs(p.monolayer_fraction - N_ML_CAP) < 1e-12


def test_species_mode_gating():
    fB = phase_fluxes_per_m2_s("MODE_B")
    fC = phase_fluxes_per_m2_s("MODE_C")
    fD = phase_fluxes_per_m2_s("MODE_D")
    assert fB["C13_CH4"] > 0 and fC["C13_CH4"] == 0 and fD["C13_CH4"] == 0
    assert fD["He"] > 0 and fB["He"] == 0 and fC["He"] == 0
    assert fB["H2"] > 0 and fC["H2"] > 0 and fD["H2"] > 0
    panels = new_panel_set()
    advance_phase(panels, "MODE_D", 1.0)
    he = [p for p in panels if p.species == "He"][0]
    assert he.admitted_per_m2 > 0 and he.N_per_m2 == 0.0   # zero capture


def test_mass_and_energy_bookkeeping():
    st = _campaign()
    for r in mass_bookkeeping_rows(st):
        assert r["identity_holds"] == "true"
        assert float(r["not_captured_per_m2"]) >= -1e-30
    assert energy_conservation_ok(st)
    last = st.energy_rows[-1]
    per_cycle = (float(SEQ.tB.energy["integrated_source_energy_J"]) +
                 float(SEQ.tC.energy["integrated_source_energy_J"]) +
                 float(SEQ.tD_hold.energy["integrated_source_energy_J"]))
    assert abs(float(last["cumulative_source_J"]) - 3 * per_cycle) \
        <= 1e-12 * max(1.0, 3 * per_cycle)


def test_three_cycle_deterministic_equality():
    a, b = _campaign(), _campaign()
    assert a.trace_rows == b.trace_rows
    assert a.cycle_rows == b.cycle_rows
    assert a.panel_rows == b.panel_rows
    assert a.energy_rows == b.energy_rows


def test_interlock_refusal_persists():
    st = _campaign()
    refused = [r for r in st.trace_rows if r["result"] == "REFUSED"]
    assert st.n_refused == 3 and len(refused) == 3
    assert all(r["to"] == "MODE_D_SENSE.D_SENSING_HOLD" for r in refused)
    assert all("IL-08" in r["interlock_refs"] for r in refused)
    assert sorted(r["cycle"] for r in refused) == ["1", "2", "3"]
    assert st.final_state == "MODE_A_BASELINE.READY_IDLE"


def test_forced_saturation_falsification():
    vrep = run_verification_3d(CFG, Grid3DConfig(8, 8, 10))
    st = _campaign()
    rep = falsification_report(CFG, SEQ, vrep, SP, campaign=st)
    cond = {c["condition"]: c for c in rep["conditions"]}
    assert cond["cryopanel_saturation"]["falsified_in_model"] == "false"
    assert cond["cryopanel_saturation"]["status"] == "DERIVED_CHECK"
    assert cond["cumulative_energy_residual"]["falsified_in_model"] == "false"
    # force saturation: pre-load a panel to capacity and re-evaluate
    st.panels[1].N_per_m2 = st.panels[1].N_cap
    rep2 = falsification_report(CFG, SEQ, vrep, SP, campaign=st)
    cond2 = {c["condition"]: c for c in rep2["conditions"]}
    assert cond2["cryopanel_saturation"]["falsified_in_model"] == "true"
    assert cond2["cryopanel_saturation"]["status"] == "CONDITIONAL"


def test_zero_pass_scan():
    st = _campaign()
    blob = json.dumps([st.trace_rows, st.cycle_rows, st.panel_rows,
                       st.energy_rows, mass_bookkeeping_rows(st)])
    assert '"PASS"' not in blob
    assert "can_PASS_now" not in blob or '"NO"' in blob
    for r in st.cycle_rows:
        assert "never PASS" in r["il05_il06_derived_context"]


def test_single_cycle_regression_untouched():
    trace, summary = run_nominal_lifecycle(CFG, SEQ, SP, VM)
    assert summary["final_state"] == "MODE_A_BASELINE"  # canon
    assert summary["n_refused"] == 1                # unchanged canon
    vrep = run_verification_3d(CFG, Grid3DConfig(8, 8, 10))
    rep = falsification_report(CFG, SEQ, vrep, SP)  # no campaign arg
    assert rep["n_conditions"] == 11                # pre-Stage-2 count


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:            # noqa: BLE001
            print(f"  FAIL  {t.__name__}: {e!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)
