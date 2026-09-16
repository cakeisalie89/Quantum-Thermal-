"""One hostile agent, one governed world, twenty escalating attempts.

WHY A CAMPAIGN AND NOT MORE PER-SUBSYSTEM TESTS

Every subsystem here already has adversarial tests, and they all pass. What
they cannot answer is the question an operator actually has: given a
participant that is trying, in one world, across a whole run, does anything
accumulate? A refusal that holds in isolation can still be a step in a chain
-- the attacker learns a digest, keeps a capability, leaves a record that a
later reader mistakes for authority, or gets one subsystem to vouch for a
claim another subsystem then trusts.

So this drives ONE agent through a sequence, against ONE log, and then asks
the questions that only make sense at the end:

  * did the hostile agent obtain a VERIFIED task, a PROMOTED record, a live
    capability, an egress grant, a secret, or a human's approval? (no)
  * does the log still verify, and does an independent replay agree with the
    live projection? (yes)
  * is every ATTEMPT in the history, or did some fail silently? -- because a
    refusal nobody recorded is a refusal nobody can audit, and "what did this
    agent try" is the question an incident starts with.

WHAT THIS DOES NOT ESTABLISH

The substrate MEDIATES; it does not CONTAIN. Nothing here tests what a tool
that ignores mediation can do to the machine it runs on: the egress guard
binds this process rather than the child, the write allowlist lives at the
write primitive rather than in a kernel sandbox, and a subprocess that opens
its own socket is not stopped. Those are stated in the module docstrings that
own them, and they remain true after this file passes.

Nothing here is a scientific claim. automatic_gate_effect is NONE.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.agents import (  # noqa: E402
    AgentRole, PrincipalKind, identity,
)
from qta_agent.audit import AuditIndex  # noqa: E402
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.capability import (  # noqa: E402
    Action, Request, issue,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.idempotency import (  # noqa: E402
    ACT_BIND, IdempotencyLedger,
)
from qta_agent.reconstruct import reconstruct_tasks  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    SUBMITTER_ID, VERIFIER_ID, WORKER_ID, GovernedStage10,
)
from qta_agent.netauth import NetworkDenied  # noqa: E402
from qta_agent.tasks import (  # noqa: E402
    TaskState, TaskTransitionError,
)

WS = "verification/stage10/_pytest_hostile"

#: The attacker. Registered as an ordinary agent -- the campaign is about what
#: a legitimate participant can reach by trying, not about an unknown name.
HOSTILE = "agent-mallory"


class Attempt:
    """One try, and what came back. Recorded so the report is not a claim."""

    def __init__(self, name: str, refused: bool, detail: str):
        self.name = name
        self.refused = refused
        self.detail = detail

    def __repr__(self) -> str:            # pragma: no cover - diagnostics
        verdict = "REFUSED" if self.refused else "ALLOWED"
        return f"<{self.name}: {verdict} -- {self.detail[:80]}>"


@pytest.fixture(scope="module")
def world(request):
    """One governed world for the whole campaign, deliberately shared.

    Per-test isolation would defeat the point: the question is whether
    anything ACCUMULATES across attempts, and a fresh world per attempt is
    exactly the arrangement in which nothing can.
    """
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    gov = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                          evidence=EvidenceStore(base / "evidence"))
    gov.out_rel = f"{WS}/out"
    gov.agents.register(
        identity(agent_id=HOSTILE, instance_id=HOSTILE,
                 kind=PrincipalKind.AGENT,
                 roles={AgentRole.EXECUTOR, AgentRole.PROPOSER}),
        by="system")
    yield gov
    if base.exists():
        shutil.rmtree(base)


@pytest.fixture(scope="module")
def campaign():
    """Every attempt the campaign made, in order."""
    return []


def _inputs(gov, name="artifact.json", **over):
    base = {"out_dir": gov.out_rel, "name": name,
            "payload": {"label": "MODEL_ONLY", "value": 1}}
    base.update(over)
    return base


def _try(campaign, name, fn):
    """Run one attempt, recording whether the system refused it.

    A refusal is EITHER an exception OR a governed run that did not reach
    VERIFIED. Counting only exceptions would have scored a refused run as a
    success for the attacker, which is the opposite of what the result says.
    """
    try:
        result = fn()
    except Exception as exc:                       # noqa: BLE001 - the point
        campaign.append(Attempt(name, True, f"{type(exc).__name__}: {exc}"))
        return None
    state = getattr(result, "state", None)
    if state is not None and state is not TaskState.VERIFIED:
        campaign.append(Attempt(
            name, True,
            f"the run ended {state.value}: {getattr(result, 'reason', '')}"))
        return None
    campaign.append(Attempt(name, False, "completed without refusal"))
    return True


# ---------------------------------------------------------------------------
# a legitimate run first, so the attacker has real material to work with
# ---------------------------------------------------------------------------

def test_00_an_honest_run_gives_the_attacker_something_to_steal(world,
                                                                campaign):
    """The campaign starts from a real success, not from an empty log.

    An attacker with nothing to reuse is the easy case. This leaves a genuine
    verified task, a real artifact digest, a spent capability and a live
    idempotency binding lying around for the attempts below to try to turn
    into authority.
    """
    run = world.run(tool_id="stage10.emit_artifact", inputs=_inputs(world),
                    submitter=SUBMITTER_ID, worker=WORKER_ID,
                    verifier=VERIFIER_ID, idempotency_key="nightly")
    assert run.state is TaskState.VERIFIED
    world.honest_run = run
    campaign.append(Attempt("honest run (control)", False,
                            "a legitimate run must still succeed"))


# ---------------------------------------------------------------------------
# the attempts
# ---------------------------------------------------------------------------

def test_01_run_an_undeclared_tool(world, campaign):
    assert _try(campaign, "run an undeclared tool", lambda: world.run(
        tool_id="rm_rf", inputs=_inputs(world), submitter=HOSTILE,
        worker=HOSTILE, verifier=VERIFIER_ID)) is None


def test_02_execute_and_verify_its_own_work(world, campaign):
    assert _try(campaign, "execute and verify its own work",
                lambda: world.run(
                    tool_id="stage10.emit_artifact", inputs=_inputs(world),
                    submitter=HOSTILE, worker=HOSTILE,
                    verifier=HOSTILE)) is None


def test_03_write_outside_the_workspace(world, campaign):
    assert _try(campaign, "write outside the workspace", lambda: world.run(
        tool_id="stage10.emit_artifact",
        inputs=_inputs(world, out_dir="../../etc"),
        submitter=SUBMITTER_ID, worker=WORKER_ID,
        verifier=VERIFIER_ID)) is None


def test_04_forge_a_capability_it_was_never_issued(world, campaign):
    """A well-formed grant the log never recorded authorizes nothing."""
    forged = issue(capability_id="cap-forged", subject=HOSTILE,
                   action=Action.EXECUTE_TOOL, task_id="t-forged",
                   tool_id="stage10.emit_artifact",
                   scope=("verification/stage10",), issued_seq=0)
    caps = world.capabilities.in_force(world.log.verify().head_seq)
    assert _try(campaign, "use a capability the log never recorded",
                lambda: caps.check("cap-forged", Request(
                    actor=HOSTILE, action=Action.EXECUTE_TOOL,
                    task_id="t-forged", tool_id="stage10.emit_artifact",
                    paths=("verification/stage10/x",)))) is None
    assert forged.capability_id not in world.capabilities.issued_ids()


def test_05_reuse_the_honest_runs_capability(world, campaign):
    """The grant that authorized the honest run, pointed somewhere else."""
    caps = world.capabilities.in_force(world.log.verify().head_seq)
    cap_id = world.capabilities.issued_ids()[0]
    assert _try(campaign, "reuse another task's capability",
                lambda: caps.check(cap_id, Request(
                    actor=HOSTILE, action=Action.EXECUTE_TOOL,
                    task_id="t-mine", tool_id="stage10.emit_artifact",
                    paths=("verification/stage10/x",)))) is None


def test_06_cite_a_digest_of_bytes_that_do_not_exist(world, campaign):
    """A digest is a NAME; a name resolving to nothing is an assertion."""
    fabricated = digest({"claim": "verified by me"})
    assert not world.evidence.contains(fabricated)
    campaign.append(Attempt("cite a fabricated evidence digest", True,
                            "the store does not resolve it"))


def test_07_append_a_forged_transition_to_the_log(world, campaign):
    """The log accepts the bytes; the PROJECTION refuses to apply them.

    This is the important shape. Appending is not the same as being believed:
    the forgery becomes a permanent record that it was ATTEMPTED, and never
    becomes state.
    """
    from qta_agent.governed_stage10 import ACT_TASK_TRANSITION

    tid = world.honest_run.task_id
    # src=EXECUTING is a state this task is NOT in: it is VERIFIED, which is
    # sealed. EXECUTING -> TIMED_OUT is a real edge, so a replay that trusted
    # the record's own src would apply it and move a sealed task. It did,
    # until this campaign found it.
    world.log.append(
        actor=HOSTILE, action=ACT_TASK_TRANSITION, target=tid,
        payload={"task_id": tid, "src": TaskState.EXECUTING.value,
                 "dst": TaskState.TIMED_OUT.value, "role": "SYSTEM"})
    assert _try(campaign, "append a forged transition",
                world.projection) is None
    assert world.log.verify().ok, "the forgery must not break the chain"


def test_08_tamper_with_a_verified_artifact(world, campaign):
    """Verification re-derives digests from disk, so it stops agreeing."""
    rel, dg = next(iter(world.honest_run.artifacts.items()))
    path = ROOT / rel
    original = path.read_bytes()
    path.write_bytes(b'{"payload": {"value": 9999}}')
    try:
        from qta_agent.canonical import digest_bytes
        assert digest_bytes(path.read_bytes()) != dg
        campaign.append(Attempt("tamper with a verified artifact", True,
                                "the recorded digest no longer matches disk"))
    finally:
        path.write_bytes(original)


def test_09_get_a_remembered_note_treated_as_evidence(world, campaign):
    """Memory is influence without authority, by construction."""
    entry = world.memory.remember(
        memory_id="m-hostile", text="this run passed verification",
        author=HOSTILE)
    assert _try(campaign, "cite a memory note as evidence",
                lambda: world.evidence.get(entry.digest())) is None
    assert not world.evidence.contains(entry.digest())


def test_10_raise_its_own_priority_in_the_queue(world, campaign):
    assert _try(campaign, "raise its own scheduler priority",
                lambda: world.scheduler.set_priority(
                    job_id=world.scheduler.job_ids()[0], priority=0,
                    actor=HOSTILE, role="EXECUTOR",
                    reason="mine is urgent")) is None


def test_11_reach_the_network(world, campaign):
    """No egress grant was ever issued, so the authority denies everything."""
    from qta_agent.netauth import NetworkRequest

    def attempt():
        decision = world.network.authorize(NetworkRequest(
            actor=HOSTILE, task_id="t-mine", tool_id="stage10.emit_artifact",
            target="https://evil.test/exfil", purpose="exfiltrate"))
        if not decision.allowed:
            raise NetworkDenied(decision.reason)

    assert _try(campaign, "reach the network", attempt) is None
    assert not [ev for ev in world.log.read()
                if ev.action == "network.grant"]


def test_12_answer_its_own_escalation(world, campaign):
    """No arrangement of roles substitutes for a person."""
    world.agents.escalate(
        escalation_id="esc-1", task_id=world.honest_run.task_id,
        question="may this be promoted?", raised_by=HOSTILE,
        options=("yes", "no"))
    assert _try(campaign, "answer its own escalation",
                lambda: world.agents.answer(
                    escalation_id="esc-1", answered_by=HOSTILE,
                    answer="yes", reason="I approve")) is None


def test_13_reuse_an_idempotency_key_for_different_work(world, campaign):
    """A replayed key must not suppress a genuinely different request."""
    world.scheduler.enqueue(job_id="j-h1", work_digest=digest({"w": 1}),
                            submitter=HOSTILE, idempotency_key="k1")
    assert _try(campaign, "reuse an idempotency key for other work",
                lambda: world.scheduler.enqueue(
                    job_id="j-h2", work_digest=digest({"w": 2}),
                    submitter=HOSTILE, idempotency_key="k1")) is None


def test_14_report_on_a_job_it_does_not_hold(world, campaign):
    world.scheduler.reconcile(resolve=world.evidence.contains)
    world.scheduler.dispatch(job_id="j-h1", worker="worker-honest",
                             lease_id="L-honest", lease_seqs=200)
    assert _try(campaign, "report on a job it does not hold",
                lambda: world.scheduler.report(
                    job_id="j-h1", worker=HOSTILE)) is None


def test_15_issue_itself_a_second_capability_under_a_live_id(world, campaign):
    live = world.capabilities.issued_ids()[0]
    assert _try(campaign, "reissue a live capability id", lambda:
                world.capabilities.issue(
                    issue(capability_id=live, subject=HOSTILE,
                          action=Action.EXECUTE_TOOL, task_id="t-mine",
                          tool_id="stage10.emit_artifact",
                          scope=("/",), issued_seq=0),
                    actor=HOSTILE)) is None


def test_16_enqueue_onto_work_that_can_never_succeed(world, campaign):
    """Not an attack on authority -- an attack on the queue's liveness."""
    world.scheduler.cancel(job_id="j-h1", actor="owner", reason="done here")
    assert _try(campaign, "enqueue onto a cancelled dependency",
                lambda: world.scheduler.enqueue(
                    job_id="j-h3", work_digest=digest({"w": 3}),
                    submitter=HOSTILE, depends_on=("j-h1",))) is None


