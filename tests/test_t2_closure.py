"""T-2: authority decided against the history the decision is appended onto.

D-2026-156. D-2026-147 moved the agent directory, the memory store and the
policy store onto EventLog.append_decided. Finding T-2 named the writers it
left: the capability ledger, the egress authority, the secret store and the
governed runner. Measured at b9b2187, each case below went wrong: its 12
cases fail there and pass on the repair, and its 6 controls pass on both.

* A ledger loaded before another process revoked a capability, egress grant
  or secret grant still found it in force. It authorized the tool run, the
  connection or the reveal of the plaintext. The capability and the egress
  grant left no record any reader could catch afterwards.
* Two authorities each loaded one call below a service's budget, and both
  calls were allowed and recorded. The budget was overrun, and only the
  second reader noticed, afterwards.
* A service registered with other terms from a stale view was appended, and
  every later load() refused the history.
* One secret id was provisioned from two different providers. Holders in two
  processes resolved different credentials under one grant, and nothing
  refused it.

Each case makes the race deterministic: two views are loaded from one log,
the first writes, and the second, now stale, decides. Each has a control
that differs only in being fresh. A refused race leaves a history that loads
and that the independent reader agrees with.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_substrate import reconstruct as rc
from agent_substrate.capability import (Action, CapabilityLedger,
                                        CapabilityRevoked, Request, issue)
from agent_substrate.events import EventLog
from agent_substrate.netauth import (NetworkAuthority, NetworkError,
                                     NetworkRequest, ServiceOperation, grant,
                                     parse_target, service)
from agent_substrate.secrets import (MappingSecretProvider, SecretError,
                                     SecretRevoked, SecretStore)
from agent_substrate.secrets import grant as secret_grant

ACTOR, TASK, TOOL = "worker-1", "task-1", "fetch.schema"


def _log(tmp_path: Path) -> Path:
    return tmp_path / "log.jsonl"


# ---- capabilities --------------------------------------------------------------

def _caps(p: Path) -> CapabilityLedger:
    led = CapabilityLedger(EventLog(p)).load()
    led.anoint(actor="root")
    led.issue(issue(capability_id="c1", subject=ACTOR,
                    action=Action.EXECUTE_TOOL, task_id=TASK, tool_id=TOOL,
                    scope=("out",), issued_seq=0), actor="root")
    return led


def _use() -> Request:
    return Request(actor=ACTOR, action=Action.EXECUTE_TOOL, task_id=TASK,
                   tool_id=TOOL, paths=("out",))


def _head(p: Path) -> int:
    return EventLog(p).verify().head_seq


def test_a_capability_revoked_elsewhere_is_not_in_force_now(tmp_path):
    """The governed runner asks ``in_force(head)``, at the log's position,
    of the ledger it loaded. Measured: the revocation another process
    recorded was not in it, and the check passed."""
    p = _log(tmp_path)
    a = _caps(p)
    CapabilityLedger(EventLog(p)).load().revoke("c1", actor="root",
                                                reason="withdrawn")
    with pytest.raises(CapabilityRevoked):
        a.in_force(_head(p)).check("c1", _use())


def test_control_a_capability_nobody_revoked_is_in_force(tmp_path):
    p = _log(tmp_path)
    a = _caps(p)
    CapabilityLedger(EventLog(p)).load()      # another reader, no write
    assert a.in_force(_head(p)).check("c1", _use()).capability_id == "c1"


def test_a_stale_revocation_of_the_same_grant_is_refused_and_loads(tmp_path):
    p = _log(tmp_path)
    _caps(p)
    a = CapabilityLedger(EventLog(p)).load()
    b = CapabilityLedger(EventLog(p)).load()
    a.revoke("c1", actor="root", reason="first")
    with pytest.raises(CapabilityRevoked, match="already"):
        b.revoke("c1", actor="root", reason="second")
    led = CapabilityLedger(EventLog(p)).load()
    assert led.revoked_ids() == ("c1",)
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


# ---- egress --------------------------------------------------------------------

def test_a_stale_revocation_of_one_egress_grant_is_refused_and_loads(
        tmp_path):
    p = _log(tmp_path)
    _egress(p)
    a = NetworkAuthority(EventLog(p)).load()
    b = NetworkAuthority(EventLog(p)).load()
    a.revoke("g1", actor="scheduler", reason="first")
    with pytest.raises(NetworkError, match="already revoked"):
        b.revoke("g1", actor="scheduler", reason="second")
    assert NetworkAuthority(EventLog(p)).load().authorize(_call()).allowed \
        is False
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


def test_a_stale_revocation_of_one_secret_grant_is_refused_and_loads(
        tmp_path):
    from agent_substrate.secrets import SecretRevoked

    p = _log(tmp_path)
    _secrets(p)
    a = SecretStore(EventLog(p)).load()
    b = SecretStore(EventLog(p)).load()
    a.revoke("sg1", actor="owner", reason="first")
    with pytest.raises(SecretRevoked, match="already revoked"):
        b.revoke("sg1", actor="owner", reason="second")
    _, events = EventLog(p).read_verified()
    assert len([e for e in events if e.action == "secret.grant"]) == 2
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


def _egress(p: Path, *, quota: int = 2) -> NetworkAuthority:
    a = NetworkAuthority(EventLog(p)).load()
    a.issue(grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
                  schemes=("https",), hosts=("api.example.com",),
                  ports=(443,), methods=("GET",), paths=("/v1",)),
            actor="scheduler")
    a.register_service(_service(quota), actor="scheduler")
    return a


def _service(quota: int = 2, description: str = ""):
    return service(service_id="registrar", hosts=("api.example.com",),
                   operations=(ServiceOperation("GET", "/v1/records"),),
                   quota_per_task=quota, description=description)


def _call() -> NetworkRequest:
    return NetworkRequest(
        actor=ACTOR, task_id=TASK, tool_id=TOOL,
        target=parse_target("https://api.example.com/v1/records"))


def test_an_egress_grant_revoked_elsewhere_authorizes_nothing(tmp_path):
    p = _log(tmp_path)
    a = _egress(p)
    NetworkAuthority(EventLog(p)).load().revoke("g1", actor="scheduler",
                                                reason="withdrawn")
    d = a.authorize(_call())
    assert d.allowed is False and "revoked" in d.reason


def test_control_an_unrevoked_egress_grant_authorizes(tmp_path):
    p = _log(tmp_path)
    a = _egress(p)
    NetworkAuthority(EventLog(p)).load()
    assert a.authorize(_call()).allowed is True


def test_a_spent_budget_is_spent_for_every_process(tmp_path):
    """Both views at one call below the budget. The first call is recorded;
    the second view's call must be refused, and the record must say so."""
    p = _log(tmp_path)
    _egress(p, quota=1)
    a = NetworkAuthority(EventLog(p)).load()
    b = NetworkAuthority(EventLog(p)).load()
    first = a.record(_call(), a.authorize(_call()), actor=ACTOR)
    assert first.allowed is True
    second = b.record(_call(), b.authorize(_call()), actor=ACTOR)
    assert second.allowed is False
    led = NetworkAuthority(EventLog(p)).load()
    assert led.calls_made("registrar", TASK) == 1
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


