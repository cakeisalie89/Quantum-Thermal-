"""Stage-6 validation-roadmap tests. MODEL-ONLY / FORECAST-ONLY.

Planning-infrastructure verification: registries, playbooks, schemas,
coverage, claim boundaries. Fixtures are TEST_FIXTURE_NOT_DATA.
"""
import copy
import json
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import csv                                                       # noqa: E402

from qta_multiphysics.hardware_governance_3d import (            # noqa: E402
    validate_matrix_update_request, load_experiment_registry,
    build_evidence_dossier, AUTOMATIC_GATE_EFFECT)

REG = json.load(open("experiment_registry.json"))
GCOV = json.load(open("experiment_gate_coverage.json"))
MCOV = json.load(open("experiment_matrix_coverage.json"))
CAMP = json.load(open("campaign_registry.json"))
EXPS = REG["experiments"]
IDS = [e["experiment_id"] for e in EXPS]
GATES = list(csv.DictReader(open("results_gate_table.csv")))
G25 = {r["gate_id"] for r in GATES if r["status"] in ("BLOCKED", "UNKNOWN")}
MATRIX_ITEMS = {r["item"] for r in
                csv.DictReader(open("validation_matrix.csv"))}
REQ_VALID = json.load(open("matrix_update_examples/valid_example.json"))

REP_ENUM = {"UNRESOLVED_MISSING_INSTRUMENT_NOISE",
            "UNRESOLVED_MISSING_REPEATABILITY",
            "UNRESOLVED_MISSING_COUNT_RATE",
            "CONDITIONALLY_RESOLVABLE_DURING_RUN"}


def test_registry_ids_unique_and_versioned():
    assert len(IDS) == 11 and len(set(IDS)) == 11
    assert REG["schema_version"] == "1.0.0"
    assert all(e["version"] == "1.0" and e["status"] in
               ("DESIGNED", "PLAYBOOK_READY") for e in EXPS)


def test_exp_af_reconciliation_complete():
    rec = {a["plan_id"]: a for a in REG["exp_af_reconciliation"]}
    assert set(rec) == {"EXP-A", "EXP-B", "EXP-C", "EXP-D", "EXP-E",
                        "EXP-F"}
    for a in rec.values():
        assert a["relationship"] in ("renamed", "merged", "decomposed",
                                     "superseded")
        assert all(m in IDS for m in a["maps_to"])
    assert rec["EXP-D"]["relationship"] == "decomposed"
    assert set(rec["EXP-D"]["maps_to"]) == {"EXP-V1", "EXP-P1"}


def test_eig_preserved_and_separated_from_execution():
    eig = REG["eig_preservation"]
    assert [r["experiment_id"] for r in eig["ranking"]] == \
        ["EXP-A", "EXP-C", "EXP-B", "EXP-F", "EXP-D", "EXP-E"]
    assert "NOT recomputed" in eig["source"]
    orders = sorted(e["execution_order"] for e in EXPS)
    assert orders == list(range(1, 12))       # total order, no ties
    assert "dependency" in eig["distinct_from_execution_order"]


def test_all_25_gates_covered_with_valid_refs():
    got = {g["gate_id"] for g in GCOV["gates"]}
    assert got == G25 and GCOV["n_gates"] == 25
    for g in GCOV["gates"]:
        assert g["classification"] in ("direct", "composite",
                                       "engineering-evidence")
        for e in g["experiment_ids"]:
            assert e in IDS, (g["gate_id"], e)
        assert g["automatic_status_effect"] == "NONE"
        assert g["human_review_required"] is True
        if g["classification"] == "composite":
            assert g["constituents"] and "constituent" in \
                g["evidence_required"]


def test_all_43_matrix_items_dispositioned():
    assert MCOV["n_items"] == 43
    for it in MCOV["items"]:
        assert it["matrix_key"] in MATRIX_ITEMS
        assert it["classification"] in ("direct", "indirect",
                                        "engineering-evidence",
                                        "unresolved")
        for e in it["experiment_ids"]:
            assert e in IDS
        assert it["automatic_matrix_effect"] == "NONE"
    unres = [x["matrix_key"] for x in MCOV["items"]
             if x["classification"] == "unresolved"]
    assert unres == ["nonlinear_threshold"]   # the one honest gap


def test_mode_species_rules_in_registry():
    for e in EXPS:
        blob = json.dumps(e)
        if e["experiment_id"] not in ("EXP-T2",):
            assert "C13_CH4 (Mode-B only)" not in blob or \
                e["experiment_id"] == "EXP-T2"
        if "MODE_D" in e["required_modes"]:
            assert any("C13_CH4" in x for x in e["forbidden_species"]), \
                e["experiment_id"]
    t2 = [e for e in EXPS if e["experiment_id"] == "EXP-T2"][0]
    assert any("He3" in x for x in t2["forbidden_species"])
    n4 = [e for e in EXPS if e["experiment_id"] == "EXP-N4"][0]
    assert any("He3" in s for s in n4["required_species"])
    assert "C13_CH4" in n4["forbidden_species"]


