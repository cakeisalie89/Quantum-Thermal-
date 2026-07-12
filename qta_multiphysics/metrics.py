"""Assemble the multiphysics gate specifications from model results.

Every gate returned here uses a status in {CONDITIONAL, BLOCKED, UNKNOWN,
DERIVED_CHECK}. NONE is PASS. The host sim (qta_full_sim.py) converts these
dicts into Gate objects whose to_dict() stamps measured_in_this_system=false and
can_PASS_now=NO. The numerical/structural self-consistency gates are
DERIVED_CHECK (first-principles numerical checks); model-forecast physical
quantities are CONDITIONAL (a real measurement could satisfy them); quantities
gated behind an already-blocked prerequisite (Mode D sensing / full cycle) are
BLOCKED.
"""
from __future__ import annotations


def _g(gid, name, mode, eq, computed, thresh, status, reason, fix, unit=""):
    return dict(gid=gid, name=name, mode=mode, eq=eq, computed=computed,
                thresh=thresh, status=status, reason=reason, fix=fix, unit=unit)


def build_gate_specs(cm, vs, mc, future3d_status):
    """cm: coupled metrics; vs: verification summary; mc: MC summary."""
    th = 0.050  # Mode D NV-layer temperature threshold [K]
    specs = []

    # ----- numerical / structural DERIVED_CHECK gates -----
    specs.append(_g(
        "THERMAL_1D_STABILITY_CHECK", "1D thermal solver numerically stable",
        "MODE_B_PROCESS", "diffusion_sanity rel_err < 0.10 AND finite AND solver=ok",
        f"{vs['diffusion']['rel_error']:.3e}", "0.10", "DERIVED_CHECK",
        "1D PDE reproduces analytic diffusion decay; fields finite; BDF converged. "
        "Numerical self-consistency only (MODEL_ONLY); not hardware-validated.",
        "Hardware thermometry of the NV-layer during Mode B to validate the model."))
    specs.append(_g(
        "THERMAL_2D_STABILITY_CHECK", "2D axisymmetric thermal solver stable",
        "MODE_B_PROCESS", "2D fields finite AND axis non-singular AND solver=ok",
        f"axis_finite={vs['symmetry']['axis_finite']}", "finite", "DERIVED_CHECK",
        "2D axisymmetric FV solver produces finite fields with no r=0 singularity "
        "(MODEL_ONLY).", "Hardware validation of 2D thermal profile."))
    specs.append(_g(
        "THERMAL_1D_MESH_CONVERGENCE_CHECK", "1D thermal mesh convergence",
        "MODE_B_PROCESS", "|NV_T(400)-NV_T(200)|/NV_T < 0.15",
        f"{vs['mesh_1d']['thermal_1d_rel_change_200_400']:.3e}", "0.15", "DERIVED_CHECK",
        "NV-layer temperature changes <15% between n=200 and n=400 (MODEL_ONLY "
        "mesh refinement).", "Confirm against measured profiles."))
    specs.append(_g(
        "THERMAL_2D_MESH_CONVERGENCE_CHECK", "2D thermal mesh convergence",
        "MODE_B_PROCESS", "|NVmax(fine)-NVmax(med)|/NVmax < 0.25",
        f"{vs['mesh_2d']['thermal_2d_rel_change_med_fine']:.3e}", "0.25", "DERIVED_CHECK",
        "2D NV-layer max temperature changes <25% between medium and fine mesh "
        "(MODEL_ONLY).", "Confirm against measured 2D profile."))
    specs.append(_g(
        "KAPITZA_BC_CHECK", "Kapitza backside boundary sign-correct",
        "MODE_B_PROCESS", "T>Tf -> heat leaves; T<Tf -> heat enters",
        f"sign_correct={vs['kapitza']['sign_correct']}", "True", "DERIVED_CHECK",
        "Backside Kapitza-radiative term cools when hotter than the fridge and "
        "warms when colder (MODEL_ONLY). alpha_K is ASSUMED.",
        "Measure interfacial (Kapitza) conductance at 10 mK."))
    specs.append(_g(
        "MOVING_BOUNDARY_MODEL_CHECK", "Moving-boundary front model present",
        "MODE_B_PROCESS", "s(t)=s0+v_front*t implemented; Stefan-ready structure",
        "implemented(v_front=0 first pass)", "implemented", "DERIVED_CHECK",
        "Front position is integrated as s(t)=s0+v_front*t and the code is "
        "structured for a later Stefan condition. First pass uses v_front=0 "
        "(static front). MODEL_ONLY / FORECAST_ONLY.",
        "Supply measured growth-front velocity / latent-heat data."))
    specs.append(_g(
        "OPTICAL_ABSORPTION_PROFILE_CHECK", "Optical absorption energy-conserving",
        "MODE_B_PROCESS", "int Q dV == absorbed power within tol (1D & 2D)",
        f"1D rel={vs['source_1d']['rel_error']:.2e}; 2D rel={vs['source_2d']['rel_error']:.2e}",
        "0.10", "DERIVED_CHECK",
        "Beer-Lambert (depth) x Gaussian (radial) deposition integrates to the "
        "absorbed power within tolerance (MODEL_ONLY). alpha, absorbed_fraction "
        "ASSUMED.", "Measure absorption coefficient and absorbed fraction at 1030 nm, 10 mK."))
    specs.append(_g(
        "GAS_TRANSPORT_STABILITY_CHECK", "Gas-transport PDE stable & finite",
        "MODE_C_PURGE", "advection-diffusion-sink finite; non-negative; solver ok",
        "finite_nonneg", "finite", "DERIVED_CHECK",
        "1D advection-diffusion-sink transport integrates stably with non-negative "
        "densities (MODEL_ONLY). Transport coefficients ASSUMED.",
        "Measure species conductances / pumping speeds in the actual line."))
    specs.append(_g(
        "LUMPED_MODEL_RETIRED_CHECK", "Lumped model is comparator-only",
        "MODE_B_PROCESS", "non-lumped feeds gates; lumped only in comparison CSV",
        "comparator_only", "comparator_only", "DERIVED_CHECK",
        "The legacy lumped thermal/gas estimate is retained ONLY in "
        "lumped_vs_nonlumped_comparison.csv as a regression sanity check; the "
        "non-lumped 1D/2D models are the gate authority (MODEL_ONLY).",
        "N/A (bookkeeping check)."))
    specs.append(_g(
        "THREE_D_LAYER_STATUS_CHECK", "3D transient layer status (forecast-only, not faked)",
        "ALL (multiphysics)", "3D status == FORECAST_ONLY_IMPLEMENTED; introduces no PASS gates",
        future3d_status, "FORECAST_ONLY_IMPLEMENTED", "DERIVED_CHECK",
        "The 3D transient layer is implemented only as an additive forecast-only / "
        "benchmark-numerical validation layer (reduced CI mesh; reduction-checked "
        "against the canonical 1D and 2D axisymmetric backends; energy-conserving "
        "deposition; deterministic). It is not hardware-validated, not COMSOL, not "
        "measured in-system, and introduces no PASS gates. 1D (canonical) and 2D "
        "axisymmetric remain the gate authority.",
        "In-system measurements would be required before any 3D physical claim."))

    # ----- model-forecast CONDITIONAL gates -----
    specs.append(_g(
        "NV_LAYER_TEMPERATURE_CHECK", "NV-layer recools for sensing",
        "MODE_D_SENSE", "T_NV_layer(after Mode C) <= 50 mK",
        f"{cm['Mode_D_T_NV_layer_K']:.3e}", f"{th:.3e}", "CONDITIONAL",
        "Model forecasts the NV layer recools to base after Mode C. FORECAST_ONLY; "
        "REQUIRES_EXPERIMENT (NV thermometry). Not a validated result.",
        "Measure NV-layer temperature post-recovery via ODMR thermometry.", "K"))
    specs.append(_g(
        "HOTSPOT_MARGIN_CHECK", "Mode B peak temperature within material margin",
        "MODE_B_PROCESS", "max_T(Mode B) below graphitization/damage margin",
        f"{cm['Mode_B_peak_T_K']:.3e}", "DESIGN_MARGIN", "CONDITIONAL",
        "Reduced-order near-field model forecasts a peak temperature. The damage "
        "margin itself is ASSUMED; FORECAST_ONLY.",
        "Measure local peak temperature / damage threshold under the actual pulse train.", "K"))
    specs.append(_g(
        "POST_PULSE_DRIFT_CHECK", "Post-recovery NV-layer drift small",
        "MODE_D_SENSE", "post-pulse drift small over sensing window",
        f"{cm['Mode_C_recool_time_s']:.3e}", "design", "CONDITIONAL",
        "Model forecasts the NV-layer temperature settles after Mode C. "
        "FORECAST_ONLY; REQUIRES_EXPERIMENT.",
        "Measure temperature drift during the sensing window.", "s"))
    specs.append(_g(
        "CRYOBAFFLE_CAPTURE_CHECK", "Cryobaffle captures process species",
        "MODE_C_PURGE", "cryobaffle capture fraction adequate",
        "ASSUMED_capture_profile", "design", "CONDITIONAL",
        "Capture probabilities are ASSUMED; the transport model uses them to "
        "forecast cleanup. FORECAST_ONLY.",
        "Measure baffle capture efficiency for each species at temperature."))
    specs.append(_g(
        "SURFACE_COVERAGE_DECAY_CHECK", "Surface coverage decays before sensing",
        "MODE_D_SENSE", "residual theta(after Mode C) < 1e-3",
        f"{cm['Mode_D_residual_surface_theta']:.3e}", "1.0e-3", "CONDITIONAL",
        "Langmuir model forecasts coverage decays during Mode C. Sticking and "
        "desorption energetics ASSUMED; FORECAST_ONLY; REQUIRES_EXPERIMENT.",
        "Measure surface coverage (e.g. desorption spectroscopy) at 10 mK."))
    specs.append(_g(
        "MICROWAVE_HEATING_PROFILE_CHECK", "MW dissipation anchored away from NV",
        "MODE_D_SENSE", "dissipated power at NV region within budget",
        "path_distributed", "design", "CONDITIONAL",
        "Path-distributed attenuation model forecasts where MW power dissipates. "
        "Attenuations DESIGN_SPECIFIED; FORECAST_ONLY.",
        "Measure attenuator heat loads and CPW termination dissipation at 10 mK."))
    specs.append(_g(
        "RADIATION_VIEWFACTOR_CHECK", "Radiation leakage to 10 mK bounded",
        "MODE_D_SENSE", "sum q_ij reaching 10 mK within budget",
        "view_factor_cascade", "design", "CONDITIONAL",
        "Staged view-factor cascade forecasts radiative load. Emissivities/view "
        "factors ASSUMED; FORECAST_ONLY.",
        "Measure radiative heat load with shutter/baffle engaged."))
    specs.append(_g(
        "VIBRATION_TRANSFER_CHECK", "Vibration attenuated for sensing",
        "MODE_D_SENSE", "NV-region amplitude below threshold; settled",
        "banded_transfer", "design", "CONDITIONAL",
        "Banded transfer model forecasts vibration attenuation and settling. "
        "Transfer factors ASSUMED; FORECAST_ONLY.",
        "Measure vibration spectrum at the sample mount."))

    # ----- BLOCKED (behind already-blocked prerequisites) -----
    specs.append(_g(
        "RESIDUAL_SPECIES_MODE_D_CHECK", "Residual process species cleared for Mode D",
        "MODE_D_SENSE", "residual CH4/H2 at sample below sensing-safe level",
        f"CH4={cm['Mode_D_residual_CH4_density_m3']:.2e}; H2={cm['Mode_D_residual_H2_density_m3']:.2e}",
        "REQUIRES_MEASUREMENT", "BLOCKED",
        "Mode D sensing capability is itself BLOCKED (helium-isotope sensing not "
        "validated; Mode B feasibility not established). The transport model "
        "forecasts residuals but cannot be a PASS. FORECAST_ONLY.",
        "Establish Mode B feasibility and validated Mode D sensing first; then "
        "measure residual partial pressures at the sample.", "1/m^3"))
    specs.append(_g(
        "COUPLED_MODE_RECOVERY_CHECK", "Full B->C->D recovery cycle viable",
        "ALL (multiphysics)", "Mode D readiness from final distributed state",
        cm["Mode_D_readiness_status"], "REQUIRES_VALIDATION", "BLOCKED",
        "End-to-end recovery depends on unvalidated LCVD (Mode B) and unvalidated "
        "helium sensing (Mode D). The coupled model forecasts recovery (limiting "
        f"process: {cm['limiting_recovery_process']}), but the full cycle cannot "
        "PASS pre-experiment. FORECAST_ONLY.",
        "Validate Mode B and Mode D independently, then demonstrate a full cycle."))

    return specs
