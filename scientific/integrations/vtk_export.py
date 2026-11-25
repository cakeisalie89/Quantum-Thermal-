"""A solved cell-centred field as VTK XML (``.vtr``), for ParaView.

ParaView is an INSPECTION surface, not an authority. This module serializes
values a model already computed and adds no physics, no interpolation and no
smoothing: the grid's coordinates are the finite-volume FACES and every value
is written as CELL data on the cell it was solved on, so what ParaView shows
is the solver's state itself.

THE GRID. A rectilinear grid of 1, 2 or 3 axes. Each axis is given by its
face coordinates; an axis given as a single coordinate is COLLAPSED (one
point, no cells along it), which is how a one-dimensional field becomes a
VTK line of cells. The cell count along an axis is its face count minus one
(one for a collapsed axis), and every cell array must have exactly that
shape, ``(nx, ny, nz)``.

ORDER. Arrays are given C-ordered ``(nx, ny, nz)``; VTK enumerates cells
with x fastest, so they are written in Fortran order. Getting that wrong
transposes the domain silently, which is why it is :func:`cell_order_flat`
with its own round-trip test rather than an inline ravel.

BYTES. ASCII with ``%.9e``, six values a line, sorted array names, no
timestamps, no host paths, LF endings: the same field gives the same bytes.
The declared precision -- nine significant digits after the point -- is
what the round trip is held to (:data:`ROUND_TRIP_RTOL`).

NOT HERE: no VTK library. The document is written as XML and read back with
``xml.etree.ElementTree``; a reader that shares no code with the writer is
the check, and a missing ParaView changes nothing about the bytes.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np

FLOAT_FMT = "%.9e"
VALUES_PER_LINE = 6
VTK_VERSION = "1.0"
#: ``%.9e`` keeps ten significant digits; the round trip may differ from
#: the solved value by half a unit in the last of them.
ROUND_TRIP_RTOL = 5e-10
LABEL = "REPRESENTATION_ONLY NOT_EVIDENCE"


class VTKExportRefused(ValueError):
    pass


@dataclass(frozen=True)
class RectilinearField:
    """Face coordinates per axis, and named cell arrays on those cells."""
    x_faces: tuple
    y_faces: tuple = (0.0,)
    z_faces: tuple = (0.0,)
    cells: dict = field(default_factory=dict)
    units: dict = field(default_factory=dict)
    coordinate_unit: str = "m"

    def shape(self) -> tuple:
        return tuple(max(len(f) - 1, 1)
                     for f in (self.x_faces, self.y_faces, self.z_faces))

    def problems(self) -> list:
        out = []
        for axis, faces in (("x", self.x_faces), ("y", self.y_faces),
                            ("z", self.z_faces)):
            a = np.asarray(faces, dtype=float)
            if a.ndim != 1 or a.size < 1:
                out.append(f"{axis}: faces must be a non-empty 1-D sequence")
                continue
            if not np.all(np.isfinite(a)):
                out.append(f"{axis}: a face coordinate is not finite")
            if a.size > 1 and not np.all(np.diff(a) > 0):
                out.append(f"{axis}: faces must increase strictly")
        if not self.cells:
            out.append("no cell array")
        shape = self.shape()
        for name, values in self.cells.items():
            if not name or not name.replace("_", "").isalnum():
                out.append(f"array name {name!r} is not a plain identifier")
            got = np.shape(values)
            if tuple(got) != shape:
                out.append(f"cell array {name!r} has shape {tuple(got)}, "
                           f"the grid has {shape}")
            elif not np.all(np.isfinite(np.asarray(values, dtype=float))):
                out.append(f"cell array {name!r} has a non-finite value")
            if name not in self.units:
                out.append(f"cell array {name!r} declares no unit")
        return out


def cell_order_flat(values) -> np.ndarray:
    """An ``(nx, ny, nz)`` array in VTK cell order (x fastest)."""
    a = np.asarray(values, dtype=float)
    if a.ndim != 3:
        raise VTKExportRefused(f"expected an (nx, ny, nz) array, got shape "
                               f"{a.shape}")
    return a.ravel(order="F")


def _payload(values, indent: str) -> str:
    flat = np.asarray(values, dtype=float).ravel()
    if flat.size == 0:
        return ""
    rows = [indent + " ".join(FLOAT_FMT % v
                              for v in flat[i:i + VALUES_PER_LINE])
            for i in range(0, flat.size, VALUES_PER_LINE)]
    return "\n" + "\n".join(rows) + "\n" + indent[:-2]


def _array(name: str, values, indent: str) -> str:
    n = int(np.asarray(values).size)
    return (f'{indent}<DataArray type="Float64" Name="{name}" '
            f'NumberOfComponents="1" NumberOfTuples="{n}" format="ascii">'
            f'{_payload(values, indent + "  ")}</DataArray>')


def vtr_document(f: RectilinearField, *, time_s: float | None = None,
                 provenance: dict | None = None) -> str:
    """One field, one time, as a VTK XML RectilinearGrid document."""
    bad = f.problems()
    if bad:
        raise VTKExportRefused("; ".join(bad))
    ext = []
    for faces in (f.x_faces, f.y_faces, f.z_faces):
        ext += ["0", str(len(faces) - 1)]
    extent = " ".join(ext)
    names = sorted(f.cells)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f"<!-- {LABEL}; coordinates in {f.coordinate_unit}; "
             "cell-centred values as solved, not resampled -->"]
    if provenance:
        lines.append("<!-- provenance: "
                     + json.dumps(provenance, sort_keys=True,
                                  ensure_ascii=True).replace("--", "- -")
                     + " -->")
    lines += [f'<VTKFile type="RectilinearGrid" version="{VTK_VERSION}" '
              'byte_order="LittleEndian" header_type="UInt64">',
              f'  <RectilinearGrid WholeExtent="{extent}">']
    if time_s is not None:
        lines += ["    <FieldData>",
                  _array("TimeValue", [float(time_s)], "      "),
                  "    </FieldData>"]
    lines += [f'    <Piece Extent="{extent}">', "      <Coordinates>"]
    for axis, faces in (("x", f.x_faces), ("y", f.y_faces),
                        ("z", f.z_faces)):
        lines.append(_array(f"{axis}_coordinates", faces, "        "))
    lines += ["      </Coordinates>",
              f'      <CellData Scalars="{names[0]}">']
    for name in names:
        lines.append(_array(name, cell_order_flat(f.cells[name]),
                            "        "))
    lines += ["      </CellData>", "      <PointData></PointData>",
              "    </Piece>", "  </RectilinearGrid>", "</VTKFile>"]
    return "\n".join(lines) + "\n"


def read_back(document: str, name: str, shape: tuple) -> np.ndarray:
    """Re-parse a ``.vtr`` document and return one cell array, ``(nx, ny,
    nz)``. An independent reader: XML, not this module's writer."""
    root = ET.fromstring(document.encode("utf-8"))
    for arr in root.iter("DataArray"):
        if arr.get("Name") == name:
            parts = (arr.text or "").split()
            flat = np.array([float(p) for p in parts], dtype=float)
            if flat.size != int(np.prod(shape)):
                raise VTKExportRefused(
                    f"{name}: {flat.size} values for a grid of {shape}")
            return flat.reshape(shape, order="F")
    raise KeyError(f"no array {name!r} in the document")


