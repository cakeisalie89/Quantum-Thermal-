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
import pathlib
import subprocess
import sys
from pathlib import Path

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


INTERLOCK_PROBE = """
import sys
sys.path.insert(0, {root!r})
import qta_full_sim as S
try:
    S.SystemState("X", LCVD_on=True, sensing_on=True).validate()
except S.InterlockViolation as e:
    print("REFUSED", str(e)[:5])
else:
    print("ALLOWED")
"""


@pytest.mark.parametrize("optimize", [False, True],
                         ids=["python", "python -O"])
def test_a_machine_interlock_holds_under_both_builds(optimize):
    """D-2026-73: the ten interlocks were ``assert`` statements.

    Under -O, LCVD and sensing on together -- the state the model calls a
    250x thermal overload -- validated cleanly, and the self-test that
    exists to catch exactly that printed "correctly blocked: False". The
    guard below did not see it: it scanned qta_agent, tools and
    qta_multiphysics, and qta_full_sim.py is at the root.
    """
    proc = _run(INTERLOCK_PROBE.format(root=str(ROOT)), optimize)
    assert proc.stdout.strip() == "REFUSED IL-01", (
        f"optimize={optimize}. stdout={proc.stdout!r} "
        f"stderr={proc.stderr[-400:]!r}")


# --- and no NEW assert may take over an enforcing path silently -----------

#: Asserts left in the scientific tree, each one a self-check on a value the
#: same function just built or on a module constant -- not a decision about
#: caller-supplied data. Pinned by COUNT per file: the docstring below always
#: said "the count is pinned", and the code compared the SET OF FILES, so a
#: second assert in one of these three joined the first unseen.
KNOWN_SELF_CHECKS = {
    # "Mode C must run with the processing source OFF": checks the
    # config clone built on the line above.
    "qta_multiphysics/coupled_mode_solver.py": 1,
    # CANONICAL_ACTIVE / RESIDUAL_ONLY module constants.
    "qta_multiphysics/species_accounting_3d.py": 2,
    # BoundarySpec3D().validate() returned the expected back condition:
    # a default constructed on the line above.
    "qta_multiphysics/thermal_3d_transient.py": 1,
}

#: The only tree allowed any assert at all. Everything else -- the agent
#: substrate, the tools, the release and trust modules, the pipeline at the
#: root -- decides or enforces something, and must do it in a way ``-O``
#: cannot remove.
SCIENTIFIC_TREE = "qta_multiphysics/"


def _asserts_in(paths, root=ROOT) -> dict:
    """``{path: number of assert statements}`` for the given files."""
    found = {}
    for rel in paths:
        try:
            tree = ast.parse((Path(root) / rel).read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        n = sum(isinstance(node, ast.Assert) for node in ast.walk(tree))
        if n:
            found[rel] = n
    return found


def _violations(found: dict) -> list:
    """Why ``found`` breaks the rule; empty when it does not."""
    out = []
    for rel, n in sorted(found.items()):
        if not rel.startswith(SCIENTIFIC_TREE):
            out.append(f"{rel}: {n} assert(s) outside the scientific tree")
        elif KNOWN_SELF_CHECKS.get(rel) != n:
            out.append(f"{rel}: {n} assert(s), {KNOWN_SELF_CHECKS.get(rel, 0)}"
                       " classified")
    for rel, n in sorted(KNOWN_SELF_CHECKS.items()):
        if rel not in found:
            out.append(f"{rel}: classified {n} self-check(s), found none -- "
                       "update KNOWN_SELF_CHECKS so it keeps meaning something")
    return out


def _production_python():
    sys.path.insert(0, str(ROOT))
    from tools.repo_scope import assert_scope_is_plausible, repository_files
    files = repository_files("*.py", include_tests=False)
    assert_scope_is_plausible(files)
    return files


def test_no_new_assert_has_taken_over_an_enforcing_path():
    """`assert` is a debugging construct; enforcement is not debugging.

    Every tracked production Python file is scanned -- not a list of
    directories, because the list is what missed qta_full_sim.py. Outside
    the scientific tree the count must be zero; inside it, each file's count
    must equal what a person classified.
    """
    bad = _violations(_asserts_in(_production_python()))
    assert not bad, (
        "asserts that `python -O` deletes: classify each as a self-check in "
        "KNOWN_SELF_CHECKS, or make it a real refusal like the ten converted "
        "in D-2026-73: " + "; ".join(bad))


def test_the_scan_covers_the_root_modules():
    """ANTI-VACUITY for the scope: the file that held ten interlock asserts
    must be inside what is scanned."""
    files = set(_production_python())
    for rel in ("qta_full_sim.py", "release_trust.py", "verify_release.py",
                "qta_agent/store.py", "tools/mutation_matrix.py"):
        assert rel in files, f"{rel} is not scanned"


@pytest.mark.parametrize("found,needle", [
    ({"qta_full_sim.py": 1}, "outside the scientific tree"),
    ({"qta_agent/store.py": 1}, "outside the scientific tree"),
    (dict(KNOWN_SELF_CHECKS, **{
        "qta_multiphysics/coupled_mode_solver.py": 2}), "1 classified"),
    (dict(KNOWN_SELF_CHECKS, **{"qta_multiphysics/grids.py": 1}),
     "0 classified"),
    ({}, "found none"),
])
def test_the_rule_refuses_what_it_should(found, needle):
    """ANTI-VACUITY for the rule: each way of breaking it is reported.

    The second-assert-in-a-classified-file case is the one the old
    set-of-files comparison could not see."""
    assert any(needle in v for v in _violations(found)), _violations(found)


def test_the_rule_accepts_the_classified_set():
    """Control: exactly the classified self-checks is not a violation."""
    assert _violations(dict(KNOWN_SELF_CHECKS)) == []
