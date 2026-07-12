"""Structured Cartesian 3D finite-volume mesh for the additive 3D transient layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

This mirrors the repository's 1D/2D mesh style (``grids.py``): genuinely graded,
nonuniform, face/volume-driven finite-volume meshes built from ``graded_faces``.
The x and y axes span a lateral box centred on the beam axis (half-extent taken
from ``GeometryConfig.thermal_radius_m`` by default, matching the 2D radial
domain); the z axis spans the resolved near-surface depth
(``GeometryConfig.thermal_depth_m``), refined toward the irradiated surface as
in the 1D backend. Reduced CI meshes are deliberately small (default 10x10x12);
full-resolution meshes are heavy/opt-in only and never required by CI or the
package consistency check.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import MultiphysicsConfig, require_positive
from .grids import graded_faces, thermal_depth_refinement, thermal_radial_refinement


@dataclass
class Grid3DConfig:
    """Reduced 3D mesh configuration (deterministic; validated like repo configs)."""
    nx: int = 10
    ny: int = 10
    nz: int = 12
    half_extent_xy_m: float | None = None   # default: GeometryConfig.thermal_radius_m
    depth_m: float | None = None             # default: GeometryConfig.thermal_depth_m
    max_ratio: float = 6.0                    # grading limit, as grids.graded_faces

    def validate(self):
        for name, v in (("nx", self.nx), ("ny", self.ny), ("nz", self.nz)):
            if int(v) != v or v < 4:
                raise ValueError(f"Grid3DConfig.{name} must be an integer >= 4 (got {v})")
        if self.half_extent_xy_m is not None:
            require_positive("half_extent_xy_m", self.half_extent_xy_m)
        if self.depth_m is not None:
            require_positive("depth_m", self.depth_m)
        require_positive("max_ratio", self.max_ratio)
        return self


class StructuredGrid3D:
    """Cell-centred structured (x, y, z) finite-volume grid with graded spacing.

    Faces/centres/half-distances per axis; exact cell volumes and face areas.
    x, y are graded toward the beam axis (domain centre); z toward the surface
    (z = 0, the irradiated front face). The back face z = depth couples to the
    Kapitza-radiative fridge sink, exactly as in the 1D/2D backends.
    """

    def __init__(self, cfg: MultiphysicsConfig, g3: Grid3DConfig | None = None):
        cfg.validate()
        self.g3 = (g3 or Grid3DConfig()).validate()
        geo = cfg.geometry
        R = self.g3.half_extent_xy_m if self.g3.half_extent_xy_m is not None \
            else geo.thermal_radius_m
        L = self.g3.depth_m if self.g3.depth_m is not None else geo.thermal_depth_m
        self.half_extent_xy_m = float(R)
        self.depth_m = float(L)

        # graded faces from the SHARED refinement-spec builders (grids.py), so the
        # z mesh uses the same attractor family as the 1D/2D depth meshes and the
        # lateral meshes use the 2D radial attractors mirrored about the beam axis.
        absorption_depth = 1.0 / cfg.laser.absorption_coeff_1_m
        z_attr = thermal_depth_refinement(L, absorption_depth,
                                          geo.nv_layer_depth_m,
                                          geo.front_position_m)
        rad = thermal_radial_refinement(R, cfg.laser.spot_radius_m)
        # mirror the radial attractors about the domain centre (beam axis at R on
        # the [0, 2R] build axis), keeping the cold-contact attractors at both ends
        lat_attr = [(R + loc, s, w) for (loc, s, w) in rad] \
                 + [(R - loc, s, w) for (loc, s, w) in rad if loc > 0.0]
        self.x_faces = np.asarray(
            graded_faces(2.0 * R, self.g3.nx, attractors=lat_attr,
                         max_ratio=self.g3.max_ratio), dtype=float) - R
        self.y_faces = np.asarray(
            graded_faces(2.0 * R, self.g3.ny, attractors=lat_attr,
                         max_ratio=self.g3.max_ratio), dtype=float) - R
        self.z_faces = np.asarray(
            graded_faces(L, self.g3.nz, attractors=z_attr,
                         max_ratio=self.g3.max_ratio), dtype=float)

        def _centres(f):
            return 0.5 * (f[:-1] + f[1:])

        self.xc, self.yc, self.zc = (_centres(self.x_faces),
                                     _centres(self.y_faces),
                                     _centres(self.z_faces))
        self.dx = np.diff(self.x_faces)
        self.dy = np.diff(self.y_faces)
        self.dz = np.diff(self.z_faces)
        self.nx, self.ny, self.nz = len(self.xc), len(self.yc), len(self.zc)
        self.n_cells = self.nx * self.ny * self.nz

        # centre -> interior-face half distances per axis (for series resistance)
        self.dRx = self.x_faces[1:-1] - self.xc[:-1]   # left cell centre -> face
        self.dLx = self.xc[1:] - self.x_faces[1:-1]    # face -> right cell centre
        self.dRy = self.y_faces[1:-1] - self.yc[:-1]
        self.dLy = self.yc[1:] - self.y_faces[1:-1]
        self.dRz = self.z_faces[1:-1] - self.zc[:-1]
        self.dLz = self.zc[1:] - self.z_faces[1:-1]

        # exact cell volumes and axis-normal face areas
        self.volumes = (self.dx[:, None, None] * self.dy[None, :, None]
                        * self.dz[None, None, :])                       # (nx,ny,nz)
        self.area_x = self.dy[:, None] * self.dz[None, :]               # (ny,nz)
        self.area_y = self.dx[:, None] * self.dz[None, :]               # (nx,nz)
        self.area_z = self.dx[:, None] * self.dy[None, :]               # (nx,ny)
        self.total_volume_m3 = float(self.volumes.sum())

    # ---- helpers ----
    def meshgrid_centres(self):
        return np.meshgrid(self.xc, self.yc, self.zc, indexing="ij")

    def index_nearest(self, x: float, y: float, z: float):
        return (int(np.argmin(np.abs(self.xc - x))),
                int(np.argmin(np.abs(self.yc - y))),
                int(np.argmin(np.abs(self.zc - z))))

    def summary(self) -> dict:
        return {
            "label": "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM",
            "kind": "structured_cartesian_3d_finite_volume",
            "nx": self.nx, "ny": self.ny, "nz": self.nz,
            "n_cells": self.n_cells,
            "half_extent_xy_m": self.half_extent_xy_m,
            "depth_m": self.depth_m,
            "dx_min_m": float(self.dx.min()), "dx_max_m": float(self.dx.max()),
            "dy_min_m": float(self.dy.min()), "dy_max_m": float(self.dy.max()),
            "dz_min_m": float(self.dz.min()), "dz_max_m": float(self.dz.max()),
            "grading_ratio_x": float(self.dx.max() / self.dx.min()),
            "grading_ratio_y": float(self.dy.max() / self.dy.min()),
            "grading_ratio_z": float(self.dz.max() / self.dz.min()),
            "total_volume_m3": self.total_volume_m3,
            "graded": True,
            "note": "Reduced deterministic CI mesh; heavy/full-resolution meshes "
                    "are opt-in only and never required by CI or the checker.",
        }
