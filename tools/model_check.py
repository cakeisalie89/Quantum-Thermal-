#!/usr/bin/env python3
"""Bounded EXHAUSTIVE checking of the two state machines' path invariants.

WHAT THIS IS, AND WHAT THE PROPERTY TESTS ARE NOT

tests/test_agent_machine_properties.py drives these machines with Hypothesis.
That explores; it does not exhaust, and the row has always said so. A
property that survives ten thousand generated histories is evidence the
invariant is not trivially violable and is not a statement about every
history.

This is the other kind. The state space of both machines is small enough to
enumerate completely, so the questions below are answered over EVERY state
and EVERY path within the bound, and the answer is a fact rather than a
sample.

WHY THE MODEL IS NOT A MODEL

The usual failure of model checking a real system is that somebody
re-describes the system in the checker's language, the description drifts,
and afterwards the proof is about a program nobody runs. There is no
re-description here. The permitted-transition graph is DERIVED by calling
the real authorization functions -- ``qta_agent.authority.check`` and
``qta_agent.scheduler.check_edge`` -- over every combination of source,
destination, role, actor identity and evidence subset in a bounded space.
Whatever those functions permit is what the graph contains, so a change to
either is a change to the graph, and there is nothing to keep in step.

WHAT IS BOUNDED, STATED PLAINLY

Evidence is enumerated over SUBSETS OF THE KEYS THE EDGES NAME, not over
arbitrary dictionaries; actors are enumerated as "is the proposer" and "is
not"; the scheduler's arithmetic (attempts, backoff, lease expiry) is
carried by its reducer rather than its edge table and is not part of this
graph. Those are the limits, and they are why the row calls this a bounded
model check and not a proof.

USAGE

    python3 tools/model_check.py           # check, report, exit non-zero on
                                           # any violated invariant
    python3 tools/model_check.py --dump    # print the derived graph
"""
from __future__ import annotations

import argparse
import itertools
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import authority as A          # noqa: E402
from qta_agent import scheduler as S          # noqa: E402

#: A digest-shaped value, so I6's syntactic check passes and the question
#: being asked is about the RULES rather than about string shapes.
DIGEST = "a" * 64

#: Evidence keys any edge in the authority table names. Enumerated as
#: subsets, which is what makes "no transition into VERIFIED without a
#: verification report" an exhaustive answer rather than a spot check.
EVIDENCE_KEYS = tuple(sorted(
    {k for e in A.EDGES for k in e.requires_evidence}))


def authority_graph() -> dict:
    """(src, dst) -> the conditions under which the real gate permits it.

    Every combination is tried. A pair absent from the result is one no
    combination in the bounded space could get past ``check``.
    """
    permitted: dict = {}
    for src, dst in itertools.product(A.State, A.State):
        if src is dst:
            continue
        for role in A.Role:
            for same_actor in (True, False):
                for r in range(len(EVIDENCE_KEYS) + 1):
                    for keys in itertools.combinations(EVIDENCE_KEYS, r):
                        req = A.TransitionRequest(
                            record_id="m", src=src, dst=dst,
                            actor="p" if same_actor else "v", role=role,
                            evidence={k: DIGEST for k in keys},
                            proposer="p", policy_id="pol@1")
                        try:
                            A.check(req)
                        except Exception:      # noqa: BLE001 - a refusal
                            continue
                        permitted.setdefault((src, dst), []).append(
                            {"role": role, "same_actor": same_actor,
                             "evidence": frozenset(keys)})
    return permitted


def scheduler_graph() -> dict:
    """(src, dst) -> permitted, from the real edge check."""
    permitted: dict = {}
    for src, dst in itertools.product(S.JobState, S.JobState):
        if src is dst:
            continue
        try:
            S.check_edge(src, dst, "j")
        except Exception:                      # noqa: BLE001 - a refusal
            continue
        permitted.setdefault((src, dst), []).append({})
    return permitted


def successors(graph: dict) -> dict:
    out: dict = {}
    for (src, dst) in graph:
        out.setdefault(src, set()).add(dst)
    return out


