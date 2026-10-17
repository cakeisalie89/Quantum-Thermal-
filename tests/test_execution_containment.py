"""An uncooperative tool, against what the executor enforces.

D-2026-149 (findings R-1, R-2). run_bounded's bounds were a session, a
process group and rlimits. Measured at 89e9da0 with a tool written to
escape them:

* a grandchild that called setsid() left the group the timeout signals and
  was still alive after run_bounded returned TIMED_OUT;
* the tool opened 5000 descriptors (RLIMIT_NOFILE was the caller's);
* nothing stopped a connection: policy denied network to tools, and the
  kernel was never asked.

Now a tool runs in its own user, PID and network namespaces when the host
allows them unprivileged, under an init that takes every process in the
namespace down with it, and with RLIMIT_NOFILE set. What a run actually
got is recorded on it (``ExecutionResult.isolation``) and these tests read
that record, not the request.

Each case has a control run with ``Limits(isolate=False)`` showing the
threat is real on this host -- so a pass means the namespace stopped it,
not that the tool failed for some other reason. Where the host forbids
unprivileged namespaces, the isolated cases are SKIPPED with that reason:
kernel containment is then a deployment requirement, not a property of
this code.

NOT CONTAINED HERE, stated as tests so it cannot be forgotten: filesystem
reads (no mount namespace or LSM policy), and GPU or disk-space quotas.
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

from agent_substrate.execution import (Limits, Outcome, namespace_isolation,
                                       run_bounded)
from agent_substrate.tools import Determinism, Field_, ToolSpec

PY = sys.executable
_PREFIX, _ISOLATION = namespace_isolation()
needs_ns = pytest.mark.skipif(
    not _PREFIX, reason=f"DEPLOYMENT_REQUIRED: {_ISOLATION['detail']}")


def _spec(timeout_s: float = 10.0) -> ToolSpec:
    return ToolSpec(tool_id="hostile", version="1.0", summary="hostile",
                    inputs=(Field_("n", "int"),),
                    determinism=Determinism.BYTE_IDENTICAL,
                    writable_scope=("out",), timeout_s=timeout_s)


def _run(tmp_path: Path, code: str, *, isolate: bool, wall: float = 10.0,
         **limits):
    (tmp_path / "out").mkdir(exist_ok=True)
    return run_bounded([PY, "-c", code], spec=_spec(wall), cwd=tmp_path,
                       limits=Limits(wall_seconds=wall, isolate=isolate,
                                     **limits),
                       env={"PATH": "/usr/bin:/bin"})


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---- what was achieved is recorded ---------------------------------------------

def test_isolation_is_present_where_it_is_required():
    """HARNESS_ISOLATION_REQUIRED=1 turns "this host has no namespaces"
    from a skip into a failure, so a run that must measure containment --
    the mutation matrix, a deployment check -- cannot pass by skipping it."""
    if os.environ.get("HARNESS_ISOLATION_REQUIRED") == "1":
        assert _PREFIX, _ISOLATION["detail"]
    else:
        pytest.skip("HARNESS_ISOLATION_REQUIRED is not set")


def test_the_run_records_the_isolation_it_got(tmp_path):
    r = _run(tmp_path, "pass", isolate=True)
    assert r.isolation == _ISOLATION
    assert r.to_record()["isolation"] == _ISOLATION
    off = _run(tmp_path, "pass", isolate=False)
    assert off.isolation["namespaces"] == []
    assert "not requested" in off.isolation["detail"]


# ---- R-1: a process that leaves the group --------------------------------------

_ESCAPE = r"""
import os, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        open("out/escapee.pid", "w").write(os.readlink("/proc/self"))
        time.sleep(120)
        os._exit(0)
    os._exit(0)
