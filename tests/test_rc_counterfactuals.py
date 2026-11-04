"""The RC network's counterfactual family (Stage 3), and what it rests on.

THE FAMILY. The owner's question asked of the two-node RC network
(``thermal.rc2_network``) in ten worlds
(``tools/adversarial_eval.RC_COUNTERFACTUALS``): each differs from the
documented point in one thing -- the heat input, the capacities, either
conductance, the initial state -- with thresholds above and below the
answer, both outputs, both directions of comparison, and one TIE, a
threshold that is the network's own value, where the right answer is to
abstain. The computation and the judgment are apart: an analytic reference
that shares no code with the model (``tools/rc2_reference.py``) computes the
network's value and a bound on its own rounding; ``rc_oracle`` decides from
them what each world answers. No agent is shown either.

WHAT IT RESTS ON, HELD HERE, CHEAPEST FIRST

* the model states a resolution its computation achieves (AUD-024): every
  output against a 90-digit evaluation of its own formula, across the
  admitted bounds, and at the points where it did not;
* the reference states a rounding bound it keeps, agrees with the model
  inside both, and refuses what is no network;
* the independent check (``scientific/checks/rc2_rk4.py``) states an error
  its march keeps, passes the model, and fails a producer off by more than
  both stated errors;
* the loop's own check table still names no check for the network (the
  premise of scenario TL1); a deployment's table does.

THE CONTROLS. Three wrong verdict rules -- always SUPPORTED, always REFUTED,
never deciding (``adversarial_eval.STAMPS``) -- each fail the family,
exactly where the world's answer differs; ``tests/test_counterfactual_eval
.py`` holds the same three against the slab family.

The loop is scripted: no language model is run, and nothing here says how a
model would answer.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import math
import random
import subprocess
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from scientific import catalog, contracts  # noqa: E402
from scientific.checks import rc2_rk4 as K  # noqa: E402
from scientific.models.thermal_rc2 import ThermalRC2Model  # noqa: E402
from scientific.quantity import Quantity, ResolutionClass  # noqa: E402
from scientific.verification import Status  # noqa: E402
from tools import adversarial_eval as E  # noqa: E402

REF = E._rc_reference()
MODEL = ThermalRC2Model()
POINT = E.RC2_RUN["parameters"]
EPS = sys.float_info.epsilon
CASES = {c[0]: c for c in E.RC_COUNTERFACTUALS}
OUTPUTS = ("T1", "T2", "E_in", "E_out", "E_stored")


def _exact(p: dict) -> dict:
    """Every output by the model's own formula -- eigenvectors and the
    steady state -- in 90-digit decimal arithmetic: what the float
    computation's rounding is measured against."""
    with localcontext() as ctx:
        ctx.prec = 90
        C1, C2, G12, G2a, x1, x2, Q, Ta, t = (
            Decimal(repr(float(p[k]))) for k in (
                "C1_J_K", "C2_J_K", "G12_W_K", "G2a_W_K", "T1_0_K",
                "T2_0_K", "Q_W", "Tamb_K", "t_end_s"))
        a11, a12, a21, a22 = -G12 / C1, G12 / C1, G12 / C2, -(G12 + G2a) / C2
        s2 = Ta + Q / G2a
        s1 = s2 + Q / G12
        tr, det = a11 + a22, a11 * a22 - a12 * a21
        disc = (tr * tr / 4 - det).sqrt()
        lam = (tr / 2 + disc, tr / 2 - disc)
        vec = [(a12, lm - a11) for lm in lam]
        d0 = (x1 - s1, x2 - s2)
        dv = vec[0][0] * vec[1][1] - vec[1][0] * vec[0][1]
        cs = ((d0[0] * vec[1][1] - vec[1][0] * d0[1]) / dv,
              (vec[0][0] * d0[1] - d0[0] * vec[0][1]) / dv)

        def expm1(x):
            if abs(x) > Decimal("1e-6"):
                return x.exp() - 1
            out, term, n = Decimal(0), x, 1
            while abs(term) > Decimal("1e-100"):
                out, n = out + term, n + 1
                term = term * x / n
            return out
        xs = [expm1(lm * t) for lm in lam]
        dT1 = sum(c * v[0] * x for c, v, x in zip(cs, vec, xs))
        dT2 = sum(c * v[1] * x for c, v, x in zip(cs, vec, xs))
        e_out = Q * t + G2a * sum(c * v[1] * x / lm
                                  for c, v, x, lm in zip(cs, vec, xs, lam))
        return {"T1": float(x1 + dT1), "T2": float(x2 + dT2),
                "E_in": float(Q * t), "E_out": float(e_out),
                "E_stored": float(C1 * dT1 + C2 * dT2)}