def reachable(succ: dict, start) -> set:
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in succ.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def paths_into(succ: dict, start, target, *, bound: int = 12) -> list:
    """Every simple path from ``start`` to ``target``.

    Simple paths only: a cycle adds nothing to the questions asked here,
    because every invariant below is about which states a path VISITS. The
    bound is a guard against a future table making that untrue, and it is
    reported rather than assumed -- a truncated search that quietly returned
    fewer paths would answer 'no violations' for the wrong reason.
    """
    found, truncated = [], False
    stack = [(start, [start])]
    while stack:
        node, path = stack.pop()
        if len(path) > bound:
            truncated = True
            continue
        for nxt in sorted(succ.get(node, ()), key=lambda s: s.value):
            if nxt in path:
                continue
            if nxt is target:
                found.append(path + [nxt])
            else:
                stack.append((nxt, path + [nxt]))
    if truncated:
        raise AssertionError(
            f"the path search hit its {bound}-state bound; the answer below "
            "would be about a truncated search rather than the machine")
    return found


def check_authority(graph: dict) -> list:
    """Every invariant the authority machine is supposed to hold."""
    problems = []
    succ = successors(graph)

    # I1: PROMOTED is entered from VERIFIED and from nowhere else.
    into_promoted = {src for (src, dst) in graph
                     if dst is A.State.PROMOTED}
    if into_promoted != {A.State.VERIFIED}:
        names = sorted(s.value for s in into_promoted)
        problems.append(
            f"I1: PROMOTED is reachable from {names}, and must be "
            "reachable only from VERIFIED")

    # AND THE PART THE EDGE CHECK CANNOT SEE.
    #
    # "PROMOTED is entered only from VERIFIED" is a statement about one
    # edge. It says nothing about how VERIFIED was reached, and review is
    # what VERIFIED is supposed to MEAN: an edge straight from STALE to
    # VERIFIED would satisfy every edge-level check above while letting a
    # record that was invalidated become canonical again without anybody
    # looking at it. This is that claim, and it is why the search is over
    # paths rather than pairs.
    #
    # The first version of this loop re-asserted the edge claim over paths
    # and was therefore redundant -- a mutation deleting it survived, which
    # is the harness saying exactly that.
    for path in paths_into(succ, A.INITIAL, A.State.PROMOTED):
        if A.State.UNDER_REVIEW not in path:
            problems.append(
                "a path reaches PROMOTED without ever being UNDER_REVIEW: "
                + " -> ".join(s.value for s in path))
            continue
        if A.State.VERIFIED not in path:
            problems.append(
                "a path reaches PROMOTED without VERIFIED: "
                + " -> ".join(s.value for s in path))
            continue
        if (path.index(A.State.UNDER_REVIEW)
                > path.index(A.State.VERIFIED)):
            problems.append(
                "a path is VERIFIED before it is UNDER_REVIEW: "
                + " -> ".join(s.value for s in path))

    # I2: terminal states are terminal.
    for state in A.TERMINAL:
        if succ.get(state):
            problems.append(
                f"I2: {state.value} is terminal and has successors "
                f"{sorted(s.value for s in succ[state])}")

    # I3: STALE returns only through re-verification.
    for path in paths_into(succ, A.State.STALE, A.State.PROMOTED):
        if A.State.UNDER_REVIEW not in path:
            problems.append(
                "I3: a path from STALE reaches PROMOTED without "
                "UNDER_REVIEW: " + " -> ".join(s.value for s in path))

    # I4: separation of duties, over every permitted way in.
    for (src, dst), ways in sorted(graph.items(),
                                   key=lambda kv: (kv[0][0].value,
                                                   kv[0][1].value)):
        if dst in (A.State.VERIFIED, A.State.PROMOTED, A.State.REJECTED):
            if any(w["same_actor"] for w in ways):
                problems.append(
                    f"I4: {src.value} -> {dst.value} is permitted to the "
                    "record's own proposer")

    # I6: the transitions that create authority require their evidence.
    for (src, dst), ways in graph.items():
        if dst is A.State.VERIFIED:
            if any("verification_report" not in w["evidence"] for w in ways):
                problems.append(
                    f"I6: {src.value} -> VERIFIED is permitted with no "
                    "verification_report")
        if dst is A.State.PROMOTED:
            need = {"verification_report", "policy_id"}
            if any(not need <= w["evidence"] for w in ways):
                problems.append(
                    f"I6: {src.value} -> PROMOTED is permitted without "
                    f"{sorted(need)}")

    # Reachability: a state nobody can reach is a state whose rules were
    # never exercised, and one somebody added and nothing connected.
    unreachable = set(A.State) - reachable(succ, A.INITIAL)
    if unreachable:
        problems.append(
            "unreachable from PROPOSED: "
            f"{sorted(s.value for s in unreachable)}")
    return problems


