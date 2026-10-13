"""The generic integrations: VTK, OpenUSD and OpenMDAO over an admitted model.

Each is a representation or an exploration, and each is held to the one
property that makes it trustworthy as that: the VTK field reads back as the
solved values, the USD stage says what was declared, and every OpenMDAO case
is the model's own output byte for byte. Each has a control that breaks the
property and must be caught.

Optional packages: ``usd-core`` (the ``viz`` extra) and ``openmdao`` (the
``uq`` extra). Where ``HARNESS_VIZ_REQUIRED`` / ``HARNESS_UQ_REQUIRED`` is
set -- the full leg of the stack-verify workflow -- a missing package FAILS
instead of skipping.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from scientific.integrations import openmdao_doe as D
from scientific.integrations import usd_export as U
from scientific.integrations import vtk_export as V
from scientific.model import run_model_with_artifacts
from scientific.models.slab_transient import SAMPLE_NAMES, SlabTransientModel

PARAMS = {"L_m": 0.05, "k_W_m_K": 15.0, "rho_c_J_m3_K": 3.6e6,
          "q_W_m3": 2e5, "h_W_m2_K": 50.0, "T_inf_K": 300.0, "T0_K": 300.0,
          "t_end_s": 600.0, "n_cells": 40, "n_steps": 80}


def _need(module: str, env: str):
    try:
        __import__(module)
    except ImportError:
        if os.environ.get(env) == "1":
            pytest.fail(f"{module} is required here ({env}=1) and missing")
        pytest.skip(f"{module} not installed")


@pytest.fixture(scope="module")
def slab():
    bundle, arts = run_model_with_artifacts(SlabTransientModel(), PARAMS)
    return bundle, arts["temperature_field"]


# ---- VTK -----------------------------------------------------------------
def test_the_slab_field_exports_and_reads_back_as_solved(slab):
    _, art = slab
    f = V.slab_field(art)
    assert f.shape() == (40, 1, 1)
    doc = V.vtr_document(f, time_s=600.0, provenance={"model": "slab"})
    assert V.round_trip_problems(f, doc) == []
    # the faces are the uniform grid the centres imply, ending at L
    assert f.x_faces[0] == 0.0
    assert f.x_faces[-1] == pytest.approx(PARAMS["L_m"], rel=1e-12)


def test_the_export_is_deterministic(slab):
    _, art = slab
    a = V.vtr_document(V.slab_field(art), time_s=600.0)
    b = V.vtr_document(V.slab_field(art), time_s=600.0)
    assert a == b


def test_cell_order_survives_a_three_axis_round_trip():
    """The ordering trap: VTK is x fastest, the arrays are C-ordered. An
    asymmetric field transposed by mistake would still read back finite."""
    vals = np.arange(2 * 3 * 4, dtype=float).reshape(2, 3, 4) + 0.5
    f = V.RectilinearField(x_faces=(0.0, 1.0, 2.0),
                           y_faces=(0.0, 1.0, 2.0, 3.0),
                           z_faces=(0.0, 1.0, 2.0, 3.0, 4.0),
                           cells={"u": vals}, units={"u": "1"})
    doc = V.vtr_document(f)
    np.testing.assert_array_equal(V.read_back(doc, "u", (2, 3, 4)), vals)
    flat = V.cell_order_flat(vals)
    assert flat[1] == vals[1, 0, 0]          # x fastest


def test_a_lossy_document_is_caught(slab):
    """CONTROL: perturb one written value beyond the declared precision; the
    round-trip check must say so."""
    _, art = slab
    f = V.slab_field(art)
    doc = V.vtr_document(f)
    first = V.FLOAT_FMT % float(np.asarray(f.cells["T_K"]).ravel()[0])
    bumped = V.FLOAT_FMT % (float(first) * (1 + 1e-6))
    bad = doc.replace(first, bumped, 1)
    assert bad != doc
    assert V.round_trip_problems(f, bad)


@pytest.mark.parametrize("field,needle", [
    (V.RectilinearField(x_faces=(0.0, 1.0), cells={"u": np.zeros((2, 1, 1))},
                        units={"u": "1"}), "shape"),
    (V.RectilinearField(x_faces=(0.0, 1.0, 0.5),
                        cells={"u": np.zeros((2, 1, 1))}, units={"u": "1"}),
     "increase"),
    (V.RectilinearField(x_faces=(0.0, 1.0), cells={"u": np.zeros((1, 1, 1))},
                        units={}), "unit"),
    (V.RectilinearField(x_faces=(0.0, 1.0),
                        cells={"u": np.full((1, 1, 1), np.nan)},
                        units={"u": "1"}), "non-finite"),
    (V.RectilinearField(x_faces=(0.0, 1.0)), "no cell array"),
])
def test_an_inconsistent_field_is_refused(field, needle):
    with pytest.raises(V.VTKExportRefused, match=needle):
        V.vtr_document(field)


def test_a_foreign_or_damaged_artefact_is_refused(slab):
    _, art = slab
    with pytest.raises(V.VTKExportRefused):
        V.slab_field(b"NOTSL\n" + art[6:])
    with pytest.raises(V.VTKExportRefused):
        V.slab_field(art[:-8])


# ---- OpenUSD -------------------------------------------------------------
def _outs(bundle):
    return {o.name: (o.quantity.value, o.quantity.unit)
            for o in bundle.outputs if o.quantity is not None}


def test_the_scene_is_deterministic_and_declares_its_units(slab):
    bundle, _ = slab
    a = U.usda_document(U.slab_scene(PARAMS, _outs(bundle)))
    b = U.usda_document(U.slab_scene(PARAMS, _outs(bundle)))
    assert a == b
    assert "metersPerUnit = 0.001" in a
    for name in SAMPLE_NAMES:
        assert f'def Sphere "{name}"' in a


def test_usd_core_reads_back_what_was_declared(slab):
    _need("pxr", "HARNESS_VIZ_REQUIRED")
    bundle, _ = slab
    doc = U.usda_document(U.slab_scene(PARAMS, _outs(bundle)))
    v = U.validate(doc)
    assert v["availability"] == "AVAILABLE"
    assert v["problems"] == []
    assert v["meters_per_unit"] == pytest.approx(1e-3)
    assert "/slab/domain" in v["prims"]


def test_usd_core_refuses_a_broken_stage():
    """CONTROL: a document pxr cannot parse is reported, not passed."""
    _need("pxr", "HARNESS_VIZ_REQUIRED")
    v = U.validate("#usda 1.0\n(\n def Xform \"x\" {\n")
    assert v["problems"]


@pytest.mark.parametrize("scene,needle", [
    (U.Scene("s", (0, 0, 0), (1, 1, 0)), "upper bound"),
    (U.Scene("1s", (0, 0, 0), (1, 1, 1)), "identifier"),
    (U.Scene("s", (0, 0, 0), (1, 1, 1),
             (U.Probe("p", (2.0, 0.5, 0.5), 1.0, "K"),)), "outside"),
    (U.Scene("s", (0, 0, 0), (1, 1, 1),
             (U.Probe("p", (0.5, 0.5, 0.5), 1.0, "K"),
              U.Probe("p", (0.6, 0.5, 0.5), 1.0, "K"))), "repeat"),
    (U.Scene("s", (0, 0, 0), (1, 1, 1), meters_per_unit=0.0), "positive"),
])
def test_an_inconsistent_scene_is_refused(scene, needle):
    with pytest.raises(U.USDExportRefused, match=needle):
        U.usda_document(scene)


# ---- OpenMDAO ------------------------------------------------------------
DESIGN = {"k_W_m_K": (5.0, 50.0), "h_W_m2_K": (10.0, 200.0)}


def test_every_case_through_openmdao_is_the_models_own_output():
    _need("openmdao", "HARNESS_UQ_REQUIRED")
    r = D.run_doe(SlabTransientModel(), PARAMS, DESIGN,
                  ("T_at_0", "T_at_L"), n_samples=5)
    assert r["availability"] == "AVAILABLE"
    assert r["plan"]["executed_as_planned"] is True
    assert r["cross_check"]["verdict"] == "BITWISE_IDENTITY"
    assert len(r["cases"]) == 5
    assert r["authority"] == "NON_AUTHORITATIVE"
    assert r["label"] == "EXPLORATION_ONLY NOT_A_RECOMMENDATION"
    for case in r["cases"]:
        for name, (lo, hi) in DESIGN.items():
            assert lo <= case["inputs"][name] <= hi


def test_the_cross_check_catches_one_ulp():
    """CONTROL: one case whose value moved by one ulp in transit must be
    reported as a mismatch. This is the plumbing defect the check exists
    for, and it is invisible at any printed precision."""
    model = SlabTransientModel()
    params = dict(PARAMS)
    got = D._outputs(model, params, ("T_at_0",))
    v, unit = got["T_at_0"]
    seen = [(params, {"T_at_0": (v, unit)}),
            (params, {"T_at_0": (float(np.nextafter(v, np.inf)), unit)})]
    rows, mismatched = D.cross_check(model, seen, ("T_at_0",), [])
    assert mismatched == [1]
    assert rows[0]["direct_run_identical"] is True


def test_the_plan_is_a_property_of_the_seed():
    spec = {k: (lo, hi, "u", "float") for k, (lo, hi) in DESIGN.items()}
    a = D.latin_hypercube(spec, 7, seed=3)
    assert a == D.latin_hypercube(spec, 7, seed=3)
    assert a != D.latin_hypercube(spec, 7, seed=4)
    for name, (lo, hi) in DESIGN.items():
        strata = sorted(int((p[name] - lo) / (hi - lo) * 7) for p in a)
        assert strata == list(range(7))     # one point per stratum


@pytest.mark.parametrize("design,needle", [
    ({"nope": (1.0, 2.0)}, "not a parameter"),
    ({"k_W_m_K": (1e-6, 1.0)}, "declared bounds"),
    ({"k_W_m_K": (2.0, 1.0)}, "empty range"),
])
def test_an_inadmissible_design_is_refused_before_anything_runs(design,
                                                                needle):
    with pytest.raises(D.DOERefused, match=needle):
        D.run_doe(SlabTransientModel(), PARAMS, design, ("T_at_0",))


def test_no_response_is_refused():
    with pytest.raises(D.DOERefused, match="no response"):
        D.run_doe(SlabTransientModel(), PARAMS, DESIGN, ())
