#!/usr/bin/env python3
"""The deployment isolation contract (Assurance I), probed on this host.

    python tools/isolation_contract.py    # probe, print; exit 0 iff it holds

THE CONTRACT (``docs/security/isolation_contract.json``) names eight
dimensions. For each it says which mechanism this repository uses, and
whether that mechanism is enforced by the KERNEL, is an APPLICATION rule, or
is NOT_PROVIDED at all. What a dimension does not cover is stated beside it.
Nothing here relabels an application rule as kernel isolation.

THE PROBE runs, through the executor every governed tool goes through
(``execution.run_bounded``, ``confine="high_assurance"``), one small tool
per dimension, and observes what the tool could do. Where an observation
needs a control, the control is taken on the same host outside the tool,
so a DENIED means the boundary refused, not that nothing was there.

WHAT A PASS MEANS. That on THIS host each dimension behaves as the contract
says. Run on a CI runner or a laptop, it says what that host does and
nothing about a deployment. A deployment runs it under its own identity, and
that attestation -- not this script's exit status anywhere else -- is what a
readiness row counts (RUN-3). CI containment is not production attestation.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate.execution import (  # noqa: E402
    Limits, Outcome, namespace_isolation, run_bounded,
)
from agent_substrate.tools import Determinism, Field_, ToolSpec  # noqa: E402

CONTRACT = ROOT / "docs" / "security" / "isolation_contract.json"
DIMENSIONS = ("filesystem", "network", "namespaces", "syscalls",
              "process_tree", "quotas", "credentials", "audit")
ENFORCEMENT = ("KERNEL", "APPLICATION", "NOT_PROVIDED")
#: How every probe runs: what a deployment relying on the boundary uses.
#: A control runs the same probes with the boundary off, to show each probe
#: can see its absence.
MODE: dict = {"confine": "high_assurance", "isolate": True}


def _limits(**over) -> Limits:
    return Limits(**{"wall_seconds": 20.0, **MODE, **over})


def _spec(scope=("out",), timeout=20.0) -> ToolSpec:
    return ToolSpec(tool_id="deployment.isolation_probe", version="1",
                    summary="isolation probe", inputs=(Field_("n", "int"),),
                    determinism=Determinism.BYTE_IDENTICAL,
                    writable_scope=scope, timeout_s=timeout)


def _run(work: Path, code: str, *args, limits: Limits | None = None,
         env: dict | None = None):
    (work / "out").mkdir(exist_ok=True)
    return run_bounded(
        [sys.executable, "-c", code, *args], spec=_spec(), cwd=work,
        limits=limits or _limits(),
        env=env if env is not None else {"PATH": "/usr/bin:/bin"})


def _json(r) -> dict:
    try:
        return json.loads(r.stdout_excerpt.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


# ------------------------------------------------------------- the probes

def probe_filesystem(work: Path) -> dict:
    sys.path.insert(0, str(ROOT / "tools"))
    import confinement_probe
    out = confinement_probe.probe(work, limits=_limits(wall_seconds=30.0))
    return {"observed": out["observed"], "confinement": out["confinement"],
            "holds": out["result"] == "PASS", "failures": out["failures"]}


_CONNECT = """\
import json, socket, sys
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    print(json.dumps({"connect": "CONNECTED"}))
except OSError as exc:
    print(json.dumps({"connect": type(exc).__name__}))
"""


def probe_network(work: Path) -> dict:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        control = socket.create_connection(("127.0.0.1", port), timeout=3)
        control.close()                  # reachable from outside the tool
        r = _run(work, _CONNECT, str(port))
    finally:
        listener.close()
    seen = _json(r).get("connect")
    return {"observed": {"host_listener_from_host": "CONNECTED",
                         "host_listener_from_tool": seen},
            "holds": r.outcome is Outcome.COMPLETED and seen != "CONNECTED"}


_NAMESPACES = """\
import json, os
print(json.dumps({n: os.readlink(f"/proc/self/ns/{n}")
                  for n in ("user", "pid", "net", "mnt")}))
"""


def probe_namespaces(work: Path) -> dict:
    mine = {n: os.readlink(f"/proc/self/ns/{n}")
            for n in ("user", "pid", "net", "mnt")}
    theirs = _json(_run(work, _NAMESPACES))
    own = sorted(n for n in mine if theirs.get(n) and theirs[n] != mine[n])
    return {"observed": {"own_namespaces": own,
                         "shared_with_host": sorted(set(mine) - set(own))},
            "holds": own == ["net", "pid", "user"]}


_SECCOMP = """\
import json
line = [x for x in open("/proc/self/status") if x.startswith("Seccomp:")]
print(json.dumps({"seccomp": line[0].split()[1] if line else None}))
"""


def probe_syscalls(work: Path) -> dict:
    mode = _json(_run(work, _SECCOMP)).get("seccomp")
    # The contract says NOT_PROVIDED: it holds while no filter is installed
    # (mode 0). A filter appearing would make the contract understate the
    # boundary, which is also a discrepancy to report.
    return {"observed": {"seccomp_mode": mode}, "holds": mode == "0"}


_TREE = """\
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c",
                  "import time, pathlib\\n"
                  "p = pathlib.Path('out/beat')\\n"
                  "while True:\\n"
                  "    p.write_text(str(time.time())); time.sleep(0.1)"],
                 start_new_session=True)
