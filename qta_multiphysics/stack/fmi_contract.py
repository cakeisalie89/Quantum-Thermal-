"""FMI export — DEFERRED, with the interface contract written down.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

The stack declares FMI "later", and this module is what "later" should mean
in a governed project: the interface is specified now, the blockers are named
now, and nothing is shipped that could be mistaken for a working FMU.

Concretely, this module emits an FMI 3.0 **interface contract** — an XML
document in ``modelDescription`` shape describing the variables, causalities,
units, and co-simulation semantics a QTA thermal-ROM FMU would have. It is
written as ``modelDescription.contract.xml``, never as ``modelDescription.xml``
inside a zip, because:

* no binary is built, so nothing can be imported by an FMI master;
* no FMI compliance is claimed or checked;
* the prerequisites below are unmet, and the contract records which.

Prerequisites, all currently open, are tracked in
:data:`EXPORT_PREREQUISITES`. The load-bearing one is state serialisation:
FMI co-simulation masters may roll a step back, which requires
``fmi3GetFMUState`` / ``fmi3SetFMUState``, and the transient solver has no
serialisable state today. The second is mode semantics: Mode B/C/D switching
is discrete and interlocked, and a communication step that straddles a mode
boundary would silently violate an interlock the FSM enforces. Neither is a
packaging detail; both are design work.
"""
from __future__ import annotations

import hashlib

from ..config import default_config
from . import AUTOMATIC_GATE_EFFECT, LABEL
from .workspace import (StrPath, guard_output_dir,
                        write_json_deterministic, write_text_deterministic)

ADOPTION_STATUS = "DEFERRED"
FMI_VERSION = "3.0"
MODEL_IDENTIFIER = "qta_thermal_rom"

UNITS = {
    "K": {"K": 1},
    "W": {"kg": 1, "m": 2, "s": -3},
    "m": {"m": 1},
    "s": {"s": 1},
}

# (name, causality, unit, description). Value references are assigned
# deterministically from this order.
VARIABLES = (
    ("laser.average_power_W", "input", "W",
     "Mode-B average laser power delivered to the sample (forecast input)"),
    ("laser.spot_radius_m", "parameter", "m",
     "1/e^2 beam spot radius (DESIGN provenance)"),
    ("laser.absorbed_fraction", "parameter", None,
     "absorbed fraction of incident power (ASSUMED provenance)"),
    ("fridge.T_fridge_K", "parameter", "K",
     "fridge base temperature at the Kapitza-radiative sink"),
    ("mode_index", "input", None,
     "operating mode as an integer (A=0, B=1, C=2, D=3); a communication "
     "step may not straddle a change of this variable"),
    ("nv_probe_T_K", "output", "K",
     "forecast NV-probe temperature (model-only; never a measurement)"),
    ("peak_T_K", "output", "K",
     "forecast peak temperature in the resolved domain (model-only)"),
    ("energy_residual_rel", "output", None,
     "relative energy-conservation residual of the step"),
)

EXPORT_PREREQUISITES = (
    {"id": "FMI-P1", "status": "OPEN",
     "requirement": "serialisable solver state",
     "detail": "fmi3GetFMUState / fmi3SetFMUState require the transient "
               "solver to save and restore its full state so a master can "
               "roll a step back; the current method-of-lines integrator "
               "exposes no such state"},
    {"id": "FMI-P2", "status": "OPEN",
     "requirement": "mode-boundary step semantics",
     "detail": "Mode B/C/D transitions are discrete and interlocked "
               "(machine_fsm.py). A communication step crossing a mode "
               "boundary would bypass an interlock, so the FMU must either "
               "reject such steps or expose the transition as an event"},
    {"id": "FMI-P3", "status": "OPEN",
     "requirement": "step-size independence evidence",
     "detail": "co-simulation results must be shown insensitive to the "
               "communication step size over the declared range, with the "
               "same convergence evidence the project applies to its meshes"},
    {"id": "FMI-P4", "status": "OPEN",
     "requirement": "claim-boundary survival",
     "detail": "FMI has no field for evidence class. Every exported output "
               "is forecast-only and must carry that in the FMU description "
               "and in a companion record, or the boundary is lost the "
               "moment the FMU is imported elsewhere"},
    {"id": "FMI-P5", "status": "OPEN",
     "requirement": "unit round-trip verification",
     "detail": "declared FMI units must round-trip against "
               "qta_multiphysics.units for every exported variable"},
)


def value_references() -> dict:
    """Stable value reference per variable (1-based, declaration order)."""
    return {name: i for i, (name, *_rest) in enumerate(VARIABLES, start=1)}


