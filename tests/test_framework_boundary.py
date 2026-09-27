"""ACTIVE FRAMEWORK vs LEGACY QTA, held (directive 10, 28).

The boundary is measured by ``tools/framework_boundary.py`` from the import
graph and ``FILE_DISPOSITION.csv``. Here: the repository satisfies it, and
every rule is shown catching the thing it exists for -- a legacy import
planted into a clean scientific module, directly, lazily inside a function,
two modules away, and through a package initialiser; a legacy file named; a
legacy-ontology module that no disposition knows about. Every plant is a
source override; the tree is not touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import framework_boundary as FB  # noqa: E402

CLEAN = "scientific/models/thermal_1d.py"
CLEAN_MOD = "scientific.models.thermal_1d"


def _plant(path: str, extra: str) -> "FB.Boundary":
    src = (ROOT / path).read_text(encoding="utf-8")
    return FB.Boundary(sources={path: src + "\n" + extra + "\n"})


@pytest.fixture(scope="module")
def real():
    return FB.Boundary()


def test_the_repository_holds_the_boundary(real):
    assert real.problems() == []


def test_the_boundary_is_not_vacuous(real):
    """Anti-vacuity for the check above: there is an active side with the
    framework in it, a legacy side with the machine in it, and the clean
    module the plants use is on the active side."""
    active = set(real.modules("ACTIVE"))
    assert CLEAN_MOD in active
    assert {"qta_agent.store", "scientific.result"} <= active
    assert {"qta_multiphysics.machine_fsm",
            "qta_multiphysics.hardware_governance_3d"} <= real.legacy_modules
    assert "BOM.csv" in real.legacy_files


@pytest.mark.parametrize("plant", [
    "from qta_multiphysics import machine_fsm",
    "import qta_multiphysics.hardware_governance_3d",
    "def _later():\n    from qta_multiphysics.mode_sequence_3d import MODES",
])
def test_a_legacy_import_in_a_clean_module_is_caught(plant):
    found = _plant(CLEAN, plant).problems()
    assert any(p.startswith(f"IMPORT: {CLEAN_MOD} reaches legacy")
               for p in found), found


def test_a_legacy_import_two_modules_away_is_caught_and_the_path_named():
    """Transitive: the clean module imports an active module that imports
    legacy. Both are reported, and the chain says how."""
    b = FB.Boundary(sources={
        "scientific/quantity.py":
            (ROOT / "scientific/quantity.py").read_text(encoding="utf-8")
            + "\nfrom qta_multiphysics import machine_fsm\n",
    })
    found = [p for p in b.problems()
             if "machine_fsm" in p and p.startswith("IMPORT: scientific.")]
    assert found, "the planted chain was not reached"
    via = [p for p in found if p.startswith(f"IMPORT: {CLEAN_MOD} ")] \
        or found
    assert "scientific.quantity > qta_multiphysics.machine_fsm" in via[0]


def test_a_package_initialiser_reaching_legacy_taints_every_submodule():
    """The edge this commit removed. Put the lazy run_all back into the
    package __init__ and every active module of the package reaches the
    legacy orchestrator -- because importing any of them runs it."""
    init = "qta_multiphysics/__init__.py"
    b = _plant(init, "def run_all(*a, **k):\n"
                     "    from .runner import run_all as _r\n"
                     "    return _r(*a, **k)")
    tainted = {p.split()[1] for p in b.problems()
               if p.startswith("IMPORT: ")}
    assert "qta_multiphysics.thermal_1d" in tainted
    # heat_switch_3d imports nothing from its own package: only the edge
    # "importing a submodule runs its package's __init__" reaches it.
    assert "qta_multiphysics.heat_switch_3d" in tainted
    assert len(tainted) > 20, sorted(tainted)


@pytest.mark.parametrize("literal", ["BOM.csv", "./hardware_registry.json",
                                     "results_gate_table.csv"])
def test_naming_a_legacy_file_is_caught(literal):
    found = _plant(CLEAN, f"_REGISTRY = {literal!r}").problems()
    assert any(p.startswith(f"OPEN: {CLEAN_MOD} names legacy file")
               and literal in p for p in found), found


def test_a_generic_name_shared_with_a_nested_legacy_file_is_not_caught():
    """SHA256SUMS exists under the legacy stage-9 release directory; a
    generic bundle's SHA256SUMS is not that file."""
    assert _plant(CLEAN, "_SUMS = 'SHA256SUMS'").problems() == []
    found = _plant(CLEAN, "_S = 'QTA_stage9_release_verification/"
                          "SHA256SUMS'").problems()
    assert any(p.startswith("OPEN:") for p in found), found


def test_a_legacy_ontology_module_no_disposition_knows_is_caught():
    """Moved or renamed out of the disposition's sight, it is still legacy
    by name."""
    b = FB.Boundary(sources={
        "scientific/_planted_fsm.py": "STATES = ()\n",
        "scientific/machine_fsm_v2.py": "STATES = ()\n",
        CLEAN: (ROOT / CLEAN).read_text(encoding="utf-8")
               + "\nfrom scientific import machine_fsm_v2\n",
    })
    found = b.problems()
    assert any(p.startswith(f"ONTOLOGY: {CLEAN_MOD} imports")
               and "machine_fsm_v2" in p for p in found), found


def test_an_unclassified_production_file_is_a_finding():
    b = FB.Boundary(sources={"scientific/_planted.py": "X = 1\n"})
    assert any(p.startswith("UNCLASSIFIED: scientific/_planted.py")
               for p in b.problems())


def test_tests_are_out_of_scope(real):
    """A test of legacy code imports it on purpose."""
    assert not any(m.startswith("tests.") for m in real.modules("ACTIVE"))
    assert any(p.startswith("tests/") and "hardware_governance" in p
               for p in real.text)


def test_every_declared_exception_still_excuses_something(real):
    assert real.stale_exceptions() == []
    b = FB.Boundary(sources={
        "qta_multiphysics/stack/rag_index.py":
            (ROOT / "qta_multiphysics/stack/rag_index.py").read_text(
                encoding="utf-8").replace("QTA_full_history-6.bundle.txt",
                                          "some-other-file.txt")})
    assert any(p.startswith("STALE:") for p in b.problems())


def test_an_exception_covers_only_what_it_names():
    """rag_index may name the history bundle it excludes; naming the BOM
    too is not covered by that."""
    found = _plant("qta_multiphysics/stack/rag_index.py",
                   "_ALSO = 'BOM.csv'").problems()
    assert any("rag_index names legacy file 'BOM.csv'" in p
               for p in found), found


def test_the_report_carries_the_checkpoint_counts(real):
    r = real.report()
    assert r["active_importing_legacy"] == 0
    assert r["active_opening_legacy_files"] == 0
    assert r["active_importing_legacy_ontology"] == 0
    assert r["active_modules"] == len(real.modules("ACTIVE")) > 50
    assert isinstance(r["transitional_reaching_legacy"], list)
    assert r["active_mode_letter_labels"] >= 0


def test_the_cli_fails_on_a_violation(monkeypatch, capsys):
    planted = _plant(CLEAN, "from qta_multiphysics import machine_fsm")
    monkeypatch.setattr(FB, "Boundary", lambda: planted)
    assert FB.main(["--check"]) == 1
    assert "IMPORT:" in capsys.readouterr().out
