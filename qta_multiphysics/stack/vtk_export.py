"""Deterministic VTK XML export of the 3D transient fields (ParaView).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

ParaView is an *inspection* surface, not an authority: this module serializes
fields that ``qta_multiphysics.thermal_3d_transient`` already computed and
adds no physics, no interpolation, and no smoothing. The structured mesh
(``mesh_3d.StructuredGrid3D``) is genuinely rectilinear and graded, so it maps
exactly onto a VTK ``RectilinearGrid`` -- node coordinates are the finite-volume
*faces* and every solver value is written as **cell** data at the cell it was
solved on. Nothing is resampled to points, so what ParaView displays is the
finite-volume state itself.

Format choices are deliberate and all serve reproducibility:

* ASCII ``.vtr`` with ``%.9e`` formatting -- the same precision the project's
  CSV exports use, so a VTK file and its CSV counterpart agree digit for digit.
* No timestamps, no host paths, no writer-version strings: two runs of the
  same solver state produce byte-identical files (checked by the Stage-10
  determinism test).
* Coordinates are SI metres, unscaled. The resolved near-field domain is
  micrometre-scale, so set a large ParaView scale factor when viewing; the
  numbers on disk stay in the project's canonical units.

Requires no third-party VTK package: the XML is written directly and re-read
with ``xml.etree.ElementTree`` for verification.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from . import AUTOMATIC_GATE_EFFECT, LABEL
from .workspace import (StrPath, guard_output_dir, relpath_in_repo,
                        sha256_file, write_json_deterministic,
                        write_text_deterministic)

FLOAT_FMT = "%.9e"
VALUES_PER_LINE = 6
VTK_VERSION = "1.0"


def _fmt_array(values: npt.ArrayLike, indent: str) -> str:
    """Fixed-precision, fixed-wrap ASCII payload for one ``DataArray``."""
    flat = np.asarray(values, dtype=float).ravel()
    if flat.size == 0:
        return ""
    out = []
    for start in range(0, flat.size, VALUES_PER_LINE):
        chunk = flat[start:start + VALUES_PER_LINE]
        out.append(indent + " ".join(FLOAT_FMT % v for v in chunk))
    return "\n" + "\n".join(out) + "\n" + indent[:-2]


def _data_array(name: str, values: npt.ArrayLike, indent: str) -> str:
    n = int(np.asarray(values).size)
    return (f'{indent}<DataArray type="Float64" Name="{name}" '
            f'NumberOfComponents="1" NumberOfTuples="{n}" format="ascii">'
            f'{_fmt_array(values, indent + "  ")}</DataArray>')


def cell_order_flat(field_xyz: npt.ArrayLike) -> np.ndarray:
    """Reorder an ``(nx, ny, nz)`` field into VTK cell order (x fastest).

    The project stores fields C-ordered with ``z`` fastest; VTK enumerates
    cells with ``x`` fastest, so this is a Fortran-order ravel. Getting this
    wrong transposes the domain silently, which is why it is a named function
    with its own round-trip test rather than an inline ``.ravel()``.
    """
    a = np.asarray(field_xyz, dtype=float)
    if a.ndim != 3:
        raise ValueError(f"expected an (nx, ny, nz) field, got shape {a.shape}")
    return a.ravel(order="F")


def vtr_document(grid: Any, cell_fields: dict, time_s: float | None = None,
                 provenance: str = "") -> str:
    """Serialize one rectilinear-grid timestep to a VTK XML ``.vtr`` string.

    ``cell_fields`` maps array name -> ``(nx, ny, nz)`` field. ``grid`` is a
    :class:`~qta_multiphysics.mesh_3d.StructuredGrid3D`.
    """
    nx, ny, nz = int(grid.nx), int(grid.ny), int(grid.nz)
    if not cell_fields:
        raise ValueError("at least one cell field is required")
    for name, fld in cell_fields.items():
        shape = np.asarray(fld).shape
        if shape != (nx, ny, nz):
            raise ValueError(
                f"cell field '{name}' has shape {shape}, expected "
                f"{(nx, ny, nz)} (mesh nx, ny, nz)")
    extent = f"0 {nx} 0 {ny} 0 {nz}"
    scalars = sorted(cell_fields)[0]

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(f"<!-- {LABEL}; automatic_gate_effect="
                 f"{AUTOMATIC_GATE_EFFECT}; coordinates in SI metres; "
                 "cell-centred finite-volume values, not resampled -->")
    if provenance:
        lines.append(f"<!-- provenance: {provenance} -->")
    lines.append(f'<VTKFile type="RectilinearGrid" version="{VTK_VERSION}" '
                 'byte_order="LittleEndian" header_type="UInt64">')
    lines.append(f'  <RectilinearGrid WholeExtent="{extent}">')
    if time_s is not None:
        lines.append("    <FieldData>")
        lines.append(_data_array("TimeValue", [float(time_s)], "      "))
        lines.append("    </FieldData>")
    lines.append(f'    <Piece Extent="{extent}">')
    lines.append("      <Coordinates>")
    for axis, faces in (("x", grid.x_faces), ("y", grid.y_faces),
                        ("z", grid.z_faces)):
        lines.append(_data_array(f"{axis}_coordinates", faces, "        "))
    lines.append("      </Coordinates>")
    lines.append(f'      <CellData Scalars="{scalars}">')
    for name in sorted(cell_fields):
        lines.append(_data_array(name, cell_order_flat(cell_fields[name]),
                                 "        "))
    lines.append("      </CellData>")
    lines.append('      <PointData></PointData>')
    lines.append("    </Piece>")
    lines.append("  </RectilinearGrid>")
    lines.append("</VTKFile>")
    return "\n".join(lines) + "\n"


def pvd_document(entries: Iterable[tuple[float, str]]) -> str:
    """ParaView collection (``.pvd``) tying timesteps to their ``.vtr`` files.

    ``entries`` is a sequence of ``(time_s, filename)`` pairs; the filename is
    stored relative to the ``.pvd`` so the whole workspace stays relocatable.
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f"<!-- {LABEL}; timestep values in seconds -->",
             f'<VTKFile type="Collection" version="{VTK_VERSION}" '
             'byte_order="LittleEndian">',
             "  <Collection>"]
    for time_s, filename in entries:
        lines.append(f'    <DataSet timestep="{FLOAT_FMT % float(time_s)}" '
                     f'group="" part="0" file="{filename}"/>')
    lines.append("  </Collection>")
    lines.append("</VTKFile>")
    return "\n".join(lines) + "\n"


