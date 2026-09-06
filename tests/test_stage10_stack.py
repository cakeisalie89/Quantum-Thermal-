"""Stage-10 tests: scientific-stack adapters (viz, UQ/MDAO, RAG, staged FEM,
selective Rust, deferred FMI).

MODEL-ONLY / FORECAST-ONLY. Software-verification results only; the scientific
gate PASS count remains zero and is asserted so below. Every test here proves
something about the *adapters* -- their determinism, their fail-closed
behaviour, and their inability to touch canonical state. None of them
validates physics, and none can create gate evidence.

Optional third-party packages (SALib, OpenMDAO, usd-core, a built Rust
extension, dolfinx) are exercised when present and skipped when absent; the
fail-closed paths are tested unconditionally.
"""
import csv
import json
import pathlib
import shutil
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np                                               # noqa: E402
import pytest                                                    # noqa: E402
from hypothesis import HealthCheck, given, settings              # noqa: E402
from hypothesis import strategies as st                          # noqa: E402

from qta_multiphysics.config import default_config               # noqa: E402
from qta_multiphysics.mesh_3d import Grid3DConfig                # noqa: E402
from qta_multiphysics.stack import (AUTOMATIC_GATE_EFFECT,       # noqa: E402
                                    LABEL)
from qta_multiphysics.stack import fem_fenicsx as FEM            # noqa: E402
from qta_multiphysics.stack import fmi_contract as FMI           # noqa: E402
from qta_multiphysics.stack import mdao_openmdao as MDAO         # noqa: E402
from qta_multiphysics.stack import rag_index as RAG              # noqa: E402
from qta_multiphysics.stack import registry as REG               # noqa: E402
from qta_multiphysics.stack import rust_kernel as RUST           # noqa: E402
from qta_multiphysics.stack import sensitivity_salib as SAL      # noqa: E402
from qta_multiphysics.stack import usd_export as USD             # noqa: E402
from qta_multiphysics.stack import vtk_export as VTK             # noqa: E402
from qta_multiphysics.stack import workspace as WS               # noqa: E402
from qta_multiphysics.thermal_3d_transient import (              # noqa: E402
    solve_thermal_3d)

DET = settings(max_examples=25, deadline=None, derandomize=True,
               suppress_health_check=[HealthCheck.too_slow])

ROOT = WS.repo_root()
TEST_WS = "verification/stage10/pytest"
TINY_MESH = Grid3DConfig(nx=5, ny=5, nz=6)


@pytest.fixture(scope="module")
def tiny_result():
    """One small 3D solve shared by the visualization tests (~0.6 s)."""
    return solve_thermal_3d(default_config(), TINY_MESH, n_eval=3)


@pytest.fixture
def ws(request):
    """A clean per-test subdirectory of the Stage-10 workspace."""
    path = ROOT / TEST_WS / request.node.name.replace("/", "_")
    if path.exists():
        shutil.rmtree(path)
    yield path
    if path.exists():
        shutil.rmtree(path)


# ----------------------------- workspace guard -----------------------------

def test_guard_refuses_canonical_and_governed_locations(tmp_path):
    with pytest.raises(ValueError):
        WS.guard_output_dir(ROOT)                 # repository root
    with pytest.raises(ValueError):
        WS.guard_output_dir(".")                  # root by another name
    for protected in ("qta_multiphysics/x", "ro-crate/x", "tests/x",
                      "attic/x", ".github/x"):
        with pytest.raises(ValueError):
            WS.guard_output_dir(protected)
    with pytest.raises(ValueError):
        WS.guard_output_dir(tmp_path)             # outside the repository


def test_guard_accepts_workspace_and_creates_it(ws):
    resolved = WS.guard_output_dir(ws)
    assert resolved.is_dir()
    assert resolved.relative_to(ROOT).parts[0] == "verification"


def test_deterministic_writers_are_byte_stable(ws):
    out = WS.guard_output_dir(ws)
    obj = {"b": 2, "a": [1, 2, 3], "nested": {"z": 1, "y": 2}}
    first = WS.write_json_deterministic(out / "a.json", obj)
    second = WS.write_json_deterministic(out / "b.json", dict(reversed(
        list(obj.items()))))
    assert first == second                        # key order cannot leak in
    assert (out / "a.json").read_bytes().endswith(b"\n")


def test_all_stack_modules_declare_no_gate_effect():
    for mod in (VTK, USD, SAL, MDAO, RAG, FEM, RUST, FMI):
        assert mod.LABEL == LABEL
        assert mod.AUTOMATIC_GATE_EFFECT == AUTOMATIC_GATE_EFFECT == "NONE"


def test_gate_table_still_reports_zero_pass():
    rows = list(csv.DictReader(open(ROOT / "results_gate_table.csv")))
    assert len(rows) == 83
    assert sum(r["status"] == "PASS" for r in rows) == 0