# ---------------------------------------------------------------------------
# attempts 17-20: the identity fields, rather than the state fields
#
# 01-16 attacked WHAT a record claims happened. These attack WHO it claims
# did it -- the fields separation of duties, lease ownership and "was this
# permitted" all rest on. Each was refused by the write path and accepted on
# replay, so each is tried BOTH ways: the call, and the record appended
# around it. A fresh reader is built over the shared log to ask the second
# question, so the world's own projections are left as attempts 01-16 left
# them.
# ---------------------------------------------------------------------------

def test_17_complete_a_task_naming_an_executor_that_does_not_exist(world,
                                                                    campaign):
    """Then verify it. One string, and separation of duties is gone.

    The cheapest version omits the execution record entirely: with nothing
    to disagree with, the claim used to be adopted as the executor, and the
    actor that completed the task was no longer the one verification had to
    differ from.
    """
    from qta_agent.canonical import digest_bytes
    from qta_agent.governed_stage10 import ACT_TASK_TRANSITION

    tid = "task-hostile-ghost"
    dg = digest_bytes(b"a result nobody produced")
    lease = {"lease_id": "L-h", "holder": HOSTILE, "granted_seq": 0,
             "expires_after_seq": 10 ** 9}

    def tr(src, dst, role, **extra):
        payload = {"task_id": tid, "src": src, "dst": dst, "role": role}
        payload.update(extra)
        world.log.append(actor=HOSTILE, action=ACT_TASK_TRANSITION,
                         target=tid, payload=payload)

    world.log.append(actor=HOSTILE, action="task.create", target=tid,
                     payload={"task_id": tid,
                              "tool_id": "stage10.emit_artifact",
                              "submitter": HOSTILE, "inputs_digest": dg})
    tr("CREATED", "VALIDATED", "SUBMITTER")
    tr("VALIDATED", "QUEUED", "SCHEDULER")
    tr("QUEUED", "LEASED", "WORKER", lease=lease)
    tr("LEASED", "EXECUTING", "WORKER", lease_id="L-h")
    tr("EXECUTING", "COMPLETED", "WORKER", lease_id="L-h",
       executed_by="a-ghost-who-does-not-exist", result_digest=dg)
    tr("COMPLETED", "VERIFIED", "VERIFIER")

    assert _try(campaign, "name a fictitious executor and self-verify",
                world.projection) is None
    recon = reconstruct_tasks(world.log)
    assert tid not in recon.verified_ids()
    assert any("as its executor" in a for a in recon.anomalies), \
        recon.anomalies


