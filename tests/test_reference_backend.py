"""The canonical reference backend's recipe and its conformance check.

``tools/reference_backend.py`` refuses a recipe that leaves any part of the
arithmetic to the host (``recipe``), and -- inside the reference runtime --
refuses a backend that is not the declared one (``verify``). Neither is
exercised by building anything here: the recipe tests read the committed
spec.json, build.sh and run.sh, each departure applied to a copy; the
conformance tests take a record with the real probe's shape, made
conformant, and depart from it one way at a time.
"""
from __future__ import annotations

import copy
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "reference_backend_tool", ROOT / "tools" / "reference_backend.py")
rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rb)

SPEC = rb.load_spec()
BUILD = rb.BUILD.read_text(encoding="utf-8")
RUN = rb.RUN.read_text(encoding="utf-8")


def _problems(spec=None, build=None, run=None):
    return rb.recipe_problems(spec or SPEC, BUILD if build is None else build,
                              RUN if run is None else run)


def _with(path, value):
    s = copy.deepcopy(SPEC)
    node = s
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return s


# ---- the recipe ------------------------------------------------------------
def test_the_committed_recipe_fixes_the_arithmetic_path():
    assert _problems() == []


def test_the_command_line_says_so():
    r = subprocess.run([sys.executable, str(ROOT / "tools" /
                                            "reference_backend.py"), "recipe"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fixes the arithmetic path" in r.stdout


@pytest.mark.parametrize("path,value,expect", [
    (("openblas", "DYNAMIC_ARCH"), "1", "DYNAMIC_ARCH is not 0"),
    (("openblas", "TARGET"), "HOST", "not one fixed target"),
    (("openblas", "TARGET"), "", "not one fixed target"),
    (("openblas", "USE_THREAD"), "1", "not single-threaded"),
    (("openblas", "NUM_THREADS"), "64", "not single-threaded"),
    (("openblas", "USE_OPENMP"), "1", "not single-threaded"),
    (("numpy", "cpu-dispatch"), "max -xop -fma4", "runtime dispatch"),
    (("numpy", "cpu-baseline"), "native", "baseline is the host's"),
    (("cflags",), "-O2 -march=native -ffp-contract=off",
     "-march=native appears"),
    (("cflags",), "-O2 -ffast-math -ffp-contract=off",
     "-ffast-math appears"),
    (("openblas", "COMMON_OPT"), "-O2 -Ofast -ffp-contract=off",
     "-Ofast appears"),
    (("cflags",), "-O2 -g0", "cflags does not turn FMA contraction off"),
    (("openblas", "FCOMMON_OPT"), "-O2",
     "FCOMMON_OPT does not turn FMA contraction off"),
    (("threads", "OPENBLAS_NUM_THREADS"), "4", "OPENBLAS_NUM_THREADS is"),
    (("threads", "OMP_NUM_THREADS"), "", "OMP_NUM_THREADS is"),
    (("software_cpu", "cpu"), "host", "passthrough"),
    (("software_cpu", "cpu"), "max", "passthrough"),
    (("software_cpu", "cpu"), "", "passthrough"),
    (("software_cpu", "emulator"), "", "no software CPU emulator"),
    (("fp_policy", "flush_to_zero"), "", "'flush_to_zero' is not stated"),
    (("fp_policy", "libm"), "UNRESOLVED", "'libm' is not stated"),
    (("sources", "numpy", "sha256"), "latest", "numpy is not pinned"),
    (("sources", "python", "binary_sha256"), "",
     "python is not pinned"),
    (("schema",), "canonical-reference-backend/0", "schema is"),
])
def test_a_recipe_that_leaves_the_arithmetic_to_the_host_is_refused(
        path, value, expect):
    problems = _problems(spec=_with(path, value))
    assert any(expect in p for p in problems), problems


def test_a_missing_floating_point_policy_is_refused():
    s = copy.deepcopy(SPEC)
    del s["fp_policy"]["rounding"]
    assert any("'rounding' is not stated" in p for p in _problems(spec=s))


@pytest.mark.parametrize("needle,expect", [
    ('DYNAMIC_ARCH="$QTA_REF_OB_DYNAMIC_ARCH"', "declared DYNAMIC_ARCH"),
    ('TARGET="$QTA_REF_OB_TARGET"', "declared TARGET"),
    ('-Dcpu-dispatch="$QTA_REF_NP_CPU_DISPATCH"', "declared dispatch"),
    ('-Dcpu-baseline="$QTA_REF_NP_CPU_BASELINE"', "declared baseline"),
    ("sha256sum -c", "does not verify what it fetches"),
    ('fetch "$QTA_REF_SCIPY_URL" "$QTA_REF_SCIPY_SHA256"',
     "does not fetch scipy against its pin"),
    ('fetch "$QTA_REF_ROOTFS_URL" "$QTA_REF_ROOTFS_SHA256"',
     "does not fetch rootfs against its pin"),
])
def test_a_build_script_that_does_not_apply_the_spec_is_refused(
        needle, expect):
    assert needle in BUILD
    problems = _problems(build=BUILD.replace(needle, "X"))
    assert any(expect in p for p in problems), problems


@pytest.mark.parametrize("needle,expect", [
    ('-cpu "$QTA_REF_CPU"', "declared software CPU"),
    ('-L "$QTA_REF_PREFIX/rootfs"', "pinned root filesystem"),
    ("env -i", "inherits the caller's environment"),
])
def test_a_run_script_that_does_not_apply_the_spec_is_refused(needle, expect):
    assert needle in RUN
    problems = _problems(run=RUN.replace(needle, "X"))
    assert any(expect in p for p in problems), problems


def test_a_fetch_that_stops_verifying_is_refused_though_others_verify():
    """``sha256sum -c`` elsewhere in the script -- the interpreter check --
    does not make fetch() verify."""
    line = '  echo "$2  $3" | sha256sum -c --quiet - || die'
    assert line in BUILD
    build = BUILD.replace(line, "  true || die")
    assert "sha256sum -c" in rb.shell_code(build)
    assert any("does not verify what it fetches" in p
               for p in _problems(build=build))


def test_a_source_fetched_against_another_pin_is_refused():
    build = BUILD.replace('fetch "$QTA_REF_NUMPY_URL" "$QTA_REF_NUMPY_SHA256"',
                          'fetch "$QTA_REF_NUMPY_URL" "$(curl -s pin)"')
    assert any("does not fetch numpy against its pin" in p
               for p in _problems(build=build))


def test_an_unverified_interpreter_is_refused():
    build = BUILD.replace('echo "$QTA_REF_PYTHON_SHA256  ', 'echo "  ')
    assert any("does not verify the interpreter" in p
               for p in _problems(build=build))


# ---- the pinned source patches ---------------------------------------------
def test_every_patch_is_pinned_explained_and_present():
    assert SPEC["patches"], "the recipe declares its OpenBLAS patch"
    for pt in SPEC["patches"]:
        assert rb.sha256(ROOT / pt["path"]) == pt["sha256"]
        assert pt["applies_to"] == "openblas" and pt["reason"]


@pytest.mark.parametrize("field,value,expect", [
    ("sha256", "0" * 64, "does not have its pinned sha256"),
    ("sha256", "latest", "is not pinned and explained"),
    ("reason", "", "is not pinned and explained"),
    ("applies_to", "numpy", "is not pinned and explained"),
])
def test_a_patch_that_is_not_the_pinned_one_is_refused(field, value, expect):
    s = copy.deepcopy(SPEC)
    s["patches"][0][field] = value
    assert any(expect in p for p in _problems(spec=s)), _problems(spec=s)


def test_a_patch_applied_without_verification_is_refused():
    line = ('    echo "$ps  $pf" | sha256sum -c --quiet - || die "$pf does not '
            'have the pinned sha256"\n')
    assert line in BUILD
    assert any("verify each patch" in p
               for p in _problems(build=BUILD.replace(line, "")))


def test_the_patches_reach_the_build_script():
    env = rb.shell_env(SPEC)
    assert env["QTA_REF_PATCH_COUNT"] == str(len(SPEC["patches"]))
    assert env["QTA_REF_PATCH_0_SHA256"] == SPEC["patches"][0]["sha256"]


def test_a_run_script_that_passes_the_host_cpu_through_is_refused():
    run = RUN.replace('-cpu "$QTA_REF_CPU"', '-cpu "$QTA_REF_CPU" -cpu max')
    assert any("passes the host CPU through" in p
               for p in _problems(run=run))


def test_a_forbidden_flag_in_the_build_script_is_refused():
    build = BUILD.replace('export CFLAGS="$FLAGS"',
                          'export CFLAGS="$FLAGS -march=native"')
    assert build != BUILD
    assert any("-march=native appears" in p for p in _problems(build=build))


def test_a_forbidden_flag_named_only_in_a_comment_is_not_a_use_of_it():
    """build.sh documents that it never uses -march=native; saying so is not
    using it."""
    assert "-march=native" in BUILD
    assert "-march=native" not in rb.shell_code(BUILD)
    build = BUILD + "\n  # -ffast-math would break IEEE semantics\n"
    assert _problems(build=build) == []


def test_every_variable_the_scripts_read_is_one_the_spec_supplies():
    """build.sh and run.sh run under ``set -u``: a QTA_REF_ variable the
    spec does not supply stops them, and one supplied but mistyped in a
    script is a variable nobody set."""
    supplied = set(rb.shell_env(SPEC))
    for name, text in (("build.sh", BUILD), ("run.sh", RUN)):
        used = set(re.findall(r"\$\{?!?(QTA_REF_[A-Z0-9_]+)", text))
        used |= set(re.findall(r'var="(QTA_REF_[A-Z0-9_]+)', text))
        used -= {"QTA_REF_JOBS", "QTA_REF_TOOL_"}
        assert used and used <= supplied, (name, sorted(used - supplied))


def test_the_env_command_quotes_what_it_exports():
    r = subprocess.run([sys.executable, str(ROOT / "tools" /
                                            "reference_backend.py"), "env"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    got = subprocess.run(["bash", "-c", r.stdout + '\nprintf %s "$QTA_REF_CFLAGS"'],
                         capture_output=True, text=True, timeout=60)
    assert got.stdout == SPEC["cflags"]


# ---- conformance, inside the reference runtime -----------------------------
PY_SHA = SPEC["sources"]["python"]["binary_sha256"]
ROOTFS = {"libc.so.6": "c" * 64, "libm.so.6": "d" * 64,
          "ld-linux-x86-64.so.2": "e" * 64}


def _conformant():
    """A probe record with the real probe's shape, as the reference runtime
    should produce it."""
    return {
        "python": "3.12.11", "implementation": "cpython", "machine": "x86_64",
        "distributions": {"numpy": "2.4.4", "scipy": "1.17.1"},
        "cpu": {"source": "cpuinfo", "simd": ["avx512f", "avx2"],
                "core": {"vendor_id": "GenuineIntel", "model": "207"}},
        "backend": {"cpu_count": 4,
                    "variables": dict(SPEC["threads"],
                                      NPY_DISABLE_CPU_FEATURES=None)},
        "native": {"numpy": {"status": "RESOLVED", "native_sha256": "1" * 64},
                   "scipy": {"status": "RESOLVED", "native_sha256": "2" * 64}},
        "runtime": {
            "status": "RESOLVED", "unresolved": [],
            "numpy": {"version": "2.4.4",
                      "cpu_features": ["SSE", "SSE2", "SSE3", "SSSE3",
                                       "SSE41", "POPCNT", "SSE42"],
                      "simd": {"baseline": ["X86_V2"], "found": [],
                               "not found": None},
                      "dispatch": {"functions": 477,
                                   "targets": {"baseline(X86_V2)": 477},
                                   "sha256": "3" * 64}},
            "blas": {"build": {"blas": {"name": "openblas"}},
                     "bundled": [
                         {"distribution": d, "name": "libopenblas-x.so.0",
                          "sha256": "4" * 64,
                          "kernel": SPEC["expected_runtime"]["openblas_kernel"],
                          "config": "OpenBLAS 0.3.31 NO_AFFINITY NEHALEM "
                                    "MAX_THREADS=1",
                          "threads": 1, "parallel": 0}
                         for d in ("numpy", "scipy")]},
            "system_libraries": {
                "c": {"name": "libc.so.6", "sha256": ROOTFS["libc.so.6"]},
                "m": {"name": "libm.so.6", "sha256": ROOTFS["libm.so.6"]},
                "gomp": None, "gfortran": None,
                "loader": {"name": "ld-linux-x86-64.so.2",
                           "sha256": ROOTFS["ld-linux-x86-64.so.2"]}},
            "interpreter": {"name": "python3.12", "sha256": PY_SHA}},
        "backend_status": "RESOLVED"}


FP_OK = {"subnormal_survives": True, "ties_to_even": True,
         "compiled_dispatch": []}


def _conf(record=None, fp=None, rootfs=ROOTFS):
    return rb.conformance(SPEC, record or _conformant(), fp or FP_OK, rootfs)


def test_the_fixture_has_the_real_probe_s_shape():
    """The record these tests depart from is shaped like what the probe
    returns, key for key, so a renamed field breaks here, not silently."""
    sys.path.insert(0, str(ROOT))
    from scientific.backend_probe import run_environment
    real = run_environment(distributions=("numpy", "scipy"))
    fake = _conformant()

    def keys(a, b, where=""):
        assert set(a) == set(b), (where, sorted(set(a) ^ set(b)))
        for k in ("runtime", "numpy", "simd", "dispatch", "blas",
                  "system_libraries", "interpreter", "backend"):
            if k in a and isinstance(a[k], dict) and isinstance(b[k], dict):
                keys(a[k], b[k], f"{where}.{k}")
    keys(real, fake)
    assert set(real["runtime"]["blas"]["bundled"][0]) == \
        set(fake["runtime"]["blas"]["bundled"][0])


def test_a_conformant_runtime_passes():
    assert _conf() == []


def _depart(fn):
    r = _conformant()
    fn(r)
    return r


def _set(path, value):
    def fn(r):
        node = r
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
    return fn


BL = ("runtime", "blas", "bundled")


@pytest.mark.parametrize("change,expect", [
    (_set(("backend_status",), "UNRESOLVED"), "not RESOLVED"),
    (_set(("runtime",), None), "not RESOLVED"),
    (_set(("runtime", "numpy", "dispatch", "targets"),
          {"baseline(X86_V2)": 400, "AVX2": 77}), "outside the baseline"),
    (_set(("runtime", "numpy", "dispatch", "targets"), {}),
     "outside the baseline"),
    (_set(("runtime", "numpy", "dispatch"), None), "outside the baseline"),
    (_set(("runtime", "numpy", "simd", "baseline"), ["X86_V3"]),
     "baseline is"),
    (_set(("runtime", "numpy", "cpu_features"), ["SSE42", "AVX2"]),
     "the CPU offers AVX2"),
    (_set(("runtime", "numpy", "cpu_features"), ["SSE42", "FMA3"]),
     "the CPU offers FMA3"),
    (_set(BL, []), "no OpenBLAS was identified"),
    (_set(BL + (0, "kernel"), "Haswell"), "runs Haswell"),
    (_set(BL + (1, "kernel"), "SkylakeX"), "runs SkylakeX"),
    (_set(BL + (0, "threads"), 4), "runs 4 threads"),
    (_set(BL + (1, "parallel"), 1), "threading model is 1"),
    (_set(BL + (0, "config"), "OpenBLAS 0.3.31 DYNAMIC_ARCH Nehalem"),
     "built with DYNAMIC_ARCH"),
    (_set(("backend", "variables", "OPENBLAS_NUM_THREADS"), "4"),
     "OPENBLAS_NUM_THREADS is '4'"),
    (_set(("runtime", "interpreter", "sha256"), "f" * 64),
     "interpreter is not the pinned one"),
    (_set(("runtime", "interpreter"), None),
     "interpreter is not the pinned one"),
    (_set(("runtime", "system_libraries", "m", "sha256"), "f" * 64),
     "libm is not the pinned"),
    (_set(("runtime", "system_libraries", "c", "sha256"), "f" * 64),
     "libc is not the pinned"),
    (_set(("runtime", "system_libraries", "loader"), None),
     "lib is not the pinned root filesystem's ld-linux"),
])
def test_a_runtime_that_is_not_the_declared_one_is_refused(change, expect):
    problems = _conf(_depart(change))
    assert any(expect in p for p in problems), problems


@pytest.mark.parametrize("fp,expect", [
    (dict(FP_OK, subnormal_survives=False), "subnormals are flushed"),
    (dict(FP_OK, ties_to_even=False), "not to nearest, ties to even"),
    (dict(FP_OK, compiled_dispatch=["AVX2"]), "built with dispatch targets"),
])
def test_a_floating_point_environment_that_departs_is_refused(fp, expect):
    assert any(expect in p for p in _conf(fp=fp))


@pytest.mark.parametrize("rootfs", [None, {}])
def test_without_the_build_record_the_libraries_are_not_assumed(rootfs):
    assert any("cannot be checked" in p for p in _conf(rootfs=rootfs))


def test_a_thread_variable_the_probe_did_not_record_is_not_as_declared():
    r = _conformant()
    del r["backend"]["variables"]["GOTO_NUM_THREADS"]
    assert any("GOTO_NUM_THREADS is None" in p for p in _conf(r))


def test_the_system_libraries_are_checked_against_the_rootfs_s_bytes():
    other = dict(ROOTFS, **{"libm.so.6": "a" * 64})
    assert any("libm is not" in p for p in _conf(rootfs=other))


def test_the_measured_floating_point_environment_here_is_ieee():
    fp = rb.fp_environment()
    assert fp["subnormal_survives"] and fp["ties_to_even"]


# ---- the identity two hosts must share -------------------------------------
def test_the_host_s_cpuinfo_is_not_part_of_the_reference_identity():
    """Under qemu-user /proc/cpuinfo is the physical host's: two hosts
    running the same software CPU see different cpuinfo and the same CPUID.
    The identity keeps what the dispatchers saw and drops cpuinfo."""
    a, b = _conformant(), _conformant()
    b["cpu"] = {"source": "cpuinfo", "simd": ["sse2"],
                "core": {"vendor_id": "AuthenticAMD", "model": "1"}}
    b["backend"]["cpu_count"] = 64
    assert rb.digest(rb.reference_identity(SPEC, a)) == \
        rb.digest(rb.reference_identity(SPEC, b))


@pytest.mark.parametrize("change", [
    _set(("runtime", "numpy", "cpu_features"), ["SSE42", "AVX"]),
    _set(("runtime", "numpy", "dispatch", "sha256"), "9" * 64),
    _set(BL + (0, "sha256"), "9" * 64),
    _set(("runtime", "system_libraries", "m", "sha256"), "9" * 64),
    _set(("runtime", "interpreter", "sha256"), "9" * 64),
    _set(("native", "scipy", "native_sha256"), "9" * 64),
    _set(("distributions", "scipy"), "1.17.0"),
    _set(("backend", "variables", "GOTO_NUM_THREADS"), "2"),
])
def test_what_the_arithmetic_depends_on_is_part_of_it(change):
    a = rb.digest(rb.reference_identity(SPEC, _conformant()))
    b = rb.digest(rb.reference_identity(SPEC, _depart(change)))
    assert a != b


def test_the_spec_is_part_of_the_identity():
    other = _with(("software_cpu", "cpu"), "Westmere-v1")
    assert rb.digest(rb.reference_identity(SPEC, _conformant())) != \
        rb.digest(rb.reference_identity(other, _conformant()))


def test_a_different_rootfs_is_a_different_identity():
    other = _with(("sources", "rootfs", "sha256"), "0" * 64)
    assert rb.digest(rb.reference_identity(SPEC, _conformant())) != \
        rb.digest(rb.reference_identity(other, _conformant()))


# ---- verify: the command run inside the reference runtime --------------------
def _verify(monkeypatch, tmp_path, capsys, record, *, build_record=True):
    import json
    import scientific.backend_probe as bp
    spec = dict(SPEC, prefix=str(tmp_path))
    if build_record:
        (tmp_path / "BUILD_RECORD.json").write_text(
            json.dumps({"rootfs_libraries": ROOTFS}))
    monkeypatch.setattr(rb, "load_spec", lambda *a: spec)
    monkeypatch.setattr(rb, "fp_environment", lambda: dict(FP_OK))
    monkeypatch.setattr(bp, "run_environment", lambda **kw: record)
    rc = rb.main(["verify"])
    return rc, json.loads(capsys.readouterr().out), spec


def test_verify_certifies_a_conformant_runtime_and_names_its_identity(
        monkeypatch, tmp_path, capsys):
    rc, out, spec = _verify(monkeypatch, tmp_path, capsys, _conformant())
    assert rc == 0 and out["conformance"] == []
    assert out["identity_digest"] == rb.digest(
        rb.reference_identity(spec, _conformant()))


def test_verify_refuses_a_runtime_that_is_not_the_declared_one(
        monkeypatch, tmp_path, capsys):
    r = _depart(_set(BL + (0, "kernel"), "Haswell"))
    rc, out, _ = _verify(monkeypatch, tmp_path, capsys, r)
    assert rc == 1 and out["conformance"]
    assert out["identity"] is None and out["identity_digest"] is None


def test_verify_without_the_build_record_refuses(monkeypatch, tmp_path,
                                                 capsys):
    rc, out, _ = _verify(monkeypatch, tmp_path, capsys, _conformant(),
                         build_record=False)
    assert rc == 1 and any("cannot be checked" in p
                           for p in out["conformance"])


def test_the_generator_starts_no_process_the_software_cpu_would_not_run():
    """qemu-user emulates the process it starts; a child that process
    exec()s is an x86-64 binary the kernel runs on the PHYSICAL CPU. So the
    generator the reference runs -- its measured closure, from the witness
    profile -- may not start processes, or part of the corpus would be
    computed outside the reference boundary with nothing recording it."""
    import json
    prof = json.loads((ROOT / "docs" / "byte_reproduction_profile.json")
                      .read_text(encoding="utf-8"))
    closure = [f for f in prof["generator"]["closure"] if f.endswith(".py")]
    assert closure
    spawning = re.compile(r"\b(subprocess|multiprocessing|os\.system|"
                          r"os\.exec\w*|os\.spawn\w*|os\.fork|os\.popen|"
                          r"ProcessPoolExecutor|pty\.spawn)\b")
    found = {f: sorted(set(spawning.findall(
        (ROOT / f).read_text(encoding="utf-8")))) for f in closure}
    assert not {f: v for f, v in found.items() if v}


def test_the_reference_is_staged_not_the_corpus_backend():
    """Nothing here migrates the canonical corpus (directive 7, s.35)."""
    assert SPEC["status"].startswith("STAGED")
    assert "not ground truth" in SPEC["purpose"]


# ---- the two-physical-host acceptance test (directive 7 s.34) ---------------
CPUINFO_INTEL = ("processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\n"
                 "model\t\t: 207\nmodel name\t: Intel(R) Xeon(R)\n"
                 "flags\t\t: fpu sse2 avx avx2 avx512f fma\n\n"
                 "processor\t: 1\nvendor_id\t: GenuineIntel\n")
CPUINFO_AMD = ("processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 25\n"
               "model\t\t: 1\nmodel name\t: AMD EPYC 7763\n"
               "flags\t\t: fpu sse2 avx avx2 fma\n")
DECLARED = ["a.json", "b.csv", "stub.json"]
EXEMPT = ["stub.json"]


def _host_run(cpuinfo=CPUINFO_INTEL, **over):
    rec = rb.host_record(SPEC, {"conformance": [], "identity_digest": "i" * 64},
                         {"a.json": "1" * 64, "b.csv": "2" * 64,
                          "stub.json": "3" * 64},
                         rb.physical_host(cpuinfo), "e" * 64, "g" * 64)
    rec.update(over)
    return rec


def _two(a, b):
    return rb.two_host(a, b, DECLARED, EXEMPT)


def test_the_physical_host_is_read_from_the_first_processor():
    h = rb.physical_host(CPUINFO_INTEL)
    assert (h["vendor"], h["family"], h["model"]) == ("GenuineIntel", "6",
                                                      "207")
    assert h["avx512f"] and h["flags_sha256"]
    assert rb.host_class(h) != rb.host_class(rb.physical_host(CPUINFO_AMD))


def test_the_marketing_name_is_not_the_cpu_class():
    other = CPUINFO_INTEL.replace("Intel(R) Xeon(R)", "Some Other Name")
    assert rb.host_class(rb.physical_host(CPUINFO_INTEL)) == \
        rb.host_class(rb.physical_host(other))


def test_two_cpu_classes_one_identity_same_bytes_is_the_proof():
    v, problems = _two(_host_run(), _host_run(CPUINFO_AMD))
    assert v == rb.TWO_HOST_IDENTICAL and problems == []


def test_one_cpu_class_twice_proves_nothing():
    assert _two(_host_run(), _host_run())[0] == "NOT_TWO_HOSTS"


def test_a_feature_set_alone_makes_a_different_class():
    no512 = CPUINFO_INTEL.replace(" avx512f", "")
    assert _two(_host_run(), _host_run(no512))[0] == rb.TWO_HOST_IDENTICAL


def test_one_differing_byte_fails_and_is_not_normalised():
    b = _host_run(CPUINFO_AMD)
    b["outputs"] = dict(b["outputs"], **{"b.csv": "f" * 64})
    v, problems = _two(_host_run(), b)
    assert v == "BYTES_DIFFER" and "b.csv" in problems[0]


def test_a_file_missing_on_both_hosts_is_not_agreement():
    a, b = _host_run(), _host_run(CPUINFO_AMD)
    for r in (a, b):
        r["outputs"] = {k: v for k, v in r["outputs"].items() if k != "a.json"}
    assert _two(a, b)[0] == "BYTES_DIFFER"


def test_an_exempt_file_may_differ():
    b = _host_run(CPUINFO_AMD)
    b["outputs"] = dict(b["outputs"], **{"stub.json": "f" * 64})
    assert _two(_host_run(), b)[0] == rb.TWO_HOST_IDENTICAL


def test_different_reference_identities_name_a_hidden_input():
    b = _host_run(CPUINFO_AMD, identity_digest="j" * 64)
    assert _two(_host_run(), b)[0] == "REFERENCE_IDENTITY_DIFFERS"


@pytest.mark.parametrize("over,expect", [
    ({"conformance": ["NumPy dispatched outside the baseline"]}, "REFUSED"),
    ({"conformance": None}, "REFUSED"),
    ({"identity_digest": None}, "REFUSED"),
    ({"physical_host": {}}, "REFUSED"),
    ({"schema": "reference-host-run/0"}, "REFUSED"),
    ({"spec_digest": "0" * 64}, "DIFFERENT_RECIPES"),
    ({"emulator_sha256": "d" * 64}, "DIFFERENT_EMULATORS"),
    ({"emulator_sha256": None}, "DIFFERENT_EMULATORS"),
    ({"generator_digest": "h" * 64}, "DIFFERENT_GENERATORS"),
    ({"generator_digest": None}, "DIFFERENT_GENERATORS"),
])
def test_a_run_that_is_not_comparable_is_never_a_proof(over, expect):
    assert _two(_host_run(), _host_run(CPUINFO_AMD, **over))[0] == expect
    assert _two(_host_run(CPUINFO_AMD, **over), _host_run())[0] == expect


def test_no_declared_outputs_is_refused():
    assert rb.two_host(_host_run(), _host_run(CPUINFO_AMD), [], [])[0] == \
        "REFUSED"


def test_the_two_host_command_exits_zero_only_on_the_proof(tmp_path):
    import json
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_host_run()))
    b.write_text(json.dumps(_host_run()))
    r = subprocess.run([sys.executable, str(ROOT / "tools" /
                                            "reference_backend.py"),
                        "two-host", str(a), str(b)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and "NOT_TWO_HOSTS" in r.stdout


# ---- host A, and the workflow that is host B --------------------------------
HOST_A = ROOT / "docs" / "reference_backend_host_a.json"


def test_host_a_is_a_conforming_run_of_this_recipe():
    """The committed host-A record is what the hosted job compares against;
    a recipe changed without re-running host A would make the two-host job
    compare two different recipes (it would say DIFFERENT_RECIPES after
    hours of building). Fail here instead, in seconds."""
    import json
    a = json.loads(HOST_A.read_text(encoding="utf-8"))
    assert a["schema"] == rb.HOST_SCHEMA
    assert a["spec_digest"] == rb.digest(SPEC)
    assert a["conformance"] == [] and len(a["identity_digest"]) == 64
    assert a["physical_host"]["flags_sha256"] and a["emulator_sha256"]
    # and of THIS generator: a changed closure re-records host A
    assert a["generator_digest"] == rb.generator_digest()
    sys.path.insert(0, str(ROOT / "tools"))
    import cross_env_semantics
    declared, exempt = cross_env_semantics.declared_scope(ROOT)
    assert set(declared) <= set(a["outputs"]), \
        sorted(set(declared) - set(a["outputs"]))


def test_the_hosted_job_is_host_b_against_host_a():
    """Read as text, not parsed: the check runs in the minimal environment,
    which has no YAML parser (D-2026-74's control counts every importer)."""
    text = (ROOT / ".github" / "workflows" / "reference-backend.yml"
            ).read_text(encoding="utf-8")
    code = rb.shell_code(text)                     # comments say nothing
    assert re.search(r'^  push:\n    paths:\n(?:      - "[^"]+"\n)*'
                     r'      - "reference_backend/\*\*"\n', code, re.M), \
        "the job does not run when the recipe changes"
    flat = " ".join(code.replace("\\\n", " ").split())
    for needle in ("bash reference_backend/build.sh",
                   "reference_backend/run.sh tools/reference_backend.py verify",
                   "reference_backend/run.sh qta_full_sim.py",
                   "tools/reference_backend.py host-record",
                   "tools/reference_backend.py two-host "
                   "docs/reference_backend_host_a.json"):
        assert needle in flat, needle
    assert "continue-on-error" not in flat
    assert "|| true" not in flat
