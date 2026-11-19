"""The slab's mid-plane temperature with a transparent error budget
(scientific.uncertainty, tools/slab_uq.py).

The checks:

- the budget is recorded as the tool derives it;
- every contribution says what it rests on, and what nothing establishes is
  named and never summed;
- the model's discretization estimate is held to a measurement against the
  closed-form series, and the record keeps the verdict, here that the
  estimate falls short by a few parts in 1e5;
- two propagations that share no step are compared at the Monte Carlo
  standard error.

A negative control stands beside each rule: a response that is not linear
must make the propagations disagree, and a reference that disagrees must
leave the estimate uncovered.

Everything is MODEL_ONLY: no temperature of any real slab was observed.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import slab_uq as Q  # noqa: E402

from scientific import uncertainty as U  # noqa: E402
from scientific.checks.slab_series import series_temperature  # noqa: E402


@pytest.fixture(scope="module")
def derived():
    return Q.derive()


def _close(a, b, path=""):
    """Equal, floats to 1e-9 relative: the same arithmetic on another BLAS
    may differ in its last bits, and nothing beyond that."""
    if isinstance(a, dict):
        assert set(a) == set(b), path
        for k in a:
            _close(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _close(x, y, f"{path}[{i}]")
    elif isinstance(a, float) and isinstance(b, float):
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), (path, a, b)
    else:
        assert a == b, (path, a, b)


def test_the_recorded_budget_is_what_the_tool_derives(derived):
    recorded = json.loads(Q.RESULTS.read_text(encoding="utf-8"))
    _close(json.loads(json.dumps(derived)), recorded)


def test_every_contribution_says_what_it_rests_on(derived):
    b = derived["budget"]
    bases = {c["name"]: c["basis"] for c in b["contributions"]}
    assert set(bases.values()) <= set(U.BASES)
    assert bases["model_form"] == bases["measurement"] == U.NOT_ESTABLISHED
    for c in b["contributions"]:
        assert (c["half_width"] is None) == (c["basis"] == U.NOT_ESTABLISHED)
    t = b["total"]
    assert t["excludes_not_established"] == ["model_form", "measurement"]
    assert "model_form" not in t["includes"]
    assert "not established: model_form, measurement" in b["statement"]
    assert derived["authority"] == "NON_AUTHORITATIVE"
    assert derived["status"].startswith("MODEL_ONLY")


def test_the_measurement_replaces_the_estimate_and_holds_it_to_account(
        derived):
    t = derived["budget"]["total"]
    assert t["replaced_by_a_measurement"] == ["discretization"]
    assert "discretization_against_the_series" in t["includes"]
    cov = derived["estimate_covers_measured"]
    reference = series_temperature(Q.POINT, [0.5 * Q.POINT["L_m"]])[0]
    assert math.isclose(cov["measured"],
                        abs(derived["budget"]["value"] - reference),
                        rel_tol=1e-9)
    # The record says what was found, not what was hoped. Richardson's
    # estimate is an estimate: here it falls short of the measured error by
    # a few parts in 1e5, and the verdict says so.
    assert cov["verdict"] == U.estimate_bounds(cov["estimate"],
                                               cov["measured"])["verdict"]
    assert abs(cov["ratio"] - 1.0) < 1e-3


def test_a_reference_that_disagrees_leaves_the_estimate_uncovered(derived):
    """The negative control: the reference at a conductivity 2% off is a
    solution of another problem, and the check must see it."""
    wrong = series_temperature({**Q.POINT,
                                "k_W_m_K": 1.02 * Q.POINT["k_W_m_K"]},
                               [0.5 * Q.POINT["L_m"]])[0]
    est = derived["estimate_covers_measured"]["estimate"]
    v = U.estimate_bounds(est, abs(derived["budget"]["value"] - wrong))
    assert v["verdict"] == "ESTIMATE_DOES_NOT_COVER" and v["ratio"] > 10


def test_the_two_propagations_agree_here_at_the_sampling_error(derived):
    a = derived["propagation"]["agreement"]
    assert a["verdict"] == "AGREE_WITHIN_SAMPLING"
    assert a["gap"] <= U.AGREEMENT_SE * a["monte_carlo_standard_error"]
    lin = derived["propagation"]["linear"]
    assert math.isclose(sum(r["share"] for r in lin["rows"]), 1.0,
                        rel_tol=1e-12)


def test_a_response_that_is_not_linear_makes_them_disagree():
    """The negative control: over a spread this wide, exp is far from
    linear, and the comparison must say so rather than agree."""
    def f(p):
        return math.exp(3.0 * p["x"])
    inputs = {"x": U.Distribution(0.4)}
    lin = U.linear(f, {"x": 0.0}, inputs)
    mc = U.monte_carlo(f, {"x": 0.0}, inputs, n=2000, seed=7)
    assert U.compare(lin, mc)["verdict"] == "DIFFER"


@pytest.mark.parametrize("kwargs, match", [
    (dict(basis="GUESSED"), "basis"),
    (dict(uncertainty_class="VIBES"), "class"),
    (dict(basis=U.NOT_ESTABLISHED, half_width=1.0), "established"),
    (dict(basis=U.MEASURED, half_width=None), "established"),
    (dict(half_width=-1.0), "half-width"),
    (dict(half_width=math.inf), "half-width"),
    (dict(basis=U.ESTIMATED, supersedes="x"), "only a measurement"),
])
def test_a_contribution_that_does_not_say_what_it_is_is_refused(kwargs,
                                                                 match):
    args = dict(name="c", uncertainty_class="NUMERICAL", basis=U.MEASURED,
                half_width=0.1, method="m")
    args.update(kwargs)
    with pytest.raises(U.BudgetRefused, match=match):
        U.Contribution(**args)


def test_a_measurement_replaces_only_an_estimate_in_the_budget():
    b = U.Budget(model="m", output="y", unit="K", value=1.0)
    b.add(U.Contribution("p", "PARAMETRIC", U.ASSUMED, 0.5, "declared"))
    with pytest.raises(U.BudgetRefused, match="no estimate"):
        b.add(U.Contribution("d", "NUMERICAL", U.MEASURED, 0.1, "ref",
                             supersedes="p"))
    with pytest.raises(U.BudgetRefused, match="already"):
        b.add(U.Contribution("p", "PARAMETRIC", U.ASSUMED, 0.5, "again"))


def test_the_check_refuses_a_tampered_record(tmp_path, monkeypatch, capsys):
    """CI runs ``--check``. A record whose stated interval was edited by
    hand, here narrowed by 0.1 K, is refused, and the difference named."""
    rec = json.loads(Q.RESULTS.read_text(encoding="utf-8"))
    rec["budget"]["total"]["half_width"] -= 0.1
    forged = tmp_path / "slab_uq.json"
    forged.write_text(json.dumps(rec), encoding="utf-8")
    monkeypatch.setattr(Q, "RESULTS", forged)
    monkeypatch.setattr(Q, "ROOT", tmp_path)
    assert Q.main(["--check"]) == 1
    assert ".budget.total.half_width" in capsys.readouterr().out