def read_back_cell_field(path: StrPath, name: str,
                         shape: tuple[int, int, int]) -> np.ndarray:
    """Re-parse a written ``.vtr`` and return one cell field in ``(nx,ny,nz)``.

    Used by the Stage-10 tests to prove the writer is lossless at the declared
    precision rather than merely well-formed.
    """
    root = ET.parse(str(path)).getroot()
    for arr in root.iter("DataArray"):
        if arr.get("Name") == name:
            flat = np.fromstring((arr.text or "").replace("\n", " "),
                                 sep=" ")
            return flat.reshape(shape, order="F")
    raise KeyError(f"cell field '{name}' not present in {path}")


def thermal_cell_fields(result: Any, k_time: int = -1) -> dict:
    """Cell fields exported for one timestep of a ``Thermal3DResult``."""
    T = result.T_xyz(k_time)
    T_fridge = float(result.cfg.fridge.T_fridge_K)
    return {"T_K": T,
            "T_rise_K": T - T_fridge,
            "cell_volume_m3": np.asarray(result.grid.volumes, dtype=float)}


def export_thermal_3d(result: Any, out_dir: StrPath,
                      time_indices: Iterable[int] | None = None,
                      basename: str = "thermal_3d") -> dict:
    """Write a ``.vtr`` per requested timestep plus the ``.pvd`` collection.

    Returns a manifest (artifact list with SHA-256, mesh summary, claim
    labels) that the Stage-10 workflow records. Writes only inside the
    guarded workspace; the canonical tree is never touched.
    """
    out = guard_output_dir(out_dir)
    nt = int(result.t.size)
    if time_indices is None:
        time_indices = range(nt)
    idx = [int(k) % nt for k in time_indices]
    if not idx:
        raise ValueError("no timesteps selected for export")

    grid = result.grid
    entries, artifacts = [], []
    prov = (f"solver=thermal_3d_transient source_mode={result.source_mode} "
            f"mesh={grid.nx}x{grid.ny}x{grid.nz}")
    for n, k in enumerate(idx):
        filename = f"{basename}_{n:04d}.vtr"
        doc = vtr_document(grid, thermal_cell_fields(result, k),
                           time_s=float(result.t[k]), provenance=prov)
        digest = write_text_deterministic(out / filename, doc)
        entries.append((float(result.t[k]), filename))
        artifacts.append({"file": filename, "sha256": digest,
                          "time_s": FLOAT_FMT % float(result.t[k]),
                          "time_index": k})

    pvd_name = f"{basename}.pvd"
    pvd_digest = write_text_deterministic(out / pvd_name, pvd_document(entries))
    artifacts.append({"file": pvd_name, "sha256": pvd_digest})

    manifest = {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.vtk_export",
        "format": "VTK XML RectilinearGrid (ascii, %.9e)",
        "open_with": f"paraview {relpath_in_repo(out / pvd_name)}",
        "coordinate_units": "m (SI, unscaled)",
        "field_units": {"T_K": "K", "T_rise_K": "K",
                        "cell_volume_m3": "m^3"},
        "cell_ordering": "VTK cell order (x fastest); solver arrays are "
                         "(nx, ny, nz) with z fastest",
        "mesh": grid.summary(),
        "source_mode": result.source_mode,
        "solver_status": result.solver_status,
        "n_timesteps_exported": len(idx),
        "artifacts": artifacts,
        "note": "visualization interchange only; adds no physics and creates "
                "no gate evidence",
    }
    manifest["manifest_sha256_of_artifacts"] = sha256_file(out / pvd_name)
    write_json_deterministic(out / f"{basename}_vtk_manifest.json", manifest)
    return manifest


def export_dir_digest(out_dir: StrPath) -> dict:
    """SHA-256 of every file in a Stage-10 VTK workspace, sorted by name."""
    out = Path(out_dir)
    return {p.name: sha256_file(p) for p in sorted(out.iterdir())
            if p.is_file()}