def _sample(rng: random.Random, kind: str) -> dict:
    """A network inside the model's admitted bounds: ordinary, anywhere in
    the bounds, or nearly defective (two close eigenvalues, nearly parallel
    eigenvectors)."""
    if kind == "anywhere":
        p = {"C1_J_K": 10 ** rng.uniform(-6, 12),
             "C2_J_K": 10 ** rng.uniform(-6, 12),
             "G12_W_K": 10 ** rng.uniform(-9, 9),
             "G2a_W_K": 10 ** rng.uniform(-9, 9),
             "T1_0_K": 10 ** rng.uniform(-3, 5),
             "T2_0_K": 10 ** rng.uniform(-3, 5),
             "Q_W": rng.choice((-1, 1)) * 10 ** rng.uniform(-3, 9),
             "Tamb_K": 10 ** rng.uniform(-3, 5),
             "t_end_s": 10 ** rng.uniform(-6, 12)}
    elif kind == "nearly defective":
        C1, G12 = 10 ** rng.uniform(-2, 2), 10 ** rng.uniform(-2, 1)
        r = 10 ** rng.uniform(2, 7)
        p = {"C1_J_K": C1, "C2_J_K": C1 * r, "G12_W_K": G12,
             "G2a_W_K": G12 * r * (1 + rng.uniform(-1e-3, 1e-3)) - G12,
             "T1_0_K": rng.uniform(250, 400),
             "T2_0_K": rng.uniform(250, 400),
             "Q_W": rng.uniform(-50, 200), "Tamb_K": rng.uniform(250, 350),
             "t_end_s": 10 ** rng.uniform(-3, 4)}
    else:
        p = {"C1_J_K": 10 ** rng.uniform(0, 5),
             "C2_J_K": 10 ** rng.uniform(0, 5),
             "G12_W_K": 10 ** rng.uniform(-2, 2),
             "G2a_W_K": 10 ** rng.uniform(-2, 2),
             "T1_0_K": rng.uniform(250, 400),
             "T2_0_K": rng.uniform(250, 400),
             "Q_W": rng.uniform(-50, 200), "Tamb_K": rng.uniform(250, 350),
             "t_end_s": 10 ** rng.uniform(-2, 6)}
    return MODEL.validate(p)


KINDS = ("ordinary", "anywhere", "nearly defective")

#: AUD-024, reproduced on the Stage 2 tree: points at which every invariant
#: of the model held -- the run would have been admitted -- and an output
#: was further from its exact value than the resolution it stated: T1 and
#: T2 by 1.3e3 times, E_stored by 1.7e3 times, E_out by 1.4e3 times; and
#: an admitted network at which the slow eigenvalue cancelled to exactly
#: zero and the model raised ZeroDivisionError, as 870 of 5000 networks
#: drawn across the admitted bounds did.
AUD024_POINTS = (
    {"C1_J_K": 95600000000.0, "C2_J_K": 480000.0, "G12_W_K": 16500000.0,
     "G2a_W_K": 1.09e-07, "T1_0_K": 300.0, "T2_0_K": 300.0, "Q_W": 10.0,
     "Tamb_K": 300.0, "t_end_s": 3600.0},
    {"C1_J_K": 7.03454, "C2_J_K": 9567.69, "G12_W_K": 54.7588,
     "G2a_W_K": 0.0247679, "T1_0_K": 342.835, "T2_0_K": 378.645,
     "Q_W": 116.685, "Tamb_K": 342.807, "t_end_s": 105498.0},
    {"C1_J_K": 53615.5, "C2_J_K": 19836.7, "G12_W_K": 0.0709731,
     "G2a_W_K": 0.0337912, "T1_0_K": 333.234, "T2_0_K": 260.995,
     "Q_W": 186.456, "Tamb_K": 282.755, "t_end_s": 3.38012},
    {"C1_J_K": 9113.0, "C2_J_K": 33977.0, "G12_W_K": 0.0223274,
     "G2a_W_K": 53.7546, "T1_0_K": 358.357, "T2_0_K": 269.486,
     "Q_W": 63.384, "Tamb_K": 312.555, "t_end_s": 190425.0},
)


