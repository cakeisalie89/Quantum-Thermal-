"""No production code enforces anything with ``assert``: ``python -O``
deletes them.

WHY. ``assert`` is a debugging construct whose documented contract is that
it may vanish. This repository once enforced a governed invariant with three
of them, and under ``-O`` the refusal became a silent write (D-2026-45); a
schema check failed open the same way. Those modules are retired; the rule
outlived them because it was never about them.

THE RULE. Every tracked production Python file -- everything but the tests
-- contains no ``assert`` statement at all. Not a list of directories, which
is what once missed a root module: the scope is the repository's own file
list. And because a scan is a claim about text, one real enforcement of the
agent substrate is also run under ``-O`` in a fresh interpreter, where the
assert would be gone, and must still refuse.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _asserts_in(paths, root=ROOT) -> dict:
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


def _production_python():
    sys.path.insert(0, str(ROOT / "tools"))
    from repo_scope import assert_scope_is_plausible, repository_files
    files = repository_files("*.py", include_tests=False)
    assert_scope_is_plausible(files)
    return files


def test_no_production_module_contains_an_assert():
    found = _asserts_in(_production_python())
    assert found == {}, (
        "asserts that `python -O` deletes; make each a real refusal: "
        f"{found}")


def test_the_scan_covers_every_layer():
    """ANTI-VACUITY for the scope: a file from each layer, and a root
    module, is scanned."""
    files = set(_production_python())
    for rel in ("agent_substrate/store.py", "scientific/model.py",
                "scientific_ai/neural/config.py", "tools/mutation_matrix.py",
                "generate_manifest.py"):
        assert rel in files, f"{rel} is not scanned"


def test_the_scan_finds_an_assert_where_there_is_one(tmp_path):
    """ANTI-VACUITY for the rule: a planted assert is found, with its
    count."""
    (tmp_path / "m.py").write_text("def f(x):\n    assert x\n    "
                                   "assert x > 1\n", encoding="utf-8")
    assert _asserts_in(["m.py"], root=tmp_path) == {"m.py": 2}


PROBE = """
import sys
sys.path.insert(0, {root!r})
from agent_substrate.authority import (Role, State, TransitionError,
                                      TransitionRequest, check)
try:
    # the proposer verifying its own record: I4 forbids it
    check(TransitionRequest(record_id="r", src=State.UNDER_REVIEW,
                            dst=State.VERIFIED, actor="p",
                            role=Role.VERIFIER, proposer="p"))
except TransitionError as e:
    print("REFUSED")
else:
    print("ALLOWED")
"""


@pytest.mark.parametrize("optimize", [False, True], ids=["normal", "-O"])
def test_an_authority_refusal_survives_optimisation(optimize):
    """The authority gate's refusal of an illegal edge, in a fresh
    interpreter with and without ``-O``: the same answer, because it is a
    raise and not an assert."""
    argv = [sys.executable] + (["-O"] if optimize else []) + [
        "-c", PROBE.format(root=str(ROOT))]
    r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "REFUSED", r.stdout + r.stderr


def test_the_two_builds_are_actually_different():
    """CONTROL: under -O an assert really is gone, so the probe above is a
    test of the build and not of nothing."""
    code = "assert False, 'kept'\nprint('STRIPPED')"
    normal = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True)
    opt = subprocess.run([sys.executable, "-O", "-c", code],
                         capture_output=True, text=True)
    assert normal.returncode != 0 and "kept" in normal.stderr
    assert opt.returncode == 0 and opt.stdout.strip() == "STRIPPED"
