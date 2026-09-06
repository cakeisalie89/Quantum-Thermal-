"""Network authority, attacked the way host allowlists actually get past.

Almost every bypass in this file is a string that looks like the allowed
destination to one parser and resolves elsewhere in another. That is the whole
threat model for an egress allowlist, so the tests are mostly adversarial
strings rather than happy-path requests.
"""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.events import EventLog  # noqa: E402
from qta_agent.netauth import (  # noqa: E402
    ACT_NET_REQUEST, AddressClass, Direction, EgressGrant, GuardedConnection,
    MalformedTarget, NetworkAuthority, NetworkDenied, NetworkError,
    NetworkRequest, classify_address, grant, grant_from_record, host_matches,
    parse_target, socket_guard,
)

ACTOR = "agent-worker-1"
TASK = "task-1"
TOOL = "fetch.schema"


def _grant(**over):
    kw = dict(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("https",), hosts=("api.example.com",), ports=(443,),
              methods=("GET",))
    kw.update(over)
    return grant(**kw)


def _auth(g=None, *, log=None):
    a = NetworkAuthority(log)
    a.issue(g or _grant(), actor="scheduler")
    return a


def _req(url="https://api.example.com/v1/schema", method="GET", *,
         actor=ACTOR, task=TASK, tool=TOOL, resolved=None):
    return NetworkRequest(actor=actor, task_id=task, tool_id=tool,
                          target=parse_target(url, method=method),
                          resolved_address=resolved)


# ---- the default ---------------------------------------------------------
def test_with_no_grant_there_is_no_network():
    a = NetworkAuthority()
    d = a.authorize(_req())
    assert d.allowed is False
    assert "default is no network" in d.reason


def test_a_covered_request_is_allowed_and_says_why():
    d = _auth().authorize(_req())
    assert d.allowed is True
    assert d.grant_id == "g1"
    assert "api.example.com:443" in d.reason


def test_denial_raises_something_a_retry_loop_will_not_swallow():
    """NetworkDenied is deliberately not an OSError."""
    assert not issubclass(NetworkDenied, OSError)
    with pytest.raises(NetworkDenied):
        NetworkAuthority().authorize(_req()).raise_if_denied()


# ---- host matching -------------------------------------------------------
@pytest.mark.parametrize("host", [
    "api.example.com.evil.test",     # allowed name as a left prefix
    "evil-api.example.com",          # allowed name as a right suffix-ish
    "notapi.example.com",
    "api.example.com.",              # differs only by the trailing dot...
    "xapi.example.com",
    "api.example.como",
    "api-example.com",
])
def test_hosts_that_merely_look_like_the_allowed_one(host):
    d = _auth().authorize(_req(f"https://{host}/v1/schema"))
    if host == "api.example.com.":
        # ...and a trailing dot is the SAME name to a resolver, so it must be
        # allowed rather than treated as a different host.
        assert d.allowed is True
    else:
        assert d.allowed is False, f"{host} slipped past the allowlist"


def test_host_matching_is_case_insensitive():
    assert _auth().authorize(_req("https://API.Example.COM/v1")).allowed


@pytest.mark.parametrize("pattern,host,expected", [
    ("example.com", "example.com", True),
    ("example.com", "sub.example.com", False),
    ("*.example.com", "sub.example.com", True),
    ("*.example.com", "a.b.example.com", True),
    ("*.example.com", "example.com", False),
    ("*.example.com", "notexample.com", False),
    ("*.example.com", "example.com.evil.test", False),
    ("example.com", "EXAMPLE.COM.", True),
    ("*", "anything.test", False),
])
def test_host_matches_by_label(pattern, host, expected):
    assert host_matches(pattern, host) is expected


def test_a_wildcard_does_not_cover_its_own_apex():
    """Granting subdomains and granting the apex are separate decisions."""
    a = _auth(_grant(hosts=("*.example.com",)))
    assert a.authorize(_req("https://api.example.com/v1")).allowed is True
    assert a.authorize(_req("https://example.com/v1")).allowed is False


def test_a_total_host_grant_is_refused():
    with pytest.raises(NetworkError, match="entire internet"):
        _grant(hosts=("*",))


@pytest.mark.parametrize("bad", ["", ".", "a..b", "..", "a.*.b"])
def test_malformed_host_patterns_are_refused(bad):
    with pytest.raises(NetworkError):
        _grant(hosts=(bad,))