# ---- the loop's check table: cheap, and first ------------------------------

def test_the_loop_s_own_table_names_no_check_for_the_network():
    """The premise of scenario TL1, kept: a deployment that states no check
    table runs no RC network. The family's table adds that one entry."""
    assert "thermal.rc2_network" not in L.CHECKS
    assert E.RC_CHECKS == {**L.CHECKS,
                           "thermal.rc2_network": "thermal.rc2_rk4"}


@pytest.mark.parametrize("table", [
    ["thermal.rc2_network"], {"thermal.rc2_network": ""},
    {"": "thermal.rc2_rk4"}, {"thermal.rc2_network": None},
    {"thermal.rc2_network": ("thermal.rc2_rk4",)}])
def test_a_check_table_maps_each_model_to_one_check(table):
    with pytest.raises(ValueError, match="check table"):
        L._check_table(table)


def test_a_contract_is_held_to_the_deployment_s_table():
    offered = contracts.contracts({"thermal.rc2_network": "thermal.rc2_rk4"})
    assert L._offered(offered, E.RC_CHECKS)["thermal.rc2_network"][
        "independent_check"]["check_id"] == "thermal.rc2_rk4"
    with pytest.raises(ValueError, match="this loop asks None"):
        L._offered(offered, L.CHECKS)


# ---- the judgment ----------------------------------------------------------

def _answer(case) -> tuple:
    cid, quantity, op, threshold, changes = case
    params = {**POINT, **changes}
    if threshold is None:
        threshold = REF.temperatures(params)[quantity]
    return E.rc_oracle(quantity, op, threshold, params)


def test_the_answers_go_every_way_and_are_clear():
    answers = {cid: _answer(c)[1] for cid, c in CASES.items()}
    assert answers == {"RC1": "SUPPORTED", "RC2": "REFUTED",
                       "RC3": "REFUTED", "RC4": "REFUTED", "RC5": "REFUTED",
                       "RC6": "SUPPORTED", "RC7": "SUPPORTED",
                       "RC8": "SUPPORTED", "RC9": "REFUTED",
                       "RC10": "INCONCLUSIVE"}
    for cid, (_, quantity, op, threshold, changes) in CASES.items():
        if threshold is None:
            continue
        ref = REF.temperatures({**POINT, **changes})
        assert abs(ref[quantity] - threshold) - ref["error_K"] >= \
            E.RC_MARGIN_K, cid


#: (changed world, the base case asking the same question): each change
#: alone turns the base's answer over
FLIPS = (("RC3", "RC1"), ("RC4", "RC1"), ("RC5", "RC1"), ("RC6", "RC2"),
         ("RC7", "RC2"), ("RC9", "RC8"))


@pytest.mark.parametrize("changed, base", FLIPS)
def test_each_change_alone_turns_the_answer_over(changed, base):
    c, b = CASES[changed], CASES[base]
    assert c[1:4] == b[1:4] and b[4] == {} and len(set(c[4])) >= 1
    assert {_answer(c)[1], _answer(b)[1]} == {"SUPPORTED", "REFUTED"}


def test_the_worlds_cover_what_the_question_can_change():
    changed = set().union(*(c[4] for c in CASES.values()))
    assert {"Q_W", "C1_J_K", "C2_J_K", "G12_W_K", "G2a_W_K", "T1_0_K",
            "T2_0_K"} <= changed
    above = [c for c in CASES.values() if c[3] is not None
             and c[3] > REF.temperatures({**POINT, **c[4]})[c[1]]]
    below = [c for c in CASES.values() if c[3] is not None
             and c[3] < REF.temperatures({**POINT, **c[4]})[c[1]]]
    assert above and below
    assert {c[1] for c in CASES.values()} == {"T1", "T2"}
    assert {c[2] for c in CASES.values()} == {"<", ">"}


def test_a_case_too_close_to_call_is_refused():
    exact = REF.temperatures(POINT)["T1"]
    for threshold in (exact + 0.5e-6, exact - 0.5e-6, exact + 1e-9):
        with pytest.raises(ValueError, match="too close to call"):
            E.rc_oracle("T1", "<", threshold, POINT)