def round_trip_problems(f: RectilinearField, document: str) -> list:
    """Every cell array read back from ``document`` against the solved
    values, at the declared precision. Empty when the export is lossless."""
    out = []
    for name, values in sorted(f.cells.items()):
        got = read_back(document, name, f.shape())
        want = np.asarray(values, dtype=float)
        err = np.abs(got - want)
        lim = ROUND_TRIP_RTOL * np.abs(want) + 1e-300
        if not np.all(err <= lim):
            worst = float(np.max(err / np.maximum(np.abs(want), 1e-300)))
            out.append(f"{name}: worst relative error {worst:.3e} exceeds "
                       f"{ROUND_TRIP_RTOL:g}")
    return out


SLAB_MAGIC = b"QTSL1\n"


def slab_field(artifact: bytes) -> RectilinearField:
    """The slab model's temperature-field artefact as a one-axis grid.

    The artefact stores cell centres and temperatures (see
    ``scientific.models.slab_transient``). The centres of a uniform grid
    determine its faces exactly -- ``dx = 2 x[0]``, faces ``i dx`` -- and a
    centre set that is not uniform is refused rather than given faces it
    did not have."""
    if not artifact.startswith(SLAB_MAGIC):
        raise VTKExportRefused("not a slab temperature-field artefact")
    body = artifact[len(SLAB_MAGIC):]
    nl = body.index(b"\n")
    head = json.loads(body[:nl].decode("utf-8"))
    n = int(head["n"])
    data = np.frombuffer(body[nl + 1:], dtype="<f8")
    if data.size != 2 * n or head.get("columns") != ["x", "T"]:
        raise VTKExportRefused("slab artefact header and payload disagree")
    x, T = data[:n].astype(float), data[n:].astype(float)
    dx = 2.0 * x[0]
    faces = np.arange(n + 1) * dx
    if not np.allclose(0.5 * (faces[:-1] + faces[1:]), x, rtol=1e-12,
                       atol=0.0):
        raise VTKExportRefused("slab cell centres are not uniform")
    return RectilinearField(
        x_faces=tuple(float(v) for v in faces),
        cells={"T_K": T.reshape(n, 1, 1)},
        units={"T_K": head.get("units", {}).get("T", "K")},
        coordinate_unit=head.get("units", {}).get("x", "m"))


def export_report(f: RectilinearField, document: str,
                  provenance: dict) -> dict:
    """What a caller records beside the written document."""
    import hashlib
    return {
        "label": LABEL,
        "format": "VTK XML RectilinearGrid, ascii, %.9e, cell data",
        "producer": "scientific.integrations.vtk_export",
        "grid_shape": list(f.shape()),
        "arrays": {k: f.units[k] for k in sorted(f.cells)},
        "coordinate_unit": f.coordinate_unit,
        "cell_ordering": "VTK cell order, x fastest",
        "round_trip": {"rtol": ROUND_TRIP_RTOL,
                       "problems": round_trip_problems(f, document)},
        "sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
        "provenance": provenance,
        "note": "representation only: adds no physics and is not evidence",
    }