# --------------------------------- VTK -------------------------------------

@given(nx=st.integers(2, 5), ny=st.integers(2, 5), nz=st.integers(2, 5))
@DET
def test_cell_order_flat_is_x_fastest(nx, ny, nz):
    field = np.arange(nx * ny * nz, dtype=float).reshape(nx, ny, nz)
    flat = VTK.cell_order_flat(field)
    assert flat.shape == (nx * ny * nz,)
    # VTK enumerates x fastest, then y, then z
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                k = ix + nx * (iy + ny * iz)
                assert flat[k] == field[ix, iy, iz]


def test_cell_order_flat_rejects_non_3d():
    with pytest.raises(ValueError):
        VTK.cell_order_flat(np.zeros((3, 3)))


def test_vtr_rejects_mismatched_field_shape(tiny_result):
    with pytest.raises(ValueError):
        VTK.vtr_document(tiny_result.grid, {"bad": np.zeros((2, 2, 2))})
    with pytest.raises(ValueError):
        VTK.vtr_document(tiny_result.grid, {})


def test_vtr_round_trips_at_declared_precision(tiny_result, ws):
    manifest = VTK.export_thermal_3d(tiny_result, ws, time_indices=[-1])
    out = ROOT / TEST_WS / "test_vtr_round_trips_at_declared_precision"
    shape = (tiny_result.grid.nx, tiny_result.grid.ny, tiny_result.grid.nz)
    back = VTK.read_back_cell_field(out / "thermal_3d_0000.vtr", "T_K", shape)
    ref = tiny_result.T_xyz(-1)
    assert np.max(np.abs(back - ref) / np.abs(ref)) < 1e-9   # %.9e
    assert manifest["automatic_gate_effect"] == "NONE"
    assert manifest["n_timesteps_exported"] == 1


def test_vtr_is_wellformed_xml_with_cell_data(tiny_result, ws):
    VTK.export_thermal_3d(tiny_result, ws, time_indices=[0])
    out = ROOT / TEST_WS / "test_vtr_is_wellformed_xml_with_cell_data"
    root = ET.parse(out / "thermal_3d_0000.vtr").getroot()
    assert root.tag == "VTKFile" and root.get("type") == "RectilinearGrid"
    grid = root.find("RectilinearGrid")
    n = tiny_result.grid.nx * tiny_result.grid.ny * tiny_result.grid.nz
    names = {a.get("Name") for a in grid.iter("DataArray")}
    assert {"T_K", "T_rise_K", "cell_volume_m3"} <= names
    for arr in grid.find("Piece").find("CellData").iter("DataArray"):
        assert int(arr.get("NumberOfTuples")) == n
    collection = ET.parse(out / "thermal_3d.pvd").getroot()
    assert collection.get("type") == "Collection"


def test_vtk_export_is_byte_deterministic(tiny_result, ws):
    """Re-running the export must reproduce every byte, manifest included."""
    VTK.export_thermal_3d(tiny_result, ws, time_indices=[0, -1])
    first = VTK.export_dir_digest(ws)
    VTK.export_thermal_3d(tiny_result, ws, time_indices=[0, -1])
    assert VTK.export_dir_digest(ws) == first
    assert set(first) == {"thermal_3d_0000.vtr", "thermal_3d_0001.vtr",
                          "thermal_3d.pvd", "thermal_3d_vtk_manifest.json"}


def test_vtk_payload_is_independent_of_the_output_directory(tiny_result):
    """Two workspaces must differ only in the manifest's location fields."""
    a = ROOT / TEST_WS / "det_a"
    b = ROOT / TEST_WS / "det_b"
    for path in (a, b):
        if path.exists():
            shutil.rmtree(path)
    VTK.export_thermal_3d(tiny_result, a, time_indices=[-1])
    VTK.export_thermal_3d(tiny_result, b, time_indices=[-1])
    da, db = VTK.export_dir_digest(a), VTK.export_dir_digest(b)
    data = {k: v for k, v in da.items() if not k.endswith("manifest.json")}
    assert data == {k: v for k, v in db.items()
                    if not k.endswith("manifest.json")}
    ma = json.loads((a / "thermal_3d_vtk_manifest.json").read_text())
    mb = json.loads((b / "thermal_3d_vtk_manifest.json").read_text())
    assert ma["artifacts"] == mb["artifacts"]
    assert ma["open_with"] != mb["open_with"]      # only the path differs
    shutil.rmtree(a)
    shutil.rmtree(b)


# --------------------------------- USD -------------------------------------