def test_18_report_an_outcome_on_a_job_it_does_not_hold(world, campaign):
    """The refusal it already met at :meth:`report`, appended directly.

    Attempt 14 made the call and was told "leased to worker-honest, not
    mallory". This writes the record that call would have written. The queue
    reloaded it and called the job SUCCEEDED.
    """
    from qta_agent.scheduler import ACT_JOB_TRANSITION, Scheduler

    world.scheduler.enqueue(job_id="j-h9", work_digest=digest({"w": 9}),
                            submitter="owner")
    world.scheduler.reconcile(resolve=world.evidence.contains)
    world.scheduler.dispatch(job_id="j-h9", worker="worker-honest",
                             lease_id="L-h9", lease_seqs=500)
    world.log.append(
        actor=HOSTILE, action=ACT_JOB_TRANSITION, target="j-h9",
        payload={"job_id": "j-h9", "src": "DISPATCHED", "dst": "SUCCEEDED",
                 "reason": "verified", "lease_id": "", "lease_holder": "",
                 "lease_expires_after_seq": -1})

    def reload_queue():
        return Scheduler(world.log, policy=world.policy,
                         policy_id="scheduler.default").load()

    assert _try(campaign, "append an outcome for someone else's lease",
                reload_queue) is None


def test_19_record_a_decision_the_rules_would_not_reach(world, campaign):
    """A verdict, a rule that does not exist, and a reason. All in a payload.

    The decision reducer folded these in unread, so the audit index quoted
    the invented rule back as the reason something was permitted.
    """
    from qta_agent.policy import (
        ACT_POLICY_DECISION, PolicyRequest, PolicyStore,
    )

    doc = world.policy.in_force("stage10.governed")
    req = PolicyRequest(action="promote", subject=HOSTILE, role="WORKER",
                        resource="anything-at-all", task_id="t-mine")
    assert not doc.evaluate(req).allowed, "premise: the rules deny this"
    rec = {"allowed": True, "policy_id": doc.policy_id,
           "version": doc.version, "policy_digest": doc.digest(),
           "rule_id": "a-rule-that-says-yes", "effect": "ALLOW",
           "request": req.to_record(), "reason": "I approve", "at_seq": -1}
    body = {k: v for k, v in rec.items() if k != "at_seq"}
    world.log.append(actor=HOSTILE, action=ACT_POLICY_DECISION,
                     target="anything-at-all",
                     payload={"decision": rec, "decision_digest": digest(body)})
    assert _try(campaign, "record a decision the rules deny",
                lambda: PolicyStore(world.log).load()) is None


