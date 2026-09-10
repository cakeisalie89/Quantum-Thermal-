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
    ACT_NET_GRANT, ACT_NET_REQUEST, AddressClass, Direction, EgressGrant,
    GuardedConnection, MODE_PINNED, MODE_UNPINNED_ACCEPTED,
    MalformedTarget, NetworkAuthority, NetworkDenied, NetworkError,
    NetworkRequest, ServiceOperation, classify_address, grant,
    grant_from_record, host_matches, parse_target, service, socket_guard,
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
    # The granter. This used to be an unrelated string and it worked,
    # which was the defect: withdrawing egress authority took none.
    a.revoke("g1", actor="scheduler", reason="rotated")
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
    """The resolve-then-connect window, and when it is actually open.

    This used to refuse even when the REQUEST carried a resolution, because
    the decision did not keep it and the guard therefore had nothing to
    compare against. It does keep it now, so a request that resolved is
    checkable against the address it resolved to -- stricter than the old
    blanket refusal, not looser. The window is open only when nothing was
    resolved AND the grant has accepted it.
    """
    host, port = listener
    base = dict(subject=ACTOR, task_id=TASK, tool_id=TOOL, schemes=("http",),
                hosts=("localhost",), ports=(port,), methods=("GET",),
                address_classes=(AddressClass.LOOPBACK,))

    # Nothing resolved, and the grant has not accepted the window.
    strict = NetworkAuthority()
    strict.issue(grant(grant_id="g1", **base), actor="scheduler")
    d = strict.authorize(_req(f"http://localhost:{port}/x"))
    assert d.allowed and d.address_mode == MODE_UNPINNED_ACCEPTED
    with socket_guard(strict, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                      allowed=d):
        with pytest.raises(GuardedConnection, match="cannot be checked"):
            _connect(listener)

    # The same grant, but the request resolved: now it IS checkable.
    d_res = strict.authorize(_req(f"http://localhost:{port}/x", resolved=host))
    assert d_res.address_mode == MODE_PINNED
    assert d_res.authorized_address == host
    with socket_guard(strict, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                      allowed=d_res):
        _connect(listener)

    # And the deployment may accept the window explicitly.
    lax = NetworkAuthority()
    lax.issue(grant(grant_id="g1", allow_unpinned_addresses=True, **base),
              actor="scheduler")
    d2 = lax.authorize(_req(f"http://localhost:{port}/x"))
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
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "tools"))
    from repo_scope import non_test_references

    # Asked over tracked AND untracked-unignored files, through the one
    # module that answers this. git grep sees tracked files only, so a
    # caller that has not been committed yet reads as absent and this guard
    # passes for the wrong reason -- which has cost three red pushes.
    production = set(non_test_references("check_egress_composition"))
    assert production - {"qta_agent/secrets.py"}, (
        "check_egress_composition is only referenced by its own module and "
        f"by tests: {sorted(production)}")


# ==========================================================================
# EXTERNAL SERVICE AUTHORITY
#
# THE GAP THIS CLOSES, stated as it was found:
#
#     "no external-service authority beyond egress: no per-service identity,
#      quota or contract"
#
# A grant answers "may this reach that host". That is the right question
# when the far side is anonymous and the wrong one when it is a service
# somebody depends on: which service, what may it be asked, and how much.
# ==========================================================================

def _service(**over):
    kw = dict(service_id="registrar", hosts=("api.example.com",),
              operations=(ServiceOperation("GET", "/v1/records"),
                          ServiceOperation("POST", "/v1/records")),
              quota_per_task=2)
    kw.update(over)
    return service(**kw)


def _svc_req(path="/v1/records", method="GET", task_id=TASK):
    return NetworkRequest(
        actor=ACTOR, task_id=task_id, tool_id=TOOL,
        target=parse_target(f"https://api.example.com{path}", method=method))


