"""Gate E: coordination of several agents on the existing substrate.

One coordinated world and one matched-budget comparison
(``tools/multi_agent_research.py``), and what each must show:

* every attempt of the adversary is refused by the mechanism that holds
  it: the assignment rule (controller and ledger), the budget's caller
  list, the directory's roles;
* a resent assignment is one message;
* after a cancellation nothing is dispatched or executed for the cancelled
  agent;
* two researchers' contradictory hypotheses are decided by the verified
  observation. The agents' claims are recorded and resolving them changes
  no record, and agents sharing one model are one lineage, whose quorum is
  not independent;
* withdrawing one agent's task leaves a result whose identical bytes other
  verified tasks produced; withdrawing the bytes makes stale every agent's
  conclusion resting on them;
* on one total budget, two agents conclude what one does, with nothing
  added but the coordination's messages.

The agents are scripts; nothing here says how a team of models behaves.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import multi_agent_research as M  # noqa: E402


@pytest.fixture(scope="module")
def team():
    return M.coordinated()


def test_every_adversary_attempt_is_refused_by_its_mechanism(team):
    adv = team["adversary"]
    assert "not assigned to 'a-open'" in \
        adv["steps an objective it is not assigned"]
    assert "who is not assigned" in \
        adv["records a hypothesis on another's objective"]
    assert "CALLER_NOT_ALLOWED" in adv["spends another agent's budget"]
    assert "may not act as REVIEWER" in adv["claims as a reviewer"]


def test_a_resent_assignment_is_one_message(team):
    assert team["duplicate_suppression"]["resent_is_the_same_message"]


def test_nothing_runs_for_a_cancelled_agent(team):
    c = team["cancellation"]
    assert (c["dispatched_after"], c["executed_after"]) == (0, 0)
    assert c["later"] != "CONCLUDED"


def test_a_disagreement_is_decided_by_the_evidence_not_the_agents(team):
    d = team["disagreement"]
    assert (d["a"], d["b"]) == ("SUPPORTED", "REFUTED")
    assert d["concluded_on"] == ["shared.a"]
    claims = team["claims"]
    assert not claims["resolved"]
    assert claims["records_changed_by_resolution"] == 0


def test_shared_lineage_is_one_opinion(team):
    claims = team["claims"]
    assert claims["independent_lineages"] == 1
    assert not claims["a_quorum_of_two_would_be_independent"]


def test_invalidation_follows_the_bytes_across_agents(team):
    x = team["cross_agent_invalidation"]
    assert x["task_withdrawn"]["affected"] == []          # bytes still held
    assert x["task_withdrawn"]["shared"] == "stands"
    stale = x["bytes_withdrawn"]
    assert set(stale["objectives_stale"]) >= {"own-0", "own-2", "shared"}
    assert stale["agents_whose_conclusions_went_stale"] == [M.RA, M.RB]


def test_the_second_reader_agrees(team):
    assert team["second_reader_anomalies"] == 0


def test_lineage_and_quorum_count_models_not_instances():
    from types import SimpleNamespace as N
    claims = [N(value_digest="v", by_instance="a"),
              N(value_digest="v", by_instance="b")]
    assert not M.independent_quorum(claims, {"a": ["m1"], "b": ["m1"]}, 2)
    assert M.independent_quorum(claims, {"a": ["m1"], "b": ["m2"]}, 2)


def test_two_agents_conclude_what_one_does_on_the_same_budget():
    m = M.matched()
    one, two = m["one_agent"], m["two_agents"]
    assert one["concluded"] == two["concluded"] == 6
    assert one["inference_calls"] == two["inference_calls"]
    assert one["governed_operations"] == two["governed_operations"]
    assert sum(one["budget_calls"].values()) == \
        sum(two["budget_calls"].values())
