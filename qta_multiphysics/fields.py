"""Scalar fields on the QTA multiphysics grids, with finite checks,
gradient helpers, and CSV/slice export.

MODEL-ONLY infrastructure.
"""
from __future__ import annotations
import csv
import numpy as np
from .grids import Grid1D, AxisymmetricGrid2D


class Field1D:
    """Scalar field defined on a Grid1D."""

    def __init__(self, grid: Grid1D, values=None, name: str = "field", unit: str = ""):
        self.grid = grid
        self.name = name
        self.unit = unit
        if values is None:
            self.values = np.zeros(grid.n, dtype=float)
        else:
            v = np.asarray(values, dtype=float)
            if v.shape != (grid.n,):
                raise ValueError(f"Field1D values shape {v.shape} != ({grid.n},)")
            self.values = v

    def min(self):
        return float(np.min(self.values))

    def max(self):
        return float(np.max(self.values))

    def gradient(self):
        """Central-difference gradient w.r.t. the grid coordinate [unit/m]."""
        return np.gradient(self.values, self.grid.centers)

    def max_abs_gradient(self):
        return float(np.max(np.abs(self.gradient())))

    def is_finite(self):
        return bool(np.all(np.isfinite(self.values)))

    def to_csv(self, path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([f"{self.grid.axis}_m", f"{self.name}_{self.unit}" if self.unit else self.name])
            for x, v in zip(self.grid.centers, self.values):
                w.writerow([f"{x:.9e}", f"{v:.9e}"])


class Field2D:
    """Scalar field defined on an AxisymmetricGrid2D, shape (nr, nz)."""

    def __init__(self, grid: AxisymmetricGrid2D, values=None, name: str = "field", unit: str = ""):
        self.grid = grid
        self.name = name
        self.unit = unit
        if values is None:
            self.values = np.zeros(grid.shape, dtype=float)
        else:
            v = np.asarray(values, dtype=float)
            if v.shape != grid.shape:
                raise ValueError(f"Field2D values shape {v.shape} != {grid.shape}")
            self.values = v

    def min(self):
        return float(np.min(self.values))

    def max(self):
        return float(np.max(self.values))

    def radial_gradient(self):
        """d/dr along axis 0 [unit/m]."""
        return np.gradient(self.values, self.grid.r_centers, axis=0)

    def depth_gradient(self):
        """d/dz along axis 1 [unit/m]."""
        return np.gradient(self.values, self.grid.z_centers, axis=1)

    def max_abs_radial_gradient(self):
        return float(np.max(np.abs(self.radial_gradient())))

    def max_abs_depth_gradient(self):
        return float(np.max(np.abs(self.depth_gradient())))

    def is_finite(self):
        return bool(np.all(np.isfinite(self.values)))

    def argmax_rz(self):
        """Return (r, z) physical coordinates of the field maximum."""
        i, j = np.unravel_index(int(np.argmax(self.values)), self.values.shape)
        return float(self.grid.r_centers[i]), float(self.grid.z_centers[j])

    def to_slice_csv(self, path, which="both"):
        """Export axis slices: r=0 column (depth profile) and z=0 row (radial profile)."""
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["slice", "coord_m", f"{self.name}_{self.unit}" if self.unit else self.name])
            if which in ("both", "depth"):
                for z, v in zip(self.grid.z_centers, self.values[0, :]):
                    w.writerow(["axis_r0_depth", f"{z:.9e}", f"{v:.9e}"])
            if which in ("both", "radial"):
                for r, v in zip(self.grid.r_centers, self.values[:, 0]):
                    w.writerow(["surface_z0_radial", f"{r:.9e}", f"{v:.9e}"])

    def to_map_csv(self, path, stride_r=1, stride_z=1):
        """Export the full (r, z) map (optionally strided to keep files small)."""
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["r_m", "z_m", f"{self.name}_{self.unit}" if self.unit else self.name])
            for i in range(0, self.grid.nr, stride_r):
                for j in range(0, self.grid.nz, stride_z):
                    w.writerow([f"{self.grid.r_centers[i]:.6e}",
                                f"{self.grid.z_centers[j]:.6e}",
                                f"{self.values[i, j]:.6e}"])