def _served(tmp_path, svc=None, g=None):
    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log).load()
    a.issue(g or _grant(methods=("GET", "POST"), paths=("/v1",)),
            actor="scheduler")
    a.register_service(svc or _service(), actor="scheduler")
    return a


def test_a_contracted_operation_is_permitted(tmp_path):
    a = _served(tmp_path)
    d = a.authorize(_svc_req())
    assert d.allowed, d.reason
    assert d.service_id == "registrar"
    assert d.service_digest


def test_an_uncontracted_operation_is_refused_though_the_grant_covers_it(
        tmp_path):
    """THE point. A grant covering /v1 covers every operation under it,
    including ones nobody reviewed."""
    a = _served(tmp_path)
    d = a.authorize(_svc_req(path="/v1/records/7", method="POST"))
    assert d.allowed, "POST /v1/records/7 is under a contracted prefix"

    d = a.authorize(NetworkRequest(
        actor=ACTOR, task_id=TASK, tool_id=TOOL,
        target=parse_target("https://api.example.com/v1/admin",
                            method="POST")))
    assert not d.allowed
    assert "no contracted operation" in d.reason
    assert d.service_id == "registrar"


def test_a_method_the_contract_omits_is_refused(tmp_path):
    a = _served(tmp_path, g=_grant(methods=("GET", "POST", "DELETE"),
                                   paths=("/v1",)))
    d = a.authorize(_svc_req(method="DELETE"))
    assert not d.allowed
    assert "no contracted operation" in d.reason


def test_the_quota_is_per_task_and_is_spent(tmp_path):
    """A retry loop against somebody else's rate limit is an outage you
    caused, and 'the grant permitted it' is true and no comfort."""
    a = _served(tmp_path)
    for _ in range(2):
        d = a.authorize(_svc_req())
        assert d.allowed
        a.record(_svc_req(), d, actor=ACTOR)

    d = a.authorize(_svc_req())
    assert not d.allowed
    assert "budget" in d.reason
    assert a.calls_made("registrar", TASK) == 2


def test_another_task_has_its_own_budget(tmp_path):
    a = _served(tmp_path)
    # A second grant, because a grant is confined to ONE task -- so without
    # this the second task is refused by the grant and the test would pass
    # while proving nothing about the quota.
    a.issue(_grant(grant_id="g2", task_id="task-2",
                   methods=("GET", "POST"), paths=("/v1",)),
            actor="scheduler")
    for _ in range(2):
        a.record(_svc_req(), a.authorize(_svc_req()), actor=ACTOR)
    assert not a.authorize(_svc_req()).allowed

    other = _svc_req(task_id="task-2")
    assert a.authorize(other).allowed, (
        "one task exhausting a service stopped every other task using it")


def test_a_refused_call_does_not_spend_the_budget(tmp_path):
    """Otherwise a mistake costs a task the calls it never made."""
    a = _served(tmp_path)
    bad = NetworkRequest(
        actor=ACTOR, task_id=TASK, tool_id=TOOL,
        target=parse_target("https://api.example.com/v1/admin", method="POST"))
    for _ in range(5):
        a.record(bad, a.authorize(bad), actor=ACTOR)
    assert a.calls_made("registrar", TASK) == 0
    assert a.authorize(_svc_req()).allowed


def test_the_budget_survives_a_restart(tmp_path):
    """Counted from the LOG. A counter this object owned would reset on
    restart, which is the one moment a task most needs its budget to be the
    one it had before."""
    a = _served(tmp_path)
    for _ in range(2):
        a.record(_svc_req(), a.authorize(_svc_req()), actor=ACTOR)

    revived = NetworkAuthority(EventLog(tmp_path / "log.jsonl")).load()
    assert revived.calls_made("registrar", TASK) == 2
    assert not revived.authorize(_svc_req()).allowed


