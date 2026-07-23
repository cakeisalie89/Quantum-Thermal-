"""Stage-7.5 runtime-resilience tests. MODEL-ONLY / FORECAST-ONLY.

Covers the additive checkpoint/restart driver and the fail-closed
--verify-existing package-checker mode. Software verification only; the
scientific gate PASS count remains zero. Mechanism tests use synthetic
markers and scratch copies — real stages are exercised by the staged
regeneration itself, not here (runtime).
"""
import json
import shutil
import subprocess
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import qta_sim_stages as Q                                       # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_stage_inventory_and_authority():
    assert Q.STAGES == ("core", "layers", "three_d")
    src = (ROOT / "qta_sim_stages.py").read_text()
    assert "authoritative clean full-run release path" in src
    # canonical orchestrator untouched by the driver (import-only reuse)
    assert "qta_full_sim" in src and "def main" in src


def test_config_id_binds_sources_env():
    c = Q._config_id()
    for k in ("orchestrator_sha256", "driver_sha256",
              "qta_multiphysics_tree_sha256", "environment"):
        assert c[k]
    assert c["seed"] == 42 and c["argv_profile"] == "standard"
    assert c == Q._config_id()          # deterministic


def test_snapshot_excludes_packaging_metadata():
    snap = Q._snapshot()
    assert "final_manifest.json" not in snap
    assert any(k.startswith("outputs/") for k in snap)
    assert "results_gate_table.csv" in snap


def _synthetic_marker(tmpdir, stage, config, outputs):
    d = tmpdir / ".sim_stage_checkpoints"
    d.mkdir(exist_ok=True)
    (d / f"{stage}.checkpoint.json").write_text(json.dumps(
        {"stage": stage, "config": config,
         "outputs_after_stage": outputs}))


def test_marker_fails_closed_on_config_and_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "CKDIR", tmp_path / ".sim_stage_checkpoints")
    good = Q._config_id()
    _synthetic_marker(tmp_path, "core", {**good, "seed": 43}, {})
    assert Q._marker_valid("core") is False          # config differs
    _synthetic_marker(tmp_path, "core", good,
                      {"results_gate_table.csv": "0" * 64})
    assert Q._marker_valid("core") is False          # hash mismatch
    _synthetic_marker(tmp_path, "core", good,
                      {"outputs/nonexistent_file.json": "0" * 64})
    assert Q._marker_valid("core") is False          # missing output
    real = Q._sha_file(ROOT / "results_gate_table.csv")
    _synthetic_marker(tmp_path, "core", good,
                      {"results_gate_table.csv": real})
    assert Q._marker_valid("core") is True           # consistent marker


def test_partial_never_complete_and_no_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "CKDIR", tmp_path / ".sim_stage_checkpoints")
    # no marker exists unless _write_marker ran after success; a killed
    # stage leaves nothing:
    assert Q._marker_valid("layers") is False
    # incompatible run (different config) can never satisfy a skip:
    _synthetic_marker(tmp_path, "layers",
                      {"environment": {"numpy": "9.9.9"}}, {})
    assert Q._marker_valid("layers") is False


def test_prerequisite_guard_refuses_out_of_order(tmp_path):
    scratch = tmp_path / "t"
    scratch.mkdir()
    # minimal fake tree: driver + orchestrator + package dir so config
    # hashing works; no checkpoints exist -> 'layers' must refuse.
    shutil.copy2(ROOT / "qta_sim_stages.py", scratch)
    shutil.copy2(ROOT / "qta_full_sim.py", scratch)
    (scratch / "qta_multiphysics").mkdir()
    (scratch / "qta_multiphysics" / "__init__.py").write_text("")
    r = subprocess.run([sys.executable, "qta_sim_stages.py", "layers"],
                       cwd=scratch, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 3
    assert "prerequisite" in r.stdout


def test_verify_existing_requires_sim_log():
    r = subprocess.run([sys.executable, "package_consistency_check.py",
                        "--verify-existing"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "verify-existing refuses to skip" in r.stdout
    assert "NOT the release gate" in r.stdout


def test_verify_existing_requires_exactly_89(tmp_path):
    scratch = tmp_path / "v"
    scratch.mkdir()
    for f in ("package_consistency_check.py", "qta_full_sim.py"):
        shutil.copy2(ROOT / f, scratch)
    out = scratch / "outputs"
    out.mkdir()
    for i in range(5):
        (out / f"f{i}.json").write_text("{}")
    log = scratch / "log.txt"
    log.write_text("clean\n")
    r = subprocess.run([sys.executable, "package_consistency_check.py",
                        "--verify-existing", "--sim-log", str(log)],
                       cwd=scratch, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 1
    assert "exactly 89 files" in r.stdout


def test_default_regeneration_branch_preserved():
    src = (ROOT / "package_consistency_check.py").read_text()
    assert "shutil.rmtree(gen_outputs_dir, ignore_errors=True)" in src
    assert "qta_full_sim.py executes (exit 0)" in src
    assert src.count("--verify-existing") >= 2
    assert "full regeneration remains authoritative" in src


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]

if __name__ == "__main__":
    print("run via pytest (uses tmp_path/monkeypatch fixtures):")
    print("  .venv/bin/python -m pytest tests/test_stage7_5_resilience.py")
    sys.exit(0)