def test_an_empty_host_list_is_refused():
    with pytest.raises(NetworkError, match="empty allowlist"):
        _grant(hosts=())


# ---- URL parsing ---------------------------------------------------------
def test_userinfo_before_the_host_is_refused():
    """'https://api.example.com@evil.test/' reads as the allowed host."""
    with pytest.raises(MalformedTarget, match="userinfo"):
        parse_target("https://api.example.com@evil.test/v1")


@pytest.mark.parametrize("url", [
    "", "not-a-url", "/only/a/path", "https://", "//example.com/x",
    "https://example.com:notaport/x", "https://example.com:0/x",
    "https://example.com:99999/x", "ftp://example.com/x",
])
def test_unparseable_or_undecidable_urls_are_refused(url):
    with pytest.raises(MalformedTarget):
        parse_target(url)


def test_an_oversized_url_is_refused():
    with pytest.raises(MalformedTarget, match="above the"):
        parse_target("https://api.example.com/" + "a" * 9000)


def test_an_unknown_method_is_refused_rather_than_assumed_safe():
    with pytest.raises(MalformedTarget, match="cannot be classified"):
        parse_target("https://api.example.com/v1", method="TRACE")


def test_the_default_port_is_a_recorded_decision():
    assert parse_target("https://api.example.com/v1").port == 443
    assert parse_target("http://api.example.com/v1").port == 80
    assert parse_target("https://api.example.com:8443/v1").port == 8443


def test_percent_encoded_paths_are_compared_as_written():
    """A grant on /v1 must not be widened by an encoded traversal."""
    a = _auth(_grant(paths=("/v1",)))
    assert a.authorize(_req("https://api.example.com/v1/schema")).allowed
    for path in ("/v2", "/v10", "/%2e%2e/v2", "/v1%2f../v2"):
        d = a.authorize(_req(f"https://api.example.com{path}"))
        assert d.allowed is False, path


def test_a_literal_traversal_does_not_climb_out_of_the_granted_prefix():
    """``/v1/../admin`` has ``/v1`` among its parents.

    That is not a quirk of this implementation -- ``PurePosixPath`` says the
    parents of ``/v1/../admin`` are ``/v1/..``, ``/v1`` and ``/``, so a
    component-prefix check ALONE accepts it. The traversal refusal is what
    stops it, and the earlier encoded-form cases never reached that branch,
    so it went untested.
    """
    a = _auth(_grant(paths=("/v1",)))
    for path in ("/v1/../admin", "/v1/./../admin", "/v1/sub/../../admin"):
        d = a.authorize(_req(f"https://api.example.com{path}"))
        assert d.allowed is False, f"{path} climbed out of the granted prefix"
        assert "does not cover path" in d.reason


def test_a_dot_segment_is_harmless_and_a_dotdot_segment_is_not():
    """The asymmetry, written down so the guard's shape is not a mystery.

    ``PurePosixPath`` drops "." from ``parts`` and keeps "..". That is also
    why a "." segment does not need refusing: "/v1/./x" is inside "/v1"
    under either reading. "/v1/../admin" is not, and its parents include
    "/v1", so the component-prefix test accepts it unless something else
    refuses the traversal.
    """
    from pathlib import PurePosixPath

    assert PurePosixPath("/v1/./x").parts == ("/", "v1", "x")
    assert ".." in PurePosixPath("/v1/../admin").parts
    assert PurePosixPath("/v1") in PurePosixPath("/v1/../admin").parents

    a = _auth(_grant(paths=("/v1",)))
    assert a.authorize(_req("https://api.example.com/v1/./schema")).allowed
    assert not a.authorize(_req("https://api.example.com/./v2")).allowed


def test_path_prefixes_match_by_component():
    a = _auth(_grant(paths=("/v1",)))
    assert a.authorize(_req("https://api.example.com/v1")).allowed
    assert a.authorize(_req("https://api.example.com/v1/x/y")).allowed
    assert not a.authorize(_req("https://api.example.com/v10")).allowed


def test_a_traversing_path_prefix_is_refused_at_construction():
    with pytest.raises(NetworkError, match=r"'\.\.' or '\.'"):
        _grant(paths=("/v1/../admin",))
    with pytest.raises(NetworkError, match="absolute path"):
        _grant(paths=("v1",))