def test_usda_contains_the_declared_geometry(tiny_result, ws):
    manifest = USD.export_usd_scene(tiny_result, ws, n_hotspots=3)
    out = ROOT / TEST_WS / "test_usda_contains_the_declared_geometry"
    text = (out / "qta_domain.usda").read_text()
    assert text.startswith("#usda 1.0")
    for prim in ('def Xform "World"', 'def Mesh "ResolvedDomain"',
                 'def Mesh "NVLayer"', 'def Cylinder "BeamAxis"',
                 'def Sphere "NVProbe"', 'def Sphere "Hotspot_01"'):
        assert prim in text
    assert text.count('def Sphere "Hotspot_') == 3
    assert LABEL in text
    assert manifest["meters_per_unit"] == USD.METERS_PER_UNIT


def test_usd_scale_matches_declared_meters_per_unit(tiny_result):
    """Stage units must really be micrometres, not merely declared as such."""
    text = USD.usda_document(tiny_result, n_hotspots=1)
    depth_m = float(tiny_result.grid.depth_m)
    for line in text.splitlines():
        if "float3[] extent" in line and "ResolvedDomain" not in line:
            continue
    # the domain mesh spans [0, depth] in z, written in stage units
    z_extent_um = depth_m / USD.METERS_PER_UNIT
    assert f"{z_extent_um:.6f}" in text
    assert f"metersPerUnit = {USD.METERS_PER_UNIT}" in text


def test_usd_export_is_byte_deterministic(tiny_result):
    a = USD.usda_document(tiny_result)
    b = USD.usda_document(tiny_result)
    assert a == b


@pytest.mark.skipif(not USD.validate_usda.__doc__ or
                    __import__("importlib").util.find_spec("pxr") is None,
                    reason="usd-core (pxr) not installed")
def test_usda_opens_in_openusd(tiny_result, ws):
    manifest = USD.export_usd_scene(tiny_result, ws)
    v = manifest["validation"]
    assert v["availability"] == "AVAILABLE"
    assert v["result"] == "VALID", v
    assert v["missing_prims"] == []
    assert v["meters_per_unit"] == USD.METERS_PER_UNIT


