#!/usr/bin/env python3
"""The ``filesystem_confinement`` deployment check, run where it applies.

DEPLOYMENT.md section 9 names the check RUN-3 is attested by: a tool reading
a sentinel outside its declared readable paths is refused by the kernel,
directly and through a symbolic link. This runs exactly that, on the host it
is invoked on, through the executor every governed tool goes through, under
``confine="high_assurance"``, which is what a deployment relying on it uses.

Every line of the result is an OBSERVATION -- what the probe tool could
open -- and the confinement the executor recorded beside it. It passes only
if every observation is the one confinement requires, and the record says
the confinement that produced them.

Run on a CI runner or a laptop it says what THAT host does, and nothing
about a deployment. The deployment runs it, attests the output under its own
identity (tools/deployment_evidence.py), and that attestation -- not this
script's exit status anywhere else -- is what a readiness row counts.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate.execution import Limits, Outcome, run_bounded  # noqa: E402
from agent_substrate.tools import Determinism, Field_, ToolSpec  # noqa: E402

CHECK = "filesystem_confinement"

_TOOL = """\
import json, os, sys
paths = json.loads(sys.argv[1])
seen = {}
for name, path in paths["read"].items():
    try:
        open(path, "rb").read(1)
        seen[name] = "OPENED"
    except PermissionError:
        seen[name] = "DENIED"
    except OSError as exc:
        seen[name] = type(exc).__name__
for name, path in paths["write"].items():
    try:
        open(path, "w").write("probe")
        seen[name] = "WROTE"
    except PermissionError:
        seen[name] = "DENIED"
print(json.dumps(seen, sort_keys=True))
"""

#: What each observation must be under confinement.
EXPECTED = {"sentinel": "DENIED", "sentinel_via_symlink": "DENIED",
            "host_configuration": "DENIED", "write_outside_scope": "DENIED",
            "write_inside_scope": "WROTE"}


def probe(work: Path) -> dict:
    (work / "out").mkdir()
    sentinel = work / "sentinel.txt"
    sentinel.write_text("a file no tool was granted\n")
    (work / "out" / "link").symlink_to(sentinel)
    paths = {"read": {"sentinel": str(sentinel),
                      "sentinel_via_symlink": str(work / "out" / "link"),
                      "host_configuration": "/etc/passwd"},
             "write": {"write_outside_scope": str(work / "stray.txt"),
                       "write_inside_scope": str(work / "out" / "ok.txt")}}
    spec = ToolSpec(tool_id="deployment.confinement_probe", version="1",
                    summary="confinement probe", inputs=(Field_("n", "int"),),
                    determinism=Determinism.BYTE_IDENTICAL,
                    writable_scope=("out",), timeout_s=30.0)
    r = run_bounded([sys.executable, "-c", _TOOL, json.dumps(paths)],
                    spec=spec, cwd=work,
                    limits=Limits(wall_seconds=30.0, confine="high_assurance"),
                    env={"PATH": "/usr/bin:/bin"})
    fs = r.isolation.get("filesystem", {})
    observed: dict = {}
    if r.outcome is Outcome.COMPLETED:
        try:
            observed = json.loads(r.stdout_excerpt.strip().splitlines()[-1])
        except (ValueError, IndexError):
            observed = {}
    failures = [f"{k}: expected {v}, observed {observed.get(k, 'nothing')}"
                for k, v in EXPECTED.items() if observed.get(k) != v]
    if not str(fs.get("applied") or "").startswith("landlock"):
        failures.append(f"the executor recorded no confinement: "
                        f"{fs.get('detail') or r.reason}")
    return {"check": CHECK, "result": "FAIL" if failures else "PASS",
            "outcome": r.outcome.value, "observed": observed,
            "expected": EXPECTED, "confinement": fs.get("applied"),
            "namespaces": r.isolation.get("namespaces", []),
            "failures": failures}


def main(argv=None) -> int:
    with tempfile.TemporaryDirectory(prefix="confinement-probe-") as d:
        out = probe(Path(d))
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
