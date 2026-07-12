"""Hierarchical operational finite-state machine for the QTA machine model.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

This module expands the canonical Mode A/B/C/D controller into the primary
OPERATIONAL controller of the theoretical machine: every physical stage of
operation is an explicit state (with substates under the major modes), every
transition is guarded by physical readiness conditions with gate/interlock
provenance, and every illegal hardware combination is prevented by an
interlock predicate over the hardware axes. The FSM is operational control
logic for a THEORETICAL machine and its simulation framework -- it makes no
hardware-validation claims and can never emit PASS.

Grounding (repository sources of truth): the canonical mode map and species
policy (mode_sequence_3d), the device-state logic (state_machine_3d, gates
A1/A3), the 14 canonical interlocks (design registry, interlock_table.csv
IL-01..IL-14), the engineering-readiness E-gates (E01 bakeout .. E14 first
measurements), the chamber pre/post-bakeout states, the stage chain
300K->77K->4K->1K->100mK->10mK, the SC heat-switch DESIGN_SPECIFIED
conductances, the canonical purge/dose windows, the vibration settling and
amplitude metrics, and the falsification-engine condition taxonomy for fault
classes. Quantities with no canonical parameterization are honestly
NOT_IMPLEMENTED (e.g. warmup rates, pumpdown curves).

Deterministic: no randomness, no timestamps, no machine paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

# --------------------------------------------------------------------------
# hardware axes (only subsystems the repository already represents)
# --------------------------------------------------------------------------
HARDWARE_AXES = (
    "laser", "microwave", "c13_feed", "he_dose", "sense_shutter",
    "process_port", "cryobaffle", "sc_heat_switch", "upstream_plant",
    "dilution", "vacuum_pumping", "ivc_valve", "charcoal_regen",
)

_DEF_HW = dict(laser="off", microwave="off", c13_feed="off", he_dose="off",
               sense_shutter="closed", process_port="closed",
               cryobaffle="engaged", sc_heat_switch="CLOSED",
               upstream_plant="off", dilution="off", vacuum_pumping="off",
               ivc_valve="closed", charcoal_regen="off")


def hw(**over) -> dict:
    d = dict(_DEF_HW)
    d.update(over)
    unknown = set(over) - set(HARDWARE_AXES)
    if unknown:
        raise ValueError(f"unknown hardware axes: {sorted(unknown)}")
    return d


# --------------------------------------------------------------------------
# interlocks: canonical IL-01..IL-14 mapped onto hardware/context predicates,
# plus FSM-level interlocks for combinations the state expansion exposes.
# type IMPOSSIBLE -> transition/state refused outright;
# type BLOCKED    -> refused until the context condition clears.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Interlock:
    id: str
    kind: str            # IMPOSSIBLE | BLOCKED
    description: str
    provenance: str

    def row(self):
        return {"id": self.id, "type": self.kind,
                "description": self.description,
                "provenance": self.provenance, "label": LABEL}


INTERLOCKS = [
    Interlock("IL-01", "IMPOSSIBLE", "laser (LCVD) active while sensing active",
              "canonical interlock table (thermal: 250x overload)"),
    Interlock("IL-02", "IMPOSSIBLE", "C-13 precursor feed while He dosing",
              "canonical interlock table (He-3 film in 2.6 s)"),
    Interlock("IL-03", "IMPOSSIBLE", "laser (LCVD) active with SC heat switch CLOSED",
              "canonical interlock table (heats mixing chamber; gate A1)"),
    Interlock("IL-04", "IMPOSSIBLE", "sensing with SC heat switch not OPEN",
              "canonical interlock table (sample not isolated at 10 mK)"),
    Interlock("IL-05", "BLOCKED", "sensing without RGA CH4 clearance",
              "canonical interlock table; RGA readiness gate E04"),
    Interlock("IL-06", "BLOCKED", "sensing without RGA H2 clearance",
              "canonical interlock table; RGA readiness gate E04"),
    Interlock("IL-07", "BLOCKED", "sensing with T_sample above 12 mK",
              "canonical interlock table (Ramsey bound)"),
    Interlock("IL-08", "BLOCKED", "sensing before vibration settled below the "
              "Mode-D amplitude threshold",
              "canonical interlock table; vibration chain metrics"),
    Interlock("IL-09", "IMPOSSIBLE", "He-3 present while laser (LCVD) active",
              "canonical interlock table"),
    Interlock("IL-10", "IMPOSSIBLE", "He-3 present while precursor feed on",
              "canonical interlock table (CH4 poisons He-3)"),
    Interlock("IL-11", "BLOCKED", "Mode D entry without a completed Mode B->C "
              "cycle (purge required)",
              "canonical interlock table"),
    Interlock("IL-12", "BLOCKED", "charcoal regeneration with IVC valve open",
              "canonical interlock table (gas burst contaminates)"),
    Interlock("IL-13", "IMPOSSIBLE", "growth active while He dosing active",
              "canonical interlock table (modes mutually exclusive)"),
    Interlock("IL-14", "IMPOSSIBLE", "laser (LCVD) active with helium film present",
              "canonical interlock table (He film blocks LCVD)"),
    # FSM-level interlocks exposed by the state expansion
    Interlock("FSM-IL-15", "IMPOSSIBLE", "laser and microwave drive simultaneously",
              "mode map: laser is Mode-B-only, MW is Mode-D-only"),
    Interlock("FSM-IL-16", "IMPOSSIBLE", "process port open during any sensing "
              "substate", "gate A3 shielding logic; state machine"),
    Interlock("FSM-IL-17", "IMPOSSIBLE", "any operational mode (A/B/C/D) with the "
              "dilution stage off", "stage chain: 10 mK base requires dilution"),
    Interlock("FSM-IL-18", "IMPOSSIBLE", "controlled warmup with any gas feed or "
              "dose line open", "gas-handling safety; species policy"),
]
_IL = {i.id: i for i in INTERLOCKS}


def hardware_interlock_violations(hwd: dict, ctx: dict) -> list:
    """Interlock ids violated by a hardware configuration in a context."""
    v = []
    sensing = ctx.get("sensing_phase", False)
    if hwd["laser"] == "on" and sensing:
        v.append("IL-01")
    if hwd["c13_feed"] == "on" and hwd["he_dose"] == "on":
        v.append("IL-02")
    if hwd["laser"] == "on" and hwd["sc_heat_switch"] == "CLOSED":
        v.append("IL-03")
    if sensing and hwd["sc_heat_switch"] != "OPEN":
        v.append("IL-04")
    if ctx.get("he3_present", False) and hwd["laser"] == "on":
        v.append("IL-09")
    if ctx.get("he3_present", False) and hwd["c13_feed"] == "on":
        v.append("IL-10")
    if hwd["charcoal_regen"] == "on" and hwd["ivc_valve"] == "open":
        v.append("IL-12")
    if ctx.get("growth_active", False) and hwd["he_dose"] == "on":
        v.append("IL-13")
    if hwd["laser"] == "on" and ctx.get("helium_film_present", False):
        v.append("IL-14")
    if hwd["laser"] == "on" and hwd["microwave"] == "on":
        v.append("FSM-IL-15")
    if sensing and hwd["process_port"] == "open":
        v.append("FSM-IL-16")
    if ctx.get("operational_mode", False) and hwd["dilution"] != "on":
        v.append("FSM-IL-17")
    if ctx.get("warmup_phase", False) and (hwd["c13_feed"] == "on"
                                           or hwd["he_dose"] == "on"):
        v.append("FSM-IL-18")
    return v


# --------------------------------------------------------------------------
# state specification (all nineteen required aspects)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class StateSpec:
    name: str
    parent: str                      # "" for top-level
    purpose: str
    entry_conditions: str
    exit_conditions: str
    hardware: dict                   # full axis map (enabled/disabled derivable)
    sensors_monitored: str           # model quantities watched (all exist in repo)
    validation_conditions: str       # gate-id provenance
    interlocks: str                  # applicable interlock ids
    thermal_configuration: str
    gas_configuration: str
    optical_configuration: str
    microwave_configuration: str
    cryogenic_configuration: str
    outputs_produced: str
    physics_modules: str
    recovery_behavior: str
    fault_behavior: str

    def hardware_enabled(self):
        on = {"on", "open", "OPEN", "engaged"}
        return sorted(a for a, s in self.hardware.items() if s in on)

    def hardware_disabled(self):
        return sorted(a for a in HARDWARE_AXES
                      if a not in self.hardware_enabled())

    def row(self):
        d = asdict(self)
        d.pop("hardware")
        d["hardware_enabled"] = ";".join(self.hardware_enabled())
        d["hardware_disabled"] = ";".join(self.hardware_disabled())
        d["label"] = LABEL
        return d


def _s(name, parent, purpose, entry, exit_, hwd, sensors, val, il, therm,
       gas, opt, mwc, cryo, outs, phys, rec, fault):
    return StateSpec(name, parent, purpose, entry, exit_, hwd, sensors, val,
                     il, therm, gas, opt, mwc, cryo, outs, phys, rec, fault)


_FAULT_REC = ("guarded transition to SAFE_RECOVERY (all sources off, ports "
              "closed, switch CLOSED for rethermalization)")
_FAULT_LATCH = ("IMPOSSIBLE-class violation or unrecoverable trip latches "
                "FAULT_LATCHED (operator acknowledgment required)")

STATES = [
    # ------------------------------ OFFLINE ------------------------------
    _s("OFFLINE", "", "machine de-energized at ambient; maintenance base",
       "from SHUTDOWN.VENT_PREP complete or FAULT_LATCHED acknowledged",
       "operator initiates INIT",
       hw(), "none (model quantities idle)",
       "none (pre-operational)", "IL-12 (during maintenance regen)",
       "ambient 300 K", "chamber vented or static", "all optical paths dark",
       "MW off", "all cryogenic stages off",
       "none", "none", "n/a (base state)", _FAULT_LATCH),
    _s("OFFLINE.MAINTENANCE", "OFFLINE",
       "maintenance access incl. sorb/charcoal regeneration",
       "OFFLINE reached; access declared", "maintenance complete",
       hw(charcoal_regen="on", vacuum_pumping="on"),
       "P_H2/P_CH4 (chamber state dict) during regen pumping",
       "E02 (NEG), E03 (77K charcoal cryotrap -- registry: NOT INSTALLED, "
       "DESIGN_SPECIFIED)",
       "IL-12 (regen requires IVC valve closed)",
       "ambient", "regen outgassing pumped; IVC valve CLOSED",
       "dark", "off", "off",
       "engineering_status entries (registry)", "none",
       "close regen, return to OFFLINE", _FAULT_LATCH),

    # -------------------------------- INIT --------------------------------
    _s("INIT", "", "power-on self-checks of controller, DAQ, and interlock "
       "chain of the theoretical machine model",
       "operator start from OFFLINE", "self-checks complete",
       hw(vacuum_pumping="on"),
       "interlock chain integrity (all 18 interlock predicates evaluable)",
       "engineering-readiness ledger E01..E10 statuses (registry)",
       "all (chain test)",
       "ambient", "roughing/turbo pumping enabled", "dark", "off", "off",
       "machine_fsm_summary.json", "none",
       "return to OFFLINE on failed self-check", _FAULT_LATCH),

    # ----------------------------- PREP_VACUUM ----------------------------
    _s("PREP_VACUUM", "", "establish UHV per the canonical chamber states",
       "INIT complete", "post-bakeout verification satisfied",
       hw(vacuum_pumping="on"),
       "P_H2_Pa, P_CH4_Pa (canonical chamber-state quantities)",
       "E01 (250C/48h UHV bakeout), E02 (NEG pump), E04 (RGA readiness)",
       "IL-05/IL-06 downstream (clearances originate here)",
       "bakeout thermal cycle (E01 parameters)", "pumpdown -> bakeout -> "
       "post-bakeout (P_H2 1e-10 -> 1e-12 Pa per CHAMBER_STATE)",
       "dark", "off", "off",
       "measured_parameters/chamber-state references", "none",
       "re-enter BAKEOUT on failed verification", _FAULT_REC),
    _s("PREP_VACUUM.PUMPDOWN", "PREP_VACUUM",
       "rough/turbo pumpdown to bakeout-start vacuum",
       "PREP_VACUUM entered", "pumpdown floor reached "
       "(NOT_IMPLEMENTED: no canonical pumpdown curve)",
       hw(vacuum_pumping="on", ivc_valve="open"),
       "P_H2_Pa, P_CH4_Pa", "E02", "IL-12",
       "ambient", "pumping, IVC open", "dark", "off", "off",
       "none", "none", "hold and continue pumping", _FAULT_REC),
    _s("PREP_VACUUM.BAKEOUT", "PREP_VACUUM",
       "250C/48h UHV bakeout (gate E01 parameters)",
       "pumpdown floor reached", "bakeout schedule complete",
       hw(vacuum_pumping="on", ivc_valve="open"),
       "chamber wall temperature (bakeout schedule), P_H2_Pa",
       "E01", "IL-12",
       "bakeout thermal cycle", "outgassing pumped", "dark", "off", "off",
       "chamber-state transition pre->post bakeout", "none",
       "extend bakeout on high residuals", _FAULT_REC),
    _s("PREP_VACUUM.POST_BAKEOUT_VERIFY", "PREP_VACUUM",
       "verify post-bakeout residuals and RGA clearances",
       "bakeout complete", "P_H2 at post-bakeout target; RGA clearances held",
       hw(vacuum_pumping="on"),
       "P_H2_Pa (target 1e-12 Pa post-bakeout), P_CH4_Pa, RGA species scan",
       "E04; clearance conditions feeding IL-05/IL-06",
       "IL-05, IL-06",
       "cooling from bakeout", "static UHV", "dark", "off", "off",
       "chamber-state record", "none",
       "return to BAKEOUT on failed verification", _FAULT_REC),

    # ------------------------------ COOLDOWN ------------------------------
    _s("COOLDOWN", "", "staged cryogenic cooldown along the canonical "
       "intercept chain 300K->77K->4K->1K->100mK->10mK",
       "post-bakeout verification satisfied", "base temperature reached",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "stage temperatures (canonical chain), switch leak forecast",
       "E05 (Ag sinter interface), E06 (suspension), RTB_JT plant gate",
       "IL-03 inverse (switch CLOSED is the thermalization state; laser "
       "structurally off)", "switch CLOSED thermalization",
       "static UHV", "dark", "off",
       "upstream RTB/JT-class plant + dilution condensation",
       "cryo_stack_3d_budget.csv references", "radiation_paths (chain)",
       "hold at last stable stage; re-approach", _FAULT_REC),
    _s("COOLDOWN.PRECOOL_UPSTREAM", "COOLDOWN",
       "upstream plant precool toward 4 K (RTB/JT-class gate; LN2 stage "
       "NOT_IMPLEMENTED in this repository)",
       "COOLDOWN entered", "4 K stage stable",
       hw(vacuum_pumping="on", upstream_plant="on", sc_heat_switch="CLOSED"),
       "77K/4K stage temperatures", "RTB_JT_OPTIONAL_COOLING_PLANT gate",
       "none additional", "upstream stages active", "static UHV", "dark",
       "off", "RTB/JT-class plant", "none", "radiation_paths",
       "hold at 77 K intercept", _FAULT_REC),
    _s("COOLDOWN.DILUTION_CONDENSE", "COOLDOWN",
       "dilution-refrigerator condensation and mK approach",
       "4 K stable", "100 mK stage reached",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "1K/100mK stage temperatures", "dilution stage (FridgeConfig)",
       "FSM-IL-17", "mixing-chamber circulation", "static UHV", "dark",
       "off", "dilution active", "none", "none",
       "recondense on loss of circulation", _FAULT_REC),
    _s("COOLDOWN.BASE_APPROACH", "COOLDOWN",
       "approach the 10 mK base with the sample thermalized (switch CLOSED)",
       "100 mK reached", "T_probe at base (T_fridge_K)",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "NV-probe temperature (3D model), switch leak forecast (CLOSED)",
       "FridgeConfig T_fridge_K = 10 mK; heat-switch DESIGN values",
       "FSM-IL-17", "switch CLOSED thermalization to MC", "static UHV",
       "dark", "off", "full stack at base",
       "thermal_3d probe references", "thermal_3d_transient (zero-source)",
       "dwell and re-approach", _FAULT_REC),

    # --------------------------- MODE_A_BASELINE ---------------------------
    _s("MODE_A_BASELINE", "", "canonical Mode A: cryogenic baseline / "
       "stabilization; machine ready-idle",
       "COOLDOWN.BASE_APPROACH complete", "operator selects processing "
       "(Mode B) after readiness",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "T_probe, vibration amplitude + settling time, P_H2 residual",
       "engineering-readiness ledger E01..E11 (Mode-A gate set)",
       "FSM-IL-17",
       "base temperature; switch CLOSED", "no active species (canonical "
       "Mode-A map)", "dark; sense shutter CLOSED (E08)", "off",
       "full stack at base",
       "thermal_3d_readiness.json baseline references",
       "thermal_3d_transient (zero-source stability)",
       "SAFE_RECOVERY re-enters here after rethermalization", _FAULT_REC),
    _s("MODE_A_BASELINE.STABILIZE", "MODE_A_BASELINE",
       "post-event settling dwell (vibration settling time 3.18 s canonical)",
       "MODE_A_BASELINE entered or SAFE_RECOVERY hand-back",
       "dwell >= max_settling_time_s and T_probe at base",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "vibration amplitude, max_settling_time_s, T_probe",
       "vibration chain metrics; E10", "IL-08 feeds forward", "as parent",
       "as parent", "dark", "off", "as parent", "none",
       "vibration_transfer", "extend dwell", _FAULT_REC),
    _s("MODE_A_BASELINE.READY_IDLE", "MODE_A_BASELINE",
       "ready-idle: all baseline validations current",
       "STABILIZE complete", "transition request to MODE_B_PROCESS",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "as parent", "as parent", "as parent", "as parent", "as parent",
       "dark", "off", "as parent", "none", "none",
       "n/a", _FAULT_REC),

    # ---------------------------- MODE_B_PROCESS ----------------------------
    _s("MODE_B_PROCESS", "", "canonical Mode B: LCVD growth / processing "
       "(C-13 methane precursor only)",
       "READY_IDLE + all Mode-B gate preconditions (A1..A4)",
       "growth complete; sources off",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN", process_port="open", laser="on",
          c13_feed="on"),
       "T_probe (3D), hotspot peak, switch leak (OPEN, gate A1), He absence "
       "(gate A4)",
       "Mode-B gate set A1..A23 (canonical table)",
       "IL-01, IL-02, IL-03, IL-09, IL-10, IL-13, IL-14, FSM-IL-15",
       "switch OPEN (MC protected, A1); processing heat to upstream dump",
       "C13_CH4 feed at the canonical working pressure; He structurally "
       "absent", "laser port open; sense shutter CLOSED (A3)",
       "off (FSM-IL-15)", "full stack; MC isolated",
       "thermal_3d_probe_timeseries/hotspots/energy; species accounting "
       "MODE_B rows", "thermal_3d_transient + laser_source_3d + "
       "mode_sequence_3d(B)",
       "source-off then MODE_C_RECOVERY", _FAULT_REC),
    _s("MODE_B_PROCESS.B_PRECONFIG", "MODE_B_PROCESS",
       "configure hardware for growth before any source is enabled",
       "MODE_B entered", "hardware verified in Mode-B configuration",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN", process_port="open"),
       "switch state, port states", "A1 (switch OPEN), A3 (shutter)",
       "IL-03", "switch OPEN", "feed still off", "port open, laser off",
       "off", "as parent", "none", "state_machine_3d",
       "revert to READY_IDLE", _FAULT_REC),
    _s("MODE_B_PROCESS.B_GROWTH_ACTIVE", "MODE_B_PROCESS",
       "laser + C-13 feed active: the growth window",
       "B_PRECONFIG verified", "pulse/process window complete",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN", process_port="open", laser="on",
          c13_feed="on"),
       "T_probe, hotspot, energy closure, coverage exposure",
       "Mode-B thermal/optical gates; energy_residual falsification bound",
       "IL-01/02/09/10/13/14, FSM-IL-15", "as parent",
       "C13_CH4 active (canonical species policy)",
       "laser ON via process port", "off", "as parent",
       "mode_recovery_3d_timeline MODE_B rows",
       "thermal_3d_transient(B) + laser_source_3d",
       "immediate source-off to B_SOURCE_OFF", _FAULT_REC),
    _s("MODE_B_PROCESS.B_SOURCE_OFF", "MODE_B_PROCESS",
       "growth inputs stopped before the Mode-C hand-off",
       "growth window complete", "sources verified off",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN", process_port="open"),
       "laser/feed states", "state hand-off (coupled_mode_solver semantics)",
       "none additional", "as parent", "feed off", "laser off", "off",
       "as parent", "none", "none", "proceed to MODE_C", _FAULT_REC),

    # ---------------------------- MODE_C_RECOVERY ----------------------------
    _s("MODE_C_RECOVERY", "", "canonical Mode C: isolation / purge / "
       "cryotrapping / thermal recovery",
       "B_SOURCE_OFF verified", "readiness verified for Mode D",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "theta residuals (purge model), T_probe recovery, recovery_time_s",
       "Mode-C gate sets (purge B1..B5 + recovery C1..C4); "
       "COUPLED_MODE_RECOVERY_CHECK",
       "IL-11 (this cycle satisfies it)",
       "switch CLOSED rethermalization from the Mode-B field",
       "purge (canonical purge_1_s=5.0, cryotrap rates); ports closed",
       "dark; shutter CLOSED", "off", "as parent",
       "mode_recovery_3d_timeline MODE_C rows; species_accounting MODE_C",
       "thermal_3d_transient(C decay) + surface_coverage purge",
       "extend recovery window", _FAULT_REC),
    _s("MODE_C_RECOVERY.C_PURGE", "MODE_C_RECOVERY",
       "species purge / cryotrapping of residual precursor",
       "MODE_C entered", "residual C-13 coverage at the purge-forecast floor",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "theta_C13 (worst-case purge forecast)", "purge gate set B1..B5",
       "feeds IL-05/IL-06 clearances", "as parent",
       "purge active (canonical parameters)", "dark", "off", "as parent",
       "species_accounting_3d.csv MODE_C row", "surface_coverage evolve",
       "extend purge window", _FAULT_REC),
    _s("MODE_C_RECOVERY.C_THERMAL_RECOVERY", "MODE_C_RECOVERY",
       "thermal recovery of the sample field toward the hand-off threshold",
       "purge floor reached", "T_probe <= mode_d_temp_threshold_K (50 mK)",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "T_probe timeseries, recovery_time_s", "recovery gate set C1..C4",
       "none additional", "switch CLOSED", "static", "dark", "off",
       "as parent", "mode_recovery_3d_timeline", "thermal_3d_transient(C)",
       "extend window; SAFE_RECOVERY if recovery falsified", _FAULT_REC),
    _s("MODE_C_RECOVERY.C_READINESS_VERIFY", "MODE_C_RECOVERY",
       "snapshot the Mode-D readiness conditions before hand-off",
       "thermal recovery achieved", "readiness snapshot recorded",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "T_probe, theta residuals, vibration amplitude",
       "IL-05..IL-08 condition snapshot; falsification report",
       "IL-05..IL-08, IL-11", "as parent", "static", "dark", "off",
       "as parent", "falsification_report_3d.json references",
       "falsification_3d", "return to C_THERMAL_RECOVERY", _FAULT_REC),

    # ------------------------------ MODE_D_SENSE ------------------------------
    _s("MODE_D_SENSE", "", "canonical Mode D: He-3/He-4 sensing / ODMR / "
       "Ramsey forecast",
       "C_READINESS_VERIFY complete (IL-11 satisfied)",
       "sensing campaign complete or blocked",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN", microwave="on", he_dose="on"),
       "T_probe (12 mK sensing bound IL-07), vibration amplitude (IL-08), "
       "coverage theta_He3/He4, MW load",
       "Mode-D gate set D1..D26; nv_eligibility_3d forecast",
       "IL-01, IL-04, IL-05, IL-06, IL-07, IL-08, FSM-IL-15, FSM-IL-16",
       "switch OPEN isolation (IL-04)", "He-3/He-4 dose at the canonical "
       "condition; C-13 structurally forbidden",
       "sense shutter CLOSED; collection path per E11", "ODMR/ESR drive "
       "(canonical line model)", "full stack; sample isolated",
       "surface_coverage_3d_summary.csv; nv_eligibility_3d.json; "
       "mode_recovery_3d_timeline MODE_D(+HOLD) rows",
       "mode_sequence_3d(D hold) + sources_3d(MW map) + surface_coverage_3d",
       "D_POST then MODE_C re-recovery or MODE_A", _FAULT_REC),
    _s("MODE_D_SENSE.D_PRECONFIG", "MODE_D_SENSE",
       "configure isolation and drive before dosing",
       "MODE_D entered", "hardware verified (switch OPEN, shutter CLOSED)",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN"),
       "switch state, shutter state", "IL-04, gate A3 logic",
       "IL-04, FSM-IL-16", "switch OPEN", "no dose yet", "shutter CLOSED",
       "off (armed)", "as parent", "none", "state_machine_3d",
       "revert to C_READINESS_VERIFY", _FAULT_REC),
    _s("MODE_D_SENSE.D_HE_DOSE", "MODE_D_SENSE",
       "admit He-3/He-4 at the canonical dose condition",
       "D_PRECONFIG verified + clearances (IL-05/06) held",
       "canonical dose window complete",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN", he_dose="on"),
       "coverage theta_He3/He4, P_dose", "coverage window (canonical 1.0 s); "
       "species accounting MODE_D rows", "IL-02/09/10/13 (inverse: no "
       "precursor/laser)", "as parent", "He dose active", "dark", "off",
       "as parent", "surface_coverage_3d_summary.csv", "surface_coverage_3d",
       "close dose, re-verify", _FAULT_REC),
    _s("MODE_D_SENSE.D_SENSING_HOLD", "MODE_D_SENSE",
       "microwave-driven sensing hold (Ramsey/ODMR forecast window)",
       "dose complete AND IL-07 (<=12 mK) AND IL-08 (vibration settled) held",
       "hold window complete",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN", microwave="on", he_dose="on"),
       "T_probe under MW load (tD_hold forecast), vibration amplitude",
       "IL-07/IL-08; nv_eligibility_3d; Mode-D detection gates",
       "IL-07, IL-08, IL-01, FSM-IL-15",
       "as parent", "dose held", "collection path active (E11)",
       "ODMR drive ON (REDUCED_ORDER NV-region map)", "as parent",
       "nv_eligibility_3d.json; tD_hold timeline rows",
       "mode_sequence_3d tD_hold + sources_3d", "D_POST", _FAULT_REC),
    _s("MODE_D_SENSE.D_POST", "MODE_D_SENSE",
       "dose and drive off; sensing campaign closed",
       "hold complete or entry refused", "hand-off selected",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="OPEN"),
       "coverage decay", "species accounting", "none additional",
       "as parent", "dose off", "dark", "off", "as parent",
       "species accounting references", "surface_coverage evolve",
       "to MODE_C_RECOVERY (re-recovery) or MODE_A_BASELINE", _FAULT_REC),

    # ------------------------------ SAFE_RECOVERY -----------------------------
    _s("SAFE_RECOVERY", "", "automatic safe-state on any BLOCKED-class trip: "
       "all sources off, ports closed, switch CLOSED to rethermalize",
       "entered from any operational state on interlock trip",
       "T_probe at base and trip condition cleared",
       hw(vacuum_pumping="on", upstream_plant="on", dilution="on",
          sc_heat_switch="CLOSED"),
       "trip condition value, T_probe",
       "falsification-engine condition taxonomy",
       "all", "switch CLOSED rethermalization", "all feeds/doses closed",
       "dark; shutter CLOSED", "off", "full stack",
       "falsification_report_3d.json references", "thermal_3d_transient "
       "(zero-source decay)", "hand back to MODE_A_BASELINE.STABILIZE",
       _FAULT_LATCH),

    # ------------------------------ FAULT_LATCHED -----------------------------
    _s("FAULT_LATCHED", "", "latched fault on IMPOSSIBLE-class violation or "
       "unrecoverable trip; operator acknowledgment required",
       "IMPOSSIBLE-class interlock violation attempted, or SAFE_RECOVERY "
       "cannot clear", "operator acknowledgment",
       hw(vacuum_pumping="on", dilution="on", sc_heat_switch="CLOSED"),
       "latched cause id", "interlock table (IMPOSSIBLE class)",
       "all", "hold cold safe", "all closed", "dark", "off",
       "dilution held", "fault record in trace", "none",
       "acknowledge -> OFFLINE.MAINTENANCE", "terminal until acknowledged"),

    # -------------------------------- SHUTDOWN --------------------------------
    _s("SHUTDOWN", "", "controlled end-of-campaign warmup and vent prep",
       "operator request from MODE_A_BASELINE",
       "OFFLINE reached",
       hw(vacuum_pumping="on", dilution="on", sc_heat_switch="CLOSED"),
       "stage temperatures (rising)", "warmup schedule: NOT_IMPLEMENTED "
       "(no canonical warmup rates)", "FSM-IL-18",
       "controlled warmup (rates NOT_IMPLEMENTED)", "all feeds closed",
       "dark", "off", "staged warmup",
       "none", "none", "hold at safe intermediate temperature", _FAULT_REC),
    _s("SHUTDOWN.CONTROLLED_WARMUP", "SHUTDOWN",
       "staged warmup with all species lines closed (FSM-IL-18)",
       "SHUTDOWN entered", "ambient approached",
       hw(vacuum_pumping="on", sc_heat_switch="CLOSED"),
       "stage temperatures", "NOT_IMPLEMENTED warmup bounds", "FSM-IL-18",
       "warming", "closed", "dark", "off", "plant off, drifting warm",
       "none", "none", "hold", _FAULT_REC),
    _s("SHUTDOWN.VENT_PREP", "SHUTDOWN",
       "vent preparation at ambient",
       "ambient reached", "vented; OFFLINE",
       hw(vacuum_pumping="on", ivc_valve="open"),
       "chamber pressure", "none (pre-operational class)", "IL-12",
       "ambient", "controlled backfill (parameters NOT_IMPLEMENTED)",
       "dark", "off", "off", "none", "none", "n/a", _FAULT_LATCH),
]

_STATES = {s.name: s for s in STATES}
TOP_LEVEL = [s.name for s in STATES if s.parent == ""]


# --------------------------------------------------------------------------
# transitions: allowed (with guards + physical justification) and forbidden
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Guard:
    key: str          # context key (model quantity)
    op: str           # "<=", ">=", "==", "is_true", "is_false"
    value: object
    provenance: str

    def holds(self, ctx):
        v = ctx.get(self.key)
        if v is None:
            return False
        if self.op == "<=":
            return v <= self.value
        if self.op == ">=":
            return v >= self.value
        if self.op == "==":
            return v == self.value
        if self.op == "is_true":
            return bool(v)
        if self.op == "is_false":
            return not bool(v)
        raise ValueError(self.op)

    def row(self):
        return f"{self.key} {self.op} {self.value} [{self.provenance}]"


def _t(src, dst, justification, *guards, interlocks=()):
    return {"from": src, "to": dst, "justification": justification,
            "guards": list(guards), "interlock_refs": list(interlocks)}


TRANSITIONS = [
    _t("OFFLINE", "OFFLINE.MAINTENANCE", "maintenance access from the base state"),
    _t("OFFLINE.MAINTENANCE", "OFFLINE", "maintenance complete",
       Guard("ivc_valve_closed", "is_true", True, "IL-12"),
       interlocks=("IL-12",)),
    _t("OFFLINE", "INIT", "operator power-on"),
    _t("INIT", "PREP_VACUUM", "self-checks complete",
       Guard("interlock_chain_ok", "is_true", True, "INIT self-check")),
    _t("INIT", "OFFLINE", "failed self-check returns to base"),
    _t("PREP_VACUUM", "PREP_VACUUM.PUMPDOWN", "begin evacuation"),
    _t("PREP_VACUUM.PUMPDOWN", "PREP_VACUUM.BAKEOUT",
       "bakeout requires rough vacuum first (E01 procedure)"),
    _t("PREP_VACUUM.BAKEOUT", "PREP_VACUUM.POST_BAKEOUT_VERIFY",
       "bakeout schedule complete (E01)"),
    _t("PREP_VACUUM.POST_BAKEOUT_VERIFY", "PREP_VACUUM.BAKEOUT",
       "failed residual verification re-enters bakeout"),
    _t("PREP_VACUUM.POST_BAKEOUT_VERIFY", "COOLDOWN",
       "cooldown requires post-bakeout UHV (contamination control)",
       Guard("P_H2_Pa", "<=", 1.0e-12, "CHAMBER_STATE post_bakeout target"),
       interlocks=("IL-05", "IL-06")),
    _t("COOLDOWN", "COOLDOWN.PRECOOL_UPSTREAM", "staged chain begins upstream"),
    _t("COOLDOWN.PRECOOL_UPSTREAM", "COOLDOWN.DILUTION_CONDENSE",
       "dilution condensation requires the 4 K stage",
       Guard("stage_4K_ready", "is_true", True, "canonical stage chain")),
    _t("COOLDOWN.DILUTION_CONDENSE", "COOLDOWN.BASE_APPROACH",
       "base approach requires 100 mK",
       Guard("stage_100mK_ready", "is_true", True, "canonical stage chain")),
    _t("COOLDOWN.BASE_APPROACH", "MODE_A_BASELINE",
       "baseline requires the sample thermalized at base",
       Guard("T_probe_K", "<=", 0.010 * 1.05,
             "FridgeConfig T_fridge_K (5% band)")),
    _t("MODE_A_BASELINE", "MODE_A_BASELINE.STABILIZE", "settling dwell"),
    _t("MODE_A_BASELINE.STABILIZE", "MODE_A_BASELINE.READY_IDLE",
       "ready requires the settling dwell",
       Guard("dwell_s", ">=", 3.183098861837907,
             "vibration max_settling_time_s (canonical)")),
    _t("MODE_A_BASELINE.READY_IDLE", "MODE_B_PROCESS",
       "processing requires baseline readiness + MC protection available",
       Guard("T_probe_K", "<=", 0.010 * 1.05, "baseline hold"),
       Guard("he3_present", "is_false", True, "gate A4"),
       interlocks=("IL-09", "IL-10", "IL-14")),
    _t("MODE_B_PROCESS", "MODE_B_PROCESS.B_PRECONFIG", "configure before sources"),
    _t("MODE_B_PROCESS.B_PRECONFIG", "MODE_B_PROCESS.B_GROWTH_ACTIVE",
       "growth requires the Mode-B hardware configuration (switch OPEN, A1)",
       Guard("sc_switch_open", "is_true", True, "gate A1 / IL-03"),
       interlocks=("IL-03", "IL-01", "IL-13", "FSM-IL-15")),
    _t("MODE_B_PROCESS.B_GROWTH_ACTIVE", "MODE_B_PROCESS.B_SOURCE_OFF",
       "growth window complete; sources stopped before hand-off"),
    _t("MODE_B_PROCESS.B_SOURCE_OFF", "MODE_C_RECOVERY",
       "recovery requires all growth inputs off",
       Guard("laser_off", "is_true", True, "hand-off semantics"),
       Guard("c13_feed_off", "is_true", True, "hand-off semantics")),
    _t("MODE_C_RECOVERY", "MODE_C_RECOVERY.C_PURGE", "purge first"),
    _t("MODE_C_RECOVERY.C_PURGE", "MODE_C_RECOVERY.C_THERMAL_RECOVERY",
       "thermal recovery follows the purge floor",
       Guard("theta_c13_residual", "<=", 1.0e-6,
             "canonical purge forecast floor (worst case 2.1e-9)")),
    _t("MODE_C_RECOVERY.C_THERMAL_RECOVERY", "MODE_C_RECOVERY.C_READINESS_VERIFY",
       "readiness snapshot requires the hand-off threshold",
       Guard("T_probe_K", "<=", 0.050,
             "SolverConfig.mode_d_temp_threshold_K")),
    _t("MODE_C_RECOVERY.C_READINESS_VERIFY", "MODE_D_SENSE",
       "sensing requires a completed B->C cycle (IL-11) + clearances",
       Guard("mode_b_complete", "is_true", True, "IL-11"),
       Guard("rga_ch4_clear", "is_true", True, "IL-05"),
       Guard("rga_h2_clear", "is_true", True, "IL-06"),
       interlocks=("IL-11", "IL-05", "IL-06")),
    _t("MODE_D_SENSE", "MODE_D_SENSE.D_PRECONFIG", "isolation configuration"),
    _t("MODE_D_SENSE.D_PRECONFIG", "MODE_D_SENSE.D_HE_DOSE",
       "dosing requires isolation (switch OPEN, IL-04) and shutter CLOSED",
       Guard("sc_switch_open", "is_true", True, "IL-04"),
       Guard("sense_shutter_closed", "is_true", True, "gate A3 / FSM-IL-16"),
       interlocks=("IL-04", "FSM-IL-16")),
    _t("MODE_D_SENSE.D_HE_DOSE", "MODE_D_SENSE.D_SENSING_HOLD",
       "the sensing hold requires the Ramsey bounds",
       Guard("T_probe_K", "<=", 0.012, "IL-07 (12 mK sensing bound)"),
       Guard("vib_amplitude_m", "<=", 1.0e-10,
             "IL-08 / mode_d_amp_threshold_m"),
       interlocks=("IL-07", "IL-08")),
    _t("MODE_D_SENSE.D_HE_DOSE", "MODE_D_SENSE.D_POST",
       "dose closed without sensing (blocked or campaign end)"),
    _t("MODE_D_SENSE.D_SENSING_HOLD", "MODE_D_SENSE.D_POST",
       "hold window complete"),
    _t("MODE_D_SENSE.D_POST", "MODE_C_RECOVERY",
       "re-recovery after sensing (He removal / rethermalization)"),
    _t("MODE_D_SENSE.D_POST", "MODE_A_BASELINE",
       "return to baseline after the campaign",
       Guard("he_dose_off", "is_true", True, "species policy")),
    _t("MODE_A_BASELINE", "SHUTDOWN", "operator end-of-campaign"),
    _t("SHUTDOWN", "SHUTDOWN.CONTROLLED_WARMUP", "staged warmup",
       Guard("all_gas_lines_closed", "is_true", True, "FSM-IL-18"),
       interlocks=("FSM-IL-18",)),
    _t("SHUTDOWN.CONTROLLED_WARMUP", "SHUTDOWN.VENT_PREP",
       "vent prep at ambient"),
    _t("SHUTDOWN.VENT_PREP", "OFFLINE", "campaign closed"),
    _t("SAFE_RECOVERY", "MODE_A_BASELINE.STABILIZE",
       "hand back to baseline after rethermalization and trip clearance",
       Guard("T_probe_K", "<=", 0.010 * 1.05, "base rethermalized"),
       Guard("trip_cleared", "is_true", True, "trip condition")),
    _t("FAULT_LATCHED", "OFFLINE.MAINTENANCE",
       "operator acknowledgment routes to maintenance",
       Guard("operator_ack", "is_true", True, "latched-fault policy")),
]

#: explicitly forbidden transitions (never allowed; attempting one raises)
FORBIDDEN = [
    ("MODE_B_PROCESS", "MODE_D_SENSE", "IL-01/IL-13",
     "growth and sensing are mutually exclusive; Mode C is mandatory"),
    ("MODE_D_SENSE", "MODE_B_PROCESS", "IL-01/IL-13",
     "sensing to growth must pass isolation/purge (Mode C)"),
    ("MODE_B_PROCESS.B_GROWTH_ACTIVE", "MODE_D_SENSE.D_SENSING_HOLD",
     "IL-01", "direct growth->sensing is the canonical impossible pair"),
    ("MODE_A_BASELINE", "MODE_D_SENSE", "IL-11",
     "Mode D requires a completed Mode B->C cycle (purge required)"),
    ("COOLDOWN", "MODE_B_PROCESS", "FSM-IL-17",
     "processing requires the established 10 mK baseline"),
    ("PREP_VACUUM", "COOLDOWN.BASE_APPROACH", "stage chain",
     "cooldown must pass the staged intercept chain"),
    ("SHUTDOWN", "MODE_B_PROCESS", "FSM-IL-18",
     "no processing during warmup"),
    ("SHUTDOWN", "MODE_D_SENSE", "FSM-IL-18",
     "no sensing during warmup"),
]


class TransitionRefused(ValueError):
    def __init__(self, src, dst, reason, ref=""):
        self.src, self.dst, self.reason, self.ref = src, dst, reason, ref
        super().__init__(f"{src} -> {dst} refused: {reason}"
                         f"{f' [{ref}]' if ref else ''}")


class MachineFSM:
    """Deterministic hierarchical FSM engine over the state/transition data."""

    def __init__(self, start: str = "OFFLINE"):
        if start not in _STATES:
            raise ValueError(f"unknown start state {start!r}")
        self.state = start
        self._allowed = {(t["from"], t["to"]): t for t in TRANSITIONS}
        self._forbidden = {(a, b): (il, why) for a, b, il, why in FORBIDDEN}

    @staticmethod
    def spec(name: str) -> StateSpec:
        return _STATES[name]

    def allowed_from(self, src: str):
        return sorted(dst for (a, dst) in self._allowed if a == src)

    def request(self, dst: str, ctx: dict | None = None) -> dict:
        """Attempt a transition; returns the evaluation record or raises
        TransitionRefused. Deterministic; never mutates on refusal."""
        ctx = ctx or {}
        src = self.state
        if dst not in _STATES:
            raise TransitionRefused(src, dst, "unknown target state")
        if (src, dst) in self._forbidden:
            il, why = self._forbidden[(src, dst)]
            raise TransitionRefused(src, dst, f"forbidden: {why}", il)
        t = self._allowed.get((src, dst))
        if t is None:
            raise TransitionRefused(src, dst, "no declared transition path")
        failed = [g for g in t["guards"] if not g.holds(ctx)]
        if failed:
            _fref = ",".join(sorted({g.provenance.split()[0].rstrip("/,")
                                     for g in failed
                                     if g.provenance.split()[0].startswith(
                                         ("IL-", "FSM-IL-"))}))
            raise TransitionRefused(
                src, dst,
                "guard not satisfied: " + "; ".join(g.row() for g in failed),
                _fref or ",".join(t["interlock_refs"]))
        hwv = hardware_interlock_violations(_STATES[dst].hardware, ctx)
        if hwv:
            kinds = {_IL[i].kind for i in hwv}
            raise TransitionRefused(
                src, dst, "hardware interlock violation "
                + ("(IMPOSSIBLE class)" if "IMPOSSIBLE" in kinds else
                   "(BLOCKED class)"), ",".join(hwv))
        self.state = dst
        return {"from": src, "to": dst, "result": "TRANSITIONED",
                "guards": "; ".join(g.row() for g in t["guards"]) or "none",
                "interlock_refs": ",".join(t["interlock_refs"]) or "",
                "justification": t["justification"], "label": LABEL}

    def trip_safe_recovery(self, cause: str) -> dict:
        """Any operational state may trip to SAFE_RECOVERY (BLOCKED class)."""
        src = self.state
        self.state = "SAFE_RECOVERY"
        return {"from": src, "to": "SAFE_RECOVERY", "result": "TRIPPED",
                "guards": "n/a (safety trip)", "interlock_refs": cause,
                "justification": "BLOCKED-class trip -> automatic safe state",
                "label": LABEL}

    def latch_fault(self, cause: str) -> dict:
        src = self.state
        self.state = "FAULT_LATCHED"
        return {"from": src, "to": "FAULT_LATCHED", "result": "LATCHED",
                "guards": "n/a (IMPOSSIBLE-class violation)",
                "interlock_refs": cause,
                "justification": "IMPOSSIBLE-class violation latches the "
                                 "machine pending operator acknowledgment",
                "label": LABEL}


# --------------------------------------------------------------------------
# table writers (deterministic rows for the canonical outputs)
# --------------------------------------------------------------------------
def state_rows():
    return [s.row() for s in STATES]


def transition_rows():
    rows = []
    for t in TRANSITIONS:
        rows.append({"from": t["from"], "to": t["to"], "kind": "ALLOWED",
                     "guards": "; ".join(g.row() for g in t["guards"]) or "none",
                     "interlock_refs": ",".join(t["interlock_refs"]) or "",
                     "justification": t["justification"], "label": LABEL})
    for a, b, il, why in FORBIDDEN:
        rows.append({"from": a, "to": b, "kind": "FORBIDDEN", "guards": "n/a",
                     "interlock_refs": il, "justification": why,
                     "label": LABEL})
    return rows


def interlock_rows():
    return [i.row() for i in INTERLOCKS]


def mermaid_diagram() -> str:
    """Deterministic Mermaid state diagram of the top level + substates."""
    lines = ["stateDiagram-v2",
             "    %% QTA machine FSM (MODEL-ONLY, forecast-only, zero PASS)"]
    for top in TOP_LEVEL:
        subs = [s.name for s in STATES if s.parent == top]
        if subs:
            lines.append(f"    state {top} {{")
            for s in subs:
                lines.append(f"        {s.split('.')[-1]}")
            lines.append("    }")
    for t in TRANSITIONS:
        a = t["from"].replace(".", "_")
        b = t["to"].replace(".", "_")
        lines.append(f"    {a} --> {b}")
    lines.append("    %% forbidden (enforced by interlocks, never drawn as "
                 "paths): " + "; ".join(f"{a} x> {b} [{il}]"
                                        for a, b, il, _ in FORBIDDEN))
    return "\n".join(lines) + "\n"

# --------------------------------------------------------------------------
# nominal lifecycle trace: the FSM as the operational controller, evaluated
# against REAL model quantities from the coupled run (no extra solves).
# --------------------------------------------------------------------------
def run_nominal_lifecycle(cfg, seq, species_rows, vib_metrics) -> tuple:
    """Walk the nominal campaign OFFLINE -> ... -> MODE_D -> baseline.

    Guards are evaluated on the model's own quantities: the 3D sequence
    probe temperatures, the species-accounting purge residual, the canonical
    vibration metrics, and the canonical chamber targets. Procedural
    milestones with no model dynamics (pumpdown floor, stage arrivals,
    operator actions) are declared as such in the provenance column. RGA
    clearances are FORECAST-basis (theta floors); measured clearance is NOT
    AVAILABLE (gate E04). Deterministic.
    """
    Tbase = float(cfg.fridge.T_fridge_K)
    probe_C_end = float(seq.tC.probe_timeseries_K()[-1])
    resC13 = float([r for r in species_rows if r["phase"] == "MODE_C"
                    and r["species"] == "C13_CH4"][0]["coverage_end"])
    amp = float(vib_metrics["nv_output_amplitude_m"])
    dwell = float(vib_metrics["max_settling_time_s"])

    base_ctx = {
        "interlock_chain_ok": True, "ivc_valve_closed": True,
        "P_H2_Pa": 1.0e-12,          # CHAMBER_STATE post_bakeout target
        "stage_4K_ready": True, "stage_100mK_ready": True,
        "T_probe_K": Tbase, "dwell_s": dwell,
        "he3_present": False, "helium_film_present": False,
        "sc_switch_open": True, "laser_off": True, "c13_feed_off": True,
        "theta_c13_residual": resC13, "mode_b_complete": True,
        "rga_ch4_clear": True, "rga_h2_clear": True,
        "sense_shutter_closed": True, "vib_amplitude_m": amp,
        "he_dose_off": True, "all_gas_lines_closed": True,
        "trip_cleared": True, "operator_ack": True,
        "sensing_phase": False, "growth_active": False,
        "operational_mode": False, "warmup_phase": False,
    }
    prov = {
        "P_H2_Pa": "CHAMBER_STATE post_bakeout (canonical)",
        "T_probe_K": "FridgeConfig / 3D sequence probe",
        "theta_c13_residual": "species_accounting purge forecast",
        "vib_amplitude_m": "vibration_transfer (canonical chain)",
        "dwell_s": "max_settling_time_s (canonical)",
        "rga_ch4_clear": "FORECAST-basis clearance (theta floors); measured "
                         "RGA clearance NOT AVAILABLE (E04)",
        "rga_h2_clear": "FORECAST-basis clearance; measured NOT AVAILABLE",
        "stage_4K_ready": "procedural milestone (no cooldown dynamics model)",
        "stage_100mK_ready": "procedural milestone",
        "interlock_chain_ok": "procedural self-check",
    }

    fsm = MachineFSM("OFFLINE")
    rows = []
    step = 0

    def go(dst, **ctx_over):
        nonlocal step
        step += 1
        ctx = dict(base_ctx)
        ctx.update(ctx_over)
        try:
            rec = fsm.request(dst, ctx)
        except TransitionRefused as e:
            rec = {"from": e.src, "to": e.dst, "result": "REFUSED",
                   "guards": e.reason, "interlock_refs": e.ref,
                   "justification": "guard/interlock enforcement (honest "
                                    "refusal; falsification-consistent)",
                   "label": LABEL}
        rec["step"] = str(step)
        keys = sorted(set(ctx_over) | ({"T_probe_K", "vib_amplitude_m"}
                                       & set(ctx)))
        rec["context_provenance"] = "; ".join(
            f"{k}={ctx[k]}" + (f" [{prov[k]}]" if k in prov else "")
            for k in keys) or "base context"
        rows.append(rec)
        return rec

    # nominal campaign
    go("INIT"); go("PREP_VACUUM"); go("PREP_VACUUM.PUMPDOWN")
    go("PREP_VACUUM.BAKEOUT"); go("PREP_VACUUM.POST_BAKEOUT_VERIFY")
    go("COOLDOWN"); go("COOLDOWN.PRECOOL_UPSTREAM")
    go("COOLDOWN.DILUTION_CONDENSE"); go("COOLDOWN.BASE_APPROACH")
    go("MODE_A_BASELINE"); go("MODE_A_BASELINE.STABILIZE")
    go("MODE_A_BASELINE.READY_IDLE")
    go("MODE_B_PROCESS", operational_mode=True)
    go("MODE_B_PROCESS.B_PRECONFIG", operational_mode=True)
    go("MODE_B_PROCESS.B_GROWTH_ACTIVE", operational_mode=True,
       growth_active=True)
    go("MODE_B_PROCESS.B_SOURCE_OFF", operational_mode=True)
    go("MODE_C_RECOVERY", operational_mode=True)
    go("MODE_C_RECOVERY.C_PURGE", operational_mode=True)
    go("MODE_C_RECOVERY.C_THERMAL_RECOVERY", operational_mode=True,
       T_probe_K=probe_C_end)
    go("MODE_C_RECOVERY.C_READINESS_VERIFY", operational_mode=True,
       T_probe_K=probe_C_end)
    go("MODE_D_SENSE", operational_mode=True, T_probe_K=probe_C_end)
    go("MODE_D_SENSE.D_PRECONFIG", operational_mode=True,
       T_probe_K=probe_C_end)
    go("MODE_D_SENSE.D_HE_DOSE", operational_mode=True,
       T_probe_K=probe_C_end)
    hold = go("MODE_D_SENSE.D_SENSING_HOLD", operational_mode=True,
              sensing_phase=True, T_probe_K=probe_C_end)
    go("MODE_D_SENSE.D_POST", operational_mode=True, T_probe_K=probe_C_end)
    go("MODE_A_BASELINE", T_probe_K=Tbase)

    status = ("NOMINAL_CAMPAIGN_COMPLETE" if hold["result"] == "TRANSITIONED"
              else f"SENSING_HOLD_ENTRY_REFUSED [{hold['interlock_refs']}]")
    summary = {
        "final_state": fsm.state,
        "steps": step,
        "n_refused": sum(1 for r in rows if r["result"] == "REFUSED"),
        "campaign_status": status,
        "sensing_refusal_consistency": (
            "the D_SENSING_HOLD refusal (vibration amplitude above the "
            "canonical Mode-D threshold, IL-08) is the same model condition "
            "that blocks the NV eligibility forecast and keeps the canonical "
            "vibration gate CONDITIONAL -- an honest forecast result, not an "
            "error") if hold["result"] == "REFUSED" else "n/a",
        "n_states": len(STATES),
        "n_top_level": len(TOP_LEVEL),
        "n_transitions_allowed": len(TRANSITIONS),
        "n_transitions_forbidden": len(FORBIDDEN),
        "n_interlocks": len(INTERLOCKS),
        "measured_in_this_system": False,
        "can_PASS_now": "NO",
        "claim_boundary": "operational control logic for a THEORETICAL "
                          "machine and its simulation framework; forecast-"
                          "only; zero PASS; no hardware validation",
        "label": LABEL,
    }
    return rows, summary
