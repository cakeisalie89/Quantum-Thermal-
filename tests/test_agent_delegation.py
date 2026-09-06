"""Who may mint a capability, and what a delegation may not widen.

THE GAP THESE TESTS CLOSE, stated as it was found:

    "no ISSUER authority: any actor that can write the log can mint a grant.
     issuer_of() records who did, and is deliberately not consulted by
     check() -- attribution, not authorization"
    "no delegation or attenuation"

Attribution answers "who did this". Authorization answers "who may". The
ledger had the first and refused the second, so an actor with log-append
access could grant itself anything and the only trace was its own name beside
the grant it wrote.

Written adversarially: nearly every test here is an attempt to obtain
authority through a route other than being given it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qta_agent.capability import (  # noqa: E402
    ACT_ISSUE, ACT_ROOT, MAX_DELEGATION_DEPTH, Action, BadDelegation,
    CapabilityExpired, CapabilityLedger, CapabilityRevoked, CapabilityUnknown,
    NotTheIssuer, Request, issue,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.reconstruct import reconstruct_subsystems  # noqa: E402

SCOPE = ("verification/stage10",)


def _ledger(tmp_path):
    log = EventLog(tmp_path / "caps.jsonl")
    return log, CapabilityLedger(log).load()


def _cap(cap_id="c1", subject="agent-1", scope=SCOPE, **kw):
    base = dict(capability_id=cap_id, subject=subject, action=Action.READ_PATHS,
                task_id="t1", scope=scope, issued_seq=1)
    base.update(kw)
    return issue(**base)


# --------------------------------------------------------------------------
# The root issuer
# --------------------------------------------------------------------------

def test_the_first_mint_establishes_a_root_and_records_it(tmp_path):
    """A root that is not in the log is a root nobody can check."""
    log, led = _ledger(tmp_path)
    assert led.root_issuer() is None
    led.issue(_cap(), actor="control-plane")

    assert led.root_issuer() == "control-plane"
    assert [ev.action for ev in log.read()] == [ACT_ROOT, ACT_ISSUE]


def test_a_second_actor_cannot_mint_after_the_root_exists(tmp_path):
    """THE defect. An actor that can append could previously grant itself."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")

    with pytest.raises(NotTheIssuer) as exc:
        led.issue(_cap(cap_id="c2", subject="attacker"), actor="attacker")
    assert "root issuer" in str(exc.value)
    assert led.issued_ids() == ("c1",)


def test_a_forged_grant_appended_directly_fails_at_load(tmp_path):
    """Not merely refused by the writer: refused by every READER.

    Going through the ledger's ``issue`` is the honest path, so an attacker
    does not use it. The check has to live in the projection, or it only
    stops people who were not attacking.
    """
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")

    forged = _cap(cap_id="c-forged", subject="attacker",
                  issued_seq=log.verify().head_seq + 1)
    log.append(actor="attacker", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **forged.body()})

    with pytest.raises(NotTheIssuer) as exc:
        CapabilityLedger(log).load()
    assert "c-forged" in str(exc.value)


