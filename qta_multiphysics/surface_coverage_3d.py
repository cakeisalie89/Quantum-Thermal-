"""He-3 / He-4 surface-coverage forecast over the 3D NV plane (Mode D).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Reuses the canonical adsorption/desorption coverage model
(``surface_coverage``: the same CoverageSpec set and kinetic-flux law used by
the 1D/2D layers) per cell of the 3D NV-depth plane, evaluated at the Mode-D
entry temperature field from the 3D mode sequence. The helium gas density is
the canonical dose condition ``n = P_dose / (k_B * T_fridge)`` with
``P_dose = 1e-6 Pa`` (the existing canonical value). The C-13 methane
precursor is structurally OFF in Mode D (zero flux; residual clearance is a
Mode C responsibility), and H2 appears only as residual background exactly as
in the 1D layer. The forecast window mirrors the 1D layer's canonical
coverage window (t_end = 1.0 s). Deterministic.
"""
from __future__ import annotations

import numpy as np

from .config import MultiphysicsConfig
from .surface_coverage import (default_coverage_specs, kinetic_flux,
                               evolve_coverage)
from .thermal_3d_transient import Thermal3DResult

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

P_HE_DOSE_PA = 1.0e-6          # canonical dose pressure (existing value, D9)
K_B = 1.380649e-23
T_DOSE_WINDOW_S = 1.0          # mirrors the canonical 1D coverage window


def nv_plane_coverage_rows(cfg: MultiphysicsConfig, tD_entry: Thermal3DResult,
                           n_probe_cells: int = 16):
    """Coverage forecast theta(He3/He4) on the NV plane at Mode-D entry.

    Evaluates the canonical coverage ODE on a deterministic subsample of the
    NV-depth plane (the ``n_probe_cells`` hottest cells plus the beam-axis
    cell), which brackets the coverage range on the plane at reduced CI cost.
    """
    g = tD_entry.grid
    iz = tD_entry.probe_index[2]
    plane = tD_entry.T_xyz(-1)[:, :, iz]
    T_gas = cfg.fridge.T_fridge_K
    n_he = P_HE_DOSE_PA / (K_B * T_gas)
    specs = default_coverage_specs()
    by = {s.name: s for s in specs}
    fluxes0 = {s.name: 0.0 for s in specs}
    for he in ("He3", "He4"):
        fluxes0[he] = kinetic_flux(n_he, T_gas, by[he].mass_amu)

    flat = plane.ravel()
    order = np.argsort(flat)[::-1]
    ix0, iy0 = tD_entry.probe_index[0], tD_entry.probe_index[1]
    axis_flat = ix0 * plane.shape[1] + iy0
    sel = list(dict.fromkeys(list(order[:n_probe_cells]) + [axis_flat]))

    rows = []
    for f in sel:
        ix, iy = np.unravel_index(f, plane.shape)
        Ts = float(plane[ix, iy])
        out, _ = evolve_coverage(specs, dict(fluxes0), Ts, T_DOSE_WINDOW_S,
                                 n_eval=8)
        rows.append({
            "x_m": f"{g.xc[ix]:.6e}", "y_m": f"{g.yc[iy]:.6e}",
            "z_m": f"{g.zc[iz]:.6e}", "T_surface_K": f"{Ts:.9e}",
            "theta_He3": f"{out['He3'][-1]:.9e}",
            "theta_He4": f"{out['He4'][-1]:.9e}",
            "theta_C13_CH4_live": "0 (Mode B precursor structurally OFF in Mode D)",
            "H2_role": "residual_background_only",
            "dose_pressure_Pa": f"{P_HE_DOSE_PA:.3e}",
            "window_s": f"{T_DOSE_WINDOW_S:.3e}",
            "label": LABEL,
        })
    return rows