def test_required_fields_present_everywhere():
    may_be_empty = {"dependencies", "gates_served"}
    for e in EXPS:
        for f in ("instruments", "controls", "calibration_requirements",
                  "uncertainty_method", "stop_criteria",
                  "acceptance_criteria", "rejection_criteria",
                  "raw_data_formats", "metadata_requirements",
                  "dependencies", "gates_served", "risk_notes"):
            if f in may_be_empty:
                assert e[f] is not None, (e["experiment_id"], f)
            else:
                assert e[f] not in (None, "", []), (e["experiment_id"], f)


def test_repetition_statuses_and_no_invented_counts():
    for e in EXPS:
        assert e["repetition_resolution_status"] in REP_ENUM, \
            e["experiment_id"]
        assert e["missing_repetition_inputs"].strip()
        # no bare resolved integer count fields exist
        assert "repetition_count" not in e
        der = e["repetition_derivation"]
        assert any(tok in der for tok in ("sigma", "SE", "CRLB",
                                          "vendor", "Welch")), \
            e["experiment_id"]


def test_playbooks_thirty_sections_and_consistency():
    for eid in IDS:
        t = pathlib.Path("EXPERIMENT_PLAYBOOKS", f"{eid}.md").read_text()
        nums = [int(m) for m in re.findall(r"^## (\d+)\.", t, re.M)]
        assert nums == list(range(1, 31)), (eid, len(nums))
        assert "not experimental evidence" in t
        assert "NONE" in t and "PASS remains zero" in t
        e = [x for x in EXPS if x["experiment_id"] == eid][0]
        assert e["repetition_resolution_status"] in t
        assert e["missing_repetition_inputs"] in t


def test_campaign_registry():
    c = CAMP["campaigns"][0]
    assert c["campaign_id"] == "Campaign-1"
    assert c["status"] == "PROPOSED_NOT_PERFORMED"
    assert set(c["experiments"]) == {"EXP-V1", "EXP-S1", "EXP-N0"}
    assert any("noise floors" in r for r in c["rationale"])
    assert "EXP-P1" in CAMP["dependency_note"]
    assert c["automatic_gate_effect"] == "NONE"


def test_update_request_examples():
    ok, why = validate_matrix_update_request(REQ_VALID)
    assert ok, why
    for name, frag in (
            ("invalid_automatic_application", "automatic_application"),
            ("invalid_missing_review", "review_id"),
            ("invalid_missing_evidence", "evidence"),
            ("invalid_unknown_experiment", "unknown experiment_id")):
        d = json.load(open(f"matrix_update_examples/{name}.json"))
        ok2, why2 = validate_matrix_update_request(d)
        assert not ok2 and any(frag in w for w in why2), (name, why2)
    bad = copy.deepcopy(REQ_VALID)
    bad["requester"] = bad["review_ids"][0]
    ok3, why3 = validate_matrix_update_request(bad)
    assert not ok3 and any("requester may not" in w for w in why3)
    bad2 = copy.deepcopy(REQ_VALID)
    bad2["item"] = "no_such_matrix_item"
    ok4, _ = validate_matrix_update_request(bad2)
    assert not ok4


def test_dossier_binding_fields_and_fail_closed():
    d = build_evidence_dossier("B3", [], {}, campaign_id="Campaign-1",
                               matrix_items=["tau_c"],
                               run_ids=["TEST_FIXTURE_NOT_DATA-R1"])
    assert d["campaign_id"] == "Campaign-1"
    assert d["matrix_items"] == ["tau_c"]
    assert d["permitted_claims"] and d["forbidden_claims"]
    assert d["review_readiness"] == "INCOMPLETE"
    assert d["automatic_gate_effect"] == "NONE"
    assert load_experiment_registry()["EXP-V1"]["title"]


def test_readiness_states_and_claims():
    statuses = {e["status"] for e in EXPS}
    assert statuses <= {"DESIGNED", "PLAYBOOK_READY"}
    assert sum(1 for e in EXPS if e["status"] == "PLAYBOOK_READY") <= 1
    for e in EXPS:
        assert e["automatic_gate_effect"] == "NONE"
        assert e["measured_in_this_system"] is False
        assert "not experimental evidence" in e["claim_limitations"]
    assert AUTOMATIC_GATE_EFFECT == "NONE"


def test_zero_pass_scan_all_new_artifacts():
    blob = json.dumps([REG, GCOV, MCOV, CAMP])
    assert '"PASS"' not in blob
    for eid in IDS:
        t = pathlib.Path("EXPERIMENT_PLAYBOOKS", f"{eid}.md").read_text()
        assert "PASS remains zero" in t
    s = open("raw_data_standard.md").read()
    assert "automatic_gate_effect = NONE" in s or \
        "automatic_gate_effect" in s


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