def test_the_second_reader_reaches_the_same_verdict_without_the_ledger(
        tmp_path):
    """Independently. reconstruct.py may not import capability.py at all."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    forged = _cap(cap_id="c-forged", subject="attacker",
                  issued_seq=log.verify().head_seq + 1)
    log.append(actor="attacker", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **forged.body()})

    recon = reconstruct_subsystems(log)
    assert recon.root_issuer == "control-plane"
    assert "c-forged" not in recon.capabilities
    assert any("attacker" in a and "root issuer" in a
               for a in recon.anomalies), recon.anomalies


def test_anointing_a_third_party_is_refused(tmp_path):
    """The root is whoever establishes it, not whoever is nominated.

    Otherwise the first writer's real power is to hand minting authority to
    anybody, which is the same authority with one more step in front of it.
    """
    log, _led = _ledger(tmp_path)
    log.append(actor="alice", action=ACT_ROOT, target="mallory",
               payload={"issuer": "mallory"})
    with pytest.raises(NotTheIssuer) as exc:
        CapabilityLedger(log).load()
    assert "mallory" in str(exc.value)


def test_two_roots_are_refused(tmp_path):
    log, led = _ledger(tmp_path)
    led.anoint(actor="control-plane")
    with pytest.raises(NotTheIssuer):
        led.anoint(actor="somebody-else")
    log.append(actor="somebody-else", action=ACT_ROOT, target="somebody-else",
               payload={"issuer": "somebody-else"})
    with pytest.raises(NotTheIssuer) as exc:
        CapabilityLedger(log).load()
    assert "already" in str(exc.value)


def test_anointing_the_same_actor_twice_is_not_a_failure(tmp_path):
    """A bootstrap that runs twice is a bootstrap that ran twice."""
    _log, led = _ledger(tmp_path)
    assert led.anoint(actor="control-plane") == "control-plane"
    assert led.anoint(actor="control-plane") == "control-plane"


def test_a_grant_minted_with_no_root_at_all_is_refused_at_load(tmp_path):
    """A hand-written log that skips the root does not get a free pass."""
    log = EventLog(tmp_path / "caps.jsonl")
    cap = _cap(issued_seq=0)
    log.append(actor="attacker", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **cap.body()})
    with pytest.raises(NotTheIssuer) as exc:
        CapabilityLedger(log).load()
    # The DISTINCTIVE phrase, not the shared one. Both this guard and the
    # wrong-actor fallback below it say "root issuer", so asserting that
    # substring let a mutation remove this guard entirely and still pass:
    # the fallback fired, with a different reason, and the test could not
    # tell. A sibling guard reaching the same verdict is not this guard
    # working.
    assert "nothing established a root" in str(exc.value)


# --------------------------------------------------------------------------
# Delegation: the holder passes on part of what it has
# --------------------------------------------------------------------------

def test_a_holder_may_delegate_something_narrower(tmp_path):
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")

    child = led.delegate(
        _cap(cap_id="c2", subject="helper",
             scope=("verification/stage10/sub",)),
        parent_id="c1", actor="agent-1")

    assert child.parent_id == "c1"
    caps = led.in_force()
    caps.check("c2", Request(actor="helper", action=Action.READ_PATHS,
                             task_id="t1",
                             paths=("verification/stage10/sub/x.json",)))


def test_a_delegation_cannot_widen_the_scope(tmp_path):
    log, led = _ledger(tmp_path)
    led.issue(_cap(scope=("verification/stage10/sub",)), actor="control-plane")

    with pytest.raises(BadDelegation) as exc:
        led.delegate(_cap(cap_id="c2", subject="helper", scope=SCOPE),
                     parent_id="c1", actor="agent-1")
    assert "does not" in str(exc.value) or "outside" in str(exc.value)


def test_a_delegation_cannot_outlive_its_parent(tmp_path):
    log, led = _ledger(tmp_path)
    led.issue(_cap(expires_after_seq=40), actor="control-plane")

    with pytest.raises(BadDelegation) as exc:
        led.delegate(_cap(cap_id="c2", subject="helper",
                          expires_after_seq=99),
                     parent_id="c1", actor="agent-1")
    assert "outlive" in str(exc.value)


def test_a_delegation_that_never_expires_under_a_parent_that_does(tmp_path):
    """NEVER_EXPIRES is not "later than 40"; it is "no end at all"."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(expires_after_seq=40), actor="control-plane")
    with pytest.raises(BadDelegation):
        led.delegate(_cap(cap_id="c2", subject="helper"),
                     parent_id="c1", actor="agent-1")


def test_a_delegation_cannot_change_the_action(tmp_path):
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    with pytest.raises(BadDelegation) as exc:
        led.delegate(_cap(cap_id="c2", subject="helper",
                          action=Action.WRITE_PATHS),
                     parent_id="c1", actor="agent-1")
    assert "attenuates" in str(exc.value)


def test_a_delegation_cannot_change_the_task(tmp_path):
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    with pytest.raises(BadDelegation) as exc:
        led.delegate(_cap(cap_id="c2", subject="helper", task_id="t2"),
                     parent_id="c1", actor="agent-1")
    assert "task" in str(exc.value)