def test_a_decision_that_no_longer_holds_is_recorded_as_refused(tmp_path):
    """The window the fold cannot close: authorized, then revoked by another
    process, then recorded. ``record`` decides again under the lock."""
    p = _log(tmp_path)
    a = _egress(p)
    decision = a.authorize(_call())
    assert decision.allowed is True
    NetworkAuthority(EventLog(p)).load().revoke("g1", actor="scheduler",
                                                reason="withdrawn")
    recorded = a.record(_call(), decision, actor=ACTOR)
    assert recorded.allowed is False and "revoked" in recorded.reason
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


def test_control_a_decision_that_still_holds_is_recorded_allowed(tmp_path):
    p = _log(tmp_path)
    a = _egress(p)
    recorded = a.record(_call(), a.authorize(_call()), actor=ACTOR)
    assert recorded.allowed is True
    assert NetworkAuthority(EventLog(p)).load().calls_made(
        "registrar", TASK) == 1


def test_a_conflicting_registration_from_a_stale_view_is_refused(tmp_path):
    """Measured: appended, and every later load() refused the history."""
    p = _log(tmp_path)
    a = NetworkAuthority(EventLog(p)).load()
    b = NetworkAuthority(EventLog(p)).load()
    a.register_service(_service(2), actor="scheduler")
    with pytest.raises(NetworkError, match="different terms"):
        b.register_service(_service(3), actor="scheduler")
    led = NetworkAuthority(EventLog(p)).load()
    assert led.service("registrar").quota_per_task == 2
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