def test_a_case_the_reference_cannot_resolve_is_refused():
    """A stiff network -- time constants a billion apart, the heated node
    started 100 K off -- where the reference's own stated error is larger
    than the margin: what it cannot resolve is not judged, and no tie is
    called there either."""
    stiff = {**POINT, "C1_J_K": 1e-3, "C2_J_K": 1e6, "G12_W_K": 1.0,
             "G2a_W_K": 1.0, "T1_0_K": 400.0, "t_end_s": 1e5}
    ref = REF.temperatures(stiff)
    assert ref["error_K"] > 3 * E.RC_MARGIN_K, ref
    for threshold in (ref["T1"], ref["T1"] + 2 * E.RC_MARGIN_K):
        with pytest.raises(ValueError, match="too close to call"):
            E.rc_oracle("T1", "<", threshold, stiff)
    assert E.rc_oracle("T1", "<", ref["T1"] + 2 * ref["error_K"]
                       + E.RC_MARGIN_K, stiff)[1] == "SUPPORTED"


def test_the_tie_is_inconclusive_and_only_the_tie():
    exact = REF.temperatures(POINT)["T1"]
    assert E.rc_oracle("T1", "<", exact, POINT)[1] == "INCONCLUSIVE"
    assert E.rc_oracle("T1", ">", exact, POINT)[1] == "INCONCLUSIVE"
    assert E.rc_oracle("T1", "<", exact + 2e-6, POINT)[1] == "SUPPORTED"
    assert E.rc_oracle("T1", "<", exact - 2e-6, POINT)[1] == "REFUTED"


#: what no network is: (the change, what the reference says of it)
NO_NETWORK = (
    ({"C1_J_K": 0.0}, "greater than zero"),
    ({"C2_J_K": -2000.0}, "greater than zero"),
    ({"G12_W_K": 0.0}, "greater than zero"),
    ({"G2a_W_K": -2.0}, "greater than zero"),
    ({"T1_0_K": 0.0}, "greater than zero"),
    ({"Tamb_K": -300.0}, "greater than zero"),
    ({"t_end_s": -1.0}, "before the start"),
    ({"Q_W": math.nan}, "finite real number"),
    ({"Q_W": math.inf}, "finite real number"),
    ({"G12_W_K": True}, "finite real number"),
    ({"C1_J_K": "500"}, "finite real number"),
    ({"R_K_W": 1.0}, "unknown"),
)


@pytest.mark.parametrize("change, why", NO_NETWORK,
                         ids=[str(c) for c, _ in NO_NETWORK])
def test_a_world_that_is_no_network_has_no_answer(change, why):
    """The reference computes nothing for it and the judge says nothing of
    it; the model the loop would run refuses it too."""
    params = {**POINT, **change}
    with pytest.raises(ValueError, match=why):
        REF.temperatures(params)
    with pytest.raises(ValueError, match=why):
        E.rc_oracle("T1", "<", 306.8, params)
    with pytest.raises(Exception):
        MODEL.validate(params)


def test_a_missing_parameter_is_no_network_to_the_reference():
    """The model fills a missing parameter with its default; the reference
    answers only for a network every parameter of which is stated."""
    params = dict(POINT)
    del params["Q_W"]
    with pytest.raises(ValueError, match="missing"):
        REF.temperatures(params)


# ---- the reference ---------------------------------------------------------

def test_the_reference_imports_nothing_of_the_repository():
    tree = ast.parse((ROOT / "tools" / "rc2_reference.py").read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "json", "math", "sys"}, imported


@pytest.mark.parametrize("kind", KINDS)
def test_the_reference_keeps_the_rounding_bound_it_states(kind):
    rng = random.Random(f"rc2-reference-{kind}")
    for _ in range(250):
        p = _sample(rng, kind)
        ref, exact = REF.temperatures(p), _exact(p)
        for q in ("T1", "T2"):
            assert abs(ref[q] - exact[q]) <= ref["error_K"], (p, q)


@pytest.mark.parametrize("kind", KINDS)
def test_the_reference_and_the_model_agree_inside_both_errors(kind):
    rng = random.Random(f"rc2-agree-{kind}")
    for _ in range(250):
        p = _sample(rng, kind)
        ref = REF.temperatures(p)
        _, outs, _ = MODEL.evaluate_outputs(p)
        for o in outs[:2]:
            assert abs(ref[o.name] - o.quantity.value) <= \
                ref["error_K"] + o.quantity.resolution, (p, o.name)


