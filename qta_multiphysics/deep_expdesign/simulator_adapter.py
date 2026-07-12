"""Strict adapter around the existing QTA forward models.

Accepts a latent parameter vector and an experiment-design vector, runs the
appropriate *existing* QTA physics path (the NV colored-noise coherence model
from :mod:`qta_multiphysics.nv_spin`, plus documented forecast maps for contrast
/ recovery / isolation observables), and returns a structured
:class:`SimulationResponse`. Deterministic seeds; failures are captured
explicitly and never replaced by invented data.

The reduced forecast maps here are model-only and are NOT claimed to be a DSMC,
Boltzmann, Fermi-liquid, or full 3D simulation. Every response is labelled
``forecast_only=True`` / ``measured_in_this_system=False``.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np

from .schemas import SimulationResponse
from .design_space import ExperimentDesign, validate_design

# fixed forecast constants for the surrogate forward map (model-only)
_SIGMA_RAD_S = 2.0 * math.pi * 5.0e3      # OU noise amplitude (matches nv_spin default)
# Frequency-grid resolution for the deep-layer forward coherence. Smaller than the
# nv_spin default (4000) for tractable repeated inference; values are stable to
# <1e-3 vs the default across the prior (verified), and it is used consistently in
# dataset generation, EIG estimation, and validation so comparisons stay matched.
_N_OMEGA = 256
_OBSERVABLE_NAMES = ["coherence_at_tmeas", "odmr_contrast", "he_contrast",
                     "thermal_recovery_obs", "isolation_leak_obs"]
# per-observable measurement noise (std, in observable units) — forecast
_MEAS_SIGMA = {
    "coherence_at_tmeas": 0.01,
    "odmr_contrast": 0.01,
    "he_contrast": 0.02,
    "thermal_recovery_obs": 0.05,   # relative (log space handled by caller)
    "isolation_leak_obs": 0.10,     # relative
}


def observable_names() -> list:
    """Names of the observable vector produced by the adapter (fixed order)."""
    return list(_OBSERVABLE_NAMES)


def _source_commit(repo_root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _temp_factor(T_K: float) -> float:
    """Forecast ODMR-contrast attenuation vs surface temperature (model-only)."""
    return float(1.0 / (1.0 + (T_K / 0.1) ** 2))


def _coverage_factor(cov: float) -> float:
    """Forecast He-contrast scaling vs coverage target (saturating, model-only)."""
    return float(1.0 - math.exp(-max(0.0, cov)))


def simulate(theta: dict, design: ExperimentDesign, *, seed: int,
             repo_root: Optional[Path] = None,
             validate: bool = True) -> SimulationResponse:
    """Run the forward model for one (latent, design) pair.

    ``theta`` maps trainable latent names to natural-unit values
    (tau_c [s], he3_he4_contrast, nv_contrast_10mK, thermal_recovery_time [s],
    mode_isolation_leak). Returns a :class:`SimulationResponse`; on any failure,
    ``success=False`` and ``observables={}`` (no invented data).
    """
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    commit = _source_commit(root)
    sim_id = f"sim-{seed:012d}"

    if validate:
        reasons = validate_design(design)
        if reasons:
            return SimulationResponse(
                simulation_id=sim_id, parameter_vector=dict(theta),
                design_vector=design.to_dict(), observables={}, success=False,
                failure_reason="invalid_design: " + "; ".join(reasons),
                numerical_profile={"backend": "numpy_cpu"}, seed=seed,
                source_commit=commit)

    try:
        from qta_multiphysics.nv_spin.noise import filter_function_coherence
        tau_c = float(theta["tau_c"])
        seq = (design.pulse_sequence or "hahn").lower()
        seq_for_coh = seq if seq in ("ramsey", "hahn", "xy8") else "hahn"
        coh = filter_function_coherence(
            sequence=seq_for_coh, tau_c_s=tau_c, sigma_rad_s=_SIGMA_RAD_S,
            total_time_s=float(design.measurement_time), n_omega=_N_OMEGA)
        odmr = float(theta["nv_contrast_10mK"]) * _temp_factor(design.surface_temperature)
        he = float(theta["he3_he4_contrast"]) * _coverage_factor(design.he3_coverage_target)
        rec = math.log10(max(1e-12, float(theta["thermal_recovery_time"])
                             * (1.0 + design.recovery_duration / 1.0e3)))
        leak = math.log10(max(1e-12, float(theta["mode_isolation_leak"])))

        clean = {
            "coherence_at_tmeas": coh,
            "odmr_contrast": odmr,
            "he_contrast": he,
            "thermal_recovery_obs": rec,    # log10 seconds
            "isolation_leak_obs": leak,     # log10 fraction
        }
        # deterministic measurement noise (seeded)
        rng = np.random.default_rng(seed)
        obs = {}
        for k in _OBSERVABLE_NAMES:
            obs[k] = float(clean[k] + rng.normal(0.0, _MEAS_SIGMA[k]))
        if not all(np.isfinite(v) for v in obs.values()):
            raise FloatingPointError("non-finite observable")
        return SimulationResponse(
            simulation_id=sim_id, parameter_vector=dict(theta),
            design_vector=design.to_dict(), observables=obs, success=True,
            failure_reason=None,
            numerical_profile={"backend": "numpy_cpu", "sequence": seq_for_coh},
            seed=seed, source_commit=commit)
    except Exception as e:  # capture, never fabricate
        return SimulationResponse(
            simulation_id=sim_id, parameter_vector=dict(theta),
            design_vector=design.to_dict(), observables={}, success=False,
            failure_reason=f"{type(e).__name__}: {e}",
            numerical_profile={"backend": "numpy_cpu"}, seed=seed,
            source_commit=commit)


def clean_forward(theta: dict, design: ExperimentDesign) -> dict:
    """Noise-free forward observable (used by EIG/likelihood reference)."""
    from qta_multiphysics.nv_spin.noise import filter_function_coherence
    seq = (design.pulse_sequence or "hahn").lower()
    seq_for_coh = seq if seq in ("ramsey", "hahn", "xy8") else "hahn"
    coh = filter_function_coherence(sequence=seq_for_coh, tau_c_s=float(theta["tau_c"]),
                                    sigma_rad_s=_SIGMA_RAD_S,
                                    total_time_s=float(design.measurement_time), n_omega=_N_OMEGA)
    return {
        "coherence_at_tmeas": coh,
        "odmr_contrast": float(theta["nv_contrast_10mK"]) * _temp_factor(design.surface_temperature),
        "he_contrast": float(theta["he3_he4_contrast"]) * _coverage_factor(design.he3_coverage_target),
        "thermal_recovery_obs": math.log10(max(1e-12, float(theta["thermal_recovery_time"])
                                               * (1.0 + design.recovery_duration / 1.0e3))),
        "isolation_leak_obs": math.log10(max(1e-12, float(theta["mode_isolation_leak"]))),
    }


def meas_sigma() -> dict:
    """Per-observable measurement-noise std (forecast)."""
    return dict(_MEAS_SIGMA)