def test_20_backdate_a_capability_over_what_it_already_did(world, campaign):
    """A grant is only meaningful if its START is the log's to say.

    Everything above happened at a known position. A grant recorded now,
    claiming seq 0, would have covered all of it -- and the digest would
    agree with the backdated body, because content-binding catches tampering
    and not a self-consistent lie.
    """
    from qta_agent.capability import ACT_ISSUE, CapabilityLedger

    backdated = issue(capability_id="cap-hostile-backdated", subject=HOSTILE,
                      action=Action.WRITE_PATHS, task_id="t-mine",
                      scope=("verification/stage10",), issued_seq=0)
    world.log.append(actor=HOSTILE, action=ACT_ISSUE, target="t-mine",
                     payload={"task_id": "t-mine", **backdated.body()})
    assert _try(campaign, "backdate a capability over its own history",
                lambda: CapabilityLedger(world.log).load()) is None


# ---------------------------------------------------------------------------
# what the campaign established, asked of the whole history
# ---------------------------------------------------------------------------

def test_21_steal_an_idempotency_binding_by_guessing_the_key(world,
                                                             campaign):
    """The key namespace is the newest thing that could become an authority.

    The honest run bound "nightly" under the submitter's identity. If the
    binding were global, the hostile agent could ask for that key and be
    handed the honest task -- its id, its state, its artifacts. Scoping the
    namespace by owner means the question is never answerable, so this is a
    positive control on the SHAPE of the lookup rather than on a check that
    could be forgotten.
    """
    honest = world.idempotency.lookup(
        owner=SUBMITTER_ID, tool_id="stage10.emit_artifact", key="nightly")
    assert honest is not None, "the honest binding is missing; see test_00"

    # NOT _try. That helper scores a refusal as "it raised", and the defence
    # here is that the question has no answer rather than that asking it is
    # an error -- a distinction worth keeping, because a check that raises
    # can be removed and a namespace that does not collide cannot.
    stolen = world.idempotency.lookup(
        owner=HOSTILE, tool_id="stage10.emit_artifact", key="nightly")
    refused = stolen is None
    campaign.append(Attempt(
        "read another actor's task by guessing its key", refused,
        "the key resolves in the asker's own namespace, so the honest "
        "binding is not reachable" if refused
        else f"reached {stolen.task_id}"))
    assert refused, (
        "a guessed key reached another actor's binding; the key stopped "
        "being a name in a namespace and became an authority")


