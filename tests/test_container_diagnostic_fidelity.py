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
