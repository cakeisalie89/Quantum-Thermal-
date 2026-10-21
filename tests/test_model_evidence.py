"""Three status systems, none of which may answer for another (Gate B, B6).

``tools/model_evidence.py`` states what scientific and model evidence
exists. It must be derivable, it must not read the software-completion or
readiness registries, and they must not read it -- so a green software
contract cannot become a model result, and a model run cannot become
production readiness. A real-model run is counted only from a bundle naming
the owner's authorization, and then only as RECORDED_NOT_VERIFIED.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import model_evidence as ME  # noqa: E402

REGISTRIES = {"model_evidence": "scientific_model_evidence",
              "harness_status": "harness_status",
              "readiness": "readiness_matrix"}


def _copy(tmp_path) -> Path:
    root = tmp_path / "repo"
    for rel in ("docs/research/grounding_bench_results.json",
                "docs/neural/current_status.json", ME.FIXTURE_CYCLE):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dst)
    return root


def _bundle(**over) -> dict:
    b = {"schema": "model-run-bundle/1", "run_id": "run-1",
         "authorization": "OD-1, granted by the owner on 2027-01-01",
         "commit": "a" * 40, "log_sha256": "b" * 64, "requests": 3,
         "cost_micro": 1200, "provider_claimed": "some-provider",
         "note": "a fixture bundle for the test"}
    b.update(over)
    return b


def test_the_committed_registry_is_what_the_repository_derives():
    assert ME.REGISTRY.read_text(encoding="utf-8") == ME.render(ME.derive())


def test_no_language_model_has_been_called():
    doc = ME.derive()
    assert doc["verdict"] == {"REAL_MODEL_EVIDENCE": "NOT_RUN"}
    kinds = {e["kind"]: e["status"] for e in doc["entries"]}
    assert kinds["FIXTURE_MODEL_CYCLE"] == "FIXTURE_ONLY"
    assert kinds["GROUNDING_BENCHMARK"] == "SCRIPTED_AGENT_EVIDENCE_ONLY"
    assert kinds["REAL_WORLD_MEASUREMENT"] == "NONE"


def _reaches(path: Path) -> tuple:
    """(imported module names, string constants) of a tool, docstrings
    excluded: what the code can READ, not what its prose names."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)) and node.body and \
                isinstance(node.body[0], ast.Expr) and \
                isinstance(node.body[0].value, ast.Constant):
            docs.add(id(node.body[0].value))
    mods, consts = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            consts.append(node.value)
    return mods, consts


@pytest.mark.parametrize("tool", sorted(REGISTRIES))
def test_no_registry_reads_another(tool):
    mods, consts = _reaches(ROOT / "tools" / f"{tool}.py")
    for other, filename in REGISTRIES.items():
        if other == tool:
            continue
        assert other not in mods, f"tools/{tool}.py imports {other}"
        assert not any(filename in c for c in consts), (
            f"tools/{tool}.py reaches {filename}: a registry that reads "
            "another can let one verdict stand in for the other")


def test_a_run_bundle_counts_as_recorded_and_never_as_verified(tmp_path):
    root = _copy(tmp_path)
    runs = root / "docs" / "model_runs"
    runs.mkdir(parents=True)
    (runs / "run-1.json").write_text(json.dumps(_bundle()))
    doc = ME.derive(root)
    assert doc["verdict"] == {"REAL_MODEL_EVIDENCE": "RECORDED_NOT_VERIFIED"}
    assert doc["entries"][0]["requests"] == 3
    assert "VERIFIED" not in {e["status"] for e in doc["entries"]}


@pytest.mark.parametrize("over, why", [
    ({"authorization": "the agent decided to"}, "OD-1"),
    ({"commit": "HEAD"}, "malformed"),
    ({"requests": -1}, "count"),
    ({"verified": True}, "exactly"),
    ({"schema": "model-run-bundle/9"}, "schema"),
])
def test_a_bundle_that_cannot_be_stated_honestly_is_refused(tmp_path, over,
                                                            why):
    root = _copy(tmp_path)
    runs = root / "docs" / "model_runs"
    runs.mkdir(parents=True)
    (runs / "run-1.json").write_text(json.dumps(_bundle(**over)))
    with pytest.raises(ME.Refused, match=why):
        ME.derive(root)


def test_every_status_used_is_defined():
    doc = ME.derive()
    for e in doc["entries"]:
        assert e["status"] in doc["statuses"]
