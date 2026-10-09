"""The provider seam, the history's security profile, and who reads what.

Three claims, held here (D-2026-92):

* THE PRIMITIVE. :mod:`qta_agent.ed25519` is a REFERENCE: it verifies, and
  it signs only test identities. Production authentication needs a provider
  that states VETTED and passes a conformance gate on behaviour, not on its
  label -- and a provisioned registry. Neither exists here.
* THE PROFILE. A history declared ``AUTHENTICATED_REQUIRED`` is enforced in
  the verified-read primitives, so a reader cannot skip authentication by
  omitting an argument: given no authenticator, it is given no events.
* THE COVERAGE. ``principals.READER_COVERAGE`` says what each reader does;
  every module that reads the log is in it, and each claim is exercised.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import events as ev_mod  # noqa: E402
from qta_agent import principals as pr  # noqa: E402
from qta_agent import signature  # noqa: E402
from qta_agent.agents import AgentDirectory  # noqa: E402
from qta_agent.audit import AuditIndex  # noqa: E402
from qta_agent.canonical import canonical_bytes, digest  # noqa: E402
from qta_agent.capability import CapabilityLedger  # noqa: E402
from qta_agent.events import (  # noqa: E402
    ACT_SECURITY_PROFILE, PROFILE_AUTHENTICATED_REQUIRED,
    PROFILE_UNAUTHENTICATED_LEGACY, ChainBroken, EventLog, EventLogError,
)
from qta_agent.idempotency import IdempotencyLedger  # noqa: E402
from qta_agent.memory import MemoryStore  # noqa: E402
from qta_agent.netauth import NetworkAuthority  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.proposals import received  # noqa: E402
from qta_agent.reconstruct import reconstruct, reconstruct_tasks  # noqa: E402
from qta_agent.scheduler import Scheduler  # noqa: E402
from qta_agent.secrets import SecretStore  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402

OWNER = pr.test_identity("owner")
ALICE = pr.test_identity("alice")
REQUIRED = PROFILE_AUTHENTICATED_REQUIRED


# ---- the provider seam -----------------------------------------------------
@dataclass(frozen=True)
class _StandIn(signature.ReferenceEd25519):
    """A provider that SAYS vetted and behaves exactly like Ed25519: the
    shape a deployment's injected adapter has. Named for what it is."""
    provider_id: str = "test/stand-in-vetted"
    assurance: str = signature.VETTED


@dataclass(frozen=True)
class _Liar(_StandIn):
    """Says vetted; accepts any well-shaped signature."""
    provider_id: str = "test/liar"

    def verify(self, public, message, signature_):
        if len(public) == 32 and len(signature_) == 64:
            return True
        return super().verify(public, message, signature_)


@dataclass(frozen=True)
class _Thrower(_StandIn):
    """Says vetted; raises on a malformed input instead of answering."""
    provider_id: str = "test/thrower"

    def verify(self, public, message, signature_):
        if len(signature_) != 64:
            raise ValueError("bad length")
        return super().verify(public, message, signature_)


def test_the_reference_conforms_and_is_still_not_a_production_provider():
    assert signature.conformance_problems(signature.REFERENCE) == []
    with pytest.raises(signature.ProviderError, match="REFERENCE_ONLY"):
        signature.require_vetted(signature.REFERENCE)


def test_saying_vetted_is_not_enough():
    with pytest.raises(signature.ProviderError) as caught:
        signature.require_vetted(_Liar())
    for accepted in ("another message", "a flipped signature bit",
                     "another key", "the malleable s + Q"):
        assert f"accepts {accepted}" in str(caught.value), caught.value


def test_a_verifier_that_raises_is_refused_rather_than_trusted():
    with pytest.raises(signature.ProviderError, match="raised ValueError"):
        signature.require_vetted(_Thrower())


