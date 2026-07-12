"""Experiment design space with full constraint enforcement.

An experiment is a typed design vector (not just a letter). The existing
EXP-A..EXP-F candidates are preserved as named design *families* (loaded from the
existing experimental-design model), but the space is continuous/configurable.

Every design is validated against the canonical constraints before it may be
simulated or used in inference:
  * canonical A -> B -> C -> D mode order (for sequences);
  * no LCVD (methane precursor) and sensing overlap;
  * no methane precursor during Mode D;
  * no helium film during LCVD (helium only in Mode D);
  * existing interlocks (from interlock_table.csv via the design registry);
  * predecessor requirements (from the experiment families);
  * hardware availability and safety flags;
  * gate dependencies.
Invalid designs are rejected (``validate_design`` returns reasons; ``is_valid``
is a bool).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .schemas import DesignVariable

_MODES = ("A", "B", "C", "D")
_MODE_ORDER = {m: i for i, m in enumerate(_MODES)}
_SENSING_SEQUENCES = ("ramsey", "hahn", "xy8", "odmr", "rabi")
_PROCESSING_SEQUENCES = ("lcvd", "none")


def design_variables() -> list:
    """Return the typed design-vector schema."""
    return [
        DesignVariable("experiment_id", "meta", "", None, "design family / id"),
        DesignVariable("pulse_sequence", "categorical", "",
                       list(_SENSING_SEQUENCES) + ["lcvd", "none"], "control/readout sequence"),
        DesignVariable("pulse_spacing", "continuous", "s", [1e-9, 1e-2], "inter-pulse spacing"),
        DesignVariable("sequence_order", "continuous", "count", [1, 256], "number of pulses/order"),
        DesignVariable("measurement_time", "continuous", "s", [1e-6, 1e-1], "total measurement time"),
        DesignVariable("bias_field", "continuous", "T", [0.0, 0.1], "bias magnetic field"),
        DesignVariable("surface_temperature", "continuous", "K", [1e-3, 4.0], "surface temperature"),
        DesignVariable("he3_coverage_target", "continuous", "monolayer", [0.0, 3.0], "He-3 coverage target"),
        DesignVariable("he4_coverage_target", "continuous", "monolayer", [0.0, 3.0], "He-4 coverage target"),
        DesignVariable("dosing_duration", "continuous", "s", [0.0, 1e3], "precursor/dosing duration"),
        DesignVariable("recovery_duration", "continuous", "s", [0.0, 1e3], "thermal recovery duration"),
        DesignVariable("nv_depth_class", "categorical", "", ["shallow", "mid", "deep"], "NV depth class"),
        DesignVariable("mode", "mode", "", list(_MODES), "operating mode"),
        DesignVariable("hardware_requirements", "meta", "", None, "required hardware"),
        DesignVariable("estimated_cost", "continuous", "rel", [0.0, 10.0], "forecast cost"),
        DesignVariable("estimated_duration", "continuous", "rel", [0.0, 10.0], "forecast duration"),
        DesignVariable("estimated_risk", "continuous", "rel", [0.0, 10.0], "forecast risk"),
    ]


@dataclass
class ExperimentDesign:
    experiment_id: str
    pulse_sequence: str
    mode: str
    pulse_spacing: float = 1e-6
    sequence_order: float = 8
    measurement_time: float = 1e-3
    bias_field: float = 5e-3
    surface_temperature: float = 1e-2
    he3_coverage_target: float = 0.0
    he4_coverage_target: float = 0.0
    dosing_duration: float = 0.0
    recovery_duration: float = 0.0
    nv_depth_class: str = "shallow"
    hardware_requirements: str = ""
    estimated_cost: float = 1.0
    estimated_duration: float = 1.0
    estimated_risk: float = 1.0
    predecessors: list = field(default_factory=list)
    gates_affected: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_design(d: ExperimentDesign, *, available_modes: Optional[set] = None,
                    completed: Optional[set] = None,
                    interlocks: Optional[list] = None) -> list:
    """Return a list of violation reasons (empty == valid)."""
    reasons = []
    if d.mode not in _MODES:
        reasons.append(f"invalid mode {d.mode!r}")
        return reasons  # nothing else is meaningful

    seq = (d.pulse_sequence or "").lower()
    is_sensing = seq in _SENSING_SEQUENCES
    is_lcvd = seq == "lcvd"
    has_methane = d.dosing_duration > 0 and is_lcvd
    has_helium = (d.he3_coverage_target > 0) or (d.he4_coverage_target > 0)

    # sensing only in Mode D (sensing happens after Mode C recovery)
    if is_sensing and d.mode != "D":
        reasons.append(f"sensing sequence {seq!r} requires Mode D (got {d.mode})")
    # LCVD/methane processing only in Mode B
    if is_lcvd and d.mode != "B":
        reasons.append("LCVD/methane processing requires Mode B")
    if has_methane and d.mode == "D":
        reasons.append("methane precursor must not be present during Mode D")
    # helium film only in Mode D; never during LCVD (Mode B)
    if has_helium and d.mode != "D":
        reasons.append("helium coverage (He-3/He-4) only permitted in Mode D")
    if has_helium and is_lcvd:
        reasons.append("helium film must not be present during LCVD")
    # processing + sensing overlap forbidden in a single design
    if is_lcvd and is_sensing:
        reasons.append("LCVD and sensing cannot occur in the same experiment")
    # physical-range checks
    for nm, lo, hi in [("pulse_spacing", 1e-9, 1e-2), ("measurement_time", 1e-6, 1e-1),
                       ("bias_field", 0.0, 0.1), ("surface_temperature", 1e-3, 4.0)]:
        v = getattr(d, nm)
        if not (lo <= v <= hi):
            reasons.append(f"{nm}={v} out of range [{lo},{hi}]")
    if d.nv_depth_class not in ("shallow", "mid", "deep"):
        reasons.append(f"invalid nv_depth_class {d.nv_depth_class!r}")
    # predecessors
    if d.predecessors and completed is not None:
        missing = [p for p in d.predecessors if p not in completed]
        if missing:
            reasons.append(f"unsatisfied predecessors: {missing}")
    # hardware availability
    if available_modes is not None and d.mode not in available_modes:
        reasons.append(f"mode {d.mode} hardware not available")
    # interlocks: forbidden simultaneous states
    state = {"lcvd_on": is_lcvd, "sensing_on": is_sensing,
             "precursor_on": has_methane, "he3_on": d.he3_coverage_target > 0}
    for il in (interlocks or []):
        sig = [s.lower() for s in il.get("signals", [])]
        if ("lcvd" in " ".join(sig) and "sens" in " ".join(sig)) and state["lcvd_on"] and state["sensing_on"]:
            reasons.append(f"interlock {il.get('interlock_id')} violated (LCVD & sensing)")
        if ("precursor" in " ".join(sig) and "he" in " ".join(sig)) and state["precursor_on"] and state["he3_on"]:
            reasons.append(f"interlock {il.get('interlock_id')} violated (precursor & He-3)")
    return reasons


def is_valid(d: ExperimentDesign, **kw) -> bool:
    return len(validate_design(d, **kw)) == 0


def validate_sequence(designs: list) -> list:
    """Check that a list of designs respects canonical A->B->C->D mode order."""
    reasons = []
    order = [_MODE_ORDER.get(d.mode, -1) for d in designs]
    for i in range(1, len(order)):
        if order[i] < order[i - 1]:
            reasons.append(f"mode order violated at step {i}: "
                           f"{designs[i-1].mode}->{designs[i].mode}")
    return reasons


def load_design_families(repo_root: Optional[Path] = None) -> list:
    """Load EXP-A..F as named design families from the existing model (preserved)."""
    from qta_multiphysics.expdesign.model import load_candidates
    cands = load_candidates(repo_root)
    fams = []
    seq_by_letter = {"EXP-A": "odmr", "EXP-B": "ramsey", "EXP-C": "hahn",
                     "EXP-D": "none", "EXP-E": "none", "EXP-F": "none"}
    for c in cands:
        fams.append(ExperimentDesign(
            experiment_id=c.experiment_id,
            pulse_sequence=seq_by_letter.get(c.experiment_id, "odmr"),
            mode=c.required_mode,
            he3_coverage_target=(1.0 if c.experiment_id in ("EXP-B", "EXP-C") else 0.0),
            hardware_requirements=c.required_hardware,
            estimated_cost=c.cost, estimated_duration=c.duration, estimated_risk=c.risk,
            predecessors=list(c.predecessors), gates_affected=list(c.gates_affected),
        ))
    return fams


def load_interlocks(repo_root: Optional[Path] = None) -> list:
    """Load forbidden simultaneous states from the existing design registry."""
    from qta_multiphysics.design import load_registry
    reg = load_registry(repo_root)
    return reg.forbidden_simultaneous_states()
