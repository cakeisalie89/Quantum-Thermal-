"""Stage-7 tests: Pydantic boundary models (positive/negative), Stage-6
protection assertions, and bounded deterministic Hypothesis properties.

MODEL-ONLY / FORECAST-ONLY. Software-verification results only; the
scientific gate PASS count remains zero and is asserted so below.
"""
import csv
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest                                                    # noqa: E402
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402
from pydantic import ValidationError                             # noqa: E402

from qta_multiphysics.stage7_boundary_models import (            # noqa: E402
    ModeId, SpeciesAssignment, SimultaneityGuard, GateStatus,
    ExperimentStatus, NonNegativeQuantity, BoundedScalar, UnknownOrValue,
    MatrixUpdateRequestModel, PathConfig, OutputMetadata,
    ManifestVerificationResult, MODES, GATE_STATUSES,
    EXPERIMENT_STATUSES)

DET = settings(max_examples=25, deadline=None, derandomize=True,
               suppress_health_check=[HealthCheck.too_slow])

REQ = json.load(open("matrix_update_examples/valid_example.json"))
REG = json.load(open("experiment_registry.json"))
GATE_ROWS = list(csv.DictReader(open("results_gate_table.csv")))


# ---------------------- Pydantic positive/negative ----------------------

def test_mode_and_species_models():
    assert ModeId(mode="MODE_B").mode == "MODE_B"
    with pytest.raises(ValidationError):
        ModeId(mode="MODE_X")
    SpeciesAssignment(species="C13_CH4", mode="MODE_B", active=True)
    SpeciesAssignment(species="He3", mode="MODE_B", active=False)  # inactive ok
    with pytest.raises(ValidationError):
        SpeciesAssignment(species="C13_CH4", mode="MODE_D", active=True)
    with pytest.raises(ValidationError):
        SpeciesAssignment(species="He4", mode="MODE_B", active=True)
    SimultaneityGuard(mode_b_processing_active=True,
                      mode_d_sensing_active=False)
    with pytest.raises(ValidationError):
        SimultaneityGuard(mode_b_processing_active=True,
                          mode_d_sensing_active=True)


def test_status_models_fail_closed():
    GateStatus(gate_id="B3", status="UNKNOWN")
    with pytest.raises(ValidationError):
        GateStatus(gate_id="NO_SUCH_GATE", status="BLOCKED")
    ExperimentStatus(experiment_id="EXP-V1", status="PLAYBOOK_READY")
    with pytest.raises(ValidationError):
        ExperimentStatus(experiment_id="EXP-ZZ", status="DESIGNED")
    with pytest.raises(ValidationError):
        ExperimentStatus(experiment_id="EXP-V1", status="PERFORMED")


def test_scalar_models():
    NonNegativeQuantity(name="pressure", value=1e-12, unit="Pa")
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValidationError):
            NonNegativeQuantity(name="p", value=bad, unit="Pa")
    BoundedScalar(name="T", value=0.01, unit="K", lower=0.0, upper=400.0)
    with pytest.raises(ValidationError):
        BoundedScalar(name="T", value=500.0, unit="K", lower=0.0,
                      upper=400.0)
    UnknownOrValue(status="UNKNOWN", value=None)
    with pytest.raises(ValidationError):
        UnknownOrValue(status="UNKNOWN", value=0.0)   # never coerced
    with pytest.raises(ValidationError):
        UnknownOrValue(status="KNOWN", value=None)


def test_request_model_and_governance_agree():
    m = MatrixUpdateRequestModel(**REQ)
    ok, why = m.validated_by_governance()
    assert ok, why
    with pytest.raises(ValidationError):
        MatrixUpdateRequestModel(**{**REQ, "automatic_application": True})
    with pytest.raises(ValidationError):
        MatrixUpdateRequestModel(**{**REQ,
                                    "requester": REQ["review_ids"][0]})
    with pytest.raises(ValidationError):
        MatrixUpdateRequestModel(**{**REQ,
                                    "experiment_ids": ["EXP-NOPE"]})


def test_misc_boundary_models():
    with pytest.raises(ValidationError):
        PathConfig(source_root=".", workspace=".")
    OutputMetadata(filename="x.csv", sha256="a" * 64, byte_size=1)
    with pytest.raises(ValidationError):
        OutputMetadata(filename="x.csv", sha256="zz", byte_size=1)
    ManifestVerificationResult(entries=305, mismatches=0,
                               detached_hash_ok=True)
    with pytest.raises(ValidationError):
        ManifestVerificationResult(entries=305, mismatches=1,
                                   detached_hash_ok=True)


