"""Resolution-aware ranking (D-2026-99) and per-quantity equivalence.

The ranking tests reproduce the defect's shape -- values agreeing far below
their resolution, ordered differently by two "backends" -- and require that
the policy reports the same tiers and the same ambiguous selection for both,
where argsort reports different sets. The equivalence tests run real models
and require every status to be reachable for the reason it names.
"""
from __future__ import annotations

import json
import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scientific import equivalence as EQ
from scientific import ranking as RK
from scientific.models.thermal_rc2 import ThermalRC2Model
from scientific.quantity import Quantity, ResolutionClass
from scientific.result import Output, OutputStatus, ResultBundle

# ------------------------------------------------------------------ ranking


def _r(key, value, res=0.0):
    return RK.Ranked(key, value, res, "test")


def test_distinguishable_values_rank_in_order():
    t = RK.tiers([_r("a", 1.0, 0.1), _r("b", 3.0, 0.1), _r("c", 2.0, 0.1)])
    assert [x.keys for x in t] == [("b",), ("c",), ("a",)]


def test_overlapping_values_share_a_tier_and_touching_bounds_overlap():
    t = RK.tiers([_r("a", 1.0, 0.5), _r("b", 2.0, 0.5), _r("c", 5.0, 0.1)])
    assert [x.keys for x in t] == [("c",), ("a", "b")]


def test_tiers_are_components_not_neighbours():
    """a and c are far apart but both overlap b: one tier, because the
    order of a against c is known and against b is not -- no tier may
    split b off arbitrarily."""
    t = RK.tiers([_r("a", 0.0, 0.6), _r("b", 1.0, 0.6), _r("c", 2.0, 0.6)])
    assert [x.keys for x in t] == [("a", "b", "c")]