@dataclass(frozen=True)
class _WrongSigner(_StandIn):
    """Says vetted and verifies correctly; SIGNS something else."""
    provider_id: str = "test/wrong-signer"

    def sign(self, secret, message):
        return super().sign(secret, message + b"!")


def test_a_provider_that_signs_what_the_rfc_does_not_is_refused():
    with pytest.raises(signature.ProviderError,
                       match="signature differs from RFC 8032"):
        signature.require_vetted(_WrongSigner())


def test_a_provider_that_behaves_like_ed25519_and_says_vetted_is_accepted():
    provider = _StandIn()
    assert signature.require_vetted(provider) is provider


def test_a_production_key_is_never_signed_with_the_reference():
    production = pr.Signer("carol", bytes(range(32)))
    ev = SimpleEvent(seq=0, hash="0" * 64, actor="carol")
    with pytest.raises(pr.AuthenticationError, match="signs only TEST"):
        production.attest(ev)
    vetted = pr.Signer("carol", bytes(range(32)), provider=_StandIn())
    assert vetted.attest(ev)["key_id"] == vetted.key_id


@dataclass(frozen=True)
class SimpleEvent:
    seq: int
    hash: str
    actor: str


def test_production_authentication_needs_a_registry_and_a_vetted_provider(
        tmp_path, monkeypatch):
    att = pr.Attestations(tmp_path / "a.jsonl")
    with pytest.raises(pr.NotConfigured):
        pr.production_authenticator(att, provider=_StandIn())
    carol = pr.Signer("carol", bytes(range(32)), provider=_StandIn())
    doc = tmp_path / "registry.json"
    doc.write_text(json.dumps(pr.KeyRegistry([carol.registered()])
                              .to_document()), encoding="utf-8")
    monkeypatch.setattr(pr, "PRODUCTION_KEYS", str(doc))
    with pytest.raises(pr.NotConfigured):
        pr.production_authenticator(att, provider=_StandIn())
    monkeypatch.setattr(pr, "PRODUCTION_REGISTRY_DIGEST", pr.registry_digest(
        json.loads(doc.read_text(encoding="utf-8"))))
    with pytest.raises(signature.ProviderError, match="REFERENCE_ONLY"):
        pr.production_authenticator(att, provider=signature.REFERENCE)
    with pytest.raises(signature.ProviderError):
        pr.production_authenticator(att, provider=_Liar())
    auth = pr.production_authenticator(att, provider=_StandIn())
    assert auth.provider.provider_id == "test/stand-in-vetted"


def test_the_authenticator_verifies_with_its_provider(tmp_path):
    """A provider that refuses every signature refuses every event: the
    verdict is the provider's, not a hard-wired primitive's."""
    @dataclass(frozen=True)
    class _Refuser(_StandIn):
        def verify(self, public, message, signature_):
            return False
    log, att = _required_history(tmp_path)
    registry = pr.KeyRegistry([OWNER.registered(), ALICE.registered()],
                              allow_test_keys=True)
    report, _ = log.read_verified()
    assert report.ok
    events = _events_of(tmp_path)
    assert pr.authenticate(events, att, registry).ok
    refused = pr.authenticate(events, att, registry, provider=_Refuser())
    assert refused.refused == {ev.seq for ev in events}


# ---- the profile -------------------------------------------------------------
def _authenticator(att):
    return pr.Authenticator(att, pr.KeyRegistry(
        [OWNER.registered(), ALICE.registered()], allow_test_keys=True))


def _required_history(tmp_path, *, records=2):
    """A REQUIRED history: the declaration, then ``records`` records by
    alice, every event attested. Returns the log object WITH an
    authenticator, as a deployment would hold it."""
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    log = EventLog(tmp_path / "log.jsonl", authenticator=_authenticator(att))
    pr.begin_history(log, att, OWNER)
    for i in range(records):
        pr.signed_append(log, att, ALICE, action="record.create",
                         target=f"r{i}",
                         payload={"record_id": f"r{i}", "kind": "k",
                                  "proposer": "alice"})
    return log, att