def test_delegating_a_grant_you_do_not_hold_is_refused(tmp_path):
    """Minting with a chain for cover."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    with pytest.raises(NotTheIssuer) as exc:
        led.delegate(_cap(cap_id="c2", subject="attacker"),
                     parent_id="c1", actor="attacker")
    assert "granted to" in str(exc.value)


def test_a_delegation_by_a_non_holder_fails_at_LOAD_too(tmp_path):
    """delegate() is the honest path, so an attacker writes the record.

    The writer-side check gives a caller a useful error. The reader-side one
    is what actually stops anybody, because nothing compels an attacker to
    call the method that refuses them.
    """
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    child = _cap(cap_id="c2", subject="attacker",
                 issued_seq=log.verify().head_seq + 1, parent_id="c1")
    log.append(actor="attacker", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **child.body()})
    with pytest.raises(NotTheIssuer) as exc:
        CapabilityLedger(log).load()
    assert "do not hold" in str(exc.value) or "granted to" in str(exc.value)


def test_a_forged_delegation_fails_at_load_too(tmp_path):
    """The writer path is the honest one; the reader path is the check."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    wider = _cap(cap_id="c2", subject="attacker", scope=("verification",),
                 issued_seq=log.verify().head_seq + 1, parent_id="c1")
    log.append(actor="agent-1", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **wider.body()})
    with pytest.raises(BadDelegation) as exc:
        CapabilityLedger(log).load()
    assert "outside" in str(exc.value) or "does not" in str(exc.value)


def test_the_second_readers_scope_test_is_on_components_not_strings(tmp_path):
    """``verification/stage10x`` is not under ``verification/stage10``.

    The widening test below uses a scope that a string prefix test ALSO
    rejects, so it cannot tell the two implementations apart. This one uses
    the sibling directory whose name merely starts the same way, which is the
    only shape where the difference shows.
    """
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    sneaky = _cap(cap_id="c2", subject="attacker",
                  scope=("verification/stage10x",),
                  issued_seq=log.verify().head_seq + 1, parent_id="c1")
    log.append(actor="agent-1", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **sneaky.body()})

    recon = reconstruct_subsystems(log)
    assert "c2" not in recon.capabilities
    assert any("stage10x" in a for a in recon.anomalies), recon.anomalies


def test_the_second_reader_catches_a_widening_delegation_independently(
        tmp_path):
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    wider = _cap(cap_id="c2", subject="attacker", scope=("verification",),
                 issued_seq=log.verify().head_seq + 1, parent_id="c1")
    log.append(actor="agent-1", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **wider.body()})

    recon = reconstruct_subsystems(log)
    assert "c2" not in recon.capabilities
    assert any("outside" in a for a in recon.anomalies), recon.anomalies


# --------------------------------------------------------------------------
# The chain is checked at USE time, not only at mint time
# --------------------------------------------------------------------------

def test_revoking_a_parent_revokes_the_delegation(tmp_path):
    """THE property. Revocation that stopped at one node would leave the
    delegate holding authority its source no longer has."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    led.delegate(_cap(cap_id="c2", subject="helper"),
                 parent_id="c1", actor="agent-1")
    req = Request(actor="helper", action=Action.READ_PATHS, task_id="t1",
                  paths=("verification/stage10/x.json",))
    led.in_force().check("c2", req)

    led.revoke("c1", actor="control-plane", reason="the parent is done")

    with pytest.raises(CapabilityRevoked) as exc:
        led.in_force().check("c2", req)
    assert "derives from" in str(exc.value)


def test_an_expired_parent_expires_a_child_that_should_not_exist(tmp_path):
    """Defence in depth, tested where it is actually reachable.

    Attenuation forbids a child outliving its parent, so no HONEST chain
    reaches this arm of the walk -- the child's own window always lapses
    first. A ``CapabilitySet`` a caller assembles is under no such rule, and
    that is exactly the object ``check`` is handed.
    """
    from qta_agent.capability import CapabilitySet
    parent = _cap(cap_id="p", subject="holder", expires_after_seq=10)
    child = _cap(cap_id="c", subject="helper", parent_id="p",
                 expires_after_seq=99)
    caps = CapabilitySet(issued={"p": parent, "c": child},
                         revoked=frozenset(), at_seq=50)
    with pytest.raises(CapabilityExpired) as exc:
        caps.check("c", Request(actor="helper", action=Action.READ_PATHS,
                                task_id="t1",
                                paths=("verification/stage10/x.json",)))
    assert "derives from" in str(exc.value)


def test_an_expired_parent_expires_the_delegation(tmp_path):
    log, led = _ledger(tmp_path)
    head = log.verify().head_seq
    led.issue(_cap(expires_after_seq=head + 3), actor="control-plane")
    led.delegate(_cap(cap_id="c2", subject="helper",
                      expires_after_seq=head + 3),
                 parent_id="c1", actor="agent-1")
    req = Request(actor="helper", action=Action.READ_PATHS, task_id="t1",
                  paths=("verification/stage10/x.json",))

    # The CHILD's own window is checked first and would raise on its own, so
    # ask at a position where only the parent's window has lapsed... which is
    # impossible by construction, since a child may not outlive its parent.
    # That is the point: the two are checked, and the child's is never later.
    with pytest.raises(CapabilityExpired):
        led.in_force(at_seq=head + 99).check("c2", req)


def test_a_chain_with_a_missing_link_grants_nothing(tmp_path):
    """A CapabilitySet assembled by a caller can omit the parent."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(), actor="control-plane")
    led.delegate(_cap(cap_id="c2", subject="helper"),
                 parent_id="c1", actor="agent-1")

    caps = led.in_force()
    trimmed = type(caps)(issued={"c2": caps.issued["c2"]},
                         revoked=caps.revoked, at_seq=caps.at_seq)
    with pytest.raises(CapabilityUnknown) as exc:
        trimmed.check("c2", Request(actor="helper", action=Action.READ_PATHS,
                                    task_id="t1",
                                    paths=("verification/stage10/x.json",)))
    assert "missing link" in str(exc.value)