def test_control_the_same_registration_from_a_stale_view_is_accepted(
        tmp_path):
    p = _log(tmp_path)
    a = NetworkAuthority(EventLog(p)).load()
    b = NetworkAuthority(EventLog(p)).load()
    a.register_service(_service(2), actor="scheduler")
    b.register_service(_service(2), actor="scheduler")
    assert NetworkAuthority(EventLog(p)).load().service(
        "registrar").quota_per_task == 2


def test_the_second_reader_flags_egress_allowed_under_a_revoked_grant(
        tmp_path):
    """An allowed request recorded after its grant's revocation, written by
    something other than the authority. The second reader held the grant
    table and never consulted it for a use."""
    p = _log(tmp_path)
    a = _egress(p)
    allowed = a.authorize(_call())
    a.revoke("g1", actor="scheduler", reason="withdrawn")
    EventLog(p).append(
        actor=ACTOR, action="network.request", target=TASK,
        payload={"request": _call().to_record(),
                 "decision": allowed.to_record(), "service_id": "registrar",
                 "task_id": TASK, "allowed": True})
    found = rc.reconstruct_subsystems(EventLog(p)).anomalies
    assert any("g1" in f and "revoked" in f for f in found), found


# ---- secrets -------------------------------------------------------------------

VALUE = "hunter2-super-secret-token-value"


def _secrets(p: Path) -> SecretStore:
    s = SecretStore(EventLog(p)).load()
    s.register("api-token", VALUE)
    s.issue(secret_grant(grant_id="sg1", subject=ACTOR, task_id=TASK,
                         tool_id=TOOL, secret_id="api-token",
                         purposes=("call-schema-api",)), actor="owner")
    return s


def _reveal(s: SecretStore) -> str:
    from agent_substrate.secrets import SecretRef
    return s.resolve(SecretRef("api-token"), grant_id="sg1", actor=ACTOR,
                     task_id=TASK, tool_id=TOOL,
                     purpose="call-schema-api").reveal()


def test_a_secret_grant_revoked_elsewhere_reveals_nothing(tmp_path):
    """Measured: the plaintext was returned, and the second reader could
    only say afterwards that a revoked grant had been used."""
    p = _log(tmp_path)
    a = _secrets(p)
    SecretStore(EventLog(p)).load().revoke("sg1", actor="owner",
                                           reason="rotated")
    with pytest.raises(SecretRevoked):
        _reveal(a)
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


def test_control_an_unrevoked_secret_grant_reveals(tmp_path):
    p = _log(tmp_path)
    a = _secrets(p)
    SecretStore(EventLog(p)).load()
    assert _reveal(a) == VALUE


