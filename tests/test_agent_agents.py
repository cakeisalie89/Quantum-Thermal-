"""Several agents: identity that cannot be borrowed, and a human that cannot
be simulated.

The tests that matter most are the ones about kind rather than role. An
escalation exists because a decision was not the agent's to make, so the
interesting question is never "does the reviewer role work" -- it is whether
any arrangement of roles lets an agent answer one. It must not.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.agents import (  # noqa: E402
    ACT_MESSAGE, BOOTSTRAP, AgentDirectory, AgentError, AgentRole,
    ConflictError, ConflictRule, EscalationError, EscalationState,
    IdentityError, INCOMPATIBLE, MessageError, PrincipalKind,
    check_separation, identity, identity_from_record,
)
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402

VALUE_A = digest({"answer": "a"})
VALUE_B = digest({"answer": "b"})
BODY = digest({"body": "text"})


@pytest.fixture()
def dir_(tmp_path):
    d = AgentDirectory(EventLog(tmp_path / "log.jsonl")).load()
    d.register(identity(agent_id="proposer", instance_id="p1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.PROPOSER}), by="system")
    d.register(identity(agent_id="executor", instance_id="x1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.EXECUTOR}), by="system")
    d.register(identity(agent_id="verifier", instance_id="v1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.VERIFIER, AgentRole.AUDITOR}),
               by="system")
    return d


def _human(d, iid="h1", by=BOOTSTRAP):
    return d.register(identity(agent_id="owner", instance_id=iid,
                               kind=PrincipalKind.HUMAN,
                               roles={AgentRole.REVIEWER}), by=by)


# ---- identity ------------------------------------------------------------
def test_an_unregistered_instance_did_not_participate(dir_):
    with pytest.raises(IdentityError, match="not registered"):
        dir_.get("ghost")


def test_an_instance_may_only_take_the_roles_it_holds(dir_):
    dir_.require("v1", AgentRole.VERIFIER)
    with pytest.raises(IdentityError, match="may not act as EXECUTOR"):
        dir_.require("v1", AgentRole.EXECUTOR)


def test_a_retired_instance_may_not_act(dir_):
    dir_.retire("v1", by="system", reason="replaced")
    with pytest.raises(IdentityError, match="was retired"):
        dir_.require("v1", AgentRole.VERIFIER)


@pytest.mark.parametrize("pair", INCOMPATIBLE)
def test_incompatible_roles_cannot_be_held_by_one_identity(pair):
    a, b = pair
    with pytest.raises(IdentityError, match="defeats the separation"):
        identity(agent_id="a", instance_id="i", kind=PrincipalKind.AGENT,
                 roles={a, b})


def test_an_identity_with_no_roles_is_refused():
    with pytest.raises(IdentityError, match="no roles"):
        identity(agent_id="a", instance_id="i", kind=PrincipalKind.AGENT,
                 roles=set())


def test_registering_one_instance_twice_is_refused(dir_, tmp_path):
    ident = identity(agent_id="proposer", instance_id="p1",
                     kind=PrincipalKind.AGENT, roles={AgentRole.VERIFIER})
    dir_.log.append(actor="mallory", action="agent.register", target="p1",
                    payload={"identity": ident.to_record()})
    with pytest.raises(IdentityError, match="registered twice"):
        AgentDirectory(EventLog(tmp_path / "log.jsonl")).load()


def test_an_identity_record_with_unknown_fields_is_refused():
    rec = identity(agent_id="a", instance_id="i", kind=PrincipalKind.AGENT,
                   roles={AgentRole.PROPOSER}).to_record()
    rec["is_human_really"] = True
    with pytest.raises(IdentityError, match="unknown fields"):
        identity_from_record(rec)


def test_identities_survive_a_restart(dir_, tmp_path):
    revived = AgentDirectory(EventLog(tmp_path / "log.jsonl")).load()
    assert [i.instance_id for i in revived.instances()] == ["p1", "v1", "x1"]
    assert revived.get("v1").digest() == dir_.get("v1").digest()


# ---- separation of duties on one task ------------------------------------
def test_one_instance_may_not_execute_and_then_verify(dir_):
    dir_.register(identity(agent_id="both", instance_id="b1",
                           kind=PrincipalKind.AGENT,
                           roles={AgentRole.EXECUTOR}), by="system")
    dir_.register(identity(agent_id="both", instance_id="b2",
                           kind=PrincipalKind.AGENT,
                           roles={AgentRole.VERIFIER}), by="system")
    already = {AgentRole.EXECUTOR: "b1"}
    check = check_separation(dir_, instance_id="b2",
                             taking=AgentRole.VERIFIER, already=already)
    assert check.allowed is False, (
        "b1 and b2 are two runs of ONE agent; an agent that restarts has not "
        "become somebody else")
    assert check.conflicting_role is AgentRole.EXECUTOR
    ok = check_separation(dir_, instance_id="v1", taking=AgentRole.VERIFIER,
                          already=already)
    assert ok.allowed is True


def test_separation_is_checked_in_both_directions(dir_):
    already = {AgentRole.VERIFIER: "v1"}
    dir_.register(identity(agent_id="verifier", instance_id="v2",
                           kind=PrincipalKind.AGENT,
                           roles={AgentRole.EXECUTOR}), by="system")
    assert not check_separation(dir_, instance_id="v2",
                                taking=AgentRole.EXECUTOR,
                                already=already).allowed


def test_an_unregistered_prior_holder_fails_closed(dir_):
    """Cannot be shown to be a different party, so it is treated as the
    same."""
    already = {AgentRole.EXECUTOR: "unknown-instance"}
    assert not check_separation(dir_, instance_id="v1",
                                taking=AgentRole.VERIFIER,
                                already=already).allowed


# ---- messages ------------------------------------------------------------
def test_a_message_is_carried_by_digest_not_inline(dir_):
    with pytest.raises(MessageError, match="carried by digest"):
        dir_.send(message_id="m1", sender_instance="p1",
                  recipient_agent="verifier", task_id="t1", subject="s",
                  body_digest="the whole body as text")


def test_duplicate_delivery_is_a_no_op(dir_):
    first = dir_.send(message_id="m1", sender_instance="p1",
                      recipient_agent="verifier", task_id="t1",
                      subject="please check", body_digest=BODY)
    again = dir_.send(message_id="m1", sender_instance="p1",
                      recipient_agent="verifier", task_id="t1",
                      subject="please check", body_digest=BODY)
    assert again == first
    assert len([e for e in dir_.log.read() if e.action == ACT_MESSAGE]) == 1


def test_a_resend_that_changes_the_body_is_refused(dir_):
    dir_.send(message_id="m1", sender_instance="p1",
              recipient_agent="verifier", task_id="t1", subject="s",
              body_digest=BODY)
    with pytest.raises(MessageError, match="rewrite"):
        dir_.send(message_id="m1", sender_instance="p1",
                  recipient_agent="verifier", task_id="t1", subject="s",
                  body_digest=digest({"body": "different"}))


def test_a_forged_redelivery_with_a_new_body_is_refused_on_replay(dir_,
                                                                  tmp_path):
    msg = dir_.send(message_id="m1", sender_instance="p1",
                    recipient_agent="verifier", task_id="t1", subject="s",
                    body_digest=BODY)
    rec = msg.to_record()
    rec["body_digest"] = digest({"body": "swapped"})
    dir_.log.append(actor="mallory", action=ACT_MESSAGE, target="verifier",
                    payload={"message": rec})
    with pytest.raises(MessageError, match="different body"):
        AgentDirectory(EventLog(tmp_path / "log.jsonl")).load()


def test_messages_are_ordered_by_the_log(dir_):
    for i in range(3):
        dir_.send(message_id=f"m{i}", sender_instance="p1",
                  recipient_agent="verifier", task_id="t1", subject=str(i),
                  body_digest=digest({"i": i}))
    assert [m.message_id for m in dir_.inbox("verifier")] == ["m0", "m1", "m2"]


def test_a_reply_to_a_message_that_was_never_sent_is_refused(dir_):
    with pytest.raises(MessageError, match="never sent"):
        dir_.send(message_id="m1", sender_instance="p1",
                  recipient_agent="verifier", task_id="t1", subject="s",
                  body_digest=BODY, in_reply_to="nope")


def test_a_message_to_a_retired_recipient_is_not_delivered_to_a_successor(
        dir_):
    dir_.send(message_id="m1", sender_instance="p1",
              recipient_agent="verifier", task_id="t1", subject="s",
              body_digest=BODY)
    dir_.retire("v1", by="system", reason="replaced")
    ok, refused = dir_.deliverable("verifier", "v1")
    assert ok == ()
    assert "retired" in refused[0][1]


def test_a_message_for_a_superseded_task_is_refused_not_delivered(dir_):
    dir_.send(message_id="m1", sender_instance="p1",
              recipient_agent="verifier", task_id="t1", subject="s",
              body_digest=BODY)
    dir_.send(message_id="m2", sender_instance="p1",
              recipient_agent="verifier", task_id="t2", subject="s",
              body_digest=BODY)
    ok, refused = dir_.deliverable("verifier", "v1",
                                   superseded_tasks=frozenset({"t1"}))
    assert [m.message_id for m in ok] == ["m2"]
    assert "superseded" in refused[0][1], (
        "acting on it would apply a decision to work that no longer exists")


def test_refusals_do_not_stop_the_deliverable_messages_behind_them(dir_):
    """A bus that throws on the first stale message stops delivering."""
    for i, task in enumerate(("t1", "t2", "t3")):
        dir_.send(message_id=f"m{i}", sender_instance="p1",
                  recipient_agent="verifier", task_id=task, subject="s",
                  body_digest=digest({"i": i}))
    ok, refused = dir_.deliverable("verifier", "v1",
                                   superseded_tasks=frozenset({"t1"}))
    assert len(ok) == 2 and len(refused) == 1


def test_an_unregistered_sender_cannot_send(dir_):
    with pytest.raises(IdentityError):
        dir_.send(message_id="m1", sender_instance="ghost",
                  recipient_agent="verifier", task_id="t1", subject="s",
                  body_digest=BODY)


# ---- conflict ------------------------------------------------------------
def _claims(dir_, values):
    for i, (iid, role, value) in enumerate(values):
        dir_.claim(claim_id=f"c{i}", task_id="t1", subject="result",
                   value_digest=value, by_instance=iid, role=role)


def test_agreeing_claims_resolve_under_any_rule(dir_):
    _claims(dir_, [("p1", AgentRole.PROPOSER, VALUE_A),
                   ("v1", AgentRole.VERIFIER, VALUE_A)])
    r = dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.REQUIRE_HUMAN)
    assert r.resolved is True and r.value_digest == VALUE_A


def test_conflicting_claims_do_not_resolve_by_arrival_order(dir_):
    """The rule people reach for makes the outcome depend on scheduling."""
    _claims(dir_, [("p1", AgentRole.PROPOSER, VALUE_A),
                   ("v1", AgentRole.VERIFIER, VALUE_B)])
    r = dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.REQUIRE_HUMAN)
    assert r.resolved is False
    assert "waits for a person" in r.reason
    assert not hasattr(ConflictRule, "LAST_WRITER_WINS")


def test_quorum_needs_distinct_instances(dir_):
    dir_.register(identity(agent_id="p2", instance_id="p2",
                           kind=PrincipalKind.AGENT,
                           roles={AgentRole.PROPOSER}), by="system")
    _claims(dir_, [("p1", AgentRole.PROPOSER, VALUE_A),
                   ("v1", AgentRole.VERIFIER, VALUE_B)])
    assert not dir_.resolve(task_id="t1", subject="result",
                            rule=ConflictRule.REQUIRE_QUORUM,
                            quorum=2).resolved
    dir_.claim(claim_id="c9", task_id="t1", subject="result",
               value_digest=VALUE_A, by_instance="p2",
               role=AgentRole.PROPOSER)
    r = dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.REQUIRE_QUORUM, quorum=2)
    assert r.resolved and r.value_digest == VALUE_A


def test_a_quorum_that_two_values_reach_is_not_a_resolution(dir_):
    for name, role in (("p2", AgentRole.PROPOSER), ("v2", AgentRole.VERIFIER),
                       ("a1", AgentRole.AUDITOR)):
        dir_.register(identity(agent_id=name, instance_id=name,
                               kind=PrincipalKind.AGENT, roles={role}),
                      by="system")
    _claims(dir_, [("p1", AgentRole.PROPOSER, VALUE_A),
                   ("p2", AgentRole.PROPOSER, VALUE_A),
                   ("v1", AgentRole.VERIFIER, VALUE_B),
                   ("v2", AgentRole.VERIFIER, VALUE_B)])
    with pytest.raises(ConflictError, match="has not resolved anything"):
        dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.REQUIRE_QUORUM, quorum=2)


def test_prefer_role_cannot_break_a_tie_inside_the_preferred_role(dir_):
    dir_.register(identity(agent_id="verifier", instance_id="v2",
                           kind=PrincipalKind.AGENT,
                           roles={AgentRole.VERIFIER}), by="system")
    _claims(dir_, [("v1", AgentRole.VERIFIER, VALUE_A),
                   ("v2", AgentRole.VERIFIER, VALUE_B)])
    r = dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.PREFER_ROLE, prefer=AgentRole.VERIFIER)
    assert r.resolved is False and "disagree with each other" in r.reason


def test_prefer_role_needs_the_role_it_prefers(dir_):
    _claims(dir_, [("p1", AgentRole.PROPOSER, VALUE_A)])
    with pytest.raises(ConflictError, match="not a rule"):
        dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.PREFER_ROLE)


def test_a_claim_names_its_value_by_digest(dir_):
    with pytest.raises(ConflictError, match="by digest"):
        dir_.claim(claim_id="c", task_id="t1", subject="result",
                   value_digest="the answer is 42", by_instance="p1",
                   role=AgentRole.PROPOSER)


def test_an_instance_cannot_claim_in_a_role_it_does_not_hold(dir_):
    with pytest.raises(IdentityError, match="may not act as"):
        dir_.claim(claim_id="c", task_id="t1", subject="result",
                   value_digest=VALUE_A, by_instance="p1",
                   role=AgentRole.VERIFIER)


# ---- escalation ----------------------------------------------------------
def _escalate(dir_, **over):
    kw = dict(escalation_id="e1", task_id="t1",
              question="widen the tolerance to make the 3D case match?",
              raised_by="p1", options=("yes", "no"))
    kw.update(over)
    return dir_.escalate(**kw)


def test_no_agent_may_answer_an_escalation_whatever_its_roles(dir_):
    """The check is on KIND, not on role.

    An agent holding REVIEWER is still an agent, and an escalation exists
    precisely because the decision was not an agent's to make.
    """
    dir_.register(identity(agent_id="reviewer-bot", instance_id="r1",
                           kind=PrincipalKind.AGENT,
                           roles={AgentRole.REVIEWER}), by="system")
    _escalate(dir_)
    with pytest.raises(EscalationError, match="AGENT principal"):
        dir_.answer(escalation_id="e1", answered_by="r1", answer="yes",
                    reason="looks fine to me")
    assert dir_.escalation("e1").state is EscalationState.OPEN


def test_an_agent_cannot_register_a_human_principal(dir_):
    """One step from answering its own escalations."""
    with pytest.raises(IdentityError, match="may not register a HUMAN"):
        dir_.register(identity(agent_id="owner", instance_id="fake-human",
                               kind=PrincipalKind.HUMAN,
                               roles={AgentRole.REVIEWER}), by="p1")


def test_a_human_may_register_another_human(dir_):
    _human(dir_, "h1")
    second = dir_.register(
        identity(agent_id="owner2", instance_id="h2",
                 kind=PrincipalKind.HUMAN, roles={AgentRole.REVIEWER}),
        by="h1")
    assert second.registered_by == "h1"


def test_the_bootstrap_is_spelled_out_so_it_is_greppable():
    assert BOOTSTRAP == "out-of-band-bootstrap"


def test_a_human_answer_is_recorded_with_its_reason(dir_):
    _human(dir_)
    _escalate(dir_)
    out = dir_.answer(escalation_id="e1", answered_by="h1", answer="no",
                      reason="tolerances are evidence-driven, not convenient")
    assert out.state is EscalationState.ANSWERED
    assert out.answer == "no" and out.answered_by == "h1"
    assert "evidence-driven" in out.answer_reason


def test_the_asker_may_not_answer_even_if_it_is_human(dir_):
    _human(dir_, "h1")
    _escalate(dir_, raised_by="h1")
    with pytest.raises(EscalationError, match="may not also"):
        dir_.answer(escalation_id="e1", answered_by="h1", answer="yes",
                    reason="mine")


def test_an_answer_must_be_one_of_the_offered_options(dir_):
    _human(dir_)
    _escalate(dir_)
    with pytest.raises(EscalationError, match="not one of"):
        dir_.answer(escalation_id="e1", answered_by="h1", answer="maybe",
                    reason="hedging")


def test_an_answer_must_carry_a_reason(dir_):
    _human(dir_)
    _escalate(dir_)
    with pytest.raises(EscalationError, match="must carry a reason"):
        dir_.answer(escalation_id="e1", answered_by="h1", answer="yes",
                    reason="  ")


def test_an_escalation_cannot_be_answered_twice(dir_):
    _human(dir_)
    _escalate(dir_)
    dir_.answer(escalation_id="e1", answered_by="h1", answer="yes",
                reason="ok")
    with pytest.raises(EscalationError, match="ANSWERED"):
        dir_.answer(escalation_id="e1", answered_by="h1", answer="no",
                    reason="changed my mind")


def test_an_escalation_with_one_option_is_a_notification(dir_):
    with pytest.raises(EscalationError, match="at least two options"):
        _escalate(dir_, options=("yes",))


def test_only_the_asker_may_withdraw(dir_):
    _human(dir_)
    _escalate(dir_)
    with pytest.raises(EscalationError, match="may not withdraw"):
        dir_.withdraw(escalation_id="e1", by="h1", reason="not needed")
    out = dir_.withdraw(escalation_id="e1", by="p1", reason="resolved itself")
    assert out.state is EscalationState.WITHDRAWN


def test_an_open_escalation_blocks_its_task_indefinitely(dir_):
    """In THIS repository it blocks forever, and that is correct.

    No human decision exists here and none is fabricated, so a workflow that
    depends on one stays blocked rather than proceeding on a manufactured
    answer.
    """
    assert dir_.is_blocked("t1") is False
    _escalate(dir_)
    assert dir_.is_blocked("t1") is True
    assert [e.escalation_id for e in dir_.open_escalations()] == ["e1"]
    dir_.withdraw(escalation_id="e1", by="p1", reason="no longer needed")
    assert dir_.is_blocked("t1") is False


def test_escalations_survive_a_restart(dir_, tmp_path):
    _escalate(dir_)
    revived = AgentDirectory(EventLog(tmp_path / "log.jsonl")).load()
    assert revived.is_blocked("t1") is True
    assert revived.escalation("e1").options == ("yes", "no")


def test_an_unknown_escalation_is_an_error(dir_):
    with pytest.raises(EscalationError, match="no escalation"):
        dir_.escalation("nope")


def test_apply_ignores_foreign_actions(dir_):
    ev = dir_.log.append(actor="x", action="task.create", target="t",
                         payload={})
    assert dir_.apply(ev) is False


def test_every_error_here_is_an_agent_error():
    for cls in (IdentityError, MessageError, ConflictError, EscalationError):
        assert issubclass(cls, AgentError)


def test_one_instance_cannot_reach_quorum_by_claiming_twice(dir_):
    """Quorum counts PARTIES, not claims.

    Counting claims makes a quorum something a single agent can manufacture
    by repeating itself, which is the opposite of what a quorum is for. The
    earlier test used distinct instances throughout, so it passed either way.
    """
    dir_.claim(claim_id="c1", task_id="t1", subject="result",
               value_digest=VALUE_A, by_instance="p1",
               role=AgentRole.PROPOSER)
    dir_.claim(claim_id="c2", task_id="t1", subject="result",
               value_digest=VALUE_A, by_instance="p1",
               role=AgentRole.PROPOSER)
    dir_.claim(claim_id="c3", task_id="t1", subject="result",
               value_digest=VALUE_B, by_instance="v1",
               role=AgentRole.VERIFIER)
    r = dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.REQUIRE_QUORUM, quorum=2)
    assert r.resolved is False, (
        "p1 repeated itself; a quorum a single agent can manufacture is not "
        "a quorum")
    assert "no value reached quorum" in r.reason


def test_a_rule_missing_its_parameter_is_refused_even_when_claims_agree(dir_):
    """Validation before the agreement shortcut.

    A malformed rule that only fails on the day the claims disagree is one
    that fails in the worst possible place.
    """
    dir_.claim(claim_id="c1", task_id="t1", subject="result",
               value_digest=VALUE_A, by_instance="p1",
               role=AgentRole.PROPOSER)
    with pytest.raises(ConflictError, match="not a rule"):
        dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.PREFER_ROLE)
    with pytest.raises(ConflictError, match="int >= 2"):
        dir_.resolve(task_id="t1", subject="result",
                     rule=ConflictRule.REQUIRE_QUORUM, quorum=1)