def test_usd_validation_reports_unavailable_without_pxr(monkeypatch, ws):
    """Absent usd-core must read UNAVAILABLE -- never as a silent pass."""
    real_import = __import__

    def blocked(name, *args, **kwargs):
        if name == "pxr" or name.startswith("pxr."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    report = USD.validate_usda(ROOT / "README.md")
    assert report["availability"] == "UNAVAILABLE"
    assert "result" not in report          # absence is not a verdict


# ------------------------------ SALib / UQ ---------------------------------

def test_parameter_bounds_are_symmetric_and_clamped():
    bounds = SAL.parameter_bounds(fraction=0.10)
    assert [b["name"] for b in bounds] == list(SAL.salib_problem()["names"])
    for b in bounds:
        assert b["low"] < b["nominal"] < b["high"]
        assert b["low"] >= 0.0
    # a fraction wide enough to leave the physical range must clamp, not wrap
    wide = {b["name"]: b for b in SAL.parameter_bounds(fraction=5.0)}
    frac = wide["laser.absorbed_fraction"]
    assert frac["high"] <= 1.0 and frac["clamped"] is True


def test_unavailable_report_names_the_authority_and_returns_no_indices():
    rep = SAL.unavailable_report("sobol")
    assert rep["availability"] == "UNAVAILABLE"
    assert rep["rows"] == []
    assert "sensitivity_3d" in rep["authority_in_force"]
    assert rep["automatic_gate_effect"] == "NONE"


def test_ranking_agreement_endpoints():
    names = list(SAL.salib_problem()["names"])
    rows = [{"parameter": n, "rank_by_ST": i + 1} for i, n in enumerate(names)]
    same = SAL.ranking_agreement(rows, names)
    assert same["identical_order"] and same["top1_agrees"]
    assert same["kendall_tau_b"] == pytest.approx(1.0)
    reverse = SAL.ranking_agreement(rows, list(reversed(names)))
    assert reverse["kendall_tau_b"] == pytest.approx(-1.0)
    assert reverse["identical_order"] is False
    mismatch = SAL.ranking_agreement(rows, names[:-1] + ["other"])
    assert mismatch["comparable"] is False


@pytest.mark.skipif(not SAL.salib_available(), reason="SALib not installed")
def test_sobol_wiring_is_deterministic_on_an_analytic_model():
    """Wiring and reproducibility only -- no solver runs, no physics claim."""
    def model(x):
        return float(x[0] * 3.0 + x[2] * 0.5 + x[0] * x[2])

    a = SAL.sobol_indices(model_fn=model, n_base=16, seed=7)
    b = SAL.sobol_indices(model_fn=model, n_base=16, seed=7)
    assert a["rows"] == b["rows"]
    assert a["n_model_evaluations"] == 16 * (4 + 2)
    ranked = {r["parameter"]: r["rank_by_ST"] for r in a["rows"]}
    # the two inert inputs must rank last, in either order
    inert = {"laser.spot_radius_m", "fridge.kapitza_coeff_W_m2_K4"}
    assert {p for p, r in ranked.items() if r > 2} == inert


@pytest.mark.skipif(not SAL.salib_available(), reason="SALib not installed")
def test_morris_wiring_is_deterministic_on_an_analytic_model():
    def model(x):
        return float(x[0] * 2.0)

    a = SAL.morris_indices(model_fn=model, n_trajectories=4, seed=11)
    b = SAL.morris_indices(model_fn=model, n_trajectories=4, seed=11)
    assert a["rows"] == b["rows"]
    assert a["rows"][0]["parameter"] == "laser.absorbed_fraction"


@pytest.mark.skipif(not SAL.salib_available(), reason="SALib not installed")
def test_cross_check_report_is_written_and_labelled(ws):
    def model(x):
        return float(x[0])

    rep = SAL.run_cross_check(ws, method="sobol", n_base=8, model_fn=model)
    out = ROOT / TEST_WS / "test_cross_check_report_is_written_and_labelled"
    doc = json.loads((out / "salib_sobol_cross_check.json").read_text())
    assert doc["role"] == "CROSS_CHECK_ONLY"
    assert "sensitivity_3d" in doc["authority"]
    assert rep["analysis"]["availability"] == "AVAILABLE"
    # an injected model is not the project's response: no agreement verdict
    assert "agreement_with_canonical" not in doc


# ------------------------------- OpenMDAO ----------------------------------

def test_only_design_provenance_parameters_may_be_optimised():
    assert MDAO.design_parameter_names() == ["laser.spot_radius_m"]
    assert set(MDAO.uncertain_parameter_names()) == {
        "laser.absorbed_fraction", "laser.absorption_coeff_1_m",
        "fridge.kapitza_coeff_W_m2_K4"}
    MDAO.assert_design_variables(["laser.spot_radius_m"])
    for forbidden in MDAO.uncertain_parameter_names():
        with pytest.raises(ValueError):
            MDAO.assert_design_variables([forbidden])


def test_component_spec_is_complete_without_openmdao():
    spec = MDAO.component_spec()
    assert spec["automatic_gate_effect"] == "NONE"
    assert len(spec["inputs"]) == len(SAL.PARAMETERS)
    for inp in spec["inputs"]:
        assert inp["lower"] < inp["default"] < inp["upper"]
        assert inp["role"] in ("design_variable", "uncertain_input")
    assert {o["om_name"] for o in spec["outputs"]} == {"probe_rise_K",
                                                      "peak_T_K"}


def test_latin_hypercube_samples_are_deterministic_and_in_bounds():
    inputs = [i for i in MDAO.component_spec()["inputs"]
              if i["role"] == "uncertain_input"]
    a = MDAO.latin_hypercube_samples(inputs, 6, seed=3)
    b = MDAO.latin_hypercube_samples(inputs, 6, seed=3)
    assert a == b and len(a) == 6
    by_name = {i["om_name"]: i for i in inputs}
    for sample in a:
        assert len(sample) == len(inputs)
        for om_name, value in sample:
            assert by_name[om_name]["lower"] <= value <= \
                   by_name[om_name]["upper"]


@pytest.mark.skipif(not MDAO.openmdao_available(),
                    reason="OpenMDAO not installed")
def test_doe_envelope_brackets_the_nominal_response(ws):
    rep = MDAO.run_doe(ws, n_samples=3)
    assert rep["availability"] == "AVAILABLE"
    assert rep["status"] == "NOT_A_RECOMMENDATION"
    assert len(rep["cases"]) == 3
    env = rep["envelope"]
    assert env["probe_rise_K_min"] <= env["probe_rise_K_mean"] \
        <= env["probe_rise_K_max"]
    assert env["probe_rise_K_min"] > 0.0


# ---------------------------- read-only RAG --------------------------------

def test_corpus_excludes_non_governed_trees():
    files = RAG.corpus_files()
    assert files == sorted(set(files))
    assert "README.md" in files and "CLAIMS_BOUNDARY.md" in files
    for rel in files:
        assert not any(part in RAG.EXCLUDED_DIRS
                       for part in pathlib.Path(rel).parts)


def test_index_is_deterministic_and_hits_carry_provenance():
    a = RAG.build_index()
    b = RAG.build_index()
    assert a.to_dict() == b.to_dict()
    assert a.stale_files() == []
    hits = a.search("gate PASS count zero", k=3)
    assert hits, "expected at least one hit for a phrase the corpus contains"
    for h in hits:
        assert h["citation"].startswith(h["path"])
        assert h["line_start"] <= h["line_end"]
        assert len(h["source_sha256"]) == 64
        assert h["evidence_status"] == "RETRIEVED_TEXT_NOT_EVIDENCE"


def test_hits_are_verbatim_spans_of_the_cited_lines():
    index = RAG.build_index()
    for hit in index.search("interlock mode", k=3):
        lines = (ROOT / hit["path"]).read_text().splitlines()
        span = "\n".join(lines[hit["line_start"] - 1:hit["line_end"]]).strip()
        assert hit["text"] == span[:RAG.MAX_SNIPPET_CHARS]


def test_empty_and_stopword_queries_return_nothing_rather_than_noise():
    index = RAG.build_index()
    assert index.search("") == []
    assert index.search("the and of to") == []


def test_retrieval_module_imports_no_network_or_model_client():
    """Read-only RAG must be provably offline: no client library in scope."""
    source = (ROOT / "qta_multiphysics" / "stack" / "rag_index.py").read_text()
    banned = ("import socket", "import urllib", "import requests",
              "import http", "httpx", "openai", "anthropic", "transformers",
              "sentence_transformers", "faiss", "chromadb")
    for token in banned:
        assert token not in source, f"unexpected client dependency: {token}"


def test_stale_index_is_detected(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "doc.md").write_text("# Heading\nalpha beta gamma\n")
    index = RAG.build_index(root=root)
    assert index.stale_files() == []
    (root / "doc.md").write_text("# Heading\ndelta epsilon\n")
    assert index.stale_files() == ["doc.md"]


def test_retrieve_states_the_boundary():
    doc = RAG.retrieve("tau_c threshold", k=2)
    assert doc["generation"] == "NONE"
    assert "not evidence" in doc["disclaimer"] or \
           "does not make it measured" in doc["disclaimer"]
    assert doc["automatic_gate_effect"] == "NONE"


@given(text=st.text(alphabet=st.characters(min_codepoint=32,
                                           max_codepoint=126),
                    min_size=0, max_size=200))
@DET
def test_tokenizer_is_total_and_drops_stopwords(text):
    tokens = RAG.tokenize(text)
    assert all(len(t) > 1 for t in tokens)
    assert not (set(tokens) & RAG.STOPWORDS)


# ------------------------------ FEniCSx (staged) ---------------------------

def test_manufactured_solution_satisfies_its_own_pde():
    mms = FEM.ManufacturedSolution(L=1.0, k=2.0, rho_cp=3.0, decay=0.7)
    z = np.linspace(0.02, 0.98, 200)
    # h is chosen where the central second difference is balanced between
    # truncation (~h^2) and cancellation (~eps/h^2); 1e-6 would be pure noise
    t, h = 0.13, 1e-4
    dTdt = (mms.T(z, t + h) - mms.T(z, t - h)) / (2 * h)
    d2Tdz2 = (mms.T(z + h, t) - 2 * mms.T(z, t) + mms.T(z - h, t)) / h ** 2
    residual = mms.rho_cp * dTdt - mms.k * d2Tdz2 - mms.source(z, t)
    assert np.max(np.abs(residual)) < 1e-5


def test_harness_reads_zero_error_for_an_exact_solver():
    rep = FEM.run_acceptance(FEM.analytic_reference_solver,
                             n_cells_sequence=(10, 20))
    assert rep["verdict"] == "EXACT_RECOVERED"
    assert rep["observed_order_L2"] is None
    assert rep["max_l2_error"] <= FEM.EXACT_RECOVERY_FLOOR


def test_harness_detects_second_order_convergence():
    rep = FEM.run_acceptance(FEM.fv_reference_solver,
                             n_cells_sequence=(20, 40, 80))
    assert rep["verdict"] == "PASS"
    assert rep["observed_order_L2"] == pytest.approx(2.0, abs=0.1)
    errors = [r["l2_error"] for r in rep["refinement"]]
    assert errors == sorted(errors, reverse=True)


def test_observed_order_rejects_degenerate_input():
    with pytest.raises(ValueError):
        FEM.observed_order([1e-3], [0.1])
    with pytest.raises(ValueError):
        FEM.observed_order([0.0, 1e-3], [0.1, 0.05])


def test_fenicsx_is_staged_and_not_authoritative():
    rep = FEM.status_report()
    assert rep["adoption_status"] == "STAGED"
    assert rep["automatic_gate_effect"] == "NONE"
    assert "thermal_1d" in rep["authority_in_force"]
    if not FEM.dolfinx_available():
        assert rep["availability"] == "UNAVAILABLE"
        assert rep["blocking"]


# --------------------------- selective Rust --------------------------------

def test_numpy_reference_matches_the_closed_form():
    rng = np.random.default_rng(1)
    a, dl, kl, dr, kr = (rng.uniform(0.1, 2.0, 64) for _ in range(5))
    got = RUST.numpy_face_conductance(a, dl, kl, dr, kr)
    assert np.allclose(got, a / (dl / kl + dr / kr), rtol=0, atol=0)
    t = rng.uniform(1.0, 500.0, 64)
    assert np.allclose(RUST.numpy_conductivity_power_law(t, 3.0, 300.0, 2.0),
                       3.0 * (t / 300.0) ** 2.0, rtol=0, atol=0)


def test_default_backend_is_numpy_without_the_env_var(monkeypatch):
    monkeypatch.delenv(RUST.ENABLE_ENV_VAR, raising=False)
    assert RUST.rust_enabled() is False
    for name in RUST.KERNELS:
        assert RUST.backend_in_force(name) == "numpy"


def test_enabling_rust_without_the_extension_still_yields_numpy(monkeypatch):
    monkeypatch.setenv(RUST.ENABLE_ENV_VAR, "1")
    monkeypatch.setattr(RUST, "rust_available", lambda: False)
    for name in RUST.KERNELS:
        assert RUST.backend_in_force(name) == "numpy"


def test_dispatch_rejects_unknown_kernels():
    with pytest.raises(KeyError):
        RUST.dispatch("no_such_kernel")


def test_parity_verdicts_are_consistent():
    """Adoption implies bit identity -- 'close enough' is never adoption."""
    report = RUST.status_report()
    assert report["default_backend"] == "numpy"
    assert report["admission_rule"] == RUST.PARITY_RULE
    assert RUST.PARITY_RULE == "bit_for_bit_identical_to_numpy_reference"
    for kernel in report["kernels"]:
        if kernel.get("adopted"):
            assert kernel["bit_identical"] is True
            assert kernel["max_ulp_difference"] == 0
        else:
            assert kernel["backend_in_force"] == "numpy"
    assert set(report["adopted_kernels"]) <= set(RUST.KERNELS)


@pytest.mark.skipif(not RUST.rust_available(),
                    reason="qta_kernels extension not built")
def test_built_extension_is_checked_kernel_by_kernel():
    for name in sorted(RUST.KERNELS):
        rep = RUST.kernel_parity(name)
        assert rep["availability"] == "AVAILABLE"
        assert rep["verdict"] in ("ADOPTED", "REJECTED_NOT_BIT_IDENTICAL")
        assert rep["max_relative_difference"] < 1e-12   # sane either way


# ------------------------------- FMI (deferred) ----------------------------

def test_fmi_is_deferred_and_produces_no_fmu(ws):
    rep = FMI.write_contract(ws)
    out = ROOT / TEST_WS / "test_fmi_is_deferred_and_produces_no_fmu"
    assert rep["adoption_status"] == "DEFERRED"
    assert rep["fmu_produced"] is False
    assert rep["compliance_claimed"] is False
    assert rep["ready_to_export"] is False and rep["open_prerequisites"]
    names = {p.name for p in out.iterdir()}
    assert "modelDescription.contract.xml" in names
    assert "modelDescription.xml" not in names      # cannot be zipped by slip
    assert not any(n.endswith(".fmu") for n in names)


def test_fmi_contract_is_wellformed_and_declares_its_variables():
    root = ET.fromstring(FMI.contract_xml())
    assert root.tag == "fmiModelDescription"
    assert root.get("fmiVersion") == FMI.FMI_VERSION
    declared = {v.get("name") for v in root.find("ModelVariables")}
    assert declared == {name for name, *_ in FMI.VARIABLES}
    outputs = {int(o.get("valueReference"))
               for o in root.find("ModelStructure")}
    vrefs = FMI.value_references()
    assert outputs == {vrefs[n] for n, c, *_ in FMI.VARIABLES
                       if c == "output"}
    cosim = root.find("CoSimulation")
    assert cosim.get("canGetAndSetFMUState") == "false"   # matches FMI-P1


def test_fmi_token_is_derived_from_the_interface(monkeypatch):
    before = FMI.instantiation_token()
    assert FMI.instantiation_token() == before          # deterministic
    monkeypatch.setattr(FMI, "VARIABLES",
                        FMI.VARIABLES + (("extra", "input", "K", "d"),))
    assert FMI.instantiation_token() != before          # content-derived


# ------------------- STACK.md / stack.json as a live authority -------------

STACK_JSON = json.loads((ROOT / "stack.json").read_text())
STACK_MD = (ROOT / "STACK.md").read_text()
REGISTRY = REG.load_registry()


def test_registry_validates_against_its_strict_model():
    """stack.json is a hand-editable trusted boundary; it must validate."""
    assert REGISTRY.schema_version == REG.REGISTRY_SCHEMA_VERSION
    assert REGISTRY.label == LABEL
    assert REGISTRY.automatic_gate_effect == "NONE"
    assert len(REGISTRY.elements) == len(STACK_JSON["elements"])


def test_registry_rejects_edits_that_would_weaken_it():
    from pydantic import ValidationError

    def mutated(**changes):
        doc = json.loads((ROOT / "stack.json").read_text())
        doc.update(changes)
        return doc

    # a dropped or altered claim-boundary label
    with pytest.raises(ValidationError):
        REG.StackRegistry.model_validate(mutated(label="anything else"))
    # a gate effect other than NONE
    with pytest.raises(ValidationError):
        REG.StackRegistry.model_validate(
            mutated(automatic_gate_effect="ADVISORY"))
    # an unknown field slipped in
    with pytest.raises(ValidationError):
        REG.StackRegistry.model_validate(mutated(surprise="value"))
    # an invented status
    doc = json.loads((ROOT / "stack.json").read_text())
    doc["elements"][0]["status"] = "MOSTLY_ADOPTED"
    with pytest.raises(ValidationError):
        REG.StackRegistry.model_validate(doc)
    # a non-adopted element with nothing outstanding
    doc = json.loads((ROOT / "stack.json").read_text())
    for element in doc["elements"]:
        if element["id"] == "fmi":
            element["open_items"] = []
    with pytest.raises(ValidationError):
        REG.StackRegistry.model_validate(doc)


def test_every_declared_stack_element_is_documented():
    """Each registry entry must have a row in the STACK.md ladder table."""
    for element in REGISTRY.elements:
        row = f"| {element.doc_key} |"
        assert row in STACK_MD, f"{element.id} missing from STACK.md"


def test_registry_statuses_match_what_the_code_reports():
    assert REGISTRY.by_id("fenicsx").status == FEM.ADOPTION_STATUS == "STAGED"
    assert REGISTRY.by_id("fmi").status == FMI.ADOPTION_STATUS == "DEFERRED"
    unadopted = {e.id for e in REGISTRY.elements if e.status != "ADOPTED"}
    # rust-selective is ADOPTED_ADMISSION_MECHANISM_ONLY: the bit-parity rule
    # is in force and verified, but no scientific path consumes the kernels.
    # containers is STAGED: ADOPTED requires "in use, exercised by CI or the
    # workflow, and its behaviour is verified in this repository", and the
    # image has never been built or run, so none of the three clauses holds.
    assert unadopted == {"slsa-sigstore", "fenicsx", "fmi", "rust-selective",
                         "containers"}
    assert REGISTRY.by_id("rust-selective").status == \
        "ADOPTED_ADMISSION_MECHANISM_ONLY"


def test_stage10_owner_modules_are_importable():
    import importlib
    for element in REGISTRY.elements:
        if element.owner_module.startswith("qta_multiphysics.stack"):
            mod = importlib.import_module(element.owner_module)
            assert mod.AUTOMATIC_GATE_EFFECT == "NONE"


def test_registry_verification_targets_exist_in_the_workflow():
    snakefile = (ROOT / "Snakefile").read_text()
    for element in REGISTRY.elements:
        command = element.verification.split("#")[0].strip()
        if not command.startswith("snakemake"):
            continue
        target = command.split()[-1]
        if target.startswith("verification/"):
            assert target.split("/")[-1] in snakefile, target
        else:
            assert f"rule {target}:" in snakefile, target


def test_rust_open_item_matches_the_measured_verdict():
    """The documented rejection must track reality, not a stale note."""
    items = REGISTRY.by_id("rust-selective").open_items
    documented_rejection = any("conductivity_power_law" in t for t in items)
    if not RUST.rust_available():
        return          # nothing measured here; the note stands as recorded
    verdicts = {k["kernel"]: k.get("adopted")
                for k in RUST.status_report()["kernels"]}
    assert documented_rejection == (verdicts["conductivity_power_law"]
                                    is False)


# ------------------- §27: adoption truthfulness + RAG completeness ----------
#
# STACK.md and stack.json both listed "Selective Rust | ADOPTED", which reads
# as an active Rust backend, while rust_kernel.py's own status record says "no
# solver imports these kernels yet". And the RAG corpus check asserted only
# that two named files were present and that no excluded directory leaked in --
# it verified neither direction of completeness, so governed evidence could
# silently drop out of the index and unapproved material could silently gain
# retrieval authority.

def test_rust_is_not_presented_as_an_active_scientific_backend():
    import json as _json
    stack = _json.loads((ROOT / "stack.json").read_text(encoding="utf-8"))
    rust = next(e for e in stack["elements"] if e["id"] == "rust-selective")
    assert rust["status"] != "ADOPTED", \
        "bare ADOPTED reads as an active Rust backend"
    assert "admission" in rust["boundary"].lower()
    assert "no active rust scientific backend" in rust["boundary"].lower()
    md = (ROOT / "STACK.md").read_text(encoding="utf-8")
    assert "no scientific path consumes Rust" in md


def test_no_scientific_module_imports_the_rust_kernels():
    """The claim above must stay true, not just be written down."""
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "tools"))
    from repo_scope import files_matching

    # Over tracked AND untracked-unignored files: a new module importing the
    # Rust kernels would otherwise be invisible to this guard until it was
    # committed, which is the blind spot that has cost this repository three
    # red pushes in other guards.
    importers = list(files_matching(r"^\s*import\s+qta_kernels"))
    assert importers == ["qta_multiphysics/stack/rust_kernel.py"], \
        f"unexpected qta_kernels importers: {importers}"


