"""The causal assurance graph, and the demonstration it exists for (6.1).

A research objective is run end to end (Gate C: a fixture model proposes a
hypothesis and a run, the governed path runs it and an independent check,
the reviewer decides, the ledger observes and concludes), and an analyst
files notes derived from the result. Then the model run is declared wrong:
its result is the deliberately incorrect observation.

The graph, a projection of the verified history, says what that withdrawal
must reach. The governed system is then left to do what it does -- the
governed path's invalidation and the research ledger's propagation -- and
the second reader, which shares no code with either, reconstructs what they
did. The two must agree exactly. Continued reliance on the withdrawn
result is then attempted, four ways, and refused.

The graph found something on its first run: the governed path's own note
about the run, derived from its artefacts, stayed ACTIVE after the run was
withdrawn and kept reaching the next model's context (D-2026-171). That is
repaired; the agreement below includes it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_research_loop as RL  # noqa: E402

from agent_substrate import assurance_graph as AG  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_model import GovernedModelRuns  # noqa: E402
from agent_substrate.memory import MemoryStore  # noqa: E402
from agent_substrate.reconstruct import (  # noqa: E402
    reconstruct, reconstruct_subsystems, reconstruct_tasks,
)
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402

OBJ = "objective:slab-q"


def _graph(w):
    report, events = w.log.read_verified()
    report.raise_if_bad()
    return AG.build(events)


def _runs(w):
    return GovernedModelRuns(root=ROOT, log=w.log,
                             evidence=EvidenceStore(w.base / "evidence"))


@pytest.fixture(scope="module")
def world():
    w = RL.World("assurance-graph", L.scripted_agent(
        hypothesis=RL.hypothesis(), run=RL.SLAB["request"]))
    assert w.controller().run("slab-q").status == "CONCLUDED"
    led = R.ResearchLedger(w.log).load()
    (obs,) = led.of("slab-q", "observation")
    bundle = led.get(obs).body["result_bundle"]
    mem = MemoryStore(w.log, evidence=EvidenceStore(w.base / "evidence"))
    mem = mem.load()
    mem.remember(memory_id="analyst-1", author="analyst",
                 derived_from=(bundle,),
                 text="T_at_half came out below the threshold")
    mem.remember(memory_id="analyst-summary", author="analyst",
                 summarizes=("analyst-1",),
                 text="the slab question is settled")
    w.obs, w.bundle = obs, bundle
    yield w
    w.clean()


def _model_task(g, bundle) -> str:
    (task,) = [x.src for x in g.into(f"bytes:{bundle}")
               if x.kind is AG.Edge.HISTORICAL_DEPENDENCY]
    return task


# --------------------------------------------------------- the questions

def test_an_auditor_s_questions_about_a_conclusion(world):
    g = _graph(world)
    ev = EvidenceStore(world.base / "evidence")
    ans = AG.explain(g, OBJ, held=ev.contains)
    assert ans["state"] == "CONCLUDED"
    assert ans["observations"] == [f"observation:{world.obs}"]
    assert ans["source_bytes"][f"bytes:{world.bundle}"] is True
    assert ans["model_was_shown"], "no context reached the conclusion"
    assert all(n.startswith(("bytes:", "memory:", "context:"))
               for n in ans["model_was_shown"])
    assert any("result_rules" in a or "admission" in a
               for a in ans["assumptions"]), ans["assumptions"]
    assert ans["policies_in_force"]
    assert any("verification_report" in v for v in ans["verified_by"])
    assert any(v.startswith("actor:") for v in ans["verified_by"])
    assert ans["authorized_by"]


def test_what_a_withdrawal_must_reach(world):
    g = _graph(world)
    task = _model_task(g, world.bundle)
    gone = AG.withdrawal(g, [task])
    stale = gone["stale"]
    for node in (f"bytes:{world.bundle}", f"observation:{world.obs}", OBJ,
                 "memory:analyst-1", "memory:analyst-summary"):
        assert node in stale, node
    assert any(n.startswith("record:") for n in stale)
    assert any(n.startswith("hypothesis:") for n in stale)
    # what was SHOWN or SAID on the strength of it is exposed, not stale
    assert gone["exposed"] and not gone["exposed"] & stale
    assert all(n.startswith(("context:", "inference:", "proposal:",
                             "task:")) for n in gone["exposed"])


def test_an_unrelated_withdrawal_reaches_nothing_of_it(world):
    g = _graph(world)
    gone = AG.withdrawal(g, ["bytes:" + "e" * 64])
    assert gone == {"stale": set(), "exposed": set()}


def test_nothing_is_connected_for_occurring_together(world):
    """Edges come from recorded fields only: the analyst's two notes were
    written one after the other, and only the summary's own field joins
    them."""
    g = _graph(world)
    links = [x for x in g.links if {x.src, x.dst} <= {
        "memory:analyst-1", "memory:analyst-summary"}]
    assert [(x.src, x.dst, x.kind) for x in links] == [
        ("memory:analyst-1", "memory:analyst-summary",
         AG.Edge.SUMMARY_DERIVATION)]
    assert g.unread, "the graph says what it does not read"


# ------------------------------------- the withdrawal, and who carries it

@pytest.fixture(scope="module")
def withdrawn(world):
    g = _graph(world)
    task = _model_task(g, world.bundle)
    predicted = AG.withdrawal(g, [task])
    before = {e.seq: e.hash for e in world.log.read_verified()[1]}
    runs = _runs(world)
    inv = runs.invalidate_task(task[len("task:"):],
                               reason="the model's input was wrong")
    moved = R.ResearchLedger(world.log, evidence=runs.evidence).load() \
        .propagate()
    return {"task": task, "predicted": predicted, "inv": inv,
            "moved": moved, "before": before}


def _reconstructed_not_standing(log) -> set:
    """What the SECOND reader says no longer stands."""
    out = set()
    rec = reconstruct(log)
    assert not rec.unauthorized and not rec.anomalies, (rec.unauthorized,
                                                        rec.anomalies)
    for rid, r in rec.records.items():
        if r["state"] in AG.NOT_STANDING["record"]:
            out.add(f"record:{rid}")
    sub = reconstruct_subsystems(log)
    assert sub.anomalies == [], sub.anomalies
    for rid, r in sub.research.items():
        if r["status"] in AG.NOT_STANDING.get(r["kind"], ()):
            out.add(f"{r['kind']}:{rid}")
    for mid, m in sub.memory.items():
        if m.get("status") in AG.NOT_STANDING["memory"]:
            out.add(f"memory:{mid}")
    for tid, t in reconstruct_tasks(log).tasks.items():
        if t["state"] in AG.NOT_STANDING["task"]:
            out.add(f"task:{tid}")
    return out


def test_the_system_carries_exactly_what_the_graph_predicts(world,
                                                             withdrawn):
    stateful = {n for n in withdrawn["predicted"]["stale"]
                if not n.startswith("bytes:")}
    actual = _reconstructed_not_standing(world.log) - {withdrawn["task"]}
    # tasks the run rejected or failed are not this withdrawal's doing
    actual = {n for n in actual if not n.startswith("task:")}
    assert actual == stateful, (sorted(actual ^ stateful))
    # and the graph of the history AFTER the withdrawal agrees the same
    # nodes no longer stand
    g = _graph(world)
    assert {n for n in stateful if not AG.standing(g, n)} == stateful


def test_the_note_the_governed_path_filed_is_carried_too(world, withdrawn):
    """D-2026-171: the run's own note, derived from its artefacts."""
    assert withdrawn["inv"].memories
    g = _graph(world)
    for mid in withdrawn["inv"].memories:
        assert not AG.standing(g, f"memory:{mid}")


