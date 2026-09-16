"""The bounded model checker, and whether it can see the things it refuses.

WHY THIS FILE EXISTS SEPARATELY FROM THE CHECKER

A checker that reports "every invariant holds" over a graph it failed to
derive says the same words as one that checked everything. Every invariant
in tools/model_check.py has the form "no path does X", and an empty graph
satisfies all of them -- so the interesting question is not whether it
passes today but whether it CAN fail.

Each test below breaks one thing and requires the checker to name it.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOL = ROOT / "tools" / "model_check.py"

from qta_agent import authority as A  # noqa: E402
from qta_agent import scheduler as S  # noqa: E402


def _checker():
    spec = importlib.util.spec_from_file_location("model_check", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mc():
    return _checker()


def test_the_real_machines_satisfy_every_invariant():
    out = subprocess.run([sys.executable, str(TOOL)], cwd=str(ROOT),
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout
    assert "every path invariant holds" in out.stdout


def test_the_derived_graph_is_the_real_one(mc):
    """Derived by interrogation, not by re-description.

    If this ever stops matching the declared table, the checker has drifted
    into being a model OF the system rather than a reading of it -- which is
    the usual way a proof ends up about a program nobody runs.
    """
    graph = mc.authority_graph()
    declared = {(e.src, e.dst) for e in A.EDGES}
    assert set(graph) == declared, (
        f"derived {sorted((a.value, b.value) for a, b in set(graph) ^ declared)}"
        " differs from the declared edge table")

    sched = mc.scheduler_graph()
    assert set(sched) == {(e.src, e.dst) for e in S.EDGES}


def test_the_interrogation_covers_every_evidence_subset(mc):
    """The I6 invariants are 'no permitted way in lacks this key'.

    That is only an exhaustive statement if every subset was tried, and the
    subsets come from the edges themselves -- so an edge naming a new key
    widens the search automatically.
    """
    assert set(mc.EVIDENCE_KEYS) == {
        k for e in A.EDGES for k in e.requires_evidence}
    assert len(mc.EVIDENCE_KEYS) >= 4


def test_a_direct_edge_into_promoted_is_caught(mc):
    """I1 at the edge level: PROMOTED is entered from VERIFIED, and from
    nowhere else."""
    graph = mc.authority_graph()
    graph[(A.State.UNDER_REVIEW, A.State.PROMOTED)] = [
        {"role": A.Role.PROMOTER, "same_actor": False,
         "evidence": frozenset({"verification_report", "policy_id"})}]
    problems = mc.check_authority(graph)
    assert any("I1" in p for p in problems), problems


def test_a_route_to_promotion_that_skips_review_is_caught(mc):
    """The part no edge check can see, and the reason the search is over
    paths.

    A STALE -> VERIFIED edge satisfies every edge-level rule: PROMOTED is
    still entered only from VERIFIED, terminality is intact, the evidence is
    there. What it does is let a record that was invalidated become
    canonical again without anybody looking at it, and only a path notices.
    """
    graph = mc.authority_graph()
    graph[(A.State.PROPOSED, A.State.VERIFIED)] = [
        {"role": A.Role.VERIFIER, "same_actor": False,
         "evidence": frozenset({"verification_report"})}]
    problems = mc.check_authority(graph)
    assert any("without ever being UNDER_REVIEW" in p
               for p in problems), problems
    assert not any(p.startswith("I1:") for p in problems), (
        "this must be caught by the PATH check and not by an edge check, or "
        "it is not testing what it says: " + str(problems))


def test_an_edge_out_of_a_terminal_state_is_caught(mc):
    """I2 stops a revoked record being brought back."""
    graph = mc.authority_graph()
    graph[(A.State.REVOKED, A.State.UNDER_REVIEW)] = [
        {"role": A.Role.VERIFIER, "same_actor": False,
         "evidence": frozenset()}]
    problems = mc.check_authority(graph)
    assert any("I2" in p and "REVOKED" in p for p in problems), problems


def test_the_interrogation_tries_more_than_one_evidence_subset(mc):
    """What makes I6 exhaustive rather than a spot check.

    "No permitted way into VERIFIED lacks a verification_report" is only a
    statement about every way if every way was tried. An interrogation that
    offered the full key set and nothing else would derive exactly one way
    per edge, all of them carrying everything, and the I6 checks would pass
    over a question nobody asked.
    """
    graph = mc.authority_graph()
    ways = graph[(A.State.PROPOSED, A.State.UNDER_REVIEW)]
    sets = {w["evidence"] for w in ways}
    assert len(sets) > 4, (
        f"this edge requires no evidence and should be permitted under many "
        f"subsets; only {len(sets)} were tried")
    assert frozenset() in sets, (
        "the empty subset is the one that answers 'is this permitted with "
        "no evidence at all', which is the question I6 exists to ask")


def test_a_route_around_a_terminal_state_is_caught(mc):
    """Edge-level terminality is not enough: an edge table can forbid
    CANCELLED -> SUCCEEDED and still leave a way round through READY."""
    graph = mc.scheduler_graph()
    graph[(S.JobState.CANCELLED, S.JobState.READY)] = [{}]
    problems = mc.check_scheduler(graph)
    assert any("cancelled job reaches SUCCEEDED" in p for p in problems), \
        problems


def test_a_transition_the_proposer_may_perform_is_caught(mc):
    graph = mc.authority_graph()
    graph[(A.State.UNDER_REVIEW, A.State.VERIFIED)].append(
        {"role": A.Role.VERIFIER, "same_actor": True,
         "evidence": frozenset({"verification_report"})})
    problems = mc.check_authority(graph)
    assert any("I4" in p and "own proposer" in p for p in problems), problems


def test_a_verification_with_no_report_is_caught(mc):
    graph = mc.authority_graph()
    graph[(A.State.UNDER_REVIEW, A.State.VERIFIED)].append(
        {"role": A.Role.VERIFIER, "same_actor": False,
         "evidence": frozenset()})
    problems = mc.check_authority(graph)
    assert any("I6" in p for p in problems), problems


def test_an_unreachable_state_is_caught(mc):
    """A state nobody can reach is a state whose rules were never
    exercised, and one somebody added and nothing connected."""
    graph = {k: v for k, v in mc.authority_graph().items()
             if k[1] is not A.State.SUPERSEDED}
    problems = mc.check_authority(graph)
    assert any("unreachable" in p and "SUPERSEDED" in p
               for p in problems), problems


def test_an_empty_graph_is_a_failure_and_not_a_clean_sheet():
    """THE anti-vacuity check.

    Every invariant is "no path does X", and no path does anything in an
    empty graph. A derivation that stopped reaching the gate would otherwise
    report the best result in the file.
    """
    src = TOOL.read_text(encoding="utf-8")
    assert "would otherwise report a clean sheet" in src
    assert "if len(auth) < len(A.EDGES):" in src
    assert "if len(sched) < len(S.EDGES):" in src


def test_the_path_search_refuses_a_truncated_answer(mc):
    """A search that hit its bound must say so rather than report the paths
    it happened to find: 'no violations' from a truncated search is a
    statement about the search."""
    succ = mc.successors(mc.authority_graph())
    with pytest.raises(AssertionError, match="bound"):
        mc.paths_into(succ, A.INITIAL, A.State.PROMOTED, bound=1)
