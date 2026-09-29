"""The backend a run actually used, and reuse refused where it is not known
(D-2026-94).

Three things the run identity could not say before, each held here:

* the native build by its INSTALLED bytes, not only by what the wheel's
  RECORD says was shipped -- a library replaced after installation changed
  nothing the identity looked at;
* what the numeric stack DECIDED at runtime -- NumPy's dispatched CPU
  features, the BLAS kernel OpenBLAS selected, the system C and math
  runtimes the loader resolves -- probed in the worker, never by the
  top-level interfaces;
* that an UNRESOLVED backend is recomputed, never reused, even against an
  identical identity: "same unknown" is not "same".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scientific import backend_probe as bp  # noqa: E402
from scientific import run_identity as ri  # noqa: E402
from scientific.run_identity import (  # noqa: E402
    RunIdentity, environment_digest, environment_record, may_reuse,
)

Z = "0" * 64


def _identity(**kw):
    base = dict(model_id="m", model_version="1", implementation_digest=Z,
                parameter_digest=Z, environment_digest=Z,
                backend_status="RESOLVED")
    return RunIdentity(**{**base, **kw})


# ---- installed bytes ---------------------------------------------------------
def test_the_native_build_is_read_by_its_installed_bytes_too():
    rec = ri.native_record("numpy")
    assert rec["status"] == "RESOLVED", rec
    assert rec["native_files"] > 0
    assert rec["installed_sha256"] != rec["native_sha256"], (
        "two different claims about the same files, hashed differently")
    assert rec["unreadable"] == [] and rec["modified"] == []


def test_a_library_replaced_after_installation_changes_the_identity(
        monkeypatch):
    before = ri.native_record("numpy")
    real = ri._installed_sha256
    victim = []

    def tampered(path):
        sha = real(path)
        if not victim and str(path).endswith(".so"):
            victim.append(str(path))
            return "f" * 64
        return sha
    monkeypatch.setattr(ri, "_installed_sha256", tampered)
    after = ri.native_record("numpy")
    assert after["status"] == "MODIFIED"
    assert after["native_sha256"] == before["native_sha256"], (
        "RECORD did not change -- which is exactly why RECORD alone could "
        "not see it")
    assert after["installed_sha256"] != before["installed_sha256"]
    assert len(after["modified"]) == 1


def test_an_unreadable_native_file_is_unresolved_not_skipped(monkeypatch):
    real = ri._installed_sha256
    hit = []

    def unreadable(path):
        if not hit and str(path).endswith(".so"):
            hit.append(path)
            return None
        return real(path)
    monkeypatch.setattr(ri, "_installed_sha256", unreadable)
    rec = ri.native_record("numpy")
    assert rec["status"] == "UNRESOLVED" and len(rec["unreadable"]) == 1


def test_installed_bytes_are_hashed_once_per_file_per_process(tmp_path):
    f = tmp_path / "lib.so"
    f.write_bytes(b"x" * 10)
    first = ri._installed_sha256(f)
    assert ri._installed_sha256(f) == first
    f.write_bytes(b"y" * 11)
    assert ri._installed_sha256(f) != first, "a changed file is re-read"


# ---- the runtime probe ---------------------------------------------------------
@pytest.fixture(scope="module")
def runtime():
    return bp.runtime_record()


def test_the_probe_reports_what_the_stack_decided(runtime):
    assert runtime["status"] == bp.RESOLVED, runtime["unresolved"]
    assert runtime["numpy"]["cpu_features"], "dispatched features are named"
    blas = runtime["blas"]
    if "openblas" in str(blas["build"]["blas"]["name"]):
        mine = [b for b in blas["bundled"] if b["distribution"] == "numpy"]
        assert len(mine) == 1, "NumPy's own OpenBLAS, found by its RECORD"
    for lib in blas["bundled"]:
        assert lib["kernel"] not in (None, bp.UNRESOLVED), lib
        assert len(lib["sha256"]) == 64
    import importlib.util
    if importlib.util.find_spec("scipy") is not None:
        assert any(b["distribution"] == "scipy" for b in blas["bundled"]), (
            "scipy.linalg runs on SciPy's own OpenBLAS, which is recorded too")
    for lib in ("c", "m"):
        assert len(runtime["system_libraries"][lib]["sha256"]) == 64


def test_the_probe_is_the_same_whatever_else_the_process_loaded(runtime):
    """Order-independence: an identity is computed in one process and the run
    in another, and a run loads more of the stack than an identity check."""
    import scipy.integrate  # noqa: F401
    import scipy.linalg  # noqa: F401
    import scipy.sparse  # noqa: F401
    assert bp.runtime_record() == runtime
    # and in a process that loaded SciPy's BLAS FIRST -- the order that made
    # a probe reading the process's mappings name the wrong library
    first = ("import sys, json; sys.path.insert(0, %r);"
             "import scipy.linalg;"
             "from scientific import backend_probe as bp;"
             "print(json.dumps(bp.runtime_record(), sort_keys=True))"
             % str(ROOT))
    out = subprocess.run([sys.executable, "-c", first], capture_output=True,
                         text=True, timeout=120, check=True)
    assert json.loads(out.stdout) == runtime
    code = ("import sys, json; sys.path.insert(0, %r);"
            "from scientific import backend_probe as bp;"
            "print(json.dumps(bp.runtime_record(), sort_keys=True))"
            % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120, check=True)
    assert json.loads(out.stdout) == runtime


def test_importing_the_probe_imports_no_numpy():
    code = ("import sys; sys.path.insert(0, %r);"
            "import scientific.backend_probe, scientific.run_identity;"
            "import scientific.model;"
            "print(sorted(m for m in ('numpy', 'scipy') if m in sys.modules))"
            % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120, check=True)
    assert out.stdout.strip() == "[]", out.stdout


@pytest.mark.parametrize("change, part", [
    ({"numpy": "cpu_features"}, "a dispatched CPU feature"),
    ({"numpy": "dispatch"}, "one function's dispatch choice"),
    ({"blas": "kernel"}, "a BLAS kernel"),
    ({"blas": "sha256"}, "a BLAS library's bytes"),
    ({"blas": "threads"}, "a BLAS thread count"),
    ({"system_libraries": "m"}, "the math library's bytes"),
])
def test_each_part_of_the_runtime_changes_the_environment(runtime, change,
                                                          part):
    (section, key), = change.items()
    other = json.loads(json.dumps(runtime))
    if section == "numpy" and key == "dispatch":
        other["numpy"]["dispatch"]["sha256"] = "e" * 64
    elif section == "numpy":
        other["numpy"]["cpu_features"] = other["numpy"]["cpu_features"][:-1]
    elif section == "blas" and key == "threads":
        other["blas"]["bundled"][-1]["threads"] += 1
    elif section == "blas":
        other["blas"]["bundled"][-1][key] = (
            "Nehalem" if key == "kernel" else "e" * 64)
    else:
        other["system_libraries"]["m"] = dict(other["system_libraries"]["m"],
                                              sha256="e" * 64)
    a = environment_digest(environment_record(runtime=runtime))
    b = environment_digest(environment_record(runtime=other))
    assert a != b, f"{part} did not reach the environment digest"


# ---- what NumPy dispatched, and how many threads the BLAS runs ---------------
def _probe_in(env_extra: dict) -> dict:
    import os
    code = ("import sys, json; sys.path.insert(0, %r);"
            "from scientific import backend_probe as bp;"
            "print(json.dumps(bp.runtime_record(), sort_keys=True))"
            % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120, check=True,
                         env=dict(os.environ, **env_extra))
    return json.loads(out.stdout)


def test_the_probe_records_what_numpy_dispatched(runtime):
    d = runtime["numpy"]["dispatch"]
    assert d["functions"] > 0
    assert sum(d["targets"].values()) == d["functions"]
    assert len(d["sha256"]) == 64


def test_the_dispatch_record_follows_the_runtime_choice_not_the_cpu(runtime):
    """Disable every dispatch target this host selected: the CPU still
    offers the features -- ``cpu_features`` may still list them -- but the
    dispatcher chooses the baseline, and the record says so."""
    chosen = [t for t in runtime["numpy"]["dispatch"]["targets"]
              if not t.startswith("baseline")]
    if not chosen:
        pytest.skip("this host dispatches nothing above the baseline")
    rec = _probe_in({"NPY_DISABLE_CPU_FEATURES": " ".join(chosen)})
    assert rec["status"] == bp.RESOLVED, rec["unresolved"]
    d = rec["numpy"]["dispatch"]
    assert all(t.startswith("baseline") for t in d["targets"]), d["targets"]
    assert d["sha256"] != runtime["numpy"]["dispatch"]["sha256"]


def test_the_dispatch_digest_is_over_each_choice(monkeypatch):
    """Two processes that dispatch the same functions to different targets
    have different records -- a digest over the function names alone would
    call them the same."""
    import numpy.lib.introspect as intro
    fake = {"add": {"dd": {"current": "X86_V4", "available": ["X86_V4"]}},
            "sqrt": {"d": {"current": "X86_V3", "available": ["X86_V3"]}}}
    monkeypatch.setattr(intro, "opt_func_info", lambda: fake)
    a = bp._numpy_dispatch()
    fake["add"]["dd"]["current"] = "X86_V3"
    b = bp._numpy_dispatch()
    assert a["functions"] == b["functions"] == 2
    assert a["sha256"] != b["sha256"]
    assert b["targets"] == {"X86_V3": 2}


def test_a_numpy_that_cannot_say_what_it_dispatched_is_unresolved(
        monkeypatch):
    monkeypatch.setattr(bp, "_numpy_dispatch", lambda: None)
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert "numpy runtime dispatch" in rec["unresolved"]
    assert environment_record(runtime=rec)["backend_status"] == "UNRESOLVED"


def test_an_empty_dispatch_table_is_not_an_answer(monkeypatch):
    import numpy.lib.introspect as intro
    monkeypatch.setattr(intro, "opt_func_info", lambda: {})
    assert bp._numpy_dispatch() is None


def test_the_blas_thread_count_is_what_the_library_answers(runtime):
    for lib in runtime["blas"]["bundled"]:
        assert isinstance(lib["threads"], int) and lib["threads"] >= 1, lib
        assert isinstance(lib["parallel"], int), lib
    one = _probe_in({"OPENBLAS_NUM_THREADS": "1"})
    assert [b["threads"] for b in one["blas"]["bundled"]] == [
        1 for _ in one["blas"]["bundled"]]


def test_a_blas_that_cannot_say_its_threads_is_unresolved(monkeypatch):
    monkeypatch.setattr(bp, "_THREADS", ("no_such_symbol",))
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert "numpy blas threads" in rec["unresolved"]


def test_the_interpreter_is_recorded_by_its_bytes(runtime):
    import hashlib
    import os
    exe = os.path.realpath(sys.executable)
    interp = runtime["interpreter"]
    assert interp["name"] == os.path.basename(exe)
    with open(exe, "rb") as fh:
        assert interp["sha256"] == hashlib.sha256(fh.read()).hexdigest()


def test_an_interpreter_that_cannot_be_read_is_unresolved(monkeypatch):
    import os
    exe = os.path.realpath(sys.executable)
    real = bp.installed_sha256
    monkeypatch.setattr(bp, "installed_sha256",
                        lambda p: None if os.path.realpath(str(p)) == exe
                        else real(p))
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert "interpreter bytes" in rec["unresolved"]


def test_glibc_tunables_is_part_of_the_backend():
    """glibc's libm picks its FMA/AVX2 variants by IFUNC; a tunable that
    masks a feature changes which variant computes exp and log."""
    assert "GLIBC_TUNABLES" in ri.BACKEND_VARIABLES
    a = environment_record(environ={})
    b = environment_record(environ={"GLIBC_TUNABLES":
                                    "glibc.cpu.hwcaps=-AVX2_Usable"})
    assert b["backend"]["variables"]["GLIBC_TUNABLES"]
    assert environment_digest(a) != environment_digest(b)


def test_a_bundled_blas_that_does_not_name_its_kernel_is_unresolved(
        monkeypatch):
    monkeypatch.setattr(bp, "_CORENAME", ("no_such_symbol",))
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert "numpy blas kernel" in rec["unresolved"]
    assert environment_record(runtime=rec)["backend_status"] == "UNRESOLVED"



@pytest.mark.parametrize("name", ["mkl", "accelerate", "blas", "openblas"])
def test_a_blas_the_probe_cannot_identify_is_unresolved(monkeypatch, name):
    """MKL, Accelerate, a system BLAS -- or OpenBLAS with no bundled copy to
    name -- does the linear algebra unnamed: UNRESOLVED, never assumed."""
    real_build, real_bundled = bp._build_blas, bp._bundled_blas
    monkeypatch.setattr(bp, "_build_blas", lambda np: {
        "blas": {"name": name, "version": "x"},
        "lapack": {"name": name, "version": "x"}})
    if name == "openblas":
        monkeypatch.setattr(bp, "_bundled_blas", lambda: [
            b for b in real_bundled() if b["distribution"] != "numpy"])
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert any(u.startswith("numpy blas") for u in rec["unresolved"])
    assert any(u.startswith("numpy lapack") for u in rec["unresolved"])
    monkeypatch.setattr(bp, "_build_blas", real_build)
    monkeypatch.setattr(bp, "_bundled_blas", real_bundled)
    assert bp.runtime_record()["status"] == bp.RESOLVED


def test_a_numpy_with_no_external_blas_has_nothing_to_name(monkeypatch):
    monkeypatch.setattr(bp, "_build_blas", lambda np: {
        "blas": {"name": "none", "version": None},
        "lapack": {"name": None, "version": None}})
    assert bp.runtime_record()["status"] == bp.RESOLVED


def test_a_scipy_without_a_bundled_blas_is_unresolved(monkeypatch):
    real = bp._bundled_blas
    monkeypatch.setattr(bp, "_bundled_blas", lambda: [
        b for b in real() if b["distribution"] != "scipy"])
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert any("scipy's BLAS" in u for u in rec["unresolved"])

def test_a_bundled_blas_that_cannot_be_read_is_unresolved(monkeypatch):
    real = bp.installed_sha256
    monkeypatch.setattr(bp, "installed_sha256", lambda p: None
                        if "openblas" in str(p) else real(p))
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert "numpy blas sha256" in rec["unresolved"]


def test_a_missing_math_library_is_unresolved(monkeypatch):
    real = bp._loader_path
    monkeypatch.setattr(bp, "_loader_path",
                        lambda n: None if n == "m" else real(n))
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED and "libm" in rec["unresolved"]
    assert rec["system_libraries"]["m"] == bp.UNRESOLVED


def test_the_dynamic_loader_is_recorded_by_its_bytes(runtime):
    loader = runtime["system_libraries"]["loader"]
    assert loader["name"].startswith(bp.LOADER_PREFIXES)
    assert len(loader["sha256"]) == 64


def test_a_loader_that_cannot_be_found_is_unresolved(monkeypatch):
    monkeypatch.setattr(bp, "_dynamic_loader", lambda: None)
    rec = bp.runtime_record()
    assert rec["status"] == bp.UNRESOLVED
    assert "dynamic loader" in rec["unresolved"]
    assert rec["system_libraries"]["loader"] == bp.UNRESOLVED


def test_a_process_without_numpy_has_no_numpy_backend_and_says_so(
        monkeypatch):
    """A model that never touches NumPy, in an environment without it, has
    an identity: its backend is the interpreter and the system libraries,
    RESOLVED -- not a crash, and not an unresolved NumPy."""
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda n, *a: None if n == "numpy" else real(n, *a))
    rec = bp.runtime_record()
    assert rec["numpy"] is None and rec["blas"]["build"] is None
    assert rec["status"] == bp.RESOLVED, rec["unresolved"]
    assert len(rec["system_libraries"]["m"]["sha256"]) == 64


def test_a_maps_line_that_is_not_a_mapping_is_skipped():
    maps = ("garbage\n"
            "7f4-7f5 r-xp 0 08:01 3 libm.so.6\n"
            "7f6-7f7 r-xp 0 08:01 4 /opt/not-libm.so.6.bak\n"
            "7f0-7f1 r-xp 0 08:01 1 /usr/lib/libm.so.6\n"
            "7f2-7f3 r-xp 0 08:01 2\n"
            "\x00\x01 nonsense /etc/passwd\n")
    assert bp._path_for_soname(maps, "libm.so.6") == "/usr/lib/libm.so.6"
    assert bp._path_for_soname(maps, "libc.so.6") is None


# ---- backend status and reuse ----------------------------------------------------
def test_without_a_probe_the_backend_is_unresolved():
    assert environment_record()["backend_status"] == "UNRESOLVED"


def test_with_a_resolved_probe_it_is_resolved(runtime):
    assert environment_record(runtime=runtime)["backend_status"] == "RESOLVED"


def test_a_modified_native_build_leaves_it_unresolved(runtime, monkeypatch):
    monkeypatch.setattr(ri, "native_record",
                        lambda d: {"status": "MODIFIED"})
    assert environment_record(runtime=runtime)["backend_status"] == (
        "UNRESOLVED")


def test_an_unresolved_backend_is_recomputed_even_when_identical():
    unknown = _identity(backend_status="UNRESOLVED")
    ok, why = may_reuse(unknown, unknown, prior_evidence_intact=True)
    assert not ok and "UNRESOLVED" in why
    ok, why = may_reuse(_identity(), unknown, prior_evidence_intact=True)
    assert not ok and "current" in why
    ok, why = may_reuse(unknown, _identity(), prior_evidence_intact=True)
    assert not ok and "prior" in why


def test_a_resolved_identical_backend_may_be_reused_and_a_different_one_not():
    assert may_reuse(_identity(), _identity(), prior_evidence_intact=True) == (
        True, "")
    ok, why = may_reuse(_identity(), _identity(environment_digest="1" * 64),
                        prior_evidence_intact=True)
    assert not ok and "environment_digest" in why


def test_backend_status_is_part_of_the_identity():
    assert _identity().digest() != _identity(
        backend_status="UNRESOLVED").digest()
    assert _identity().to_record()["backend_status"] == "RESOLVED"


def test_a_governed_identity_is_resolved_and_its_bundle_agrees(runtime):
    """The worker's identity and the bundle it produces describe the SAME
    environment: both go through the one helper that probes the runtime."""
    from scientific.catalog import models
    from scientific.model import run_identity_for, run_model
    model = models().lookup("thermal.conduction_1d", "1.0.0")
    params = model.validate({})
    ident = run_identity_for(model, params)
    assert ident.backend_status == "RESOLVED"
    bundle = run_model(model, {})
    assert bundle.environment_digest == ident.environment_digest