def test_the_reference_starts_at_the_initial_state_and_ends_steady():
    p = {**POINT, "T1_0_K": 290.0, "T2_0_K": 310.0}
    at0 = REF.temperatures({**p, "t_end_s": 0.0})
    assert (at0["T1"], at0["T2"]) == (290.0, 310.0)
    late = REF.temperatures({**p, "t_end_s": 1e9})
    s2 = p["Tamb_K"] + p["Q_W"] / p["G2a_W_K"]
    assert late["T2"] == pytest.approx(s2, rel=1e-13)
    assert late["T1"] == pytest.approx(s2 + p["Q_W"] / p["G12_W_K"],
                                       rel=1e-13)


def test_the_reference_from_the_command_line():
    tool = [sys.executable, str(ROOT / "tools" / "rc2_reference.py")]
    out = subprocess.run([*tool, json.dumps(POINT)], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["T1"] == REF.temperatures(POINT)["T1"]
    bad = subprocess.run([*tool, json.dumps({**POINT, "C1_J_K": 0.0})],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and "refused" in bad.stderr
    assert subprocess.run(tool, capture_output=True, timeout=60
                          ).returncode == 2


# ---- the model (AUD-024) ---------------------------------------------------

def _within(p: dict) -> list:
    """The outputs at ``p`` further from the 90-digit value than the
    resolution the model states for them."""
    _, outs, invs = MODEL.evaluate_outputs(p)
    assert all(i.holds for i in invs), p
    exact = _exact(p)
    return [(o.name, abs(o.quantity.value - exact[o.name]),
             o.quantity.resolution) for o in outs
            if abs(o.quantity.value - exact[o.name]) > o.quantity.resolution]


@pytest.mark.parametrize("point", AUD024_POINTS)
def test_where_the_stated_resolution_was_missed_it_is_kept(point):
    assert _within(MODEL.validate(point)) == []


@pytest.mark.parametrize("kind", KINDS)
def test_every_output_is_within_its_stated_resolution(kind):
    rng = random.Random(f"rc2-model-{kind}")
    for _ in range(250):
        p = _sample(rng, kind)
        assert _within(p) == [], p


def test_the_resolution_is_never_below_its_floor():
    """The judge's tie rests on it: no temperature is stated to better than
    256 eps of its own size."""
    rng = random.Random("rc2-floor")
    for _ in range(200):
        _, outs, _ = MODEL.evaluate_outputs(_sample(rng, "anywhere"))
        for o in outs[:2]:
            assert o.quantity.resolution >= 256 * EPS * abs(o.quantity.value)


# ---- the independent check -------------------------------------------------

def test_the_check_is_admitted_and_declared():
    run, digest = catalog.check("thermal.rc2_rk4")
    assert run is K.run_check and digest() == K.check_digest()
    c = contracts.contract("thermal.rc2_network", "thermal.rc2_rk4")
    assert c["independent_check"]["independence"] == \
        "DIFFERENT_IMPLEMENTATION"


def test_the_check_shares_no_code_with_the_producer():
    assert K.check_digest() != MODEL.implementation_digest()
    tree = ast.parse((ROOT / "scientific/checks/rc2_rk4.py").read_text())
    modules = {n.module or "" for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom)}
    assert not any("models" in m for m in modules), modules


def test_the_check_takes_the_steps_it_states():
    """The step rule the check's docstring states: at least twenty steps in
    the network's fastest time constant, bounded by the largest absolute row
    sum of its matrix (Gershgorin), at least four hundred in all, and none
    past two million -- NOT_RUN there, never a pass. With four hundred at
    the least, the transient has decayed wherever the rule binds, so a
    blunter step would change no verdict here; this holds the rule itself
    to what the check says it is."""
    for change, want in (({}, 1440), ({"C1_J_K": 50.0}, 14_400),
                         ({"t_end_s": 1.0}, 400),
                         ({"C1_J_K": 1e-3, "t_end_s": 1e6}, None)):
        p = MODEL.validate({**POINT, **change})
        rate = max(2 * p["G12_W_K"] / p["C1_J_K"],
                   (2 * p["G12_W_K"] + p["G2a_W_K"]) / p["C2_J_K"])
        n = max(400, math.ceil(p["t_end_s"] * rate * 20))
        assert (n if n <= 2_000_000 else None) == want
        assert K.steps_for(p) == want, change


@pytest.mark.parametrize("kind", ("ordinary", "nearly defective"))
def test_the_check_keeps_the_error_it_states(kind):
    """The check's own answer against the 90-digit value, inside the error
    it states for it -- Richardson's estimate and the rounding bound, each
    a part of it: a check wrong in the producer's direction would pass
    it."""
    rng = random.Random(f"rc2-rk4-{kind}")
    held = 0
    while held < 40:
        p = _sample(rng, kind)
        n = K.steps_for(p)
        if n is None or n > 20_000:     # a stiff march costs seconds here
            continue
        own = K.solve(p)
        coarse, fine = K.march(p, n), K.march(p, 2 * n)
        held += 1
        exact = _exact(p)
        for i, (q, (value, err)) in enumerate(zip(("T1", "T2"), own)):
            assert value == fine[i]
            assert err >= abs(coarse[i] - fine[i]) / 15.0
            assert err >= 2 * (2 * n) * EPS * fine[2]
            assert abs(value - exact[q]) <= err, (p, q)


@pytest.mark.parametrize("cid", sorted(CASES, key=lambda c: int(c[2:])))
def test_the_check_passes_the_model_in_every_world(cid):
    r = K.run_check(MODEL.run({**POINT, **CASES[cid][4]}),
                    verifier_id="test")
    assert r.status is Status.PASS, r.measured
    assert r.measured.value <= 0.5


def _with(bundle, values: dict, resolution: float):
    outs = []
    for o in bundle.outputs:
        if o.name in values:
            o = dataclasses.replace(o, quantity=Quantity(
                values[o.name], "K", resolution=resolution,
                resolution_class=ResolutionClass.SINGLE_EVALUATION,
                resolution_basis="test producer", reporting_digits=12))
        outs.append(o)
    return dataclasses.replace(bundle, outputs=tuple(outs))


def test_the_check_passes_a_perfect_producer_and_fails_a_near_miss():
    """A producer answering the exact values, claiming almost no error,
    passes; the same values moved by a hundredth of a microkelvin fail --
    as they would only if the check's own error is as small as it says."""
    b = MODEL.run(POINT)
    exact = _exact(MODEL.validate(POINT))
    perfect = _with(b, {"T1": exact["T1"], "T2": exact["T2"]}, 1e-13)
    assert K.run_check(perfect, verifier_id="test").status is Status.PASS
    near = _with(b, {"T1": exact["T1"] + 1e-8, "T2": exact["T2"]}, 1e-13)
    assert K.run_check(near, verifier_id="test").status is Status.FAIL
    # the same miss, by a producer whose stated error covers it, passes:
    # the criterion is the sum of both stated errors, the producer's too
    owned = _with(b, {"T1": exact["T1"] + 1e-8, "T2": exact["T2"]}, 1e-8)
    assert K.run_check(owned, verifier_id="test").status is Status.PASS


def test_the_check_states_its_own_answer_as_its_agreement():
    """AUD-025: the RC check's agreement is its own march at each output it
    compares, with the error its criterion credits it -- three times its
    stated error, plus rounding -- which ``test_the_check_keeps_the_error_
    it_states`` holds against the 90-digit value."""
    r = K.run_check(MODEL.run(POINT), verifier_id="test")
    own = K.solve(MODEL.validate(POINT))
    got = dict(r.agreement)
    assert set(got) == set(K.OUTPUT_NAMES)
    for name, (value, err) in zip(K.OUTPUT_NAMES, own):
        assert (got[name].value, got[name].unit) == (value, "K")
        assert got[name].resolution == K.GCI_SAFETY * err \
            + 64 * EPS * abs(value)


def _understated():
    """AUD-025's producer: T1 5e-10 K off the exact value, stated to
    1e-13 K, and its independent check -- which PASSes it, its own error
    being larger than the understatement."""
    b = MODEL.run(POINT)
    exact = _exact(MODEL.validate(POINT))
    off = _with(b, {"T1": exact["T1"] + 5e-10, "T2": exact["T2"]}, 1e-13)
    report = K.run_check(off, verifier_id="test")
    pred = {"model_id": MODEL.model_id, "quantity": "T1", "op": "<",
            "value": exact["T1"] + 2.5e-10, "unit": "K"}
    return exact, off.to_record(), report, pred


def test_a_verdict_claims_no_more_than_the_check_verified():
    """AUD-025, at the RC network's documented point. The check PASSes a
    producer 5e-10 K off stating 1e-13 K; read with the producer's
    resolution, "T1 < exact + 2.5e-10 K" was decided REFUTED, and the world
    answers SUPPORTED. Read as the check's agreement supports -- |v_producer
    - v_check| + r_check -- the interval holds the exact value, and the
    ledger abstains."""
    exact, bundle, report, pred = _understated()
    assert report.status is Status.PASS and report.measured.value < 1.0
    (name, q), = [a for a in report.agreement if a[0] == "T1"]
    v_check, r_check = q.value, q.resolution
    assert abs(v_check - exact["T1"]) <= r_check       # the check is honest
    assert r_check > 5e-10 - 2.5e-10                   # and wider than the gap
    then = R.evaluate(pred, [{"model_id": MODEL.model_id,
                              "values": R.values_of(bundle)}])
    assert then[0] == "REFUTED"                        # the defect, as it was
    seen = R.observed_values(bundle, report.to_record())
    t1 = seen["T1"]
    assert t1["resolution"] == abs(t1["value"] - v_check) + r_check
    assert abs(t1["value"] - exact["T1"]) <= t1["resolution"]
    now = R.evaluate(pred, [{"model_id": MODEL.model_id, "values": seen}])
    assert now[0] == "INCONCLUSIVE", now


def test_only_what_the_check_compared_is_observed():
    """The check compares T1 and T2; the energies nobody compared are not
    observed, and no compared output is observed more precisely than its
    producer stated."""
    _, bundle, report, _ = _understated()
    seen = R.observed_values(bundle, report.to_record())
    assert set(seen) == {n for n, _ in report.agreement} == {"T1", "T2"}
    own = R.values_of(bundle)
    for n, v in seen.items():
        assert v["value"] == own[n]["value"] and v["unit"] == own[n]["unit"]
        assert v["resolution"] >= own[n]["resolution"]


def test_the_observation_is_never_more_precise_than_its_producer_says():
    """Where the producer's own stated error is the wider -- here 1e-6 K on
    the exact values, a thousand times the check's -- the observation keeps
    it: the agreement bounds the producer's value, it never narrows what
    the producer claimed."""
    b = MODEL.run(POINT)
    exact = _exact(MODEL.validate(POINT))
    modest = _with(b, {"T1": exact["T1"], "T2": exact["T2"]}, 1e-6)
    report = K.run_check(modest, verifier_id="test")
    assert report.status is Status.PASS
    seen = R.observed_values(modest.to_record(), report.to_record())
    for n, q in report.agreement:
        assert abs(seen[n]["value"] - q.value) + q.resolution < 1e-6
        assert seen[n]["resolution"] == 1e-6


def test_a_report_written_before_the_agreement_is_read_as_it_was():
    """A report with no agreement -- every report decided under
    admission/1 -- leaves the producer's values as that history recorded
    them: the old verdict replays as it was decided, not reinterpreted."""
    _, bundle, report, pred = _understated()
    old = {k: v for k, v in report.to_record().items() if k != "agreement"}
    assert R.observed_values(bundle, old) == R.values_of(bundle)
    assert R.observed_values(bundle, None) == R.values_of(bundle)
    assert R.evaluate(pred, [{"model_id": MODEL.model_id, "values":
                              R.observed_values(bundle, old)}])[0] == \
        "REFUTED"


@pytest.mark.parametrize("entry", [
    "T1", ["T1"], ["T1", {"value": float("nan"), "resolution": 1.0}],
    ["T1", {"value": 300.0, "resolution": -1.0}],
    ["T1", {"value": 300.0, "resolution": float("inf")}],
    ["T1", {"value": True, "resolution": 1.0}],
    ["T1", "300 K"],
    ["T1", {"value": 10 ** 400, "resolution": 1.0}],
    ["T1", {"value": 300.0, "resolution": 10 ** 400}],
])
def test_an_agreement_entry_it_cannot_read_widens_nothing_and_observes_nothing(
        entry):
    """The ledger never narrows on what it cannot read: an unreadable entry
    is no agreement, so its output is not observed (the store refuses such
    a report under admission/2; this reader stays safe without the store)."""
    _, bundle, report, _ = _understated()
    rec = report.to_record()
    rec["agreement"] = [e for e in rec["agreement"] if e[0] != "T1"] + [entry]
    seen = R.observed_values(bundle, rec)
    assert "T1" not in seen and set(seen) == {"T2"}


def test_a_network_too_stiff_or_a_failed_output_is_not_run():
    stiff = MODEL.run({**POINT, "C1_J_K": 1e-3, "t_end_s": 1e6})
    r = K.run_check(stiff, verifier_id="test")
    assert r.status is Status.NOT_RUN and "too stiff" in r.limitations[-1]
    failed = dataclasses.replace(MODEL.run(POINT), outputs=tuple(
        dataclasses.replace(o, status=type(o.status).FAILED, quantity=None,
                            reason="test")
        if o.name == "T2" else o for o in MODEL.run(POINT).outputs))
    r = K.run_check(failed, verifier_id="test")
    assert r.status is Status.NOT_RUN and "not OK" in r.limitations[-1]


def test_the_check_refuses_another_model():
    from scientific.models.slab_transient import SlabTransientModel
    b = SlabTransientModel().run(E.SLAB_RUN["parameters"])
    with pytest.raises(ValueError, match="this check is for"):
        K.run_check(b, verifier_id="test")


# ---- the family, through the loop ------------------------------------------

@pytest.mark.parametrize("cid", sorted(CASES, key=lambda c: int(c[2:])))
def test_each_case_is_answered_as_its_world_answers(cid):
    out = E.run_rc_counterfactual(CASES[cid])
    assert out["error"] is None, out
    assert out["verdict"] == out["oracle"], out
    assert out["status"] == ("ABSTAINED" if out["oracle"] == "INCONCLUSIVE"
                             else "CONCLUDED"), out
    assert out["task_succeeded"], out


def _ask(name: str, params: dict, checks) -> "E.World":
    hyp = E.rc_hypothesis(threshold=306.8)
    return E.World(name, L.scripted_agent(
        hypothesis=hyp, run={**E.RC2_RUN, "parameters": params}),
        question={"prediction": hyp["prediction"], "parameters": params},
        checks=checks)


def test_a_world_that_is_no_network_concludes_nothing():
    """Asked at a point with no heat capacity, the loop's run is refused by
    the model, nothing is observed, and nothing is concluded."""
    w = _ask("rc-no-network", {**POINT, "C1_J_K": 0.0}, E.RC_CHECKS)
    try:
        with pytest.raises(Exception, match="FAILED"):
            w.controller().run(E.OBJECTIVE)
        led = w.ledger()
        assert led.get(E.OBJECTIVE).status == "OPEN"
        assert led.of(E.OBJECTIVE, "observation") == ()
        assert led.get("h1").status == "OPEN"
    finally:
        w.clean()


def test_without_the_deployment_s_table_the_network_is_not_run():
    w = _ask("rc-no-table", dict(POINT), None)
    try:
        report = w.controller().run(E.OBJECTIVE)
        refused = [s for s in report.steps if s.action == "MODEL_RUN"]
        assert refused and all(s.outcome == "REFUSED" for s in refused)
        assert "no independent check" in refused[0].detail
        assert report.status != "CONCLUDED"
        assert not [e for e in w.events() if e.action == "task.create"
                    and "rc2" in json.dumps(e.payload)]
    finally:
        w.clean()


# ---- the controls ----------------------------------------------------------

#: stamp -> the cases it fails: exactly those whose world answers otherwise
FAILS = {
    "SUPPORTED": ["RC10", "RC2", "RC3", "RC4", "RC5", "RC9"],
    "REFUTED": ["RC1", "RC10", "RC6", "RC7", "RC8"],
    "INCONCLUSIVE": ["RC1", "RC2", "RC3", "RC4", "RC5", "RC6", "RC7",
                     "RC8", "RC9"],
}


@pytest.mark.parametrize("stamp", E.STAMPS)
def test_each_wrong_verdict_rule_fails_the_family(monkeypatch, stamp):
    monkeypatch.setattr(R, "evaluate", E.stamped(stamp, R.evaluate))
    failed = sorted(cid for cid, case in CASES.items()
                    if not E.run_rc_counterfactual(case)["task_succeeded"])
    assert failed == FAILS[stamp]
