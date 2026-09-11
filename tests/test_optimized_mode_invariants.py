"""The governed invariants must hold under `python -O`, which deletes asserts.

WHAT THIS FOUND

`PASS = 0`, `can_PASS_now = NO` and `measured_in_this_system = false` are the
three claims this package exists to hold. In `_write_gate_csv` -- the function
that EMITS the canonical gate table -- all three were enforced by `assert`
statements, and `assert` is a debugging construct whose documented contract is
that it may vanish. Under `python -O` it does. Reproduced (D-2026-45):

    normal:  refused -- illegal gate state PASS, no file written
    -O:      FORGED,,,,,,,PASS,,,true,,YES,,,   written, no error, exit 0

The measurement intake failed open the same way: a file declaring
`schema_version: "99.99-WRONG-VERSION"` returned `REJECTED_FILE` normally and
`OK` under -O.

WHY A SUBPROCESS AND NOT A FLAG

There is no way to un-strip an assert inside a running interpreter. `-O` is
decided at compile time, so the only test that can tell the two builds apart
is one that starts a second interpreter. Patching or monkeying with
`__debug__` in-process would test something else and report it as this.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a scientific claim.
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(code: str, optimize: bool):
    """Run a snippet in a fresh interpreter, with or without -O."""
    argv = [sys.executable] + (["-O"] if optimize else []) + ["-c", code]
    return subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                          timeout=120)


GATE_PROBE = """
import sys, tempfile, pathlib
sys.path.insert(0, {root!r})
from qta_multiphysics.integrated_layers import _write_gate_csv, _GATE_HEADER
row = {{k: "" for k in _GATE_HEADER}}
row.update({{"gate_id": "FORGED", "status": "PASS",
            "can_PASS_now": "YES", "measured_in_this_system": "true"}})
out = pathlib.Path(tempfile.mkdtemp()) / "g.csv"
try:
    _write_gate_csv(out, [row])
except Exception as e:
    print("REFUSED", type(e).__name__)
else:
    print("WROTE", out.read_text().count("PASS"))
"""


@pytest.mark.parametrize("optimize", [False, True],
                         ids=["python", "python -O"])
def test_the_gate_table_refuses_a_PASS_row_under_both_builds(optimize):
    """The zero-PASS rule may not depend on an interpreter flag."""
    proc = _run(GATE_PROBE.format(root=str(ROOT)), optimize)
    assert "REFUSED" in proc.stdout, (
        "the canonical gate table accepted a row with status=PASS, "
        f"can_PASS_now=YES and measured_in_this_system=true. optimize="
        f"{optimize}. stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r}")


INGEST_PROBE = """
import sys, json, tempfile, pathlib
sys.path.insert(0, {root!r})
from qta_multiphysics.measurement_ingest_3d import ingest_and_compare
d = pathlib.Path(tempfile.mkdtemp()) / "m.json"
d.write_text(json.dumps({{"schema_version": "99.99-WRONG", "measurements": []}}))
print(ingest_and_compare(path=d)["ingestion_status"])
"""


@pytest.mark.parametrize("optimize", [False, True],
                         ids=["python", "python -O"])
def test_the_intake_refuses_a_wrong_schema_under_both_builds(optimize):
    proc = _run(INGEST_PROBE.format(root=str(ROOT)), optimize)
    assert proc.stdout.strip() == "REJECTED_FILE", (
        "a measurement file of another schema was ingested. optimize="
        f"{optimize}. stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r}")


def test_the_two_builds_are_actually_different():
    """ANTI-VACUITY. If -O were not reaching the child, both cases above
    would pass while testing one build twice -- which is exactly the shape
    of a guard that never ran."""
    plain = _run("import sys; print(sys.flags.optimize, __debug__)", False)
    opt = _run("import sys; print(sys.flags.optimize, __debug__)", True)
    assert plain.stdout.strip() == "0 True", plain.stdout
    assert opt.stdout.strip() == "1 False", opt.stdout


# --- and no NEW assert may take over an enforcing path silently -----------

#: Asserts left in the scientific tree, each one a self-check on a value the
#: same function just built or on a module constant -- not a decision about
#: caller-supplied data. Recorded by site so a new one cannot join them
#: without somebody adding it here and saying which kind it is.
KNOWN_SELF_CHECKS = {
    ("qta_multiphysics/coupled_mode_solver.py",
     "Mode C must run with the processing source OFF"),
    ("qta_multiphysics/species_accounting_3d.py",
     "CANONICAL_ACTIVE / RESIDUAL_ONLY module constants"),
    ("qta_multiphysics/thermal_3d_transient.py",
     "BoundarySpec3D().validate() returned the expected back condition"),
}


def test_no_new_assert_has_taken_over_an_enforcing_path():
    """`assert` is a debugging construct; enforcement is not debugging.

    qta_agent and tools/ carry ZERO asserts and must stay that way: the
    substrate is where authority is decided. The scientific tree keeps a
    handful of self-checks, enumerated above, and the count is pinned so
    that a new one has to be classified by a person rather than absorbed.
    """
    found = []
    for root in ("qta_agent", "tools", "qta_multiphysics"):
        for path in sorted((ROOT / root).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    found.append((str(path.relative_to(ROOT)), node.lineno))

    substrate = [f for f in found if f[0].startswith(("qta_agent/", "tools/"))]
    assert not substrate, (
        "the agent substrate decides authority, and an assert there is a "
        f"refusal an interpreter flag can switch off: {substrate}")

    files = {f[0] for f in found}
    known = {k[0] for k in KNOWN_SELF_CHECKS}
    assert files == known, (
        f"asserts now live in {sorted(files - known)} and no longer in "
        f"{sorted(known - files)}. Each one is a check that `python -O` "
        "deletes: classify it in KNOWN_SELF_CHECKS, or make it a real "
        "refusal like the four converted in D-2026-45")