def test_a_host_no_service_claims_is_decided_by_the_grant_alone(tmp_path):
    """Anonymous egress stays legitimate, and visibly different in the
    record from a call to something somebody agreed a contract with."""
    a = _served(tmp_path, g=_grant(hosts=("api.example.com", "other.test"),
                                   methods=("GET",), paths=("/v1",)))
    d = a.authorize(NetworkRequest(
        actor=ACTOR, task_id=TASK, tool_id=TOOL,
        target=parse_target("https://other.test/v1/x")))
    assert d.allowed
    assert d.service_id is None


def test_the_service_and_the_grant_must_BOTH_permit(tmp_path):
    """Registering a service does not widen anything."""
    a = _served(tmp_path, g=_grant(methods=("GET",), paths=("/v1/records",)))
    d = a.authorize(_svc_req(path="/v1/records", method="POST"))
    assert not d.allowed, (
        "the service contract permitted POST and the grant did not; "
        "registering a service must not widen a grant")


def test_a_service_with_no_operations_is_refused():
    """An empty contract permits nothing and is almost always a
    serialization failure rather than an intent."""
    with pytest.raises(NetworkError) as exc:
        service(service_id="s", hosts=("h.test",), operations=(),
                quota_per_task=1)
    assert "declares no operations" in str(exc.value)


def test_a_quota_of_zero_is_refused():
    """A denial wearing a contract's name."""
    with pytest.raises(NetworkError) as exc:
        service(service_id="s", hosts=("h.test",),
                operations=(ServiceOperation("GET", "/x"),),
                quota_per_task=0)
    assert "denial wearing a contract" in str(exc.value)


def test_re_registering_a_service_with_different_terms_is_refused(tmp_path):
    """Every decision recorded under the old contract would afterwards read
    as though it had been made under the new one."""
    a = _served(tmp_path)
    with pytest.raises(NetworkError) as exc:
        a.register_service(_service(quota_per_task=99), actor="scheduler")
    assert "already registered with different terms" in str(exc.value)


def test_a_forged_service_registration_fails_on_replay(tmp_path):
    from qta_agent.netauth import ACT_SERVICE_REGISTER
    a = _served(tmp_path)
    svc = _service(quota_per_task=10_000)
    a.log.append(actor="mallory", action=ACT_SERVICE_REGISTER,
                 target="registrar",
                 payload={"service": svc.body(),
                          "service_digest": svc.digest()})
    with pytest.raises(NetworkError) as exc:
        NetworkAuthority(EventLog(tmp_path / "log.jsonl")).load()
    assert "nobody reviewed" in str(exc.value)


def test_a_service_record_whose_digest_disagrees_is_refused(tmp_path):
    from qta_agent.netauth import ACT_SERVICE_REGISTER
    log = EventLog(tmp_path / "log.jsonl")
    svc = _service()
    log.append(actor="mallory", action=ACT_SERVICE_REGISTER, target="s",
               payload={"service": {**svc.body(), "quota_per_task": 9999},
                        "service_digest": svc.digest()})
    with pytest.raises(NetworkError) as exc:
        NetworkAuthority(log).load()
    assert "hashes to" in str(exc.value)


def test_service_operation_paths_match_by_component(tmp_path):
    """/v1/records does not cover /v1/records-admin.

    Named for the SERVICE contract rather than for the grant: the grant has
    a test of this shape already, and giving both the same name meant only
    the second was ever collected.
    """
    svc = _service(operations=(ServiceOperation("GET", "/v1/records"),))
    assert svc.permits("GET", "/v1/records/7") == ""
    assert "no contracted operation" in svc.permits("GET", "/v1/records-admin")


def test_a_traversal_in_the_path_is_refused(tmp_path):
    svc = _service()
    assert "no contracted operation" in svc.permits(
        "GET", "/v1/records/../admin")


