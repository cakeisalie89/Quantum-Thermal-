"""NF-1T s.2, s.53, s.62: the substrate keeps the hardware era out.

A future change that brings a machine mode, a B-to-C-to-D sequence or a
gas-species routing rule into the learned-model substrate -- by name, by
import, or by importing a module that carries one -- fails here.

This file is the one place in the substrate's tests allowed to SPELL the
terms (the audit classifies it TEST_OF_BOUNDARY); it never uses them.
"""
from __future__ import annotations

import ast
import csv
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import neural_legacy_audit as A  # noqa: E402

PKG = ROOT / "scientific_ai"
SOURCES = sorted(PKG.rglob("*.py"))
PURE = ["config", "accounting", "solver", "family", "estimates", "claims",
        "manifests", "units", "tokens", "features", "documents", "datasets",
        "constraints", "ood", "source_surface_adsorption"]
FORBIDDEN_PACKAGES = ("qta_multiphysics", "qta_agent", "qta_full_sim",
                      "qta_sim_stages", "package_consistency_check")


def _imports(path: Path) -> list:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            out.append(node.module or "")
    return out


def _legacy_modules() -> set:
    with (ROOT / "FILE_DISPOSITION.csv").open(newline="") as fh:
        rows = list(csv.reader(fh))
    return {r[0][:-3].replace("/", ".") for r in rows
            if len(r) > 1 and r[1] == "RETIRE_TO_HISTORY"
            and r[0].endswith(".py")}


def test_there_is_a_substrate_to_check():
    assert len(SOURCES) >= 20


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(
    ROOT)))
def test_no_substrate_module_imports_the_legacy_or_the_agent_layer(path):
    legacy = _legacy_modules()
    for name in _imports(path):
        root = name.split(".")[0]
        assert root not in FORBIDDEN_PACKAGES, (path, name)
        assert name not in legacy, (path, name)


@pytest.mark.parametrize("path", SOURCES + [ROOT / "tools" / "neural.py",
                                   ROOT / "tools" / "neural_ledger.py"],
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_no_substrate_source_names_a_machine_mode_or_species_route(path):
    assert A.hits(path.read_text(encoding="utf-8")) == {}, path


def test_only_the_ledger_tool_reaches_the_authority_substrate():
    """The tool that generates data and trains never loads qta_agent.

    ``tools/neural.py`` runs the governed source model and trains; recording
    documents in an authority history is ``tools/neural_ledger.py``'s job,
    in its own process. An import of qta_agent creeping back into the
    generating tool would put a dataset and an authority verdict in one
    process -- the direction tests/test_agent_substrate_isolation.py exists
    to keep closed.
    """
    tools = ROOT / "tools"
    gen = _imports(tools / "neural.py")
    assert not [n for n in gen if n.split(".")[0] == "qta_agent"], gen
    assert "neural_ledger" not in gen
    assert "neural_ledger.py" in (tools / "neural.py").read_text(
        encoding="utf-8"), "the generating tool no longer runs the ledger"
    led = _imports(tools / "neural_ledger.py")
    assert any(n.split(".")[0] == "qta_agent" for n in led)


def test_no_substrate_test_names_them_either():
    for path in sorted((ROOT / "tests").glob("test_neural_*.py")):
        if path.name == "test_neural_boundary.py":
            continue
        assert A.hits(path.read_text(encoding="utf-8")) == {}, path


def test_the_pure_modules_load_no_framework():
    mods = ", ".join(f"scientific_ai.neural.{m}" for m in PURE)
    code = (f"import sys; import {mods}; "
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('jax', 'jaxlib', 'torch', 'qta_multiphysics', 'qta_agent')]; "
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
        for imp in _imports(ROOT / "qta_agent" / name):
            assert imp.split(".")[0] not in ("scientific", "scientific_ai",
                                             "numpy", "jax"), (name, imp)


def test_the_audit_finds_no_neural_leak_and_no_unexplained_file():
    rep = A.audit()
    assert rep["active_neural_semantic_leaks"] == 0
    assert rep["unclassified"] == 0


@pytest.mark.parametrize("planted,cls", [
    ("scientific_ai/neural/new_router.py", "ACTIVE_NEURAL_SEMANTIC_LEAK"),
    ("tools/neural_export.py", "ACTIVE_NEURAL_SEMANTIC_LEAK"),
    ("tests/test_neural_new.py", "ACTIVE_NEURAL_SEMANTIC_LEAK"),
    ("docs/neural/new_report.json", "ACTIVE_NEURAL_SEMANTIC_LEAK"),
])
def test_a_planted_leak_would_be_classified_as_one(planted, cls):
    assert A.classify(planted, {})[0] == cls


@pytest.mark.parametrize("line", [
    "route species by Mode B", "mode_c_threshold = 3", "MODE_D",
    "B -> C -> D", "B→C→D", "methane inventory",
    "helium leak"])
def test_the_audit_patterns_catch_each_spelling(line):
    assert A.hits(line), line


def test_ordinary_words_are_not_mistaken_for_the_terms():
    assert A.hits("a model mode of operation; modern methods; "
                  "the hello world; mode argument") == {}
