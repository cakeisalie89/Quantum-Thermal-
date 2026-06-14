"""Finite-volume meshes for the QTA non-lumped multiphysics layer.

This module provides genuine face/volume-driven finite-volume meshes:

  Grid1D            : 1D cell-centred mesh on [0, length]. Uniform by default;
                      nonuniform (graded/refined) via Grid1D.graded(...).
  AxisymmetricGrid2D: cell-centred (r, z) mesh with cylindrical weighting and a
                      symmetry axis at r=0. Uniform by default; nonuniform via
                      AxisymmetricGrid2D.graded(...). Annular cell volumes and
                      face areas are computed EXACTLY from the face radii
                      (pi (r_out^2 - r_in^2) ...), not midpoint-approximated.

Both grids expose, for every cell: faces, centres, per-cell widths, the
distances from each centre to its bounding faces, centre-to-centre face
distances, control volumes, face areas, and explicit boundary-face metadata.
The transient solvers consume these arrays directly, so refinement actually
changes the discrete operators (it is not unused decoration).

Graded meshes are built by the equidistribution (moving-mesh) principle: a
mesh-density function rho(x) is raised near each refinement target, and faces
are placed so that the integral of rho between consecutive faces is constant.
This produces smoothly graded, strictly monotone faces clustered where rho is
large. The largest/smallest cell ratio is capped (max_ratio) so the implicit
stiff integrator stays tractable.

MODEL-ONLY infrastructure. It does not represent measured data.
"""
from __future__ import annotations
import numpy as np
from .units import require_positive


# --------------------------------------------------------------------------- #
#  Graded-face generator (equidistribution / moving mesh)
# --------------------------------------------------------------------------- #
def graded_faces(length, n_cells, attractors=None, max_ratio=6.0,
                 base_density=1.0, n_ref=8001):
    """Return ``n_cells + 1`` strictly increasing face coordinates on
    ``[0, length]``, clustered near ``attractors``.

    attractors: iterable of (location_m, strength, width_m). Each contributes
        ``strength * exp(-((x - location)/width)^2)`` to the mesh-density
        function. Empty/None -> a uniform mesh (identical to ``linspace``).
    max_ratio: cap on the (largest cell width)/(smallest cell width). The
        density is clipped to ``base_density * max_ratio`` so refinement cannot
        produce arbitrarily small cells (which would make the stiff ODE
        intractable). Must be >= 1.
    n_ref: number of points in the dense reference grid used to integrate the
        density. Chosen large enough to resolve narrow refinement widths.

    The endpoints are pinned exactly to 0 and ``length``.
    """
    length = float(length)
    n = int(n_cells)
    if n < 3:
        raise ValueError("graded_faces requires n_cells >= 3")
    if max_ratio < 1.0:
        raise ValueError("max_ratio must be >= 1")
    if not attractors:
        return np.linspace(0.0, length, n + 1)

    xs = np.linspace(0.0, length, int(n_ref))
    rho = np.full_like(xs, float(base_density))
    for loc, strength, width in attractors:
        if strength <= 0.0 or width <= 0.0:
            continue
        loc = min(max(float(loc), 0.0), length)
        rho += float(strength) * np.exp(-((xs - loc) / float(width)) ** 2)
    # Cap the density so the smallest cell is at most max_ratio x finer than the
    # coarsest (cell width ~ 1/rho; base_density is the floor).
    rho = np.minimum(rho, base_density * max_ratio)

    # Cumulative integral C(x) (monotone increasing because rho > 0).
    cdf = np.concatenate(([0.0], np.cumsum(0.5 * (rho[1:] + rho[:-1]) * np.diff(xs))))
    targets = np.linspace(0.0, cdf[-1], n + 1)
    faces = np.interp(targets, cdf, xs)
    faces[0] = 0.0
    faces[-1] = length
    # Guard against any pathological non-monotonicity from interpolation.
    faces = np.maximum.accumulate(faces)
    if np.any(np.diff(faces) <= 0.0):
        # Degenerate request; fall back to a uniform mesh rather than emit a
        # zero-width cell.
        return np.linspace(0.0, length, n + 1)
    return faces