def test_a_refused_call_does_not_spend_the_budget_on_replay(tmp_path):
    """ISOLATED from the in-memory path, which masked it.

    ``record`` keeps its own count so a caller that just recorded sees the
    right number immediately. That count is not what survives a restart --
    ``apply`` rebuilds it from the log -- and a guard present in one and
    missing from the other is exactly the shape that passes every test until
    a process comes back.
    """
    a = _served(tmp_path)
    bad = NetworkRequest(
        actor=ACTOR, task_id=TASK, tool_id=TOOL,
        target=parse_target("https://api.example.com/v1/admin", method="POST"))
    for _ in range(5):
        a.record(bad, a.authorize(bad), actor=ACTOR)

    revived = NetworkAuthority(EventLog(tmp_path / "log.jsonl")).load()
    assert revived.calls_made("registrar", TASK) == 0, (
        "refused calls were counted against the budget on replay")
    assert revived.authorize(_svc_req()).allowed


# --------------------------------------------------------------------------
# Who may withdraw egress authority.
#
# Issuing was guarded; withdrawing was not, on either path, so one appended
# line took away authority somebody else granted. A grant revoked out from
# under a running task fails it closed -- correct as a direction, and not
# something an unrelated actor gets to decide.
# --------------------------------------------------------------------------

def test_an_unrelated_actor_may_not_revoke_an_egress_grant(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = _auth(log=log)
    with pytest.raises(NetworkError, match="may not revoke"):
        a.revoke("g1", actor="mallory", reason="denial of service")
    assert a.authorize(_req()).allowed is True


def test_replay_refuses_an_egress_revocation_from_an_unrelated_actor(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _auth(log=log)
    log.append(actor="mallory", action=ACT_NET_GRANT, target="g1",
               payload={"grant_id": "g1", "revoke": True, "reason": "dos"})
    with pytest.raises(NetworkError, match="revokes egress grant"):
        NetworkAuthority(log).load()


def test_the_granter_may_revoke_its_own_egress_grant(tmp_path):
    """Anti-vacuity: the ordinary withdrawal still works, and still bites."""
    log = EventLog(tmp_path / "log.jsonl")
    a = _auth(log=log)
    a.revoke("g1", actor="scheduler", reason="rotated")
    assert a.authorize(_req()).allowed is False
    assert NetworkAuthority(log).load().authorize(_req()).allowed is False


# --------------------------------------------------------------------------
# A destination is an address AND a port.
#
# _covers checks nine dimensions of a request, port among them. The
# process-layer guard -- the one layer that sees the connection actually
# being made -- checked the address and not the port, because the decision
# carried the addresses to check against and nothing to check the port
# against.
#
# BOTH authorized branches skipped it: the pinned one and the one where the
# grant accepted the unpinned window. The path further down DOES check the
# port, because it rebuilds a request with the real one -- but that path is
# reached only when there is NO authorizing decision, which is the
# dependency-reached-the-network case. Every connection made under a
# decision, which is every connection the system means to make, skipped it.
# --------------------------------------------------------------------------

PIN = "127.0.0.1"


def _pinned(ports=(443,), **over):
    kw = dict(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              schemes=("https",), hosts=("localhost",), ports=ports,
              methods=("GET",), addresses=(PIN,),
              address_classes=(AddressClass.LOOPBACK.value,))
    kw.update(over)
    a = NetworkAuthority()
    a.issue(grant(**kw), actor="scheduler")
    req = NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                         target=parse_target("https://localhost/v1/x",
                                             method="GET"),
                         resolved_address=PIN)
    d = a.authorize(req)
    assert d.allowed, d.reason
    return a, d


def _connect_pinned(a, d, port, host=None):
    with socket_guard(a, actor=ACTOR, task_id=TASK, tool_id=TOOL, allowed=d):
        s = socket.socket()
        s.settimeout(0.05)
        try:
            socket.socket.connect(s, (host or PIN, port))
            return None                    # the guard let it through
        except GuardedConnection as exc:
            return exc
        except OSError:
            return None                    # the guard let it through; the OS
        finally:                           # is what refused, which is not
            s.close()                      # this module's doing


@pytest.mark.parametrize("port", [22, 6379, 9200, 80])
def test_a_pinned_address_does_not_grant_every_port_on_it(port):
    a, d = _pinned(ports=(443,))
    exc = _connect_pinned(a, d, port)
    assert exc is not None, (
        f"the guard permitted a connect to the pinned address on port "
        f"{port}, which this authorization was not issued for; the address "
        "being right is half of a destination being right")
    assert "does not realize this authorization" in str(exc)


def test_the_granted_port_is_still_permitted():
    """Anti-vacuity: a guard that refused every port would pass the above."""
    a, d = _pinned(ports=(443,))
    assert _connect_pinned(a, d, 443) is None


def test_ONE_decision_does_not_cover_every_port_its_grant_permits():
    """A decision authorizes one operation, not its grant's whole set.

    THIS TEST REPLACES ONE THAT ASSERTED THE DEFECT. Its predecessor said a
    grant naming several ports means the guard is "a set membership test",
    and checked that a decision issued for :443 also permitted :8443. That is
    the bearer-token semantics, written down as if it were the requirement.

    A grant is what the actor MIGHT request. A decision is one particular
    operation authorized at one particular point. The connection has to
    realize the decision; it is not enough that it independently satisfies
    the grant.
    """
    a, d443 = _pinned(ports=(443, 8443))
    assert _connect_pinned(a, d443, 443) is None
    assert _connect_pinned(a, d443, 8443) is not None, (
        "a decision issued for :443 was spendable on :8443 because both are "
        "inside the parent grant")


def test_each_permitted_port_works_under_its_OWN_decision():
    """Anti-vacuity for the test above: the grant's other port is reachable.

    A rule that simply refused the second port would pass the test above and
    make half the grant unusable. Authorized separately, each works.
    """
    a, _ = _pinned(ports=(443, 8443))
    for port in (443, 8443):
        req = NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                             target=parse_target(
                                 f"https://localhost:{port}/v1/x",
                                 method="GET"),
                             resolved_address=PIN)
        d = a.authorize(req)
        assert d.allowed, d.reason
        assert d.authorized_port == port
        assert _connect_pinned(a, d, port) is None