def test_one_secret_id_is_provisioned_from_one_source(tmp_path):
    """Two processes provisioning the same id from DIFFERENT providers: a
    holder in each would resolve a different credential under one grant."""
    p = _log(tmp_path)
    a = SecretStore(EventLog(p)).load()
    b = SecretStore(EventLog(p)).load()
    a.provision(MappingSecretProvider({"tok": "value-one"}, source="vault-a"),
                "tok")
    with pytest.raises(SecretError, match="provisioned from"):
        b.provision(MappingSecretProvider({"tok": "value-two"},
                                          source="vault-b"), "tok")
    assert "tok" not in b.secret_ids()
    assert not rc.reconstruct_subsystems(EventLog(p)).anomalies


def test_the_second_reader_flags_one_secret_from_two_sources(tmp_path):
    """The writer refuses the race above; a record written outside it is
    refused on replay and reported by the second reader."""
    p = _log(tmp_path)
    SecretStore(EventLog(p)).load().provision(
        MappingSecretProvider({"tok": "value-one"}, source="vault-a"), "tok")
    log = EventLog(p)
    _, events = log.read_verified()
    (first,) = [e for e in events if e.action == "secret.provision"]
    forged = dict(first.payload,
                  provider=dict(first.payload["provider"], source="vault-b"))
    log.append(actor=first.actor, action=first.action, target=first.target,
               payload=forged)
    with pytest.raises(SecretError, match="provisioned from"):
        SecretStore(EventLog(p)).load()
    assert any("provisioned from" in a
               for a in rc.reconstruct_subsystems(EventLog(p)).anomalies)


def test_control_the_same_source_may_provision_in_another_process(tmp_path):
    p = _log(tmp_path)
    a = SecretStore(EventLog(p)).load()
    b = SecretStore(EventLog(p)).load()
    a.provision(MappingSecretProvider({"tok": "value-one"}, source="vault-a"), "tok")
    b.provision(MappingSecretProvider({"tok": "value-one"}, source="vault-a"), "tok")
    assert "tok" in b.secret_ids()


# ---- one id, one grant -----------------------------------------------------------

def test_a_capability_id_issued_elsewhere_is_refused_and_loads(tmp_path):
    """Measured: the second issuance was appended, stamped at another
    position, and replay refused the history ("issued twice with
    different terms")."""
    p = _log(tmp_path)
    _caps(p)
    a = CapabilityLedger(EventLog(p)).load()
    b = CapabilityLedger(EventLog(p)).load()
    cap = issue(capability_id="c2", subject=ACTOR, action=Action.EXECUTE_TOOL,
                task_id=TASK, tool_id=TOOL, scope=("out",), issued_seq=0)
    a.issue(cap, actor="root")
    with pytest.raises(Exception, match="already exists"):
        b.issue(cap, actor="root")
    assert "c2" in CapabilityLedger(EventLog(p)).load().issued_ids()


def test_an_egress_grant_id_issued_elsewhere_is_refused_and_loads(tmp_path):
    p = _log(tmp_path)
    a = NetworkAuthority(EventLog(p)).load()
    b = NetworkAuthority(EventLog(p)).load()
    g = grant(grant_id="g9", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("https",), hosts=("api.example.com",), ports=(443,),
              methods=("GET",))
    a.issue(g, actor="scheduler")
    with pytest.raises(NetworkError, match="already exists"):
        b.issue(g, actor="scheduler")
    NetworkAuthority(EventLog(p)).load()


def test_a_secret_grant_id_issued_elsewhere_is_refused_and_loads(tmp_path):
    p = _log(tmp_path)
    a = SecretStore(EventLog(p)).load()
    b = SecretStore(EventLog(p)).load()
    for s in (a, b):
        s.register("api-token", VALUE)
    g = secret_grant(grant_id="sg9", subject=ACTOR, task_id=TASK,
                     tool_id=TOOL, secret_id="api-token",
                     purposes=("call-schema-api",))
    a.issue(g, actor="owner")
    with pytest.raises(SecretError, match="already exists"):
        b.issue(g, actor="owner")
    SecretStore(EventLog(p)).load()
