"""The diagnostic must reproduce the pipeline it is diagnosing.

WHY THIS FILE EXISTS

`analysis/collect_container_3d.py` regenerates the canonical outputs so a
container's bytes can be compared against the committed copies. It called
``run_all(out, verbose=False)`` while ``qta_full_sim.py`` calls
``run_all(..., mc_samples=30)`` -- and ``run_all``'s default is 60.

So the collector regenerated ``multiphysics_summary.json`` with twice the
Monte Carlo samples, and every distribution differed. In an environment where
all 62 other files were byte-identical, that one file looked like exactly the
cross-environment divergence the collector was built to investigate. It was
the collector.

A diagnostic that does not reproduce the pipeline it is diagnosing
manufactures the divergence it was built to explain, and every conclusion
drawn from it is about the tool. These tests pin the correspondence.
"""
from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLLECTOR = ROOT / "analysis" / "collect_container_3d.py"
CANONICAL = ROOT / "qta_full_sim.py"


def _calls(path: Path, func: str) -> list:
    """Every call to ``func`` in ``path``, as (args, keywords) pairs."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name == func:
            out.append(node)
    return out


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def test_the_collector_exists_where_the_matrix_says_it_does():
    assert COLLECTOR.is_file(), (
        "docs/R59_CROSS_ENVIRONMENT_ANALYSIS.md and the completion matrix "
        "both point at this file")


def test_the_collector_passes_the_canonical_monte_carlo_sample_count():
    """The defect, pinned.

    Not "the collector passes 30" -- that would go stale the moment the
    canonical value changed. The assertion is that the two AGREE.
    """
    canonical_calls = [c for c in _calls(CANONICAL, "run_all")
                       if _kwarg(c, "mc_samples") is not None]
    assert canonical_calls, (
        "qta_full_sim.py no longer passes mc_samples to run_all; the "
        "correspondence this test pins has moved and must be re-established")
    canonical_value = _kwarg(canonical_calls[0], "mc_samples")

    collector_calls = _calls(COLLECTOR, "run_all")
    assert collector_calls, "the collector no longer calls run_all"
    for call in collector_calls:
        passed = _kwarg(call, "mc_samples")
        if passed is None:
            # It may pass the module constant rather than a literal.
            names = [kw.value.id for kw in call.keywords
                     if kw.arg == "mc_samples" and isinstance(kw.value,
                                                              ast.Name)]
            assert names, (
                "the collector calls run_all without mc_samples, so it uses "
                f"the default (60) while qta_full_sim.py passes "
                f"{canonical_value}. That regenerates "
                "multiphysics_summary.json with twice the Monte Carlo "
                "samples and manufactures a divergence.")
            source = COLLECTOR.read_text(encoding="utf-8")
            m = re.search(rf"^{names[0]}\s*=\s*(\d+)", source, re.M)
            assert m, f"{names[0]} is not a module-level integer constant"
            passed = int(m.group(1))
        assert passed == canonical_value, (
            f"the collector passes mc_samples={passed} and qta_full_sim.py "
            f"passes {canonical_value}; the diagnostic is measuring a "
            "different pipeline from the one under investigation")


def test_the_collector_uses_the_same_3d_entry_point_and_mode():
    """``heavy`` changes which meshes run. A mismatch there is the same
    class of defect as the sample count, and would be harder to spot."""
    canonical = _calls(CANONICAL, "_run_3d_all") or _calls(CANONICAL,
                                                           "run_3d_all")
    assert canonical, "qta_full_sim.py no longer calls run_3d_all"
    collector = _calls(COLLECTOR, "run_3d_all")
    assert collector, "the collector no longer calls run_3d_all"
    # The canonical call derives heavy from argv; the collector must use the
    # default (reduced CI mesh) that a plain `python qta_full_sim.py` uses.
    assert _kwarg(collector[0], "heavy") is False, (
        "the collector must regenerate with heavy=False, which is what a "
        "plain `python qta_full_sim.py` run does")


def test_the_collector_writes_only_into_a_caller_supplied_empty_directory():
    """Captured bytes must be freshly generated, never the committed copies
    read back by accident."""
    source = COLLECTOR.read_text(encoding="utf-8")
    assert "if any(out.iterdir())" in source
    assert "REFUSING" in source
    assert "NOT the committed repository copies" in source


def test_the_collector_records_the_fingerprint_the_analysis_relies_on():
    source = COLLECTOR.read_text(encoding="utf-8")
    for field in ("numpy", "scipy", "blas", "lapack", "simd", "cpu",
                  "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                  "PYTHONHASHSEED"):
        assert field in source, (
            f"the fingerprint no longer records {field!r}, which "
            "docs/R59_CROSS_ENVIRONMENT_ANALYSIS.md cites")


def test_the_analysis_document_states_what_it_could_not_establish():
    """An analysis that only lists findings reads as more complete than it
    is."""
    doc = (ROOT / "docs" / "R59_CROSS_ENVIRONMENT_ANALYSIS.md").read_text(
        encoding="utf-8")
    assert "**Not established**" in doc
    assert "could not be\n  downloaded" in doc or \
        "could not be downloaded" in doc.replace("\n  ", " ")
    assert "inference from `set -e` ordering" in doc
    assert "No tolerance was widened" in doc


# --- the comparison the collector never made --------------------------------

def test_the_collector_compares_against_the_committed_copies():
    """R59's actual measurement, which the collector did not perform.

    It captured hashes into an artifact and left the comparison to whoever
    downloaded the zip. When that zip turned out to be unreachable -- its
    signed URL points at a storage host some egress policies refuse -- the
    question "what did the 8-file divergence count measure" had no answer
    anywhere, because nothing had ever computed one.
    """
    import analysis.collect_container_3d as C

    assert hasattr(C, "compare_with_committed")
    assert hasattr(C, "emit_summary")


def test_the_comparison_counts_identical_differing_and_missing(tmp_path,
                                                               monkeypatch):
    import analysis.collect_container_3d as C

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "same.json").write_text("a")
    (outputs / "differs.json").write_text("b")
    monkeypatch.setattr(C, "REPO_ROOT", tmp_path)
    committed = tmp_path / "outputs"          # same dir stands in as committed
    _ = committed
    inventory = {
        "same.json": {"sha256": hashlib.sha256(b"a").hexdigest(), "size": 1},
        "differs.json": {"sha256": hashlib.sha256(b"XX").hexdigest(),
                         "size": 2},
        "absent.json": {"sha256": "0" * 64, "size": 0},
    }
    got = C.compare_with_committed(inventory)
    assert got["identical"] == 1
    assert got["differing"] == 1 and got["differing_files"] == ["differs.json"]
    assert got["not_committed"] == 1


def test_the_summary_is_greppable_out_of_a_job_log(capsys):
    """The markers are the retrieval route.

    An artifact needs a signed URL to a storage host; a job log is served by
    the logs API with no redirect at all, so the evidence has to be IN the
    log rather than pointed at from it.
    """
    import analysis.collect_container_3d as C

    C.emit_summary(
        {"python": "3.12.3", "numpy": "2.4.4", "cpu": {"model": "x",
                                                       "count": 4,
                                                       "flags": ["avx2"]}},
        {"regenerated": 63, "identical": 62, "differing": 1,
         "not_committed": 0, "differing_files": ["a.json"],
         "not_committed_files": [], "identical_files": []})
    out = capsys.readouterr().out
    assert "::QTA-3D-ENV::" in out
    assert "::QTA-3D-CPU::" in out
    assert "::QTA-3D-COMPARISON::" in out
    assert "::QTA-3D-DIFFERS:: a.json" in out
    assert "::QTA-3D-VERDICT:: DIVERGENT (1 file(s))" in out


def test_a_clean_comparison_reports_identical(capsys):
    import analysis.collect_container_3d as C

    C.emit_summary({}, {"regenerated": 63, "identical": 63, "differing": 0,
                        "not_committed": 0, "differing_files": [],
                        "not_committed_files": [], "identical_files": []})
    out = capsys.readouterr().out
    assert "::QTA-3D-VERDICT:: IDENTICAL (63/63" in out
    assert "::QTA-3D-DIFFERS::" not in out


def test_the_diagnostic_never_fails_the_build_on_a_divergence():
    """It reports; it does not gate.

    A diagnostic that exits non-zero on a byte difference has become a gate,
    and nothing in this repository outside the declared scientific
    authority is allowed to be one.
    """
    src = (ROOT / "analysis" / "collect_container_3d.py").read_text(
        encoding="utf-8")
    assert "DIAGNOSTIC ONLY: a divergence is reported, never a failure" in src


def test_the_container_script_reports_every_step():
    """R59 recorded steps 1-3 as passing by INFERENCE from set -e ordering.

    The job-logs API served only the pytest tail, so those steps' output was
    never actually read. A marker per step turns the inference into a
    record.
    """
    src = (ROOT / "container_verify.sh").read_text(encoding="utf-8")
    for step in ("environment", "git-available", "qta_full_sim",
                 "package_consistency", "manuscript_consistency",
                 "cross-environment-3d", "manifest_freshness"):
        assert f'step "{step}"' in src, f"{step} does not announce itself"
        assert f'done_ "{step}"' in src, f"{step} does not report success"


def test_the_container_installs_git_and_ships_the_repository():
    """48 governance tests died with FileNotFoundError in hosted run
    33113363458 because the image has no git.

    Installing git without shipping .git would be worse than neither: the
    binary exists, `git ls-files` exits 128 outside a repository, and a scan
    built on it returns an EMPTY set -- so every structural guard passes
    having examined nothing.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "install -y --no-install-recommends git" in dockerfile
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert not any(line.strip() == ".git" for line in ignore.splitlines()), (
        ".git is excluded from the build context, so the git binary above "
        "would find no repository")
