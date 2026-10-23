"""NF-1T claim hygiene: the status vocabulary says what was done, and no
more.

A. The learned-model status is computed from the learned-model evidence and
   nothing else: no section reads another programme's results, and none of
   the retired programme's vocabulary appears in it.
C. Simulated distributed execution is software-path validation, never
   hardware validation, in every human-readable place it is reported.
D. Decision stability under numeric drift is never written as byte identity
   or as scientific equivalence, and the status claims no stored witness.

(B, E and F -- plain language, the learned-record refusal and the locked
architecture counts -- live with the suites they belong to.)
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import repo_content_audit as RCA  # noqa: E402

from scientific_ai.neural import claims, status  # noqa: E402

NEURAL = ROOT / "docs" / "neural"


def _read(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _status_inputs(**change):
    base = dict(
        flagship_manifest=_read("docs/neural/flagship_model_manifest.json"),
        flagship_meta=_read("docs/neural/flagship_meta_validation.json"),
        dev_manifest=_read("docs/neural/development_model_manifest.json"),
        claims_doc=_read("docs/neural/claims.json"),
        training=_read("docs/neural/dev/training_manifest.json"),
        dataset=_read("docs/neural/dev/dataset_manifest.json"),
        evaluation=_read("docs/neural/dev/evaluation_report.json"),
        checkpoint=_read("docs/neural/dev/checkpoint_manifest.json"),
        distributed=_read("docs/neural/distributed_readiness.json"),
        family=_read("docs/neural/architecture_family.json"),
        equivalence="NOT_ESTABLISHED",
        sources={})
    base.update(change)
    return base


# ---- A. the status is the learned-model evidence's, and only its ----------

def test_the_status_derives_from_the_learned_model_evidence_only():
    """Every source the committed status names is a learned-model evidence
    document, by digest."""
    st = _read("docs/neural/current_status.json")
    assert set(st["derived_from"]) == {"neural_evidence"}
    for path in st["derived_from"]["neural_evidence"]:
        assert path.startswith("docs/neural/"), path


def test_the_status_carries_none_of_the_retired_vocabulary():
    """Read with the repository content audit's own patterns: the status
    and its rendering carry none of the retired programme's terms."""
    st = _read("docs/neural/current_status.json")
    md = (ROOT / "SCIENTIFIC_AI_STATUS.md").read_text(encoding="utf-8")
    for text in (json.dumps(st), md):
        hits = [m.group(0) for c, rx in RCA._TERMS for m in rx.finditer(text)]
        assert hits == [], hits


def test_the_learned_model_claims_read_no_foreign_document():
    """A document that is not learned-model evidence changes no claim."""
    evidence = [_read("docs/neural/distributed_readiness.json"),
                _read("docs/neural/flagship_model_manifest.json")]
    subject = evidence[1]["configuration_digest"]
    foreign = {"schema": "some-other-programme/1", "status": "PASS",
               "rows": [{"status": "PASS"}] * 83}
    assert claims.evaluate(subject, evidence) == \
        claims.evaluate(subject, evidence + [foreign])


def test_the_completion_matrix_disclaims_scientific_validity():
    doc = _read("docs/completion_matrix.json")
    assert "says nothing about scientific validity" in doc["does_not_mean"]


# ---- C. simulated is not hardware ------------------------------------------

def test_a_simulated_run_reads_as_software_path_validation_only():
    rep = _read("docs/neural/distributed_readiness.json")
    t = claims.evaluate("0" * 64, [rep])
    reason = t["DISTRIBUTED_SOFTWARE_READY"]["reason"]
    assert t["DISTRIBUTED_SOFTWARE_READY"]["holds"]
    assert "SOFTWARE-PATH VALIDATION ONLY" in reason
    assert "not distributed hardware validation" in reason
    assert "tensor and pipeline parallelism are PLAN_ONLY" in reason
    assert not t["DISTRIBUTED_HARDWARE_VALIDATED"]["holds"]


@pytest.mark.parametrize("kind, executed", [
    ("SIMULATED_MULTI_DEVICE", True), ("CPU_DEVELOPMENT", True),
    ("MULTI_GPU_NODE", False)])
def test_a_report_that_did_not_run_on_hardware_never_validates_hardware(
        kind, executed):
    rep = copy.deepcopy(_read("docs/neural/distributed_readiness.json"))
    rep["execution_profile"] = dict(rep["execution_profile"], kind=kind,
                                    hardware_executed=executed)
    t = claims.evaluate("0" * 64, [rep])
    assert not t["DISTRIBUTED_HARDWARE_VALIDATED"]["holds"]
    assert "SOFTWARE-PATH VALIDATION ONLY" in \
        t["DISTRIBUTED_SOFTWARE_READY"]["reason"]
    st = status.build(**_status_inputs(distributed=rep))
    assert st["distributed"]["hardware"] == "NOT_VALIDATED"
    # the rendered device description comes from the profile, never assumed
    row = status.render(st).split("| distributed software |")[1]
    assert ("simulated" in row.splitlines()[0]) == (
        kind in claims.SIMULATED_PROFILES)


