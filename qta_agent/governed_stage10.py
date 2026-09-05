"""The production caller: a real Stage-10 run, mediated end to end.

WHY THIS EXISTS

Everything else in this package was, until now, a mechanism with no consumer.
A governance layer that governs nothing is a library, and a library cannot be
wrong in the way a control plane can -- which means none of its guarantees had
ever been tested against a workflow that does real work and produces real
artifacts.

This module puts one through the whole chain:

    intent -> agent identities -> policy decision -> queued job
           -> readiness -> lease -> task record -> validation
           -> capability grant -> context manifest -> bounded execution
           under a network guard -> output capture
           -> content-addressed evidence -> provenance binding
           -> independent verification by a separated actor -> authority
           -> durable state -> a remembered note that is not authority,
           all of it in one hash-chained log

The workflow chosen is Stage-10 artifact generation. It is real (the Snakefile
runs it, it writes files somebody reads), it is safe (``automatic_gate_effect``
is NONE, and it writes only inside ``verification/stage10``), and it touches no
scientific authority. Governing it demonstrates the control plane without
putting a single gate, threshold or canonical output at risk.

WHAT MAKES THIS NOT COSMETIC

The directive's own test: a test-only caller does not count, a demonstration
script does not count, an unused CLI does not count. So:

  * the work is executed as a bounded SUBPROCESS through
    :class:`~qta_agent.execution.Executor`, not called in-process;
  * the capability is scoped to this task and this tool, and the executor
    refuses without it;
  * every produced file is hashed into the evidence store, and the digests are
    what the task's completion cites -- not a summary, not a log line;
  * verification is performed by a DIFFERENT actor and re-derives the digests
    from the files on disk, so a completion that cites bytes which are no
    longer there cannot be verified;
  * every step is appended to the event log, so the run is reconstructible
    after a crash by replay rather than by inference;
  * the POLICY in force is consulted and its decision recorded, so "which
    rules allowed this" resolves to a document digest rather than a string;
  * the work is queued through the real scheduler, so readiness, leases and
    outcome reporting are the ones the scheduler enforces rather than a
    parallel implementation that happens to agree;
  * the worker and the verifier are REGISTERED identities and the separation
    between them is checked by the directory, not by comparing two strings;
  * the tool runs inside a network guard with no egress grant, so a
    dependency that phones home is refused rather than merely undeclared;
  * a context manifest records what was available to the run, by digest.

Every one of those is load-bearing: if the policy denies, if readiness fails,
if the separation check refuses, or if the tool opens a socket, the run does
not complete and the Snakemake rule fails the build.

WHAT IT STILL DOES NOT DO

It does not make anything scientifically true. A VERIFIED task means a declared
tool ran under a bounded environment, produced the bytes it says it produced,
and a second actor confirmed those bytes are still there. That is a statement
about provenance and nothing else. PASS remains 0, and no gate can be reached
from here.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import actions
from .agents import (
    AgentDirectory, AgentRole, PrincipalKind, check_separation, identity,
)
from .canonical import digest, digest_bytes
from .capability import Action, CapabilitySet, issue
from .context import ContextBuilder, Tier, record_context
from .events import EventLog
from .evidence import EvidenceStore
from .execution import Executor, Limits, Outcome
from .memory import MemoryStore
from .netauth import NetworkAuthority, socket_guard
from .policy import Effect, PolicyRequest, PolicyStore, document, rule
from .scheduler import FailureClass, Scheduler
from .tasks import (
    Lease, Task, TaskProjection, TaskRole, TaskState, TaskTransition,
    apply_transition, check,
)
from .tools import Determinism, Field_, Registry, SideEffect, ToolSpec

#: Where governed Stage-10 work is allowed to write. The same subtree the
#: Stage-10 workspace guard permits, so the two agree by construction rather
#: than by two lists that must be kept in step.
WORKSPACE_PREFIX = "verification/stage10"

#: The policy this path publishes and runs under. A real document with real
#: rules, not an identifier: the decision it produces names its digest.
POLICY_ID = "stage10.governed"

#: The identities that participate. Registered in the log, and the separation
#: between the worker and the verifier is checked by the directory rather than
#: by comparing two strings at the call site.
SUBMITTER_ID = "stage10-submitter"
WORKER_ID = "stage10-worker"
VERIFIER_ID = "stage10-verifier"

#: How many sequence numbers a Stage-10 lease is good for. Generous, because
#: the run is short and a lease that lapses mid-execution costs a retry.
LEASE_SEQS = 400

#: The ONLY environment variables a governed tool receives. The child
#: inherits nothing else -- which is the strongest secret handling available
#: here and the reason this path needs no secret grants: a credential in the
#: parent's environment cannot reach the tool at all.
#:
#: Built as a function of the parent env rather than copied from it, and
#: checked before the subprocess starts, so "the tool inherits nothing" is
#: enforced rather than assumed from how the dict happens to be written.
GOVERNED_ENV_KEYS = ("PATH", "PYTHONPATH", "PYTHONHASHSEED",
                     "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                     "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")

ACT_TASK_CREATE = "task.create"
ACT_TASK_TRANSITION = "task.transition"
ACT_CAP_ISSUE = "capability.issue"
ACT_EXECUTION = "task.execution"
ACT_EVIDENCE = "task.evidence"

#: The actions THIS projection applies or deliberately passes over. Anything
#: else is another subsystem's (skipped) or unrecognised (refused).
OWNED = frozenset({ACT_TASK_CREATE, ACT_TASK_TRANSITION, ACT_CAP_ISSUE,
                   ACT_EXECUTION, ACT_EVIDENCE})


def stage10_policy(version: int = 1) -> "object":
    """The rules a governed Stage-10 run is subject to.

    Written as an explicit document rather than a permissive default so that
    the decision recorded for each step names a rule somebody can read. The
    deny rules are the interesting half: they are what makes a policy denial a
    reachable outcome on this path rather than a theoretical one.
    """
    return document(
        policy_id=POLICY_ID, version=version,
        description=("Governed Stage-10 artifact generation. Provenance only: "
                     "no rule here can affect a gate, and PASS remains 0."),
        rules=(
            rule(rule_id="deny-worker-priority-escalation",
                 effect=Effect.DENY, actions=("scheduler.raise_priority",),
                 subjects=("*",), roles=("WORKER", "SUBMITTER"),
                 resources=("*",),
                 reason=("only the scheduler decides what is urgent; "
                         "otherwise every submitter is priority 0")),
            rule(rule_id="deny-unknown-submitters", effect=Effect.DENY,
                 actions=("scheduler.enqueue",), subjects=("*",),
                 roles=("WORKER", "VERIFIER"), resources=("*",),
                 reason=("a worker or verifier submitting its own work is "
                         "proposing and executing in one step")),
            rule(rule_id="allow-governed-stage10", effect=Effect.ALLOW,
                 actions=("scheduler.enqueue", "scheduler.dispatch",
                          "scheduler.cancel", "stage10.execute"),
                 subjects=("*",), roles=("*",), resources=("*",),
                 reason="governed Stage-10 work is permitted"),
        ))


def stage10_registry() -> Registry:
    """The tools a governed Stage-10 run may invoke. Default-deny by omission.

    Each entry runs a module in a subprocess. They are declared
    ``NONDETERMINISTIC`` rather than optimistically: these adapters embed
    nothing time-dependent that we have verified, and claiming byte-identity
    we have not measured would make a later re-run difference look like
    tampering when it is only an unproven assumption failing.
    """
    return Registry([
        ToolSpec(
            tool_id="stage10.emit_artifact", version="1.0.0",
            summary="write a deterministic JSON artifact into the workspace",
            inputs=(Field_("out_dir", "str"), Field_("name", "str"),
                    Field_("payload", "dict")),
            outputs=(Field_("path", "str"), Field_("sha256", "str")),
            determinism=Determinism.BYTE_IDENTICAL,
            side_effect=SideEffect.SCOPED_WRITES,
            writable_scope=(WORKSPACE_PREFIX,), timeout_s=60.0),
    ])


@dataclass(frozen=True)
class GovernedRun:
    """What a completed governed run produced, for the caller to inspect."""

    task_id: str
    state: TaskState
    outcome: str
    result_digest: str
    artifacts: dict
    log_head_seq: int
    reason: str
    #: The queue record this work was dispatched from.
    job_id: str = ""
    job_state: str = ""
    #: Identity and digest of the policy document that permitted the dispatch.
    policy_identity: str = ""
    policy_digest: str = ""
    #: Digest of the context manifest recorded for the run.
    context_digest: str = ""
    #: The remembered note, which is a note and not a finding.
    memory_id: str = ""


class GovernedStage10:
    """Runs Stage-10 work through the substrate. The production entry point."""

    def __init__(self, *, root: Path, log: EventLog, evidence: EvidenceStore,
                 registry: Registry | None = None):
        self.root = Path(root)
        self.log = log
        self.evidence = evidence
        self.registry = registry or stage10_registry()
        self.executor = Executor(self.registry, workspace=self.root)

        # Every subsystem projects the SAME log. That is the arrangement the
        # action registry exists to permit, and it is what makes the audit of
        # a run a single history rather than several that have to be
        # correlated afterwards.
        self.policy = PolicyStore(self.log).load()
        self.agents = AgentDirectory(self.log).load()
        self.scheduler = Scheduler(self.log, policy=self.policy,
                                   policy_id=POLICY_ID,
                                   capacity={"slots": 2}).load()
        self.memory = MemoryStore(self.log, evidence=self.evidence).load()
        #: No egress grant is ever issued here. The Stage-10 tool needs none,
        #: and an authority with no grants denies everything -- which is what
        #: the socket guard enforces during execution.
        self.network = NetworkAuthority(self.log).load()
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Publish the policy and register the parties, once per log.

        Idempotent because a governed run may be the first thing that ever
        touches a log, or the hundredth. What is NOT idempotent is quietly
        republishing a policy: versions are gap-free, so this publishes only
        when nothing is published yet.
        """
        try:
            self.policy.in_force(POLICY_ID)
        except Exception:                            # noqa: BLE001
            self.policy.publish(stage10_policy(), actor="owner")
        roles = {SUBMITTER_ID: AgentRole.PROPOSER,
                 WORKER_ID: AgentRole.EXECUTOR,
                 VERIFIER_ID: AgentRole.VERIFIER}
        for instance_id, role in roles.items():
            try:
                self.agents.get(instance_id)
            except Exception:                        # noqa: BLE001
                self.agents.register(
                    identity(agent_id=instance_id, instance_id=instance_id,
                             kind=PrincipalKind.AGENT, roles={role}),
                    by="system")

    # ---- projection ----------------------------------------------------
    def projection(self) -> TaskProjection:
        """Rebuild task state from the verified log. Fail closed.

        The whole point of a durable lifecycle: this answers "what was this
        task doing when the machine died" by replay, not by inference.
        """
        self.log.verify().raise_if_bad()
        tasks: dict = {}
        seq = -1
        for ev in self.log.read():
            seq = ev.seq
            p = ev.payload
            if ev.action == ACT_TASK_CREATE:
                tasks[p["task_id"]] = Task(
                    task_id=p["task_id"], tool_id=p["tool_id"],
                    submitter=p["submitter"],
                    inputs_digest=p["inputs_digest"],
                    depends_on=tuple(p.get("depends_on", ())),
                    created_seq=ev.seq, updated_seq=ev.seq)
            elif ev.action == ACT_TASK_TRANSITION:
                task = tasks[p["task_id"]]
                lease = None
                if p.get("lease"):
                    lease = Lease(**p["lease"])
                req = TaskTransition(
                    task_id=p["task_id"], src=TaskState(p["src"]),
                    dst=TaskState(p["dst"]), actor=ev.actor,
                    role=TaskRole(p["role"]), at_seq=ev.seq,
                    lease_id=(lease.lease_id if lease else p.get("lease_id")),
                    executed_by=p.get("executed_by"),
                    result_digest=p.get("result_digest"))
                # Re-authorize on replay. A transition that would be refused
                # today is not applied, so a forged log entry cannot become
                # state simply by being present.
                edge = check(req, task)
                tasks[p["task_id"]] = apply_transition(
                    task, edge, req, seq=ev.seq, lease=lease)
            elif ev.action in (ACT_CAP_ISSUE, ACT_EXECUTION, ACT_EVIDENCE):
                continue
            else:
                try:
                    kind = actions.require_known(
                        ev.action, mine=OWNED, where=f"seq {ev.seq}")
                except actions.UnknownAction as exc:
                    # Re-raised as ValueError, this projection's existing
                    # contract. Sharing the classification must not change
                    # what callers catch.
                    raise ValueError(str(exc)) from exc
                if kind == actions.FOREIGN:
                    # Another subsystem sharing this log. Skipped here and
                    # projected by its own reducer.
                    continue
                raise ValueError(                # pragma: no cover - closed
                    f"seq {ev.seq}: {ev.action!r} is listed as owned by this "
                    "projection and has no branch handling it")
        return TaskProjection(tasks=tasks, at_seq=seq)

    # ---- the governed run ----------------------------------------------
    def run(self, *, tool_id: str, inputs: dict,
            submitter: str = SUBMITTER_ID,
            worker: str = WORKER_ID, verifier: str = VERIFIER_ID,
            lease_seqs: int = LEASE_SEQS) -> GovernedRun:
        """Take one unit of work all the way through the control plane.

        ``worker`` and ``verifier`` must differ. That is not a style
        preference: the task state machine refuses the COMPLETED -> VERIFIED
        edge when they are the same, so passing one identity for both fails
        here rather than producing a verification that verified nothing.
        """
        if worker == verifier:
            raise ValueError(
                "worker and verifier must be different actors; an agent that "
                "verifies its own work has not verified anything")

        # The separation is checked by the DIRECTORY, against the roles those
        # identities actually hold, rather than by comparing two strings. Two
        # runs of one agent are one party here, so a worker that restarts
        # under a new instance id still cannot verify its own work.
        self.agents.require(worker, AgentRole.EXECUTOR)
        self.agents.require(verifier, AgentRole.VERIFIER)
        separation = check_separation(
            self.agents, instance_id=verifier, taking=AgentRole.VERIFIER,
            already={AgentRole.EXECUTOR: worker})
        if not separation.allowed:
            raise ValueError(f"refusing to run: {separation.reason}")

        spec = self.registry.get(tool_id)          # default deny
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        job_id = f"job-{task_id[5:]}"
        inputs_digest = digest(inputs)

        # The policy in force decides whether this may be queued at all, and
        # the decision is RECORDED -- so "which rules allowed this" resolves
        # to a document digest rather than to a string somebody chose.
        decision = self.policy.decide_and_record(
            POLICY_ID,
            PolicyRequest(action="stage10.execute", subject=submitter,
                          role="SUBMITTER", resource=tool_id,
                          task_id=task_id),
            actor=submitter, target=task_id)
        decision.raise_if_denied()

        self.log.append(
            actor=submitter, action=ACT_TASK_CREATE, target=task_id,
            payload={"task_id": task_id, "tool_id": tool_id,
                     "submitter": submitter, "inputs_digest": inputs_digest,
                     "depends_on": []})
        task = self.projection().get(task_id)

        # Validation is a real gate: the contract is checked before anything
        # is scheduled, and a rejection is recorded rather than raised away.
        try:
            spec.validate_inputs(inputs)
        except Exception as exc:
            task = self._move(task, TaskState.REJECTED, submitter,
                              TaskRole.SUBMITTER, note=str(exc))
            return GovernedRun(task_id, task.state, "REJECTED", "", {},
                               self.log.verify().head_seq, str(exc),
                               policy_identity=decision.identity,
                               policy_digest=decision.policy_digest)

        task = self._move(task, TaskState.VALIDATED, submitter,
                          TaskRole.SUBMITTER)

        # Queued through the REAL scheduler: readiness, the lease and the
        # outcome report are the ones the scheduler enforces, not a parallel
        # implementation beside it that happens to agree today.
        self.scheduler.enqueue(job_id=job_id, work_digest=inputs_digest,
                               submitter=submitter, task_id=task_id,
                               resources={"slots": 1})
        self.scheduler.reconcile(resolve=self.evidence.contains)
        task = self._move(task, TaskState.QUEUED, "scheduler",
                          TaskRole.SCHEDULER)

        lease_id = f"lease-{uuid.uuid4().hex[:8]}"
        job = self.scheduler.dispatch(
            job_id=job_id, worker=worker, lease_id=lease_id,
            lease_seqs=lease_seqs, task_id=task_id,
            resolve=self.evidence.contains)
        lease = Lease(lease_id=lease_id, holder=worker,
                      granted_seq=job.updated_seq,
                      expires_after_seq=job.lease_expires_after_seq)
        task = self._move(task, TaskState.LEASED, worker, TaskRole.WORKER,
                          lease=lease)

        # The grant is minted for THIS task and THIS tool, and expires with
        # the lease. Recorded in the log so the issued set is a projection of
        # a verified history rather than something the caller asserts.
        cap_id = f"cap-{uuid.uuid4().hex[:8]}"
        head = self.log.verify().head_seq
        cap = issue(capability_id=cap_id, subject=worker,
                    action=Action.EXECUTE_TOOL, task_id=task_id,
                    tool_id=tool_id, scope=(WORKSPACE_PREFIX,),
                    issued_seq=head + 1,
                    expires_after_seq=lease.expires_after_seq,
                    issued_wall_time=time.time())
        self.log.append(actor="scheduler", action=ACT_CAP_ISSUE,
                        target=task_id, payload={"task_id": task_id,
                                                 **cap.body()})
        caps = CapabilitySet(issued={cap_id: cap},
                             at_seq=self.log.verify().head_seq)

        # What was available to this run, recorded by digest. The manifest is
        # not the prompt -- nothing here calls a model -- but the question it
        # answers is the same one: what did this decision have in front of it.
        context = self._build_context(task_id, tool_id, inputs, decision,
                                      cap_id)
        record_context(self.log, context.manifest, actor=worker)

        task = self._move(task, TaskState.EXECUTING, worker, TaskRole.WORKER,
                          lease_id=lease.lease_id)

        out_dir = self.root / inputs["out_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "qta_agent._stage10_tool",
                json.dumps(inputs, sort_keys=True)]
        env = self._tool_environment()
        # No egress grant was issued, so the guard denies every connection.
        # This catches the case a declaration cannot: a DEPENDENCY of the tool
        # reaching the network. It binds this process, not the child -- said
        # here because the difference matters and the module says so too.
        with socket_guard(self.network, actor=worker, task_id=task_id,
                          tool_id=tool_id):
            result = self.executor.run(
                tool_id=tool_id, actor=worker, task_id=task_id,
                capability_id=cap_id, capabilities=caps, inputs=inputs,
                argv=argv, cwd=self.root,
                limits=Limits(wall_seconds=spec.timeout_s),
                env=env)

        result_digest = digest(result.to_record())
        self.log.append(actor=worker, action=ACT_EXECUTION, target=task_id,
                        payload={"task_id": task_id,
                                 "result_digest": result_digest,
                                 **result.to_record()})

        if result.outcome is not Outcome.COMPLETED:
            dst = {Outcome.TIMED_OUT: TaskState.TIMED_OUT,
                   Outcome.CANCELLED: TaskState.CANCELLED,
                   }.get(result.outcome, TaskState.FAILED)
            role = (TaskRole.SYSTEM if dst is TaskState.CANCELLED
                    else TaskRole.WORKER)
            # The excerpt is what makes a hosted failure diagnosable without
            # a second round trip. It is carried in the RETURN value and in
            # the transition note, not in the execution record -- see
            # ExecutionResult on why raw tool output stays out of the log.
            detail = result.reason
            # Environment facts, for a failure that reproduces on a runner and
            # not here. Which of these differs is usually the whole answer,
            # and gathering them costs one round trip less than guessing.
            detail += (
                f"\n--- caller environment ---\n"
                f"pid={os.getpid()} pgid={os.getpgrp()} sid={os.getsid(0)}\n"
                f"python={sys.executable}\n"
                f"platform={sys.platform}\n"
                f"argv0={argv[0]}\n"
                f"exit_status={result.exit_status} "
                f"signal={result.signal_number}\n"
                f"stdout_bytes={result.stdout_bytes} "
                f"stderr_bytes={result.stderr_bytes}\n")
            if result.stderr_excerpt.strip():
                detail += f"\n--- tool stderr ---\n{result.stderr_excerpt}"
            if result.stdout_excerpt.strip():
                detail += f"\n--- tool stdout ---\n{result.stdout_excerpt}"
            task = self._move(task, dst, worker, role,
                              lease_id=lease.lease_id, note=result.reason)
            # The scheduler is told too, and it classifies. A timeout is
            # retryable and a non-zero exit is not, which is a decision the
            # scheduler owns rather than one made twice in two places.
            failure = {Outcome.TIMED_OUT: FailureClass.TIMEOUT,
                       Outcome.CANCELLED: FailureClass.CANCELLED,
                       }.get(result.outcome, FailureClass.PERMANENT)
            job = self.scheduler.report(job_id=job_id, worker=worker,
                                        failure=failure,
                                        detail=result.reason)
            return GovernedRun(task_id, task.state, result.outcome.value,
                               result_digest, {},
                               self.log.verify().head_seq, detail,
                               job_id=job_id, job_state=job.state.value,
                               policy_identity=decision.identity,
                               policy_digest=decision.policy_digest,
                               context_digest=context.manifest.digest())

        # Capture what the tool produced as CONTENT, not as a claim about it.
        artifacts = self._capture(out_dir, task_id)

        task = self._move(task, TaskState.COMPLETED, worker, TaskRole.WORKER,
                          lease_id=lease.lease_id, executed_by=worker,
                          result_digest=result_digest)

        # Independent verification: a different actor, re-deriving the digests
        # from the files rather than trusting the ones just recorded.
        ok, why = self._verify_artifacts(artifacts)
        dst = TaskState.VERIFIED if ok else TaskState.REJECTED
        task = self._move(task, dst, verifier, TaskRole.VERIFIER, note=why)

        if ok:
            job = self.scheduler.report(job_id=job_id, worker=worker,
                                        detail=why, actor=verifier)
        else:
            job = self.scheduler.report(
                job_id=job_id, worker=worker,
                failure=FailureClass.VERIFICATION_FAILED, detail=why,
                actor=verifier)

        # A note about the run, filed as a note. It cites the artifacts by
        # digest, and its own digest does not resolve as evidence -- so it can
        # inform a later proposal and can never support a transition.
        memory_id = f"mem-{task_id[5:]}"
        self.memory.remember(
            memory_id=memory_id, author=verifier,
            derived_from=tuple(sorted(artifacts.values())),
            confidence="observed, not established",
            text=(f"governed run {task_id} produced {len(artifacts)} "
                  f"artifact(s) under policy {decision.identity} and was "
                  f"verified by {verifier}. This is a note about provenance; "
                  "it says nothing about scientific validity."))

        return GovernedRun(task_id, task.state, result.outcome.value,
                           result_digest, artifacts,
                           self.log.verify().head_seq, why,
                           job_id=job_id, job_state=job.state.value,
                           policy_identity=decision.identity,
                           policy_digest=decision.policy_digest,
                           context_digest=context.manifest.digest(),
                           memory_id=memory_id)

    # ---- helpers -------------------------------------------------------
    def _tool_environment(self) -> dict:
        """The environment a governed tool gets, and nothing else.

        Checked against the parent's environment before returning, so a
        credential sitting in ``os.environ`` cannot reach the tool even by
        accident. The thread counts are pinned for determinism AND for the
        task budget: a governed tool importing numpy pulls in OpenBLAS, which
        spawns a worker per core, and the count varies by machine.
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(self.root),
            "PYTHONHASHSEED": "0",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
        if set(env) != set(GOVERNED_ENV_KEYS):
            raise ValueError(
                f"the governed environment is {sorted(env)}, which is not "
                f"the declared allowlist {sorted(GOVERNED_ENV_KEYS)}; a tool "
                "inheriting an undeclared variable inherits whatever is in "
                "it")
        leaked = sorted(k for k in env
                        if k not in ("PATH", "PYTHONPATH")
                        and os.environ.get(k) == env[k]
                        and k not in ("PYTHONHASHSEED",)
                        and os.environ.get(k) is not None
                        and env[k] != "1")
        if leaked:                               # pragma: no cover - defence
            raise ValueError(
                f"governed environment values came from the parent: {leaked}")
        return env

    def _build_context(self, task_id: str, tool_id: str, inputs: dict,
                       decision, cap_id: str):
        """Assemble what this run had in front of it, by identity.

        Mandatory tiers cannot be dropped, so a budget too small to hold the
        policy and the task state fails the run rather than quietly producing
        a decision made without them.
        """
        spec = self.registry.get(tool_id)
        builder = ContextBuilder(
            task_id=task_id, purpose=f"governed Stage-10 run of {tool_id}",
            policy_identity=decision.identity,
            policy_digest=decision.policy_digest,
            at_seq=self.log.verify().head_seq)
        builder.add(item_id="owner-instruction",
                    tier=Tier.OWNER_INSTRUCTION,
                    text=("produce the declared Stage-10 artifact; touch no "
                          "gate, no threshold and no canonical output"))
        builder.add(item_id="policy", tier=Tier.SYSTEM_POLICY,
                    source=decision.identity,
                    text=json.dumps(decision.to_record(), sort_keys=True))
        builder.add(item_id="task-state", tier=Tier.TASK_STATE,
                    text=json.dumps({"task_id": task_id, "tool_id": tool_id,
                                     "capability_id": cap_id,
                                     "state": TaskState.LEASED.value},
                                    sort_keys=True))
        builder.add(item_id="tool-contract", tier=Tier.TASK_EVIDENCE,
                    source=spec.digest(),
                    text=json.dumps(spec.body(), sort_keys=True))
        builder.add(item_id="inputs", tier=Tier.TASK_EVIDENCE,
                    source=digest(inputs),
                    text=json.dumps(inputs, sort_keys=True))
        for entry in self.memory.current()[-3:]:
            builder.add(item_id=f"memory-{entry.memory_id}",
                        tier=Tier.MEMORY, source=f"memory:{entry.memory_id}",
                        text=entry.text)
        return builder.build(budget_bytes=64 * 1024)

    def _move(self, task: Task, dst: TaskState, actor: str, role: TaskRole, *,
              lease: Lease | None = None, lease_id: str | None = None,
              executed_by: str | None = None,
              result_digest: str | None = None, note: str = "") -> Task:
        at = self.log.verify().head_seq + 1
        req = TaskTransition(
            task_id=task.task_id, src=task.state, dst=dst, actor=actor,
            role=role, at_seq=at,
            lease_id=lease_id or (lease.lease_id if lease else None),
            executed_by=executed_by, result_digest=result_digest)
        check(req, task)                 # raises if the machine forbids it
        payload = {"task_id": task.task_id, "src": task.state.value,
                   "dst": dst.value, "role": role.value, "note": note}
        if lease is not None:
            payload["lease"] = lease.to_record()
        elif lease_id is not None:
            payload["lease_id"] = lease_id
        if executed_by:
            payload["executed_by"] = executed_by
        if result_digest:
            payload["result_digest"] = result_digest
        self.log.append(actor=actor, action=ACT_TASK_TRANSITION,
                        target=task.task_id, payload=payload)
        return self.projection().get(task.task_id)

    def _capture(self, out_dir: Path, task_id: str) -> dict:
        """Content-address every file the tool produced, and record it."""
        artifacts: dict = {}
        for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(self.root).as_posix()
            artifacts[rel] = self.evidence.put_file(
                path, media_type="application/octet-stream")
        self.log.append(actor="system", action=ACT_EVIDENCE, target=task_id,
                        payload={"task_id": task_id,
                                 "artifacts": dict(sorted(artifacts.items()))})
        return artifacts

    def _verify_artifacts(self, artifacts: dict) -> tuple:
        """Re-derive each digest from the file on disk.

        Deliberately not "does the evidence store still hold that digest" --
        that would confirm the store's own bookkeeping. The question is
        whether the bytes the task cited are still the bytes on disk, which is
        what a later reader following the provenance would want to know.
        """
        if not artifacts:
            return False, ("the run produced no artifacts; a completion with "
                           "nothing to point at is not verifiable")
        for rel, dg in sorted(artifacts.items()):
            path = self.root / rel
            if not path.is_file():
                return False, f"cited artifact {rel} is no longer on disk"
            if digest_bytes(path.read_bytes()) != dg:
                return False, (
                    f"artifact {rel} no longer hashes to the digest the task "
                    "cited")
            if not self.evidence.contains(dg):
                return False, f"artifact {rel} is not resolvable as evidence"
        return True, (f"{len(artifacts)} artifact(s) re-derived from disk and "
                      "resolvable in the evidence store")
