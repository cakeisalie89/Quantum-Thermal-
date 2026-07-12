"""Superconducting heat-switch path as a state-dependent lumped conductance.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

REDUCED_ORDER stage-level channel: the SC switch connects the mixing-chamber /
base stage to the 4 K stage; it is NOT resolved inside the 3D sample field.
Conductances are the repository's existing DESIGN_SPECIFIED values (gate A1
and the validation plan): G_open = 1e-8 W/K, G_closed = 1e-5 W/K -- design
targets, never measured in this system. The forecast leak enters the energy
ledger and the falsification checks (A1 bound: leak < 1% of P_cool_MC =
2e-6 W). The gas-gap heat switch remains NOT_IMPLEMENTED (registry only).
Deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

T_4K_STAGE_K = 4.0            # canonical stage temperature (radiation chain)
A1_LEAK_BOUND_W = 2.0e-6      # 1% of P_cool_MC = 200 uW (gate A1 threshold)


@dataclass(frozen=True)
class HeatSwitchSpec:
    name: str = "SC_heat_switch_MC_to_4K"
    G_open_W_K: float = 1.0e-8      # DESIGN_SPECIFIED (gate A1 / validation plan)
    G_closed_W_K: float = 1.0e-5    # DESIGN_SPECIFIED (validation plan)
    provenance: str = ("DESIGN_SPECIFIED design targets (gate A1, validation "
                       "plan); not measured in this system")
    status: str = "REDUCED_ORDER"   # lumped stage-level path, not 3D-resolved

    def conductance_W_K(self, state: str) -> float:
        if state == "OPEN":
            return self.G_open_W_K
        if state == "CLOSED":
            return self.G_closed_W_K
        raise ValueError(f"heat-switch state must be OPEN or CLOSED, got {state!r}")


def switch_leak_forecast_W(state: str, T_base_K: float,
                           spec: HeatSwitchSpec | None = None) -> dict:
    """Bounded stage-level leak forecast P = G(state) * (T_4K - T_base) [W]."""
    spec = spec or HeatSwitchSpec()
    G = spec.conductance_W_K(state)
    P = G * (T_4K_STAGE_K - T_base_K)
    return {"switch": spec.name, "state": state, "G_W_K": G,
            "T_hot_K": T_4K_STAGE_K, "T_cold_K": T_base_K,
            "leak_forecast_W": P, "A1_bound_W": A1_LEAK_BOUND_W,
            "within_A1_bound_in_model": bool(P < A1_LEAK_BOUND_W),
            "status": spec.status, "provenance": spec.provenance,
            "label": LABEL}
