"""A projection that refused a history does not resume past the refusal.

D-2026-156. Every writer that decides under the lock first folds what other
writers appended since it last read, O(new) from an anchor. Each fold moved
its anchor past the new records BEFORE applying them, and most marked a
record folded before checking it. So when a record was refused -- a forged
or conflicting record written outside the gate -- the first decision was
refused, correctly, and the SECOND call started after the refused record:
it decided against a projection that had silently skipped it, and wrote.
Measured at b9b2187 on the memory store: attempt 1 refused ("written
twice"), attempt 2 written, onto a history every fresh load refuses. A
refused load() had the same hole from the other side: it emptied the
projection, kept the old anchor, and the next catch-up advanced from there
over a projection with nothing in it.

Each case below gives one writer a history it must refuse, then asks it to
decide twice. Both must be refused and nothing may be written. The control
takes the same two decisions without the forged record.
"""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import test_t2_closure as T2  # noqa: E402
from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate.agents import (AgentDirectory, AgentRole,  # noqa: E402
                                    IdentityError, PrincipalKind, identity)
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.capability import (ACT_ISSUE, Action,  # noqa: E402
                                        CapabilityError, CapabilityLedger,
                                        issue)
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.idempotency import (ACT_BIND,  # noqa: E402
                                         IdempotencyError, IdempotencyLedger)
from agent_substrate.memory import (ACT_MEMORY_WRITE,  # noqa: E402
                                    MemoryError_, MemoryStore)
from agent_substrate.netauth import (ACT_NET_GRANT,  # noqa: E402
                                     NetworkAuthority, NetworkError, grant)
from agent_substrate.policy import (ACT_POLICY_DECISION,  # noqa: E402
                                    PolicyError, PolicyRequest, PolicyStore)
from agent_substrate.scheduler import (Scheduler, SchedulerError,  # noqa: E402
                                       default_policy)
from agent_substrate.secrets import (ACT_SECRET_GRANT,  # noqa: E402
                                     SecretError, SecretStore)
from agent_substrate.secrets import grant as secret_grant  # noqa: E402
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402

ACTOR, TASK, TOOL = T2.ACTOR, T2.TASK, T2.TOOL


def _head(p: Path) -> int:
    return EventLog(p).verify().head_seq


def _forge_variant(p: Path, action: str, mutate) -> None:
    """Re-append the first ``action`` record with ``mutate`` applied to a
    copy of its payload: a writer outside the gate, using a real record's
    shape."""
    log = EventLog(p)
    report, events = log.read_verified()
    report.raise_if_bad()
    first = next(ev for ev in events if ev.action == action)
    payload = copy.deepcopy(first.payload)
    mutate(payload)
    log.append(actor=first.actor, action=action, target=first.target,
               payload=payload)


@dataclass(frozen=True)
class Case:
    name: str
    #: A loaded writer, holding an anchor, over a history it accepts.
    make: Callable[[Path], object]
    #: Append one record that the writer's own replay refuses.
    forge: Callable[[Path], None]
    #: One ordinary decision that writes; ``i`` keeps ids distinct.
    decide: Callable[[object, int], object]
    #: A fresh projection of ``p``.
    reload: Callable[[Path], object]
    refusal: type
    has_load: bool = True


# ---- capability ledger ------------------------------------------------------

def _forge_backdated(p: Path) -> None:
    bad = issue(capability_id="cap-forged", subject="mallory",
                action=Action.EXECUTE_TOOL, task_id=TASK, tool_id=TOOL,
                scope=("out",), issued_seq=0)
    EventLog(p).append(actor="mallory", action=ACT_ISSUE, target=TASK,
                       payload={"task_id": TASK, **bad.body()})


def _cap_decide(led, i):
    return led.issue(issue(capability_id=f"c-new-{i}", subject=ACTOR,
                           action=Action.EXECUTE_TOOL, task_id=TASK,
                           tool_id=TOOL, scope=("out",), issued_seq=0),
                     actor="root")


# ---- egress authority and secret store --------------------------------------

def _egress_decide(a, i):
    return a.issue(grant(grant_id=f"g-new-{i}", subject=ACTOR, task_id=TASK,
                         tool_id=TOOL, schemes=("https",),
                         hosts=("api.example.com",), ports=(443,),
                         methods=("GET",), paths=("/v1",)),
                   actor="scheduler")


def _secrets_decide(s, i):
    return s.issue(secret_grant(grant_id=f"sg-new-{i}", subject=ACTOR,
                                task_id=TASK, tool_id=TOOL,
                                secret_id="api-token",
                                purposes=("call-schema-api",)),
                   actor="owner")


def _widen(field, value):
    def mutate(payload):
        payload["grant"][field] = value
    return mutate


# ---- agent directory and memory store ---------------------------------------

def _directory(p: Path) -> AgentDirectory:
    d = AgentDirectory(EventLog(p)).load()
    d.register(identity(agent_id="proposer", instance_id="p1",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.PROPOSER}), by="system")
    return d