time.sleep(60)
"""


def probe_process_tree(work: Path) -> dict:
    """A grandchild in a session of its own, outliving a tool that times
    out, must die with it."""
    r = _run(work, _TREE, limits=_limits(wall_seconds=2.0))
    beat = work / "out" / "beat"
    first = beat.stat().st_mtime if beat.exists() else None
    time.sleep(1.0)
    after = beat.stat().st_mtime if beat.exists() else None
    return {"observed": {"outcome": r.outcome.value,
                         "grandchild_started": first is not None,
                         "grandchild_alive_after": after != first},
            "holds": r.outcome is Outcome.TIMED_OUT and first is not None
            and after == first}


_CPU = "while True: pass\n"
_MEMORY = """\
import json
try:
    b = bytearray(1 << 30)
    print(json.dumps({"allocated": len(b)}))
except MemoryError:
    print(json.dumps({"allocated": "MemoryError"}))
"""
_DISK = """\
import json
try:
    with open("out/big", "wb") as f:
        f.write(b"x" * (1 << 21))
    print(json.dumps({"wrote": "ALL"}))
except OSError as exc:
    print(json.dumps({"wrote": type(exc).__name__}))
"""


def probe_quotas(work: Path) -> dict:
    # the CPU limit, not the wall bound, must end it: a wall bound twenty
    # times the CPU limit leaves no doubt which one did
    started = time.monotonic()
    cpu = _run(work, _CPU, limits=_limits(cpu_seconds=1, wall_seconds=20.0))
    cpu_ended_after = time.monotonic() - started
    mem = _json(_run(work, _MEMORY, limits=_limits(
        address_space_bytes=1 << 29)))
    disk = _run(work, _DISK, limits=_limits(output_bytes=1 << 20))
    size = (work / "out" / "big").stat().st_size \
        if (work / "out" / "big").exists() else 0
    gpus = sorted(p.name for p in Path("/dev").glob("nvidia*"))
    observed = {"cpu_bound_run": cpu.outcome.value,
                "cpu_bound_run_ended_within_s": round(cpu_ended_after, 1),
                "allocation_past_address_space": mem.get("allocated"),
                "file_past_size_limit_bytes": size,
                "disk_run": disk.outcome.value,
                "gpu_devices": gpus}
    holds = (cpu.outcome is Outcome.FAILED and cpu_ended_after < 10.0
             and mem.get("allocated") == "MemoryError"
             and size <= (1 << 20))
    return {"observed": observed, "holds": holds}


_ENV_AND_FILES = """\
import json, os, sys
try:
    open(sys.argv[1]).read()
    secret_file = "OPENED"
except PermissionError:
    secret_file = "DENIED"
except OSError as exc:
    secret_file = type(exc).__name__
print(json.dumps({"env": sorted(os.environ), "secret_file": secret_file}))
"""


def probe_credentials(work: Path) -> dict:
    vault = Path(tempfile.mkdtemp(prefix="isolation-vault-"))
    try:
        secret = vault / "token"
        secret.write_text("not-a-real-credential\n")
        os.environ["QT_ISOLATION_PROBE_TOKEN"] = "not-a-real-credential"
        seen = _json(_run(work, _ENV_AND_FILES, str(secret)))
    finally:
        os.environ.pop("QT_ISOLATION_PROBE_TOKEN", None)
        for p in vault.iterdir():
            p.unlink()
        vault.rmdir()
    env = seen.get("env") or []
    return {"observed": {"environment": env,
                         "credential_file": seen.get("secret_file")},
            "holds": "QT_ISOLATION_PROBE_TOKEN" not in env
            and seen.get("secret_file") == "DENIED"}


def probe_audit(work: Path) -> dict:
    r = _run(work, "print('{}')")
    iso = r.isolation
    fs = iso.get("filesystem", {})
    return {"observed": {"namespaces_recorded": iso.get("namespaces"),
                         "confinement_recorded": fs.get("applied"),
                         "mode_recorded": fs.get("mode")},
            "holds": bool(iso.get("namespaces")) and
            str(fs.get("applied") or "").startswith("landlock")
            and fs.get("mode") == MODE["confine"]}


PROBES = {"filesystem": probe_filesystem, "network": probe_network,
          "namespaces": probe_namespaces, "syscalls": probe_syscalls,
          "process_tree": probe_process_tree, "quotas": probe_quotas,
          "credentials": probe_credentials, "audit": probe_audit}


def host() -> dict:
    prefix, rec = namespace_isolation()
    return {"kernel": os.uname().release, "machine": os.uname().machine,
            "namespaces": rec, "python": sys.version.split()[0]}


def run() -> dict:
    """Every dimension probed on this host, beside what the contract says."""
    contract = {d["id"]: d for d in
                json.loads(CONTRACT.read_text(encoding="utf-8"))["dimensions"]}
    out: dict = {"host": host(), "dimensions": {}}
    for name in DIMENSIONS:
        with tempfile.TemporaryDirectory(prefix=f"isolation-{name}-") as d:
            got = PROBES[name](Path(d))
        out["dimensions"][name] = {
            "enforcement": contract[name]["enforcement"], **got}
    out["holds"] = all(d["holds"] for d in out["dimensions"].values())
    return out


def main(argv=None) -> int:
    out = run()
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["holds"] else 1


if __name__ == "__main__":
    sys.exit(main())
