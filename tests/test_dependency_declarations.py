"""Every direct import is declared in a group that ships it (D-2026-74).

CI syncs every dependency group, so CI cannot see a package that arrives only
transitively. This checks the declaration instead of the environment, and
proves -- on the pyproject.toml this commit replaced -- that it would have
caught the case that happened.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import dependency_declarations as dd  # noqa: E402


@pytest.fixture(scope="module")
def pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_the_tree_declares_every_import(pyproject):
    problems, seen = dd.check(ROOT, pyproject)
    assert len(seen) > 50, f"only {len(seen)} imports seen"
    assert not problems, "\n".join(problems)


def test_the_scan_sees_the_imports_that_broke(pyproject):
    """ANTI-VACUITY for scope: tests/ is scanned, and yaml is seen there as
    an import-time import."""
    _, seen = dd.check(ROOT, pyproject)
    yaml_users = {p for p, name, eager in seen if name == "yaml" and eager}
    assert yaml_users == {"tests/test_bootstrap_workflow_contract.py",
                          "tests/test_release_workflow_contract.py"}


def test_the_check_refuses_the_pyproject_before_the_repair(pyproject):
    """The anti-vacuity control: the same tree against a pyproject without
    PyYAML reports exactly the two sites D-2026-74 found."""
    before = json.loads(json.dumps(pyproject))
    before["dependency-groups"]["dev"] = [
        r for r in before["dependency-groups"]["dev"]
        if not r.startswith("pyyaml")]
    problems, _ = dd.check(ROOT, before)
    assert len(problems) == 2, problems
    assert all("imports yaml" in p and "minimal environment" in p
               for p in problems)


def test_pyyaml_is_dev_and_not_runtime(pyproject):
    """Declared where it is used -- two tests -- and not promoted."""
    d = dd.declared(pyproject)
    assert "pyyaml" in d["dev"]
    assert "pyyaml" not in d["runtime"]
    assert "pyyaml" in dd.minimal(pyproject)


def test_the_workflow_group_is_not_minimal(pyproject):
    """The group that used to supply yaml is not in a bare sync; if it were,
    the check could not have seen D-2026-74."""
    assert "snakemake" not in dd.minimal(pyproject)


@pytest.mark.parametrize("src,eager", [
    ("import numpy\n", True),
    ("from numpy import linalg\n", True),
    ("if True:\n    import numpy\n", True),
    ("def f():\n    import numpy\n", False),
    ("try:\n    import numpy\nexcept ImportError:\n    pass\n", False),
    ("try:\n    import numpy\nexcept Exception:\n    pass\n", False),
    ("try:\n    import numpy\nexcept (OSError, ModuleNotFoundError):\n"
     "    pass\n", False),
    # a try that does not catch the import failure does not guard it
    ("try:\n    import numpy\nexcept KeyError:\n    pass\n", True),
    # the else branch of a guard runs only when the body succeeded, but the
    # import there is not itself guarded
    ("try:\n    pass\nexcept ImportError:\n    pass\nelse:\n"
     "    import numpy\n", True),
])
def test_eagerness_is_decided_by_structure(src, eager):
    assert [(n, e) for _, _, n, e in dd.scan_source(src, "x.py")] == [
        ("numpy", eager)]


def test_relative_imports_are_not_packages():
    assert dd.scan_source("from . import x\nfrom .y import z\n", "x.py") == []


def test_an_eager_import_from_a_non_minimal_group_is_refused(
        pyproject, tmp_path):
    (tmp_path / "t.py").write_text("import snakemake\n")
    problems, _ = dd.check(tmp_path, pyproject, files=["t.py"])
    assert len(problems) == 1 and "minimal environment" in problems[0]


def test_a_lazy_import_must_still_be_declared_somewhere(pyproject, tmp_path):
    (tmp_path / "t.py").write_text(
        "def f():\n    import pytest_nonexistent_pkg_xyz\n")
    problems, _ = dd.check(tmp_path, pyproject, files=["t.py"])
    assert len(problems) == 1 and "no known distribution" in problems[0]


def test_a_lazy_import_from_an_extra_is_accepted(pyproject, tmp_path):
    """Control: the stack adapters import their extras lazily, and that is
    what the extras are for."""
    (tmp_path / "t.py").write_text("def f():\n    import SALib\n")
    assert dd.check(tmp_path, pyproject, files=["t.py"])[0] == []


def test_the_environmental_list_is_pinned_and_reasoned():
    assert set(dd.ENVIRONMENTAL) == {"dolfinx", "sigstore"}
    for name, why in dd.ENVIRONMENTAL.items():
        assert len(why) >= 40, f"{name}: say why"


def test_the_inventory_is_regenerated_from_pyproject_and_lock():
    """The committed inventory is exactly what pyproject + uv.lock give; it
    said 71 packages and omitted h5py before it was derived."""
    path = ROOT / dd.INVENTORY
    committed = json.loads(path.read_text())
    assert path.read_text() == dd._render(dd.build_inventory(ROOT, committed))
    names = {e["package"] for e in committed["direct_dependencies"]}
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    declared = set().union(*dd.declared(
        tomllib.loads((ROOT / "pyproject.toml").read_text())).values())
    assert names == declared
    assert f"({len(lock['package'])} packages" in committed["transitive"]


def test_the_verifier_passes_on_the_tree():
    r = subprocess.run([sys.executable, "tools/dependency_declarations.py"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_stale_inventory_fails_the_verifier(tmp_path, monkeypatch):
    """The committed file said 71 packages and omitted h5py; --check must
    refuse exactly that kind of drift, not only pass on a fresh one."""
    stale = json.loads((ROOT / dd.INVENTORY).read_text())
    stale["transitive"] = "see uv.lock (71 packages; lockfile is the " \
                          "resolution authority)"
    p = tmp_path / "dependency_inventory.json"
    p.write_text(dd._render(stale))
    monkeypatch.setattr(dd, "INVENTORY", str(p))
    assert dd.main([]) == 1


def test_a_lazy_import_of_an_undeclared_real_package_is_refused(
        pyproject, tmp_path):
    """A distribution that IS installed -- transitively -- but declared
    nowhere: lazy or not, that is the D-2026-74 shape."""
    before = json.loads(json.dumps(pyproject))
    before["dependency-groups"]["dev"] = [
        r for r in before["dependency-groups"]["dev"]
        if not r.startswith("pyyaml")]
    (tmp_path / "t.py").write_text("def f():\n    import yaml\n")
    problems, _ = dd.check(tmp_path, before, files=["t.py"])
    assert len(problems) == 1 and "declared nowhere" in problems[0]
