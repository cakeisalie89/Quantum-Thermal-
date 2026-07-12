"""One-at-a-time model-sensitivity ranking for the 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

MODEL sensitivity only (never experimental importance): deterministic
one-at-a-time +10% perturbations of four uncertain model inputs, ranked by the
normalized response of the Mode-B NV-probe temperature rise on the reduced CI
mesh. Sensitivities are properties of THIS model under THESE assumptions.
Deterministic; no randomness.
"""
from __future__ import annotations

import copy

from .config import MultiphysicsConfig, default_config
from .mesh_3d import Grid3DConfig
from .thermal_3d_transient import solve_thermal_3d

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

CI = Grid3DConfig(nx=10, ny=10, nz=12)
STEP = 0.10   # +10% one-sided


def _rise(cfg):
    r = solve_thermal_3d(cfg, CI, n_eval=9)
    return float(r.probe_timeseries_K()[-1]) - cfg.fridge.T_fridge_K


def sensitivity_rows(cfg: MultiphysicsConfig | None = None) -> list:
    cfg = cfg or default_config()
    base = _rise(cfg)

    def perturbed(mutator, name, provenance):
        c = copy.deepcopy(cfg)
        mutator(c)
        r = _rise(c)
        S = ((r - base) / base) / STEP if base != 0 else 0.0
        return {"parameter": name, "perturbation": f"+{STEP:.0%}",
                "base_probe_rise_K": f"{base:.9e}",
                "perturbed_probe_rise_K": f"{r:.9e}",
                "normalized_sensitivity": f"{S:.6e}",
                "abs_normalized_sensitivity": abs(S),
                "parameter_provenance": provenance,
                "meaning": "model sensitivity of THIS forecast model; not "
                           "experimental importance",
                "label": LABEL}

    rows = [
        perturbed(lambda c: setattr(c.laser, "absorbed_fraction",
                                    c.laser.absorbed_fraction * (1 + STEP)),
                  "laser.absorbed_fraction", "ASSUMED"),
        perturbed(lambda c: setattr(c.laser, "spot_radius_m",
                                    c.laser.spot_radius_m * (1 + STEP)),
                  "laser.spot_radius_m", "DESIGN"),
        perturbed(lambda c: setattr(c.laser, "absorption_coeff_1_m",
                                    c.laser.absorption_coeff_1_m * (1 + STEP)),
                  "laser.absorption_coeff_1_m", "LITERATURE_BOUND/ASSUMED"),
        perturbed(lambda c: setattr(c.fridge, "kapitza_coeff_W_m2_K4",
                                    c.fridge.kapitza_coeff_W_m2_K4 * (1 + STEP)),
                  "fridge.kapitza_coeff_W_m2_K4", "ASSUMED"),
    ]
    rows.sort(key=lambda r: -r["abs_normalized_sensitivity"])
    for k, r in enumerate(rows, 1):
        r["rank"] = str(k)
        r["abs_normalized_sensitivity"] = f"{r['abs_normalized_sensitivity']:.6e}"
    return rows