def test_container_is_not_presented_as_runtime_verified():
    """Same failure mode as Selective Rust, caught the same way.

    "Reproducible container | ADOPTED" read as an image that CI builds and
    runs, while no build has ever completed. ADOPTED is defined as "in use,
    exercised by CI or the workflow, and its behaviour is verified in this
    repository" -- the container satisfies none of those three clauses, so the
    row was false by the repository's own vocabulary.
    """
    import json as _json
    stack = _json.loads((ROOT / "stack.json").read_text(encoding="utf-8"))
    c = next(e for e in stack["elements"] if e["id"] == "containers")
    assert c["status"] == "STAGED", \
        "ADOPTED reads as an image CI builds and runs; none has been built"
    assert "never been built or run" in c["boundary"]
    assert "certifies nothing" in c["boundary"]
    md = (ROOT / "STACK.md").read_text(encoding="utf-8")
    assert "| Reproducible container | **STAGED**" in md


def test_container_open_items_state_the_real_blocker():
    """The blocker is egress on layer blobs, not a missing daemon, and not an
    unresolved digest. Both earlier claims were false and must not return."""
    import json as _json
    stack = _json.loads((ROOT / "stack.json").read_text(encoding="utf-8"))
    c = next(e for e in stack["elements"] if e["id"] == "containers")
    items = " ".join(c["open_items"])
    assert "ATTEMPTED_BUT_BLOCKED_BY_BLOB_EGRESS" in items
    assert "RUNTIME_BUILT=NO" in items
    # The digest IS resolved and pinned; claiming otherwise is the stale bug.
    assert "digest still UNRESOLVED" not in items
    md = (ROOT / "STACK.md").read_text(encoding="utf-8")
    assert "base-image digest still `UNRESOLVED`" not in md
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "@sha256:" in dockerfile, "the digest pin must actually be there"


