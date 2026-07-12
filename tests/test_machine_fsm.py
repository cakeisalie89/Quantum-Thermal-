"""Tests for the hierarchical machine FSM (operational controller).

MODEL-ONLY / FORECAST-ONLY. All checks verify control-logic behavior of the
theoretical-machine model. Zero PASS in any asserted artifact.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.config import default_config
from qta_multiphysics.mesh_3d import Grid3DConfig
from qta_multiphysics.machine_fsm import (
    STATES, TOP_LEVEL, TRANSITIONS, FORBIDDEN, INTERLOCKS, HARDWARE_AXES,
    MachineFSM, TransitionRefused, hardware_interlock_violations, hw,
    state_rows, transition_rows, interlock_rows, mermaid_diagram,
    run_nominal_lifecycle,
)
from qta_multiphysics.mode_sequence_3d import run_mode_sequence_3d
from qta_multiphysics.species_accounting_3d import species_accounting_rows
from qta_multiphysics.vibration_transfer import vibration_transfer

CFG = default_config()
G3 = Grid3DConfig(nx=8, ny=8, nz=10)


def _ctx(**over):
    c = {"interlock_chain_ok": True, "ivc_valve_closed": True,
         "P_H2_Pa": 1.0e-12, "stage_4K_ready": True,
         "stage_100mK_ready": True, "T_probe_K": 0.010,
         "dwell_s": 4.0, "he3_present": False,
         "helium_film_present": False, "sc_switch_open": True,
         "laser_off": True, "c13_feed_off": True,
         "theta_c13_residual": 1e-9, "mode_b_complete": True,
         "rga_ch4_clear": True, "rga_h2_clear": True,
         "sense_shutter_closed": True, "vib_amplitude_m": 1e-11,
         "he_dose_off": True, "all_gas_lines_closed": True,
         "trip_cleared": True, "operator_ack": True,
         "sensing_phase": False, "growth_active": False,
         "operational_mode": False, "warmup_phase": False}
    c.update(over)
    return c


def test_state_table_schema_and_hierarchy():
    """Every state carries all nineteen aspects; substates have valid parents;
    the lifecycle top level covers the full operational envelope."""
    names = {s.name for s in STATES}
    for s in STATES:
        assert s.parent == "" or s.parent in names, s.name
        assert s.parent == "" or s.name.startswith(s.parent + "."), s.name
        row = s.row()
        for k in ("purpose", "entry_conditions", "exit_conditions",
                  "sensors_monitored", "validation_conditions", "interlocks",
                  "thermal_configuration", "gas_configuration",
                  "optical_configuration", "microwave_configuration",
                  "cryogenic_configuration", "outputs_produced",
                  "physics_modules", "recovery_behavior", "fault_behavior",
                  "hardware_enabled", "hardware_disabled"):
            assert str(row[k]).strip() != "", f"{s.name}.{k} empty"
    for top in ("OFFLINE", "INIT", "PREP_VACUUM", "COOLDOWN",
                "MODE_A_BASELINE", "MODE_B_PROCESS", "MODE_C_RECOVERY",
                "MODE_D_SENSE", "SAFE_RECOVERY", "FAULT_LATCHED", "SHUTDOWN"):
        assert top in TOP_LEVEL, top


def test_transition_tables_closed_and_disjoint():
    """Allowed/forbidden endpoints are declared states; the sets are disjoint;
    every allowed transition carries a physical justification."""
    names = {s.name for s in STATES}
    allowed = set()
    for t in TRANSITIONS:
        assert t["from"] in names and t["to"] in names, t
        assert t["justification"].strip()
        allowed.add((t["from"], t["to"]))
    for a, b, il, why in FORBIDDEN:
        assert a in names and b in names
        assert (a, b) not in allowed, f"forbidden pair also allowed: {a}->{b}"
        assert il and why


def test_forbidden_growth_sensing_pairs_raise():
    for src, dst in (("MODE_B_PROCESS", "MODE_D_SENSE"),
                     ("MODE_D_SENSE", "MODE_B_PROCESS"),
                     ("MODE_A_BASELINE", "MODE_D_SENSE")):
        f = MachineFSM(src)
        try:
            f.request(dst, _ctx())
        except TransitionRefused as e:
            assert "forbidden" in str(e)
            assert f.state == src, "state mutated on refusal"
        else:
            raise AssertionError(f"{src}->{dst} not refused")


def test_guard_blocking_before_readiness():
    """Subsequent states are unreachable before readiness conditions hold."""
    f = MachineFSM("COOLDOWN.BASE_APPROACH")
    try:
        f.request("MODE_A_BASELINE", _ctx(T_probe_K=0.5))
    except TransitionRefused as e:
        assert "T_probe_K" in str(e)
    else:
        raise AssertionError("baseline entered before base temperature")
    f2 = MachineFSM("MODE_C_RECOVERY.C_PURGE")
    try:
        f2.request("MODE_C_RECOVERY.C_THERMAL_RECOVERY",
                   _ctx(theta_c13_residual=1e-3))
    except TransitionRefused as e:
        assert "theta_c13_residual" in str(e)
    else:
        raise AssertionError("recovery entered before purge floor")


def test_sensing_hold_blocked_by_il07_and_il08():
    f = MachineFSM("MODE_D_SENSE.D_HE_DOSE")
    try:
        f.request("MODE_D_SENSE.D_SENSING_HOLD",
                  _ctx(T_probe_K=0.020, vib_amplitude_m=5e-9,
                       operational_mode=True))
    except TransitionRefused as e:
        assert "IL-07" in e.ref and "IL-08" in e.ref
    else:
        raise AssertionError("sensing hold entered outside Ramsey bounds")
    # vibration alone
    f2 = MachineFSM("MODE_D_SENSE.D_HE_DOSE")
    try:
        f2.request("MODE_D_SENSE.D_SENSING_HOLD",
                   _ctx(T_probe_K=0.010, vib_amplitude_m=5e-9,
                        operational_mode=True))
    except TransitionRefused as e:
        assert e.ref == "IL-08", e.ref
    else:
        raise AssertionError("vibration bound not enforced")


def test_hardware_interlocks_impossible_class():
    """Illegal hardware combinations violate the canonical interlocks."""
    v = hardware_interlock_violations(
        hw(laser="on", sc_heat_switch="CLOSED"), {})
    assert "IL-03" in v
    v = hardware_interlock_violations(hw(c13_feed="on", he_dose="on"), {})
    assert "IL-02" in v
    v = hardware_interlock_violations(hw(laser="on", microwave="on"),
                                      {"sensing_phase": False})
    assert "FSM-IL-15" in v
    v = hardware_interlock_violations(hw(laser="on"),
                                      {"he3_present": True})
    assert "IL-09" in v
    v = hardware_interlock_violations(hw(charcoal_regen="on",
                                         ivc_valve="open"), {})
    assert "IL-12" in v
    assert hardware_interlock_violations(hw(), {}) == []


def test_safe_recovery_and_fault_latch_paths():
    f = MachineFSM("MODE_B_PROCESS.B_GROWTH_ACTIVE")
    rec = f.trip_safe_recovery("energy_residual_MODE_B")
    assert f.state == "SAFE_RECOVERY" and rec["result"] == "TRIPPED"
    f.request("MODE_A_BASELINE.STABILIZE", _ctx())      # guarded hand-back
    assert f.state == "MODE_A_BASELINE.STABILIZE"
    f2 = MachineFSM("MODE_D_SENSE.D_HE_DOSE")
    f2.latch_fault("IL-02")
    assert f2.state == "FAULT_LATCHED"
    f2.request("OFFLINE.MAINTENANCE", _ctx(operator_ack=True))
    assert f2.state == "OFFLINE.MAINTENANCE"


def test_nominal_lifecycle_trace_deterministic_and_honest():
    seq = run_mode_sequence_3d(CFG, G3, n_eval_b=7, n_eval_c=17)
    sp = species_accounting_rows(CFG)
    _, vm = vibration_transfer()
    rows1, sum1 = run_nominal_lifecycle(CFG, seq, sp, vm)
    rows2, sum2 = run_nominal_lifecycle(CFG, seq, sp, vm)
    assert rows1 == rows2 and sum1 == sum2, "trace not deterministic"
    assert sum1["final_state"] == "MODE_A_BASELINE"
    assert sum1["n_refused"] == 1
    ref = [r for r in rows1 if r["result"] == "REFUSED"][0]
    assert ref["to"] == "MODE_D_SENSE.D_SENSING_HOLD"
    assert ref["interlock_refs"] == "IL-08"
    assert "REFUSED" in sum1["campaign_status"]
    assert sum1["can_PASS_now"] == "NO"
    assert sum1["measured_in_this_system"] is False


def test_tables_and_diagram_no_pass_tokens():
    rows = state_rows() + transition_rows() + interlock_rows()
    txt = " ".join(str(v) for r in rows for v in r.values())
    assert '"PASS"' not in txt and ",PASS," not in txt
    d = mermaid_diagram()
    assert d.startswith("stateDiagram-v2")
    for top in TOP_LEVEL:
        assert top.replace(".", "_") in d.replace(" ", "") or top in d
    assert ',PASS,' not in d and '"PASS"' not in d


def test_hardware_axes_cover_every_state():
    for s in STATES:
        assert set(s.hardware) == set(HARDWARE_AXES), s.name


def test_interlock_registry_complete():
    ids = [i.id for i in INTERLOCKS]
    assert ids[:14] == [f"IL-{k:02d}" for k in range(1, 15)]
    assert all(i.kind in ("IMPOSSIBLE", "BLOCKED") for i in INTERLOCKS)
    assert len(INTERLOCKS) == 18


ALL_TESTS = [
    test_state_table_schema_and_hierarchy,
    test_transition_tables_closed_and_disjoint,
    test_forbidden_growth_sensing_pairs_raise,
    test_guard_blocking_before_readiness,
    test_sensing_hold_blocked_by_il07_and_il08,
    test_hardware_interlocks_impossible_class,
    test_safe_recovery_and_fault_latch_paths,
    test_nominal_lifecycle_trace_deterministic_and_honest,
    test_tables_and_diagram_no_pass_tokens,
    test_hardware_axes_cover_every_state,
    test_interlock_registry_complete,
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
