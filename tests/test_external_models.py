"""External models, held to their rules without their runtimes: the FMU
bundle's own invariants, the catalog's admission of an external model, and
the governed FMU tool's refusal of bytes nobody cited."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
