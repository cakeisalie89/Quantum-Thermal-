"""Dynamic cryopanel inventory model for the campaign-continuity layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Evolves per-panel adsorbed inventory (per unit panel area) for the in-scope
species {H2, C13_CH4, He} across the canonical Mode B -> C -> D phase windows
and across repeated campaign cycles. Capture-only during cold operations
(no regeneration term below the regeneration temperature -- IL-12 semantics).

Model (single Langmuir-type capture channel per panel/species):

    dN/dt = s * F(t) * (1 - N / N_cap)

with the EXACT constant-flux window solution used directly (deterministic,
no ODE solver):

    N(t0+dt) = N_cap - (N_cap - N(t0)) * exp(-s * F * dt / N_cap)

Units: N, N_cap [molecules/m^2]; F [molecules/m^2/s]; s dimensionless.

Parameter provenance (explicit; nothing presented as measured):
- s_H2 = 0.01               ASSUMED (cryopanel_memory_model.csv PANEL-H2 row)
- s_C13_CH4 = 0.55          ASSUMED midpoint of the CSV's declared 0.3-0.8
                            range (PANEL-CH4 row); PLACEHOLDER choice of
                            midpoint; coverage dependence neglected (stated)
- s_He = 0.0                ASSUMED: helium is not cryopumped by 4 K panels
                            (charcoal sorb NOT INSTALLED, gate E03); helium
                            is tracked through the mass ledger, not captured
- N_cap = N_ML_cap * sites  N_ML_cap = 1 monolayer, PLACEHOLDER worst-case
                            capacity basis (capacity per area is declared
                            unmeasured in the CSV); sites = 1.0e19 /m^2, the
                            canonical monolayer site density of the surface-
                            coverage layer (surface_coverage.evolve_coverage)
- Fluxes: canonical kinetic-flux law at canonical conditions only --
  Mode-B C-13 exposure at P_work = 1e-4 Pa; continuous H2 residual at the
  post-bakeout target 1e-12 Pa; Mode-D He dose at 1e-6 Pa. Gas temperature
  for panel-incident flux: 300 K (canonical top-stage temperature; ASSUMED
  thermalization of chamber gas).
- Phase windows: the canonical solver windows (SolverConfig pulse/recovery
  windows; the canonical 1.0 s dose window) -- the forecast is of the
  campaign AS MODELED, not of an arbitrary process duration.

Uncertainty/limitations (recorded, not hidden): sticking coefficients and
the monolayer capacity are ASSUMED/PLACEHOLDER and EXPERIMENTALLY_UNMEASURED;
saturation-time forecasts inherit that status; per-panel area is not needed
(per-area bookkeeping) and is not invented. Deterministic throughout.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .surface_coverage import kinetic_flux
from .species_accounting_3d import (P_HE_DOSE_PA, P_C13_WORK_PA,
                                    DOSE_WINDOW_S)

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

K_B = 1.380649e-23
SITES_PER_M2 = 1.0e19          # canonical monolayer basis (surface_coverage)
N_ML_CAP = 1.0                 # PLACEHOLDER worst-case capacity [monolayers]
T_GAS_K = 300.0                # ASSUMED chamber-gas thermalization temperature
P_H2_RESIDUAL_PA = 1.0e-12     # canonical post-bakeout target (CHAMBER_STATE)

MASS_AMU = {"H2": 2.016, "C13_CH4": 17.035, "He": 3.016}

STICKING = {
    "H2": (0.01, "ASSUMED (cryopanel_memory_model.csv PANEL-H2)"),
    "C13_CH4": (0.55, "ASSUMED midpoint of CSV 0.3-0.8 (PANEL-CH4); "
                       "PLACEHOLDER midpoint choice"),
    "He": (0.0, "ASSUMED zero: He not cryopumped at 4 K panels; charcoal "
                "sorb NOT INSTALLED (E03)"),
}

#: which species has a nonzero incident flux in which phase (mode/species
#: policy: methane exposure only in B; He dose only in D; H2 residual always)
def phase_fluxes_per_m2_s(phase: str) -> dict:
    f = {"H2": kinetic_flux(P_H2_RESIDUAL_PA / (K_B * T_GAS_K), T_GAS_K,
                            MASS_AMU["H2"]),
         "C13_CH4": 0.0, "He": 0.0}
    if phase == "MODE_B":
        f["C13_CH4"] = kinetic_flux(P_C13_WORK_PA / (K_B * T_GAS_K), T_GAS_K,
                                    MASS_AMU["C13_CH4"])
    if phase == "MODE_D":
        f["He"] = kinetic_flux(P_HE_DOSE_PA / (K_B * T_GAS_K), T_GAS_K,
                               MASS_AMU["He"])
    return f


@dataclass
class PanelInventory:
    """Per-area adsorbed inventory for one panel/species channel."""
    panel_id: str
    species: str
    N_per_m2: float = 0.0
    admitted_per_m2: float = 0.0     # integral of incident flux (bookkeeping)

    @property
    def sticking(self) -> float:
        return STICKING[self.species][0]

    @property
    def N_cap(self) -> float:
        return N_ML_CAP * SITES_PER_M2

    @property
    def monolayer_fraction(self) -> float:
        return self.N_per_m2 / self.N_cap

    def capture_window(self, flux_per_m2_s: float, dt_s: float) -> None:
        """Advance by one constant-flux window (exact Langmuir solution)."""
        if dt_s < 0:
            raise ValueError("dt_s must be >= 0")
        self.admitted_per_m2 += flux_per_m2_s * dt_s
        s = self.sticking
        if s <= 0.0 or flux_per_m2_s <= 0.0 or dt_s == 0.0:
            return
        Nc = self.N_cap
        self.N_per_m2 = Nc - (Nc - self.N_per_m2) * math.exp(
            -s * flux_per_m2_s * dt_s / Nc)
        # numerical guard (exact solution is bounded by construction)
        self.N_per_m2 = min(self.N_per_m2, Nc)

    def row(self, cycle: int, phase: str) -> dict:
        return {"cycle": str(cycle), "phase": phase,
                "panel_id": self.panel_id, "species": self.species,
                "sticking": f"{self.sticking:.3e}",
                "sticking_provenance": STICKING[self.species][1],
                "N_per_m2": f"{self.N_per_m2:.9e}",
                "monolayer_fraction": f"{self.monolayer_fraction:.9e}",
                "capacity_basis": f"N_ML_cap={N_ML_CAP:g} monolayer x "
                                  f"sites={SITES_PER_M2:.1e}/m2 (PLACEHOLDER "
                                  "worst case; capacity unmeasured)",
                "admitted_per_m2": f"{self.admitted_per_m2:.9e}",
                "label": LABEL}


def new_panel_set() -> list:
    """In-scope panels (canonical CSV rows PANEL-H2 / PANEL-CH4 + the He
    zero-capture channel)."""
    return [PanelInventory("PANEL-H2", "H2"),
            PanelInventory("PANEL-CH4", "C13_CH4"),
            PanelInventory("PANEL-He(no-capture)", "He")]


def advance_phase(panels: list, phase: str, dt_s: float) -> None:
    """Advance every panel through one canonical phase window."""
    fx = phase_fluxes_per_m2_s(phase)
    for p in panels:
        p.capture_window(fx[p.species], dt_s)


def phase_windows_s(cfg) -> dict:
    """Canonical per-cycle exposure windows (the campaign AS MODELED)."""
    return {"MODE_B": float(cfg.solver.pulse_window_s),
            "MODE_C": float(cfg.solver.recovery_window_s),
            "MODE_D": float(DOSE_WINDOW_S)}
