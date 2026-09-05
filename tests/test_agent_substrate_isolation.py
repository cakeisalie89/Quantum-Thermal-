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
    "tests/test_agent_checkpoint.py",
    "tests/test_agent_execution.py",
    "tests/test_agent_governed_stage10.py",
    "tests/test_agent_audit.py",
    "tests/test_agent_policy.py",
    "tests/test_agent_scheduler.py",
    "tests/test_agent_netauth.py",
    "tests/test_agent_secrets.py",
    "tests/test_agent_memory.py",
    "tests/test_agent_context.py",
    "tests/test_agent_agents.py",
    "tests/test_agent_concurrency.py",
    "tests/test_agent_crash_recovery.py",
    "tests/test_agent_substrate_isolation.py",
}

#: NOTE for whoever adds the next test file and sees this pass locally:
#: the check below uses ``git grep``, which sees TRACKED files only. A new
#: test importing qta_agent is invisible to it until it is staged, so the
#: suite goes green locally and red in CI the moment it is committed. That has
#: happened twice. Run this test after ``git add``, or expect the hosted run
#: to be the thing that tells you.
#:
#: Scanning the filesystem instead would fix the surprise and break the
#: invariant: an untracked scratch file is not part of the repository, so it
#: cannot make the repository's import graph wrong. The tracked set is the
#: right question; only the timing is the trap.

#: A linearization of the dependency graph. Each module may import only
#: modules strictly earlier in this tuple, which keeps the graph a line
#: rather than a web and makes "the log is the truth" structurally true
#: instead of aspirational.
LAYERS = ("canonical", "actions", "events", "evidence", "capability", "tools",
          "execution", "checkpoint", "authority", "policy", "secrets",
          "netauth", "store", "invalidation", "reconstruct", "tasks",
          "scheduler", "memory", "context", "agents", "audit",
          "_stage10_tool", "governed_stage10")

#: The ONLY modules permitted to reach into the scientific tree, and the only
#: thing they may reach for.
#:
#: The direction matters and the two are not symmetric. Science importing the
#: agent would let a gate depend on an authority verdict, which is the failure
#: this whole package is built to make impossible -- that stays absolutely
#: forbidden. The agent importing a workflow is what GOVERNING a workflow
#: means; a control plane with no reachable production path is a library.
#:
#: So the crossing is allowed, named, and narrow: two bridge modules, one
#: import, and it is the Stage-10 WRITE GUARD rather than any solver. A
#: governed run is therefore subject to exactly the same write allowlist as an
#: ungoverned one -- the substrate adds authority, it does not replace the
#: guard that was already there.
BRIDGE_MODULES = {
    "governed_stage10": {"qta_multiphysics"},
    "_stage10_tool": {"qta_multiphysics"},
}

#: Reaching any of these from qta_agent would make an authority verdict able
#: to influence a computed result. Forbidden for EVERY module, bridges too.
FORBIDDEN_SCIENTIFIC = {
    "qta_full_sim", "metrics", "runner_3d", "convergence_3d", "uncertainty",
    "config", "machine_fsm", "mode_sequence_3d", "state_machine_3d",
}


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

def test_no_substrate_module_reaches_a_solver_or_a_gate():
    """The direction that would let an authority verdict change a result.

    Absolute, and it applies to the bridge modules too: a bridge may call the
    Stage-10 write guard, and may not call anything that computes physics.
    """
    bad = []
    for mod in _modules():
        hit = _all_imported_names(mod) & FORBIDDEN_SCIENTIFIC
        if hit:
            bad.append(f"{mod.name}: {sorted(hit)}")
    assert not bad, (
        "qta_agent reached a solver or gate module; automatic_gate_effect="
        f"NONE would be false: {bad}")


def test_only_the_declared_bridges_reach_into_the_scientific_tree():
    """Crossing the boundary is deliberate, named, and narrow.

    A new crossing means editing BRIDGE_MODULES, which puts it in the diff and
    in review rather than arriving as an incidental import.
    """
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for mod in _modules():
        allowed = BRIDGE_MODULES.get(mod.stem, set())
        for name in _all_imported_names(mod):
            if name in stdlib or name == "qta_agent" or name in allowed:
                continue
            bad.append(f"{mod.stem}: {name}")
    assert not bad, (
        "a module reached outside the standard library and its own package "
        f"without being a declared bridge: {bad}")


def test_the_core_substrate_is_stdlib_only():
    """Everything that is NOT a bridge must run with no dependency at all.

    An authority layer that stops working when a wheel is unavailable is an
    authority layer that stops working. Hypothesis appears in its property
    tests, not in the code under test. Bridges are exempt by definition --
    calling a workflow is what they are for.
    """
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for mod in _modules():
        if mod.stem in BRIDGE_MODULES:
            continue
        for name in _all_imported_names(mod):
            if name in stdlib or name == "qta_agent":
                continue
            bad.append(f"{mod.stem}: {name}")
    assert not bad, f"non-stdlib dependency in the core substrate: {bad}"


def test_a_bridge_may_only_reach_the_stage10_write_guard():
    """Named narrowly: the guard, not the package.

    ``qta_multiphysics`` is a large tree. A bridge that may import any of it
    could import a solver tomorrow without changing the allowlist, so the
    permitted SYMBOL is pinned too.
    """
    for stem in BRIDGE_MODULES:
        src = (PKG / f"{stem}.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if "qta_multiphysics" not in stripped:
                continue
            if stripped.startswith("#") or stripped.startswith(('"', "'")):
                continue
            assert "qta_multiphysics.stack" in stripped, (
                f"{stem}.py reaches qta_multiphysics outside the stack "
                f"workspace layer: {stripped}")


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


def test_no_gate_computing_module_references_the_substrate():
    """A gate must not be computable only when the substrate is importable.

    The Snakefile is deliberately excluded here and checked separately below:
    it holds both gate rules and Stage-10 rules, and the governed Stage-10
    rule referencing qta_agent is the production integration rather than a
    violation. Everything that actually computes a gate is checked with no
    exception at all.
    """
    r = subprocess.run(
        ["git", "-C", str(ROOT), "grep", "-l", "qta_agent", "--",
         "qta_full_sim.py", "qta_multiphysics/", "generate_manifest.py"],
        capture_output=True, text=True)
    assert r.stdout.strip() == "", (
        f"a gate-computing module references qta_agent:\n{r.stdout}")


def test_the_workflow_touches_the_substrate_only_in_the_governed_rule():
    """The production integration is confined to one named rule.

    A gate rule that imported qta_agent would make a scientific result depend
    on whether the authority layer is importable, which is exactly what
    automatic_gate_effect=NONE denies. Confining the reference to
    ``s10_governed`` keeps the integration real and keeps that denial true.
    """
    raw = (ROOT / "Snakefile").read_text(encoding="utf-8")
    # Comment lines are stripped first. A block that ends where the next rule
    # BEGINS also swallows that rule's preceding comments, so the explanation
    # above s10_governed would otherwise be attributed to whatever rule came
    # before it -- a false positive that says nothing about the code.
    text = "\n".join(line for line in raw.splitlines()
                     if not line.lstrip().startswith("#"))
    offenders = []
    for block in text.split("\nrule ")[1:]:
        name = block.split(":", 1)[0].strip()
        body = block.split("\n", 1)[1] if "\n" in block else ""
        if "qta_agent" in body and name != "s10_governed":
            offenders.append(name)
    assert not offenders, (
        f"rules other than s10_governed reference qta_agent: {offenders}")
    assert "qta_agent" in raw, (
        "the workflow no longer references the substrate at all, so the "
        "production path is not being exercised")


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
