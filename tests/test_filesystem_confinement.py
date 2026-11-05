"""Filesystem confinement, observed rather than reported (D-2026-157).

Finding R-3: a tool's reads were confined by nothing but the process's
credentials. Measured at b9b2187 (tests/test_execution_containment.py), a
tool read /etc/hostname and /etc/passwd. The executor now has the namespace's
init restrict itself with Landlock before it forks the tool, and prove the
restriction by failing to open a probe path the policy does not grant.

Every case here is an OBSERVED OUTCOME: what the tool could open, not what
the record says it could. Each has a control that differs only in what makes
it hostile. The modes:

* ``research`` confines where the host can. Where it cannot, the tool runs
  and the execution record says it was not confined, and why.
* ``high_assurance`` confines, or the tool does not run.

On a host whose kernel offers no Landlock or no unprivileged namespaces, the
confined cases SKIP with that reason, and HARNESS_ISOLATION_REQUIRED=1 turns
the skip into a failure, as for the namespace cases.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_substrate.execution as X  # noqa: E402
from agent_substrate.execution import Limits, Outcome, run_bounded  # noqa: E402
from agent_substrate.tools import Determinism, Field_, ToolSpec  # noqa: E402

PY = sys.executable
_PREFIX, _ISOLATION = X.namespace_isolation()


def _landlock_abi() -> int:
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(444, None, ctypes.c_size_t(0),
                            ctypes.c_uint32(1)))


_ABI = _landlock_abi()
_WHY = ("DEPLOYMENT_REQUIRED: " + (_ISOLATION["detail"] if not _PREFIX
                                   else f"no Landlock (abi {_ABI})"))
needs_confinement = pytest.mark.skipif(not _PREFIX or _ABI < 1, reason=_WHY)


def test_confinement_is_present_where_it_is_required():
    if os.environ.get("HARNESS_ISOLATION_REQUIRED") == "1":
        assert _PREFIX and _ABI >= 1, _WHY
    else:
        pytest.skip("HARNESS_ISOLATION_REQUIRED is not set")


def _spec() -> ToolSpec:
    return ToolSpec(tool_id="hostile", version="1.0", summary="hostile",
                    inputs=(Field_("n", "int"),),
                    determinism=Determinism.BYTE_IDENTICAL,
                    writable_scope=("out",), timeout_s=10.0)


def _run(tmp_path: Path, code: str, *, confine: str = "research", **limits):
    (tmp_path / "out").mkdir(exist_ok=True)
    return run_bounded([PY, "-c", code], spec=_spec(), cwd=tmp_path,
                       limits=Limits(wall_seconds=10.0, confine=confine,
                                     **limits),
                       env={"PATH": "/usr/bin:/bin"})


def _opens(path) -> str:
    """Tool code: print OPENED or DENIED for ``path``."""
    return ("import sys\n"
            f"try:\n    open({str(path)!r}, 'rb').read(1)\n"
            "    print('OPENED')\n"
            "except PermissionError:\n    print('DENIED')\n")


def _fs(r) -> dict:
    return r.isolation["filesystem"]


# ---- what a confined tool can open ------------------------------------------

@needs_confinement
def test_a_file_beside_the_workspace_is_refused(tmp_path):
    (tmp_path / "sentinel.txt").write_text("not yours")
    r = _run(tmp_path, _opens(tmp_path / "sentinel.txt"))
    assert r.outcome is Outcome.COMPLETED, r.reason
    assert "DENIED" in r.stdout_excerpt
    assert _fs(r)["applied"].startswith("landlock")


@needs_confinement
def test_control_a_file_in_the_writable_scope_opens(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "mine.txt").write_text("yours")
    r = _run(tmp_path, _opens(tmp_path / "out" / "mine.txt"))
    assert "OPENED" in r.stdout_excerpt, r.stderr_excerpt


@needs_confinement
def test_the_hosts_configuration_is_refused(tmp_path):
    """R-3's reproduction, inverted: /etc/hostname and /etc/passwd were read
    at b9b2187."""
    for path in ("/etc/hostname", "/etc/passwd"):
        if not Path(path).exists():
            continue
        r = _run(tmp_path, _opens(path))
        assert "DENIED" in r.stdout_excerpt, (path, r.stdout_excerpt)


@needs_confinement
def test_control_unconfined_the_hosts_configuration_opens(tmp_path):
    r = _run(tmp_path, _opens("/etc/passwd"), isolate=False)
    assert "OPENED" in r.stdout_excerpt
    assert _fs(r)["applied"] is None


@needs_confinement
def test_a_symlink_in_the_workspace_does_not_reach_outside_it(tmp_path):
    (tmp_path / "sentinel.txt").write_text("not yours")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "link").symlink_to(tmp_path / "sentinel.txt")
    r = _run(tmp_path, _opens(tmp_path / "out" / "link"))
    assert "DENIED" in r.stdout_excerpt, r.stdout_excerpt


@needs_confinement
def test_a_file_on_another_mount_is_refused(tmp_path):
    shm = Path("/dev/shm")
    if not shm.is_dir():
        pytest.skip("no /dev/shm on this host")
    other = shm / f"confinement-{uuid.uuid4().hex}"
    other.write_text("another filesystem")
    try:
        r = _run(tmp_path, _opens(other))
        assert "DENIED" in r.stdout_excerpt, r.stdout_excerpt
    finally:
        other.unlink()


@needs_confinement
def test_a_write_outside_the_scope_is_refused_not_only_detected(tmp_path):
    """Before, an undeclared write was found afterwards by an inventory.
    Now it is refused at the open."""
    code = ("try:\n    open('stray.txt', 'w').write('x')\n    print('WROTE')\n"
            "except PermissionError:\n    print('DENIED')\n")
    r = _run(tmp_path, code)
    assert "DENIED" in r.stdout_excerpt
    assert not (tmp_path / "stray.txt").exists()


@needs_confinement
def test_control_a_write_inside_the_scope_lands(tmp_path):
    r = _run(tmp_path, "open('out/ok.txt', 'w').write('x')\n")
    assert r.outcome is Outcome.COMPLETED, r.reason
    assert (tmp_path / "out" / "ok.txt").read_text() == "x"


@needs_confinement
def test_a_grandchild_that_leaves_the_session_is_still_confined(tmp_path):
    (tmp_path / "sentinel.txt").write_text("not yours")
    code = ("import os, sys\n"
            "sys.stdout.flush()\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.setsid()\n"
            "    try:\n"
            f"        open({str(tmp_path / 'sentinel.txt')!r}).read()\n"
            "        print('OPENED', flush=True)\n"
            "    except PermissionError:\n"
            "        print('DENIED', flush=True)\n"
            "    os._exit(0)\n"
            "os.waitpid(pid, 0)\n")
    r = _run(tmp_path, code)
    assert "DENIED" in r.stdout_excerpt, r.stdout_excerpt


@needs_confinement
def test_the_network_is_still_unreachable_under_confinement(tmp_path):
    code = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
            "    print('CONNECTED')\n"
            "except OSError:\n"
            "    print('NO ROUTE')\n")
    r = _run(tmp_path, code)
    assert "NO ROUTE" in r.stdout_excerpt


# ---- a sandbox too narrow fails loudly; a grant widens it ---------------------

@needs_confinement
def test_a_tool_needing_an_ungranted_path_fails_with_the_denial(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "data.json").write_text('{"x": 1}')
    code = "import json\nprint(json.load(open('inputs/data.json'))['x'])\n"
    r = _run(tmp_path, code)
    assert r.outcome is Outcome.FAILED
    assert "PermissionError" in r.stderr_excerpt


@needs_confinement
def test_control_the_same_path_granted_is_read(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "data.json").write_text('{"x": 1}')
    code = "import json\nprint(json.load(open('inputs/data.json'))['x'])\n"
    r = _run(tmp_path, code, read_paths=("inputs",))
    assert r.outcome is Outcome.COMPLETED, r.stderr_excerpt
    assert r.stdout_excerpt.strip() == "1"
    assert str(tmp_path / "inputs") in _fs(r)["policy"]["read"]


# ---- the modes: never a silent downgrade --------------------------------------

def _no_namespaces(monkeypatch):
    monkeypatch.setattr(X, "namespace_isolation", lambda: (
        (), {"namespaces": [], "detail": "unshare refused: simulated"}))


def test_high_assurance_without_namespaces_does_not_run_the_tool(
        tmp_path, monkeypatch):
    _no_namespaces(monkeypatch)
    r = _run(tmp_path, "open('out/ran.txt', 'w').write('x')\n",
             confine="high_assurance")
    assert r.outcome is Outcome.FAILED
    assert "did not run" in r.reason and "simulated" in r.reason
    assert not (tmp_path / "out" / "ran.txt").exists()


def test_research_without_namespaces_runs_and_says_it_was_not_confined(
        tmp_path, monkeypatch):
    _no_namespaces(monkeypatch)
    r = _run(tmp_path, "open('out/ran.txt', 'w').write('x')\n")
    assert r.outcome is Outcome.COMPLETED, r.reason
    assert _fs(r)["applied"] is None
    assert "simulated" in _fs(r)["detail"]


def test_high_assurance_refuses_a_run_that_asks_for_no_isolation(tmp_path):
    r = _run(tmp_path, "open('out/ran.txt', 'w').write('x')\n",
             confine="high_assurance", isolate=False)
    assert r.outcome is Outcome.FAILED and "did not run" in r.reason
    assert not (tmp_path / "out" / "ran.txt").exists()


@needs_confinement
def test_a_confinement_that_cannot_be_shown_is_not_reported(tmp_path):
    """A FALSE ISOLATION REPORT. Granting the working directory grants the
    probe, so the restriction cannot be shown to be in force. Research mode
    runs the tool and records it as unconfined; it does not say
    "confined"."""
    r = _run(tmp_path, "print('ran')\n", read_paths=(".",))
    assert r.outcome is Outcome.COMPLETED, r.reason
    assert _fs(r)["applied"] is None
    assert "readable after restriction" in _fs(r)["detail"]


@needs_confinement
def test_high_assurance_does_not_run_a_tool_it_cannot_show_is_confined(
        tmp_path):
    r = _run(tmp_path, "open('out/ran.txt', 'w').write('x')\n",
             confine="high_assurance", read_paths=(".",))
    assert r.outcome is Outcome.FAILED
    assert "readable after restriction" in r.reason
    assert not (tmp_path / "out" / "ran.txt").exists()


@needs_confinement
def test_control_high_assurance_runs_a_confinable_tool(tmp_path):
    r = _run(tmp_path, "open('out/ran.txt', 'w').write('x')\n",
             confine="high_assurance")
    assert r.outcome is Outcome.COMPLETED, r.reason
    assert _fs(r)["applied"].startswith("landlock")
    assert (tmp_path / "out" / "ran.txt").exists()


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="confine must be one of"):
        Limits(confine="permissive")


def test_the_policy_grants_what_it_says_and_nothing_wider(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "data").mkdir()
    probe = tmp_path / ".exec-x" / "probe"
    pol = X.confinement_policy(cwd=tmp_path, writable_scope=("out",),
                               read_paths=(), probe=probe)
    assert str(tmp_path / "pkg") in pol["read"]
    assert str(tmp_path / "data") not in pol["read"]
    assert str(tmp_path) not in pol["read"]
    assert pol["write"] == [str(tmp_path / "out"), "/dev/null"]
    assert pol["list"] == [str(tmp_path)]
    assert pol["probe"] == str(probe)
    json.dumps(pol)


# ---- an interpreter installed outside /usr --------------------------------
#
# Found by hosted CI at 5e31ae0: the second-interpreter job runs on a
# uv-managed CPython under the runner's home, and every confined run there
# exited 127 -- "cannot execute ...: Permission denied". The kernel opens an
# ELF's loader FOR EXECUTION, and the policy granted the interpreter's own
# trees and nothing of the system's libraries. Here the interpreter lives
# under /usr, which granted them by accident.

def _loader(executable: str) -> str | None:
    """The ELF interpreter (PT_INTERP) the kernel opens to start
    ``executable``, or None for anything but a little-endian ELF64."""
    import struct
    with open(os.path.realpath(executable), "rb") as f:
        head = f.read(64)
        if head[:4] != b"\x7fELF" or head[4] != 2 or head[5] != 1:
            return None
        phoff, = struct.unpack_from("<Q", head, 0x20)
        phentsize, phnum = struct.unpack_from("<HH", head, 0x36)
        for i in range(phnum):
            f.seek(phoff + i * phentsize)
            p_type, _, p_offset = struct.unpack("<IIQ", f.read(16))
            if p_type == 3:                                   # PT_INTERP
                f.seek(p_offset)
                return f.read(256).split(b"\0", 1)[0].decode()
    return None


def _granted(path: str, grants) -> bool:
    real = os.path.realpath(path)
    return any(real == os.path.realpath(g)
               or real.startswith(os.path.realpath(g).rstrip("/") + "/")
               for g in grants)


def test_the_loader_is_granted_for_an_interpreter_outside_usr(
        tmp_path, monkeypatch):
    loader = _loader(PY)
    if loader is None:
        pytest.skip("this interpreter is not a dynamically linked ELF64")
    for name in ("base_prefix", "prefix", "exec_prefix", "base_exec_prefix"):
        monkeypatch.setattr(sys, name, "/opt/elsewhere/python")
    pol = X.confinement_policy(cwd=tmp_path, writable_scope=(),
                               read_paths=(), probe=tmp_path / "p")
    assert _granted(loader, pol["read"]), (loader, pol["read"])
    # Read and execute, never write.
    assert not _granted(loader, pol["write"])


def _standalone() -> str | None:
    """An interpreter whose real path is outside /usr, if this host has one:
    the CI second interpreter's shape."""
    import glob
    cands = [os.environ.get("HARNESS_STANDALONE_PYTHON", ""), PY]
    for pattern in ("~/.local/share/uv/python/*/bin/python3",
                    "/opt/*/python/*/bin/python3"):
        cands += sorted(glob.glob(os.path.expanduser(pattern)))
    for c in cands:
        if c and os.path.isfile(c) and not os.path.realpath(c).startswith(
                "/usr/"):
            return c
    return None


