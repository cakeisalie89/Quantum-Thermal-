"""Finite-volume grids for the QTA non-lumped multiphysics layer.

Grid1D: uniform cell-centered 1D grid (coordinate z or x).
AxisymmetricGrid2D: uniform (r,z) grid with 2*pi*r cell-volume weighting,
symmetry axis at r=0.

MODEL-ONLY infrastructure.
"""
from __future__ import annotations
import numpy as np
from .units import require_positive


class Grid1D:
    """Uniform cell-centered 1D finite-volume grid on [0, length]."""

    def __init__(self, length: float, n_cells: int, axis: str = "z"):
        self.length = require_positive("Grid1D.length", length)
        if n_cells < 3:
            raise ValueError("Grid1D requires n_cells >= 3")
        self.n = int(n_cells)
        self.axis = axis
        self.dx = self.length / self.n
        # Cell centers and faces.
        self.faces = np.linspace(0.0, self.length, self.n + 1)
        self.centers = 0.5 * (self.faces[:-1] + self.faces[1:])
        # Per-cell volume per unit cross-sectional area = dx (1D slab).
        self.cell_volume = np.full(self.n, self.dx)
        self.boundary_labels = {"low": f"{axis}=0", "high": f"{axis}=L"}

    def __repr__(self):
        return f"Grid1D(length={self.length:.3e}, n={self.n}, dx={self.dx:.3e}, axis={self.axis})"


class AxisymmetricGrid2D:
    """Uniform (r,z) cell-centered grid with cylindrical 2*pi*r weighting.

    r in [0, R] (radial), z in [0, L] (depth). The symmetry axis is at r=0.
    Cell (i, j) has center (r_i, z_j); its physical volume is
    2*pi*r_i*dr*dz (annular ring volume).
    """

    def __init__(self, radius: float, length: float, n_r: int, n_z: int):
        self.radius = require_positive("AxisymmetricGrid2D.radius", radius)
        self.length = require_positive("AxisymmetricGrid2D.length", length)
        if n_r < 3 or n_z < 3:
            raise ValueError("AxisymmetricGrid2D requires n_r, n_z >= 3")
        self.nr = int(n_r)
        self.nz = int(n_z)
        self.dr = self.radius / self.nr
        self.dz = self.length / self.nz

        self.r_faces = np.linspace(0.0, self.radius, self.nr + 1)
        self.z_faces = np.linspace(0.0, self.length, self.nz + 1)
        self.r_centers = 0.5 * (self.r_faces[:-1] + self.r_faces[1:])
        self.z_centers = 0.5 * (self.z_faces[:-1] + self.z_faces[1:])

        # 2D mesh of centers, shape (nr, nz).
        self.R, self.Z = np.meshgrid(self.r_centers, self.z_centers, indexing="ij")
        # Annular cell volume 2*pi*r*dr*dz, shape (nr, nz).
        self.cell_volume = (2.0 * np.pi * self.R * self.dr * self.dz)
        # Face areas for radial faces: 2*pi*r_face*dz (one per radial face row).
        self.r_face_area = 2.0 * np.pi * self.r_faces[:, None] * self.dz  # (nr+1, 1)
        # Face areas for axial faces: 2*pi*r_center*dr (annulus area).
        self.z_face_area = (2.0 * np.pi * self.r_centers * self.dr)[:, None]  # (nr, 1)

        self.boundary_labels = {
            "axis": "r=0 (symmetry)",
            "outer": "r=R",
            "source": "z=0 (source/front side)",
            "sink": "z=L (sink/backside)",
        }

    @property
    def shape(self):
        return (self.nr, self.nz)

    def __repr__(self):
        return (f"AxisymmetricGrid2D(R={self.radius:.3e}, L={self.length:.3e}, "
                f"nr={self.nr}, nz={self.nz}, dr={self.dr:.3e}, dz={self.dz:.3e})")
