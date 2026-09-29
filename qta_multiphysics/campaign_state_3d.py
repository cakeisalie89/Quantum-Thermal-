"""Campaign continuity layer: carried state across repeated B->C->D cycles.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

A separate campaign API on top of the unchanged single-cycle canon:
``run_nominal_lifecycle`` and every single-cycle default are untouched.
``build_campaign(cfg, seq, species_rows, vib_metrics, n_cycles)`` walks the
machine FSM through N consecutive processing->recovery->sensing cycles and
carries, cycle to cycle:

- temperature (identical-cycle approximation, stated: each cycle reuses the
  deterministic single-cycle thermal solutions; the recovery end-state
  vs. the nominal base start is recorded as the approximation delta),
- residual C-13 contamination (worst-case theta0=1 re-declared at each
  Mode B, canonical purge each Mode C -- the established worst-case style),
- surface coverage (He dose per cycle from the canonical window),
- device state (single authority: state_machine_3d via the FSM),
- cryopanel loading (cryopanel_dynamics_3d; the genuinely accumulating
  cross-cycle state),
- cumulative energy (phase-level closures summed; see the ledger section).

IL-05/IL-06 derived context: the campaign records, per cycle, the model
quantities behind the clearance flags (purge residual, panel monolayer
fractions). The flags themselves keep their FORECAST-basis semantics --
this layer adds derived context and can never produce a PASS or an
automatic clearance upgrade. Deterministic throughout.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .machine_fsm import MachineFSM, TransitionRefused, LABEL as FSM_LABEL
from .cryopanel_dynamics_3d import (new_panel_set, advance_phase,
                                    phase_windows_s, N_ML_CAP)
from .species_accounting_3d import cryopanel_operating_point

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

SCHEMA_VERSION = "1.0"
DEFAULT_CYCLES = 3

_CYCLE_PATH = [
    "MODE_B_PROCESS", "MODE_B_PROCESS.B_PRECONFIG",
    "MODE_B_PROCESS.B_GROWTH_ACTIVE", "MODE_B_PROCESS.B_SOURCE_OFF",
    "MODE_C_RECOVERY", "MODE_C_RECOVERY.C_PURGE",
    "MODE_C_RECOVERY.C_THERMAL_RECOVERY",
    "MODE_C_RECOVERY.C_READINESS_VERIFY",
    "MODE_D_SENSE", "MODE_D_SENSE.D_PRECONFIG", "MODE_D_SENSE.D_HE_DOSE",
    "MODE_D_SENSE.D_SENSING_HOLD",          # expected honest refusal (IL-08)
    "MODE_D_SENSE.D_POST",
    "MODE_A_BASELINE", "MODE_A_BASELINE.STABILIZE",
    "MODE_A_BASELINE.READY_IDLE",
]

_PROLOGUE = [
    "INIT", "PREP_VACUUM", "PREP_VACUUM.PUMPDOWN", "PREP_VACUUM.BAKEOUT",
    "PREP_VACUUM.POST_BAKEOUT_VERIFY", "COOLDOWN",
    "COOLDOWN.PRECOOL_UPSTREAM", "COOLDOWN.DILUTION_CONDENSE",
    "COOLDOWN.BASE_APPROACH", "MODE_A_BASELINE",
    "MODE_A_BASELINE.STABILIZE", "MODE_A_BASELINE.READY_IDLE",
]


@dataclass
class CampaignState:
    n_cycles: int
    panels: list = field(default_factory=new_panel_set)
    trace_rows: list = field(default_factory=list)
    panel_rows: list = field(default_factory=list)
    cycle_rows: list = field(default_factory=list)
    energy_rows: list = field(default_factory=list)
    cumulative: dict = field(default_factory=dict)
    n_refused: int = 0
    approx_delta_K: float = 0.0
    final_state: str = ""


def _base_ctx(cfg, seq, species_rows, vib_metrics):
    Tbase = float(cfg.fridge.T_fridge_K)
    probe_C_end = float(seq.tC.probe_timeseries_K()[-1])
    resC13 = float([r for r in species_rows if r["phase"] == "MODE_C"
                    and r["species"] == "C13_CH4"][0]["coverage_end"])
    amp = float(vib_metrics["nv_output_amplitude_m"])
    dwell = float(vib_metrics["max_settling_time_s"])
    return {
        "interlock_chain_ok": True, "ivc_valve_closed": True,
        "P_H2_Pa": 1.0e-12, "stage_4K_ready": True, "stage_100mK_ready": True,
        "T_probe_K": Tbase, "dwell_s": dwell, "he3_present": False,
        "helium_film_present": False, "sc_switch_open": True,
        "laser_off": True, "c13_feed_off": True,
        "theta_c13_residual": resC13, "mode_b_complete": True,
        "rga_ch4_clear": True, "rga_h2_clear": True,
        "sense_shutter_closed": True, "vib_amplitude_m": amp,
        "he_dose_off": True, "all_gas_lines_closed": True,
        "trip_cleared": True, "operator_ack": True,
        "sensing_phase": False, "growth_active": False,
        "operational_mode": False, "warmup_phase": False,
    }, Tbase, probe_C_end, resC13


def build_campaign(cfg, seq, species_rows, vib_metrics,
                   n_cycles: int = DEFAULT_CYCLES) -> CampaignState:
    """Deterministic N-cycle campaign with carried state (separate API;
    single-cycle canon untouched)."""
    st = CampaignState(n_cycles=n_cycles)
    ctx0, Tbase, probeC, resC13 = _base_ctx(cfg, seq, species_rows,
                                            vib_metrics)
    st.approx_delta_K = abs(probeC - Tbase)
    op = cryopanel_operating_point()
    windows = phase_windows_s(cfg, op)
    fsm = MachineFSM("OFFLINE")
    step = 0

    def go(dst, cycle, **over):
        nonlocal step
        step += 1
        ctx = dict(ctx0)
        ctx.update(over)
        try:
            rec = fsm.request(dst, ctx)
        except TransitionRefused as e:
            rec = {"from": e.src, "to": e.dst, "result": "REFUSED",
                   "guards": e.reason, "interlock_refs": e.ref,
                   "justification": "guard/interlock enforcement (honest "
                                    "refusal; identical every cycle)",
                   "label": FSM_LABEL}
            st.n_refused += 1
        rec["step"] = str(step)
        rec["cycle"] = str(cycle)
        st.trace_rows.append(rec)
        return rec

    for dst in _PROLOGUE:
        go(dst, 0)

    for cyc in range(1, n_cycles + 1):
        # ---- Mode B: exposure + growth (methane live only here) ----
        go("MODE_B_PROCESS", cyc, operational_mode=True)
        go("MODE_B_PROCESS.B_PRECONFIG", cyc, operational_mode=True)
        go("MODE_B_PROCESS.B_GROWTH_ACTIVE", cyc, operational_mode=True,
           growth_active=True)
        advance_phase(st.panels, "MODE_B", windows["MODE_B"], op)
        for p in st.panels:
            st.panel_rows.append(p.row(cyc, "MODE_B"))
        go("MODE_B_PROCESS.B_SOURCE_OFF", cyc, operational_mode=True)
        # ---- Mode C: purge + recovery ----
        go("MODE_C_RECOVERY", cyc, operational_mode=True)
        go("MODE_C_RECOVERY.C_PURGE", cyc, operational_mode=True)
        advance_phase(st.panels, "MODE_C", windows["MODE_C"], op)
        for p in st.panels:
            st.panel_rows.append(p.row(cyc, "MODE_C"))
        go("MODE_C_RECOVERY.C_THERMAL_RECOVERY", cyc, operational_mode=True,
           T_probe_K=probeC)
        go("MODE_C_RECOVERY.C_READINESS_VERIFY", cyc, operational_mode=True,
           T_probe_K=probeC)
        # ---- Mode D: dose + (refused) sensing hold ----
        go("MODE_D_SENSE", cyc, operational_mode=True, T_probe_K=probeC)
        go("MODE_D_SENSE.D_PRECONFIG", cyc, operational_mode=True,
           T_probe_K=probeC)
        go("MODE_D_SENSE.D_HE_DOSE", cyc, operational_mode=True,
           T_probe_K=probeC)
        advance_phase(st.panels, "MODE_D", windows["MODE_D"], op)
        for p in st.panels:
            st.panel_rows.append(p.row(cyc, "MODE_D"))
        go("MODE_D_SENSE.D_SENSING_HOLD", cyc, operational_mode=True,
           sensing_phase=True, T_probe_K=probeC)
        go("MODE_D_SENSE.D_POST", cyc, operational_mode=True,
           T_probe_K=probeC)
        go("MODE_A_BASELINE", cyc, T_probe_K=Tbase)
        go("MODE_A_BASELINE.STABILIZE", cyc)
        go("MODE_A_BASELINE.READY_IDLE", cyc)
        # ---- per-cycle carried-state summary ----
        ch4 = [p for p in st.panels if p.species == "C13_CH4"][0]
        h2 = [p for p in st.panels if p.species == "H2"][0]
        st.cycle_rows.append({
            "cycle": str(cyc),
            "theta_c13_residual_before_D": f"{resC13:.9e}",
            "theta_basis": "worst-case theta0=1 re-declared each Mode B; "
                           "canonical purge each Mode C (identical-cycle)",
            "panel_CH4_monolayer_fraction": f"{ch4.monolayer_fraction:.9e}",
            "panel_H2_monolayer_fraction": f"{h2.monolayer_fraction:.9e}",
            "T_probe_recovery_end_K": f"{probeC:.9e}",
            "T_base_K": f"{Tbase:.9e}",
            "identical_cycle_approx_delta_K": f"{st.approx_delta_K:.3e}",
            "il05_il06_derived_context": (
                f"FORECAST-basis clearance; derived context: theta_residual="
                f"{resC13:.3e}, panel_CH4_ML={ch4.monolayer_fraction:.3e}, "
                f"panel_H2_ML={h2.monolayer_fraction:.3e}; measured RGA "
                "clearance NOT AVAILABLE (E04); no automatic upgrade, "
                "never PASS"),
            "sensing_hold": "REFUSED [IL-08] (identical every cycle)",
            "label": LABEL})
    st.final_state = fsm.state
    return st

# --------------------------------------------------------------------------
# CP3: cumulative cross-phase / cross-cycle energy ledger + bookkeeping
# --------------------------------------------------------------------------
from .energy_accounting_3d import ENERGY_CLOSURE_TOL  # noqa: E402
from .numerics import resolution_class  # noqa: E402

#: Unit roundoff of IEEE double precision, 2**-53.
_UNIT_ROUNDOFF = 2.0 ** -53


def ledger_floor(unexplained_J: float, terms_abs_J: float,
                 operations: int) -> float:
    """What an energy ledger can resolve: the energy its own balance leaves
    UNEXPLAINED, plus the rounding bound of the arithmetic that formed it.

    A phase closes ``source - sink - dU = residual`` by quadrature of the
    solution; ``|residual|`` is energy the accounting cannot place. A term of
    the ledger smaller than that is not distinguished from zero by it --
    whatever the digits say. Summed over phases, the unexplained parts add
    in magnitude (signed residuals could cancel; what they fail to explain
    does not). The rounding term, ``operations * u * sum|terms|``, is the
    first-order bound for that many floating-point additions over those
    magnitudes; it keeps the floor positive for a closure that happens to
    land on exactly zero, and it is ~1e-25 J here, far below every residual.

    This is not a tolerance chosen to absorb a difference between hosts. It
    is the ledger's own closure, and it is what makes the cycle-1 MODE_C
    cumulative dU -- a cancellation of +9.114e-12 J against -9.114e-12 J
    that leaves one ulp on one backend and nothing on another -- BELOW
    RESOLUTION on both, instead of an exact zero on one and a claim of
    1.6e-27 J of stored energy on the other (D-2026-98).
    """
    return abs(unexplained_J) + operations * _UNIT_ROUNDOFF * terms_abs_J


def attach_energy_ledger(st: CampaignState, seq) -> None:
    """Cumulative energy ledger from the EXISTING phase-level closures.

    Identical-cycle basis: every cycle reuses the deterministic single-cycle
    phase energies (stated approximation). No new physics; sums only.

    Every energy term is published with its resolution class against the
    ledger's own floor (:func:`ledger_floor`), per phase and cumulatively,
    and the floor is published beside it.
    """
    phases = [("MODE_B", seq.tB.energy), ("MODE_C", seq.tC.energy)]
    if seq.tD_hold is not None:
        phases.append(("MODE_D_HOLD", seq.tD_hold.energy))
    cum = {"source_J": 0.0, "sink_J": 0.0, "dU_J": 0.0, "residual_J": 0.0}
    unexplained = terms_abs = 0.0
    n_phases = 0
    for cyc in range(1, st.n_cycles + 1):
        for name, e in phases:
            src = float(e["integrated_source_energy_J"])
            snk = float(e["boundary_sink_energy_J"])
            dU = float(e["internal_energy_change_J"])
            res = float(e["residual_J"])
            cum["source_J"] += src; cum["sink_J"] += snk
            cum["dU_J"] += dU; cum["residual_J"] += res
            denom = max(abs(cum["source_J"]), abs(cum["sink_J"]),
                        abs(cum["dU_J"]), 1e-30)
            phase_abs = abs(src) + abs(snk) + abs(dU)
            # the phase's own balance: two subtractions
            phase_floor = ledger_floor(res, phase_abs, 2)
            n_phases += 1
            unexplained += abs(res)
            terms_abs += phase_abs
            # every phase balance, plus a running sum of n_phases terms
            cum_floor = ledger_floor(unexplained, terms_abs, n_phases + 2)
            st.energy_rows.append({
                "cycle": str(cyc), "phase": name,
                "phase_source_J": f"{src:.9e}", "phase_sink_J": f"{snk:.9e}",
                "phase_dU_J": f"{dU:.9e}", "phase_residual_J": f"{res:.9e}",
                "phase_rel_residual": f"{float(e['rel_residual']):.6e}",
                "phase_resolution_floor_J": f"{phase_floor:.9e}",
                "phase_source_J_resolution":
                    resolution_class(src, phase_floor),
                "phase_sink_J_resolution": resolution_class(snk, phase_floor),
                "phase_dU_J_resolution": resolution_class(dU, phase_floor),
                "cumulative_source_J": f"{cum['source_J']:.9e}",
                "cumulative_sink_J": f"{cum['sink_J']:.9e}",
                "cumulative_dU_J": f"{cum['dU_J']:.9e}",
                "cumulative_residual_J": f"{cum['residual_J']:.9e}",
                "cumulative_rel_residual":
                    f"{cum['residual_J']/denom:.6e}",
                "cumulative_resolution_floor_J": f"{cum_floor:.9e}",
                "cumulative_source_J_resolution":
                    resolution_class(cum["source_J"], cum_floor),
                "cumulative_sink_J_resolution":
                    resolution_class(cum["sink_J"], cum_floor),
                "cumulative_dU_J_resolution":
                    resolution_class(cum["dU_J"], cum_floor),
                "basis": "identical-cycle sums of the existing phase-level "
                         "closures; no new physics",
                "label": LABEL})
    st.cumulative = dict(cum)
    st.cumulative["closure_tol"] = ENERGY_CLOSURE_TOL
    if st.energy_rows:
        last = st.energy_rows[-1]
        st.cumulative["resolution_floor_J"] = float(
            last["cumulative_resolution_floor_J"])
        for k in ("source_J", "sink_J", "dU_J"):
            st.cumulative[f"{k}_resolution"] = last[
                f"cumulative_{k}_resolution"]


def energy_conservation_ok(st: CampaignState) -> bool:
    """Cumulative closure inherits the shared phase tolerance
    (identical-cycle sums cannot degrade the relative residual)."""
    if not st.energy_rows:
        return False
    return abs(float(st.energy_rows[-1]["cumulative_rel_residual"])) \
        <= ENERGY_CLOSURE_TOL


def mass_bookkeeping_rows(st: CampaignState) -> list:
    """Per-panel admitted = captured + not-captured (pumped/reflected);
    identity by construction, reported for auditability."""
    rows = []
    for p in st.panels:
        adm, cap = p.admitted_per_m2, p.N_per_m2
        rows.append({"panel_id": p.panel_id, "species": p.species,
                     "admitted_per_m2": f"{adm:.9e}",
                     "captured_per_m2": f"{cap:.9e}",
                     "not_captured_per_m2": f"{adm-cap:.9e}",
                     "identity_holds": str(bool(adm - cap >= -1e-30)).lower(),
                     "note": "not-captured = pumped/reflected (chain-level; "
                             "no spatial claim)", "label": LABEL})
    return rows
