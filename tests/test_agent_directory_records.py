"""The agent directory's records: an answer closes its question, and a
record the reducer cannot read is refused in its own words (AUD-060,
AUD-061).

AUD-060. An escalation answer moves the question to ANSWERED, or, from its
asker, WITHDRAWN. The directory took an answer record whose state was OPEN
on the answer path: it recorded a person's answer on a question still open,
and the next answer replaced it. answer() and withdraw() never write one,
and the second reader refused it while the directory loaded it.

AUD-061. Eight malformed directory records raised KeyError, ValueError or
TypeError out of the reducer, where each refusal it makes is an AgentError:
AUD-058's class, in another reducer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import agents as A  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402


# ------------------------------------------- AUD-061: the reducer's words
@pytest.mark.parametrize("action, payload, error, words", [
    (A.ACT_AGENT_REGISTER, {}, A.IdentityError,
     "agent.register carries no identity record"),
    (A.ACT_AGENT_RETIRE, {}, A.IdentityError, "a retirement names no instance"),
    (A.ACT_AGENT_RETIRE, {"instance_id": ["x"]}, A.IdentityError,
     "a retirement names no instance"),
    (A.ACT_MESSAGE, {}, A.MessageError,
     "agent.message carries no message record"),
    (A.ACT_CLAIM, {"by_instance": "x"}, A.ConflictError,
     r"a claim record lacks \['claim_id', 'task_id', 'subject', "
     r"'value_digest'\]"),
    (A.ACT_CLAIM, {"by_instance": "x", "claim_id": "c", "task_id": "t",
                   "subject": "s", "value_digest": "d", "role": "KING"},
     A.ConflictError, "claim 'c' names the role 'KING', which is no role"),
    (A.ACT_ESCALATION, {}, A.EscalationError,
     "agent.escalation carries no escalation record"),
    (A.ACT_ESCALATION_ANSWER, {}, A.EscalationError,
     "an answer names no escalation"),
])
def test_a_malformed_directory_record_is_refused_in_its_own_words(
        tmp_path, action, payload, error, words):
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="x", action=action, target="t", payload=payload)
    with pytest.raises(error, match=rf"seq \d+: {words}"):
        A.AgentDirectory(log).load()
    assert reconstruct_subsystems(log).anomalies, "the second reader notes it"


# ------------------------------------- AUD-060: an answer closes the question
def _asked(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    d = A.AgentDirectory(log).load()
    for name in ("h1", "h2"):
        d.register(A.identity(agent_id=name, instance_id=name,
                              kind=A.PrincipalKind.HUMAN,
                              roles={A.AgentRole.REVIEWER}), by=A.BOOTSTRAP)
    d.register(A.identity(agent_id="bot", instance_id="bot",
                          kind=A.PrincipalKind.AGENT,
                          roles={A.AgentRole.PROPOSER}), by="system")
    d.escalate(escalation_id="e1", task_id="t1", question="ship it?",
               raised_by="bot", options=("yes", "no"))
    return log, d


def _answer(log, *, by, state, answer="yes"):
    log.append(actor=by, action=A.ACT_ESCALATION_ANSWER, target="t1",
               payload={"escalation_id": "e1", "state": state,
                        "answer": answer, "answered_by": by, "reason": "r"})


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_an_answer_that_leaves_the_question_open_is_refused(tmp_path, state):
    log, _ = _asked(tmp_path)
    _answer(log, by="h1", state=state)
    with pytest.raises(A.EscalationError, match=r"seq \d+: escalation 'e1' "
                                                "is moved to"):
        A.AgentDirectory(EventLog(log.path)).load()
    found = reconstruct_subsystems(EventLog(log.path)).anomalies
    assert any("neither answered nor withdrawn" in a for a in found), found


def test_a_recorded_answer_is_not_replaced_by_the_next(tmp_path):
    """As it was: h1's answer, left OPEN, was replaced by h2's."""
    log, _ = _asked(tmp_path)
    _answer(log, by="h1", state="OPEN", answer="yes")
    _answer(log, by="h2", state="ANSWERED", answer="no")
    with pytest.raises(A.EscalationError, match="neither answered nor "
                                                "withdrawn"):
        A.AgentDirectory(EventLog(log.path)).load()


def test_an_answer_and_a_withdrawal_still_close_it(tmp_path):
    log, d = _asked(tmp_path)
    d.answer(escalation_id="e1", answered_by="h1", answer="no", reason="r")
    e = A.AgentDirectory(EventLog(log.path)).load().escalation("e1")
    assert (e.state, e.answer, e.answered_by) == (
        A.EscalationState.ANSWERED, "no", "h1")
    log2, d2 = _asked(tmp_path / "w")
    d2.withdraw(escalation_id="e1", by="bot", reason="not needed")
    e2 = A.AgentDirectory(EventLog(log2.path)).load().escalation("e1")
    assert e2.state is A.EscalationState.WITHDRAWN
    assert reconstruct_subsystems(EventLog(log.path)).anomalies == []