@needs_confinement
def test_a_standalone_interpreter_runs_confined_and_stays_confined():
    """The deployment probe, run BY such an interpreter: it starts, and every
    denial the probe expects is still observed."""
    import subprocess
    interp = _standalone()
    if interp is None:
        pytest.skip("no interpreter outside /usr on this host; the CI "
                    "second-interpreter job runs on one")
    out = subprocess.run([interp, "tools/confinement_probe.py"], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    doc = json.loads(out.stdout)
    assert doc["result"] == "PASS", (interp, doc["failures"], out.stderr)


@needs_confinement
def test_a_confined_run_fingerprints_the_same_runtime_as_an_unconfined_one(
        tmp_path):
    """The run identity is what decides whether two runs are "the same
    run". Measured while this was written: with /dev/null granted read-only,
    ctypes.util.find_library failed inside the confinement, libc and libm
    came out UNRESOLVED, and every confined run carried a different
    environment digest from the same code unconfined."""
    import subprocess

    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT),
           "PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "1",
           "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "NUMEXPR_NUM_THREADS": "1"}
    code = ("import json, sys\n"
            "from scientific.backend_probe import run_environment\n"
            "open(sys.argv[1], 'w').write(json.dumps(run_environment(),"
            " sort_keys=True, default=str))\n")
    (tmp_path / "out").mkdir()
    spec = ToolSpec(tool_id="fingerprint", version="1.0", summary="probe",
                    inputs=(Field_("n", "int"),),
                    determinism=Determinism.BYTE_IDENTICAL,
                    writable_scope=(str(tmp_path / "out"),), timeout_s=60.0)
    r = run_bounded([PY, "-c", code, str(tmp_path / "out" / "c.json")],
                    spec=spec, cwd=ROOT, limits=Limits(wall_seconds=60.0),
                    env=env)
    assert r.outcome is Outcome.COMPLETED, r.stderr_excerpt
    assert _fs(r)["applied"].startswith("landlock")
    subprocess.run([PY, "-c", code, str(tmp_path / "plain.json")], env=env,
                   check=True, timeout=120)
    confined = json.loads((tmp_path / "out" / "c.json").read_text())
    plain = json.loads((tmp_path / "plain.json").read_text())
    assert confined == plain
    assert confined["backend_status"] == "RESOLVED"