def _events_of(tmp_path):
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    report, events = EventLog(tmp_path / "log.jsonl",
                              authenticator=_authenticator(att)
                              ).read_verified()
    report.raise_if_bad()
    return events


def _forge(path, *, actor, action, target, payload, seq=None):
    """Append a correctly chained record by writing the FILE, as a writer
    holding it can: no write-side guard runs."""
    log = EventLog(path)
    raw = Path(path).read_bytes() if Path(path).exists() else b""
    last = [json.loads(x) for x in raw.splitlines()]
    prev = last[-1] if last else None
    body = {"seq": seq if seq is not None else (prev["seq"] + 1 if prev
                                                else 0),
            "event_id": f"forged-{len(last)}", "wall_time": 1.0,
            "actor": actor, "action": action, "target": target,
            "payload": payload,
            "prev_hash": prev["hash"] if prev else "0" * 64,
            "canonical_form_version": ev_mod.CANONICAL_FORM_VERSION}
    rec = dict(body, hash=digest(body))
    with Path(path).open("ab") as fh:
        fh.write(canonical_bytes(rec) + b"\n")
    return log


def test_a_history_with_no_declaration_reads_as_it_always_did(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="p", action="record.create", target="r",
               payload={"record_id": "r", "kind": "k", "proposer": "p"})
    report, events = EventLog(tmp_path / "log.jsonl").read_verified()
    assert report.ok and len(events) == 1


def test_a_required_history_gives_a_reader_without_an_authenticator_nothing(
        tmp_path):
    _required_history(tmp_path)
    report, events = EventLog(tmp_path / "log.jsonl").read_verified()
    assert not report.ok
    assert events == []
    assert any(REQUIRED in p and "no authenticator" in p
               for p in report.problems), report.problems


def test_with_the_authenticator_it_is_read_whole(tmp_path):
    _required_history(tmp_path)
    events = _events_of(tmp_path)
    assert [e.action for e in events] == [
        ACT_SECURITY_PROFILE, "record.create", "record.create"]


def test_an_unattested_event_ends_what_is_read(tmp_path):
    log, att = _required_history(tmp_path)
    # Past the write-side guard, which refuses an unsigned append to a
    # REQUIRED history outright (D-2026-93): written as a holder of the
    # file can write it.
    _forge(tmp_path / "log.jsonl", actor="alice", action="record.create",
           target="r9", payload={"record_id": "r9", "kind": "k",
                                 "proposer": "alice"})
    # A writer does not build on it either: its own head check is gated.
    with pytest.raises(ev_mod.ChainBroken, match="MISSING"):
        pr.signed_append(log, att, ALICE, action="record.create",
                         target="r10", payload={"record_id": "r10",
                                                "kind": "k",
                                                "proposer": "alice"})
    report, events = EventLog(tmp_path / "log.jsonl",
                              authenticator=_authenticator(att)
                              ).read_verified()
    assert not report.ok
    assert [e.seq for e in events] == [0, 1, 2], (
        "the events read are the prefix before the first refused one")
    assert any("MISSING" in p for p in report.problems), report.problems


def test_a_profile_is_declared_once_and_first_on_the_write_side(tmp_path):
    log, att = _required_history(tmp_path)
    with pytest.raises(pr.AuthenticationError, match="first event"):
        pr.begin_history(log, att, OWNER)
    with pytest.raises(EventLogError, match="declared once"):
        log.append(actor="owner", action=ACT_SECURITY_PROFILE,
                   target="history",
                   payload={"profile": PROFILE_UNAUTHENTICATED_LEGACY})
    with pytest.raises(EventLogError, match="must be one of"):
        EventLog(tmp_path / "other.jsonl").append(
            actor="owner", action=ACT_SECURITY_PROFILE, target="history",
            payload={"profile": "WHATEVER"})