def check_scheduler(graph: dict) -> list:
    problems = []
    succ = successors(graph)

    # TERMINAL means FINISHED here, not frozen, and the two machines differ
    # on purpose: authority.check refuses any move out of a terminal state,
    # while the scheduler declares exactly one -- a succeeded job whose input
    # later changed becomes INVALIDATED. That is a fact about the world
    # arriving after the work, not a re-run, and the history keeps saying the
    # job succeeded. So the invariant is stated with the exception named
    # rather than by widening it into "terminal states may have successors",
    # which would check nothing.
    for state in S.TERMINAL:
        if state is S.JobState.SUCCEEDED:
            continue
        if succ.get(state):
            problems.append(
                f"{state.value} is terminal and has successors "
                f"{sorted(s.value for s in succ[state])}")

    # Accepted work leaves SUCCEEDED only by being invalidated. Anything
    # else would let a finished job be re-run, or re-reported, under the
    # same identity.
    after_success = succ.get(S.JobState.SUCCEEDED, set())
    if after_success != {S.JobState.INVALIDATED}:
        problems.append(
            "SUCCEEDED leads to "
            f"{sorted(s.value for s in after_success)}; only INVALIDATED "
            "is permitted")

    # A cancelled job never finishes. Checked over paths, because an edge
    # table can forbid CANCELLED -> SUCCEEDED directly and still allow a
    # route round through READY.
    if S.JobState.CANCELLED in succ:
        for path in paths_into(succ, S.JobState.CANCELLED,
                               S.JobState.SUCCEEDED):
            problems.append(
                "a cancelled job reaches SUCCEEDED: "
                + " -> ".join(s.value for s in path))

    unreachable = set(S.JobState) - reachable(succ, S.INITIAL)
    if unreachable:
        problems.append(
            "unreachable from WAITING: "
            f"{sorted(s.value for s in unreachable)}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump", action="store_true",
                    help="print the derived permitted-transition graph")
    args = ap.parse_args()

    auth = authority_graph()
    sched = scheduler_graph()

    # ANTI-VACUITY. Every invariant below is "no path does X", and a graph
    # with no edges satisfies all of them. A derivation that refused
    # everything -- a check() that started raising, a State enum that lost
    # its members -- would otherwise report a clean sheet.
    if len(auth) < len(A.EDGES):
        print(f"derived only {len(auth)} authority transition(s) from "
              f"{len(A.EDGES)} declared edges; the interrogation is not "
              "reaching the gate, and every invariant below would pass over "
              "an empty graph")
        return 1
    if len(sched) < len(S.EDGES):
        print(f"derived only {len(sched)} scheduler transition(s) from "
              f"{len(S.EDGES)} declared edges")
        return 1

    print(f"authority: {len(auth)} permitted transition(s) derived by "
          f"interrogating check() over {len(list(A.State)) ** 2} state "
          f"pairs x {len(list(A.Role))} roles x 2 actor identities x "
          f"{2 ** len(EVIDENCE_KEYS)} evidence subsets")
    print(f"scheduler: {len(sched)} permitted transition(s) derived from "
          "check_edge()")

    if args.dump:
        for (src, dst), ways in sorted(auth.items(),
                                       key=lambda kv: (kv[0][0].value,
                                                       kv[0][1].value)):
            roles = sorted({w["role"].value for w in ways})
            print(f"  {src.value:12s} -> {dst.value:12s} roles={roles}")
        for (src, dst) in sorted(sched, key=lambda p: (p[0].value,
                                                       p[1].value)):
            print(f"  {src.value:12s} -> {dst.value:12s}")

    problems = check_authority(auth) + check_scheduler(sched)
    if problems:
        print(f"\n{len(problems)} INVARIANT VIOLATION(S):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("every path invariant holds over the whole derived graph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