# ------------------------ Stage-6 protection core ------------------------

def test_stage6_state_protected():
    assert sum(1 for r in GATE_ROWS if r["status"] == "PASS") == 0
    ids = [e["experiment_id"] for e in REG["experiments"]]
    assert len(ids) == 11
    st6 = {e["experiment_id"]: e["status"] for e in REG["experiments"]}
    assert st6["EXP-V1"] == "PLAYBOOK_READY"
    assert all(v == "DESIGNED" for k, v in st6.items() if k != "EXP-V1")
    camp = json.load(open("campaign_registry.json"))
    assert camp["campaigns"][0]["status"] == "PROPOSED_NOT_PERFORMED"
    mc = json.load(open("experiment_matrix_coverage.json"))
    unres = [x["matrix_key"] for x in mc["items"]
             if x["classification"] == "unresolved"]
    assert unres == ["nonlinear_threshold"]
    rec = {a["plan_id"]: a["maps_to"]
           for a in REG["exp_af_reconciliation"]}
    assert set(rec["EXP-D"]) == {"EXP-V1", "EXP-P1"}


# --------------------- bounded Hypothesis properties ---------------------

@DET
@given(species=st.sampled_from(["C13_CH4", "He3", "He4"]),
       mode=st.sampled_from(list(MODES)))
def test_prop_species_separation(species, mode):
    ok_mode = {"C13_CH4": "MODE_B", "He3": "MODE_D",
               "He4": "MODE_D"}[species]
    if mode == ok_mode:
        SpeciesAssignment(species=species, mode=mode, active=True)
    else:
        with pytest.raises(ValidationError):
            SpeciesAssignment(species=species, mode=mode, active=True)


@DET
@given(b=st.booleans(), d=st.booleans())
def test_prop_no_simultaneous_processing_sensing(b, d):
    if b and d:
        with pytest.raises(ValidationError):
            SimultaneityGuard(mode_b_processing_active=b,
                              mode_d_sensing_active=d)
    else:
        SimultaneityGuard(mode_b_processing_active=b,
                          mode_d_sensing_active=d)


@DET
@given(v=st.one_of(st.just(float("nan")), st.just(float("inf")),
                   st.just(float("-inf")),
                   st.floats(max_value=-1e-30, allow_nan=False,
                             allow_infinity=False)))
def test_prop_invalid_scalars_rejected(v):
    with pytest.raises(ValidationError):
        NonNegativeQuantity(name="q", value=v, unit="u")


@DET
@given(s=st.text(min_size=1, max_size=12))
def test_prop_unknown_ids_fail_closed(s):
    if s not in {r["gate_id"] for r in GATE_ROWS}:
        with pytest.raises(ValidationError):
            GateStatus(gate_id=s, status="BLOCKED")
    ids = {e["experiment_id"] for e in REG["experiments"]}
    if s not in ids:
        with pytest.raises(ValidationError):
            ExperimentStatus(experiment_id=s, status="DESIGNED")


@DET
@given(status=st.text(min_size=1, max_size=16))
def test_prop_status_vocabularies_closed(status):
    if status not in GATE_STATUSES:
        with pytest.raises(ValidationError):
            GateStatus(gate_id="B3", status=status)
    if status not in EXPERIMENT_STATUSES:
        with pytest.raises(ValidationError):
            ExperimentStatus(experiment_id="EXP-V1", status=status)


@DET
@given(reviewer=st.sampled_from(["REV-1", "REV-2", "SELF"]))
def test_prop_requester_never_reviewer(reviewer):
    doc = {**REQ, "requester": "SELF", "review_ids": [reviewer]}
    if reviewer == "SELF":
        with pytest.raises(ValidationError):
            MatrixUpdateRequestModel(**doc)
    else:
        MatrixUpdateRequestModel(**doc)


@DET
@given(auto=st.sampled_from([True, "true", 1, None]))
def test_prop_automatic_application_impossible(auto):
    with pytest.raises(ValidationError):
        MatrixUpdateRequestModel(**{**REQ, "automatic_application": auto})


@DET
@given(payload=st.dictionaries(
    st.sampled_from(["a", "b", "c", "z"]),
    st.integers(min_value=0, max_value=9), max_size=4))
def test_prop_serialization_stable_ordering(payload):
    a = json.dumps(payload, sort_keys=True)
    b = json.dumps(dict(reversed(list(payload.items()))), sort_keys=True)
    assert a == b


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
