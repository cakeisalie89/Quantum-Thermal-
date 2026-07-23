"""Hardware-governance tests (Stage 5). MODEL-ONLY / FORECAST-ONLY.

All fixtures are labeled TEST_FIXTURE_NOT_DATA and are not measurements.
Required coverage: class separation, malformed records, raw-hash mismatch,
expired/missing calibration, custody ordering, missing controls,
unresolved repetition requirements, uncertainty requirements, review
completeness, audit-chain tampering, synthetic-path regression, gate-table
immutability, deterministic empty execution, zero-PASS enforcement.
"""
import copy
import hashlib
import json
import os
import sys
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qta_multiphysics.hardware_governance_3d import (       # noqa: E402
    schema_validate_hardware, full_deficiencies, validate_review_record,
    build_quarantine_report, build_evidence_dossier, append_audit,
    verify_audit_chain, governance_summary, plan_registry,
    AUTOMATIC_GATE_EFFECT, REPS_UNKNOWN, CUSTODY_CAVEAT)
from qta_multiphysics.measurement_ingest_3d import ingest_and_compare  # noqa: E402

ITEM = sorted(plan_registry())[0]

FIX = {"measurement_id": "TEST_FIXTURE_NOT_DATA-HW-1",
       "quantity": "P_H2_Pa", "value": 1.1e-12, "units": "Pa",
       "timestamp": "2026-07-18T01:00:00Z",
       "alignment": {"mode": "MODE_A"},
       "data_class": "HARDWARE_UNVERIFIED",
       "experiment_id": ITEM, "sample_id": "TEST_FIXTURE_NOT_DATA-S1",
       "operator_id": "TEST_FIXTURE_NOT_DATA-O1",
       "instrument_id": "I1", "calibration_id": "C1", "run_id": "R1",
       "raw_data": {"filename": "fixture.dat", "byte_size": 11,
                     "sha256": "a" * 64, "format": "dat",
                     "acquisition_timestamp_utc": "2026-07-18T00:59:00Z"},
       "chain_of_custody": [
           {"actor": "O1", "action": "acquisition",
            "timestamp": "2026-07-18T00:59:00Z"},
           {"actor": "O1", "action": "archive",
            "timestamp": "2026-07-18T00:59:30Z"},
           {"actor": "sys", "action": "ingestion",
            "timestamp": "2026-07-18T01:00:00Z"}],
       "calibration": {"status": "CALIBRATED", "reference": "ref-0",
                        "date": "2026-07-01T00:00:00Z",
                        "valid_until": "2026-12-31T00:00:00Z"},
       "uncertainty": {"type": "stddev", "value": 1e-13,
                        "method": "statistical"},
       "control_refs": ["TEST_FIXTURE_NOT_DATA-CTL-1"],
       "instrument": {"id": "I1", "type": "RGA (fixture)",
                       "calibration": {"status": "CALIBRATED",
                                        "reference": "ref-0",
                                        "date": "2026-07-01"}},
       "provenance": {"origin": "TEST_FIXTURE_NOT_DATA",
                       "generator": "test", "note": "fixture"}}

REVIEW = {"reviewer_id": "TEST_FIXTURE_NOT_DATA-REV-1",
          "review_date": "2026-07-18T02:00:00Z",
          "checklist_version": "1.0",
          "decision": "ACCEPT_AS_EVIDENCE",
          "record_sha256": "b" * 64}


def _m(**kw):
    r = copy.deepcopy(FIX)
    r.update(kw)
    return r


def test_class_separation():
    ok, _ = schema_validate_hardware(_m(data_class="SYNTHETIC"))
    assert not ok                                   # not a hardware class
    q = build_quarantine_report([FIX])
    assert q["n_quarantined"] == 1
    d = build_evidence_dossier("B3", [FIX], {FIX["measurement_id"]: REVIEW})
    assert d["n_entries"] == 0                      # UNVERIFIED never enters
    rev = _m(data_class="HARDWARE_REVIEWED")
    d2 = build_evidence_dossier("B3", [rev],
                                {rev["measurement_id"]: REVIEW})
    # raw file inaccessible -> hard deficiency -> excluded
    assert d2["n_entries"] == 0 and d2["n_excluded"] == 1


def test_malformed_records_fail_closed():
    ok, why = schema_validate_hardware({"data_class": "HARDWARE_UNVERIFIED"})
    assert not ok and any("missing identifier" in w for w in why)
    ok, why = schema_validate_hardware(_m(experiment_id="NOT-IN-PLAN"))
    assert not ok and any("plan registry" in w for w in why)
    ok, why = schema_validate_hardware("string")
    assert not ok
    q = build_quarantine_report([{"data_class": "HARDWARE_UNVERIFIED"}])
    assert q["n_rejected"] == 1 and q["n_quarantined"] == 0


def test_raw_hash_mismatch_detected():
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "fixture.dat"
        p.write_bytes(b"fixture!!!!")
        good = hashlib.sha256(p.read_bytes()).hexdigest()
        defs = full_deficiencies(_m(raw_data={**FIX["raw_data"],
                                              "sha256": good}), td)
        assert not any("MISMATCH" in x for x in defs)
        defs2 = full_deficiencies(FIX, td)          # sha 'aaaa...' wrong
        assert any("raw-hash MISMATCH" in x for x in defs2)
    defs3 = full_deficiencies(FIX, None)
    assert any("not accessible for re-hash" in x for x in defs3)