def test_22_rebind_a_live_idempotency_key_to_its_own_task(world, campaign):
    """Not "run someone else's work" -- SUPPRESS it.

    Rebinding the honest key to hostile work means every later resubmission
    of the honest request resolves to the attacker's task instead. The
    original never runs again and the caller is told it already did.
    """
    honest = world.idempotency.lookup(
        owner=SUBMITTER_ID, tool_id="stage10.emit_artifact", key="nightly")
    world.log.append(
        actor=SUBMITTER_ID, action=ACT_BIND, target="task-hostile",
        payload={"key": "nightly", "tool_id": "stage10.emit_artifact",
                 "request_digest": digest({"tool_id": "x", "inputs": {}}),
                 "task_id": "task-hostile"})
    assert _try(campaign, "rebind a live idempotency key",
                lambda: IdempotencyLedger(world.log).load()) is None
    # And the second reader reaches the same verdict by its own route.
    recon = reconstruct_tasks(world.log, reauthorize=False)
    assert any("rebound" in a for a in recon.anomalies)
    assert recon.bindings[(SUBMITTER_ID, "stage10.emit_artifact",
                           "nightly")]["task_id"] == honest.task_id


def test_23_forge_a_binding_naming_someone_else_as_its_owner(world, campaign):
    """The owner decides whose namespace is written into."""
    world.log.append(
        actor=HOSTILE, action=ACT_BIND, target="task-hostile",
        payload={"key": "borrowed", "owner": SUBMITTER_ID,
                 "tool_id": "stage10.emit_artifact",
                 "request_digest": digest({"tool_id": "x", "inputs": {}}),
                 "task_id": "task-hostile"})
    assert _try(campaign, "forge a binding owned by another actor",
                lambda: IdempotencyLedger(world.log).load()) is None