# ---- scheme, port, method -----------------------------------------------
def test_https_does_not_imply_http():
    """The port is held constant so the SCHEME is the only difference.

    Written with port 80 first, which the port check refused on its own --
    the test passed with the scheme check deleted, because a neighbouring
    guard was doing the work.
    """
    a = _auth()
    d = a.authorize(_req("http://api.example.com:443/v1"))
    assert d.allowed is False
    assert "schemes" in d.reason, d.reason
    assert a.authorize(_req("https://api.example.com:443/v1")).allowed


def test_an_alternate_port_is_a_different_destination():
    a = _auth()
    d = a.authorize(_req("https://api.example.com:8443/v1"))
    assert d.allowed is False and "ports" in d.reason


def test_a_mutating_method_is_never_implied_by_a_safe_one():
    a = _auth(_grant(methods=("GET", "HEAD")))
    d = a.authorize(_req(method="POST"))
    assert d.allowed is False
    assert "never implied" in d.reason


def test_a_granted_mutating_method_is_allowed():
    a = _auth(_grant(methods=("GET", "POST")))
    assert a.authorize(_req(method="POST")).allowed is True


def test_an_unknown_scheme_cannot_be_granted():
    with pytest.raises(NetworkError, match="not one this layer"):
        _grant(schemes=("gopher",))


def test_a_grant_must_name_its_ports_and_methods():
    with pytest.raises(NetworkError, match="must name its ports"):
        _grant(ports=())
    with pytest.raises(NetworkError, match="at least one method"):
        _grant(methods=())


# ---- who and what the grant is bound to ---------------------------------
def test_a_grant_is_not_portable_between_actors():
    d = _auth().authorize(_req(actor="agent-worker-2"))
    assert d.allowed is False and "granted to" in d.reason


def test_a_grant_is_not_portable_between_tasks():
    d = _auth().authorize(_req(task="task-2"))
    assert d.allowed is False and "confined to task" in d.reason


def test_a_grant_is_not_portable_between_tools():
    d = _auth().authorize(_req(tool="exfiltrate"))
    assert d.allowed is False and "permits tool" in d.reason


def test_an_expired_grant_authorizes_nothing():
    a = _auth(_grant(issued_seq=1, expires_after_seq=10))
    a.set_position(10)
    assert a.authorize(_req()).allowed is True
    a.set_position(11)
    d = a.authorize(_req())
    assert d.allowed is False and "expired" in d.reason


def test_a_revoked_grant_authorizes_nothing(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = _auth(log=log)
    assert a.authorize(_req()).allowed is True
    a.revoke("g1", actor="owner", reason="rotated")
    d = a.authorize(_req())
    assert d.allowed is False and "revoked" in d.reason


def test_a_grant_that_would_expire_before_issue_is_refused():
    with pytest.raises(NetworkError, match="never valid"):
        _grant(issued_seq=10, expires_after_seq=9)


def test_reusing_a_grant_id_is_refused():
    a = _auth()
    with pytest.raises(NetworkError, match="already exists"):
        a.issue(_grant(hosts=("other.example.com",)), actor="scheduler")


# ---- addresses -----------------------------------------------------------
@pytest.mark.parametrize("addr,expected", [
    ("127.0.0.1", AddressClass.LOOPBACK),
    ("127.1.2.3", AddressClass.LOOPBACK),
    ("::1", AddressClass.LOOPBACK),
    ("10.0.0.1", AddressClass.PRIVATE),
    ("192.168.1.1", AddressClass.PRIVATE),
    ("169.254.1.1", AddressClass.PRIVATE),
    ("fd00::1", AddressClass.PRIVATE),
    ("224.0.0.1", AddressClass.SPECIAL),
    ("0.0.0.0", AddressClass.SPECIAL),
    ("93.184.216.34", AddressClass.PUBLIC),
    ("2606:2800:220:1:248:1893:25c8:1946", AddressClass.PUBLIC),
])
def test_address_classification(addr, expected):
    assert classify_address(addr) is expected


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.2", "[::1]", "0.0.0.0", "10.0.0.5",
    "169.254.169.254",
])
def test_loopback_and_private_targets_are_not_public(host):
    """The metadata endpoint and the loopback aliases, by literal address."""
    a = _auth(_grant(hosts=("api.example.com", "*.example.com"),
                     address_classes=(AddressClass.PUBLIC,)))
    d = a.authorize(_req(f"https://{host}/v1"))
    assert d.allowed is False, host