def test_the_decision_carries_the_operation_it_authorized():
    """The conservation the defect was, restated at the right level.

    The decision used to carry the grant's SETS -- every pinned address and
    every permitted port. It carries the one operation now, and the grant's
    pin set stays beside it as audit context rather than as the enforcement
    set.
    """
    a, d = _pinned(ports=(443, 8443))
    assert d.authorized_host == "localhost"
    assert d.authorized_port == 443
    assert d.authorized_scheme == "https"
    assert d.authorized_address == PIN
    assert d.address_mode == MODE_PINNED
    rec = d.to_record()
    assert rec["authorized_port"] == 443
    assert rec["authorized_address"] == PIN
    # The grant's pin set is still recorded, and is not the enforcement set.
    assert rec["pinned_addresses"] == [PIN]
    assert "pinned_ports" not in rec


def test_a_decision_does_not_cover_another_address_its_grant_pins():
    """The same rule on the address dimension."""
    a = NetworkAuthority()
    a.issue(grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
                  schemes=("https",), hosts=("localhost",), ports=(443,),
                  methods=("GET",), addresses=(PIN, "127.0.0.2"),
                  address_classes=(AddressClass.LOOPBACK.value,)),
            actor="scheduler")
    # Resolved to the SECOND pin on purpose. Resolving to the first would let
    # a decision that simply echoed the grant's first pinned address produce
    # the identical value, and this test could not tell the two apart -- a
    # mutation doing exactly that survived until this line changed.
    req = NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                         target=parse_target("https://localhost/v1/x",
                                             method="GET"),
                         resolved_address="127.0.0.2")
    d = a.authorize(req)
    assert d.allowed and d.authorized_address == "127.0.0.2"
    assert _connect_pinned(a, d, 443, host="127.0.0.2") is None
    exc = _connect_pinned(a, d, 443, host=PIN)
    assert exc is not None and "not the address this authorization" in str(exc), (
        "a decision that resolved to 127.0.0.2 was spendable on the grant's "
        "other pinned address")


