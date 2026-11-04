"""Counterfactual cases of the offline evaluation (Phase D).

The evaluation's task has one answer, SUPPORTED. A system that concluded
SUPPORTED whatever its evidence said would pass it every time, and so would
an evaluation whose oracle could not be anything else. Each counterfactual
case asks the owner's question of another world and judges the loop against
that world's answer, which comes from the slab's closed-form solution, not
from the model the loop runs:

* every case is concluded as its world answers, two of them REFUTED;
* the answers are clear: none is within the margin of its threshold, and a
  case that would be is refused;
* a rubber stamp -- a verdict rule that calls every decided hypothesis
  SUPPORTED -- still passes the evaluation's task, and fails exactly the
  two cases whose answer is REFUTED;
* its mirror, a rule that calls every hypothesis REFUTED, and a rule that
  never decides (Stage 3, ``adversarial_eval.STAMPS``) each fail the task
  and exactly the cases whose answer differs -- as each fails the RC
  network's family (``tests/test_rc_counterfactuals.py``).

The loop is scripted: no language model is run, and nothing here says how
a model would answer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_substrate import research as R  # noqa: E402
from tools import adversarial_eval as E  # noqa: E402

POINT = E.SLAB_RUN["parameters"]


@pytest.mark.parametrize("case", E.COUNTERFACTUALS,
                         ids=[c[0] for c in E.COUNTERFACTUALS])
def test_each_case_is_concluded_as_its_world_answers(case):
    out = E.run_counterfactual(case)
    assert out["error"] is None
    assert out["status"] == "CONCLUDED"
    assert out["verdict"] == out["oracle"], out
    assert out["task_succeeded"]


def test_the_answers_go_both_ways_and_are_clear():
    answers = []
    for _, threshold, changes in E.COUNTERFACTUALS:
        exact, oracle = E.oracle_of(threshold, {**POINT, **changes})
        assert abs(exact - threshold) >= E.MARGIN_K
        answers.append(oracle)
    assert sorted(answers) == ["REFUTED", "REFUTED", "SUPPORTED",
                               "SUPPORTED"]


def test_the_oracle_is_the_closed_form_the_package_is_checked_by():
    """At the documented point the closed form gives what the slab's
    recorded verification says it gives, and the task's own answer."""
    uq = json.loads((ROOT / "docs" / "assurance" / "slab_uq.json")
                    .read_text())
    exact, oracle = E.oracle_of(1000.0, POINT)
    assert oracle == E.ORACLE
    assert abs(exact - uq["reference"]["value"]) < 1e-9


def test_a_case_too_close_to_call_is_refused():
    exact, _ = E.oracle_of(1000.0, POINT)
    for threshold in (exact + 0.5, exact - 0.5):
        with pytest.raises(ValueError, match="too close to call"):
            E.oracle_of(threshold, POINT)


def test_a_rubber_stamp_passes_the_task_and_fails_the_counterfactuals(
        monkeypatch):
    """The positive control. A verdict rule that calls every decided
    hypothesis SUPPORTED is wrong, and the evaluation's single task cannot
    tell: its answer is SUPPORTED. The counterfactual cases can, and they
    fail exactly where the world's answer is REFUTED."""
    real = R.evaluate

    def stamp(prediction, observations):
        status, reason = real(prediction, observations)
        if status == "REFUTED":
            status = "SUPPORTED"
        return status, reason

    monkeypatch.setattr(R, "evaluate", stamp)
    assert E.run_one(None, "benign")["task_succeeded"], \
        "the task alone does not see the rubber stamp"
    failed = [case[0] for case in E.COUNTERFACTUALS
              if not E.run_counterfactual(case)["task_succeeded"]]
    assert failed == ["CF1", "CF3"]


#: the other two wrong verdict rules -> (does the evaluation's own task
#: succeed under it, the cases it fails); the rubber stamp is the test above
STAMP_FAILS = {"REFUTED": (False, ["CF2", "CF4"]),
               "INCONCLUSIVE": (False, ["CF1", "CF2", "CF3", "CF4"])}


@pytest.mark.parametrize("stamp", sorted(STAMP_FAILS))
def test_the_other_wrong_verdict_rules_fail_the_task_and_the_cases(
        monkeypatch, stamp):
    monkeypatch.setattr(R, "evaluate", E.stamped(stamp, R.evaluate))
    task, fails = STAMP_FAILS[stamp]
    assert E.run_one(None, "benign")["task_succeeded"] is task
    failed = [case[0] for case in E.COUNTERFACTUALS
              if not E.run_counterfactual(case)["task_succeeded"]]
    assert failed == fails
