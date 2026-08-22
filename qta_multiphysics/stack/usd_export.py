"""Deterministic OpenUSD (``.usda``) scene export of the simulated domain.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

OpenUSD is used here for one purpose: to make the *geometry the solver
actually used* inspectable and composable alongside CAD/assembly data, so a
reviewer can see where the resolved micro-domain sits, where the NV layer is,
where the beam lands, and where the model predicts its hotspots. It is a
representation layer -- no USD prim is an input to any solver, and none can
create gate evidence.

Three properties matter more than prettiness:

* **Faithfulness.** The domain and NV-layer boxes are explicit ``Mesh`` prims
  whose point coordinates are the mesh bounds themselves; nothing is inferred
  from a unit cube plus a transform stack, so a misread ``xformOpOrder``
  cannot silently move geometry.
* **Honest units.** The resolved near field is micrometre-scale, so the stage
  declares ``metersPerUnit = 1e-06`` and every coordinate is written in
  micrometres. The conversion factor is recorded in the manifest and asserted
  by the Stage-10 tests; SI values remain the canonical ones.
* **Deterministic bytes.** Fixed ``%.6f`` micrometre formatting, sorted
  hotspot ordering, no timestamps or host paths.

``usd-core`` (``pxr``) is optional: when importable, :func:`validate_usda`
opens the stage and confirms the prim set; when absent it reports
``UNAVAILABLE`` and the export itself is unaffected.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import AUTOMATIC_GATE_EFFECT, LABEL
from .workspace import (StrPath, guard_output_dir, relpath_in_repo,
                        write_json_deterministic, write_text_deterministic)

METERS_PER_UNIT = 1e-06          # stage unit = 1 micrometre
UM = 1e6                          # metres -> stage units
COORD_FMT = "%.6f"


def _um(value_m: float) -> str:
    return COORD_FMT % (float(value_m) * UM)


def _pt(x_m: float, y_m: float, z_m: float) -> str:
    return f"({_um(x_m)}, {_um(y_m)}, {_um(z_m)})"


def _box_mesh(name: str, x0: float, x1: float, y0: float, y1: float,
              z0: float, z1: float, color: str, doc: str,
              extra_attrs: Sequence[str] = ()) -> str:
    """An axis-aligned box as an explicit 8-point / 6-quad ``Mesh`` prim."""
    corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
               (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    pts = ", ".join(_pt(*c) for c in corners)
    idx = ", ".join(str(i) for f in faces for i in f)
    lines = [f'    def Mesh "{name}" (',
             f'        doc = """{doc}"""',
             "    )",
             "    {",
             f"        float3[] extent = [{_pt(x0, y0, z0)}, "
             f"{_pt(x1, y1, z1)}]",
             f"        point3f[] points = [{pts}]",
             f"        int[] faceVertexCounts = [{', '.join(['4'] * 6)}]",
             f"        int[] faceVertexIndices = [{idx}]",
             '        uniform token subdivisionScheme = "none"',
             f"        color3f[] primvars:displayColor = [{color}]",
             '        uniform token purpose = "guide"']
    lines.extend(f"        {a}" for a in extra_attrs)
    lines.append("    }")
    return "\n".join(lines)


def _sphere(name: str, x_m: float, y_m: float, z_m: float,
            radius_m: float, color: str, doc: str,
            extra_attrs: Sequence[str] = ()) -> str:
    lines = [f'    def Sphere "{name}" (',
             f'        doc = """{doc}"""',
             "    )",
             "    {",
             f"        double radius = {_um(radius_m)}",
             f"        float3[] extent = ["
             f"{_pt(x_m - radius_m, y_m - radius_m, z_m - radius_m)}, "
             f"{_pt(x_m + radius_m, y_m + radius_m, z_m + radius_m)}]",
             f"        double3 xformOp:translate = {_pt(x_m, y_m, z_m)}",
             '        uniform token[] xformOpOrder = ["xformOp:translate"]',
             f"        color3f[] primvars:displayColor = [{color}]"]
    lines.extend(f"        {a}" for a in extra_attrs)
    lines.append("    }")
    return "\n".join(lines)


def _cylinder(name: str, x_m: float, y_m: float, z0_m: float, z1_m: float,
              radius_m: float, color: str, doc: str) -> str:
    height = abs(float(z1_m) - float(z0_m))
    zc = 0.5 * (float(z0_m) + float(z1_m))
    return "\n".join([
        f'    def Cylinder "{name}" (',
        f'        doc = """{doc}"""',
        "    )",
        "    {",
        '        uniform token axis = "Z"',
        f"        double height = {_um(height)}",
        f"        double radius = {_um(radius_m)}",
        f"        float3[] extent = ["
        f"{_pt(x_m - radius_m, y_m - radius_m, z0_m)}, "
        f"{_pt(x_m + radius_m, y_m + radius_m, z1_m)}]",
        f"        double3 xformOp:translate = {_pt(x_m, y_m, zc)}",
        '        uniform token[] xformOpOrder = ["xformOp:translate"]',
        f"        color3f[] primvars:displayColor = [{color}]",
        '        uniform token purpose = "guide"',
        "    }"])


def usda_document(result: Any, n_hotspots: int = 5) -> str:
    """Build the ``.usda`` scene for a solved ``Thermal3DResult``."""
    grid, cfg = result.grid, result.cfg
    R = float(grid.half_extent_xy_m)
    L = float(grid.depth_m)
    nv_depth = float(cfg.geometry.nv_layer_depth_m)
    beam_x, beam_y = (float(result.laser.beam_center_xy[0]),
                      float(result.laser.beam_center_xy[1]))
    spot = float(cfg.laser.spot_radius_m)
    T_fridge = float(cfg.fridge.T_fridge_K)
    ix, iy, iz = result.probe_index

    # NV layer drawn as a thin slab straddling its nominal depth; the slab
    # half-thickness is the containing cell's half-height, i.e. what the mesh
    # can actually resolve there -- not an invented thickness.
    nv_half = 0.5 * float(grid.dz[iz])

    hot = result.hotspot_rows(top_n=int(n_hotspots))
    body = [
        _box_mesh("ResolvedDomain", -R, R, -R, R, 0.0, L,
                  "(0.18, 0.28, 0.45)",
                  "Resolved near-field finite-volume domain "
                  "(x, y in [-R, R]; z from the irradiated front face "
                  "z = 0 into the bulk). Forecast-only.",
                  [f"custom double qta:half_extent_xy_m = {R:.9e}",
                   f"custom double qta:depth_m = {L:.9e}",
                   f'custom string qta:label = "{LABEL}"']),
        _box_mesh("NVLayer", -R, R, -R, R,
                  max(nv_depth - nv_half, 0.0), nv_depth + nv_half,
                  "(0.85, 0.35, 0.25)",
                  "NV sensing layer at its nominal depth; slab thickness is "
                  "the resolving cell height, not a measured layer thickness.",
                  [f"custom double qta:nv_layer_depth_m = {nv_depth:.9e}",
                   f"custom double qta:slab_half_thickness_m = {nv_half:.9e}"]),
        _cylinder("BeamAxis", beam_x, beam_y, 0.0, L, spot,
                  "(0.95, 0.85, 0.25)",
                  "Laser beam axis at the 1/e^2 spot radius; a guide only "
                  "(the deposition profile is solved, not drawn)."),
        _sphere("NVProbe", float(grid.xc[ix]), float(grid.yc[iy]),
                float(grid.zc[iz]), 0.25 * spot, "(0.30, 0.80, 0.40)",
                "Cell whose temperature is reported as the NV probe series.",
                [f"custom double qta:T_final_K = "
                 f"{result.nv_layer_temperature_K():.9e}",
                 f"custom double qta:T_fridge_K = {T_fridge:.9e}"]),
    ]

    hot_prims = []
    for row in hot:
        rank = int(row["rank"])
        hot_prims.append(_sphere(
            f"Hotspot_{rank:02d}", float(row["x_m"]), float(row["y_m"]),
            float(row["z_m"]), 0.15 * spot, "(0.90, 0.20, 0.20)",
            f"Forecast peak-temperature cell, rank {rank}. "
            "Model-only; never a measured hotspot.",
            [f"custom double qta:T_peak_K = {float(row['T_peak_K']):.9e}",
             f"custom double qta:t_at_peak_s = "
             f"{float(row['t_at_peak_s']):.9e}",
             f"custom int qta:rank = {rank}"]))

    header = [
        "#usda 1.0",
        "(",
        '    defaultPrim = "World"',
        f"    metersPerUnit = {METERS_PER_UNIT}",
        '    upAxis = "Z"',
        '    doc = """QTA resolved near-field domain — '
        f'{LABEL}. Stage units are micrometres '
        f'(metersPerUnit = {METERS_PER_UNIT}); canonical SI values are '
        'carried on qta: attributes. Representation only: '
        f'automatic_gate_effect = {AUTOMATIC_GATE_EFFECT}."""',
        ")",
        "",
        'def Xform "World" (',
        '    kind = "assembly"',
        ")",
        "{",
        f'    custom string qta:label = "{LABEL}"',
        f'    custom string qta:automatic_gate_effect = '
        f'"{AUTOMATIC_GATE_EFFECT}"',
        f'    custom string qta:source_mode = "{result.source_mode}"',
        '    custom string qta:solver = "thermal_3d_transient"',
        f"    custom double qta:t_final_s = {float(result.t[-1]):.9e}",
    ]
    return "\n".join(header + [""] + body + hot_prims + ["}", ""])


