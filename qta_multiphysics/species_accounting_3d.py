"""Species / mass accounting for the coupled 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Per-phase, per-species ledger enforcing the canonical mode/species policy and
reusing only existing canonical parameters:

* Mode B (C-13 methane precursor): the 3D arrival-flux footprint is
  NOT_IMPLEMENTED (no inlet geometry parameterized); the exposure is recorded
  as a BOUNDED_FORECAST at the canonical Mode-B working pressure (1e-4 Pa),
  and the purge forecast uses the stated WORST CASE theta0 = 1.
* Mode C (purge): the canonical 1D-layer purge parameters exactly
  (t_end = 2.0 s, purge_1_s = 5.0, canonical CoverageSpec cryotrap rates) via
  the same evolve_coverage ODE -- residual coverage before Mode D is a
  DERIVED forecast of that model.
* Mode D (He-3 / He-4): incident flux from the canonical kinetic-flux law at
  the canonical dose condition; C-13 methane is structurally forbidden
  (validator raises); H2 remains residual/background only.

Coverage boundedness (0 <= theta <= 1) is the surface-accounting check.
Deterministic.
"""
from __future__ import annotations

from .config import MultiphysicsConfig
from .surface_coverage import (default_coverage_specs, kinetic_flux,
                               evolve_coverage)
from .mode_sequence_3d import CANONICAL_ACTIVE, RESIDUAL_ONLY, LEGACY_ALIASES

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

K_B = 1.380649e-23
P_HE_DOSE_PA = 1.0e-6          # canonical dose pressure (D9)
P_C13_WORK_PA = 1.0e-4         # canonical Mode-B working pressure
PURGE_WINDOW_S = 2.0           # canonical Mode-C coverage purge window (1D layer)
PURGE_RATE_1_S = 5.0           # canonical Mode-C purge rate (1D layer)
DOSE_WINDOW_S = 1.0            # canonical coverage window (1D layer)
THETA0_WORST_CASE = 1.0        # stated worst-case Mode-B end coverage


def _canonical_name(internal: str) -> str:
    inv = {v: k for k, v in LEGACY_ALIASES.items()}
    return {v: k for k, v in inv.items()}.get(internal, internal)


def species_accounting_rows(cfg: MultiphysicsConfig) -> list:
    """Deterministic per-phase species ledger rows."""
    specs = default_coverage_specs()
    by = {s.name: s for s in specs}
    T = cfg.fridge.T_fridge_K
    rows = []

    def row(phase, species, role, allowed, flux, flux_status, th0, th1,
            window, status, note):
        bounded = (th0 == "" or 0.0 <= float(th0) <= 1.0) and \
                  (th1 == "" or 0.0 <= float(th1) <= 1.0)
        rows.append({
            "phase": phase, "species": species, "canonical_role": role,
            "allowed_active": str(allowed).lower(),
            "flux_in_per_m2_s": flux, "flux_status": flux_status,
            "coverage_start": th0, "coverage_end": th1,
            "window_s": f"{window:.3e}" if window != "" else "",
            "coverage_bounded_0_1": str(bool(bounded)).lower(),
            "status": status, "note": note, "label": LABEL})

    # ---- Mode B: C-13 methane exposure (footprint NOT_IMPLEMENTED) ----
    row("MODE_B", "C13_CH4", "MODE_B_precursor", True, "",
        "NOT_IMPLEMENTED (no inlet geometry parameterized; exposure bounded "
        f"by the canonical working pressure {P_C13_WORK_PA:.1e} Pa)",
        "", f"{THETA0_WORST_CASE:.3e}", "",
        "BOUNDED_FORECAST",
        "worst-case end-of-Mode-B coverage theta0=1 declared for the purge "
        "forecast; no arrival-flux map is invented")
    for he in ("He3", "He4"):
        row("MODE_B", he, "MODE_D_sensing", False, "0.0",
            "structurally zero (gate A4; validator raises if declared live)",
            "", "", "", "IMPLEMENTED", "forbidden during Mode B")

    # ---- Mode C: canonical purge of the worst-case C13 coverage ----
    theta0 = {s.name: (THETA0_WORST_CASE if s.name == "CH4" else 0.0)
              for s in specs}
    covC, _ = evolve_coverage(specs, {s.name: 0.0 for s in specs}, T,
                              PURGE_WINDOW_S, theta0=theta0,
                              purge_1_s=PURGE_RATE_1_S, n_eval=24)
    resC13 = float(covC["CH4"][-1])
    row("MODE_C", "C13_CH4", "MODE_B_precursor", False, "0.0",
        "source off (purge)", f"{THETA0_WORST_CASE:.3e}", f"{resC13:.9e}",
        PURGE_WINDOW_S, "REDUCED_ORDER",
        "canonical purge model (purge_1_s=5.0 + spec cryotrap) from the "
        "declared worst case; residual-before-Mode-D forecast")

    # ---- Mode D: He dose flux + coverage; C13 forbidden; H2 residual ----
    n_he = P_HE_DOSE_PA / (K_B * T)
    fluxes = {s.name: 0.0 for s in specs}
    for he in ("He3", "He4"):
        fluxes[he] = kinetic_flux(n_he, T, by[he].mass_amu)
    covD, _ = evolve_coverage(specs, fluxes, T, DOSE_WINDOW_S, n_eval=8)
    for he in ("He3", "He4"):
        row("MODE_D", he, "MODE_D_sensing", True, f"{fluxes[he]:.9e}",
            "IMPLEMENTED (canonical kinetic-flux law at the canonical dose "
            f"condition {P_HE_DOSE_PA:.1e} Pa, T_fridge)",
            "0.000e+00", f"{float(covD[he][-1]):.9e}", DOSE_WINDOW_S,
            "IMPLEMENTED", "canonical coverage window")
    row("MODE_D", "C13_CH4", "MODE_B_precursor", False, "0.0",
        "structurally forbidden (validator raises on any live declaration)",
        f"{resC13:.9e}", f"{resC13:.9e}", "", "IMPLEMENTED",
        "residual carried from the Mode-C purge forecast; no canonical "
        "residual-coverage bound is parameterized (bound NOT_IMPLEMENTED)")
    row("MODE_D", "H2", "residual_background_only", False, "",
        "residual/background only; never active (validator raises)",
        "", "", "", "IMPLEMENTED", "")
    assert set(CANONICAL_ACTIVE["MODE_D"]) == {"He3", "He4"}
    assert "H2" in RESIDUAL_ONLY
    return rows
