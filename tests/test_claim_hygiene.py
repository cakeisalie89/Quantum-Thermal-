"""NF-1T closure: the status vocabulary says what was done, and no more.

A. The legacy QTA hardware forecast's ``PASS_count = 0`` is a fact about
   that forecast. It is not the Scientific-AI harness's status: no current
   document states it without saying whose it is, and no current status is
   computed from it.
C. Simulated distributed execution is software-path validation, never
   hardware validation, in every human-readable place it is reported.
D. A stored witness's byte identity is scoped to the witnessed backend;
   decision stability under drift is never written as byte identity or as
   scientific equivalence.

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

import pass_semantics_audit as PA  # noqa: E402

from scientific_ai.neural import claims, status  # noqa: E402

NEURAL = ROOT / "docs" / "neural"


def _read(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _audit_text(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return PA.audit(tmp_path, files=[rel])


# ---- A. the legacy PASS count is not the current status -------------------

def test_the_committed_report_is_current_and_has_no_leak():
    fresh = PA.audit()
    assert fresh == json.loads((ROOT / PA.OUT).read_text(encoding="utf-8"))
    assert fresh["current_ai_semantic_leaks"] == 0
    assert fresh["unclassified"] == 0


@pytest.mark.parametrize("rel", ["scientific_ai/neural/x.py",
                                 "qta_agent/x.py", "SCIENTIFIC_AI_STATUS.md",
                                 "docs/completion_matrix.json",
                                 "conftest.py"])
def test_an_unlabelled_pass_statement_in_current_material_is_a_leak(
        tmp_path, rel):
    rep = _audit_text(tmp_path, rel, "intro\n\nPASS remains 0.\n\nend\n")
    assert rep["current_ai_semantic_leaks"] == 1, rep["findings"]
    assert rep["findings"][0]["leaks"] == [{"line": 3,
                                            "pattern": "pass_remains_zero"}]


@pytest.mark.parametrize("label", [
    "the legacy QTA gate table", "the hardware-era forecast",
    "historical", "the hardware forecast's gates"])
def test_a_statement_that_says_whose_pass_it_is_is_a_guard(tmp_path, label):
    rep = _audit_text(tmp_path, "scientific_ai/neural/x.py",
                      f"{label}: PASS remains 0\n")
    assert rep["current_ai_semantic_leaks"] == 0
    assert rep["findings"][0]["occurrences"] == {
        "LEGACY_COMPATIBILITY_GUARD": {"pass_remains_zero": 1}}


def test_the_label_must_be_near_the_statement(tmp_path):
    text = "legacy\n\n\n\nPASS remains 0\n"
    rep = _audit_text(tmp_path, "qta_agent/x.py", text)
    assert rep["current_ai_semantic_leaks"] == 1


def test_the_no_effect_invariant_must_be_stated_as_none(tmp_path):
    ok = _audit_text(tmp_path, "qta_agent/a.py",
                     "automatic_gate_effect = NONE\n")
    bad = _audit_text(tmp_path, "qta_agent/b.py",
                      "automatic_gate_effect is whatever the run says\n")
    assert ok["current_ai_semantic_leaks"] == 0
    assert bad["current_ai_semantic_leaks"] == 1


@pytest.mark.parametrize("rel, cls", [
    ("qta_multiphysics/gates.py", "LEGACY_QTA_CANONICAL"),
    ("results_gate_table.csv", "LEGACY_QTA_CANONICAL"),
    ("docs/DEFECT_LEDGER.md", "DOCUMENTATION_HISTORY"),
    ("tests/test_stage6_roadmap.py", "LEGACY_COMPATIBILITY_GUARD"),
    ("tools/mutations/x.json", "TOOLING_REFERENCE")])
def test_the_legacy_record_keeps_its_historical_zero(tmp_path, rel, cls):
    rep = _audit_text(tmp_path, rel, "PASS remains 0\n")
    assert rep["current_ai_semantic_leaks"] == 0
    assert list(rep["findings"][0]["occurrences"]) == [cls]


def test_a_file_no_rule_covers_is_unclassified(tmp_path):
    rep = _audit_text(tmp_path, "newdir/notes.yaml", "PASS remains 0\n")
    assert rep["unclassified"] == 1


def test_words_that_only_contain_pass_are_not_matched(tmp_path):
    rep = _audit_text(tmp_path, "scientific_ai/neural/x.py",
                      "5190 passed; bypass count 0; passes = 0\n")
    assert rep["findings"] == []


def test_the_check_fails_on_a_leak_or_an_unclassified_file(monkeypatch):
    clean = PA.audit()
    for change in ({"current_ai_semantic_leaks": 1}, {"unclassified": 1}):
        monkeypatch.setattr(PA, "audit", lambda c=change: dict(clean, **c))
        assert PA.main([]) == 1
    monkeypatch.setattr(PA, "audit", lambda: clean)
    assert PA.main([]) == 0


def test_a_file_is_audited_before_it_is_committed(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.md").write_text("x\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.md"],
                   check=True)
    (tmp_path / "new.md").write_text("PASS remains 0\n")
    (tmp_path / ".gitignore").write_text("ignored.md\n")
    (tmp_path / "ignored.md").write_text("x\n")
    files = PA.repository_files(tmp_path)
    assert {"tracked.md", "new.md"} <= set(files)
    assert "ignored.md" not in files


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
        witness_profile=_read("docs/byte_reproduction_profile.json"),
        equivalence="NOT_ESTABLISHED",
        pass_audit={"current_ai_semantic_leaks": 0, "unclassified": 0},
        mode_audit={"active_neural_semantic_leaks": 0, "unclassified": 0},
        legacy_gate_statuses={"CONDITIONAL": 47, "BLOCKED": 23,
                              "DERIVED_CHECK": 11, "UNKNOWN": 2},
        sources={})
    base.update(change)
    return base


def test_no_current_section_of_the_status_reads_the_legacy_gate_table():
    """Change the legacy gate table as far as it can go: every gate PASS.
    Only the legacy section may move."""
    a = status.build(**_status_inputs())
    b = status.build(**_status_inputs(legacy_gate_statuses={"PASS": 83}))
    legacy = "legacy_qta_hardware_forecast"
    assert a[legacy]["PASS_count"] == 0 and b[legacy]["PASS_count"] == 83
    assert {k: v for k, v in a.items() if k != legacy} == \
        {k: v for k, v in b.items() if k != legacy}


def test_the_legacy_section_is_labelled_and_is_the_only_pass_count():
    st = _read("docs/neural/current_status.json")
    lg = st["legacy_qta_hardware_forecast"]
    assert lg["classification"] == status.LEGACY_LABEL == "LEGACY_QTA_ONLY"
    flat = json.dumps({k: v for k, v in st.items()
                       if k != "legacy_qta_hardware_forecast"})
    assert "PASS_count" not in flat and "results_gate_table" not in flat \
        .replace('"path": "results_gate_table.csv"', "")


def test_the_learned_model_claims_do_not_read_the_legacy_gate_table():
    evidence = [_read("docs/neural/distributed_readiness.json"),
                _read("docs/neural/flagship_model_manifest.json")]
    subject = evidence[1]["configuration_digest"]
    gate_doc = {"schema": "qta-gate-table", "PASS_count": 83,
                "gates": [{"status": "PASS"}] * 83}
    assert claims.evaluate(subject, evidence) == \
        claims.evaluate(subject, evidence + [gate_doc])


def test_the_rendered_status_states_the_gate_pass_only_as_legacy():
    """Stricter than the audit's two-line window: on the same line. (The
    scale ladder's meta-validation PASS is another word, and the gate
    vocabulary's patterns do not match it.)"""
    md = (ROOT / "SCIENTIFIC_AI_STATUS.md").read_text(encoding="utf-8")
    lines = md.splitlines()
    gate = [(n, name) for n, name in PA.occurrences(md)
            if name.startswith("pass_") or name == "zero_pass"]
    assert gate, "the legacy section no longer states the legacy count"
    for n, _ in gate:
        assert re.search(r"(?i)legacy", lines[n - 1]), lines[n - 1]


