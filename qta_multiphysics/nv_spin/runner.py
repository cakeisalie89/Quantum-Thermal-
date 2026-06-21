"""Top-level NV spin-dynamics runner.

Responsibilities:
  * Enforce Mode C recovery readiness BEFORE any Mode D spin calculation
    (raises :class:`ModeReadinessError` otherwise) - sensing never runs before
    isolation/purge/cryotrap/thermal recovery is declared ready.
  * Build the NV Hamiltonian at the Mode D sample temperature.
  * Run Ramsey / Hahn / XY8 coherence curves (colored-noise) and an ODMR
    transition spectrum.
  * Extract the distinct observables tau_c (input) vs T1 (input) vs T2 (echo)
    vs T2* (Ramsey).
  * Write deterministic, provenance-tagged ``nv_*`` outputs.

Determinism: fixed seed, sorted JSON keys, fixed float formatting, LF newlines.
Two runs with the same config produce byte-identical files.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import numpy as np

from . import model as _model
from . import sequences as _seq
from .spin_config import NVSpinConfig, standard_config

import qutip as qt


class ModeReadinessError(RuntimeError):
    """Raised when Mode D spin dynamics are requested before Mode C readiness."""


_FLOAT = "{:.10e}"


def _fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float):
        if math.isnan(x):
            return "nan"
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        return _FLOAT.format(x)
    return str(x)


def _write_csv(path: Path, header: list, rows: list) -> None:
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join(_fmt(v) for v in r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(
        json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def parameter_provenance(cfg: NVSpinConfig) -> dict:
    """Explicit provenance for every spin-layer parameter (forecast vs measured)."""
    mc = _model.measured_constants()
    measured = {
        "D_ZFS_rad_s": {
            "value": mc["D_ZFS_rad_s"], "unit": "rad/s",
            "status": mc["D_ZFS_status"], "source": mc["D_ZFS_provenance"],
            "measured_in_this_system": False,
        },
        "gamma_e_rad_s_T": {
            "value": mc["gamma_e_rad_s_T"], "unit": "rad/s/T",
            "status": mc["gamma_e_status"], "source": mc["gamma_e_provenance"],
            "measured_in_this_system": False,
        },
        "dD_dT_rad_s_K": {
            "value": mc["dD_dT_rad_s_K"], "unit": "rad/s/K",
            "status": mc["dD_dT_status"], "source": mc["dD_dT_provenance"],
            "measured_in_this_system": False,
        },
    }
    assumed = {
        "tau_c_s": {
            "value": cfg.tau_c_s, "unit": "s", "status": cfg.tau_c_status,
            "source": "QTA forecast knob; OU bath correlation time; NOT measured "
                      "on F-diamond. Distinct from T1, T2, T2*.",
            "measured_in_this_system": False, "sweepable": True,
        },
        "sigma_rad_s": {
            "value": cfg.sigma_rad_s, "unit": "rad/s", "status": "ASSUMED",
            "source": "Surface-noise amplitude (forecast).",
            "measured_in_this_system": False,
        },
        "T1_s": {
            "value": cfg.T1_s, "unit": "s", "status": "ASSUMED",
            "source": "Longitudinal relaxation (forecast input).",
            "measured_in_this_system": False,
        },
        "T2_pure_s": {
            "value": cfg.T2_pure_s, "unit": "s", "status": "ASSUMED",
            "source": "Markovian pure-dephasing time (forecast input).",
            "measured_in_this_system": False,
        },
        "E_strain_rad_s": {
            "value": cfg.E_strain_rad_s, "unit": "rad/s", "status": "ASSUMED",
            "source": "Transverse strain/E-field splitting (forecast).",
            "measured_in_this_system": False,
        },
        "B_T": {
            "value": cfg.B_T, "unit": "T", "status": "DESIGN_SPECIFIED",
            "source": "Bias field (design-specified, not metrology-verified).",
            "measured_in_this_system": False,
        },
        "T_K": {
            "value": cfg.T_K, "unit": "K", "status": "DESIGN_SPECIFIED",
            "source": "Mode D sample temperature target (10 mK base).",
            "measured_in_this_system": False,
        },
        "he_coverage_monolayers": {
            "value": cfg.he_coverage_monolayers, "unit": "monolayer",
            "status": "ASSUMED",
            "source": "He-3/He-4 surface coverage forecast hypothesis. "
                      "Isotope contrast is NOT measured; no fabricated contrast.",
            "measured_in_this_system": False,
        },
    }
    return {
        "_note": "NV spin-layer parameter provenance. Forecast/model-only. "
                 "tau_c is UNKNOWN and is never measured evidence.",
        "forecast_only": True,
        "measured_or_literature": measured,
        "assumed_or_unknown": assumed,
    }


def run_nv_spin_layer(
    *,
    mode_c_ready: bool,
    cfg: Optional[NVSpinConfig] = None,
    output_dir: Optional[Path] = None,
    write_outputs: bool = True,
) -> dict:
    """Run the NV spin layer for Mode D, enforcing Mode C readiness first.

    Parameters
    ----------
    mode_c_ready : bool
        Must be True. If False, raises :class:`ModeReadinessError` and performs
        no spin calculation (sensing cannot precede recovery readiness).
    cfg : NVSpinConfig, optional
        Defaults to the standard profile.
    output_dir : Path, optional
        Defaults to ``qta_multiphysics/outputs``.
    """
    if not mode_c_ready:
        raise ModeReadinessError(
            "Mode D NV spin dynamics requested before Mode C recovery readiness. "
            "Refusing: sensing must not run before isolation/purge/cryotrap/"
            "thermal recovery is ready."
        )

    cfg = cfg or standard_config()
    cfg.validate()

    if output_dir is None:
        output_dir = Path(__file__).resolve().parents[1] / "outputs"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Hamiltonian at Mode D temperature.
    H = _model.nv_ground_state_hamiltonian(
        B_T=cfg.B_T, theta_B_rad=cfg.theta_B_rad, T_K=cfg.T_K,
        E_strain_rad_s=cfg.E_strain_rad_s,
    )

    # Coherence curves for the three sequences.
    seq_times = {
        "ramsey": np.linspace(0.0, cfg.ramsey_max_s, cfg.n_time_points),
        "hahn": np.linspace(0.0, cfg.hahn_max_s, cfg.n_time_points),
        "xy8": np.linspace(0.0, cfg.xy8_max_s, cfg.n_time_points),
    }
    curves = {}
    observables = {}
    obs_name = {"ramsey": "T2star_s", "hahn": "T2_echo_s", "xy8": "T2_xy8_s"}
    for seq, times in seq_times.items():
        cur = _seq.coherence_curve(
            sequence=seq, tau_c_s=cfg.tau_c_s, sigma_rad_s=cfg.sigma_rad_s,
            times_s=times, n_time=cfg.n_time_inner, n_traj=cfg.n_traj,
            seed=cfg.seed,
        )
        curves[seq] = cur
        t_1e = _seq.one_over_e_time(cur["times_s"], cur["W_mc"])
        observables[obs_name[seq]] = t_1e

    # ODMR spectrum about the lower transition line.
    lines = _model.transition_frequencies_Hz(H)
    f0 = lines[0] if lines else _model.measured_constants()["D_ZFS_Hz"]
    freq_grid = np.linspace(f0 - cfg.odmr_span_Hz, f0 + cfg.odmr_span_Hz, cfg.odmr_points)
    odmr = _seq.odmr_spectrum(
        H=H, freq_grid_Hz=freq_grid, linewidth_Hz=cfg.odmr_linewidth_Hz,
    )

    # Convergence / consistency metric: max |W_mc - W_ff| across sequences.
    max_mc_ff = 0.0
    for seq in curves:
        d = float(np.max(np.abs(curves[seq]["W_mc"] - curves[seq]["W_ff"])))
        max_mc_ff = max(max_mc_ff, d)

    summary = {
        "_note": "NV spin-dynamics forecast summary. Model-only; no measured "
                 "coherence, isotope contrast, or validated result.",
        "forecast_only": True,
        "profile": cfg.profile,
        "surface_condition": cfg.surface_condition,
        "he_coverage_monolayers": cfg.he_coverage_monolayers,
        "distinct_quantities": {
            "tau_c_s": {"value": cfg.tau_c_s, "kind": "OU_bath_correlation_time",
                        "status": cfg.tau_c_status},
            "T1_s": {"value": cfg.T1_s, "kind": "longitudinal_relaxation",
                     "status": "ASSUMED"},
            "T2_pure_s": {"value": cfg.T2_pure_s, "kind": "markovian_pure_dephasing",
                          "status": "ASSUMED"},
        },
        "observables": {
            "T2star_s": {"value": observables.get("T2star_s"), "unit": "s",
                         "pulse_sequence": "ramsey",
                         "model_approximation": "fixed-seed OU colored noise, "
                         "phase-accumulation MC; filter-function cross-check",
                         "isotope_or_surface": cfg.surface_condition,
                         "forecast_only": True},
            "T2_echo_s": {"value": observables.get("T2_echo_s"), "unit": "s",
                          "pulse_sequence": "hahn_echo",
                          "model_approximation": "fixed-seed OU colored noise, "
                          "phase-accumulation MC; filter-function cross-check",
                          "isotope_or_surface": cfg.surface_condition,
                          "forecast_only": True},
            "T2_xy8_s": {"value": observables.get("T2_xy8_s"), "unit": "s",
                         "pulse_sequence": "xy8",
                         "model_approximation": "fixed-seed OU colored noise, "
                         "phase-accumulation MC; filter-function cross-check",
                         "isotope_or_surface": cfg.surface_condition,
                         "forecast_only": True},
        },
        "odmr_lines_Hz": lines,
        "numerical_convergence": {
            "n_traj": cfg.n_traj,
            "n_time_inner": cfg.n_time_inner,
            "max_abs_W_mc_minus_W_ff": max_mc_ff,
            "seed": cfg.seed,
        },
        "measured_parameters_used": [
            "D_ZFS (literature)", "gamma_e (NIST)", "dD/dT (literature)",
        ],
    }

    if write_outputs:
        # nv_coherence_curves.csv (long form)
        rows = []
        for seq in ("ramsey", "hahn", "xy8"):
            c = curves[seq]
            for i in range(len(c["times_s"])):
                rows.append([seq, c["times_s"][i], c["W_mc"][i], c["W_ff"][i]])
        _write_csv(output_dir / "nv_coherence_curves.csv",
                   ["sequence", "total_time_s", "coherence_mc", "coherence_filter_fn"],
                   rows)

        # nv_sequence_contrast.csv (per-sequence 1/e observable)
        srows = []
        for seq in ("ramsey", "hahn", "xy8"):
            srows.append([
                seq, obs_name[seq], observables[obs_name[seq]],
                cfg.tau_c_s, cfg.surface_condition, "FORECAST_ONLY",
            ])
        _write_csv(output_dir / "nv_sequence_contrast.csv",
                   ["sequence", "observable", "one_over_e_time_s", "tau_c_s_input",
                    "isotope_or_surface", "status"], srows)

        # nv_odmr_spectrum.csv
        orows = [[odmr["freq_Hz"][i], odmr["signal"][i]] for i in range(len(odmr["freq_Hz"]))]
        _write_csv(output_dir / "nv_odmr_spectrum.csv",
                   ["frequency_Hz", "odmr_signal"], orows)

        # summaries / provenance
        _write_json(output_dir / "nv_spin_dynamics_summary.json", summary)
        _write_json(output_dir / "nv_spin_parameter_provenance.json",
                    parameter_provenance(cfg))

    return {
        "summary": summary,
        "curves": curves,
        "odmr": odmr,
        "observables": observables,
        "hamiltonian": H,
    }