def test_a_declaration_written_anywhere_else_is_refused_on_read(tmp_path):
    """Written past the write-side guard, straight into the file."""
    path = tmp_path / "log.jsonl"
    EventLog(path).append(actor="p", action="record.create", target="r",
                          payload={"record_id": "r", "kind": "k",
                                   "proposer": "p"})
    _forge(path, actor="p", action=ACT_SECURITY_PROFILE, target="history",
           payload={"profile": PROFILE_UNAUTHENTICATED_LEGACY})
    report, events = EventLog(path).read_verified()
    assert not report.ok and events == []
    assert any("declares its security profile once" in p
               for p in report.problems), report.problems


def test_an_unknown_declared_profile_reads_as_nothing(tmp_path):
    path = tmp_path / "log.jsonl"
    _forge(path, actor="p", action=ACT_SECURITY_PROFILE, target="history",
           payload={"profile": "LAX"})
    report, events = EventLog(path).read_verified()
    assert not report.ok and events == []
    assert any("unknown security profile" in p for p in report.problems)


def test_the_anchored_read_is_gated_too(tmp_path):
    log, att = _required_history(tmp_path)
    anchor = log._anchor
    assert anchor is not None
    pr.signed_append(log, att, ALICE, action="record.create", target="r5",
                     payload={"record_id": "r5", "kind": "k",
                              "proposer": "alice"})
    bare = EventLog(tmp_path / "log.jsonl")
    report, tail = bare.read_verified_from(anchor)
    assert not report.ok and tail == []
    assert any("no authenticator" in p for p in report.problems)
    report, tail = EventLog(tmp_path / "log.jsonl",
                            authenticator=_authenticator(att)
                            ).read_verified_from(anchor)
    assert report.ok and [e.target for e in tail] == ["r5"]



def test_a_reader_that_saw_the_history_empty_does_not_keep_it_legacy(
        tmp_path):
    """A log object that read the history before its first event must not
    remember that emptiness as a profile: genesis then declared REQUIRED,
    and an anchored read -- which takes genesis's profile as known -- would
    fold the tail with no authenticator at all."""
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    reader = EventLog(tmp_path / "log.jsonl")
    reader.read_verified()
    writer = EventLog(tmp_path / "log.jsonl",
                      authenticator=_authenticator(att))
    pr.begin_history(writer, att, OWNER)
    for i in range(3):
        pr.signed_append(writer, att, ALICE, action="record.create",
                         target=f"r{i}",
                         payload={"record_id": f"r{i}", "kind": "k",
                                  "proposer": "alice"})
    report, events = reader.read_verified_from(writer.anchor_at(1))
    assert not report.ok and events == [], (report.problems, events)
    assert any("no authenticator" in p for p in report.problems)


def test_a_writer_without_an_authenticator_cannot_extend_it(tmp_path):
    """The writer's head check is the gate too -- also when it is an
    anchored read whose tail is empty, which is exactly what a writer that
    wrote the head itself performs."""
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    bare = EventLog(tmp_path / "log.jsonl")
    pr.begin_history(bare, att, OWNER, registry=_authenticator(att).registry)
    assert bare._anchor is not None, "the writer holds an anchor at its head"
    with pytest.raises(ChainBroken, match="no authenticator"):
        pr.signed_append(bare, att, ALICE, registry=_authenticator(att).registry,
                         action="record.create",
                         target="r0", payload={"record_id": "r0", "kind": "k",
                                               "proposer": "alice"})
    report = bare.verify_from(bare._anchor)
    assert not report.ok
    held = EventLog(tmp_path / "log.jsonl", authenticator=_authenticator(att))
    pr.signed_append(held, att, ALICE, action="record.create", target="r0",
                     payload={"record_id": "r0", "kind": "k",
                              "proposer": "alice"})

