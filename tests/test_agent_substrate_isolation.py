"""The substrate is infrastructure, not science -- asserted, not merely stated.

`AUTHORITIES.md`, `authorities.json`, and `AGENT_SUBSTRATE.md` all claim
`automatic_gate_effect = NONE`: that `qta_agent/` cannot read, write, or
influence any of the 83 gates, and that PASS = 0 is unaffected by anything it
does. Prose cannot enforce that. One `import` from a solver would make every
one of those sentences false while every other test in the repository stayed
green.

These tests are the enforcement. They fail the moment the claim stops being
true, which is the only form in which a claim of this kind is worth making.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PKG = ROOT / "qta_agent"

#: Test modules are the substrate's only legitimate consumers today. Adding a
#: consumer here is a deliberate act that this list makes visible in review.
ALLOWED_IMPORTERS = {
    "tests/test_agent_substrate.py",
    "tests/test_agent_substrate_properties.py",
    "tests/test_agent_evidence.py",
    "tests/test_agent_substrate_isolation.py",
}

#: A linearization of the dependency graph. Each module may import only
#: modules strictly earlier in this tuple, which keeps the graph a line
#: rather than a web and makes "the log is the truth" structurally true
#: instead of aspirational.
LAYERS = ("canonical", "events", "evidence", "authority", "store",
          "invalidation", "reconstruct")


def _modules():
    return sorted(p for p in PKG.glob("*.py") if p.name != "__init__.py")


def _internal_imports(path: Path) -> set:
    """Relative imports of sibling modules, including lazy ones inside bodies.

    ``ast.walk`` rather than a scan of module-level statements: several call
    sites import lazily to avoid an import cycle, and a lazy dependency is
    still a dependency.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                deps.add(node.module.split(".")[0])
            else:
                deps.update(a.name.split(".")[0] for a in node.names)
    return deps


