"""External secret providers: the seam where a credential enters the process.

Every test here is about one of three things.

WHAT THE PROVIDER REFUSES. A provider is the only place in the package that
reads a credential from outside, so it is the only place where a bad name, a
bad path or a bad byte can turn into a registered secret. The refusals are
checked one at a time, and each one is checked by trying to get past it.

WHAT THE STORE RECORDS. Provisioning is provenance: which id, from which
kind of source, at whose request. Never the value, never a digest of it. The
tests read the log back and assert on what is absent as hard as on what is
present, because "no value in the log" is the property that fails silently.

WHAT HAPPENS WHEN IT GOES WRONG HALFWAY. A credential set that is half
loaded is worse than one that failed to load, because the failure surfaces
later, inside the run, at the one call that needed the missing one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.events import EventLog  # noqa: E402
from qta_agent.secrets import (  # noqa: E402
    ACT_SECRET_PROVISION, MAX_SECRET_BYTES, MIN_SECRET_LEN,
    FileSecretProvider, MappingSecretProvider, ProviderError,
    ProviderRefused, ProviderUnavailable, SecretError, SecretProvider,
    SecretRef, SecretStore, grant,
)

VALUE = "hunter2-super-secret-token-value"
OTHER = "a-completely-different-credential"


@pytest.fixture()
def secret_dir(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    (d / "api-token").write_text(VALUE, encoding="utf-8")
    (d / "db-password").write_text(OTHER + "\n", encoding="utf-8")
    return d


@pytest.fixture()
def log(tmp_path):
    return EventLog(tmp_path / "log.jsonl")


# ---- the file provider, and what it refuses ------------------------------
def test_a_file_becomes_a_secret_and_the_value_never_becomes_a_str(
        secret_dir):
    p = FileSecretProvider(secret_dir)
    buf = p.fetch("api-token")
    assert isinstance(buf, bytearray), (
        "a provider returns a zeroable buffer; a str cannot be zeroed and "
        "the whole load path exists to avoid making one")
    assert bytes(buf).decode("utf-8") == VALUE


def test_one_trailing_newline_is_stripped_and_only_one(secret_dir):
    p = FileSecretProvider(secret_dir)
    assert bytes(p.fetch("db-password")).decode("utf-8") == OTHER
    (secret_dir / "two-newlines").write_bytes((OTHER + "\n\n").encode())
    assert bytes(p.fetch("two-newlines")).decode("utf-8") == OTHER + "\n"


def test_a_crlf_ending_is_stripped_whole(secret_dir):
    (secret_dir / "windows").write_bytes((OTHER + "\r\n").encode())
    got = bytes(FileSecretProvider(secret_dir).fetch("windows")).decode()
    assert got == OTHER, (
        "a stray \\r is invisible in every log and breaks authentication "
        "against services that compare bytes")


@pytest.mark.parametrize("bad", [
    "../outside",
    "a/b",
    ".hidden",
    "",
    "with space",
    "x" * 65,
    "tab\tname",
])
def test_a_name_that_could_leave_the_directory_is_refused_by_the_name_rule(
        secret_dir, bad):
    with pytest.raises(ProviderRefused, match="not a usable secret id"):
        FileSecretProvider(secret_dir).fetch(bad)


def test_the_name_rule_refuses_before_the_filesystem_is_asked(
        secret_dir, monkeypatch):
    """The refusal must not depend on the path layer catching it after.

    Two guards in series look like defence in depth and are not, if the
    first one is never actually reached: it is the second that is doing the
    work, and the day the second changes the first is discovered to have
    been decorative. So the filesystem is made to explode, and the name rule
    still has to be the thing that fires.
    """
    import qta_agent.safeio as safeio

    def explode(*a, **k):
        raise AssertionError("the filesystem was consulted for a bad name")

    monkeypatch.setattr(safeio.ReadRoot, "open", explode)
    with pytest.raises(ProviderRefused, match="not a usable secret id"):
        FileSecretProvider(secret_dir).fetch("../../etc/shadow")


def test_a_missing_secret_is_refused_not_empty(secret_dir):
    with pytest.raises(ProviderRefused, match="holds no secret"):
        FileSecretProvider(secret_dir).fetch("never-registered")


def test_a_missing_root_is_unavailable_not_refused(tmp_path):
    """"The store is not mounted" and "no such secret" are different facts.

    An operator sent to look for a missing key when the volume failed to
    mount looks in the wrong place, and the two conditions are reported by
    two exception types for exactly that reason.
    """
    with pytest.raises(ProviderUnavailable, match="not usable"):
        FileSecretProvider(tmp_path / "no-such-dir").fetch("api-token")
    assert issubclass(ProviderUnavailable, ProviderError)
    assert not issubclass(ProviderUnavailable, ProviderRefused)


def test_a_symlink_out_of_the_root_is_refused(secret_dir, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.write_text(OTHER, encoding="utf-8")
    (secret_dir / "escape").symlink_to(outside)
    with pytest.raises(ProviderRefused):
        FileSecretProvider(secret_dir).fetch("escape")


def test_a_directory_is_not_a_secret(secret_dir):
    (secret_dir / "subdir").mkdir()
    with pytest.raises(ProviderRefused):
        FileSecretProvider(secret_dir).fetch("subdir")


def test_a_root_that_is_a_file_is_unavailable(secret_dir):
    with pytest.raises(ProviderUnavailable):
        FileSecretProvider(secret_dir / "api-token").fetch("api-token")


def test_a_short_value_is_refused_with_the_length_in_the_message(secret_dir):
    (secret_dir / "tiny").write_text("abc", encoding="utf-8")
    with pytest.raises(ProviderRefused, match=f"at least {MIN_SECRET_LEN}"):
        FileSecretProvider(secret_dir).fetch("tiny")


def test_an_empty_file_is_refused_rather_than_registering_nothing(secret_dir):
    (secret_dir / "empty").write_bytes(b"")
    with pytest.raises(ProviderRefused, match="0 byte"):
        FileSecretProvider(secret_dir).fetch("empty")


def test_a_value_that_is_only_a_newline_is_empty_after_stripping(secret_dir):
    (secret_dir / "just-newline").write_bytes(b"\n")
    with pytest.raises(ProviderRefused, match="0 byte"):
        FileSecretProvider(secret_dir).fetch("just-newline")


def test_a_nul_byte_is_refused_because_it_would_truncate_later(secret_dir):
    (secret_dir / "nulled").write_bytes(b"abcdefgh\x00ijkl")
    with pytest.raises(ProviderRefused, match="NUL"):
        FileSecretProvider(secret_dir).fetch("nulled")


def test_invalid_utf8_is_refused_at_load_not_at_reveal(secret_dir):
    """reveal() decodes as UTF-8, so undecodable bytes fail there.

    "There" is inside a run, under a grant, at the moment the credential was
    finally needed. Failing at load turns that into a startup error.
    """
    (secret_dir / "binary").write_bytes(b"\xff\xfe" * 8)
    with pytest.raises(ProviderRefused, match="not valid UTF-8"):
        FileSecretProvider(secret_dir).fetch("binary")


def test_an_oversized_file_is_refused_rather_than_read_whole(secret_dir):
    (secret_dir / "huge").write_bytes(b"x" * (MAX_SECRET_BYTES + 1))
    with pytest.raises(ProviderRefused):
        FileSecretProvider(secret_dir).fetch("huge")


def test_the_root_can_be_held_open_across_several_fetches(secret_dir):
    with FileSecretProvider(secret_dir) as p:
        first = bytes(p.fetch("api-token"))
        second = bytes(p.fetch("db-password"))
    assert first.decode() == VALUE and second.decode() == OTHER


def test_a_held_root_is_closed_on_the_way_out(secret_dir):
    p = FileSecretProvider(secret_dir)
    with p:
        assert p._open is not None
    assert p._open is None, (
        "a descriptor held for the session has to be released with it, or a "
        "long-lived process leaks one per provider")


def test_a_refusal_inside_a_held_root_leaves_it_usable(secret_dir):
    with FileSecretProvider(secret_dir) as p:
        with pytest.raises(ProviderRefused):
            p.fetch("never-registered")
        assert bytes(p.fetch("api-token")).decode() == VALUE, (
            "one bad lookup must not close the root out from under the next")


# ---- the mapping provider ------------------------------------------------
def test_the_mapping_provider_applies_the_same_rules(tmp_path):
    p = MappingSecretProvider({"api-token": VALUE, "tiny": "abc"})
    assert bytes(p.fetch("api-token")).decode() == VALUE
    with pytest.raises(ProviderRefused, match=f"at least {MIN_SECRET_LEN}"):
        p.fetch("tiny")
    with pytest.raises(ProviderRefused, match="holds no secret"):
        p.fetch("absent")


def test_the_mapping_provider_hands_out_a_copy(tmp_path):
    p = MappingSecretProvider({"api-token": VALUE})
    first = p.fetch("api-token")
    for i in range(len(first)):
        first[i] = 0
    assert bytes(p.fetch("api-token")).decode() == VALUE, (
        "the store zeroes what it is given; if that were the provider's own "
        "buffer, the second load would return zeros")


# ---- provisioning into the store ----------------------------------------
def test_provision_registers_and_returns_a_reference_not_a_value(secret_dir):
    store = SecretStore()
    ref = store.provision(FileSecretProvider(secret_dir), "api-token")
    assert isinstance(ref, SecretRef)
    assert store.secret_ids() == ("api-token",)
    assert VALUE not in str(ref) and VALUE not in repr(ref)


def test_a_provisioned_secret_resolves_through_the_ordinary_grant_path(
        secret_dir, log):
    store = SecretStore(log)
    ref = store.provision(FileSecretProvider(secret_dir), "api-token")
    store.issue(grant(grant_id="sg1", subject="agent", task_id="t1",
                      tool_id="fetch", secret_id="api-token",
                      purposes=("call-api",)), actor="owner")
    handle = store.resolve(ref, grant_id="sg1", actor="agent", task_id="t1",
                           tool_id="fetch", purpose="call-api")
    assert handle.reveal() == VALUE, (
        "a provisioned secret is an ordinary secret; if the load path had "
        "its own resolution it would have its own authority checks too")


def test_the_provision_event_carries_provenance_and_no_value(
        secret_dir, log):
    store = SecretStore(log)
    store.provision(FileSecretProvider(secret_dir), "api-token",
                    actor="deploy-bot")
    recs = [e for e in log.read() if e.action == ACT_SECRET_PROVISION]
    assert len(recs) == 1
    ev = recs[0]
    assert ev.actor == "deploy-bot" and ev.target == "api-token"
    assert ev.payload["provider"]["kind"] == "file"
    body = log.path.read_text(encoding="utf-8")
    assert VALUE not in body, "the log must never carry a secret value"
    import hashlib
    for algo in ("sha256", "sha1", "md5"):
        d = hashlib.new(algo, VALUE.encode()).hexdigest()
        assert d not in body, (
            f"the log carries a {algo} of the value, which is an offline "
            "guessing oracle published by this repository")


def test_the_recorded_length_is_the_stored_length(secret_dir, log):
    store = SecretStore(log)
    store.provision(FileSecretProvider(secret_dir), "db-password")
    ev = [e for e in log.read()
          if e.action == ACT_SECRET_PROVISION][0]
    assert ev.payload["bytes"] == len(OTHER), (
        "recorded after stripping, or an operator comparing it against the "
        "credential they issued sees an off-by-one and mistrusts the store")


def test_a_provider_description_containing_the_value_is_refused(
        secret_dir, log):
    """A configured URL with the token in it is the ordinary case.

    Not an exotic attack: ``https://user:TOKEN@host/`` is how half the
    world configures a client, and describe() is written to the log.
    """
    store = SecretStore(log)

    class Leaky(SecretProvider):
        kind = "leaky"

        def describe(self):
            return {"kind": self.kind, "source": f"https://x:{VALUE}@h/"}

        def fetch(self, secret_id):
            return bytearray(VALUE.encode())

    with pytest.raises(SecretError, match="provider description"):
        store.provision(Leaky(), "api-token")
    assert store.secret_ids() == (), (
        "the secret must not stay registered after the provenance for it "
        "was refused")
    body = (log.path.read_text(encoding="utf-8")
            if log.path.exists() else "")
    assert VALUE not in body
    assert not [e for e in log.read() if e.action == ACT_SECRET_PROVISION], (
        "a refused provisioning must leave no provenance behind either")


def test_provisioning_twice_is_refused_before_the_value_is_read(secret_dir):
    """The duplicate check has to come first, or it costs a read.

    Fetching a credential this process has already decided it will not
    store puts plaintext in memory for nothing.
    """
    store = SecretStore()
    store.provision(FileSecretProvider(secret_dir), "api-token")

    reads = []

    class Counting(SecretProvider):
        kind = "counting"

        def describe(self):
            return {"kind": self.kind}

        def fetch(self, secret_id):
            reads.append(secret_id)
            return bytearray(VALUE.encode())

    with pytest.raises(SecretError, match="already registered"):
        store.provision(Counting(), "api-token")
    assert reads == []


def test_a_provider_returning_a_str_is_refused(secret_dir):
    class Sloppy(SecretProvider):
        kind = "sloppy"

        def describe(self):
            return {"kind": self.kind}

        def fetch(self, secret_id):
            return VALUE

    with pytest.raises(ProviderError, match="bytearray"):
        SecretStore().provision(Sloppy(), "api-token")


def test_something_that_is_not_a_provider_is_refused(secret_dir):
    class QuacksRight:
        kind = "duck"

        def describe(self):
            return {"kind": "duck"}

        def fetch(self, secret_id):
            return bytearray(VALUE.encode())

    with pytest.raises(ProviderError, match="SecretProvider"):
        SecretStore().provision(QuacksRight(), "api-token")


def test_the_base_provider_supplies_no_behaviour(secret_dir):
    with pytest.raises(NotImplementedError):
        SecretProvider().fetch("api-token")
    with pytest.raises(NotImplementedError):
        SecretProvider().describe()


# ---- all or nothing ------------------------------------------------------
def test_provision_all_loads_every_id(secret_dir, log):
    store = SecretStore(log)
    refs = store.provision_all(FileSecretProvider(secret_dir),
                               ["api-token", "db-password"])
    assert [r.secret_id for r in refs] == ["api-token", "db-password"]
    assert store.secret_ids() == ("api-token", "db-password")


def test_provision_all_with_no_ids_is_a_refusal_not_an_empty_success(
        secret_dir):
    """The vacuous-success defect, refused at the one place it would recur.

    A deployment step that loads an empty credential list and reports
    success is indistinguishable from one that worked, right up until the
    first call that needed a secret.
    """
    with pytest.raises(ProviderError, match="vacuous"):
        SecretStore().provision_all(FileSecretProvider(secret_dir), [])


def test_provision_all_rolls_back_what_it_already_loaded(secret_dir, log):
    store = SecretStore(log)
    with pytest.raises(ProviderRefused, match="holds no secret"):
        store.provision_all(FileSecretProvider(secret_dir),
                            ["api-token", "absent", "db-password"])
    assert store.secret_ids() == (), (
        "a half-loaded credential set fails later, inside the run, at the "
        "one call that needed the missing one")


def test_rollback_zeroes_rather_than_dropping(secret_dir):
    store = SecretStore()
    with pytest.raises(ProviderRefused):
        store.provision_all(FileSecretProvider(secret_dir),
                            ["api-token", "absent"])
    assert not store.redactor().contains_secret(VALUE), (
        "a forgotten secret must leave the redactor too, or the store keeps "
        "answering questions about a value it no longer holds")


def test_duplicate_ids_are_named_as_a_caller_mistake(secret_dir):
    with pytest.raises(ProviderError, match="duplicate"):
        SecretStore().provision_all(FileSecretProvider(secret_dir),
                                    ["api-token", "api-token"])


def test_a_rolled_back_id_can_be_provisioned_again(secret_dir):
    store = SecretStore()
    with pytest.raises(ProviderRefused):
        store.provision_all(FileSecretProvider(secret_dir),
                            ["api-token", "absent"])
    ref = store.provision(FileSecretProvider(secret_dir), "api-token")
    assert store.secret_ids() == ("api-token",) and ref.secret_id


# ---- the seam is the only door -------------------------------------------
def test_no_module_in_the_package_reads_a_credential_from_the_environment():
    """The point of the whole module, checked structurally.

    ``os.environ.get("...TOKEN")`` anywhere in the package would be a second
    door into the same room, with none of the refusals above on it.
    """
    import ast

    from qta_agent.secrets import looks_like_a_secret_key

    offenders = []
    inspected = 0
    for path in sorted((ROOT / "qta_agent").glob("*.py")):
        inspected += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for arg in node.args:
                if not (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)):
                    continue
                if not looks_like_a_secret_key(arg.value):
                    continue
                src = ast.unparse(node.func)
                if "environ" in src or "getenv" in src:
                    offenders.append(f"{path.name}:{node.lineno} {src}")
    assert inspected > 10, (
        f"only {inspected} module(s) inspected; a structural check that "
        "reads almost nothing passes for the wrong reason")
    assert not offenders, offenders


def test_the_governed_environment_still_inherits_nothing(secret_dir,
                                                         monkeypatch):
    """Provisioning must not have opened a path back to inheritance.

    The strongest property R33 has is that a governed child's environment is
    BUILT, not copied. Adding a provider is only an improvement if that is
    still true afterwards.
    """
    from qta_agent.governed_stage10 import GOVERNED_ENV_KEYS

    monkeypatch.setenv("QTA_API_TOKEN", VALUE)
    assert "QTA_API_TOKEN" not in GOVERNED_ENV_KEYS
    assert os.environ["QTA_API_TOKEN"] == VALUE
