"""A writer decides against the history its record is appended onto.

D-2026-147. The agent directory and the memory store checked their rules
against the projection they had loaded and then appended outside the
writer lock. Two people answering one escalation each found it OPEN in
their own view; both answers were written, each caller was told its
decision stood, and every later load() refused the history ("answering it
again would rewrite a decision that was already recorded"). Nothing wrong
ever became authoritative -- the reducer refused -- but an ordinary race
left a log nobody could load. EventLog.append_decided exists for this and
these writers did not use it.

Each test below makes the race deterministic: two projections are loaded
from one log, the first writes, and the second -- whose view is now stale
-- tries a write that conflicts. The second must be REFUSED, the log must
still load, and the decision on record must be the first one. The last
test races real processes on a barrier.
"""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from agent_substrate.agents import (BOOTSTRAP, AgentDirectory, AgentRole,
                                    ConflictError, EscalationError,
                                    EscalationState, IdentityError,
                                    MessageError, PrincipalKind, identity)
from agent_substrate.events import EventLog
from agent_substrate.memory import MemoryError_, MemoryStatus, MemoryStore

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _directory(path: Path) -> AgentDirectory:
    d = AgentDirectory(EventLog(path)).load()
    d.register(identity(agent_id="proposer", instance_id="p1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.PROPOSER}), by="system")
    for iid in ("h1", "h2"):
        d.register(identity(agent_id=f"owner-{iid}", instance_id=iid,
                            kind=PrincipalKind.HUMAN,
                            roles={AgentRole.REVIEWER}), by=BOOTSTRAP)
    d.escalate(escalation_id="e1", task_id="t1", question="ship it?",
               raised_by="p1", options=("yes", "no"))
    return d


def _two_views(path: Path):
    return (AgentDirectory(EventLog(path)).load(),
            AgentDirectory(EventLog(path)).load())


# ---- the agent directory ----------------------------------------------------

def test_a_second_answer_from_a_stale_view_is_refused_and_the_log_loads(
        tmp_path):
    p = tmp_path / "log.jsonl"
    _directory(p)
    a, b = _two_views(p)
    a.answer(escalation_id="e1", answered_by="h1", answer="yes",
             reason="approve")
    with pytest.raises(EscalationError, match="ANSWERED"):
        b.answer(escalation_id="e1", answered_by="h2", answer="no",
                 reason="reject")
    c = AgentDirectory(EventLog(p)).load()
    e = c.escalation("e1")
    assert (e.state, e.answer, e.answered_by) == (
        EscalationState.ANSWERED, "yes", "h1")


def test_control_an_answer_from_an_up_to_date_view_is_written(tmp_path):
    """The stale view was refused for being stale, not for answering."""
    p = tmp_path / "log.jsonl"
    _directory(p)
    _, b = _two_views(p)
    b.answer(escalation_id="e1", answered_by="h2", answer="no",
             reason="reject")
    assert AgentDirectory(EventLog(p)).load().escalation("e1").answer == "no"


def test_a_withdrawal_racing_an_answer_is_refused(tmp_path):
    p = tmp_path / "log.jsonl"
    _directory(p)
    a, b = _two_views(p)
    a.answer(escalation_id="e1", answered_by="h1", answer="yes",
             reason="approve")
    with pytest.raises(EscalationError, match="ANSWERED"):
        b.withdraw(escalation_id="e1", by="p1", reason="no longer needed")
    assert AgentDirectory(EventLog(p)).load().escalation(
        "e1").state is EscalationState.ANSWERED


def test_an_answer_by_a_principal_retired_meanwhile_is_refused(tmp_path):
    """The replay refuses a retired principal's answer, and the write path
    did not ask: a stale view let the retired person answer, and the
    history became unloadable."""
    p = tmp_path / "log.jsonl"
    _directory(p)
    a, b = _two_views(p)
    a.retire("h2", by="h1", reason="left the project")
    with pytest.raises(EscalationError, match="retired"):
        b.answer(escalation_id="e1", answered_by="h2", answer="no",
                 reason="reject")
    AgentDirectory(EventLog(p)).load()


def test_a_second_registration_of_one_instance_is_refused(tmp_path):
    p = tmp_path / "log.jsonl"
    _directory(p)
    a, b = _two_views(p)
    ident = identity(agent_id="checker", instance_id="c1",
                     kind=PrincipalKind.AGENT, roles={AgentRole.VERIFIER})
    a.register(ident, by="system")
    with pytest.raises(IdentityError, match="already registered"):
        b.register(ident, by="system")
    AgentDirectory(EventLog(p)).load()


def test_a_second_escalation_with_one_id_is_refused(tmp_path):
    p = tmp_path / "log.jsonl"
    _directory(p)
    a, b = _two_views(p)
    a.escalate(escalation_id="e2", task_id="t1", question="a?",
               raised_by="p1", options=("x", "y"))
    with pytest.raises(EscalationError, match="already raised"):
        b.escalate(escalation_id="e2", task_id="t1", question="b?",
                   raised_by="p1", options=("x", "y"))
    AgentDirectory(EventLog(p)).load()


def test_a_message_id_reused_by_a_stale_view_is_refused(tmp_path):
    p = tmp_path / "log.jsonl"
    _directory(p)
    a, b = _two_views(p)
    a.send(message_id="m1", sender_instance="p1", recipient_agent="owner-h1",
           task_id="t1", subject="first", body_digest=DIGEST_A)
    with pytest.raises(MessageError, match="already sent"):
        b.send(message_id="m1", sender_instance="p1",
               recipient_agent="owner-h1", task_id="t1", subject="second",
               body_digest=DIGEST_B)
    # A true resend from the stale view is still the no-op it always was.
    b.send(message_id="m1", sender_instance="p1", recipient_agent="owner-h1",
           task_id="t1", subject="first", body_digest=DIGEST_A)
    c = AgentDirectory(EventLog(p)).load()
    assert len(c.inbox("owner-h1")) == 1


def test_a_claim_id_reused_by_a_stale_view_is_refused(tmp_path):
    p = tmp_path / "log.jsonl"
    _directory(p)
    a, b = _two_views(p)
    a.claim(claim_id="k1", task_id="t1", subject="s", value_digest=DIGEST_A,
            by_instance="p1", role=AgentRole.PROPOSER)
    with pytest.raises(ConflictError, match="already recorded"):
        b.claim(claim_id="k1", task_id="t1", subject="s",
                value_digest=DIGEST_B, by_instance="p1",
                role=AgentRole.PROPOSER)
    AgentDirectory(EventLog(p)).load()


# ---- the memory store ---------------------------------------------------------

def test_two_writers_of_one_memory_id_leave_a_loadable_log(tmp_path):
    p = tmp_path / "log.jsonl"
    a = MemoryStore(EventLog(p)).load()
    b = MemoryStore(EventLog(p)).load()
    a.remember(memory_id="m", text="first", author="w1")
    with pytest.raises(MemoryError_, match="already exists"):
        b.remember(memory_id="m", text="second", author="w2")
    assert MemoryStore(EventLog(p)).load().get("m").text == "first"


def test_a_supersede_against_a_retraction_made_meanwhile_is_refused(
        tmp_path):
    p = tmp_path / "log.jsonl"
    MemoryStore(EventLog(p)).load().remember(memory_id="old", text="x",
                                             author="w1")
    a = MemoryStore(EventLog(p)).load()
    b = MemoryStore(EventLog(p)).load()
    b.remember(memory_id="new", text="y", author="w1")
    a.retract("old", actor="w1", reason="wrong")
    with pytest.raises(MemoryError_, match="RETRACTED and may not move"):
        b.supersede(old_id="old", new_id="new", actor="w1", reason="better")
    m = MemoryStore(EventLog(p)).load()
    assert m.get("old").status is MemoryStatus.RETRACTED


def test_a_cascade_skips_an_entry_withdrawn_meanwhile(tmp_path):
    p = tmp_path / "log.jsonl"
    MemoryStore(EventLog(p)).load().remember(
        memory_id="n", text="from a source", author="w1",
        derived_from=("src",))
    a = MemoryStore(EventLog(p)).load()
    b = MemoryStore(EventLog(p)).load()
    a.retract("n", actor="w1", reason="withdrawn")
    assert b.invalidate_source("src", actor="auditor", reason="changed") == ()
    assert MemoryStore(EventLog(p)).load().get(
        "n").status is MemoryStatus.RETRACTED


# ---- a real race, across processes -------------------------------------------

def _answer_after_barrier(path: str, who: str, barrier, out) -> None:
    d = AgentDirectory(EventLog(Path(path))).load()
    barrier.wait()
    try:
        d.answer(escalation_id="e1", answered_by=who, answer="yes",
                 reason=f"{who} approves")
        out.put((who, "written"))
    except EscalationError as exc:
        out.put((who, f"refused: {exc}"))


def test_two_processes_answering_at_once_write_one_answer(tmp_path):
    p = tmp_path / "log.jsonl"
    _directory(p)
    ctx = mp.get_context("spawn")
    barrier, out = ctx.Barrier(2), ctx.Queue()
    procs = [ctx.Process(target=_answer_after_barrier,
                         args=(str(p), who, barrier, out))
             for who in ("h1", "h2")]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(60)
        assert pr.exitcode == 0
    results = dict(out.get(timeout=5) for _ in procs)
    assert sorted(v.split(":")[0] for v in results.values()) == [
        "refused", "written"], results
    winner = next(k for k, v in results.items() if v == "written")
    assert AgentDirectory(EventLog(p)).load().escalation(
        "e1").answered_by == winner