def test_the_completion_matrix_says_whose_pass_count_it_is():
    blurb = _read("docs/completion_matrix.json")["does_not_mean"]
    assert "legacy QTA" in blurb and "not a measure" in blurb


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


# ---- D. witness byte identity is not generic-host identity -----------------

def _current_texts():
    plan = (ROOT / "ARCHITECTURE_CONVERGENCE_PLAN.md").read_text(
        encoding="utf-8")
    ledger = (ROOT / "docs" / "DEFECT_LEDGER.md").read_text(encoding="utf-8")
    return {
        "SCIENTIFIC_AI_STATUS.md": (ROOT / "SCIENTIFIC_AI_STATUS.md")
        .read_text(encoding="utf-8"),
        "NEURAL_SUBSTRATE.md": (ROOT / "NEURAL_SUBSTRATE.md").read_text(
            encoding="utf-8"),
        "plan, sections 17 on": plan[plan.index("\n## 17."):],
        "ledger, D-2026-103 on": ledger[ledger.index("\n## D-2026-103"):],
    }


def test_every_full_corpus_byte_identity_names_the_witnessed_backend():
    rx = re.compile(r"\b88\s*(?:/|of)\s*88\b")
    for name, text in _current_texts().items():
        for line in text.splitlines():
            if rx.search(line) and re.search(r"(?i)byte[- ]identical",
                                             line):
                assert re.search(r"(?i)witness(?:ed backend| profile)|"
                                 r"reference backend|stored witness",
                                 line), (name, line)