def test_nothing_already_written_changed(world, withdrawn):
    after = {e.seq: e.hash for e in world.log.read_verified()[1]}
    assert all(after[s] == h for s, h in withdrawn["before"].items())


# ---------------------------------- continued reliance is refused

def test_a_verdict_on_the_withdrawn_observation_is_refused(world,
                                                           withdrawn):
    led = R.ResearchLedger(world.log).load()
    hid = next(h for h in led.of("slab-q", "hypothesis"))
    with pytest.raises(R.ResearchError):
        led.set_status(hid, "SUPPORTED", reason="it held before",
                       basis=[world.obs])


def test_the_withdrawn_result_cannot_be_promoted(world, withdrawn):
    store = AuthorityStore(world.log,
                           evidence=EvidenceStore(world.base / "evidence"))
    store = store.load()
    rid = next(n for n in withdrawn["predicted"]["stale"]
               if n.startswith("record:"))[len("record:"):]
    assert store.get(rid).state is State.STALE
    with pytest.raises((StoreError, Exception)):
        store.transition(record_id=rid, dst=State.PROMOTED, actor="carol",
                         role=Role.PROMOTER, policy_id="promote")


def test_the_objective_cannot_be_concluded_on_it_again(world, withdrawn):
    led = R.ResearchLedger(world.log).load()
    assert led.get("slab-q").status == "STALE"
    hid = next(h for h in led.of("slab-q", "hypothesis"))
    with pytest.raises(R.ResearchError):
        led.set_status("slab-q", "CONCLUDED", reason="again", basis=[hid])