def test_a_deployment_that_pins_the_profile_refuses_a_downgrade(tmp_path):
    """The rewrite the declaration cannot stop on its own: the whole history
    written again WITHOUT it. A pinned deployment refuses it."""
    path = tmp_path / "log.jsonl"
    EventLog(path).append(actor="alice", action="record.create", target="r",
                          payload={"record_id": "r", "kind": "k",
                                   "proposer": "alice"})
    report, events = EventLog(path).read_verified()
    assert report.ok, "unpinned, a history with no declaration is legacy"
    report, events = EventLog(path, required_profile=REQUIRED
                              ).read_verified()
    assert not report.ok and events == []
    assert any(p.startswith("DOWNGRADE") for p in report.problems)
    empty = EventLog(tmp_path / "new.jsonl", required_profile=REQUIRED)
    assert empty.read_verified()[0].ok, "an empty history downgrades nothing"


def test_an_unknown_pinned_profile_is_refused_when_the_log_is_opened(tmp_path):
    with pytest.raises(EventLogError, match="unknown security profile"):
        EventLog(tmp_path / "log.jsonl", required_profile="LAX")


# ---- reader by reader --------------------------------------------------------
_POLICY_ID = "scheduler.default"

#: Readers exercised here by BEHAVIOUR: each is built over a REQUIRED history
#: on a log with no authenticator and must refuse, then over the same history
#: on a log WITH one and must load. The rest of READER_COVERAGE's GATED
#: entries -- GovernedStage10, GovernedOrigins, CheckpointStore -- need a
#: governed root or a checkpoint directory to build; they are held by the
#: structural test below, which proves they read only through the gated
#: primitives.
BEHAVIOUR = {
    "qta_agent.store.AuthorityStore": lambda log: AuthorityStore(log).load(),
    "qta_agent.reconstruct": lambda log: (reconstruct(log),
                                          reconstruct_tasks(log)),
    "qta_agent.scheduler.Scheduler": lambda log: Scheduler(
        log, policy=PolicyStore(log).load(), policy_id=_POLICY_ID,
        capacity={"slots": 1}).load(),
    "qta_agent.memory.MemoryStore": lambda log: MemoryStore(log).load(),
    "qta_agent.policy.PolicyStore": lambda log: PolicyStore(log).load(),
    "qta_agent.audit.AuditIndex": lambda log: AuditIndex.from_log(log),
    "qta_agent.agents.AgentDirectory": lambda log: AgentDirectory(log).load(),
    "qta_agent.capability.CapabilityLedger":
        lambda log: CapabilityLedger(log).load(),
    "qta_agent.idempotency.IdempotencyLedger":
        lambda log: IdempotencyLedger(log).load(),
    "qta_agent.netauth.NetworkAuthority":
        lambda log: NetworkAuthority(log).load(),
    "qta_agent.secrets.SecretStore": lambda log: SecretStore(log).load(),
    "qta_agent.proposals.received": lambda log: received(log),
}
STRUCTURAL_ONLY = {"qta_agent.governed_stage10.GovernedStage10",
                   "qta_agent.governed_model.GovernedOrigins",
                   "qta_agent.checkpoint.CheckpointStore"}


@pytest.mark.parametrize("reader", sorted(BEHAVIOUR))
def test_each_gated_reader_refuses_without_and_reads_with(tmp_path, reader):
    assert pr.READER_COVERAGE[reader] in (pr.GATED, pr.GATED_AND_OWN_VERDICT)
    log, att = _required_history(tmp_path, records=0)
    with pytest.raises(Exception, match=REQUIRED):
        BEHAVIOUR[reader](EventLog(tmp_path / "log.jsonl"))
    BEHAVIOUR[reader](EventLog(tmp_path / "log.jsonl",
                               authenticator=_authenticator(att)))


