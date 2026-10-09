"""The independent verifiers and the FMI boundary, held to what they claim.

Most of this runs anywhere: the slab and RC models, the closed-form series
check, the FMU boundary reader against real and tampered archives, the
FEniCSx lock, the FMU build's determinism. The parts that need an external
runtime -- dolfinx for the finite-element check, fmpy to execute the FMU --
run when one is configured (QTA_FENICSX_PYTHON, QTA_FMI_PYTHON) and are
SKIPPED otherwise; the hosted harness-integrations workflow sets
QTA_INTEGRATIONS_REQUIRED, under which a missing runtime is a FAILURE, so
those jobs cannot pass by skipping.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import fenicsx_env as FE  # noqa: E402
import fmi_build as FBUILD  # noqa: E402

import scientific.models.slab_transient as ST  # noqa: E402
from scientific import fmi_boundary as FB  # noqa: E402
from scientific.checks import fenicsx_slab as FX  # noqa: E402
from scientific.checks import slab_series as SS  # noqa: E402
from scientific.models.thermal_rc2 import (  # noqa: E402
    FMI_VARIABLES, ThermalRC2Model, closed_form,
)
from scientific.verification import Status  # noqa: E402

SLAB = {"L_m": 0.05, "k_W_m_K": 15.0, "rho_c_J_m3_K": 3.6e6,
        "q_W_m3": 2.0e5, "h_W_m2_K": 50.0, "T_inf_K": 300.0, "T0_K": 300.0,
        "t_end_s": 600.0, "n_cells": 40, "n_steps": 120}


def _required(kind: str) -> bool:
    return kind in os.environ.get("QTA_INTEGRATIONS_REQUIRED", "").split(",")


def _runtime(kind: str, var: str) -> str:
    exe = os.environ.get(var, "")
    if exe and Path(exe).is_file():
        return exe
    if _required(kind):
        pytest.fail(f"{kind} is REQUIRED here and {var} names no interpreter")
    pytest.skip(f"no {kind} runtime ({var} unset)")


def _wrong(p, factor_k=1.0, factor_h=1.0):
    """A producer that computes with wrong properties and declares the
    right ones: a wrong result that looks right from outside."""
    orig = ST._solve
    ST._solve = lambda pp, n, m: orig(
        {**pp, "k_W_m_K": pp["k_W_m_K"] * factor_k,
         "h_W_m2_K": pp["h_W_m2_K"] * factor_h}, n, m)
    try:
        return ST.SlabTransientModel().run(p)
    finally:
        ST._solve = orig


@pytest.fixture(scope="module")
def slab_bundle():
    return ST.SlabTransientModel().run(SLAB)


# ---- the slab model ------------------------------------------------------

def test_the_slab_conserves_heat_and_states_its_resolution(slab_bundle):
    assert all(i.holds for i in slab_bundle.invariants)
    for name in ST.SAMPLE_NAMES:
        q = slab_bundle.output(name).quantity
        assert q.resolution_class.value == "DISCRETIZATION_ESTIMATE"
        assert q.resolution > 0


def test_the_slab_estimate_is_honest_against_the_exact_series(slab_bundle):
    xs = [f * SLAB["L_m"] for f in ST.SAMPLE_FRACTIONS]
    exact = SS.series_temperature(SLAB, xs)
    for name, ref in zip(ST.SAMPLE_NAMES, exact):
        q = slab_bundle.output(name).quantity
        assert abs(q.value - ref) <= 3.0 * q.resolution


def test_an_odd_resolution_is_refused():
    with pytest.raises(ValueError, match="even"):
        ST.SlabTransientModel().run({**SLAB, "n_cells": 41})


def test_the_slab_converges_at_second_order():
    xs = [0.0]
    ref = SS.series_temperature(SLAB, xs)[0]
    errs = []
    for n, m in ((20, 60), (40, 120), (80, 240)):
        b = ST.SlabTransientModel().run({**SLAB, "n_cells": n,
                                         "n_steps": m})
        errs.append(abs(b.output("T_at_0").quantity.value - ref))
    orders = [math.log(a / b) / math.log(2) for a, b in zip(errs, errs[1:])]
    assert all(o > 1.8 for o in orders), orders


# ---- the series check ----------------------------------------------------

def test_the_series_check_passes_the_correct_solver(slab_bundle):
    r = SS.run_check(slab_bundle, verifier_id="test")
    assert r.status is Status.PASS
    assert r.measured.value <= 1.0


@pytest.mark.parametrize("fk, fh", [(1.10, 1.0), (1.0, 1.5), (0.95, 1.0)])
def test_the_series_check_rejects_a_wrong_solver(fk, fh):
    bad = _wrong(SLAB, fk, fh)
    assert SS.run_check(bad, verifier_id="test").status is Status.FAIL


def test_the_series_check_refuses_another_model():
    b = ThermalRC2Model().run({"t_end_s": 10.0})
    with pytest.raises(ValueError, match="this check is for"):
        SS.run_check(b, verifier_id="test")


def test_the_series_is_steady_at_long_times():
    p = {**SLAB, "t_end_s": 1e9}
    x = [0.0, SLAB["L_m"]]
    got = SS.series_temperature(p, x)
    L, k, q, h, Ti = (p[n] for n in ("L_m", "k_W_m_K", "q_W_m3", "h_W_m2_K",
                                     "T_inf_K"))
    steady = [Ti + q * L / h + q * L * L / (2 * k), Ti + q * L / h]
    assert got == pytest.approx(steady, rel=1e-12)


# ---- the FEniCSx check without and with dolfinx --------------------------

def test_the_fenicsx_check_is_not_run_without_a_runtime(slab_bundle,
                                                        monkeypatch):
    monkeypatch.delenv(FX.ENV_VAR, raising=False)
    r = FX.run_check(slab_bundle, verifier_id="test")
    assert r.status is Status.NOT_RUN
    assert any("FEniCSx unavailable" in lim for lim in r.limitations)


def test_the_fenicsx_runner_is_identified_by_its_bytes():
    d = FX.check_digest()
    assert len(d) == 64
    assert FX.runner_sha256() == __import__("hashlib").sha256(
        FX.RUNNER.read_bytes()).hexdigest()


def test_fenicsx_agrees_with_the_solver_and_rejects_a_wrong_one(slab_bundle):
    exe = _runtime("fenicsx", FX.ENV_VAR)
    ok = FX.run_check(slab_bundle, verifier_id="test", exe=exe)
    assert ok.status is Status.PASS, ok.to_record()
    assert ok.evidence
    bad = FX.run_check(_wrong(SLAB, 1.10), verifier_id="test", exe=exe)
    assert bad.status is Status.FAIL


def test_fenicsx_reduces_to_the_one_dimensional_slab():
    exe = _runtime("fenicsx", FX.ENV_VAR)
    fem = FX.fem_solution(SLAB, exe=exe)
    assert fem["lateral_max_abs_K"] <= FX.LATERAL_MAX_K
    assert abs(fem["fine"]["energy"]["residual_rel"]) <= 1e-9


# ---- the FEniCSx environment lock ----------------------------------------

def test_the_committed_lock_and_its_sha256_record_agree():
    n = FE.check_lock(FE.LOCK.read_text(), FE.SHA_RECORD.read_text())
    assert n > 100


@pytest.mark.parametrize("line, why", [
    ("https://example.org/linux-64/x-1-0.conda#" + "0" * 32, "conda-forge"),
    ("https://conda.anaconda.org/conda-forge/linux-64/x-1-0.conda",
     "explicit, hashed"),
    ("https://conda.anaconda.org/conda-forge/linux-64/x-1-0.conda#xyz",
     "32 hex"),
])
def test_a_lock_line_outside_the_policy_is_refused(line, why):
    with pytest.raises(FE.EnvError, match=why):
        FE.lock_entries("@EXPLICIT\n" + line + "\n")


def test_a_package_only_in_the_lock_is_refused():
    """Installed but never given a sha256: it would enter unverified."""
    lock = FE.LOCK.read_text() + (
        "https://conda.anaconda.org/conda-forge/linux-64/extra-1-0.conda#"
        + "0" * 32 + "\n")
    with pytest.raises(FE.EnvError, match="only in lock"):
        FE.check_lock(lock, FE.SHA_RECORD.read_text())


def test_a_package_only_in_the_record_is_refused():
    """The record vouches for something the environment does not have: the
    two files have stopped describing one environment."""
    rec = json.loads(FE.SHA_RECORD.read_text())
    extra = dict(rec["packages"][0])
    extra["url"] = extra["url"].replace(".conda", "-extra.conda")
    rec["packages"].append(extra)
    with pytest.raises(FE.EnvError, match="only in record"):
        FE.check_lock(FE.LOCK.read_text(), json.dumps(rec))


def test_a_lock_that_disagrees_with_its_record_is_refused():
    lock = FE.LOCK.read_text()
    first = next(ln for ln in lock.splitlines() if ln.startswith("https://"))
    url, _, md5 = first.partition("#")
    bad = lock.replace(first, url + "#" + ("0" if md5[0] != "0" else "1")
                       + md5[1:])
    with pytest.raises(FE.EnvError, match="md5 differs"):
        FE.check_lock(bad, FE.SHA_RECORD.read_text())


def test_the_cache_check_finds_a_missing_and_a_wrong_package(tmp_path):
    rec = json.loads(FE.SHA_RECORD.read_text())
    rec["packages"] = rec["packages"][:2]
    text = json.dumps(rec)
    pkgs = tmp_path / "pkgs"
    pkgs.mkdir()
    (pkgs / rec["packages"][0]["url"].rsplit("/", 1)[1]).write_bytes(b"x")
    with pytest.raises(FE.EnvError) as exc:
        FE.verify_cache(tmp_path, text)
    assert "missing" in str(exc.value) and "mismatch" in str(exc.value)


# ---- the RC network closed form ------------------------------------------

def test_the_closed_form_starts_at_the_initial_state_and_ends_steady():
    p = ThermalRC2Model().validate({"T1_0_K": 290.0, "T2_0_K": 310.0})
    r0 = closed_form(p, 0.0)
    assert r0["T1"] == pytest.approx(290.0, abs=1e-9)
    assert r0["T2"] == pytest.approx(310.0, abs=1e-9)
    rinf = closed_form(p, 1e9)
    s2 = p["Tamb_K"] + p["Q_W"] / p["G2a_W_K"]
    assert rinf["T2"] == pytest.approx(s2, rel=1e-12)
    assert rinf["T1"] == pytest.approx(s2 + p["Q_W"] / p["G12_W_K"],
                                       rel=1e-12)


def test_the_closed_form_conserves_heat():
    b = ThermalRC2Model().run({"t_end_s": 1234.5})
    assert all(i.holds for i in b.invariants)


# ---- the FMU build and the FMI boundary ----------------------------------

needs_cc = pytest.mark.skipif(shutil.which("cc") is None and
                              shutil.which("gcc") is None,
                              reason="no C compiler")


@pytest.fixture(scope="module")
def fmu_build(tmp_path_factory):
    if shutil.which("cc") is None and shutil.which("gcc") is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("fmu")
    return FBUILD.build(out), FBUILD.build(out / "fault", fault=True), out


@needs_cc
def test_the_fmu_build_is_deterministic_on_one_host(fmu_build, tmp_path):
    rec, fault, _ = fmu_build
    again = FBUILD.build(tmp_path)
    assert again["fmu_sha256"] == rec["fmu_sha256"]
    assert fault["fmu_sha256"] != rec["fmu_sha256"]


def test_a_modified_vendored_header_stops_the_build(tmp_path, monkeypatch):
    hdr = tmp_path / "fmi3"
    shutil.copytree(FBUILD.HEADERS, hdr)
    (hdr / "fmi3Functions.h").write_text("/* changed */\n")
    monkeypatch.setattr(FBUILD, "HEADERS", hdr)
    with pytest.raises(FBUILD.BuildError, match="fmi3Functions.h"):
        FBUILD.check_headers()


@needs_cc
def test_the_boundary_reads_the_real_fmu(fmu_build):
    rec, _, _ = fmu_build
    d = FB.describe(rec["fmu"])
    assert d.archive_sha256 == rec["fmu_sha256"]
    assert d.claim["observation_kind"] == "SIMULATION_RESULT"
    assert d.claim["authority"] == "NON_AUTHORITATIVE"
    assert FB.contract_differences(d, FMI_VARIABLES) == []


def _copy(src, dst, md=None, extra=(), drop_binary=False, link=None):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if drop_binary and info.filename.startswith("binaries/"):
                continue
            if info.filename == "modelDescription.xml" and md:
                data = md(data)
            zout.writestr(info, data)
        for name, data in extra:
            zout.writestr(name, data)
        if link:
            zi = zipfile.ZipInfo(link)
            zi.external_attr = (0o120777 << 16)
            zout.writestr(zi, b"/etc/passwd")
    return dst


@needs_cc
@pytest.mark.parametrize("case, kw, why", [
    ("no_annotation", dict(md=lambda d: d.replace(
        b"org.scientific-ai-harness.claim-boundary", b"other")),
     "claim-boundary"),
    ("measured", dict(md=lambda d: d.replace(
        b'"SIMULATION_RESULT"', b'"RAW_OBSERVATION"')), "SIMULATION_RESULT"),
    ("authority", dict(md=lambda d: d.replace(
        b'"NON_AUTHORITATIVE"', b'"AUTHORITATIVE"')), "no authority"),
    ("fmi2", dict(md=lambda d: d.replace(b'fmiVersion="3.0"',
                                         b'fmiVersion="2.0"')), "3.0"),
    ("undefined_unit", dict(md=lambda d: d.replace(
        b'unit="K" min="0"/>', b'unit="degC" min="0"/>')), "not defined"),
    ("doctype", dict(md=lambda d: d.replace(
        b"<fmiModelDescription", b"<!DOCTYPE a>\n<fmiModelDescription", 1)),
     "DOCTYPE"),
    ("escape", dict(extra=(("../x", b"x"),)), "escapes"),
    ("absolute", dict(extra=(("/abs", b"x"),)), "escapes"),
    ("link", dict(link="binaries/x86_64-linux/evil.so"), "link"),
    ("no_binary", dict(drop_binary=True), "no binary"),
])
def test_the_boundary_refuses_a_tampered_fmu(fmu_build, tmp_path, case, kw,
                                             why):
    rec, _, _ = fmu_build
    dst = _copy(rec["fmu"], tmp_path / f"{case}.fmu", **kw)
    with pytest.raises(FB.FmuRefused, match=why):
        FB.describe(dst)


def test_a_non_archive_is_refused(tmp_path):
    p = tmp_path / "x.fmu"
    p.write_bytes(b"not a zip")
    with pytest.raises(FB.FmuRefused, match="zip"):
        FB.describe(p)


@needs_cc
def test_a_unit_changed_to_another_defined_unit_is_a_contract_difference(
        fmu_build, tmp_path):
    rec, _, _ = fmu_build
    dst = _copy(rec["fmu"], tmp_path / "u.fmu", md=lambda d: d.replace(
        b'name="T2" valueReference="10" causality="output" '
        b'variability="continuous" declaredType="Temperature"',
        b'name="T2" valueReference="10" causality="output" '
        b'variability="continuous" declaredType="Energy"'))
    diffs = FB.contract_differences(FB.describe(dst), FMI_VARIABLES)
    assert diffs == ["T2: unit 'J', declared 'K'"]


# ---- the FMU executed by fmpy --------------------------------------------

@needs_cc
def test_the_fmu_agrees_with_the_closed_form_and_the_fault_does_not(
        fmu_build):
    from scientific.checks import fmu_rc2 as F
    exe = _runtime("fmi", F.ENV_VAR)
    rec, fault, _ = fmu_build
    b = F.fmu_bundle(rec["fmu"], {"t_end_s": 3600.0}, step_s=60.0,
                     build_record=rec, exe=exe)
    assert b.provenance["fmi"]["claim_boundary"]["authority"] == \
        "NON_AUTHORITATIVE"
    assert F.run_check(b, verifier_id="test").status is Status.PASS
    bf = F.fmu_bundle(fault["fmu"], {"t_end_s": 3600.0}, step_s=60.0,
                      build_record=fault, exe=exe)
    assert F.run_check(bf, verifier_id="test").status is Status.FAIL


@needs_cc
def test_the_fmu_state_restores_exactly_and_refuses_bad_steps(fmu_build):
    from scientific.checks import fmu_rc2 as F
    exe = _runtime("fmi", F.ENV_VAR)
    rec, _, _ = fmu_build
    st = F.run_runner({"kind": "state", "fmu": rec["fmu"],
                       "start": {"Q": 10.0}, "n_sub": 2, "step": 30.0,
                       "t_split": 600.0, "t_end": 1200.0}, exe=exe)
    assert st["direct"] == st["from_memory"] == st["from_bytes"]
    assert st["corrupt_serialization_refused"]
    ref = F.run_runner({"kind": "refusals", "fmu": rec["fmu"]}, exe=exe)
    assert all(ref["refused"].values()), ref["refused"]


@needs_cc
def test_a_sha_mismatch_with_the_build_record_is_refused(fmu_build):
    from scientific.checks import fmu_rc2 as F
    rec, fault, _ = fmu_build
    with pytest.raises(FB.FmuRefused, match="build recorded"):
        F.fmu_bundle(fault["fmu"], {"t_end_s": 10.0}, step_s=5.0,
                     build_record=rec, exe=sys.executable)