def _all_imported_names(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


# --- the substrate does not reach into the science ---------------------------

def test_no_substrate_module_imports_the_scientific_tree():
    """The direction that would let the substrate change a computed result."""
    forbidden = {"qta_multiphysics", "qta_full_sim", "metrics", "runner_3d",
                 "convergence_3d", "uncertainty", "config"}
    bad = []
    for mod in _modules():
        hit = _all_imported_names(mod) & forbidden
        if hit:
            bad.append(f"{mod.name}: {sorted(hit)}")
    assert not bad, (
        "qta_agent must not import the scientific tree; "
        f"automatic_gate_effect=NONE would be false: {bad}")


def test_the_substrate_imports_only_the_standard_library_and_itself():
    """No third-party dependency, so the layer cannot fail for supply reasons.

    An authority layer that stops working when a wheel is unavailable is an
    authority layer that stops working. Hypothesis appears in its property
    tests, not in the code under test.
    """
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for mod in _modules():
        for name in _all_imported_names(mod):
            if name in stdlib or name == "qta_agent":
                continue
            bad.append(f"{mod.name}: {name}")
    assert not bad, f"non-stdlib dependency in the substrate: {bad}"


# --- the science does not reach into the substrate ---------------------------

def test_nothing_outside_the_package_imports_the_substrate():
    """The direction that would make a gate depend on an authority verdict.

    Uses git grep over tracked files so an untracked scratch file cannot make
    this pass or fail spuriously.
    """
    r = subprocess.run(
        ["git", "-C", str(ROOT), "grep", "-lE",
         r"^\s*(from|import)\s+qta_agent\b", "--", "*.py"],
        capture_output=True, text=True)
    importers = {line.strip() for line in r.stdout.splitlines() if line.strip()}
    importers = {p for p in importers if not p.startswith("qta_agent/")}
    unexpected = importers - ALLOWED_IMPORTERS
    assert not unexpected, (
        "qta_agent is imported outside its own package and tests, so a "
        f"scientific result could now depend on an authority verdict: "
        f"{sorted(unexpected)}")


def test_the_substrate_is_absent_from_the_gate_pipeline():
    """A gate must not be computable only when the substrate is importable."""
    r = subprocess.run(
        ["git", "-C", str(ROOT), "grep", "-l", "qta_agent", "--",
         "qta_full_sim.py", "qta_multiphysics/", "Snakefile",
         "generate_manifest.py"],
        capture_output=True, text=True)
    assert r.stdout.strip() == "", (
        f"the gate pipeline references qta_agent:\n{r.stdout}")


# --- the declared layering is the real layering ------------------------------

def test_every_module_is_placed_in_the_declared_layering():
    placed = set(LAYERS)
    actual = {m.stem for m in _modules()}
    assert actual == placed, (
        "a module exists that the declared layering does not place; "
        "an unplaced module has no stated position in the dependency line: "
        f"{sorted(actual ^ placed)}")


@pytest.mark.parametrize("name", LAYERS)
def test_a_module_imports_only_strictly_earlier_layers(name):
    """Rejects cycles and back-edges alike, including lazy ones.

    ``authority`` imports ``evidence`` inside a function body to avoid an
    import cycle at module load. That is still a dependency and is still
    ordered here -- hiding a back-edge behind a deferred import would defeat
    the whole point of declaring the order.
    """
    earlier = set(LAYERS[:LAYERS.index(name)])
    deps = _internal_imports(PKG / f"{name}.py")
    late = deps - earlier
    assert not late, (
        f"{name}.py imports {sorted(late)}, which is not strictly earlier "
        f"than it in the declared layering {LAYERS}")


def test_the_layering_makes_canonical_the_only_root():
    """Everything reduces to one definition of 'the same bytes'."""
    assert _internal_imports(PKG / "canonical.py") == set()
    for name in LAYERS[1:]:
        assert (PKG / f"{name}.py").exists()


def test_reconstruct_does_not_import_the_implementation_it_checks():
    """Differential verification is worthless if both sides share code.

    ``reconstruct.py`` exists to disagree with ``store.py``. If it imported
    ``AuthorityStore`` it would inherit the same bug and agree anyway -- a
    green comparison that proves only that one implementation equals itself.

    Docstrings are stripped before the name check, because the module
    docstring legitimately explains that it does *not* import that class.
    """
    assert "store" not in _internal_imports(PKG / "reconstruct.py")

    tree = ast.parse((PKG / "reconstruct.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                first.value.value = ""
    code = ast.unparse(tree)
    assert "AuthorityStore" not in code, (
        "reconstruct.py names AuthorityStore in executable code, so it is "
        "not the independent implementation it claims to be")


# --- the registry says what the code does ------------------------------------

def test_the_registry_declares_the_substrate_has_no_gate_effect():
    reg = json.loads((ROOT / "authorities.json").read_text(encoding="utf-8"))
    entry = reg["authorities"]["agent_authority_substrate"]
    assert entry["automatic_gate_effect"] == "NONE"
    assert "does_not_mean" in entry, (
        "the boundary between provenance and physics must be explicit")
    blob = " ".join(entry["does_not_mean"].split()).lower()
    # Matched on substance, not typography: a test that breaks when someone
    # rewraps a paragraph is a test that gets loosened rather than fixed.
    assert re.search(r"pass\s*=\s*0", blob), (
        "the disclaimer does not state that PASS=0 is unaffected")
    assert "gate" in blob, "the disclaimer does not mention the gates"


def test_every_registered_substrate_module_exists():
    reg = json.loads((ROOT / "authorities.json").read_text(encoding="utf-8"))
    entry = reg["authorities"]["agent_authority_substrate"]
    cited = set(re.findall(r"qta_agent/(\w+)\.py", entry["authority"]))
    assert cited, "the registry must name the modules it governs"
    missing = [c for c in cited if not (PKG / f"{c}.py").exists()]
    assert not missing, f"registry names modules that do not exist: {missing}"


def test_the_narrative_document_mirrors_the_registry():
    """`AGENT_SUBSTRATE.md` may elaborate; it may not contradict."""
    doc = (ROOT / "AGENT_SUBSTRATE.md").read_text(encoding="utf-8")
    flat = " ".join(doc.replace("*", " ").split())
    assert "automatic_gate_effect" in doc and "NONE" in doc
    assert re.search(r"PASS\s*=\s*0 is unaffected", flat), (
        "AGENT_SUBSTRATE.md must state that PASS=0 is unaffected")
    for name in LAYERS:
        assert f"`{name}.py`" in doc, (
            f"{name}.py is governed but not described in AGENT_SUBSTRATE.md")
