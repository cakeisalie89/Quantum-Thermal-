"""A run on a different numeric backend is a different run.

The run identity's environment was the interpreter and the numpy and scipy
VERSIONS, read from metadata. R59 is the counterexample it could not see:
the same versions on a CPU with a different SIMD set dispatch a different
kernel and change digits. So could a different thread count (the order of a
reduction), a dispatch override, or another native build of the same
version. Reuse was within one history on one machine, which kept this from
biting; it is still the identity's job to say what "the same computation"
is, not the deployment's to avoid asking.

Each part of the backend is now in the environment record, and each is
shown here to change the RunIdentity -- and to make ``may_reuse`` refuse,
naming the environment -- while what changes no kernel (a mitigation flag,
an unrelated variable) does not. Nothing imports numpy to find out.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scientific import run_identity as ri  # noqa: E402
from scientific.identity import digest  # noqa: E402

X86 = """processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 207
model name\t: Some Xeon
flags\t\t: fpu vme sse sse2 ssse3 sse4_1 sse4_2 popcnt avx avx2 fma f16c \
avx512f avx512cd avx512bw avx512vl hypervisor md_clear
"""

ARM = """processor\t: 0
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics asimdhp \
asimddp sve
CPU implementer\t: 0x41
CPU architecture: 8
CPU part\t: 0xd0c
"""

B = "b" * 64


def _identity(env: dict) -> ri.RunIdentity:
    return ri.RunIdentity(model_id="m", model_version="1",
                          implementation_digest=B, parameter_digest=B,
                          environment_digest=ri.environment_digest(env))


def _record(**kw):
    kw.setdefault("environ", {})
    kw.setdefault("cpuinfo", X86)
    return ri.environment_record(**kw)


def test_the_cpu_is_read_as_a_kernel_sees_it():
    cpu = ri.cpu_record(X86)
    assert cpu["simd"] == sorted(["sse", "sse2", "ssse3", "sse4_1",
                                  "sse4_2", "popcnt", "avx", "avx2", "fma",
                                  "f16c", "avx512f", "avx512cd", "avx512bw",
                                  "avx512vl"])
    assert cpu["core"] == {"vendor_id": "GenuineIntel", "cpu family": "6",
                           "model": "207"}
    arm = ri.cpu_record(ARM)
    assert arm["simd"] == ["asimd", "asimddp", "asimdhp", "sve"]
    assert arm["core"]["CPU part"] == "0xd0c"


def _refused(a: dict, b: dict) -> str:
    ok, why = ri.may_reuse(_identity(a), _identity(b),
                           prior_evidence_intact=True)
    assert not ok
    return why


@pytest.mark.parametrize("change", [
    "a SIMD feature", "another core", "a thread count", "a dispatch override",
    "a core-type override", "another native build", "an unreadable CPU"])
def test_a_changed_backend_changes_the_run_identity(change, monkeypatch):
    base = _record()
    if change == "a SIMD feature":
        other = _record(cpuinfo=X86.replace(" avx512f", ""))
    elif change == "another core":
        other = _record(cpuinfo=X86.replace("207", "143"))
    elif change == "a thread count":
        other = _record(environ={"OPENBLAS_NUM_THREADS": "4"})
    elif change == "a dispatch override":
        other = _record(environ={"NPY_DISABLE_CPU_FEATURES": "AVX512F"})
    elif change == "a core-type override":
        other = _record(environ={"OPENBLAS_CORETYPE": "Haswell"})
    elif change == "another native build":
        real = ri.native_record
        monkeypatch.setattr(
            ri, "native_record",
            lambda d: {**real(d), "native_sha256": "0" * 64})
        other = _record()
    else:
        other = _record(cpuinfo=None)
    assert ri.environment_digest(other) != ri.environment_digest(base)
    assert _identity(other).digest() != _identity(base).digest()
    assert "environment_digest" in _refused(base, other)


@pytest.mark.parametrize("change", ["a mitigation flag", "a variable no "
                                    "kernel reads"])
def test_what_changes_no_kernel_does_not_change_it(change):
    base = _record()
    other = (_record(cpuinfo=X86.replace(" md_clear", " md_clear retbleed"))
             if change == "a mitigation flag"
             else _record(environ={"HOME": "/elsewhere"}))
    assert ri.environment_digest(other) == ri.environment_digest(base)
    ok, _ = ri.may_reuse(_identity(base), _identity(other),
                         prior_evidence_intact=True)
    assert ok


def test_the_native_build_is_read_from_the_wheel_record():
    """numpy's compiled code -- its extension modules and the BLAS it
    bundles -- by the digests its wheel recorded."""
    rec = ri.native_record("numpy")
    assert rec["native_files"] > 0
    assert len(rec["native_sha256"]) == 64
    assert ri.native_record("no-such-distribution") == {"status": "ABSENT"}


def _fake_files(so_hash: str):
    import importlib.metadata as md
    files = []
    for name, h in (("pkg/core.cpython-312-x86_64-linux-gnu.so", so_hash),
                    ("pkg.libs/libopenblas-abc.so.0.3", "b" * 8),
                    ("pkg/__init__.py", "c" * 8)):
        f = md.PackagePath(name)
        f.hash = md.FileHash(f"sha256={h}")
        files.append(f)
    return files


def test_the_native_digest_is_of_the_recorded_compiled_code(monkeypatch):
    """Same version, another build: one extension module's recorded hash
    differs, and so does the record. Pure-Python files are not native code
    and are not counted."""
    import importlib.metadata as md
    monkeypatch.setattr(md, "files", lambda d: _fake_files("a" * 8))
    one = ri.native_record("pkg")
    monkeypatch.setattr(md, "files", lambda d: _fake_files("d" * 8))
    other = ri.native_record("pkg")
    assert one["native_files"] == other["native_files"] == 2
    assert one["native_sha256"] != other["native_sha256"]


def test_the_environment_is_read_without_importing_the_numeric_stack():
    """The scientific core states what it runs on without loading numpy to
    ask -- in a fresh interpreter, where nothing else has loaded it."""
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from scientific.run_identity import environment_record\n"
            "r = environment_record()\n"
            "assert r['native']['numpy']['native_files'] > 0\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('numpy', 'scipy')))" % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True, timeout=120)
    assert out.stdout.strip() == "[]", out.stdout


def test_an_unreadable_cpu_is_reused_only_on_its_own_host():
    rec = ri.cpu_record(None)
    assert rec["source"] == "UNAVAILABLE" and "host" in rec


def test_the_record_is_the_digest_s_input():
    env = _record()
    assert ri.environment_digest(env) == digest(env)
