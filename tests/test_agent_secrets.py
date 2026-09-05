"""Secrets: the accidents, then the attacks.

The accidental disclosures come first because they are the ones that actually
happen -- an f-string in an error path, a subprocess command echoed into a
diagnostic, an environment dumped into a CI log. The deliberate ones come
after, and the composition test at the end is the one that matters most: two
grants that are each individually correct, used together to do something
neither permits.
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.events import EventLog  # noqa: E402
from qta_agent.netauth import (  # noqa: E402
    AddressClass, NetworkAuthority, NetworkRequest, grant as net_grant,
    parse_target,
)
from qta_agent.secrets import (  # noqa: E402
    ACT_SECRET_ACCESS, ACT_SECRET_GRANT, ANY_PURPOSE, MIN_SECRET_LEN,
    Redactor, Secret, SecretDenied, SecretError, SecretExpired, SecretRef,
    SecretRevoked, SecretStore, UnknownSecret, check_egress_composition,
    egress_purpose, grant, looks_like_a_secret_key,
)

VALUE = "hunter2-super-secret-token-value"
ACTOR = "agent-worker-1"
TASK = "task-1"
TOOL = "fetch.schema"


@pytest.fixture()
def store():
    s = SecretStore()
    s.register("api-token", VALUE)
    s.issue(_grant(), actor="owner")
    return s


def _grant(**over):
    kw = dict(grant_id="sg1", subject=ACTOR, task_id=TASK, tool_id=TOOL,
              secret_id="api-token", purposes=("call-schema-api",))
    kw.update(over)
    return grant(**kw)


def _resolve(store, **over):
    kw = dict(grant_id="sg1", actor=ACTOR, task_id=TASK, tool_id=TOOL,
              purpose="call-schema-api")
    kw.update(over)
    return store.resolve(SecretRef("api-token"), **kw)


# ---- references travel, values do not -----------------------------------
def test_a_reference_carries_no_value_and_no_digest_of_one():
    ref = SecretRef("api-token")
    assert VALUE not in str(ref) and VALUE not in repr(ref)
    assert json.dumps(ref.to_record()) == '{"secret_ref": "api-token"}'
    assert not hasattr(ref, "value")
    assert not any("digest" in f for f in SecretRef.__dataclass_fields__), (
        "a digest of the value would let anyone holding it confirm a guess "
        "offline")


def test_registering_returns_a_reference_not_a_value(store):
    s = SecretStore()
    out = s.register("other", VALUE)
    assert isinstance(out, SecretRef)
    assert out.secret_id == "other"


def test_resolve_refuses_a_raw_string_in_place_of_a_reference(store):
    with pytest.raises(SecretError, match="expected a SecretRef"):
        store.resolve("api-token", grant_id="sg1", actor=ACTOR,
                      task_id=TASK, tool_id=TOOL, purpose="call-schema-api")


# ---- accidental disclosure ----------------------------------------------
def test_a_secret_renders_as_a_placeholder_in_every_string_path(store):
    secret = _resolve(store)
    assert str(secret) == "<secret:api-token>"
    assert repr(secret) == "<secret:api-token>"
    assert f"{secret}" == "<secret:api-token>"
    assert f"{secret!r}" == "<secret:api-token>"
    assert f"{secret:>40}" == "<secret:api-token>", (
        "a format spec goes through __format__, so a class overriding only "
        "__str__ leaks through an f-string that pads or aligns")
    assert "%s" % (secret,) == "<secret:api-token>"
    assert "{}".format(secret) == "<secret:api-token>"
    assert VALUE not in " ".join([str(secret), repr(secret), f"{secret}"])


def test_a_secret_refuses_implicit_conversion_to_bytes(store):
    with pytest.raises(SecretError, match="visible in the code"):
        bytes(_resolve(store))


def test_revealing_is_explicit_and_returns_the_value(store):
    assert _resolve(store).reveal() == VALUE


def test_a_secret_in_a_logged_structure_does_not_leak(store):
    secret = _resolve(store)
    payload = {"tool": TOOL, "credential": secret, "args": [secret]}
    text = json.dumps(payload, default=str)
    assert VALUE not in text
    assert text.count("<secret:api-token>") == 2


# ---- redaction ----------------------------------------------------------
def test_redaction_finds_the_exact_value(store):
    r = store.redactor()
    assert r.text(VALUE) == "<redacted:api-token>"


def test_redaction_finds_a_value_embedded_in_a_longer_string(store):
    r = store.redactor()
    out = r.text(f"curl -H 'Authorization: Bearer {VALUE}' https://x/y")
    assert VALUE not in out
    assert "<redacted:api-token>" in out
    assert out.startswith("curl -H 'Authorization: Bearer ")


def test_redaction_finds_every_repetition_and_every_secret():
    s = SecretStore()
    s.register("a", "aaaa-first-secret-value")
    s.register("b", "bbbb-second-secret-value")
    r = s.redactor()
    out = r.text("aaaa-first-secret-value / bbbb-second-secret-value "
                 "/ aaaa-first-secret-value")
    assert "first-secret" not in out and "second-secret" not in out
    assert out.count("<redacted:a>") == 2
    assert out.count("<redacted:b>") == 1


def test_a_secret_containing_another_is_replaced_whole():
    s = SecretStore()
    s.register("outer", "prefix-INNER-SECRET-suffix")
    s.register("inner", "INNER-SECRET")
    out = s.redactor().text("prefix-INNER-SECRET-suffix")
    assert out == "<redacted:outer>", (
        "replacing the inner one first would leave 'prefix-<redacted:inner>"
        "-suffix', which still discloses the outer secret's shape")


@pytest.mark.parametrize("encode", [
    lambda v: base64.b64encode(v.encode()).decode(),
    lambda v: base64.b64encode(v.encode()).decode().rstrip("="),
    lambda v: base64.urlsafe_b64encode(v.encode()).decode(),
    lambda v: v.encode().hex(),
    urllib.parse.quote,
    urllib.parse.quote_plus,
])
def test_redaction_finds_the_encodings_it_claims_to(store, encode):
    encoded = encode(VALUE)
    assert store.redactor().text(f"x={encoded}") != f"x={encoded}"


def test_redaction_finds_a_json_escaped_value():
    s = SecretStore()
    s.register("quoted", 'has "quotes" and \\ backslash')
    body = json.dumps({"k": 'has "quotes" and \\ backslash'})
    assert s.redactor().text(body) != body


def test_the_module_is_honest_about_what_redaction_misses(store):
    """A transformed secret is not found, and the docs say so."""
    import hashlib
    import re

    import qta_agent.secrets as mod
    transformed = hashlib.sha256(VALUE.encode()).hexdigest()
    assert store.redactor().text(transformed) == transformed
    assert store.redactor().text(VALUE[::-1]) == VALUE[::-1]
    doc = re.sub(r"\s+", " ", mod.__doc__ or "")
    assert "It does NOT find a secret that has been hashed" in doc


@pytest.mark.parametrize("surface", ["stdout", "stderr", "exception",
                                     "command", "environment", "json",
                                     "audit"])
def test_redaction_covers_every_output_surface(store, surface):
    r = store.redactor()
    if surface == "exception":
        out = r.walk(RuntimeError(f"failed calling api with {VALUE}"))
        assert VALUE not in str(out)
        return
    if surface == "environment":
        out = r.environment({"API_TOKEN": VALUE, "INNOCENT_NAME": VALUE,
                             "PATH": "/usr/bin"})
        assert VALUE not in json.dumps(out)
        assert out["INNOCENT_NAME"] == "<redacted:api-token>", (
            "key-name redaction would miss this, which is exactly how a "
            "secret reaches a diagnostic dump")
        return
    if surface == "command":
        out = r.walk(["curl", "-H", f"Authorization: Bearer {VALUE}"])
        assert VALUE not in " ".join(out)
        return
    if surface == "json":
        out = r.walk({"nested": {"list": [{"k": VALUE}]}})
        assert VALUE not in json.dumps(out)
        return
    out = r.text(f"tool wrote {VALUE} to {surface}")
    assert VALUE not in out


def test_redaction_walks_bytes(store):
    out = store.redactor().walk(f"token={VALUE}".encode())
    assert VALUE.encode() not in out


def test_a_redactor_does_not_expose_what_it_holds(store):
    """It must hold the values -- it cannot find what it does not know.

    So the property is not "holds nothing", which would be a lie, but "offers
    no way to read them back and does not print them". ``repr`` is the one
    that matters: it is what ends up in a log line about the redactor.
    """
    r = store.redactor()
    assert VALUE not in repr(r) and VALUE not in str(r)
    public = [n for n in dir(r) if not n.startswith("_")]
    assert not any(VALUE in str(getattr(r, n, "")) for n in public), (
        f"a public attribute of the redactor discloses the value: {public}")


def test_assert_clean_refuses_to_emit_a_leaking_structure(store):
    store.assert_clean({"ok": "nothing here"})
    with pytest.raises(SecretError, match="contains a registered secret"):
        store.assert_clean({"payload": VALUE}, what="an event payload")


def test_a_short_secret_is_refused_rather_than_silently_unredactable():
    s = SecretStore()
    with pytest.raises(SecretError, match=f"at least {MIN_SECRET_LEN}"):
        s.register("tiny", "ab")
    with pytest.raises(SecretError):
        Redactor().add("tiny", "ab")


# ---- authority ----------------------------------------------------------
def test_an_ungranted_secret_does_not_resolve(store):
    with pytest.raises(SecretDenied, match="was ever issued"):
        _resolve(store, grant_id="no-such-grant")


@pytest.mark.parametrize("over,fragment", [
    ({"actor": "agent-worker-2"}, "granted to"),
    ({"task_id": "task-2"}, "confined to task"),
    ({"tool_id": "exfiltrate"}, "permits tool"),
    ({"purpose": "some-other-purpose"}, "permits purposes"),
])
def test_a_grant_is_bound_to_who_what_and_why(store, over, fragment):
    with pytest.raises(SecretDenied, match=fragment):
        _resolve(store, **over)


def test_a_grant_over_the_wrong_secret_does_not_resolve(store):
    store.register("other-token", "another-long-secret-value")
    with pytest.raises(SecretDenied, match="covers 'api-token'"):
        store.resolve(SecretRef("other-token"), grant_id="sg1", actor=ACTOR,
                      task_id=TASK, tool_id=TOOL, purpose="call-schema-api")


def test_any_purpose_must_be_written_out():
    with pytest.raises(SecretError, match="no purposes"):
        _grant(purposes=())
    with pytest.raises(SecretError, match="bare string"):
        _grant(purposes="call-schema-api")
    s = SecretStore()
    s.register("api-token", VALUE)
    s.issue(_grant(purposes=(ANY_PURPOSE,)), actor="owner")
    assert _resolve(s, purpose="anything-at-all").reveal() == VALUE


def test_a_grant_over_an_unregistered_secret_is_refused():
    s = SecretStore()
    with pytest.raises(UnknownSecret, match="would look like authority"):
        s.issue(_grant(), actor="owner")


# ---- lifetime -----------------------------------------------------------
def test_revocation_takes_effect_at_the_next_use_not_the_next_object(store):
    """A handle captured before revocation must stop working."""
    secret = _resolve(store)
    assert secret.reveal() == VALUE
    store.revoke("sg1", actor="owner", reason="rotated")
    with pytest.raises(SecretRevoked):
        secret.reveal()


def test_expiry_takes_effect_at_the_next_use(store):
    s = SecretStore()
    s.register("api-token", VALUE)
    s.issue(_grant(issued_seq=1, expires_after_seq=10), actor="owner")
    secret = _resolve(s)
    s.set_position(10)
    assert secret.reveal() == VALUE
    s.set_position(11)
    with pytest.raises(SecretExpired):
        secret.reveal()


def test_forgetting_a_value_makes_a_live_grant_refuse(store):
    secret = _resolve(store)
    store.forget("api-token")
    with pytest.raises(UnknownSecret, match="no longer held"):
        secret.reveal()


def test_forget_zeroes_the_buffer_rather_than_only_dropping_it():
    s = SecretStore()
    s.register("api-token", VALUE)
    buf = s._values["api-token"]
    s.forget("api-token")
    assert bytes(buf) == b"\x00" * len(buf)


def test_a_handle_holds_no_value_of_its_own(store):
    secret = _resolve(store)
    assert VALUE not in repr(vars(Secret) if False else secret.__slots__)
    for slot in secret.__slots__:
        assert VALUE not in str(getattr(secret, slot, ""))


def test_registering_the_same_id_twice_is_refused(store):
    with pytest.raises(SecretError, match="already registered"):
        store.register("api-token", "a-completely-different-value")


# ---- provenance ---------------------------------------------------------
def test_accesses_are_recorded_without_the_value(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    s = SecretStore(log)
    s.register("api-token", VALUE)
    s.issue(_grant(), actor="owner")
    _resolve(s).reveal()

    grants = [e for e in log.read() if e.action == ACT_SECRET_GRANT]
    accesses = [e for e in log.read() if e.action == ACT_SECRET_ACCESS]
    assert len(grants) == 1 and len(accesses) == 1
    flat = json.dumps([e.payload for e in log.read()])
    assert VALUE not in flat
    assert accesses[0].payload["purpose"] == "call-schema-api"
    assert accesses[0].payload["secret_id"] == "api-token"
    assert "value" not in flat and "secret_digest" not in flat, (
        "a digest of the value in the log is an offline guessing oracle")


def test_the_store_records_who_asked_for_what(store):
    _resolve(store).reveal()
    _resolve(store).reveal()
    assert len(store.accesses()) == 2
    assert store.accesses()[0]["purpose"] == "call-schema-api"
    assert VALUE not in json.dumps(store.accesses())


# ---- composition: secret + network --------------------------------------
def _net_decision(host, *, task=TASK, tool=TOOL):
    g = net_grant(grant_id="ng1", subject=ACTOR, task_id=task, tool_id=tool,
                  schemes=("https",), hosts=(host,), ports=(443,),
                  methods=("POST",), address_classes=(AddressClass.PUBLIC,))
    a = NetworkAuthority()
    a.issue(g, actor="scheduler")
    return a.authorize(NetworkRequest(
        actor=ACTOR, task_id=task, tool_id=tool,
        target=parse_target(f"https://{host}/v1/upload", method="POST")))


def test_holding_a_secret_and_holding_egress_do_not_compose():
    """The confused deputy this system actually has.

    A component legitimately holds a credential for one service and
    legitimately holds egress to another. Neither grant is violated by
    sending the first to the second, which is why the check is on the
    combination.
    """
    s = SecretStore()
    s.register("api-token", VALUE)
    g = _grant(purposes=(egress_purpose("api.example.com"),))
    s.issue(g, actor="owner")

    check_egress_composition(g, _net_decision("api.example.com"))
    with pytest.raises(SecretDenied, match="would need"):
        check_egress_composition(g, _net_decision("collector.evil.test"))


def test_composition_refuses_an_unauthorized_destination_outright():
    g = _grant(purposes=(ANY_PURPOSE,))
    a = NetworkAuthority()
    denied = a.authorize(NetworkRequest(
        actor=ACTOR, task_id=TASK, tool_id=TOOL,
        target=parse_target("https://evil.test/v1", method="POST")))
    with pytest.raises(SecretDenied, match="unauthorized destination"):
        check_egress_composition(g, denied)
    with pytest.raises(SecretDenied):
        check_egress_composition(g, None)


def test_a_wildcard_purpose_does_not_bypass_the_egress_pairing():
    """ANY_PURPOSE is broad on purpose, and still needs a live destination."""
    g = _grant(purposes=(ANY_PURPOSE,))
    check_egress_composition(g, _net_decision("api.example.com"))


def test_egress_purpose_normalises_the_host():
    assert egress_purpose("API.Example.COM.") == "egress:api.example.com"
    with pytest.raises(SecretError):
        egress_purpose("")


# ---- key-name heuristics are advisory -----------------------------------
@pytest.mark.parametrize("key", [
    "token", "API_KEY", "api-key", "api.key", "bearer_token",
    "Authorization", "x-session-id", "client_secret", "PRIVATE_KEY",
    "db.password", "aws/access/key",
])
def test_suspicious_key_names_are_recognised(key):
    assert looks_like_a_secret_key(key), key


@pytest.mark.parametrize("key", [
    "path", "count", "result", "tokenizer_name",
    "secretary_name", "token_count", "password_policy_doc_url", "",
])
def test_ordinary_key_names_are_not_flagged(key):
    """A heuristic that flags ordinary fields is one people switch off.

    ``tokenizer_name`` and ``secretary_name`` contain "token" and "secret" as
    substrings and are not secrets; whole-word matching is what separates
    them. ``token_count`` is the borderline case and stays flagged, because
    "token" IS one of its words -- a false positive on a count is cheap.
    """
    if key in ("token_count", "password_policy_doc_url"):
        # Borderline and deliberately left flagged: "token" and "password"
        # ARE words here. A false positive on a counter or a doc URL costs a
        # redaction that was not needed; a false negative costs a secret.
        assert looks_like_a_secret_key(key)
        return
    assert not looks_like_a_secret_key(key)


def test_the_key_heuristic_is_not_the_defence(store):
    """The leak that matters is the one under an innocent name."""
    r = store.redactor()
    assert r.text(f"result={VALUE}") == "result=<redacted:api-token>"
