"""Repository-wide authority-consistency invariants.

MODEL-ONLY / FORECAST-ONLY. Software verification only; the scientific gate
PASS count remains zero and is asserted so below.

These tests exist because the repository's green unit suite did not prevent two
canonical paths from encoding *opposite* meanings for the same interlock ID.
A single scientific fact must have one explicit authority, and every executable
consumer must agree with it. Each test here crosses a module boundary on
purpose -- an isolated unit test cannot catch a contradiction between modules.
"""
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest                                                    # noqa: E402

from qta_multiphysics import machine_fsm as FSM                  # noqa: E402
from qta_multiphysics import state_machine_3d as SM3             # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ------------------------- heat-switch single authority --------------------
# AUTHORITIES.md registers machine_fsm.py as the authority for
# "States, transitions, interlocks, switches/valves/shutters" and
# state_machine_3d.py for "Per-mode device states". qta_full_sim.py is the
# gate authority and must not encode a competing switch semantic.

def test_mode_d_heat_switch_is_open_in_the_registered_authorities():
    """Mode D isolates: the SC heat switch is OPEN during sensing."""
    assert SM3.device_state("MODE_D").heat_switch_state == "OPEN"
    # and the thermalising modes keep it CLOSED, so the semantic is not simply
    # inverted everywhere
    assert SM3.device_state("MODE_A").heat_switch_state == "CLOSED"
    assert SM3.device_state("MODE_C").heat_switch_state == "CLOSED"


def test_fsm_interlock_forbids_sensing_with_switch_closed():
    """machine_fsm IL-04 must fire for sensing with the switch not OPEN."""
    interlocks = {i.id: i for i in FSM.INTERLOCKS}
    assert "IL-04" in interlocks
    text = interlocks["IL-04"].description.lower()
    assert "sensing" in text and "open" in text
    # executable check, not just wording: the violation detector must agree
    closed = FSM.hw(sc_heat_switch="CLOSED", microwave="on")
    opened = FSM.hw(sc_heat_switch="OPEN", microwave="on")
    ctx = {"sensing_phase": True}
    assert "IL-04" in FSM.hardware_interlock_violations(closed, ctx)
    assert "IL-04" not in FSM.hardware_interlock_violations(opened, ctx)


def test_qta_full_sim_agrees_with_the_registered_switch_authority():
    """The gate path must not encode the opposite Mode-D switch state.

    This is the regression guard for the contradiction this branch fixed:
    qta_full_sim previously asserted that sensing *required* the switch
    CLOSED, i.e. exactly the state machine_fsm.py calls an IL-04 violation.
    """
    import qta_full_sim as SIM
    ok = SIM.SystemState("D_ok", sensing_on=True, heat_switch_closed=False,
                         RGA_pass_CH4=True, RGA_pass_H2=True,
                         T_sample_ok=True, vib_settled=True)
    ok.validate()                       # OPEN during sensing must be legal

    bad = SIM.SystemState("D_bad", sensing_on=True, heat_switch_closed=True,
                          RGA_pass_CH4=True, RGA_pass_H2=True,
                          T_sample_ok=True, vib_settled=True)
    with pytest.raises(AssertionError, match="IL-04"):
        bad.validate()                  # CLOSED during sensing must be refused


def test_canonical_interlock_table_states_the_authoritative_semantic():
    rows = {r["id"]: r for r in
            csv.DictReader(open(ROOT / "interlock_table.csv"))}
    assert "IL-04" in rows
    cond = rows["IL-04"]["condition"].lower().replace(" ", "")
    assert "heat_switch_closed" in cond, (
        f"interlock_table IL-04 condition is {rows['IL-04']['condition']!r}; "
        "the impossible state is sensing with the switch CLOSED")


def test_no_canonical_path_encodes_mode_d_switch_closed():
    """Sweep the executable canonical paths for the inverted semantic."""
    import qta_full_sim as SIM
    src = (ROOT / "qta_full_sim.py").read_text()
    # make_D must construct the isolated state
    assert "heat_switch_closed = False" in src, (
        "make_D must build Mode D with the heat switch OPEN")
    # and the gate table must still be untouched by this
    rows = list(csv.DictReader(open(ROOT / "results_gate_table.csv")))
    assert len(rows) == 83
    assert sum(r["status"] == "PASS" for r in rows) == 0
    assert SIM is not None