def test_an_unresolved_request_cannot_satisfy_a_PIN():
    """Skipping a check is not passing it.

    The address-class and pinning checks both sat behind `if addr is not
    None`, so a caller that omitted resolved_address skipped both -- and a
    grant pinning exactly one address was satisfied by a request that
    resolved to nothing. The bypass was one keyword argument.
    """
    a, _ = _pinned(ports=(443,))
    req = NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                         target=parse_target("https://localhost/v1/x",
                                             method="GET"),
                         resolved_address=None)
    d = a.authorize(req)
    assert d.allowed is False
    assert "cannot be checked against a pin" in d.reason


def test_an_unclassifiable_address_is_classified_at_the_socket():
    """The class check, moved to the layer that can answer it.

    Authorizing a URL before resolving is this module's normal calling
    convention, so demanding a resolver at authorize() would be a new
    contract rather than a repair. The guard holds the address the
    connection is actually going to, so a granted NAME that resolves inside
    the perimeter is refused there.
    """
    a = NetworkAuthority()
    a.issue(grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
                  schemes=("https",), hosts=("localhost",), ports=(443,),
                  methods=("GET",), allow_unpinned_addresses=True,
                  address_classes=(AddressClass.PUBLIC.value,)),
            actor="scheduler")
    req = NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                         target=parse_target("https://localhost/v1/x",
                                             method="GET"),
                         resolved_address=None)
    d = a.authorize(req)
    assert d.allowed, d.reason           # the class could not be answered yet
    exc = _connect_pinned(a, d, 443)     # ...and 127.0.0.1 is LOOPBACK
    assert exc is not None and "LOOPBACK" in str(exc), exc


def test_unpinned_does_not_become_any_hostname_on_an_allowed_port():
    """The waiver is about addresses, not about which name was asked for."""
    a = NetworkAuthority()
    a.issue(grant(grant_id="g1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
                  schemes=("https",), hosts=("a.example.com", "b.example.com"),
                  ports=(443,), methods=("GET",),
                  allow_unpinned_addresses=True,
                  address_classes=(AddressClass.PUBLIC.value,)),
            actor="scheduler")
    req = NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                         target=parse_target("https://a.example.com/v1/x",
                                             method="GET"),
                         resolved_address=None)
    d = a.authorize(req)
    assert d.address_mode == MODE_UNPINNED_ACCEPTED
    exc = _connect_pinned(a, d, 443, host="b.example.com")
    assert exc is not None and "names a different host" in str(exc), (
        "a decision for a.example.com was spendable on b.example.com because "
        "both are inside the grant")


def test_the_pinned_and_unpinned_paths_agree_about_ports():
    """The two authorized branches must accept the same language.

    Neither checked the port before this. They are the same question --
    is this destination one the grant permits -- so the check belongs ahead
    of the branch rather than inside one of them, which is how they came to
    disagree in the first place.
    """
    unpinned = NetworkAuthority()
    unpinned.issue(grant(grant_id="g2", subject=ACTOR, task_id=TASK,
                         tool_id=TOOL, schemes=("https",),
                         hosts=("localhost",), ports=(443,),
                         methods=("GET",),
                         address_classes=(AddressClass.LOOPBACK.value,),
                         allow_unpinned_addresses=True),
                   actor="scheduler")
    req = NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                         target=parse_target("https://localhost/v1/x",
                                             method="GET"),
                         resolved_address=PIN)
    d = unpinned.authorize(req)
    assert d.allowed and not d.pinned_addresses

    with socket_guard(unpinned, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                      allowed=d):
        s = socket.socket()
        s.settimeout(0.05)
        try:
            socket.socket.connect(s, (PIN, 22))
            refused = False
        except GuardedConnection:
            refused = True
        except OSError:
            refused = False
        finally:
            s.close()
    assert refused, ("the unpinned path permitted port 22; the two branches "
                     "no longer agree about what a destination is")


