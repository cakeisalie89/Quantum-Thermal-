"""Bounded auxiliary source channels for the coupled 3D layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Every channel here REUSES an existing canonical chain value and distributes it
as a clearly labelled bounded approximation -- no new physics, no invented
numbers:

* Microwave / ESR drive (Mode D only): the canonical delivery-line model's
  ``dissipated_power_NV_region_W`` deposited UNIFORMLY over the NV-layer
  region cells. REDUCED_ORDER (documented uniform-distribution approximation;
  a 3D field/SAR map remains NOT_IMPLEMENTED).
* Radiative front-face load: the canonical stage-chain result
  ``P_rad_to_10mK_W`` for the mode's shutter/baffle state, applied as a
  uniform front-face flux. REDUCED_ORDER (stage-chain, shutter/baffle-state
  coupled; 3D view factors remain NOT_IMPLEMENTED).
* Vibration heating: the canonical transfer-chain ``total_dissipated_power_W``
  as a BOUNDED_FORECAST scalar in the energy ledger only (no invented spatial
  distribution; 3D modal analysis remains NOT_IMPLEMENTED).
* Collection-path optical heat load: NOT_IMPLEMENTED (no repository
  parameterization exists); the implemented optical channel is the laser
  deposition itself (exactly-conservative, in the thermal core).

Deterministic: no randomness anywhere in this module.
"""
from __future__ import annotations

import numpy as np

from .config import MultiphysicsConfig
from .mesh_3d import StructuredGrid3D
from .materials_3d import region_map
from .microwave_heating_1d import microwave_path_1d
from .radiation_paths import radiation_paths
from .vibration_transfer import vibration_transfer
from .state_machine_3d import DeviceState3D

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"


def microwave_nv_region_map_W(cfg: MultiphysicsConfig, grid: StructuredGrid3D,
                              state: DeviceState3D):
    """Per-cell microwave power [W] (nx,ny,nz): canonical NV-region dissipation
    uniformly over the NV-layer cells when the drive is active; zero otherwise.
    REDUCED_ORDER."""
    Q = np.zeros((grid.nx, grid.ny, grid.nz))
    meta = {"channel": "microwave_esr_drive",
            "status": "REDUCED_ORDER",
            "approximation": "canonical line-model NV-region dissipation "
                             "distributed uniformly over NV-layer cells; "
                             "3D field/SAR map NOT_IMPLEMENTED",
            "active": bool(state.microwave_active),
            "total_W": 0.0, "label": LABEL}
    if not state.microwave_active:
        return Q, meta
    _, m = microwave_path_1d()
    P = float(m["dissipated_power_NV_region_W"])
    ids = region_map(cfg, grid)
    nv = ids == 1
    Q[nv] = P / int(nv.sum())
    meta["total_W"] = P
    return Q, meta


def radiative_front_flux_W_m2(grid: StructuredGrid3D, state: DeviceState3D):
    """Uniform front-face radiative flux [W/m^2] from the canonical stage
    chain evaluated at the mode's shutter/baffle state. REDUCED_ORDER."""
    _, m = radiation_paths(shutter_state=state.shutter_state,
                           baffle_state=state.baffle_state)
    P = float(m["total_heat_load_to_10mK_W"])  # fail loud on schema drift
    A_front = float(np.sum(grid.area_z))
    meta = {"channel": "radiative_front_face_load",
            "status": "REDUCED_ORDER",
            "approximation": "canonical stage-chain P_rad_to_10mK for the "
                             "mode's shutter/baffle state applied as a uniform "
                             "front-face flux; 3D view factors NOT_IMPLEMENTED",
            "shutter_state": state.shutter_state,
            "baffle_state": state.baffle_state,
            "total_W": P, "front_area_m2": A_front, "label": LABEL}
    return P / A_front, meta


def vibration_bound_W(state: DeviceState3D):
    """Canonical transfer-chain dissipated power as a ledger-only scalar bound
    [W]. BOUNDED_FORECAST (no spatial distribution is invented)."""
    _, m = vibration_transfer()
    return float(m["total_dissipated_power_W"]), {
        "channel": "vibration_heating",
        "status": "BOUNDED_FORECAST",
        "approximation": "canonical transfer-chain total dissipated power as "
                         "an energy-ledger scalar bound; no spatial map; 3D "
                         "modal analysis NOT_IMPLEMENTED",
        "mode": state.mode, "label": LABEL}


def optical_collection_load():
    """Collection-path optical heat load: NOT_IMPLEMENTED (honest stub)."""
    return {"channel": "optical_collection_path_load",
            "status": "NOT_IMPLEMENTED",
            "reason": "no repository parameterization of collection-path "
                      "absorption exists; the implemented optical channel is "
                      "the exactly-conservative laser deposition in the "
                      "thermal core", "label": LABEL}


def laser_deposition_summary(cfg: MultiphysicsConfig, grid: StructuredGrid3D,
                             laser, state: DeviceState3D) -> dict:
    """Deterministic deposition summary for the active laser channel."""
    frac = laser.cell_fractions(grid,
                                front_position_m=cfg.geometry.front_position_m)
    ids = region_map(cfg, grid)
    P = float(np.asarray(laser.base.temporal_power_W(0.0)))
    tot = float(frac.sum())
    return {"channel": "laser_deposition", "status": "IMPLEMENTED",
            "active": bool(state.laser_active),
            "temporal_mode": laser.mode,
            "temporal_note": "averaged mode = time-averaged deposition "
                             "approximation of the fs pulse train (stated "
                             "approximation); pulse-envelope mode available",
            "captured_fraction_in_box": tot,
            "captured_power_W_at_t0": P * tot if state.laser_active else 0.0,
            "fraction_in_nv_layer": float(frac[ids == 1].sum()),
            "fraction_in_bulk": float(frac[ids == 0].sum()),
            "conservation": "cell-integrated (erf x/y, Beer-Lambert z); "
                            "discrete sum equals analytic captured fraction "
                            "to machine precision",
            "ultrafast_note": "no electron-phonon nonequilibrium model is "
                              "implemented or claimed",
            "label": LABEL}
