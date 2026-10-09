"""The current-active type-check scope (directive s.27): every tracked
Python file in exactly one of ACTIVE, TESTS or LEGACY; the active scope
covering what the directive names; the mypy override held to exactly the
modules frozen by NF-1T provenance, and those modules really frozen; every
local ignore narrow and explained; legacy debt recorded apart."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import typecheck_scope as TS  # noqa: E402


def test_every_tracked_python_file_is_in_exactly_one_scope():
    files = TS._tracked()
    parts = TS.partition(files)
    assert sum(len(v) for v in parts.values()) == len(files)
    seen = [f for v in parts.values() for f in v]
    assert len(seen) == len(set(seen)) == len(files)


@pytest.mark.parametrize("required", [
    "qta_agent/events.py", "scientific/catalog.py",
    "scientific_ai/neural/model/network.py",
    "qta_multiphysics/stack/rust_kernel.py",
    "integrations/fmi/fmpy_runner.py", "integrations/fenicsx/runner.py",
    "qta_agent/proposals.py", "tools/harness_status.py",
    "tools/typecheck_scope.py", "ro_crate_tools.py",
])
def test_the_active_scope_covers_what_the_directive_names(required):
    assert TS.partition([required])["active"] == [required]


@pytest.mark.parametrize("legacy", [
    "qta_full_sim.py", "qta_multiphysics/runner.py",
    "package_consistency_check.py",
])
def test_the_hardware_era_tree_is_legacy_debt_not_active(legacy):
    assert TS.partition([legacy])["legacy"] == [legacy]


def test_a_sibling_directory_is_not_mistaken_for_an_active_one():
    """``tools`` is active; ``tools_old/x.py`` is not inside it."""
    assert TS.partition(["tools_old/x.py"])["legacy"] == ["tools_old/x.py"]
    assert TS.partition(["scientificx.py"])["legacy"] == ["scientificx.py"]


def test_the_mypy_override_is_exactly_the_frozen_list():
    assert TS.config_frozen() == set(TS.FROZEN)


def test_the_witnessed_environment_lock_is_not_where_typing_is_configured():
    """pyproject.toml is in the byte-reproduction witness's environment lock;
    the active scope's configuration is a file of its own so that typing
    work cannot make the witness stop applying (as a first draft did)."""
    prof = json.loads((ROOT / "docs" / "byte_reproduction_profile.json")
                      .read_text())
    assert "pyproject.toml" in prof["environment"]["lock"]
    assert TS.CONFIG.name not in prof["environment"]["lock"]
    assert "--config-file" in (ROOT / "tools" / "typecheck_scope.py"
                               ).read_text()


def test_the_frozen_modules_are_the_closure_the_dataset_pins():
    """An override that outlived its reason would be a silenced error. The
    reason is that the NF-1T dataset pins this model's implementation
    digest; if that stopped being true, or a frozen module left the
    closure, the override would have to go."""
    from scientific.identity import source_closure
    from scientific.models.surface_adsorption import SurfaceAdsorptionModel
    closure = source_closure(SurfaceAdsorptionModel.implementation_modules)
    assert closure is not None
    assert set(TS.FROZEN) <= set(closure)
    manifest = json.loads((ROOT / "docs" / "neural" / "dev" /
                           "dataset_manifest.json").read_text())
    pinned = json.dumps(manifest)
    digest = SurfaceAdsorptionModel().implementation_digest()
    assert f"surface.langmuir_capture@1.0.0#{digest}" in pinned


def test_the_override_disables_codes_and_never_whole_modules():
    sections = TS.config_sections()
    for mod, opts in sections.items():
        assert "ignore_errors" not in opts, mod
        if "disable_error_code" in opts:
            assert mod in TS.FROZEN
            assert "import-not-found" not in opts["disable_error_code"]
    import configparser
    cp = configparser.ConfigParser()
    cp.read_string(TS.CONFIG.read_text())
    assert "ignore_missing_imports" not in cp["mypy"], "no global ignore"
    assert "ignore_errors" not in cp["mypy"]


_IGNORE = re.compile(r"#\s*type:\s*ignore(\[[^\]]*\])?")


def test_every_ignore_in_the_active_scope_is_local_coded_and_explained():
    """No bare ``# type: ignore``; each names its error code and has a
    reason on its own line or within the three lines above it."""
    problems = []
    for rel in TS.partition(TS._tracked())["active"]:
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = _IGNORE.search(line)
            if not m or rel == "tools/typecheck_scope.py":
                continue
            if not m.group(1):
                problems.append(f"{rel}:{i + 1}: bare type: ignore")
                continue
            after = line[m.end():].strip()
            # another ignore is not a reason: strip them before looking
            above = _IGNORE.sub("", " ".join(lines[max(0, i - 3):i]))
            if not after.startswith("#") and "#" not in above:
                problems.append(f"{rel}:{i + 1}: no reason given")
    assert not problems, problems


def test_the_recorded_scope_matches_the_tool():
    doc = json.loads(TS.DOC.read_text())
    assert doc["active"] == list(TS.ACTIVE)
    assert doc["tests"] == list(TS.TESTS)
    assert doc["frozen"] == {m: TS.FROZEN[m] for m in sorted(TS.FROZEN)}
    debt = doc["legacy"]
    assert debt["errors"] >= 0 and debt["measured_files"] > 0
    assert sum(debt["by_file"].values()) == debt["errors"]
    assert all(TS.partition([f])["legacy"] == [f] for f in debt["by_file"])


@pytest.mark.parametrize("text, want", [
    ("x.py:1: error: no\nFound 3 errors in 2 files (checked 9 source "
     "files)", (3, 2)),
    ("Found 1 error in 1 file (checked 1 source file)", (1, 1)),
    ("Success: no issues found in 125 source files", (0, 0)),
])
def test_the_summary_is_read_from_mypy_not_assumed(text, want):
    assert TS.summary(text) == want


def test_a_mypy_that_printed_no_summary_is_not_clean():
    with pytest.raises(RuntimeError, match="no summary"):
        TS.summary("Traceback (most recent call last):\n  boom")


@pytest.mark.parametrize("code, out, want", [
    (1, "x.py:1: error: no\nFound 1 error in 1 file (checked 1 source "
        "file)", 1),
    (0, "Success: no issues found in 3 source files", 0),
    (2, "Success: no issues found in 3 source files", 1),
])
def test_check_fails_on_any_error_or_any_failure(monkeypatch, code, out,
                                                 want):
    seen = []

    def fake(targets, *extra):
        seen.append(extra)
        return code, out
    monkeypatch.setattr(TS, "_mypy", fake)
    assert TS.check() == want
    # the bodies of unannotated functions are checked too
    assert seen == [("--check-untyped-defs",)]