# ===========================================================================
# P0-R11 -- THE ADDRESS CLASS, IN THE BRANCH THAT NEEDED IT.
#
# The class check in socket_guard sits behind `literal is not None`, so it
# answers only for a connect target that was ALREADY an IP. Unpinned mode is
# the case where the target is a NAME -- it is what the branch exists for --
# and there the class went unchecked entirely, while the comment above it
# said "this layer is holding the address the connection is actually going
# to, so it can answer the question the authorization could not".
#
# It was not holding that address. The OS resolver chose it afterwards.
#
# Reproduced with no external networking and no resolver of our own:
# `localhost` is a hostname that resolves to loopback, so a PUBLIC-only grant
# for it is a grant whose class restriction the connection violates.
# ===========================================================================

def _loopback_listener():
    """A real listener, so a PERMITTED connect actually completes."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    return srv, srv.getsockname()[1]


def _unpinned_named_connect(classes, host="localhost"):
    """Authorize a NAME unpinned under ``classes``, then connect to it.

    Returns ("ALLOWED", peer) or ("REFUSED", message).
    """
    srv, port = _loopback_listener()
    try:
        auth = NetworkAuthority()
        auth.issue(_grant(hosts=(host,), ports=(port,),
                          address_classes=classes,
                          addresses=(), allow_unpinned_addresses=True),
                   actor="scheduler")
        d = auth.authorize(NetworkRequest(
            actor=ACTOR, task_id=TASK, tool_id=TOOL,
            target=parse_target(f"https://{host}:{port}/v1/x", method="GET")))
        assert d.allowed and d.address_mode == MODE_UNPINNED_ACCEPTED, d
        with socket_guard(auth, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                          allowed=d):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            try:
                s.connect((host, port))
                return "ALLOWED", s.getpeername()
            except GuardedConnection as exc:
                return "REFUSED", str(exc)
            finally:
                s.close()
    finally:
        srv.close()


def test_an_unpinned_NAME_resolving_inside_the_perimeter_is_refused():
    """THE defect: PUBLIC-only authority reaching loopback through a name."""
    result, detail = _unpinned_named_connect((AddressClass.PUBLIC.value,))
    assert result == "REFUSED", (
        f"a grant permitting PUBLIC only reached {detail} through an "
        "unpinned name; the class restriction was never applied because the "
        "connect target was a name rather than an address")
    assert "LOOPBACK" in detail and "PUBLIC" in detail, detail


def test_an_unpinned_NAME_inside_a_permitted_class_still_connects():
    """Anti-vacuity: the rule names a real condition.

    A guard that refused every unpinned name would pass the test above while
    breaking the mode entirely.
    """
    result, detail = _unpinned_named_connect((AddressClass.LOOPBACK.value,))
    assert result == "ALLOWED", detail
    assert detail[0] == "127.0.0.1", detail


def test_the_connection_goes_TO_THE_ADDRESS_THE_GUARD_CLASSIFIED():
    """One resolution, not two -- and observably so.

    Classifying a name and then handing the NAME to the real connect resolves
    it a SECOND time, and a second resolution is a second answer: the
    rebinding window, opened by the check written to close it.

    ``getpeername()`` cannot tell the two apart, because both end up at a
    loopback address. So the resolution the guard sees is steered somewhere
    the OS would not send it: the listener is on 127.0.0.2 and the patched
    resolver returns only that, while the real resolver still maps
    ``localhost`` to 127.0.0.1 where nothing is listening. Connecting to the
    classified address succeeds; handing the name back does not.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.2", 0))
    except OSError:                              # pragma: no cover - not Linux
        pytest.skip("127.0.0.2 is not bindable on this host")
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()

    real_gai = socket.getaddrinfo

    def steered(host, prt, *a, **kw):
        if host == "localhost":
            return [(socket.AF_INET, socket.SOCK_STREAM,
                     socket.IPPROTO_TCP, "", ("127.0.0.2", prt))]
        return real_gai(host, prt, *a, **kw)      # pragma: no cover

    try:
        auth = NetworkAuthority()
        auth.issue(_grant(hosts=("localhost",), ports=(port,),
                          address_classes=(AddressClass.LOOPBACK.value,),
                          addresses=(), allow_unpinned_addresses=True),
                   actor="scheduler")
        d = auth.authorize(NetworkRequest(
            actor=ACTOR, task_id=TASK, tool_id=TOOL,
            target=parse_target(f"https://localhost:{port}/v1/x",
                                method="GET")))
        socket.getaddrinfo = steered
        try:
            with socket_guard(auth, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                              allowed=d):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2.0)
                try:
                    s.connect(("localhost", port))
                    peer = s.getpeername()
                finally:
                    s.close()
        finally:
            socket.getaddrinfo = real_gai
    finally:
        srv.close()

    assert peer[0] == "127.0.0.2", (
        f"connected to {peer}, not the address the guard classified. The "
        "name was handed back to a second resolver, so what was checked and "
        "what was reached are two different answers")


