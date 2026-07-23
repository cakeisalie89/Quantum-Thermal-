"""Deterministic uncertainty propagation for the three-cycle campaign.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

A fixed ensemble of M parameter draws (its own documented seed; every
existing Monte Carlo family, seed, count, mesh and tolerance untouched) is
propagated through the ANALYTIC campaign layer only -- no PDE solves. Each
sampled apparatus parameter is drawn ONCE per ensemble member and held
fixed for that member's entire three-cycle B->C->D campaign (correlation
preservation by construction); carried state (panel inventory, residual
coverage, clearance context) accumulates across phases and cycles with no
resampling anywhere.

Varied parameters -- ONLY where an authoritative range/spread already
exists in the project (verified before coding):

- s_CH4_panel ~ Uniform(0.3, 0.8)
    source: cryopanel_memory_model.csv PANEL-CH4 row, verbatim
    "ASSUMED s ~ 0.3-0.8 at 4 K"; the Stage-2 midpoint 0.55 is this
    distribution's center. PLACEHOLDER/ASSUMED status carried over.
- sticking_coverage_CH4 ~ lognormal-multiplicative(center 0.5, sigma 0.2)
    center: canonical CoverageSpec("CH4", sticking=0.5)
    spread: qta_multiphysics/uncertainty.py gas-surrogate block
    `stick = clip(lf(0.5, 0.2), ...)` (same parameter, same center).
- purge_1_s ~ lognormal-multiplicative(center 5.0, sigma 0.3)
    center: canonical species-accounting purge rate (5.0 1/s)
    spread: uncertainty.py `purge = lf(..., 0.3)` for the purge-rate class.

Thermal uncertainty enters by MARGINAL composition only: the canonical
3-D Monte Carlo summary (n=120, seed 12345) quantiles are quoted verbatim
and labeled marginal -- sample-level draws are NOT matched across families,
so no joint thermal-sensing/thermal-panel correlation is claimed.

Excluded from variation (fixed; reasons recorded machine-readably):
s_H2_panel (CSV gives a point '~0.01', no range), s_He (structural zero),
N_ML_cap (PLACEHOLDER point, no range), tau_c and C_contr (UNKNOWN gates,
no authoritative range -- forecasts conditional on canonical values),
vibration transfer factors (ASSUMED points, no ranges), cumulative energy
terms (from fixed deterministic solves), all canonical pressures/windows,
and the sensitivity layer's +10% STEP (a perturbation convention, not a
distribution -- deliberately not used).

No cross-parameter correlations are introduced (none exist in any
authoritative source); parameters are drawn independently and this is
stated in the output record.
"""
from __future__ import annotations

import math

import numpy as np

from .cryopanel_dynamics_3d import (phase_fluxes_per_m2_s, phase_windows_s,
                                    SITES_PER_M2, N_ML_CAP)
from .species_accounting_3d import DOSE_WINDOW_S  # noqa: F401 (provenance)

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
SCHEMA_VERSION = "1.0"

#: dedicated deterministic seed for this additive family (date-stamped);
#: the canonical families keep 42 / 12345 untouched.
CAMPAIGN_UNC_SEED = 20260717
#: ensemble size matches the existing 3-D MC convention (uncertainty.py
#: n_samples=120) -- cited, not invented.
N_MEMBERS = 120

# canonical centers (single authorities)
S_CH4_PANEL_LO, S_CH4_PANEL_HI = 0.3, 0.8          # CSV verbatim range
STICK_COV_CH4_CENTER = 0.5                          # CoverageSpec("CH4")
STICK_COV_SIGMA = 0.2                               # uncertainty.py spread
PURGE_CENTER_1_S = 5.0                              # species accounting
PURGE_SIGMA = 0.3                                   # uncertainty.py spread
CRYOTRAP_CH4_1_S = 5.0                              # CoverageSpec("CH4")
PURGE_WINDOW_S = 2.0                                # canonical purge window

DISTRIBUTIONS = [
    {"parameter": "s_CH4_panel", "distribution": "uniform",
     "params": {"low": S_CH4_PANEL_LO, "high": S_CH4_PANEL_HI},
     "center_source": "cryopanel_memory_model.csv PANEL-CH4 "
                      "('ASSUMED s ~ 0.3-0.8 at 4 K', verbatim range)",
     "classification": "ASSUMED/PLACEHOLDER (unmeasured)"},
    {"parameter": "sticking_coverage_CH4",
     "distribution": "lognormal_multiplicative",
     "params": {"center": STICK_COV_CH4_CENTER, "sigma": STICK_COV_SIGMA,
                "clip": [1e-3, 1.0]},
     "center_source": "surface_coverage.default_coverage_specs CH4 "
                      "sticking=0.5",
     "spread_source": "uncertainty.py gas-surrogate stick=lf(0.5,0.2)",
     "classification": "ASSUMED (unmeasured)"},
    {"parameter": "purge_1_s", "distribution": "lognormal_multiplicative",
     "params": {"center": PURGE_CENTER_1_S, "sigma": PURGE_SIGMA,
                "min": 0.1},
     "center_source": "species_accounting_3d canonical purge_1_s=5.0",
     "spread_source": "uncertainty.py purge=lf(.,0.3) spread for the "
                      "purge-rate class",
     "classification": "ASSUMED (unmeasured)"},
]