def test_a_delegation_cycle_is_refused_rather_than_walked(tmp_path):
    """Unreachable through delegate(); reachable through a hostile log."""
    from qta_agent.capability import CapabilitySet
    a = _cap(cap_id="a", parent_id="b", issued_seq=1)
    b = _cap(cap_id="b", parent_id="a", issued_seq=1)
    caps = CapabilitySet(issued={"a": a, "b": b}, revoked=frozenset(),
                         at_seq=5)
    with pytest.raises(Exception) as exc:
        caps.check("a", Request(actor="agent-1", action=Action.READ_PATHS,
                                task_id="t1",
                                paths=("verification/stage10/x.json",)))
    assert "cycle" in str(exc.value)


def test_the_chain_depth_is_bounded(tmp_path):
    """One delegate must not be able to make every check expensive."""
    log, led = _ledger(tmp_path)
    led.issue(_cap(subject="a0"), actor="control-plane")
    parent, made = "c1", 0
    # Delegate until it refuses rather than computing where it should. The
    # property is that the chain is bounded at all; hard-coding the arithmetic
    # would make this test agree with the implementation's off-by-one instead
    # of with the property.
    for i in range(MAX_DELEGATION_DEPTH + 4):
        try:
            led.delegate(_cap(cap_id=f"c-{i}", subject=f"a{i + 1}"),
                         parent_id=parent, actor=f"a{i}")
        except BadDelegation as exc:
            assert "deep" in str(exc) or "bound" in str(exc)
            break
        parent = f"c-{i}"
        made += 1
    else:
        raise AssertionError(
            f"delegation never refused after {made} links; the chain is "
            "unbounded and every check on the leaf walks all of it")
    assert made == MAX_DELEGATION_DEPTH, made

    # And what delegate() ALLOWED, check() must still walk. A writer bound
    # looser than the reader's bound mints grants nothing can use.
    led.in_force().check(
        parent, Request(actor=f"a{made}", action=Action.READ_PATHS,
                        task_id="t1",
                        paths=("verification/stage10/x.json",)))


def test_parent_id_is_part_of_the_digest(tmp_path):
    """Re-parenting a grant must produce a different capability.

    If ``parent_id`` were kept beside the body, a delegation could be moved
    under a wider parent without changing the identity anything cites.
    """
    a = _cap(cap_id="c9", parent_id="p1")
    b = _cap(cap_id="c9", parent_id="p2")
    assert a.digest() != b.digest()
    assert "parent_id" in a.body()


def test_a_capability_cannot_be_its_own_parent(tmp_path):
    with pytest.raises(Exception) as exc:
        _cap(cap_id="c1", parent_id="c1")
    assert "itself" in str(exc.value)