def test_decision_stability_is_never_written_as_identity_or_equivalence():
    for name, text in _current_texts().items():
        for line in text.splitlines():
            if "DECISION_STABLE_WITH_NUMERIC_DRIFT" in line:
                assert "SCIENTIFICALLY_EQUIVALENT" not in line, (name, line)
                assert not re.search(r"REPRODUCTION_STATUS=BYTE_IDENTICAL",
                                     line), (name, line)


def test_the_status_keeps_the_three_reproduction_facts_apart():
    rp = _read("docs/neural/current_status.json")["reproduction"]
    assert rp["scientific_equivalence"] == "NOT_ESTABLISHED"
    assert all("not a hosted runner" in w["context"]
               for w in rp["stored_witness"])
    assert "DIFFERENT_RESOLVED_BACKEND" in rp["generic_hosted_runner"]
    assert "not byte identity" in rp["generic_hosted_runner"]


# ---- the status builder derives, it does not assert ------------------------

def test_the_status_derives_plan_only_axes_from_what_ran():
    st = status.build(**_status_inputs())
    assert st["distributed"]["plan_only"] == ["tensor", "pipeline"]
    rep = copy.deepcopy(_read("docs/neural/distributed_readiness.json"))
    rep["checks"].append({"check": "tensor_parallel_equivalence",
                          "passed": True})
    st = status.build(**_status_inputs(distributed=rep))
    assert st["distributed"]["plan_only"] == ["pipeline"]


def test_the_status_reports_the_equivalence_it_is_given_and_scopes_witnesses():
    st = status.build(**_status_inputs(equivalence="NOT_ESTABLISHED"))
    rp = st["reproduction"]
    assert rp["scientific_equivalence"] == "NOT_ESTABLISHED"
    assert [w["byte_identical"] for w in rp["stored_witness"]] == [88]
    assert all("not a hosted runner" in w["context"]
               for w in rp["stored_witness"])
    assert "not byte identity" in rp["generic_hosted_runner"]


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