EXCLUSIONS = [
    {"parameter": "s_H2_panel", "reason": "CSV declares a point value "
     "('~ 0.01'), no range", "class": "UNKNOWN_RANGE_FIXED"},
    {"parameter": "s_He_panel", "reason": "structural zero (no 4 K He "
     "cryopumping; sorb NOT INSTALLED)", "class": "STRUCTURAL_FIXED"},
    {"parameter": "N_ML_cap", "reason": "PLACEHOLDER point; no declared "
     "range", "class": "PLACEHOLDER_FIXED"},
    {"parameter": "tau_c", "reason": "UNKNOWN gate; no authoritative "
     "range; forecasts conditional on canonical value",
     "class": "UNKNOWN_EXCLUDED"},
    {"parameter": "C_contr", "reason": "UNKNOWN gate; no authoritative "
     "range", "class": "UNKNOWN_EXCLUDED"},
    {"parameter": "vibration_transfer_factors", "reason": "ASSUMED points, "
     "no declared ranges", "class": "UNKNOWN_RANGE_FIXED"},
    {"parameter": "cumulative_energy_terms", "reason": "derived from fixed "
     "deterministic solves; no campaign-level variation source",
     "class": "DERIVED_FIXED"},
    {"parameter": "sensitivity_STEP_10pct", "reason": "a perturbation "
     "convention, not a distribution; deliberately not used",
     "class": "CONVENTION_NOT_A_DISTRIBUTION"},
]


def draw_members(n_members: int = N_MEMBERS, seed: int = CAMPAIGN_UNC_SEED):
    """One draw per member, reused for the member's whole campaign."""
    rng = np.random.default_rng(seed)
    members = []
    for _ in range(n_members):
        s_panel = float(rng.uniform(S_CH4_PANEL_LO, S_CH4_PANEL_HI))
        stick = float(np.clip(
            STICK_COV_CH4_CENTER * math.exp(rng.normal(0.0, STICK_COV_SIGMA)),
            1e-3, 1.0))
        purge = float(max(
            PURGE_CENTER_1_S * math.exp(rng.normal(0.0, PURGE_SIGMA)), 0.1))
        members.append({"s_CH4_panel": s_panel,
                        "sticking_coverage_CH4": stick,
                        "purge_1_s": purge})
    return members


def _panel_ml_per_cycle(s_panel: float, cfg, n_cycles: int) -> list:
    """Exact-solution CH4 panel loading per cycle for one member (linear
    regime at canonical exposures; exact form used regardless)."""
    F = phase_fluxes_per_m2_s("MODE_B")["C13_CH4"]
    dt = phase_windows_s(cfg)["MODE_B"]
    Ncap = N_ML_CAP * SITES_PER_M2
    N = 0.0
    out = []
    for _ in range(n_cycles):
        N = Ncap - (Ncap - N) * math.exp(-s_panel * F * dt / Ncap)
        out.append(N / Ncap)
    return out


def _residual_theta(purge_1_s: float) -> float:
    """Canonical closed-form Mode-C purge collapse from worst case
    theta0=1: residual = exp(-(purge+cryotrap)*window) -- the same analytic
    identity exercised by the existing coupled-layer test."""
    return math.exp(-(purge_1_s + CRYOTRAP_CH4_1_S) * PURGE_WINDOW_S)


def _q(vals):
    a = np.asarray(vals, dtype=float)
    return {"p05": f"{np.quantile(a, 0.05):.9e}",
            "p50": f"{np.quantile(a, 0.50):.9e}",
            "p95": f"{np.quantile(a, 0.95):.9e}"}


def propagate(cfg, n_cycles: int = 3, n_members: int = N_MEMBERS,
              seed: int = CAMPAIGN_UNC_SEED) -> dict:
    """Deterministic ensemble propagation (analytic layer only)."""
    members = draw_members(n_members, seed)
    ml = {c: [] for c in range(1, n_cycles + 1)}
    res = []
    for m in members:
        per_cycle = _panel_ml_per_cycle(m["s_CH4_panel"], cfg, n_cycles)
        for c in range(1, n_cycles + 1):
            ml[c].append(per_cycle[c - 1])
        res.append(_residual_theta(m["purge_1_s"]))
    # correlation-persistence witness: per-member cycle-3/cycle-1 ratio
    ratios = [ml[n_cycles][i] / ml[1][i] for i in range(n_members)]
    cycles = []
    for c in range(1, n_cycles + 1):
        cycles.append({
            "cycle": str(c),
            "panel_CH4_ML": _q(ml[c]),
            "theta_c13_residual_before_D": _q(res),
            "clearance_context": ("IL-05/06 FORECAST-basis derived context "
                                  "under uncertainty; never PASS"),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "seed": seed, "n_members": n_members, "n_cycles": n_cycles,
        "distributions": DISTRIBUTIONS,
        "exclusions": EXCLUSIONS,
        "correlation_structure": (
            "each apparatus parameter drawn once per member and held fixed "
            "for the member's entire campaign (perfect within-member "
            "persistence); parameters mutually independent -- no "
            "authoritative cross-parameter correlations exist and none "
            "were invented"),
        "persistence_witness": {
            "per_member_cycle3_over_cycle1_ML": _q(ratios),
            "note": "== n_cycles exactly in the linear regime because the "
                    "member's sticking persists; independent per-cycle "
                    "resampling would destroy this"},
        "marginal_composition": {
            "thermal": "quantiles quoted from the canonical 3-D Monte Carlo "
                       "summary (n=120, seed 12345); MARGINAL, not joint -- "
                       "sample-level draws are not matched across families",
        },
        "cycles": cycles,
        "measured_in_this_system": False,
        "can_PASS_now": "NO",
        "label": LABEL,
    }


def quantile_rows(rep: dict) -> list:
    rows = []
    for c in rep["cycles"]:
        for qty in ("panel_CH4_ML", "theta_c13_residual_before_D"):
            q = c[qty]
            rows.append({"cycle": c["cycle"], "quantity": qty,
                         "p05": q["p05"], "p50": q["p50"], "p95": q["p95"],
                         "basis": "deterministic ensemble "
                                  f"(seed {rep['seed']}, M={rep['n_members']})",
                         "label": LABEL})
    return rows