time.sleep(60)
"""


def _escapee(tmp_path: Path) -> int:
    deadline = time.time() + 5
    f = tmp_path / "out" / "escapee.pid"
    while time.time() < deadline and not (f.exists() and f.read_text()):
        time.sleep(0.05)
    return int(f.read_text())


def test_control_without_isolation_a_setsid_grandchild_outlives_the_timeout(
        tmp_path):
    """The threat, on this host: the group is killed, the escapee is not."""
    r = _run(tmp_path, _ESCAPE, isolate=False, wall=1.5)
    pid = _escapee(tmp_path)
    try:
        assert r.outcome is Outcome.TIMED_OUT
        assert _alive(pid), "the control did not reproduce the escape"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


@needs_ns
def test_a_setsid_grandchild_dies_with_the_run(tmp_path):
    r = _run(tmp_path, _ESCAPE, isolate=True, wall=1.5)
    pid = _escapee(tmp_path)
    try:
        assert r.outcome is Outcome.TIMED_OUT
        assert "pid" in r.isolation["namespaces"]
        deadline = time.time() + 3
        while _alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), "a process the tool started survived it"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


@needs_ns
def test_an_escapee_dies_when_the_tool_exits_normally_too(tmp_path):
    """Not only on timeout: a tool that leaves a daemon behind and exits 0
    leaves nothing running."""
    code = _ESCAPE.replace("time.sleep(60)", "time.sleep(0.5)")
    r = _run(tmp_path, code, isolate=True)
    pid = _escapee(tmp_path)
    try:
        assert r.outcome is Outcome.COMPLETED
        deadline = time.time() + 3
        while _alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(pid)
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


# ---- the run's own outcome is still what it was ---------------------------------

@needs_ns
@pytest.mark.parametrize("code, exit_status, signal_number", [
    ("import sys; sys.exit(7)", 7, None),
    ("import os, signal; os.kill(os.getpid(), signal.SIGUSR1)", None,
     signal.SIGUSR1),
    ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", None,
     signal.SIGTERM),
])
def test_an_isolated_run_reports_the_tools_own_ending(tmp_path, code,
                                                      exit_status,
                                                      signal_number):
    """Under a namespace init the tool is not PID 1: its exit status and its
    death by signal are its own, reported through the init."""
    r = _run(tmp_path, code, isolate=True)
    assert r.exit_status == exit_status
    assert r.signal_number == signal_number


# ---- R-2: the network --------------------------------------------------------------

def _listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _connect_code(port: int) -> str:
    return (f"import socket\n"
            f"try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
            f"    print('CONNECTED')\n"
            f"except OSError as e:\n"
            f"    print('REFUSED', e)\n")


def test_control_without_isolation_the_tool_reaches_the_hosts_loopback(
        tmp_path):
    srv, port = _listener()
    try:
        r = _run(tmp_path, _connect_code(port), isolate=False)
    finally:
        srv.close()
    assert "CONNECTED" in r.stdout_excerpt, r.stdout_excerpt


@needs_ns
def test_an_isolated_tool_reaches_no_network_not_even_the_hosts_loopback(
        tmp_path):
    srv, port = _listener()
    try:
        r = _run(tmp_path, _connect_code(port), isolate=True)
    finally:
        srv.close()
    assert "REFUSED" in r.stdout_excerpt, r.stdout_excerpt
    assert "net" in r.isolation["namespaces"]


# ---- R-2: descriptors and environment ----------------------------------------------

_FDS = r"""
import os
n = 0
try:
    for _ in range(5000):
        os.open("/dev/null", os.O_RDONLY); n += 1
except OSError:
    pass
print("OPENED", n)
"""


@pytest.mark.parametrize("isolate", [True, False])
def test_descriptors_are_bounded(tmp_path, isolate):
    r = _run(tmp_path, _FDS, isolate=isolate, open_files=64)
    opened = int(r.stdout_excerpt.split()[-1])
    assert opened < 64, r.stdout_excerpt


def test_control_a_generous_descriptor_bound_is_a_real_bound(tmp_path):
    """The bound, not something else, stopped the loop above."""
    r = _run(tmp_path, _FDS, isolate=False, open_files=1024)
    assert 900 < int(r.stdout_excerpt.split()[-1]) < 1024


def test_the_callers_environment_does_not_reach_the_tool(tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("HARNESS_TEST_SECRET", "do-not-leak")
    r = _run(tmp_path, "import os; print(sorted(os.environ))", isolate=True)
    assert "HARNESS_TEST_SECRET" not in r.stdout_excerpt
    assert "do-not-leak" not in r.stdout_excerpt


# ---- not contained here: stated so it is not forgotten ---------------------------

def test_filesystem_reads_are_not_confined_by_this_executor(tmp_path):
    """No mount namespace and no LSM policy: a tool reads what its user can.
    Confinement of reads is a deployment requirement (a read-only container
    filesystem, or an LSM profile), recorded in the readiness matrix."""
    r = _run(tmp_path, "print(len(open('/etc/hostname').read()) > 0)",
             isolate=True)
    assert "True" in r.stdout_excerpt
    assert "mnt" not in r.isolation["namespaces"]
