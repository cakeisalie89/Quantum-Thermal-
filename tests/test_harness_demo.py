"""The generic end-to-end demonstration: run for real, and its judge held to
what a demonstration must show -- above all that the negative twin is
REJECTED.

Without the FEniCSx and FMI runtimes the demonstration runs its generic
leg only: the slab result is checked against the series solution, which the
admission policy does not admit on (an ANALYTIC_REFERENCE), so the honest
outcome there is REJECTED. With both runtimes (QTA_FENICSX_PYTHON,
QTA_FMI_PYTHON; REQUIRED under QTA_INTEGRATIONS_REQUIRED) the full
demonstration runs, FEniCSx admits the slab result, and the fault FMU is
the twin that must be refused.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import harness_demo as HD  # noqa: E402


@pytest.fixture(scope="module")
def generic():
    return HD.run(fenicsx=None, fmi=None, required=set())


def test_the_generic_leg_runs_end_to_end(generic):
    leg = generic["legs"]["slab"]
    assert generic["accepted"], generic["why"]
    assert leg["checks"] == {"thermal.slab_series": "PASS"}
    assert leg["decision"]["state"] == "REJECTED"
    assert leg["decision"]["rejection"]["problems"] == [
        "the report is not an independent check"]
    assert leg["decision"]["proposer"] == HD.AGENT
    assert leg["hdf5"]["sha256"]
    rec = generic["reconstruction"]
    assert rec["generic_consistency_findings"] == {}
    assert rec["second_reader_anomalies"] == []
    assert rec["proposals_reconstructed"] == 1
    assert generic["crate"]["problems"] == [] and generic["crate"]["files"]
    r41 = generic["recovery"]
    assert r41["mode"] == "CHECKPOINT_ASSISTED" and r41["healthy"]
    assert r41["agrees_with_full_replay"] and r41["prefix_verified"] is False
    assert all(c["path"] and c["source_sha256"]
               for c in generic["context"]["citations"])


def test_a_demonstration_compared_with_itself_is_byte_identical(generic,
                                                                tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(generic))
    res = HD.compare(a, a)
    assert res["status"] == "BYTE_IDENTICAL"


def _full():
    return {"legs": {
        "slab": {"checks": {"thermal.slab_series": "PASS",
                            "thermal.slab_fenicsx": "PASS"},
                 "decision": {"state": "VERIFIED"}},
        "fmu": {"decision": {"state": "VERIFIED", "report_status": "PASS"}},
        "fmu_negative_twin": {
            "decision": {"state": "REJECTED", "report_status": "FAIL"},
            "invariants": {"finite": True, "energy_balance": True,
                           "claim_boundary": True}}},
        "reconstruction": {"generic_consistency_findings": {},
                           "second_reader_anomalies": []},
        "crate": {"problems": []},
        "recovery": {"mode": "CHECKPOINT_ASSISTED", "healthy": True,
                     "agrees_with_full_replay": True,
                     "prefix_verified": False}}


def test_the_judge_accepts_a_complete_demonstration():
    assert HD.judge(_full(), "py", "py") == (True, [])


@pytest.mark.parametrize("path, value, why", [
    (("legs", "fmu_negative_twin", "decision", "state"), "VERIFIED",
     "NEGATIVE TWIN WAS NOT REJECTED"),
    (("legs", "fmu_negative_twin", "decision", "report_status"), "PASS",
     "did not FAIL"),
    (("legs", "fmu_negative_twin", "invariants", "energy_balance"), False,
     "only the independent check"),
    (("legs", "fmu", "decision", "state"), "REJECTED", "good FMU"),
    (("legs", "slab", "decision", "state"), "REJECTED", "slab decided"),
    (("legs", "slab", "checks", "thermal.slab_fenicsx"), "FAIL",
     "slab checks"),
    (("reconstruction", "second_reader_anomalies"), ["x"],
     "reconstruction"),
    (("reconstruction", "generic_consistency_findings"), {"E": ["x"]},
     "reconstruction"),
    (("crate", "problems"), ["a.h5"], "crate"),
    (("recovery", "mode"), "FULL_REPLAY", "restart"),
    (("recovery", "agrees_with_full_replay"), False, "restart"),
    (("recovery", "healthy"), False, "restart"),
])
def test_the_judge_refuses_an_incomplete_demonstration(path, value, why):
    rep = copy.deepcopy(_full())
    d = rep
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value
    ok, reasons = HD.judge(rep, "py", "py")
    assert not ok and any(why in r for r in reasons), reasons


def test_a_required_runtime_that_is_missing_fails_the_demonstration():
    with pytest.raises(HD.DemoFailed, match="FEniCSx is required"):
        HD.run(fenicsx=None, fmi=None, required={"fenicsx"})


def _runtimes():
    fem = os.environ.get("QTA_FENICSX_PYTHON", "")
    fmi = os.environ.get("QTA_FMI_PYTHON", "")
    req = set(filter(None, os.environ.get("QTA_INTEGRATIONS_REQUIRED",
                                          "").split(",")))
    if not (fem and fmi):
        if {"fenicsx", "fmi"} <= req:
            pytest.fail("both runtimes are REQUIRED for the full "
                        "demonstration")
        pytest.skip("the full demonstration needs FEniCSx and FMI runtimes")
    return fem, fmi


def test_the_full_demonstration_and_its_negative_twin():
    fem, fmi = _runtimes()
    rep = HD.run(fenicsx=fem, fmi=fmi, required={"fenicsx", "fmi"})
    assert rep["accepted"], rep["why"]
    assert rep["legs"]["slab"]["decision"]["state"] == "VERIFIED"
    assert rep["legs"]["slab"]["decision"]["report_check"] == \
        "thermal.slab_fenicsx"
    twin = rep["legs"]["fmu_negative_twin"]
    assert twin["decision"]["state"] == "REJECTED"
    assert twin["claim_boundary"]["authority"] == "NON_AUTHORITATIVE"
    assert rep["reconstruction"]["proposals_reconstructed"] == 3


@pytest.mark.parametrize("accepted, want", [(True, 0), (False, 1)])
def test_the_demonstration_exits_by_its_judge(monkeypatch, tmp_path,
                                              accepted, want):
    """Snakemake's harness_demo rule is only as strict as this exit status:
    a demonstration the judge does not accept must fail the target."""
    rep = {"accepted": accepted, "why": [] if accepted else ["twin"],
           "legs": {}}
    monkeypatch.setattr(HD, "run", lambda **kw: rep)
    assert HD.main(["run", "--out", str(tmp_path / "r.json")]) == want
    assert json.loads((tmp_path / "r.json").read_text())["accepted"] is \
        accepted


def test_a_demonstration_that_raises_fails(monkeypatch, tmp_path):
    def boom(**kw):
        raise HD.DemoFailed("FEniCSx is required")
    monkeypatch.setattr(HD, "run", boom)
    assert HD.main(["run", "--out", str(tmp_path / "r.json")]) == 1