def _forge_reregistration(p: Path) -> None:
    other = identity(agent_id="proposer", instance_id="p1",
                     kind=PrincipalKind.AGENT, roles={AgentRole.VERIFIER})

    def mutate(payload):
        payload["identity"] = other.to_record()
    _forge_variant(p, "agent.register", mutate)


def _memory(p: Path) -> MemoryStore:
    m = MemoryStore(EventLog(p), evidence=EvidenceStore(p.parent / "ev"))
    m.load().remember(memory_id="m1", text="first", author="agent-1")
    return m


def _forge_rewrite(payload):
    payload["entry"]["text"] = "quietly different"


# ---- policy store, scheduler, authority store -------------------------------

def _policy(p: Path) -> PolicyStore:
    pol = PolicyStore(EventLog(p)).load()
    pol.publish(default_policy(), actor="owner")
    return pol


def _forge_decision(p: Path) -> None:
    doc = PolicyStore(EventLog(p)).load().in_force("scheduler.default")
    req = PolicyRequest(action="promote", subject="mallory", role="WORKER",
                        resource="anything")
    rec = {"allowed": True, "policy_id": doc.policy_id,
           "version": doc.version, "policy_digest": doc.digest(),
           "rule_id": "a-rule-that-says-yes", "effect": "ALLOW",
           "request": req.to_record(), "reason": "I approve", "at_seq": -1}
    body = {k: v for k, v in rec.items() if k != "at_seq"}
    EventLog(p).append(actor="mallory", action=ACT_POLICY_DECISION,
                       target="anything",
                       payload={"decision": rec,
                                "decision_digest": digest(body)})


def _policy_decide(pol, i):
    return pol.decide_and_record(
        "scheduler.default",
        PolicyRequest(action="dispatch", subject="w1", role="WORKER",
                      resource=f"job-{i}"),
        actor="scheduler")


def _scheduler_on(p: Path) -> Scheduler:
    log = EventLog(p)
    return Scheduler(log, policy=PolicyStore(log).load(),
                     policy_id="scheduler.default", capacity={"slots": 2})


def _scheduler(p: Path) -> Scheduler:
    _policy(p)
    return _scheduler_on(p).load()


def _forge_finished_job(p: Path) -> None:
    EventLog(p).append(actor="mallory", action="scheduler.enqueue",
                       target="forged",
                       payload={"job": {"job_id": "forged",
                                        "work_digest": digest({"w": "x"}),
                                        "submitter": "mallory", "priority": 0,
                                        "state": "SUCCEEDED"}})


def _scheduler_decide(s, i):
    return s.enqueue(job_id=f"j-new-{i}", work_digest=digest({"w": i}),
                     submitter="owner")


def _store(p: Path) -> AuthorityStore:
    s = AuthorityStore(EventLog(p)).load()
    s.create(record_id="r0", kind="k", proposer="alice")
    return s


def _forge_unknown_action(p: Path) -> None:
    EventLog(p).append(actor="mallory", action="record.invent", target="r",
                       payload={"record_id": "r"})


# ---- idempotency ledger and proposal ingress --------------------------------

def _bindings(p: Path) -> IdempotencyLedger:
    led = IdempotencyLedger(EventLog(p)).load()
    led.bind(owner="alice", tool_id="tool", key="k0",
             request_digest=digest({"r": 0}), task_id="task-0")
    return led


def _forge_rebinding(payload):
    payload["task_id"] = "task-hostile"
    payload["request_digest"] = digest({"r": "hostile"})


def _bind_decide(led, i):
    return led.bind(owner="alice", tool_id="tool", key=f"k-new-{i}",
                    request_digest=digest({"r": 100 + i}),
                    task_id=f"task-new-{i}")


FIXTURE = ROOT / "integrations" / "proposals" / "recorded_fixture.jsonl"


def _proposal(i: int):
    text = FIXTURE.read_text(encoding="utf-8")
    response = list(PR.RecordedFixtureAdapter(text).responses({}))[0]
    hits = [{"path": "STACK.md", "line_start": i + 1, "line_end": i + 2,
             "source_sha256": digest("STACK.md"), "text": f"span {i}",
             "evidence_status": PR.EVIDENCE_STATUS}]
    ctx = PR.assemble_context("slab", retrieve=lambda q, k: {"hits": hits})
    return PR.envelope(response, context=ctx, agent_id="ai-proposer-fixture",
                       adapter=PR.RecordedFixtureAdapter(text))


def _ingress(p: Path) -> PR.ProposalIngress:
    ing = PR.ProposalIngress(EventLog(p))
    ing.receive(_proposal(0))
    return ing


def _forge_receipt(payload):
    payload["envelope_digest"] = "0" * 64