def test_the_d_2026_99_shape_backend_noise_cannot_move_a_selection():
    """Sixteen hottest of a plane whose temperatures agree to ten digits.
    Two backends perturb the last bits differently; argsort picks different
    cells, the policy reports the same tiers and the same ambiguity."""
    rng = random.Random(99)
    base = [10.0 + 1e-3 * (i // 10) for i in range(40)]
    noise_a = [v + rng.uniform(-1, 1) * 1e-12 for v in base]
    noise_b = [v + rng.uniform(-1, 1) * 1e-12 for v in base]
    res = 1e-9
    a = [_r(f"cell{i:02d}", v, res) for i, v in enumerate(noise_a)]
    b = [_r(f"cell{i:02d}", v, res) for i, v in enumerate(noise_b)]
    top_a = sorted(range(40), key=lambda i: -noise_a[i])[:16]
    top_b = sorted(range(40), key=lambda i: -noise_b[i])[:16]
    assert set(top_a) != set(top_b), "the fixture must reproduce the defect"
    sa, sb = RK.select_top(a, 16), RK.select_top(b, 16)
    assert sa == sb
    assert sa.status == RK.AMBIGUOUS
    assert sa.selected == tuple(f"cell{i}" for i in range(30, 40))
    assert len(sa.candidates) == 10 and sa.slots_left == 6
    cmp = RK.compare("a", RK.tiers(a), "b", RK.tiers(b))
    assert cmp.status == RK.IDENTICAL


def test_a_selection_that_resolution_decides_is_resolved():
    items = [_r(f"k{i}", float(i), 0.1) for i in range(10)]
    s = RK.select_top(items, 3)
    assert s.status == RK.RESOLVED
    assert s.selected == ("k9", "k8", "k7") and s.candidates == ()


def test_ascending_order_is_supported():
    s = RK.select_top([_r("x", 1.0), _r("y", 0.0)], 1, descending=False)
    assert s.selected == ("y",)


def test_a_quantity_without_a_resolution_is_refused_not_ranked():
    with pytest.raises(RK.RankingRefused, match="D-2026-99"):
        RK.Ranked.from_quantity("x", Quantity(1.0, "K"))
    with pytest.raises(RK.RankingRefused):
        RK.ranked_quantities({"x": Quantity(1.0, "K")})


def test_an_exact_quantity_ranks_with_zero_width():
    r = RK.Ranked.from_quantity("x", Quantity(0.0, "W", exact=True))
    assert r.resolution == 0.0


@pytest.mark.parametrize("bad", [
    lambda: RK.Ranked("", 1.0, 0.1, "b"),
    lambda: RK.Ranked("k", float("nan"), 0.1, "b"),
    lambda: RK.Ranked("k", 1.0, -0.1, "b"),
    lambda: RK.Ranked("k", 1.0, float("inf"), "b"),
    lambda: RK.Ranked("k", True, 0.1, "b"),
    lambda: RK.Ranked("k", 1.0, 0.1, " "),
    lambda: RK.tiers([]),
    lambda: RK.tiers([_r("k", 1.0), _r("k", 2.0)]),
    lambda: RK.tiers([("k", 1.0)]),
    lambda: RK.select_top([_r("k", 1.0)], 0),
    lambda: RK.select_top([_r("k", 1.0)], True),
])
def test_malformed_ranking_input_is_refused(bad):
    with pytest.raises(RK.RankingRefused):
        bad()


def test_methods_that_order_a_pair_oppositely_disagree():
    oat = RK.tiers([_r("p", 3.0, 0.1), _r("q", 2.0, 0.1), _r("s", 1.0, 0.1)])
    sob = RK.tiers([_r("p", 2.0, 0.1), _r("q", 3.0, 0.1), _r("s", 1.0, 0.1)])
    c = RK.compare("oat", oat, "sobol", sob)
    assert c.status == RK.DISAGREE
    assert c.discordant == (("p", "q"),)
    assert c.kendall_tau_b == pytest.approx(1 / 3)


def test_a_pair_one_method_leaves_tied_is_not_a_disagreement():
    a = RK.tiers([_r("p", 3.0, 0.1), _r("q", 2.0, 0.1)])
    b = RK.tiers([_r("p", 3.0, 1.0), _r("q", 2.0, 1.0)])
    c = RK.compare("a", a, "b", b)
    assert c.status == RK.CONSISTENT
    assert c.resolved_by_a_only == (("p", "q"),)


def test_comparing_different_key_sets_or_one_key_is_refused():
    with pytest.raises(RK.RankingRefused, match="different keys"):
        RK.compare("a", RK.tiers([_r("p", 1.0), _r("q", 2.0)]),
                   "b", RK.tiers([_r("p", 1.0), _r("z", 2.0)]))
    with pytest.raises(RK.RankingRefused, match="no pair"):
        RK.compare("a", RK.tiers([_r("p", 1.0)]),
                   "b", RK.tiers([_r("p", 1.0)]))


_vals = st.lists(st.tuples(st.floats(-1e3, 1e3), st.floats(0, 10)),
                 min_size=1, max_size=25)


@settings(max_examples=200, deadline=None)
@given(_vals)
def test_tiers_are_strictly_ordered_and_partition_the_keys(vals):
    items = [_r(f"k{i}", v, r) for i, (v, r) in enumerate(vals)]
    by = {i.key: i for i in items}
    t = RK.tiers(items)
    assert sorted(k for x in t for k in x.keys) == sorted(by)
    for hi, lo in zip(t, t[1:]):
        for a in hi.keys:
            for b in lo.keys:
                assert by[a].value - by[a].resolution > \
                    by[b].value + by[b].resolution


@settings(max_examples=200, deadline=None)
@given(_vals, st.randoms(use_true_random=False))
def test_ranking_does_not_depend_on_input_order(vals, rnd):
    items = [_r(f"k{i}", v, r) for i, (v, r) in enumerate(vals)]
    shuffled = list(items)
    rnd.shuffle(shuffled)
    assert RK.tiers(items) == RK.tiers(shuffled)
    k = 1 + len(items) // 2
    assert RK.select_top(items, k) == RK.select_top(shuffled, k)


@settings(max_examples=200, deadline=None)
@given(_vals, st.integers(1, 30))
def test_selection_never_splits_a_tier(vals, k):
    items = [_r(f"k{i}", v, r) for i, (v, r) in enumerate(vals)]
    s = RK.select_top(items, k)
    for t in RK.tiers(items):
        inside = set(t.keys) & set(s.selected)
        assert not inside or inside == set(t.keys)
    assert len(s.selected) <= k
    if s.status == RK.AMBIGUOUS:
        assert 0 < s.slots_left < len(s.candidates)
        assert len(s.selected) + s.slots_left == k

# -------------------------------------------------------------- equivalence


PARAMS = {"C1_J_K": 500.0, "C2_J_K": 2000.0, "G12_W_K": 5.0,
          "G2a_W_K": 2.0, "T1_0_K": 300.0, "T2_0_K": 300.0, "Q_W": 10.0,
          "Tamb_K": 300.0, "t_end_s": 3600.0}


@pytest.fixture(scope="module")
def bundle():
    return ThermalRC2Model().run(PARAMS)


def _rebuild(b: ResultBundle, *, outputs=None, invariants=None, impl=None,
             env=None) -> ResultBundle:
    rec = b.to_record()
    if outputs is not None:
        rec["outputs"] = outputs
    if invariants is not None:
        rec["invariants"] = invariants
    if impl:
        rec["implementation_digest"] = impl
    if env:
        rec["environment_digest"] = env
    return ResultBundle.from_record(rec)


def _shift(b, name, dv, scale_res=1.0, res=None):
    outs = b.to_record()["outputs"]
    for o in outs:
        if o["name"] == name:
            q = o["quantity"]
            q["value"] += dv
            if res is not None:
                q.update(res)
            elif q["resolution"] is not None:
                q["resolution"] *= scale_res
            q.pop("zero_state")
    return outs


def test_the_same_bundle_is_byte_identical(bundle):
    r = EQ.compare(bundle, bundle)
    assert r["status"] == EQ.BYTE_IDENTICAL


def test_another_backend_within_declared_resolution_is_equivalent(bundle):
    q = bundle.output("T1").quantity
    other = _rebuild(bundle, outputs=_shift(bundle, "T1",
                                            0.9 * 2 * q.resolution),
                     env="e" * 64)
    r = EQ.compare(bundle, other)
    assert r["status"] == EQ.EQUIVALENT
    t1 = [v for v in r["quantities"] if v["name"] == "T1"][0]
    assert t1["bound"] == pytest.approx(2 * q.resolution)
    assert "SINGLE_EVALUATION" in t1["bound_provenance"]


def test_a_difference_beyond_the_two_bounds_is_not_equivalent(bundle):
    q = bundle.output("T2").quantity
    other = _rebuild(bundle, outputs=_shift(bundle, "T2",
                                            2.5 * q.resolution + 1e-300))
    r = EQ.compare(bundle, other)
    assert r["status"] == EQ.NOT_EQUIVALENT
    assert r["decision"] == EQ.DECISION_STABLE, \
        "the decision is stable and the result still differs -- reported apart"


def test_an_unstated_resolution_is_not_established(bundle):
    outs = _shift(bundle, "E_in", 0.0, res={
        "resolution": None, "resolution_class": "NOT_STATED",
        "resolution_basis": ""})
    r = EQ.compare(bundle, _rebuild(bundle, outputs=outs, env="e" * 64))
    assert r["status"] == EQ.NOT_ESTABLISHED


def test_a_unit_change_is_not_equivalent_never_converted(bundle):
    outs = bundle.to_record()["outputs"]
    for o in outs:
        if o["name"] == "E_out":
            o["quantity"]["unit"] = "kJ"
    r = EQ.compare(bundle, _rebuild(bundle, outputs=outs))
    assert r["status"] == EQ.NOT_EQUIVALENT


def test_an_invariant_failing_on_one_side_is_not_equivalent(bundle):
    inv = bundle.to_record()["invariants"]
    inv[0]["holds"] = False
    r = EQ.compare(bundle, _rebuild(bundle, invariants=inv))
    assert r["status"] == EQ.NOT_EQUIVALENT
    assert r["decision"] == EQ.DECISION_CHANGED


def test_a_failed_output_is_not_established(bundle):
    outs = bundle.to_record()["outputs"]
    outs[0] = {"name": outs[0]["name"], "status": "FAILED", "quantity": None,
               "reason": "diverged"}
    r = EQ.compare(bundle, _rebuild(bundle, outputs=outs))
    assert r["status"] == EQ.NOT_ESTABLISHED


def test_an_output_the_contract_does_not_classify_is_not_established(bundle):
    doc = EQ.load_contracts()
    del doc["models"]["thermal.rc2_network"]["outputs"]["T1"]
    other = _rebuild(bundle, env="e" * 64)
    r = EQ.compare(bundle, other, doc)
    assert r["status"] == EQ.NOT_ESTABLISHED
    assert [v["rule"] for v in r["quantities"]
            if v["name"] == "T1"] == ["UNCLASSIFIED"]


def test_an_output_on_one_side_only_is_not_equivalent(bundle):
    outs = bundle.to_record()["outputs"][1:]
    r = EQ.compare(bundle, _rebuild(bundle, outputs=outs))
    assert r["status"] == EQ.NOT_EQUIVALENT


def test_no_invariants_means_not_established(bundle):
    a = _rebuild(bundle, invariants=[])
    b = _rebuild(bundle, invariants=[], env="e" * 64)
    assert EQ.compare(a, b)["status"] == EQ.NOT_ESTABLISHED


def test_different_models_or_parameters_are_refused(bundle):
    other = ThermalRC2Model().run(dict(PARAMS, Q_W=11.0))
    with pytest.raises(EQ.EquivalenceRefused, match="parameters"):
        EQ.compare(bundle, other)
    rec = bundle.to_record()
    rec["model_id"] = "fmi.thermal_rc2"
    with pytest.raises(EQ.EquivalenceRefused, match="different models"):
        EQ.compare(bundle, ResultBundle.from_record(rec))


def test_a_model_without_a_contract_is_refused(bundle):
    other = _rebuild(bundle, env="e" * 64)
    with pytest.raises(EQ.EquivalenceRefused, match="no equivalence"):
        EQ.compare(bundle, other, {"models": {}})


@pytest.mark.parametrize("mutate, why", [
    (lambda d: d["models"]["thermal.rc2_network"]["outputs"]["T1"].update(
        tolerance=1e-6), "no numbers"),
    (lambda d: d["models"]["thermal.rc2_network"].update(rtol=0.01),
     "no numbers"),
    (lambda d: d["models"]["thermal.rc2_network"]["outputs"]["T1"].update(
        rule="CLOSE_ENOUGH"), "rule"),
    (lambda d: d["models"]["thermal.rc2_network"]["outputs"].update(
        T1={"rule": "EXCLUDED"}), "says why"),
    (lambda d: d["models"]["thermal.rc2_network"].update(provenance=""),
     "where"),
    (lambda d: d.update(schema="equivalence-contracts/0"), "schema"),
])
def test_a_contract_with_a_tolerance_or_without_reasons_is_refused(
        tmp_path, mutate, why):
    doc = json.loads(EQ.CONTRACTS.read_text())
    mutate(doc)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(EQ.EquivalenceRefused, match=why):
        EQ.load_contracts(p)


def test_every_admitted_model_has_a_contract_for_every_output():
    from scientific import catalog
    doc = EQ.load_contracts()
    for model_id, _, _ in catalog.models().entries():
        assert model_id in doc["models"], model_id


def test_the_report_is_deterministic(bundle):
    other = _rebuild(bundle, env="e" * 64)
    assert EQ.compare(bundle, other) == EQ.compare(bundle, other)
    assert math.isfinite(EQ.compare(bundle, other)["quantities"][0][
        "difference"])


def test_a_quantity_record_without_resolution_class_cannot_be_smuggled():
    with pytest.raises(Exception):
        Quantity(1.0, "K", resolution=1e-9)
    assert Quantity(1.0, "K", resolution=1e-9,
                    resolution_class=ResolutionClass.SINGLE_EVALUATION,
                    resolution_basis="b").resolution == 1e-9


def test_output_status_values_are_closed():
    with pytest.raises(ValueError):
        Output("x", "MAYBE")
    assert OutputStatus("OK") is OutputStatus.OK