def test_loopback_is_reachable_only_when_explicitly_granted():
    g = grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("http",), hosts=("localhost",), ports=(8080,),
              methods=("GET",),
              address_classes=(AddressClass.LOOPBACK,))
    a = NetworkAuthority()
    a.issue(g, actor="scheduler")
    d = a.authorize(_req("http://localhost:8080/health", resolved="127.0.0.1"))
    assert d.allowed is True


def test_a_name_that_resolves_outside_the_pinned_addresses_is_refused():
    """The rebinding signature, at the policy layer.

    The substitute address is deliberately another PUBLIC one: an address
    refused for its class would prove nothing about the pin, because the class
    check runs first.
    """
    a = _auth(_grant(addresses=("93.184.216.34",)))
    assert a.authorize(_req(resolved="93.184.216.34")).allowed is True
    d = a.authorize(_req(resolved="93.184.216.35"))
    assert d.allowed is False
    assert "answer changed between the check and the connect" in d.reason


def test_the_metadata_endpoint_is_refused_by_class_before_any_pin():
    """169.254.169.254 is the address an exfiltration attempt asks for."""
    d = _auth().authorize(_req(resolved="169.254.169.254"))
    assert d.allowed is False
    assert "PRIVATE" in d.reason


def test_a_pinned_address_must_be_an_ip():
    with pytest.raises(NetworkError, match="not an IP address"):
        _grant(addresses=("example.com",))


# ---- redirection ---------------------------------------------------------
def test_a_redirect_target_is_a_new_decision_not_an_inherited_one():
    """Following a redirect is making a second request."""
    a = _auth()
    assert a.authorize(_req()).allowed is True
    redirected = a.authorize(_req("https://evil.test/v1/schema"))
    assert redirected.allowed is False, (
        "authority must not flow to wherever the first response points; a "
        "redirect is a destination the grant never named")