# --------------------------------------------------------------------------- #
#  Refinement-spec builders (pure geometry -> attractor lists). Shared by the
#  1D and 2D thermal solvers so that, given the same configuration, the depth
#  meshes are IDENTICAL (this is what makes the 2D->1D reduction check exact).
# --------------------------------------------------------------------------- #
def thermal_depth_refinement(thermal_depth_m, absorption_depth_m,
                             nv_layer_depth_m, front_position_m=0.0):
    """Attractors for the depth (z) axis of the thermal solvers: surface,
    laser absorption depth, NV layer, process/recovery front, and the cold
    thermal-contact boundary at z = L."""
    L = float(thermal_depth_m)
    a = float(absorption_depth_m)
    nv = float(nv_layer_depth_m)
    fr = float(front_position_m)
    surf_w = max(a, 0.02 * L)
    nv_w = max(2.0 * nv, 0.05 * a, 1.0e-9)
    return [
        (0.0, 6.0, surf_w),            # front surface (laser entry / strongest gradients)
        (a, 3.0, a),                   # 1/e laser absorption depth
        (nv, 3.0, nv_w),               # NV layer
        (fr, 2.0, surf_w),             # process / recovery front (s0)
        (L, 2.5, 0.1 * L),             # cold thermal contact (z = L, Kapitza side)
    ]


def thermal_radial_refinement(thermal_radius_m, spot_radius_m):
    """Attractors for the radial (r) axis of the 2D thermal solver: beam axis
    (r=0, where the Gaussian source peaks), the beam waist / aperture-exposure
    region, and the cold radial contact at r = R."""
    R = float(thermal_radius_m)
    w0 = float(spot_radius_m)
    return [
        (0.0, 5.0, 0.5 * w0),          # beam axis (source peak, symmetry axis)
        (w0, 4.0, w0),                 # beam waist / exposure-edge region
        (R, 2.5, 0.1 * R),             # cold radial contact (r = R)
    ]


# --------------------------------------------------------------------------- #
#  1D mesh
# --------------------------------------------------------------------------- #
class Grid1D:
    """1D cell-centred finite-volume mesh on ``[0, length]``.

    Default construction is uniform (so callers that rely on a scalar ``dx``
    keep working). Pass ``faces`` explicitly, or use :meth:`graded`, for a
    nonuniform mesh. Control volumes are per unit cross-sectional area (slab),
    so ``cell_volume[i] == cell_widths[i]``.
    """

    def __init__(self, length, n_cells, axis="z", faces=None):
        self.length = require_positive("Grid1D.length", length)
        if n_cells < 3:
            raise ValueError("Grid1D requires n_cells >= 3")
        self.n = int(n_cells)
        self.axis = axis

        if faces is None:
            faces = np.linspace(0.0, self.length, self.n + 1)
            self.uniform = True
        else:
            faces = np.asarray(faces, dtype=float)
            if faces.shape != (self.n + 1,):
                raise ValueError(f"faces shape {faces.shape} != ({self.n + 1},)")
            if np.any(np.diff(faces) <= 0.0):
                raise ValueError("faces must be strictly increasing")
            self.uniform = bool(np.allclose(np.diff(faces), faces[1] - faces[0]))

        self.faces = faces
        self.centers = 0.5 * (faces[:-1] + faces[1:])
        self.cell_widths = np.diff(faces)                 # (n,)
        self.widths = self.cell_widths                    # alias
        # centre-to-centre distances at the (n-1) interior faces
        self.face_distance = np.diff(self.centers)        # (n-1,)
        # distance from each cell centre to its two bounding faces
        self.dist_to_left_face = self.centers - faces[:-1]    # (n,)
        self.dist_to_right_face = faces[1:] - self.centers    # (n,)
        # unit-cross-section control volume = cell width
        self.cell_volume = self.cell_widths.copy()        # (n,)
        # nominal spacing (exact for a uniform mesh; informational otherwise)
        self.dx = self.length / self.n

        self.boundary_faces = {
            "low":  {"index": 0, "coord": float(faces[0]), "label": f"{axis}=0",
                     "area": 1.0, "outward_normal": -1, "kind": "surface/front"},
            "high": {"index": self.n, "coord": float(faces[-1]), "label": f"{axis}=L",
                     "area": 1.0, "outward_normal": +1, "kind": "thermal_contact"},
        }
        self.boundary_labels = {"low": f"{axis}=0", "high": f"{axis}=L"}

    @classmethod
    def graded(cls, length, n_cells, refine_at=None, axis="z", max_ratio=6.0):
        faces = graded_faces(length, n_cells, refine_at, max_ratio=max_ratio)
        return cls(length, n_cells, axis=axis, faces=faces)

    @property
    def min_width(self):
        return float(self.cell_widths.min())

    @property
    def max_width(self):
        return float(self.cell_widths.max())

    def __repr__(self):
        kind = "uniform" if self.uniform else "graded"
        return (f"Grid1D({kind}, length={self.length:.3e}, n={self.n}, "
                f"min_dx={self.min_width:.3e}, max_dx={self.max_width:.3e}, axis={self.axis})")


