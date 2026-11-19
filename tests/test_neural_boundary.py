"""NF-1T: the learned-model substrate stands alone.

It imports neither the agent substrate (only ``tools/neural_ledger.py``
records its documents in an authority history, in its own process) nor any
module the repository retired, and its sources, tools and tests carry none
of the retired programme's vocabulary -- read with the repository content
audit's own patterns (``tools/repo_content_audit.py``), so there is one
definition of that vocabulary and this file spells none of it.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import repo_content_audit as RCA  # noqa: E402

PKG = ROOT / "scientific_ai"
SOURCES = sorted(PKG.rglob("*.py"))
PURE = ["config", "accounting", "solver", "family", "estimates", "claims",
        "manifests", "units", "tokens", "features", "documents", "datasets",
        "constraints", "ood", "source_surface_adsorption"]
FORBIDDEN_PACKAGES = ("agent_substrate",)


def _imports(path: Path) -> list:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            out.append(node.module or "")
    return out


def _retired_modules() -> set:
    return RCA.retired_from_inventory(ROOT)[1]


def _terms(text: str) -> list:
    scanned = RCA._strip_protocol(text)
    return sorted({m.group(0).lower() for _, rx in RCA._TERMS
                   for m in rx.finditer(scanned)})


def test_there_is_a_substrate_to_check():
    assert len(SOURCES) >= 20


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(
    ROOT)))
def test_no_substrate_module_imports_a_retired_or_the_agent_layer(path):
    retired = _retired_modules()
    for name in _imports(path):
        parts = name.split(".")
        assert parts[0] not in FORBIDDEN_PACKAGES, (path, name)
        assert not any(".".join(parts[:i]) in retired
                       for i in range(1, len(parts) + 1)), (path, name)


@pytest.mark.parametrize("path", SOURCES + [ROOT / "tools" / "neural.py",
                                   ROOT / "tools" / "neural_ledger.py"],
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_no_substrate_source_carries_the_retired_vocabulary(path):
    assert _terms(path.read_text(encoding="utf-8")) == [], path


def test_only_the_ledger_tool_reaches_the_authority_substrate():
    """The tool that generates data and trains never loads agent_substrate.

    ``tools/neural.py`` runs the governed source model and trains; recording
    documents in an authority history is ``tools/neural_ledger.py``'s job,
    in its own process. An import of agent_substrate creeping back into the
    generating tool would put a dataset and an authority verdict in one
    process -- the direction tests/test_agent_substrate_isolation.py exists
    to keep closed.
    """
    tools = ROOT / "tools"
    gen = _imports(tools / "neural.py")
    assert not [n for n in gen if n.split(".")[0] == "agent_substrate"], gen
    assert "neural_ledger" not in gen
    assert "neural_ledger.py" in (tools / "neural.py").read_text(
        encoding="utf-8"), "the generating tool no longer runs the ledger"
    led = _imports(tools / "neural_ledger.py")
    assert any(n.split(".")[0] == "agent_substrate" for n in led)


def test_no_substrate_test_names_them_either():
    tests = sorted((ROOT / "tests").glob("test_neural_*.py"))
    assert tests
    for path in tests:
        assert _terms(path.read_text(encoding="utf-8")) == [], path


def test_the_scan_sees_a_retired_name_and_a_retired_import(tmp_path):
    """CONTROL: the two checks above are not vacuous. A retired output's
    name is a finding, and so is importing a module the inventory
    retired."""
    assert _terms("reads " + RCA.RETIRED_NAMES[0])
    mod = sorted(_retired_modules())[0]
    probe = tmp_path / "probe.py"
    probe.write_text(f"import {mod}\n", encoding="utf-8")
    assert mod in _imports(probe)
    assert mod in _retired_modules()


def test_the_pure_modules_load_no_framework():
    mods = ", ".join(f"scientific_ai.neural.{m}" for m in PURE)
    code = (f"import sys; import {mods}; "
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('jax', 'jaxlib', 'torch', 'agent_substrate')]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_jax_is_imported_in_exactly_one_guarded_place():
    for path in SOURCES:
        names = _imports(path)
        if any(n.split(".")[0] in ("jax", "jaxlib") for n in names):
            assert path.name == "_jax.py", path
    tree = ast.parse((PKG / "neural" / "model" / "_jax.py").read_text())
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    assert tries and any(isinstance(h.type, ast.Name)
                         and h.type.id == "ImportError"
                         for t in tries for h in t.handlers)


def test_the_governance_layer_imports_no_numerics():
    for name in ("learned_rules.py", "learned_lifecycle.py"):
        for imp in _imports(ROOT / "agent_substrate" / name):
            assert imp.split(".")[0] not in ("scientific", "scientific_ai",
                                             "numpy", "jax"), (name, imp)