def test_container_doc_does_not_claim_a_missing_daemon():
    """dockerd and containerd run here; only the blob egress is blocked."""
    doc = (ROOT / "container_verification.md").read_text(encoding="utf-8")
    assert "LOCAL_RUNTIME` | `AVAILABLE" in doc
    assert "ATTEMPTED_BUT_BLOCKED_BY_BLOB_EGRESS" in doc
    assert "production.cloudfront.docker.com" in doc
    assert "RUNTIME_SCIENTIFICALLY_REPRODUCED` | `NO" in doc
    # workflow_dispatch reachability must be stated, not assumed away.
    assert "default branch" in doc


def test_fenicsx_is_not_presented_as_certifying_anything():
    import json as _json
    stack = _json.loads((ROOT / "stack.json").read_text(encoding="utf-8"))
    fx = next(e for e in stack["elements"] if e["id"] == "fenicsx")
    assert fx["status"] == "STAGED"
    assert "certifies nothing" in fx["boundary"] or \
        "not an independent benchmark" in fx["boundary"]


def _governed_text_files():
    import subprocess
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    tracked = [f for f in out.split() if f.endswith((".md", ".txt"))]
    return {f for f in tracked
            if not any(part in RAG.EXCLUDED_DIRS
                       for part in pathlib.Path(f).parts)}


def test_rag_indexes_every_governed_document():
    """expected governed evidence  subset-of  RAG index."""
    missing = sorted(_governed_text_files() - set(RAG.corpus_files()))
    assert not missing, f"governed documents absent from the RAG index: {missing}"


def test_rag_indexes_nothing_beyond_governed_documents():
    """RAG indexed entries  subset-of  allowed governed evidence.

    corpus_files() walks the filesystem, so an untracked or unapproved
    document would silently gain retrieval authority.
    """
    extra = sorted(set(RAG.corpus_files()) - _governed_text_files())
    assert not extra, f"unapproved material in the RAG index: {extra}"


def test_rag_corpus_completeness_is_exact_in_both_directions():
    assert set(RAG.corpus_files()) == _governed_text_files()