def test_an_executed_tensor_check_is_no_longer_called_plan_only():
    rep = copy.deepcopy(_read("docs/neural/distributed_readiness.json"))
    rep["checks"].append({"check": "tensor_parallel_equivalence",
                          "passed": True})
    reason = claims.evaluate("0" * 64, [rep])[
        "DISTRIBUTED_SOFTWARE_READY"]["reason"]
    assert "pipeline parallelism is PLAN_ONLY" in reason
    assert "tensor" not in reason.split(";")[-1]


def test_the_status_never_reports_hardware_validation():
    st = _read("docs/neural/current_status.json")
    assert st["distributed"]["hardware_executed"] is False
    assert st["distributed"]["hardware"] == "NOT_VALIDATED"
    assert set(st["distributed"]["plan_only"]) == {"tensor", "pipeline"}
    md = (ROOT / "SCIENTIFIC_AI_STATUS.md").read_text(encoding="utf-8")
    assert "Distributed hardware: NOT_VALIDATED" in md
    assert not re.search(r"(?i)hardware[ -]validated(?! *=)", md
                         .replace("NOT_VALIDATED", ""))


# ---- D. decision stability is not byte identity ---------------------------

def _current_texts():
    ledger = (ROOT / "docs" / "DEFECT_LEDGER.md").read_text(encoding="utf-8")
    return {
        "SCIENTIFIC_AI_STATUS.md": (ROOT / "SCIENTIFIC_AI_STATUS.md")
        .read_text(encoding="utf-8"),
        "NEURAL_SUBSTRATE.md": (ROOT / "NEURAL_SUBSTRATE.md").read_text(
            encoding="utf-8"),
        "ledger, D-2026-103 on": ledger[ledger.index("\n## D-2026-103"):],
    }


def test_decision_stability_is_never_written_as_identity_or_equivalence():
    for name, text in _current_texts().items():
        for line in text.splitlines():
            if "DECISION_STABLE_WITH_NUMERIC_DRIFT" in line:
                assert "SCIENTIFICALLY_EQUIVALENT" not in line, (name, line)
                assert not re.search(r"REPRODUCTION_STATUS=BYTE_IDENTICAL",
                                     line), (name, line)


def test_the_status_claims_no_stored_witness_and_no_equivalence():
    rp = _read("docs/neural/current_status.json")["reproduction"]
    assert rp["scientific_equivalence"] == "NOT_ESTABLISHED"
    assert "stored_witness" not in rp
    assert "per run" in rp["byte_reproduction"]


# ---- the status builder derives, it does not assert ------------------------

def test_the_status_derives_plan_only_axes_from_what_ran():
    st = status.build(**_status_inputs())
    assert st["distributed"]["plan_only"] == ["tensor", "pipeline"]
    rep = copy.deepcopy(_read("docs/neural/distributed_readiness.json"))
    rep["checks"].append({"check": "tensor_parallel_equivalence",
                          "passed": True})
    st = status.build(**_status_inputs(distributed=rep))
    assert st["distributed"]["plan_only"] == ["pipeline"]


def test_the_status_reports_the_equivalence_it_is_given():
    st = status.build(**_status_inputs(equivalence="NOT_ESTABLISHED"))
    assert st["reproduction"]["scientific_equivalence"] == "NOT_ESTABLISHED"
    st = status.build(**_status_inputs(equivalence="ESTABLISHED_ELSEWHERE"))
    assert st["reproduction"]["scientific_equivalence"] == \
        "ESTABLISHED_ELSEWHERE"


def test_unallocated_weights_are_asserted_only_on_a_refused_allocation():
    st = status.build(**_status_inputs())
    assert st["flagship"]["real_weights_allocated"] is False
    meta = dict(_read("docs/neural/flagship_meta_validation.json"),
                real_allocation_refused=False)
    st = status.build(**_status_inputs(flagship_meta=meta))
    assert st["flagship"]["real_weights_allocated"] is None
    assert "NOT EXECUTED" not in status.render(st).split(
        "flagship real-weight allocation")[1].splitlines()[0]


def test_the_committed_status_is_what_the_evidence_gives():
    """``SCIENTIFIC_AI_STATUS.md`` and its JSON are recomputed from the
    committed documents -- the same check ``tools/neural.py verify`` makes,
    held here so the status builder cannot drift from what is committed."""
    import neural
    st = neural.build_status()
    assert st == _read("docs/neural/current_status.json")
    assert status.render(st) == (ROOT / "SCIENTIFIC_AI_STATUS.md").read_text(
        encoding="utf-8")
