"""The egress perimeter: what an address is, who may contract a host, and a
malformed record refused in the reducer's own words (AUD-057 to AUD-059).

AUD-057. A grant permits address classes, PUBLIC only by default, because
reaching an inside network from a governed tool is a different act. The
classifier called the shared address space, 100.64.0.0/10, PUBLIC, though
the classes say it is PRIVATE; and it classified an IPv4 address carried in
IPv6 by its IPv6 flags, so ``::ffff:127.0.0.1`` read as PRIVATE rather than
LOOPBACK. Both the decision and the socket guard ask the classifier.

AUD-058. Three malformed network records raised KeyError out of the
reducer, where its refusals are NetworkError.

AUD-059. A service contract narrows what a grant permits on the hosts it
claims. Anyone could register one, under a role registry too, and a request
was decided by the first registered service, by name, among those claiming
its host -- so a wider contract under a name that sorts first lifted a
narrower one's operations and budget.

No test here makes a connection. Where the socket guard is exercised, the
name resolves to the address under test and ``socket.socket.connect`` is
replaced before the guard keeps it as the original: a connection the guard
lets through is recorded, not made.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import roles as R  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.netauth import (  # noqa: E402
    ACT_NET_GRANT, ACT_SERVICE_REGISTER, AddressClass, EgressGrant,
    GuardedConnection, NetworkAuthority, NetworkError, NetworkRequest,
    ServiceOperation, classify_address, grant, parse_target, service,
    socket_guard)
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402

ACTOR, TASK, TOOL = "worker", "t1", "tool"
HOST = "api.example.org"


# ------------------------------------------------- AUD-057: the address
@pytest.mark.parametrize("addr, cls", [
    ("100.64.0.0", "PRIVATE"),
    ("100.64.0.1", "PRIVATE"),
    ("100.100.100.100", "PRIVATE"),
    ("100.127.255.255", "PRIVATE"),
    ("100.63.255.255", "PUBLIC"),           # just outside, either side
    ("100.128.0.0", "PUBLIC"),
    ("::ffff:127.0.0.1", "LOOPBACK"),
    ("::ffff:0.0.0.0", "SPECIAL"),
    ("::ffff:10.0.0.1", "PRIVATE"),
    ("::ffff:169.254.169.254", "PRIVATE"),
    ("::ffff:100.64.0.1", "PRIVATE"),
    ("::ffff:93.184.216.34", "PUBLIC"),
    ("93.184.216.34", "PUBLIC"),            # and the classes as they were
    ("127.0.0.1", "LOOPBACK"),
    ("10.0.0.1", "PRIVATE"),
    ("0.0.0.0", "SPECIAL"),
])
def test_an_address_is_classified_by_where_it_reaches(addr, cls):
    assert classify_address(addr).value == cls


def _auth(classes=(AddressClass.PUBLIC,), *, unpinned=False):
    a = NetworkAuthority()
    a.issue(grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
                  schemes=("https",), hosts=(HOST,), ports=(443,),
                  methods=("GET",), address_classes=classes,
                  allow_unpinned_addresses=unpinned), actor="issuer")
    return a


def _req(resolved=None, path="/v1/data"):
    return NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                          target=parse_target(f"https://{HOST}{path}"),
                          resolved_address=resolved)


@pytest.mark.parametrize("addr", ["100.64.0.1", "100.127.255.254"])
def test_a_public_grant_does_not_reach_shared_address_space(addr):
    d = _auth().authorize(_req(addr))
    assert d.allowed is False
    assert "PRIVATE" in d.reason
    assert _auth().authorize(_req("100.128.0.1")).allowed is True


def test_a_private_grant_does_not_reach_loopback_by_a_mapped_address():
    a = _auth((AddressClass.PRIVATE,))
    assert a.authorize(_req("::ffff:127.0.0.1")).allowed is False
    assert a.authorize(_req("::ffff:0.0.0.0")).allowed is False
    assert a.authorize(_req("::ffff:10.0.0.1")).allowed is True


def _steer(monkeypatch, addr, family=socket.AF_INET) -> list:
    """HOST resolves to ``addr`` alone, and no connection leaves the test."""
    made: list = []
    real = socket.getaddrinfo

    def resolve(name, port, fam=0, socktype=0, proto=0, flags=0):
        if name != HOST:
            return real(name, port, fam, socktype, proto, flags)
        sockaddr = ((addr, port) if family == socket.AF_INET
                    else (addr, port, 0, 0))
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 sockaddr)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect",
                        lambda sock, address: made.append(address))
    return made


def _through_the_guard(a, family=socket.AF_INET) -> str:
    d = a.authorize(_req())
    assert d.allowed, d.reason
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                          allowed=d):
            s.connect((HOST, 443))
        return "CONNECTED"
    except GuardedConnection as exc:
        return f"REFUSED: {exc}"
    finally:
        s.close()


@pytest.mark.parametrize("addr, verdict", [
    ("100.64.0.1", "REFUSED"),
    ("93.184.216.34", "CONNECTED"),
])
def test_the_guard_does_not_connect_a_public_grant_to_shared_address_space(
        monkeypatch, addr, verdict):
    made = _steer(monkeypatch, addr)
    out = _through_the_guard(_auth(unpinned=True))
    assert out.startswith(verdict), out
    assert made == ([] if verdict == "REFUSED" else [(addr, 443)])


def _ipv6_sockets() -> bool:
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _ipv6_sockets(), reason="no IPv6 sockets here")
@pytest.mark.parametrize("classes, verdict", [
    ((AddressClass.PRIVATE,), "REFUSED"),
    ((AddressClass.LOOPBACK,), "CONNECTED"),
])
def test_the_guard_classifies_a_mapped_address_as_what_it_reaches(
        monkeypatch, classes, verdict):
    made = _steer(monkeypatch, "::ffff:127.0.0.1", socket.AF_INET6)
    out = _through_the_guard(_auth(classes, unpinned=True), socket.AF_INET6)
    assert out.startswith(verdict), out
    assert len(made) == (verdict == "CONNECTED")


# --------------------------------------------- AUD-058: the reducer's words
@pytest.mark.parametrize("action, payload, words", [
    (ACT_NET_GRANT, {"revoke": True}, "a revocation names no egress grant"),
    (ACT_NET_GRANT, {"revoke": True, "grant_id": ["g1"]},
     "a revocation names no egress grant"),
    (ACT_NET_GRANT, {"grant_id": "g1"},
     "network.grant carries no grant record"),
    (ACT_SERVICE_REGISTER, {}, "network.service carries no service record"),
])
def test_a_malformed_network_record_is_refused_in_the_reducers_words(
        tmp_path, action, payload, words):
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="someone", action=action, target="t", payload=payload)
    with pytest.raises(NetworkError, match=rf"seq \d+: {words}"):
        NetworkAuthority(log).load()
    assert reconstruct_subsystems(log).anomalies, "the second reader notes it"


# ------------------------------------------ AUD-059: who contracts a host
REG = R.RoleRegistry([
    R.RoleGrant("g-eve", "eve", R.EGRESS_ISSUER, (f"egress:{HOST}",)),
    R.RoleGrant("g-ivy", "ivy", R.EGRESS_ISSUER,
                ("egress:other.example.org",)),
])


def _svc(sid="zz-contract", hosts=(HOST,), path="/v1/read", quota=1):
    return service(service_id=sid, hosts=hosts,
                   operations=(ServiceOperation("GET", path),),
                   quota_per_task=quota)


def _governed(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log, roles=REG).load()
    a.issue(EgressGrant(grant_id="eg1", subject=ACTOR, task_id=TASK,
                        tool_id=TOOL, schemes=("https",), hosts=(HOST,),
                        ports=(443,), methods=("GET",),
                        allow_unpinned_addresses=True), actor="eve")
    return log, a


def _written_around(log, svc, actor):
    log.append(actor=actor, action=ACT_SERVICE_REGISTER,
               target=svc.service_id,
               payload={"service": svc.body(),
                        "service_digest": svc.digest()})


def test_only_an_egress_issuer_over_its_hosts_contracts_a_host(tmp_path):
    log, a = _governed(tmp_path)
    with pytest.raises(NetworkError, match=R.NOT_GRANTED):
        a.register_service(_svc(), actor="mallory")
    with pytest.raises(NetworkError, match=R.OUT_OF_SCOPE):
        a.register_service(_svc(), actor="ivy")
    a.register_service(_svc(), actor="eve")                 # the control
    a.register_service(_svc(sid="other", hosts=("other.example.org",)),
                       actor="ivy")
    again = NetworkAuthority(EventLog(log.path), roles=REG).load()
    assert again.services() == ("other", "zz-contract")


def test_a_contract_nobody_may_make_is_refused_by_both_readers(tmp_path):
    log, _ = _governed(tmp_path)
    _written_around(log, _svc(sid="rogue"), "mallory")
    with pytest.raises(NetworkError, match=R.NOT_GRANTED):
        NetworkAuthority(EventLog(log.path), roles=REG).load()
    sub = reconstruct_subsystems(log, roles=REG)
    assert "rogue" not in sub.services
    assert any(R.NOT_GRANTED in x for x in sub.anomalies), sub.anomalies


@pytest.mark.parametrize("a, b, overlap", [
    (HOST, HOST, True),
    ("API.Example.org.", HOST, True),
    (HOST, "other.example.org", False),
    ("*.example.org", HOST, True),
    ("*.example.org", "example.org", False),
    ("*.example.org", "*.api.example.org", True),
    ("*.org", "*.example.org", True),
    ("*.a.org", "*.b.org", False),
])
def test_when_two_host_patterns_overlap(a, b, overlap):
    from agent_substrate.netauth import patterns_overlap
    from agent_substrate.reconstruct import _sub_hosts_overlap
    assert patterns_overlap(a, b) is overlap
    assert patterns_overlap(b, a) is overlap
    assert _sub_hosts_overlap(a, b) is overlap
    assert _sub_hosts_overlap(b, a) is overlap


def test_a_second_contract_for_a_contracted_host_is_refused(tmp_path):
    """The displacement, as it was: a wider contract under a name that sorts
    first decided instead of the narrower one in force."""
    log, a = _governed(tmp_path)
    a.register_service(_svc(), actor="eve")
    assert a.authorize(_req(path="/v1/admin")).allowed is False
    wider = _svc(sid="aa-anything", path="/", quota=1000)
    with pytest.raises(NetworkError, match="decided by one contract"):
        a.register_service(wider, actor="eve")
    a.register_service(_svc(), actor="eve")       # the same, again: not two
    _written_around(log, wider, "eve")
    with pytest.raises(NetworkError, match="decided by one contract"):
        NetworkAuthority(EventLog(log.path), roles=REG).load()
    sub = reconstruct_subsystems(log, roles=REG)
    assert set(sub.services) == {"zz-contract"}
    assert any("decided by one contract" in x for x in sub.anomalies)


def test_one_host_one_contract_holds_with_no_role_registry(tmp_path):
    a = NetworkAuthority(EventLog(tmp_path / "log.jsonl")).load()
    a.register_service(_svc(hosts=("*.example.org",)), actor="anyone")
    with pytest.raises(NetworkError, match="decided by one contract"):
        a.register_service(_svc(sid="aa"), actor="anyone")
    local = NetworkAuthority()                       # and with no log at all
    local.register_service(_svc(), actor="anyone")
    with pytest.raises(NetworkError, match="decided by one contract"):
        local.register_service(_svc(sid="aa"), actor="anyone")