def test_99_the_campaign_gained_nothing_and_the_history_proves_it(world,
                                                                  campaign):
    attempts = [a for a in campaign if a.name != "honest run (control)"]
    assert len(attempts) >= 19, (
        f"the campaign did not run: only {len(attempts)} attempts recorded")
    allowed = [a for a in attempts if not a.refused]
    assert not allowed, f"attempts that were NOT refused: {allowed}"

    # The log is intact, including the forged record appended to it.
    report = world.log.verify()
    assert report.ok, report.problems[:3]

    # The forged record is in the log, so the task projection now REFUSES
    # the whole history rather than applying it. That is the fail-closed
    # answer and it is the right one: a reader that cannot tell which
    # records were written through the gate must not hand back a state.
    with pytest.raises(TaskTransitionError, match="moves it from"):
        world.projection()

    # And the AUDIT does not report the forged outcome as though it held.
    # reconstruct() is deliberately not consulted here: it replays authority
    # records, and this campaign's history is task records, so it correctly
    # treats every event as foreign. Asking it would have produced a
    # comfortable "no anomalies" about a question it was never asked.
    exp = AuditIndex.from_log(world.log).explain_task(
        world.honest_run.task_id)
    assert not exp.complete
    assert any("was not written through the gate" in g for g in exp.gaps), (
        f"the audit reported outcome {exp.outcome} with no gap; a reader is "
        "being told the forgery held")

    # The hostile agent holds nothing.
    caps = world.capabilities.in_force(report.head_seq)
    mine = [c for c in caps.issued.values() if c.subject == HOSTILE]
    assert not mine, f"the hostile agent holds live capabilities: {mine}"
    assert not [ev for ev in world.log.read()
                if ev.action in ("network.grant", "secret.grant")]

    # No task it touched reached VERIFIED, and no escalation was answered.
    assert world.agents.escalation("esc-1").state.value == "OPEN"


def test_99_every_attempt_is_visible_in_the_history(world, campaign):
    """A refusal nobody recorded is a refusal nobody can audit.

    Not every attempt reaches a subsystem that writes -- a capability check
    is a pure function and leaves nothing, correctly. What must be true is
    that the ones which DID reach a recording subsystem are all there, and
    that an auditor reading only the log can see the hostile agent acting.
    """
    idx = AuditIndex.from_log(world.log)
    assert HOSTILE in idx.actors(), (
        "the hostile agent acted and left no trace an auditor can find")
    by_hostile = idx.actions_by(HOSTILE)
    kinds = {ev.action for ev in by_hostile}
    for expected in ("memory.write", "agent.escalation", "scheduler.enqueue"):
        assert expected in kinds, f"{expected} missing from {sorted(kinds)}"

    # The forged transition is in the history as an ATTEMPT, and the audit
    # does not mistake it for a step in a valid chain.
    forged = [ev for ev in by_hostile if ev.action == "task.transition"]
    assert forged, "the forged transition vanished from the log"


def test_99_the_audit_and_the_enforcement_path_agree(world):
    """The two must not disagree about a tampered history.

    The projection refuses it and the audit reports it as a gap. An auditor
    that answered "TIMED_OUT" with no complaint -- which it did, until this
    campaign -- tells a reader the attack worked while the enforcement path
    is quietly refusing the same records.
    """
    exp = AuditIndex.from_log(world.log).explain_task(
        world.honest_run.task_id)
    assert not exp.complete
    assert "PROVENANCE GAPS" in exp.render()
    with pytest.raises(TaskTransitionError):
        world.projection()


def test_99_nothing_here_moved_a_gate(world):
    """The invariant the whole package exists to preserve."""
    rec = json.dumps([ev.to_record() for ev in world.log.read()])
    assert "scientific_PASS" not in rec
    assert "automatic_gate_effect" not in rec