@needs_confinement
def test_a_declared_output_is_writable_without_a_scope(tmp_path):
    """An EXTERNAL tool declares no local scope, and its contract still
    names the file it produces; that file's directory is granted."""
    (tmp_path / "res").mkdir()
    (tmp_path / "other").mkdir()
    spec = ToolSpec(tool_id="ext", version="1.0", summary="ext",
                    inputs=(Field_("n", "int"),),
                    determinism=Determinism.BYTE_IDENTICAL, timeout_s=10.0)
    code = ("open('res/a.json', 'w').write('{}')\n"
            "try:\n    open('other/b.json', 'w').write('{}')\n"
            "    print('WROTE OTHER')\n"
            "except PermissionError:\n    print('DENIED OTHER')\n")
    r = run_bounded([PY, "-c", code], spec=spec, cwd=tmp_path,
                    limits=Limits(wall_seconds=10.0),
                    env={"PATH": "/usr/bin:/bin"},
                    collect=(("artifact", "res/a.json", True),))
    assert r.outcome is Outcome.COMPLETED, r.reason
    assert "DENIED OTHER" in r.stdout_excerpt
    assert (tmp_path / "res" / "a.json").exists()
    assert not (tmp_path / "other" / "b.json").exists()


@needs_confinement
def test_a_temporary_file_lands_in_the_runs_own_directory(tmp_path):
    """A tool may make temporary files -- the runtime fingerprint does, when
    the loader cache does not name a library -- and they go to a directory
    private to the run. The host's /tmp, where every other run's files are,
    stays refused."""
    code = ("import os, tempfile\n"
            "with tempfile.NamedTemporaryFile() as f:\n"
            "    print('TMP', os.path.dirname(f.name))\n"
            "try:\n"
            "    open('/tmp/confinement-stray', 'w').write('x')\n"
            "    print('HOST TMP WROTE')\n"
            "except PermissionError:\n"
            "    print('HOST TMP DENIED')\n")
    r = _run(tmp_path, code)
    assert r.outcome is Outcome.COMPLETED, r.stderr_excerpt
    assert "HOST TMP DENIED" in r.stdout_excerpt
    assert f"TMP {_fs(r)['tmpdir']}" in r.stdout_excerpt
    assert _fs(r)["tmpdir"].startswith(str(tmp_path))