def test_an_unpinned_name_that_is_not_the_authorized_name_is_still_refused():
    """The name binding survives the new resolution step."""
    srv, port = _loopback_listener()
    try:
        auth = NetworkAuthority()
        auth.issue(_grant(hosts=("localhost", "other.invalid"), ports=(port,),
                          address_classes=(AddressClass.LOOPBACK.value,),
                          addresses=(), allow_unpinned_addresses=True),
                   actor="scheduler")
        d = auth.authorize(NetworkRequest(
            actor=ACTOR, task_id=TASK, tool_id=TOOL,
            target=parse_target(f"https://localhost:{port}/v1/x",
                                method="GET")))
        with socket_guard(auth, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                          allowed=d):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            with pytest.raises(GuardedConnection, match="different host"):
                s.connect(("other.invalid", port))
            s.close()
    finally:
        srv.close()


def test_an_unpinned_name_on_another_port_is_still_refused():
    """The port binding survives it too."""
    srv, port = _loopback_listener()
    try:
        auth = NetworkAuthority()
        auth.issue(_grant(hosts=("localhost",), ports=(port, port + 1),
                          address_classes=(AddressClass.LOOPBACK.value,),
                          addresses=(), allow_unpinned_addresses=True),
                   actor="scheduler")
        d = auth.authorize(NetworkRequest(
            actor=ACTOR, task_id=TASK, tool_id=TOOL,
            target=parse_target(f"https://localhost:{port}/v1/x",
                                method="GET")))
        with socket_guard(auth, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                          allowed=d):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            with pytest.raises(GuardedConnection, match="does not realize"):
                s.connect(("localhost", port + 1))
            s.close()
    finally:
        srv.close()


def test_connect_ex_takes_the_same_path_as_connect():
    """Both entry points, or the guard is a suggestion."""
    srv, port = _loopback_listener()
    try:
        auth = NetworkAuthority()
        auth.issue(_grant(hosts=("localhost",), ports=(port,),
                          address_classes=(AddressClass.PUBLIC.value,),
                          addresses=(), allow_unpinned_addresses=True),
                   actor="scheduler")
        d = auth.authorize(NetworkRequest(
            actor=ACTOR, task_id=TASK, tool_id=TOOL,
            target=parse_target(f"https://localhost:{port}/v1/x",
                                method="GET")))
        with socket_guard(auth, actor=ACTOR, task_id=TASK, tool_id=TOOL,
                          allowed=d):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            with pytest.raises(GuardedConnection, match="LOOPBACK"):
                s.connect_ex(("localhost", port))
            s.close()
    finally:
        srv.close()
