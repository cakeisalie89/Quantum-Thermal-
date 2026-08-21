"""Layer-aware material regions for the 3D transient layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

The 3D thermal core uses the SAME homogeneous canonical diamond material model
(``material_models.diamond_k`` / ``diamond_cp``) as the 1D/2D backends -- no new
material physics is introduced. This module provides the layer-aware REGION MAP
(bulk diamond vs the NV sensing layer) supported by ``GeometryConfig``, for
probe extraction and reporting. Distinct per-region property sets are
NOT_IMPLEMENTED (no repository parameters exist for them) and are reported as
such rather than faked. Deterministic.
"""
from __future__ import annotations

import numpy as np

from .config import MultiphysicsConfig
from .mesh_3d import StructuredGrid3D
from .material_models import floor_report

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

REGIONS = ("DIAMOND_BULK", "NV_LAYER")


def region_map(cfg: MultiphysicsConfig, grid: StructuredGrid3D):
    """Integer region ids per cell: 0 = bulk diamond, 1 = NV sensing layer."""
    z_nv = cfg.geometry.nv_layer_depth_m
    ids = np.zeros((grid.nx, grid.ny, grid.nz), dtype=int)
    iz = int(np.argmin(np.abs(grid.zc - z_nv)))
    ids[:, :, iz] = 1
    return ids


def summary(cfg: MultiphysicsConfig, grid: StructuredGrid3D) -> dict:
    ids = region_map(cfg, grid)
    return {
        "regions": list(REGIONS),
        "nv_layer_depth_m": float(cfg.geometry.nv_layer_depth_m),
        "nv_layer_cell_count": int((ids == 1).sum()),
        "bulk_cell_count": int((ids == 0).sum()),
        "material_model": "homogeneous canonical diamond (diamond_k/diamond_cp), "
                          "identical to the 1D/2D backends",
        "per_region_property_sets": "NOT_IMPLEMENTED (no repository parameters)",
        # The numerical floors inside diamond_k/diamond_cp exceed the models
        # they guard throughout the sub-kelvin regime this machine operates in,
        # so below their crossover they are an effective material-property
        # assumption rather than regularization. Declared here so a consumer of
        # any sub-kelvin prediction sees it without reading material_models.py.
        "property_floor_declaration": floor_report(),
        "label": LABEL,
    }