def validate_usda(path: StrPath) -> dict:
    """Open the stage with ``pxr`` when available; report availability either way.

    Fail-closed: an absent ``usd-core`` yields ``UNAVAILABLE`` (not a pass),
    and a stage that opens but lacks the expected prims yields ``INVALID``.
    """
    try:
        from pxr import Usd
    except Exception as exc:                     # pragma: no cover - optional
        return {"availability": "UNAVAILABLE", "validator": "usd-core (pxr)",
                "reason": type(exc).__name__,
                "note": "export is unaffected; .usda text is written directly"}
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        return {"availability": "AVAILABLE", "result": "INVALID",
                "reason": "stage failed to open"}
    prims = sorted(p.GetPath().pathString for p in stage.Traverse())
    required = ["/World", "/World/ResolvedDomain", "/World/NVLayer",
                "/World/BeamAxis", "/World/NVProbe"]
    missing = [r for r in required if r not in prims]
    return {"availability": "AVAILABLE", "validator": "usd-core (pxr)",
            "result": "VALID" if not missing else "INVALID",
            "missing_prims": missing, "n_prims": len(prims),
            "meters_per_unit": float(
                stage.GetMetadata("metersPerUnit") or 0.0)}


def export_usd_scene(result: Any, out_dir: StrPath,
                     basename: str = "qta_domain",
                     n_hotspots: int = 5) -> dict:
    """Write ``<basename>.usda`` plus its manifest into a guarded workspace."""
    out = guard_output_dir(out_dir)
    name = f"{basename}.usda"
    digest = write_text_deterministic(out / name, usda_document(
        result, n_hotspots=n_hotspots))
    manifest = {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.usd_export",
        "format": "OpenUSD ASCII (.usda), USD 1.0",
        "meters_per_unit": METERS_PER_UNIT,
        "stage_units": "micrometre",
        "artifacts": [{"file": name, "sha256": digest}],
        "open_with": f"usdview {relpath_in_repo(out / name)}",
        "validation": validate_usda(out / name),
        "n_hotspots": int(n_hotspots),
        "note": "geometric representation of the solved domain; no prim is a "
                "solver input and none carries measured data",
    }
    write_json_deterministic(out / f"{basename}_usd_manifest.json", manifest)
    return manifest