def _last_name(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def test_the_coverage_table_covers_every_module_that_reads_the_log():
    """Every qta_agent module that calls a verified-read primitive is in the
    table -- and ``tools/verified_read_guard.py`` holds that no production
    code reads the log any other way, so every one of them is gated."""
    readers = set()
    for path in sorted((ROOT / "qta_agent").glob("*.py")):
        if path.name == "events.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # A call on a LOG -- the receiver's last name is ``log`` or ends
            # in ``_log``, the rule tools/verified_read_guard.py states --
            # so a signature's ``provider.verify`` is not a log read.
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("read_verified",
                                           "read_verified_from", "verify",
                                           "verify_from", "advance")
                    and _last_name(node.func.value).endswith("log")):
                readers.add(f"qta_agent.{path.stem}")
    assert {"qta_agent.store", "qta_agent.scheduler",
            "qta_agent.reconstruct"} <= readers, (
        f"the scan found too little to mean anything: {sorted(readers)}")
    covered = {k.rsplit(".", 1)[0] if k.count(".") > 1 else k
               for k in pr.READER_COVERAGE if k.startswith("qta_agent.")}
    missing = sorted(r for r in readers if r not in covered)
    assert not missing, f"read the log and are not in READER_COVERAGE: {missing}"
    assert set(BEHAVIOUR) | STRUCTURAL_ONLY == {
        k for k, v in pr.READER_COVERAGE.items()
        if v in (pr.GATED, pr.GATED_AND_OWN_VERDICT)}


@pytest.mark.parametrize("tool, args", [
    ("tools/audit_log.py", ["verify"]),
    ("tools/generic_consistency.py", ["--evidence", "EVIDENCE"]),
    ("tools/independent_verify.py", []),
])
def test_a_tool_that_opens_its_own_log_refuses_a_required_history(
        tmp_path, tool, args):
    assert pr.READER_COVERAGE[tool] == pr.REFUSES_REQUIRED_ONLY
    _required_history(tmp_path)
    (tmp_path / "evidence").mkdir()
    argv = [str(tmp_path / "log.jsonl")] + [
        str(tmp_path / "evidence") if a == "EVIDENCE" else a for a in args]
    run = subprocess.run([sys.executable, str(ROOT / tool), *argv],
                         cwd=ROOT, capture_output=True, text=True,
                         timeout=120)
    assert run.returncode != 0, (tool, run.stdout[-500:], run.stderr[-500:])
    assert REQUIRED in run.stdout + run.stderr, (run.stdout[-800:],
                                                 run.stderr[-800:])


def test_the_hypothesis_lifecycle_is_recorded_as_not_built():
    assert pr.READER_COVERAGE["hypothesis lifecycle"] == pr.NOT_BUILT
    assert not (ROOT / "qta_agent" / "hypothesis.py").exists(), (
        "a hypothesis module exists: say what it does with authentication")


def test_the_independent_reader_refuses_in_its_own_code(tmp_path, monkeypatch):
    """With the read primitive's gate switched OFF, the independent reader
    still folds nothing of a REQUIRED history it was given no authenticator
    for: its refusal is its own, not the primitive's."""
    _required_history(tmp_path)
    monkeypatch.setattr(EventLog, "_security_gate",
                        lambda self, events, problems, *, whole: events)
    out = reconstruct(EventLog(tmp_path / "log.jsonl"))
    assert out.records == {}
    assert any(REQUIRED in u for u in out.unauthorized), out.unauthorized


def test_the_independent_reader_refuses_a_misplaced_declaration(
        tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    EventLog(path).append(actor="p", action="record.create", target="r",
                          payload={"record_id": "r", "kind": "k",
                                   "proposer": "p"})
    _forge(path, actor="p", action=ACT_SECURITY_PROFILE, target="history",
           payload={"profile": PROFILE_UNAUTHENTICATED_LEGACY})
    monkeypatch.setattr(EventLog, "_security_gate",
                        lambda self, events, problems, *, whole: events)
    out = reconstruct(EventLog(path))
    assert any("declared once" in u for u in out.unauthorized), (
        out.unauthorized)
