"""External models and implementation decisions, held to their rules
without their runtimes: the FMU bundle's own invariants, the catalog's
admission of an external model, the governed FMU tool's refusal of bytes
nobody cited, and the selective-Rust decision rule over synthetic
measurements."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import rust_kernel_decision as RKD  # noqa: E402
from scientific import catalog  # noqa: E402
from scientific.checks import fmu_rc2  # noqa: E402
from scientific.quantity import Quantity, ResolutionClass as RC  # noqa: E402
from scientific.result import Output, OutputStatus  # noqa: E402

GOOD_CLAIM = {"observation_kind": "SIMULATION_RESULT",
              "authority": "NON_AUTHORITATIVE",
              "scientific_model": "thermal.rc2_network"}


def _outs(e_in=36000.0, e_out=1000.0, e_st=35000.0, r=1e-3):
    def q(v, u):
        return Quantity(v, u, resolution=r,
                        resolution_class=RC.DISCRETIZATION_ESTIMATE,
                        resolution_basis="test")
    return [Output("T1", OutputStatus.OK, q(310.0, "K")),
            Output("T2", OutputStatus.OK, q(305.0, "K")),
            Output("E_in", OutputStatus.OK, q(e_in, "J")),
            Output("E_out", OutputStatus.OK, q(e_out, "J")),
            Output("E_stored", OutputStatus.OK, q(e_st, "J"))]


def _inv(outs, claim=GOOD_CLAIM):
    return {i.invariant_id: i.holds for i in fmu_rc2._invariants(outs, claim)}


def test_a_balanced_fmu_result_holds_its_invariants():
    assert _inv(_outs()) == {"finite": True, "energy_balance": True,
                             "claim_boundary": True}


def test_an_unbalanced_fmu_result_fails_its_energy_invariant():
    assert _inv(_outs(e_st=34000.0))["energy_balance"] is False


def test_the_balance_bound_comes_from_the_declared_resolutions():
    # a gap of 0.02 J: inside 3 x (3 x 0.01) J, outside 3 x (3 x 0.001) J
    assert _inv(_outs(e_st=34999.98, r=1e-2))["energy_balance"] is True
    assert _inv(_outs(e_st=34999.98, r=1e-3))["energy_balance"] is False


@pytest.mark.parametrize("claim", [
    dict(GOOD_CLAIM, authority="AUTHORITATIVE"),
    dict(GOOD_CLAIM, observation_kind="MEASUREMENT"), {}])
def test_a_broken_claim_boundary_fails_its_invariant(claim):
    assert _inv(_outs(), claim)["claim_boundary"] is False


def test_a_failed_output_fails_finite():
    outs = _outs()
    outs[0] = Output("T1", OutputStatus.FAILED, reason="non-finite")
    inv = _inv(outs)
    assert inv["finite"] is False and "energy_balance" not in inv


class _B:
    def __init__(self, mid="fmi.thermal_rc2", ver="1.0.0", impl="a" * 64,
                 arch="a" * 64, auth="NON_AUTHORITATIVE"):
        self.model_id, self.model_version = mid, ver
        self.implementation_digest = impl
        self.provenance = {"fmi": {"archive_sha256": arch,
                                   "claim_boundary": {"authority": auth}}}


def test_an_external_model_is_admitted_by_the_catalog_table():
    assert catalog.external_model_problems(_B()) == []
    assert "not an admitted external model" in \
        catalog.external_model_problems(_B(mid="fmi.other"))[0]
    assert catalog.external_model_problems(_B(arch="b" * 64))
    assert catalog.external_model_problems(_B(auth="AUTHORITATIVE"))


def test_the_governed_fmu_tool_refuses_bytes_nobody_cited(tmp_path,
                                                          monkeypatch):
    from scientific import _governed_fmu as GF

    class WS:
        @staticmethod
        def assert_in_workspace(path, what=""):
            return Path(path)
    f = tmp_path / "x.fmu"
    f.write_bytes(b"fmu bytes")
    import hashlib
    good = hashlib.sha256(b"fmu bytes").hexdigest()
    assert GF._cited(WS, str(f), good, "FMU") == b"fmu bytes"
    with pytest.raises(SystemExit, match="not the FMU cited"):
        GF._cited(WS, str(f), "0" * 64, "FMU")


# ------------------------------------------------------------ selective Rust

def _measurement(parity=True, speedup=3.0, sites=None):
    k = {"bit_identical": parity, "max_ulp_difference": 0 if parity else 2,
         "n_test_values": 4096,
         "timing": {"4096": {"speedup": 1.0},
                    "1000000": {"speedup": speedup}}}
    cfg = {"extension_importable": True, "numpy_version": "2.4.4",
           "simd": {}, "kernels": {"face_conductance": dict(k)}}
    return {"configs": {"native": cfg,
                        "without_x86_v4": json.loads(json.dumps(cfg)),
                        "x86_v2_baseline": json.loads(json.dumps(cfg))},
            "call_sites": sites if sites is not None else
            {"scientific/solver.py": 10}, "workload_n": 1000000}


def test_a_kernel_meeting_every_criterion_is_adopted():
    d = RKD.decide(_measurement())["face_conductance"]
    assert d["decision"] == "RUST_KERNEL_face_conductance_ADOPTED"
    assert d["failed_criteria"] == []


@pytest.mark.parametrize("kw, criterion", [
    ({"sites": {}}, "NO_PRODUCTION_CALL_SITE"),
    ({"speedup": 1.5}, "SPEEDUP_BELOW"),
])
def test_a_kernel_failing_a_criterion_is_rejected(kw, criterion):
    d = RKD.decide(_measurement(**kw))["face_conductance"]
    assert d["decision"].endswith("_REJECTED")
    assert any(c.startswith(criterion) for c in d["failed_criteria"])


def test_parity_must_hold_in_every_dispatch_not_just_one():
    m = _measurement()
    m["configs"]["without_x86_v4"]["kernels"]["face_conductance"][
        "bit_identical"] = False
    d = RKD.decide(m)["face_conductance"]
    assert d["decision"].endswith("_REJECTED")
    assert d["failed_criteria"][0].startswith(
        "NOT_BIT_IDENTICAL_IN_EVERY_DISPATCH")


def test_an_unmeasured_extension_decides_nothing():
    m = _measurement()
    m["configs"]["native"]["extension_importable"] = False
    with pytest.raises(SystemExit):
        RKD.decide(m)


def test_the_committed_decisions_are_current():
    assert RKD.check() == []


def test_a_new_call_site_invalidates_the_decisions(monkeypatch):
    monkeypatch.setattr(RKD, "call_sites",
                        lambda root=RKD.ROOT: {"scientific/new.py": 3})
    assert any("call sites changed" in p for p in RKD.check())


def test_a_measurement_deciding_otherwise_is_a_problem(monkeypatch):
    """A runner whose measurement would ADOPT a kernel the registry rejects
    fails the check: the committed decision must be what measurement
    decides, wherever it is re-measured."""
    m = _measurement()
    for c in m["configs"].values():
        c["kernels"]["conductivity_power_law"] = dict(
            c["kernels"]["face_conductance"], bit_identical=False)
    monkeypatch.setattr(RKD, "call_sites",
                        lambda root=RKD.ROOT: {"scientific/solver.py": 10})
    probs = RKD.check(m)
    assert any("face_conductance: recorded RUST_KERNEL_face_conductance_"
               "REJECTED, this measurement decides "
               "RUST_KERNEL_face_conductance_ADOPTED" in p for p in probs)
    assert not any(p.startswith("conductivity_power_law") for p in probs)
