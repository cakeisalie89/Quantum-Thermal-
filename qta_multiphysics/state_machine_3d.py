"""Mode-driven device state machine for the coupled 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Maps the canonical mode (A/B/C/D) to the device states that gate every
coupling channel: radiation shutter, cryo-baffle, laser, microwave drive,
superconducting heat switch, and the active-species set. The mapping encodes
the repository's existing DESIGN_SPECIFIED logic (gates A1/A3, the canonical
mode map, and the radiation chain's parameterized shutter/baffle states);
it introduces no new physics and no new numbers. Deterministic.

State semantics (DESIGN_SPECIFIED):
* SC heat switch: CLOSED = high-conductance thermalization path (baseline
  cooldown, Mode C recovery); OPEN = low-leakage isolation (Mode B protects
  the mixing chamber per gate A1; Mode D sensing isolation).
* Radiation shutter: OPEN only in Mode B (laser/process access); CLOSED in
  A/C/D (shielded sensing/recovery, gate A3).
* Cryo-baffle: engaged in all modes.
* Laser: Mode B only. Microwave/ESR drive: Mode D only.
"""
from __future__ import annotations

from dataclasses import dataclass

from .mode_sequence_3d import MODES, CANONICAL_ACTIVE, validate_live_species

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


@dataclass(frozen=True)
class DeviceState3D:
    mode: str
    shutter_state: str          # "open" | "closed"
    baffle_state: str           # "engaged"
    laser_active: bool
    microwave_active: bool
    heat_switch_state: str      # "OPEN" | "CLOSED"
    active_species: frozenset

    def as_row(self) -> dict:
        return {"mode": self.mode, "shutter_state": self.shutter_state,
                "baffle_state": self.baffle_state,
                "laser_active": str(self.laser_active).lower(),
                "microwave_active": str(self.microwave_active).lower(),
                "heat_switch_state": self.heat_switch_state,
                "active_species": ";".join(sorted(self.active_species)),
                "label": LABEL}


_STATES = {
    "MODE_A": dict(shutter_state="closed", laser_active=False,
                   microwave_active=False, heat_switch_state="CLOSED"),
    "MODE_B": dict(shutter_state="open", laser_active=True,
                   microwave_active=False, heat_switch_state="OPEN"),
    "MODE_C": dict(shutter_state="closed", laser_active=False,
                   microwave_active=False, heat_switch_state="CLOSED"),
    "MODE_D": dict(shutter_state="closed", laser_active=False,
                   microwave_active=True, heat_switch_state="OPEN"),
}


def device_state(mode: str) -> DeviceState3D:
    """Device states for a canonical mode (raises on unknown modes)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; canonical modes are {MODES}")
    live = validate_live_species(mode, CANONICAL_ACTIVE[mode])
    return DeviceState3D(mode=mode, baffle_state="engaged",
                         active_species=live, **_STATES[mode])


def all_device_states():
    return [device_state(m) for m in MODES]
