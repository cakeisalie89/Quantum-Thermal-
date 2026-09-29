"""A suite whose green is partly a fact about collection order.

D-2026-63. `tests/` has no `__init__.py`, so pytest puts `tests/` on
`sys.path` and not the repository root. Three of 106 modules imported
`tools.*` or `qta_agent` at top level without inserting the root themselves,
and passed every full run because the alphabet put a module that does insert
it first. Run alone, all three failed to collect.

Every refusal test here is paired with a positive control. A checker that
refused every directory would pass the refusal tests and be worthless.

MODEL-ONLY / FORECAST-ONLY. Nothing here changes a gate, a threshold or a
canonical output. PASS remains 0.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from tools import test_isolation as TI

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _module(d: pathlib.Path, name: str, body: str) -> pathlib.Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


# --- the mechanism, end to end -------------------------------------------

def test_a_module_that_needs_the_repository_root_collects_alone():
    """The defect itself, against a real module that carries no path insert.

    `tests/test_cross_env_semantics.py` does `from tools.cross_env_semantics
    import ...` and nothing else. Before conftest.py this died at collection
    with ModuleNotFoundError unless another module had run first.
    """
    target = ROOT / "tests" / "test_cross_env_semantics.py"
    assert "sys.path.insert" not in target.read_text(encoding="utf-8"), (
        "this test is only meaningful while the module relies on the "
        "conftest rather than inserting the path itself")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def test_the_conftest_puts_the_root_on_the_path_when_nothing_else_has():
    """Checked by running it, not by reading it.

    A conftest.py that exists and does nothing satisfies any grep for the
    file. This asks a fresh interpreter, with only `tests/` on the path the
    way pytest leaves it, whether the import works after the conftest is
    executed.
    """
    prog = "\n".join([
        "import sys, runpy",
        # pytest's own path, near enough: the root is NOT on it. The stdlib
        # entries stay, or nothing would import at all.
        f"sys.path[:] = [p for p in sys.path if p not in ('', {str(ROOT)!r})]",
        f"sys.path.insert(0, {str(ROOT / 'tests')!r})",
        # The precondition is the control. If the root were reachable anyway,
        # the import below would prove nothing about the conftest.
        "try:",
        "    import tools.cross_env_semantics",
        "except ModuleNotFoundError:",
        "    pass",
        "else:",
        "    raise SystemExit('precondition failed: root already importable')",
        f"runpy.run_path({str(ROOT / 'conftest.py')!r})",
        "import tools.cross_env_semantics",
        "print('ok')",
    ])
    r = subprocess.run([sys.executable, "-c", prog], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1000:]
    assert "ok" in r.stdout


# --- the tool's own refusals ---------------------------------------------

def test_a_module_that_cannot_be_collected_is_refused_and_named(tmp_path,
                                                                monkeypatch):
    broken = _module(tmp_path, "test_broken.py",
                     "import a_module_that_does_not_exist\n")
    monkeypatch.setattr(TI, "TESTS", tmp_path)
    assert TI.main([]) == 1
    assert broken.exists()


def test_a_module_that_fails_to_parse_is_refused_too(tmp_path, monkeypatch,
                                                     capsys):
    """Not every collection failure is a missing module.

    A checker that looked for "ModuleNotFoundError" in the output would pass
    this one -- a SyntaxError, a fixture error or a timeout would each be
    reported as a module that collects perfectly well. The exit code is the
    property; the message is a proxy for it.
    """
    _module(tmp_path, "test_unparseable.py", "def test_x(:\n    pass\n")
    monkeypatch.setattr(TI, "TESTS", tmp_path)
    assert TI.main([]) == 1
    cap = capsys.readouterr()
    assert "test_unparseable.py" in cap.err
    assert "ModuleNotFoundError" not in cap.err, \
        "the fixture must fail for a reason other than a missing module"


def test_a_directory_of_importable_modules_passes(tmp_path, monkeypatch):
    """The positive control."""
    _module(tmp_path, "test_fine.py", "def test_one():\n    assert True\n")
    monkeypatch.setattr(TI, "TESTS", tmp_path)
    assert TI.main([]) == 0


def test_an_empty_scope_is_refused_rather_than_agreed_with(tmp_path,
                                                           monkeypatch):
    """"Every module collects alone" is trivially true of no modules.

    A moved directory or a changed glob produces exactly that, and silence
    would read as agreement.
    """
    monkeypatch.setattr(TI, "TESTS", tmp_path)
    assert TI.modules() == []
    assert TI.main([]) == 2


def test_the_tool_reports_how_many_it_checked(tmp_path, monkeypatch, capsys):
    """A verdict without its scope is a claim on trust (D-2026-54)."""
    for i in range(3):
        _module(tmp_path, f"test_m{i}.py", "def test_x():\n    assert True\n")
    monkeypatch.setattr(TI, "TESTS", tmp_path)
    assert TI.main([]) == 0
    out = capsys.readouterr().out
    assert "3 of 3 test modules" in out


def test_the_refusal_names_every_broken_module_not_the_first(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    for i in range(2):
        _module(tmp_path, f"test_bad{i}.py", "import not_a_real_module\n")
    _module(tmp_path, "test_ok.py", "def test_y():\n    assert True\n")
    monkeypatch.setattr(TI, "TESTS", tmp_path)
    assert TI.main([]) == 1
    cap = capsys.readouterr()
    assert "test_bad0.py" in cap.err and "test_bad1.py" in cap.err, \
        "reporting only the first would understate the scope of the problem"
    assert "1 of 3 test modules" in cap.out


def test_the_refusal_is_a_return_code_not_an_assert():
    """`python -O` deletes asserts. A gate a flag removes is not a gate."""
    src = (ROOT / "tools" / "test_isolation.py").read_text(encoding="utf-8")
    assert "raise ScopeError" in src
    assert "\n    assert " not in src


@pytest.mark.parametrize("name", [
    "test_agent_authority_boundaries.py",
    "test_cross_env_semantics.py",
    "test_hotspot_ranking_determinism.py",
])
def test_the_three_that_were_found_collect_alone(name):
    """Named, so a regression in any one of them is reported as itself."""
    ok, tail = TI.collects_alone(ROOT / "tests" / name, timeout=300)
    assert ok, tail