CASES = (
    Case("capability", T2._caps, _forge_backdated, _cap_decide,
         lambda p: CapabilityLedger(EventLog(p)).load(), CapabilityError),
    Case("egress", T2._egress,
         lambda p: _forge_variant(p, ACT_NET_GRANT,
                                  _widen("hosts", ["evil.example"])),
         _egress_decide, lambda p: NetworkAuthority(EventLog(p)).load(),
         NetworkError),
    Case("secrets", T2._secrets,
         lambda p: _forge_variant(p, ACT_SECRET_GRANT,
                                  _widen("purposes", ["anything"])),
         _secrets_decide, lambda p: SecretStore(EventLog(p)).load(),
         SecretError),
    Case("agents", _directory, _forge_reregistration,
         lambda d, i: d.register(identity(
             agent_id=f"a{i}", instance_id=f"a{i}", kind=PrincipalKind.AGENT,
             roles={AgentRole.PROPOSER}), by="system"),
         lambda p: AgentDirectory(EventLog(p)).load(), IdentityError),
    Case("memory", _memory,
         lambda p: _forge_variant(p, ACT_MEMORY_WRITE, _forge_rewrite),
         lambda m, i: m.remember(memory_id=f"m-new-{i}", text="next",
                                 author="agent-1"),
         lambda p: MemoryStore(EventLog(p)).load(), MemoryError_),
    Case("policy", _policy, _forge_decision, _policy_decide,
         lambda p: PolicyStore(EventLog(p)).load(), PolicyError),
    Case("scheduler", _scheduler, _forge_finished_job, _scheduler_decide,
         lambda p: _scheduler_on(p).load(), SchedulerError),
    Case("store", _store, _forge_unknown_action,
         lambda s, i: s.create(record_id=f"r-new-{i}", kind="k",
                               proposer="alice"),
         lambda p: AuthorityStore(EventLog(p)).load(), StoreError),
    Case("idempotency", _bindings,
         lambda p: _forge_variant(p, ACT_BIND, _forge_rebinding),
         _bind_decide, lambda p: IdempotencyLedger(EventLog(p)).load(),
         IdempotencyError),
    Case("proposals", _ingress,
         lambda p: _forge_variant(p, PR.ACT_PROPOSAL_RECEIVE, _forge_receipt),
         lambda ing, i: ing.receive(_proposal(10 + i)),
         lambda p: PR.received(EventLog(p)), PR.ProposalRefused,
         has_load=False),
)
IDS = [c.name for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_a_refused_fold_keeps_refusing(tmp_path, case):
    p = tmp_path / "log.jsonl"
    w = case.make(p)
    case.forge(p)
    with pytest.raises(case.refusal):
        case.reload(p)                     # premise: the history is refused
    forged_head = _head(p)
    for attempt in range(2):
        with pytest.raises(case.refusal):
            case.decide(w, attempt)
    assert _head(p) == forged_head, (
        "a decision was written onto a history this writer's own replay "
        "refuses")


@pytest.mark.parametrize("case", [c for c in CASES if c.has_load],
                         ids=[c.name for c in CASES if c.has_load])
def test_a_refused_load_leaves_nothing_to_resume(tmp_path, case):
    """load() emptied the projection, kept the old anchor and raised; the
    next catch-up advanced from that anchor over an empty projection."""
    p = tmp_path / "log.jsonl"
    w = case.make(p)
    case.forge(p)
    with pytest.raises(case.refusal):
        w.load()
    forged_head = _head(p)
    with pytest.raises(case.refusal):
        case.decide(w, 0)
    assert _head(p) == forged_head


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_control_an_unforged_history_takes_both_decisions(tmp_path, case):
    p = tmp_path / "log.jsonl"
    w = case.make(p)
    before = _head(p)
    case.decide(w, 0)
    case.decide(w, 1)
    assert _head(p) >= before + 2
    case.reload(p)


def test_a_ledger_that_refused_a_fold_answers_for_no_position(tmp_path):
    """in_force() folds only for a position past what it has read, so a
    check placed in the fold alone would still answer from a projection
    that skipped the refused record."""
    p = tmp_path / "log.jsonl"
    led = T2._caps(p)
    _forge_backdated(p)
    head = _head(p)
    for _ in range(2):
        with pytest.raises(CapabilityError, match="claims it was issued"):
            led.in_force(head)
    with pytest.raises(CapabilityError, match="claims it was issued"):
        led.in_force(0)


def test_a_load_that_succeeds_clears_a_refusal(tmp_path, monkeypatch):
    """The refusal is remembered until the projection is rebuilt from the
    whole verified log, not forever: a fold that failed for a reason that
    has gone away is recovered by load(), and only by load()."""
    p = tmp_path / "log.jsonl"
    led = T2._caps(p)
    _cap_decide(CapabilityLedger(EventLog(p)).load(), 7)   # another writer
    head = _head(p)
    real = CapabilityLedger.apply
    failed = []

    def once(self, ev):
        if ev.seq == head and not failed:
            failed.append(ev.seq)
            raise OSError("transient")
        return real(self, ev)

    monkeypatch.setattr(CapabilityLedger, "apply", once)
    with pytest.raises(OSError):
        led.in_force(head)
    with pytest.raises(OSError):
        led.in_force(head)                 # remembered, not resumed past
    led.load()
    assert "c-new-7" in led.in_force(head).issued
