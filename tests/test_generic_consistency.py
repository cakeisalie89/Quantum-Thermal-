"""The generic consistency verifier (directive 17), on a real governed history.

``tools/generic_consistency.py`` checks what the framework produces -- the
event history, the evidence it cites, the scientific results decided from
them -- and nothing of QTA. Here: a genuine governed model run is consistent
on every check, and each check is shown finding the thing it exists for -- a
broken chain, a tampered or lost evidence object, a forged admission, a live
store that disagrees with the independent reader, a document that does not
parse or is about another bundle, a model or check the catalog does not
admit. The history is produced by the governed path itself, then copied for
each test to damage.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import generic_consistency as GC  # noqa: E402
from qta_agent.authority import State  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import REVIEWER_ID, GovernedModelRuns  # noqa: E402
from qta_agent.reconstruct import Divergence  # noqa: E402

WS = "verification/stage10/_pytest_generic_consistency"
MODEL = {"model_id": "thermal.conduction_1d", "model_version": "1.0.0"}
PARAMS = {"n_cells": 60, "n_eval": 20}
CHECK = "thermal_1d.reduction_2d_radial_disabled"


@pytest.fixture(scope="module")
def world():
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    (base / "genuine").mkdir(parents=True)
    g = GovernedModelRuns(root=ROOT,
                          log=EventLog(base / "genuine" / "log.jsonl"),
                          evidence=EvidenceStore(base / "genuine" /
                                                 "evidence"))
    run = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/genuine/run")
    chk = g.check(run, check_id=CHECK, out_dir=f"{WS}/genuine/check")
    assert g.decide(run, chk).state is State.VERIFIED
    yield run, chk
    if base.exists():
        shutil.rmtree(base)


def _copy(name: str) -> Path:
    src, dst = ROOT / WS / "genuine", ROOT / WS / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copytree(src / "evidence", dst / "evidence")
    shutil.copy(src / "log.jsonl", dst / "log.jsonl")
    return dst


def _verify(base: Path) -> dict:
    return GC.verify(base / "log.jsonl", base / "evidence")


def _blob(base: Path, sha: str) -> Path:
    path = EvidenceStore(base / "evidence")._blob_path(sha)
    assert path.is_file(), sha
    return path


def test_a_genuine_history_is_consistent_on_every_check(world):
    run, _ = world
    result = _verify(_copy("control"))
    assert all(result[c] == [] for c in GC.CHECKS), result
    assert any(run.record_id in n and "ADMITTED" in n
               and "this tree" in n for n in result["notes"]), result
    base = ROOT / WS / "control"
    assert GC.main([str(base / "log.jsonl"), "--evidence",
                    str(base / "evidence")]) == GC.OK


def test_a_broken_chain_is_a_finding(world):
    base = _copy("chain")
    lines = (base / "log.jsonl").read_text(encoding="utf-8").splitlines(True)
    mid = len(lines) // 2
    event = json.loads(lines[mid])
    event["actor"] = "someone-else"         # content, not layout: the chain
    lines[mid] = json.dumps(event) + "\n"   # hashes canonical JSON
    (base / "log.jsonl").write_text("".join(lines), encoding="utf-8")
    result = _verify(base)
    assert result["EVENT_HISTORY"], result
    assert GC.main([str(base / "log.jsonl"), "--evidence",
                    str(base / "evidence")]) == GC.FINDING


def test_a_tampered_evidence_object_is_a_finding(world):
    run, _ = world
    base = _copy("tampered")
    blob = _blob(base, run.bundle_sha256)
    blob.write_bytes(blob.read_bytes() + b" ")
    assert _verify(base)["EVIDENCE"]


def test_a_tampered_object_no_record_cites_is_a_finding(world):
    """Reading a cited digest re-hashes it, so a cited object's damage is
    seen twice. An object no authority record cites -- a task artifact, a
    report nobody decided on -- is seen only by re-hashing the store."""
    base = _copy("uncited")
    sha = EvidenceStore(base / "evidence").put(b"an artifact nobody cites")
    blob = _blob(base, sha)
    blob.write_bytes(b"something else")
    result = _verify(base)
    assert any(f.startswith("store:") and sha[:12] in f
               for f in result["EVIDENCE"]), result
    assert result["RECONSTRUCTION"] == [], result


def test_lost_evidence_is_a_finding(world):
    _, chk = world
    base = _copy("lost")
    _blob(base, chk.report_sha256).unlink()
    result = _verify(base)
    assert any("does not resolve" in f for f in result["EVIDENCE"]), result
    assert any(f.startswith("undecidable:")
               for f in result["RECONSTRUCTION"]), result


def test_a_forged_admission_is_a_finding(world):
    """A VERIFIED transition naming no admission policy -- the shape a
    history written before admission policies has -- refused by the
    independent reader and by the live store alike."""
    run, chk = world
    base = _copy("forged")
    g = GovernedModelRuns(root=ROOT, log=EventLog(base / "log.jsonl"),
                          evidence=EvidenceStore(base / "evidence"))
    rid = "forged-no-policy"
    rec = g.authority.get(run.record_id)
    g.authority.create(record_id=rid, kind=rec.kind, proposer=rec.proposer,
                       evidence={"result_bundle": run.bundle_sha256})
    EventLog(base / "log.jsonl").append(
        actor=REVIEWER_ID, action="record.transition", target=rid,
        payload={"record_id": rid, "src": "PROPOSED", "dst": "VERIFIED",
                 "role": "VERIFIER",
                 "evidence": {"verification_report": chk.report_sha256},
                 "policy_id": None, "stale_reason": None,
                 "edge_reason": "forged", "idempotency_key": None})
    found = _verify(base)["RECONSTRUCTION"]
    assert any(f.startswith("refused:") and rid in f for f in found), found
    assert any("live store refuses" in f for f in found), found


def test_the_live_store_is_compared_with_the_independent_reader(
        world, monkeypatch):
    monkeypatch.setattr(GC.rc, "compare", lambda store, recon: (
        Divergence("r", "state", "VERIFIED", "PROPOSED"),))
    found = _verify(_copy("compare"))["RECONSTRUCTION"]
    assert any("disagree" in f for f in found), found


def _fetch(base: Path):
    return EvidenceStore(base / "evidence").get


def _put(base: Path, doc) -> str:
    return EvidenceStore(base / "evidence").put(
        json.dumps(doc, indent=1, sort_keys=True).encode())


def test_the_genuine_documents_read_back_as_what_they_claim(world):
    run, chk = world
    base = ROOT / WS / "genuine"
    bundle, vr, found = GC.read_result(
        {"result_bundle": run.bundle_sha256,
         "verification_report": chk.report_sha256}, _fetch(base))
    assert found == [] and bundle is not None and vr is not None
    assert vr.subject_digest == bundle.digest()


def test_a_report_about_another_bundle_is_a_finding(world):
    run, chk = world
    base = _copy("subject")
    report = json.loads(_fetch(base)(chk.report_sha256))
    other = _put(base, {**report, "subject_digest": "c" * 64})
    _, _, found = GC.read_result({"result_bundle": run.bundle_sha256,
                                  "verification_report": other},
                                 _fetch(base))
    assert "the report is not about the cited bundle" in found, found


@pytest.mark.parametrize("key", ["result_bundle", "verification_report"])
def test_a_document_that_does_not_parse_is_a_finding(world, key):
    run, chk = world
    base = _copy(f"parse-{key}")
    ev = {"result_bundle": run.bundle_sha256,
          "verification_report": chk.report_sha256,
          key: _put(base, {"not": "a scientific document"})}
    _, _, found = GC.read_result(ev, _fetch(base))
    assert any(f.startswith(f"{key} does not parse") for f in found), found


def test_a_result_citing_no_bundle_is_a_finding():
    _, _, found = GC.read_result({}, lambda sha: b"")
    assert found == ["cites no result bundle"]


def test_a_proposed_result_without_a_report_is_not_a_finding(world):
    run, _ = world
    bundle, vr, found = GC.read_result({"result_bundle": run.bundle_sha256},
                                       _fetch(ROOT / WS / "genuine"))
    assert found == [] and bundle is not None and vr is None


def test_an_unadmitted_model_or_check_is_a_finding(world):
    from scientific.catalog import check, models
    from scientific.registry import ModelRegistry
    run, chk = world
    bundle, vr, _ = GC.read_result(
        {"result_bundle": run.bundle_sha256,
         "verification_report": chk.report_sha256},
        _fetch(ROOT / WS / "genuine"))
    assert GC.identity_findings(bundle, vr, models(), check) == []
    assert GC.identity_findings(bundle, vr, ModelRegistry(), check) == [
        "thermal.conduction_1d@1.0.0 is not an admitted model"]

    def nothing(check_id):
        raise KeyError(check_id)
    assert GC.identity_findings(bundle, vr, models(), nothing) == [
        f"{CHECK} is not an admitted check"]


def test_it_asks_nothing_of_qta():
    """Directive 15/17: no gate table, no PASS count, no machine FSM, no BOM,
    no mode letters -- in what it reads or what it says."""
    src = (ROOT / "tools/generic_consistency.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]          # past the module docstring
    for token in ("results_gate_table", "gate_table", "PASS_count",
                  "can_PASS_now", "machine_fsm", "BOM", "MODE_",
                  "qta_full_sim", "hardware"):
        assert token not in code, token


def test_the_cli_cannot_ask_without_a_log(tmp_path):
    assert GC.main([str(tmp_path / "none.jsonl"), "--evidence",
                    str(tmp_path)]) == GC.CANNOT_ASK
