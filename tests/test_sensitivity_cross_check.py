"""The generic sensitivity cross-check: OAT and Sobol, compared at
resolution, disagreement reported and never resolved.

The SALib-dependent tests skip without SALib, except where
QTA_UQ_REQUIRED=1 (stack-verify's full leg sets it) -- there a missing SALib
is a failure, so the global path cannot pass by skipping.
"""
from __future__ import annotations

import os
import sys

import pytest

from scientific import ranking as RK
from scientific import sensitivity as S
from scientific.models.thermal_rc2 import ThermalRC2Model
from scientific.quantity import Quantity, ResolutionClass

EPS = sys.float_info.epsilon
FIXED = {"C1_J_K": 500.0, "C2_J_K": 2000.0, "G12_W_K": 5.0, "G2a_W_K": 2.0,
         "T1_0_K": 300.0, "T2_0_K": 300.0, "Q_W": 10.0, "Tamb_K": 300.0,
         "t_end_s": 3600.0}
VARIED = ("C1_J_K", "C2_J_K", "G12_W_K", "G2a_W_K", "Q_W")


def _need_salib():
    if S.salib_available():
        return
    if os.environ.get("QTA_UQ_REQUIRED") == "1":
        pytest.fail("QTA_UQ_REQUIRED=1 and SALib is not installed")
    pytest.skip("SALib (the uq extra) is not installed")


def _q(v):
    return Quantity(v, "1", resolution=8 * EPS * max(abs(v), 1.0),
                    resolution_class=ResolutionClass.SINGLE_EVALUATION,
                    resolution_basis="a few roundings")


def _synthetic(f):
    return S.Response("synthetic@0", "0" * 64, "y", "1",
                      lambda p: _q(f(p)))


#: locally a dominates (b sits at the bottom of a parabola), globally b
#: carries most of the variance over +-10 %: the two methods must disagree.
def _parabola(p):
    return p["a"] + 50.0 * (p["b"] - 1.0) ** 2


@pytest.fixture(scope="module")
def rc2():
    return S.model_response(ThermalRC2Model(), "T1", FIXED)


def test_oat_ranks_by_the_responses_declared_resolution(rc2):
    out = S.oat(rc2, {k: FIXED[k] for k in VARIED}, 0.1)
    assert out["method"]["evaluations"] == len(VARIED) + 1
    assert [t.keys for t in out["tiers"]][0] == ("Q_W",)
    for row in out["rows"]:
        assert row["resolution"] > 0


def test_the_fast_path_publishes_what_run_publishes(rc2):
    m = ThermalRC2Model()
    _, outs, _ = m.evaluate_outputs(FIXED)
    run = m.run(FIXED)
    assert [o.to_record() for o in outs] == \
        [o.to_record() for o in run.outputs]


def test_methods_that_disagree_are_reported_as_such():
    _need_salib()
    rep = S.cross_check(_synthetic(_parabola), {"a": 1.0, "b": 1.0},
                        fraction=0.1, step=0.01, n_base=512)
    assert rep["status"] == RK.DISAGREE
    assert rep["comparison"]["discordant_pairs"] == [["a", "b"]]
    assert rep["rankings"]["OAT"][0]["members"] == ["a"]
    assert rep["rankings"]["SOBOL_SALIB"][0]["members"] == ["b"]
    assert rep["interpretation"].startswith("REQUIRES_HUMAN_INTERPRETATION")
    assert rep["automatic_gate_effect"] == "NONE"
    for field in ("response", "parameter_domain", "base_point", "methods",
                  "rankings", "comparison"):
        assert rep[field], field
    assert {m["id"] for m in rep["methods"]} == {"OAT", "SOBOL_SALIB"}


def test_the_rc2_network_cross_check(rc2):
    _need_salib()
    rep = S.cross_check(rc2, {k: FIXED[k] for k in VARIED}, n_base=256)
    assert rep["status"] in (RK.IDENTICAL, RK.CONSISTENT, RK.DISAGREE)
    assert rep["response"]["model"] == "thermal.rc2_network@1.0.0"
    assert rep["methods"][1]["evaluations"] == 256 * (len(VARIED) + 2)
    for row in rep["rows"]["SOBOL_SALIB"]:
        assert row["ST_conf"] > 0


def test_the_cross_check_is_deterministic():
    _need_salib()
    a = S.cross_check(_synthetic(_parabola), {"a": 1.0, "b": 1.0},
                      step=0.01, n_base=64)
    b = S.cross_check(_synthetic(_parabola), {"a": 1.0, "b": 1.0},
                      step=0.01, n_base=64)
    assert a == b


def test_without_salib_nothing_is_substituted(monkeypatch):
    monkeypatch.setattr(S, "salib_available", lambda: False)
    rep = S.cross_check(_synthetic(_parabola), {"a": 1.0, "b": 1.0},
                        step=0.01)
    assert rep["status"] == "GLOBAL_UNAVAILABLE"
    assert rep["comparison"] is None
    assert "SOBOL_SALIB" not in rep["rankings"]


def test_a_response_without_resolution_is_refused():
    resp = S.Response("m@0", "0" * 64, "y", "1",
                      lambda p: Quantity(p["a"], "1"))
    with pytest.raises(S.SensitivityRefused, match="D-2026-99"):
        S.oat(resp, {"a": 1.0}, 0.1)


@pytest.mark.parametrize("kw, why", [
    ({"step": 0.0}, "step"), ({"step": 1.5}, "step"),
    ({"fraction": 0.0}, "fraction"),
    ({"n_base": 100}, "power of two"),
])
def test_malformed_configuration_is_refused(kw, why):
    if "n_base" in kw or "fraction" in kw:
        _need_salib()
    with pytest.raises(S.SensitivityRefused, match=why):
        S.cross_check(_synthetic(_parabola), {"a": 1.0, "b": 1.0}, **kw)


def test_a_relative_domain_around_zero_is_refused():
    with pytest.raises(S.SensitivityRefused, match="empty"):
        S._domain({"a": 0.0}, 0.1)


def test_no_parameters_is_refused():
    with pytest.raises(S.SensitivityRefused, match="no parameter"):
        S.cross_check(_synthetic(_parabola), {})


def test_a_failed_output_is_refused_not_ranked():
    m = ThermalRC2Model()
    resp = S.model_response(m, "T1", FIXED)

    def bad(p):
        raise S.SensitivityRefused("T1 is FAILED: non-finite closed form")
    broken = S.Response(resp.model, resp.implementation_digest, "T1", "K",
                        bad)
    with pytest.raises(S.SensitivityRefused, match="FAILED"):
        S.oat(broken, {"Q_W": 10.0}, 0.1)