def test_calibration_expired_or_missing():
    defs = full_deficiencies(_m(calibration={"status": "UNKNOWN"}))
    assert any("CALIBRATED required" in x for x in defs)
    late = _m(timestamp="2027-02-01T00:00:00Z",
              alignment={"mode": "MODE_A"})
    defs2 = full_deficiencies(late)
    assert any("expired at acquisition" in x for x in defs2)
    defs3 = full_deficiencies(_m(calibration={"status": "CALIBRATED"}))
    assert any("missing 'reference'" in x for x in defs3)


def test_custody_ordering_and_stages():
    bad = _m(chain_of_custody=list(reversed(FIX["chain_of_custody"])))
    defs = full_deficiencies(bad)
    assert any("non-decreasing time order" in x for x in defs)
    short = _m(chain_of_custody=FIX["chain_of_custody"][:1])
    defs2 = full_deficiencies(short)
    assert any("missing required stage 'archive'" in x for x in defs2)
    assert any("missing required stage 'ingestion'" in x for x in defs2)
    q = build_quarantine_report([FIX])
    assert q["quarantined"][0]["custody_caveat"] == CUSTODY_CAVEAT
    assert "does not prove" in CUSTODY_CAVEAT


def test_missing_controls_recorded():
    defs = full_deficiencies(_m(control_refs=[]))
    assert any("no control/background" in x for x in defs)


def test_unresolved_repetition_requirement():
    defs = full_deficiencies(FIX)
    assert any(REPS_UNKNOWN in x and "UNRESOLVED_REQUIREMENT" in x
               for x in defs)
    rev = _m(data_class="HARDWARE_REVIEWED")
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "fixture.dat"
        p.write_bytes(b"fixture!!!!")
        rev["raw_data"] = {**FIX["raw_data"],
                           "sha256":
                           hashlib.sha256(p.read_bytes()).hexdigest()}
        d = build_evidence_dossier("B3", [rev],
                                   {rev["measurement_id"]: REVIEW}, td)
        assert d["n_entries"] == 1
        assert d["review_readiness"] == "INCOMPLETE"
        assert any(REPS_UNKNOWN in r for r in d["readiness_reasons"])
        assert d["entries"][0]["unresolved_requirements"]


def test_uncertainty_requirements():
    defs = full_deficiencies(_m(uncertainty={"type": "none"}))
    assert any("explicit uncertainty" in x for x in defs)
    defs2 = full_deficiencies(_m(uncertainty={"type": "stddev",
                                              "value": 1e-13}))
    assert any("missing 'method'" in x for x in defs2)


def test_review_completeness_and_human_only():
    ok, why = validate_review_record({})
    assert not ok
    ok, why = validate_review_record({**REVIEW, "decision": "MAYBE"})
    assert not ok
    ok, why = validate_review_record({**REVIEW, "authored_by_tool": True})
    assert not ok and any("human" in w or "tools" in w for w in why)
    ok, _ = validate_review_record(REVIEW)
    assert ok
    d = build_evidence_dossier(
        "B3", [_m(data_class="HARDWARE_REVIEWED")],
        {FIX["measurement_id"]: {**REVIEW, "decision": "REJECT"}})
    assert d["n_entries"] == 0


def test_audit_chain_tamper_detection():
    with tempfile.TemporaryDirectory() as td:
        log = os.path.join(td, "audit.jsonl")
        append_audit(log, {"e": 1})
        append_audit(log, {"e": 2})
        ok, msg = verify_audit_chain(log)
        assert ok and "2 entries" in msg
        with open(log, "a") as f:
            f.write(json.dumps({"prev_sha256": "f" * 64,
                                "event": {"e": 3}}) + "\n")
        ok2, msg2 = verify_audit_chain(log)
        assert not ok2 and "chain break" in msg2
        lines = open(log).read().splitlines()
        lines[0] = lines[0].replace('"e": 1', '"e": 9')
        with open(log, "w") as f:
            f.write("\n".join(lines) + "\n")
        ok3, _ = verify_audit_chain(log)
        assert not ok3


def test_synthetic_path_regression():
    rep = ingest_and_compare()
    assert rep["ingestion_status"] == "OK"
    assert rep["n_accepted"] == 8 and rep["n_rejected"] == 2


def test_gate_table_immutability():
    before = hashlib.sha256(open("results_gate_table.csv", "rb")
                            .read()).digest()
    build_quarantine_report([FIX])
    build_evidence_dossier("B3", [FIX], {})
    governance_summary()
    after = hashlib.sha256(open("results_gate_table.csv", "rb")
                           .read()).digest()
    assert before == after


def test_deterministic_empty_execution():
    a = json.dumps(governance_summary(), sort_keys=True)
    b = json.dumps(governance_summary(), sort_keys=True)
    assert a == b
    assert "NO_HARDWARE_DATA" in governance_summary()["default_execution"]
    q = build_quarantine_report([])
    assert q["n_quarantined"] == 0 and q["n_rejected"] == 0


def test_zero_pass_enforcement_scoped():
    ok, why = schema_validate_hardware(_m(status="PASS"))
    assert not ok and any("status" in w for w in why)
    ok2, why2 = schema_validate_hardware(
        _m(provenance={"origin": "TEST_FIXTURE_NOT_DATA",
                       "generator": "t",
                       "note": "the word PASS in free text is fine"}))
    assert ok2, why2                        # free text not policed
    blob = json.dumps([build_quarantine_report([FIX]),
                       build_evidence_dossier("B3", [], {}),
                       governance_summary()])
    assert '"PASS"' not in blob
    assert AUTOMATIC_GATE_EFFECT == "NONE"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:            # noqa: BLE001
            print(f"  FAIL  {t.__name__}: {e!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)