# --------------------------------------------------------------------------- #
#  2D axisymmetric mesh
# --------------------------------------------------------------------------- #
class AxisymmetricGrid2D:
    """Cell-centred ``(r, z)`` mesh with cylindrical weighting.

    ``r in [0, radius]`` (radial), ``z in [0, length]`` (depth); the symmetry
    axis is at ``r = 0``. Uniform by default; pass ``r_faces`` / ``z_faces`` or
    use :meth:`graded` for a nonuniform mesh.

    Exact metrics (from the face radii, not midpoint approximations):
      ring_area[i]      = pi (r_face[i+1]^2 - r_face[i]^2)     [m^2]
      cell_volume[i,j]  = ring_area[i] * dz_cell[j]            [m^3]
      r_face_area[i,j]  = 2 pi r_face[i] * dz_cell[j]          [m^2]  (lateral)
      z_face_area[i,j]  = ring_area[i]                         [m^2]  (annulus)
    The inner radial face area of the axis cell is exactly 0 (r_face[0]=0), so
    the r=0 symmetry boundary carries no flux automatically.
    """

    def __init__(self, radius, length, n_r, n_z, r_faces=None, z_faces=None):
        self.radius = require_positive("AxisymmetricGrid2D.radius", radius)
        self.length = require_positive("AxisymmetricGrid2D.length", length)
        if n_r < 3 or n_z < 3:
            raise ValueError("AxisymmetricGrid2D requires n_r, n_z >= 3")
        self.nr = int(n_r)
        self.nz = int(n_z)

        if r_faces is None:
            r_faces = np.linspace(0.0, self.radius, self.nr + 1)
            self.uniform_r = True
        else:
            r_faces = np.asarray(r_faces, dtype=float)
            if r_faces.shape != (self.nr + 1,):
                raise ValueError(f"r_faces shape {r_faces.shape} != ({self.nr + 1},)")
            if np.any(np.diff(r_faces) <= 0.0):
                raise ValueError("r_faces must be strictly increasing")
            self.uniform_r = bool(np.allclose(np.diff(r_faces), r_faces[1] - r_faces[0]))

        if z_faces is None:
            z_faces = np.linspace(0.0, self.length, self.nz + 1)
            self.uniform_z = True
        else:
            z_faces = np.asarray(z_faces, dtype=float)
            if z_faces.shape != (self.nz + 1,):
                raise ValueError(f"z_faces shape {z_faces.shape} != ({self.nz + 1},)")
            if np.any(np.diff(z_faces) <= 0.0):
                raise ValueError("z_faces must be strictly increasing")
            self.uniform_z = bool(np.allclose(np.diff(z_faces), z_faces[1] - z_faces[0]))

        self.r_faces = r_faces
        self.z_faces = z_faces
        self.r_centers = 0.5 * (r_faces[:-1] + r_faces[1:])
        self.z_centers = 0.5 * (z_faces[:-1] + z_faces[1:])

        # per-cell widths
        self.dr_cell = np.diff(r_faces)                       # (nr,)
        self.dz_cell = np.diff(z_faces)                       # (nz,)
        # centre-to-centre distances at interior faces
        self.r_face_distance = np.diff(self.r_centers)        # (nr-1,)
        self.z_face_distance = np.diff(self.z_centers)        # (nz-1,)
        # centre-to-bounding-face distances
        self.dist_to_inner_rface = self.r_centers - r_faces[:-1]   # (nr,)
        self.dist_to_outer_rface = r_faces[1:] - self.r_centers    # (nr,)
        self.dist_to_low_zface = self.z_centers - z_faces[:-1]     # (nz,)
        self.dist_to_high_zface = z_faces[1:] - self.z_centers     # (nz,)

        # centre mesh, shape (nr, nz)
        self.R, self.Z = np.meshgrid(self.r_centers, self.z_centers, indexing="ij")

        # EXACT annular ring cross-section per radial cell, and cell volumes
        self.ring_area = np.pi * (r_faces[1:] ** 2 - r_faces[:-1] ** 2)     # (nr,)
        self.cell_volume = self.ring_area[:, None] * self.dz_cell[None, :]  # (nr, nz)

        # EXACT face areas
        # radial (lateral cylinder) faces at each r_face, axial extent dz_cell
        self.r_face_area = 2.0 * np.pi * r_faces[:, None] * self.dz_cell[None, :]   # (nr+1, nz)
        # axial (annulus) faces: ring cross-section, identical for every z-face of a column
        self.z_face_area = np.repeat(self.ring_area[:, None], self.nz + 1, axis=1)  # (nr, nz+1)

        # nominal scalar spacings (exact for a uniform mesh; kept for back-compat)
        self.dr = self.radius / self.nr
        self.dz = self.length / self.nz

        self.boundary_faces = {
            "axis":   {"r_index": 0, "coord_r": float(r_faces[0]),
                       "label": "r=0 (symmetry)", "kind": "symmetry"},
            "outer":  {"r_index": self.nr, "coord_r": float(r_faces[-1]),
                       "label": "r=R (radial cold contact)", "kind": "cold_contact"},
            "source": {"z_index": 0, "coord_z": float(z_faces[0]),
                       "label": "z=0 (surface/source side)", "kind": "surface/insulated"},
            "sink":   {"z_index": self.nz, "coord_z": float(z_faces[-1]),
                       "label": "z=L (Kapitza sink)", "kind": "kapitza_sink"},
        }
        self.boundary_labels = {
            "axis": "r=0 (symmetry)", "outer": "r=R",
            "source": "z=0 (source/front side)", "sink": "z=L (sink/backside)",
        }

    @classmethod
    def graded(cls, radius, length, n_r, n_z, refine_r=None, refine_z=None, max_ratio=6.0):
        rf = graded_faces(radius, n_r, refine_r, max_ratio=max_ratio)
        zf = graded_faces(length, n_z, refine_z, max_ratio=max_ratio)
        return cls(radius, length, n_r, n_z, r_faces=rf, z_faces=zf)

    @property
    def shape(self):
        return (self.nr, self.nz)

    @property
    def total_volume(self):
        return float(self.cell_volume.sum())

    def __repr__(self):
        kr = "uniform" if self.uniform_r else "graded"
        kz = "uniform" if self.uniform_z else "graded"
        return (f"AxisymmetricGrid2D(R={self.radius:.3e}[{kr}], L={self.length:.3e}[{kz}], "
                f"nr={self.nr}, nz={self.nz})")