def instantiation_token() -> str:
    """Deterministic token derived from the interface itself.

    Not a random GUID: the token must change when — and only when — the
    interface changes, so a consumer can tell two contracts apart by content.
    """
    payload = "|".join(f"{n}:{c}:{u}" for n, c, u, _d in VARIABLES)
    digest = hashlib.sha256(
        f"{FMI_VERSION}|{MODEL_IDENTIFIER}|{payload}".encode()).hexdigest()
    return (f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-"
            f"{digest[16:20]}-{digest[20:32]}")


def _unit_definitions() -> list:
    lines = ["  <UnitDefinitions>"]
    for unit in sorted(UNITS):
        attrs = " ".join(f'{k}="{v}"' for k, v in sorted(UNITS[unit].items()))
        lines.append(f'    <Unit name="{unit}">')
        lines.append(f"      <BaseUnit {attrs}/>")
        lines.append("    </Unit>")
    lines.append("  </UnitDefinitions>")
    return lines


def contract_xml() -> str:
    """FMI 3.0 interface contract in ``modelDescription`` shape (not an FMU)."""
    cfg = default_config()
    starts = {
        "laser.average_power_W": float(cfg.laser.average_power_W),
        "laser.spot_radius_m": float(cfg.laser.spot_radius_m),
        "laser.absorbed_fraction": float(cfg.laser.absorbed_fraction),
        "fridge.T_fridge_K": float(cfg.fridge.T_fridge_K),
        "mode_index": 1,
    }
    vrefs = value_references()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!-- FMI INTERFACE CONTRACT — NOT AN FMU. No binary is built, no "
        "FMI compliance is claimed, and this file is deliberately not named "
        "modelDescription.xml so it cannot be zipped into a working FMU by "
        f"accident. {LABEL}. automatic_gate_effect="
        f"{AUTOMATIC_GATE_EFFECT}. -->",
        f'<fmiModelDescription fmiVersion="{FMI_VERSION}" '
        f'modelName="QTA thermal ROM (forecast-only)" '
        f'instantiationToken="{{{instantiation_token()}}}" '
        f'variableNamingConvention="structured" '
        f'generationTool="qta_multiphysics.stack.fmi_contract (contract '
        f'only)" description="{LABEL}; every output is a forecast of THIS '
        'model under THESE assumptions and is not a measurement">',
        f'  <CoSimulation modelIdentifier="{MODEL_IDENTIFIER}" '
        'canHandleVariableCommunicationStepSize="false" '
        'canBeInstantiatedOnlyOncePerProcess="true" '
        'canGetAndSetFMUState="false" canSerializeFMUState="false" '
        'providesDirectionalDerivatives="false"/>',
    ]
    lines += _unit_definitions()
    lines.append("  <ModelVariables>")
    for name, causality, unit, desc in VARIABLES:
        variability = "fixed" if causality == "parameter" else "continuous"
        attrs = [f'name="{name}"', f'valueReference="{vrefs[name]}"',
                 f'causality="{causality}"', f'variability="{variability}"']
        if unit:
            attrs.append(f'unit="{unit}"')
        if name in starts:
            attrs.append(f'start="{starts[name]}"')
        attrs.append(f'description="{desc}"')
        lines.append(f"    <Float64 {' '.join(attrs)}/>")
    lines.append("  </ModelVariables>")
    lines.append("  <ModelStructure>")
    for name, causality, _u, _d in VARIABLES:
        if causality == "output":
            lines.append(f'    <Output valueReference="{vrefs[name]}"/>')
    lines.append("  </ModelStructure>")
    lines.append("</fmiModelDescription>")
    return "\n".join(lines) + "\n"


def readiness_report() -> dict:
    """Deferred-adoption status with the open prerequisites enumerated."""
    open_items = [p for p in EXPORT_PREREQUISITES if p["status"] == "OPEN"]
    return {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.fmi_contract",
        "component": "FMI 3.0 co-simulation export",
        "adoption_status": ADOPTION_STATUS,
        "fmi_version": FMI_VERSION,
        "model_identifier": MODEL_IDENTIFIER,
        "instantiation_token": instantiation_token(),
        "artifact_produced": "modelDescription.contract.xml "
                             "(interface contract only)",
        "fmu_produced": False,
        "compliance_claimed": False,
        "prerequisites": list(EXPORT_PREREQUISITES),
        "open_prerequisites": [p["id"] for p in open_items],
        "ready_to_export": not open_items,
        "n_variables": len(VARIABLES),
        "value_references": value_references(),
        "note": "the contract fixes the interface so later work is "
                "implementation, not redesign; no FMU may be built until "
                "every prerequisite is CLOSED",
    }


def write_contract(out_dir: StrPath) -> dict:
    """Write the contract XML and its readiness report into the workspace."""
    out = guard_output_dir(out_dir)
    digest = write_text_deterministic(out / "modelDescription.contract.xml",
                                      contract_xml())
    report = readiness_report()
    report["contract_sha256"] = digest
    write_json_deterministic(out / "fmi_readiness.json", report)
    return report
