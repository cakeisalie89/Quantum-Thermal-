"""Coupling ledger for the coupled 3D transient layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

"Coupled" is defined here explicitly: every arrow between physics domains is
enumerated with its transferred quantity, units, direction, update scheme,
honesty status, and the concrete check/test that covers it. Statuses use the
single project vocabulary: IMPLEMENTED, REDUCED_ORDER, BOUNDED_FORECAST,
SCAFFOLD, NOT_IMPLEMENTED. Nothing in this ledger is a hardware claim.
Deterministic.
"""
from __future__ import annotations

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

ALLOWED_STATUSES = ("IMPLEMENTED", "REDUCED_ORDER", "BOUNDED_FORECAST",
                    "SCAFFOLD", "NOT_IMPLEMENTED")


def _c(source, target, quantity, units, direction, scheme, status, check,
       modes, note=""):
    assert status in ALLOWED_STATUSES, status
    return {"source_domain": source, "target_domain": target,
            "quantity": quantity, "units": units, "direction": direction,
            "scheme": scheme, "status": status,
            "conservation_or_check": check, "active_modes": modes,
            "note": note, "label": LABEL}


def coupling_ledger() -> list:
    """The full coupling inventory of the 3D layer (deterministic order)."""
    return [
        _c("laser (fs, Gaussian x Beer-Lambert)", "thermal field",
           "volumetric deposited power", "W per cell", "one-way",
           "explicit precomputed cell-integrated fractions x P(t)",
           "IMPLEMENTED",
           "source-integral consistency (exact, rel 0.0) + energy closure",
           "MODE_B",
           "time-averaged deposition approximation in 'averaged' mode "
           "(stated); pulse-envelope mode available"),
        _c("temperature field", "material properties k(T), cp(T)",
           "conductivity / heat capacity", "W/m/K ; J/kg/K",
           "two-way within the thermal solve", "monolithic (inside BDF rhs)",
           "IMPLEMENTED",
           "analytic diffusion-decay check + energy closure", "ALL",
           "homogeneous canonical diamond model, identical to 1D/2D"),
        _c("temperature field", "Kapitza-radiative back boundary",
           "back-face sink flux", "W/m^2",
           "two-way within the thermal solve (nonlinear BC)",
           "monolithic (inside BDF rhs)", "IMPLEMENTED",
           "Kapitza sign check + energy closure", "ALL", ""),
        _c("Mode B final field", "Mode C initial condition",
           "temperature field hand-off", "K", "one-way", "staggered",
           "IMPLEMENTED", "mode-sequence handoff test", "MODE_B->MODE_C", ""),
        _c("Mode C final state", "Mode D readiness",
           "NV probe vs recovery threshold", "K", "one-way", "staggered",
           "IMPLEMENTED", "blocked-before-recovery test", "MODE_C->MODE_D", ""),
        _c("temperature (NV plane)", "surface coverage rates",
           "desorption/adsorption rate modulation", "1/s", "one-way",
           "staggered (per-point canonical isotherm ODE)", "REDUCED_ORDER",
           "coverage boundedness + closed-form purge-decay test",
           "MODE_C,MODE_D", "per-point ODE on the NV plane; not a surface PDE"),
        _c("He-3/He-4 dose flux", "surface coverage",
           "incident molecular flux", "1/m^2/s", "one-way", "staggered",
           "IMPLEMENTED", "coverage boundedness (0<=theta<=1)", "MODE_D",
           "canonical kinetic-flux law at the canonical dose condition"),
        _c("shutter/baffle state", "radiative front-face load",
           "attenuated stage-chain heat load", "W", "one-way", "staggered",
           "REDUCED_ORDER",
           "state-ordering check (open > closed) + energy closure covers the "
           "applied flux", "ALL",
           "uniform front-face distribution; 3D view factors NOT_IMPLEMENTED"),
        _c("shutter/baffle state", "molecular-beam exposure gating",
           "exposure allowed flag", "bool", "one-way", "staggered",
           "IMPLEMENTED", "closed-shutter -> no exposure test", "MODE_B,MODE_D",
           ""),
        _c("microwave/ESR drive", "thermal field (NV region)",
           "dissipated drive power", "W", "one-way", "staggered",
           "REDUCED_ORDER",
           "zero-outside-Mode-D test + energy closure covers the map",
           "MODE_D",
           "canonical line-model NV-region dissipation, uniform over region; "
           "3D field/SAR map NOT_IMPLEMENTED"),
        _c("heat-switch state", "MC<->4K stage conductance path",
           "state-dependent lumped leak", "W (G in W/K)", "one-way (ledger)",
           "staggered stage-level", "REDUCED_ORDER",
           "conductance ordering + A1 bound forecast (OPEN states)", "ALL",
           "DESIGN_SPECIFIED G_open/G_closed; not resolved inside the 3D "
           "sample field; gas-gap switch NOT_IMPLEMENTED"),
        _c("support geometry", "parasitic conductive load",
           "support leakage", "W", "-", "-", "SCAFFOLD",
           "registry entry only (cryo_stack_3d)", "ALL",
           "no canonical support-conductance numbers exist; none invented"),
        _c("vibration spectrum", "heating",
           "transfer-chain dissipated power", "W", "one-way (ledger scalar)",
           "staggered", "BOUNDED_FORECAST",
           "ledger presence; no spatial claim", "ALL",
           "3D modal analysis NOT_IMPLEMENTED"),
        _c("vibration amplitude", "NV eligibility penalty",
           "NV-region amplitude vs Mode-D threshold", "m", "one-way",
           "staggered", "IMPLEMENTED",
           "eligibility blocking test (threshold from canonical chain)",
           "MODE_D", ""),
        _c("optical collection path", "heat load", "absorbed optical power",
           "W", "-", "-", "NOT_IMPLEMENTED",
           "honest stub in sources_3d", "MODE_D",
           "no repository parameterization; laser deposition is the "
           "implemented optical channel"),
        _c("temperature/species/loads", "NV sensing eligibility forecast",
           "eligibility flags + reasons", "-", "one-way", "staggered",
           "IMPLEMENTED",
           "eligibility test (never PASS; blocked/conditional/unknown only)",
           "MODE_D", ""),
        _c("surface coverage", "optical/boundary feedback",
           "absorption or boundary-state modification", "-", "-", "-",
           "NOT_IMPLEMENTED", "-", "-",
           "no validated coverage-to-optics model exists; none invented"),
    ]


def ledger_summary(entries=None) -> dict:
    entries = entries if entries is not None else coupling_ledger()
    counts = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {"n_couplings": len(entries), "status_counts": counts,
            "allowed_statuses": list(ALLOWED_STATUSES), "label": LABEL}