def test_the_withdrawn_notes_are_not_shown_to_the_next_model(world,
                                                             withdrawn):
    mem = MemoryStore(world.log,
                      evidence=EvidenceStore(world.base / "evidence")).load()
    current = {e.memory_id for e in mem.current()}
    for mid in ("analyst-1", "analyst-summary", *withdrawn["inv"].memories):
        assert mid not in current, mid


# ------------------------------------------- the graph's own rules, alone

def _ev(seq, action, payload, actor="writer", target=""):
    from types import SimpleNamespace
    return SimpleNamespace(seq=seq, action=action, payload=payload,
                           actor=actor, target=target)


B1, B2 = "1" * 64, "2" * 64


def _produced(seq, task, sha, state="VERIFIED"):
    return [_ev(seq, "task.create", {"task_id": task}),
            _ev(seq + 1, "task.evidence", {"task_id": task,
                                           "artifacts": {"out": sha}}),
            _ev(seq + 2, "task.transition", {"task_id": task,
                                             "dst": state})]


def _cites(seq, rid, sha):
    return [_ev(seq, "record.create", {"record_id": rid,
                                       "evidence": {"result_bundle": sha}}),
            _ev(seq + 1, "record.transition",
                {"record_id": rid, "dst": "VERIFIED",
                 "evidence": {"verification_report": B2}})]


def test_bytes_another_task_produced_keep_their_currency():
    g = AG.build(_produced(0, "t1", B1) + _produced(3, "t2", B1)
                 + _cites(6, "r1", B1))
    assert AG.withdrawal(g, ["task:t1"])["stale"] == set()
    assert {"record:r1", f"bytes:{B1}"} <= AG.withdrawal(
        g, ["task:t1", "task:t2"])["stale"]


def test_a_producer_already_withdrawn_does_not_hold_them_up():
    g = AG.build(_produced(0, "t1", B1) + _produced(3, "t2", B1,
                                                    "INVALIDATED")
                 + _cites(6, "r1", B1))
    assert "record:r1" in AG.withdrawal(g, ["task:t1"])["stale"]


def test_a_record_withdrawn_directly_reaches_what_was_read_from_it():
    g = AG.build(_produced(0, "t1", B1) + _cites(3, "r1", B1) + [
        _ev(5, "research.observation", {"observation": {
            "observation_id": "o1", "record_id": "r1",
            "result_bundle": B1}})])
    assert "observation:o1" in AG.withdrawal(g, ["record:r1"])["stale"]


def test_a_claim_a_request_or_a_view_exposes_and_never_stales():
    events = [
        _ev(0, "proposal.receive", {"envelope": {
            "agent_id": "a", "citations": [{"source_sha256": B1,
                                            "path": "x.md"}]}},
            target="p1"),
        _ev(1, "idempotency.bind", {"key": "p1", "task_id": "t9"}),
        _ev(2, "context.build", {"manifest_digest": "c1", "manifest": {
            "items": [{"item_id": "i", "source": B1}]}}),
    ]
    g = AG.build(events)
    kinds = {(x.src, x.dst): x.kind for x in g.links}
    assert kinds[(f"bytes:{B1}", "proposal:p1")] is AG.Edge.CLAIMED_SUPPORT
    assert kinds[("proposal:p1", "task:t9")] is AG.Edge.REQUEST
    assert kinds[(f"bytes:{B1}", "context:c1")] is \
        AG.Edge.CONTEXT_INCLUSION
    gone = AG.withdrawal(g, [f"bytes:{B1}"])
    assert gone["stale"] == set()
    assert gone["exposed"] == {"proposal:p1", "context:c1"}


def test_a_verdict_rests_on_its_basis_and_a_conclusion_on_its_verdict():
    events = [
        _ev(0, "research.objective", {"objective": {"objective_id": "q"}}),
        _ev(1, "research.observation", {"observation": {
            "observation_id": "o1", "record_id": "r1",
            "result_bundle": B1}}),
        _ev(2, "research.hypothesis", {"hypothesis": {
            "hypothesis_id": "h1", "proposal_id": "p1", "depends_on": []}}),
        _ev(3, "research.status", {"target_id": "h1",
                                   "status": "SUPPORTED", "basis": ["o1"]}),
        _ev(4, "research.status", {"target_id": "q", "status": "CONCLUDED",
                                   "basis": ["h1"]}),
    ]
    g = AG.build(events)
    assert AG.withdrawal(g, ["observation:o1"])["stale"] == {
        "hypothesis:h1", "objective:q"}
    # the proposal the hypothesis came from supports nothing
    assert AG.withdrawal(g, ["proposal:p1"])["stale"] == set()


def test_what_the_graph_does_not_read_it_counts():
    g = AG.build([_ev(0, "scheduler.enqueue", {}),
                  _ev(1, "scheduler.enqueue", {})])
    assert g.unread == {"scheduler.enqueue": 2} and g.links == []
