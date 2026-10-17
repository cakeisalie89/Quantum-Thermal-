"""The solved domain and its probe points as an OpenUSD stage (``.usda``).

A representation layer: the geometry a model solved on, made inspectable
and composable with other scene data. No prim is an input to any model and
none is evidence.

FAITHFUL. The domain is an explicit eight-point ``Mesh`` whose points ARE
its bounds -- not a unit cube under a transform stack, where a misread
``xformOpOrder`` would move geometry silently. Probe points are ``Sphere``
prims centred where the model reported a value, each carrying that value and
its unit as custom attributes.

UNITS. The stage declares ``metersPerUnit``; every coordinate is the SI
value divided by it, written with ``%.9g``. The factor is in the report.

DETERMINISTIC. Probes sorted by name, fixed formatting, no timestamps.

VALIDATION. With ``usd-core`` installed (the ``viz`` extra), :func:`validate`
opens the stage with ``pxr`` and checks the prim set, the unit metadata and
the domain's extent against what was declared. Without it the validation is
UNAVAILABLE -- reported, never assumed to have passed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

LABEL = "REPRESENTATION_ONLY NOT_EVIDENCE"
FMT = "%.9g"


class USDExportRefused(ValueError):
    pass


@dataclass(frozen=True)
class Probe:
    name: str
    position_m: tuple
    value: float
    unit: str


@dataclass(frozen=True)
class Scene:
    name: str
    lower_m: tuple
    upper_m: tuple
    probes: tuple = ()
    meters_per_unit: float = 1.0

    def problems(self) -> list:
        out = []
        if not self.name.isidentifier():
            out.append(f"scene name {self.name!r} is not an identifier")
        if len(self.lower_m) != 3 or len(self.upper_m) != 3:
            out.append("bounds must be three coordinates each")
        elif not all(u > lo for lo, u in zip(self.lower_m, self.upper_m)):
            out.append("every upper bound must exceed its lower bound")
        if not self.meters_per_unit > 0:
            out.append("metersPerUnit must be positive")
        names = [p.name for p in self.probes]
        if len(set(names)) != len(names):
            out.append("probe names repeat")
        for p in self.probes:
            if not p.name.isidentifier():
                out.append(f"probe name {p.name!r} is not an identifier")
            if len(p.position_m) == 3 and len(self.lower_m) == 3 and not all(
                    lo <= c <= u for c, lo, u in
                    zip(p.position_m, self.lower_m, self.upper_m)):
                out.append(f"probe {p.name} lies outside the domain")
        return out


def _c(v: float, mpu: float) -> str:
    return FMT % (float(v) / mpu)


def _pt(xyz, mpu) -> str:
    return "(" + ", ".join(_c(v, mpu) for v in xyz) + ")"


def usda_document(scene: Scene) -> str:
    bad = scene.problems()
    if bad:
        raise USDExportRefused("; ".join(bad))
    m = scene.meters_per_unit
    (x0, y0, z0), (x1, y1, z1) = scene.lower_m, scene.upper_m
    corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
               (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5),
             (2, 3, 7, 6), (3, 0, 4, 7)]
    span = min(x1 - x0, y1 - y0, z1 - z0)
    radius = span / 50.0
    out = ["#usda 1.0", "(",
           f'    defaultPrim = "{scene.name}"',
           f"    metersPerUnit = {FMT % m}",
           '    upAxis = "Z"',
           f'    doc = "{LABEL}"', ")", "",
           f'def Xform "{scene.name}"', "{",
           '    def Mesh "domain"', "    {",
           "        int[] faceVertexCounts = [" + ", ".join("4" for _ in faces)
           + "]",
           "        int[] faceVertexIndices = ["
           + ", ".join(str(i) for f in faces for i in f) + "]",
           "        point3f[] points = ["
           + ", ".join(_pt(c, m) for c in corners) + "]",
           "        float3[] extent = [" + _pt(scene.lower_m, m) + ", "
           + _pt(scene.upper_m, m) + "]",
           "    }"]
    for p in sorted(scene.probes, key=lambda q: q.name):
        out += ["", f'    def Sphere "{p.name}"', "    {",
                f"        double radius = {_c(radius, m)}",
                "        double3 xformOp:translate = "
                + _pt(p.position_m, m),
                '        uniform token[] xformOpOrder = '
                '["xformOp:translate"]',
                f"        custom double harness:value = {FMT % p.value}",
                f'        custom string harness:unit = "{p.unit}"',
                "    }"]
    out += ["}", ""]
    return "\n".join(out)


def validate(document: str) -> dict:
    """Open the stage with ``pxr`` and check it says what was declared."""
    try:
        from pxr import Sdf, Usd, UsdGeom
    except ImportError:
        return {"availability": "UNAVAILABLE", "validator": "usd-core (pxr)",
                "problems": []}
    layer = Sdf.Layer.CreateAnonymous(".usda")
    try:
        parsed = layer.ImportFromString(document)
    except Exception as exc:  # pxr raises Tf.ErrorException on bad syntax
        parsed, why = False, f"{type(exc).__name__}: {str(exc)[:200]}"
    else:
        why = "ImportFromString returned False"
    if not parsed:
        return {"availability": "AVAILABLE", "validator": "usd-core (pxr)",
                "problems": [f"pxr could not parse the document ({why})"]}
    stage = Usd.Stage.Open(layer)
    problems = []
    root = stage.GetDefaultPrim()
    if not root:
        problems.append("no default prim")
    prims = sorted(str(p.GetPath()) for p in stage.Traverse())
    mesh = UsdGeom.Mesh.Get(stage, root.GetPath().AppendChild("domain")) \
        if root else None
    if not mesh:
        problems.append("no domain mesh")
    else:
        if len(mesh.GetPointsAttr().Get()) != 8:
            problems.append("the domain mesh does not have eight points")
    return {"availability": "AVAILABLE", "validator": "usd-core (pxr)",
            "meters_per_unit": UsdGeom.GetStageMetersPerUnit(stage),
            "prims": prims, "problems": problems}


def slab_scene(params: dict, bundle_outputs: dict) -> Scene:
    """The slab model's domain -- thickness ``L_m`` along x, a unit-aspect
    square cross-section -- with its five reported temperatures as probes
    at the positions the model reported them."""
    from ..models.slab_transient import SAMPLE_FRACTIONS, SAMPLE_NAMES
    L = float(params["L_m"])
    probes = tuple(Probe(n, (f * L, 0.5 * L, 0.5 * L),
                         float(bundle_outputs[n][0]), bundle_outputs[n][1])
                   for n, f in zip(SAMPLE_NAMES, SAMPLE_FRACTIONS))
    return Scene("slab", (0.0, 0.0, 0.0), (L, L, L), probes,
                 meters_per_unit=1e-3 if L < 1.0 else 1.0)


def export_report(scene: Scene, document: str, validation: dict,
                  provenance: dict) -> dict:
    return {
        "label": LABEL,
        "format": "OpenUSD ASCII (.usda)",
        "producer": "scientific.integrations.usd_export",
        "meters_per_unit": scene.meters_per_unit,
        "probes": [p.name for p in sorted(scene.probes,
                                          key=lambda q: q.name)],
        "validation": validation,
        "sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
        "provenance": provenance,
        "note": "representation only: no prim is a model input or evidence",
    }