# ---- audit ---------------------------------------------------------------
def test_attempts_are_recorded_including_refusals(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = _auth(log=log)
    for url in ("https://api.example.com/v1", "https://evil.test/v1"):
        req = _req(url)
        a.record(req, a.authorize(req), actor=ACTOR)
    recorded = [ev for ev in log.read() if ev.action == ACT_NET_REQUEST]
    assert len(recorded) == 2
    assert recorded[0].payload["decision"]["allowed"] is True
    assert recorded[1].payload["decision"]["allowed"] is False
    assert recorded[1].payload["request"]["target"]["host"] == "evil.test"


def test_the_audit_trail_carries_no_payload(tmp_path):
    """An audit record holding request bodies is a second copy of the
    secrets they contained."""
    log = EventLog(tmp_path / "log.jsonl")
    a = _auth(log=log)
    req = _req()
    a.record(req, a.authorize(req), actor=ACTOR)
    rec = [ev for ev in log.read() if ev.action == ACT_NET_REQUEST][0]
    flat = repr(rec.payload)
    for forbidden in ("headers", "body", "authorization", "cookie"):
        assert forbidden not in flat.lower()


def test_grants_survive_a_restart(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = _auth(log=log)
    revived = NetworkAuthority(EventLog(tmp_path / "log.jsonl")).load()
    assert revived.authorize(_req()).allowed is True
    assert revived._grants["g1"].digest() == a._grants["g1"].digest()


def test_a_grant_record_whose_digest_disagrees_is_refused(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = _auth(log=log)
    g = a._grants["g1"]
    body = g.body()
    body["hosts"] = ["evil.test"]
    log.append(actor="mallory", action="network.grant", target=TASK,
               payload={"grant": body, "grant_digest": g.digest(),
                        "grant_id": "g2"})
    with pytest.raises(NetworkError, match="hashes to"):
        NetworkAuthority(EventLog(tmp_path / "log.jsonl")).load()


def test_a_grant_record_with_unknown_fields_is_refused():
    rec = _grant().body()
    rec["bypass_all_checks"] = True
    with pytest.raises(NetworkError, match="unknown fields"):
        grant_from_record(rec)


def test_grant_record_roundtrip_preserves_the_digest():
    g = _grant(paths=("/v1",), addresses=("93.184.216.34",))
    assert grant_from_record(g.body()).digest() == g.digest()


# ---- the process layer ---------------------------------------------------
@pytest.fixture()
def listener():
    """A real local server, so the guard is tested against real sockets."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (TimeoutError, OSError):
                continue
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield srv.getsockname()
    stop.set()
    thread.join(timeout=2)
    srv.close()


def _connect(addr):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(addr)
    finally:
        s.close()


def test_the_guard_refuses_a_connection_no_grant_covers(listener):
    a = NetworkAuthority()
    with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL):
        with pytest.raises(GuardedConnection, match="not authorized"):
            _connect(listener)


def test_the_guard_permits_a_pinned_address(listener):
    host, port = listener
    g = grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("http",), hosts=("localhost",), ports=(port,),
              methods=("GET",),
              address_classes=(AddressClass.LOOPBACK,), addresses=(host,))
    a = NetworkAuthority()
    a.issue(g, actor="scheduler")
    decision = a.authorize(
        _req(f"http://localhost:{port}/health", resolved=host))
    assert decision.allowed is True
    with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                      allowed=decision):
        _connect(listener)


def test_the_guard_refuses_an_address_outside_the_pinned_set(listener):
    """Rebinding, at the socket layer: the name checked out, the address did
    not."""
    host, port = listener
    g = grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("http",), hosts=("localhost",), ports=(port,),
              methods=("GET",), address_classes=(AddressClass.LOOPBACK,),
              addresses=("127.0.0.9",))
    a = NetworkAuthority()
    a.issue(g, actor="scheduler")
    decision = a.authorize(
        _req(f"http://localhost:{port}/health", resolved="127.0.0.9"))
    assert decision.allowed is True
    with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                      allowed=decision):
        with pytest.raises(GuardedConnection, match="rebinding"):
            _connect((host, port))


def test_an_unpinned_grant_is_refused_at_the_socket_unless_accepted(listener):
    host, port = listener
    base = dict(subject=ACTOR, task_id=TASK, tool_id=TOOL, schemes=("http",),
                hosts=("localhost",), ports=(port,), methods=("GET",),
                address_classes=(AddressClass.LOOPBACK,))
    strict = NetworkAuthority()
    strict.issue(grant(grant_id="g1", **base), actor="scheduler")
    d = strict.authorize(_req(f"http://localhost:{port}/x", resolved=host))
    with socket_guard(strict, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                      allowed=d):
        with pytest.raises(GuardedConnection, match="cannot be checked"):
            _connect(listener)

    lax = NetworkAuthority()
    lax.issue(grant(grant_id="g1", allow_unpinned_addresses=True, **base),
              actor="scheduler")
    d2 = lax.authorize(_req(f"http://localhost:{port}/x", resolved=host))
    with socket_guard(lax, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                      allowed=d2):
        _connect(listener)


def test_the_guard_catches_a_connection_the_caller_never_made(listener):
    """The realistic case: a dependency that phones home."""
    host, port = listener

    def library_that_phones_home():
        _connect((host, port))

    a = NetworkAuthority()
    a.issue(_grant(), actor="scheduler")
    with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL):
        with pytest.raises(GuardedConnection):
            library_that_phones_home()


def test_the_guard_refuses_an_address_shape_it_cannot_classify():
    a = NetworkAuthority()
    with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with pytest.raises(GuardedConnection, match="non-inet"):
                s.connect("/tmp/does-not-exist.sock")
        finally:
            s.close()


def test_the_guard_is_removed_when_the_block_ends(listener):
    original = socket.socket.connect
    a = NetworkAuthority()
    with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL):
        assert socket.socket.connect is not original
    assert socket.socket.connect is original


def test_the_guard_is_removed_even_when_the_block_raises():
    original = socket.socket.connect
    a = NetworkAuthority()
    with pytest.raises(RuntimeError):
        with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL):
            raise RuntimeError("boom")
    assert socket.socket.connect is original


# ---- honesty about what is enforced -------------------------------------
def test_the_module_states_which_layer_is_not_provided():
    """A guard described as stronger than it is will be relied on as such."""
    import re

    import qta_agent.netauth as mod
    doc = re.sub(r"\s+", " ", mod.__doc__ or "")
    assert "kernel layer (NOT provided)" in doc
    assert "This module cannot create them, does not pretend to" in doc
    guard_doc = re.sub(r"\s+", " ", socket_guard.__doc__ or "")
    assert "It is not containment" in guard_doc


def test_only_egress_is_modelled():
    assert [d.value for d in Direction] == ["EGRESS"]
    assert EgressGrant.__dataclass_fields__["direction"].default \
        is Direction.EGRESS


# ---- found by fuzzing ----------------------------------------------------
@pytest.mark.parametrize("field", ["schemes", "methods", "ports",
                                   "address_classes", "addresses", "hosts"])
def test_a_grant_field_is_type_checked_before_it_is_transformed(field):
    """Found by a fuzz campaign, not by inspection.

    ``methods`` was upper-cased and ``schemes`` lower-cased before anything
    checked they held strings, so a record whose field was a list of lists
    raised AttributeError -- outside NetworkError, which is what every caller
    catches. The refusal happened by accident, in a place nobody chose.
    """
    kw = dict(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("https",), hosts=("api.example.com",), ports=(443,),
              methods=("GET",))
    kw[field] = [[], [], []]
    with pytest.raises(NetworkError):
        grant(**kw)


@pytest.mark.parametrize("field", ["schemes", "methods", "ports",
                                   "addresses", "hosts"])
def test_a_bare_string_field_is_refused_rather_than_iterated(field):
    """``methods="GET"`` would become {'G','E','T'} and match nothing."""
    kw = dict(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("https",), hosts=("api.example.com",), ports=(443,),
              methods=("GET",))
    kw[field] = "GET"
    with pytest.raises(NetworkError):
        grant(**kw)


# ---- the grant's window, and a grant that replaced one already in force ----
#
# Egress had the two defects capability.py had, in the same shape: the window
# was checked at one end, and a re-issued grant_id overwrote the grant on
# replay. issue() refuses a duplicate id, so that path was reachable only by
# a record written around it -- which is precisely the record it mattered for.

def test_a_grant_does_not_authorize_traffic_from_before_it_existed(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log)
    for i in range(3):
        log.append(actor="x", action="record.create", target=f"r{i}",
                   payload={})
    a.issue(_grant(), actor="scheduler")
    issued_at = a._at_seq

    a.set_position(issued_at)
    assert a.authorize(_req()).allowed
    a.set_position(issued_at - 1)
    d = a.authorize(_req())
    assert not d.allowed
    assert "reach backwards" in d.reason, d.reason


def test_the_authority_stamps_the_grants_start_from_the_log(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log)
    for i in range(3):
        log.append(actor="x", action="record.create", target=f"r{i}",
                   payload={})
    a.issue(_grant(issued_seq=0), actor="scheduler")
    seq = [e.seq for e in log.read()][-1]
    assert a._grants["g1"].issued_seq == seq


def test_a_backdated_grant_record_is_refused_on_replay(tmp_path):
    from qta_agent.netauth import ACT_NET_GRANT

    log = EventLog(tmp_path / "log.jsonl")
    for i in range(3):
        log.append(actor="x", action="record.create", target=f"r{i}",
                   payload={})
    g = _grant(issued_seq=0)
    log.append(actor="mallory", action=ACT_NET_GRANT, target=TASK,
               payload={"grant": g.body(), "grant_digest": g.digest(),
                        "grant_id": g.grant_id})
    with pytest.raises(NetworkError, match="claims it was issued at seq"):
        NetworkAuthority(log).load()


def test_a_second_grant_under_one_id_does_not_silently_widen_the_first(
        tmp_path):
    """It used to overwrite. A record naming an existing grant_id with a
    wider host set replaced authority already in force, and nothing said so.
    """
    from qta_agent.netauth import ACT_NET_GRANT

    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log)
    a.issue(_grant(), actor="scheduler")
    wider = _grant(hosts=("api.example.com", "evil.example.net"),
                   issued_seq=a._at_seq + 1)
    log.append(actor="mallory", action=ACT_NET_GRANT, target=TASK,
               payload={"grant": wider.body(), "grant_digest": wider.digest(),
                        "grant_id": wider.grant_id})
    with pytest.raises(NetworkError, match="issued twice with different"):
        NetworkAuthority(log).load()


def test_the_same_grant_recorded_twice_is_still_a_replay(tmp_path):
    """The guard above must refuse a REPLACEMENT, not a retried append."""
    from qta_agent.netauth import ACT_NET_GRANT

    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log)
    a.issue(_grant(), actor="scheduler")
    same = a._grants["g1"]
    log.append(actor="scheduler", action=ACT_NET_GRANT, target=TASK,
               payload={"grant": same.body(), "grant_digest": same.digest(),
                        "grant_id": same.grant_id})
    reloaded = NetworkAuthority(log).load()
    assert set(reloaded._grants) == {"g1"}
    assert reloaded._grants["g1"].digest() == same.digest()


# ---- the confused deputy, checked at the boundary --------------------------
#
# check_egress_composition was well written, well tested, and had NO caller
# outside its own tests -- so the defence was a function rather than a
# boundary. Two grants that are each individually correct compose into
# something neither permits, and nothing in the request path asked.

SECRET_VALUE = "hunter2-super-secret-token-value"


def _deputy_world():
    """A credential for ONE host, and egress to two. Both grants legitimate."""
    from qta_agent.secrets import (
        SecretStore, egress_purpose, grant as sec_grant,
    )

    store = SecretStore()
    store.register("api-token", SECRET_VALUE)
    store.issue(sec_grant(grant_id="sg1", subject=ACTOR, task_id=TASK,
                          tool_id=TOOL, secret_id="api-token",
                          purposes=(egress_purpose("api.example.com"),)),
                actor="owner")
    net = NetworkAuthority(None)
    for host in ("api.example.com", "collector.evil.test"):
        net.issue(_grant(grant_id=f"g-{host}", hosts=(host,),
                         methods=("POST",)), actor="owner")
    return store, net


def _post(host):
    return _req(f"https://{host}/v1", method="POST")


def test_a_body_carrying_a_secret_may_not_go_to_an_unpaired_host():
    """THE PAIRING. Each grant alone is untouched; the combination is not."""
    store, net = _deputy_world()
    body = f'{{"token": "{SECRET_VALUE}"}}'
    assert net.authorize(_post("api.example.com"), body=body,
                         secrets=store).allowed
    d = net.authorize(_post("collector.evil.test"), body=body, secrets=store)
    assert not d.allowed
    assert "two grants" in d.reason, d.reason


def test_an_ordinary_body_still_reaches_a_granted_host():
    """The guard must refuse the PAIRING, not the traffic.

    A check that refused every POST to a granted host would be removed
    rather than fixed, and then it would be protecting nothing.
    """
    store, net = _deputy_world()
    assert net.authorize(_post("collector.evil.test"),
                         body='{"hello": "world"}', secrets=store).allowed


def test_the_pairing_is_checked_by_value_not_by_key_name():
    """The leak that matters is a credential under an innocent name."""
    store, net = _deputy_world()
    sneaky = f'{{"greeting": "{SECRET_VALUE}"}}'
    assert not net.authorize(_post("collector.evil.test"), body=sneaky,
                             secrets=store).allowed


def test_a_store_that_cannot_answer_does_not_become_permission():
    """Fail-closed, and without raising out of a function documented total."""
    _, net = _deputy_world()

    class Broken:
        def redactor(self):
            raise RuntimeError("store is unavailable")

    d = net.authorize(_post("collector.evil.test"), body="anything",
                      secrets=Broken())
    assert not d.allowed
    assert "could not be consulted" in d.reason


def test_without_a_body_the_decision_is_unchanged():
    """Callers that pass nothing get exactly the previous behaviour."""
    store, net = _deputy_world()
    assert net.authorize(_post("collector.evil.test")).allowed
    assert net.authorize(_post("collector.evil.test"),
                         secrets=store).allowed


def test_the_composition_check_has_a_production_caller():
    """A defence nothing invokes is a defence that does not exist.

    This is the property that was actually missing: the function was
    correct and unreachable. Asserted structurally so it cannot quietly
    return to being test-only.
    """
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(ROOT), "grep", "-l", "check_egress_composition",
         "--", "*.py"], capture_output=True, text=True)
    files = {x.strip() for x in r.stdout.splitlines() if x.strip()}
    production = {f for f in files if not f.startswith("tests/")}
    assert production - {"qta_agent/secrets.py"}, (
        "check_egress_composition is only referenced by its own module and "
        f"by tests: {sorted(files)}")
