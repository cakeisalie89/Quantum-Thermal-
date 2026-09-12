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
  * the run is mediated by a network guard installed in THIS process with no
    egress grant, so a dependency that phones home FROM THE SUPERVISOR is
    refused rather than merely undeclared -- a narrower statement than this
    line used to make, for the reason set out below;
  * a context manifest records what was available to the run, by digest.

WHERE THE NETWORK GUARD REACHES, AND WHERE IT STOPS

The tool is executed as a SUBPROCESS. :func:`~qta_agent.netauth.socket_guard`
replaces ``socket.socket.connect`` in the process that enters it, and a
subprocess does not inherit a Python monkeypatch. So:

  IN-PROCESS MEDIATION -- what this module has.
      A connection attempted by this supervisor, or by any library it
      imported, is checked against the live grants. No egress grant is
      issued for a governed Stage-10 run, so every such connection is
      refused.

  CHILD / DESCENDANT OS CONTAINMENT -- what this module does NOT have.
      The tool's own process, and anything it spawns, can open sockets
      freely. Nothing here prevents that. Only a kernel-level boundary --
      a network namespace, seccomp, a firewall -- can, and this module
      neither creates one nor pretends to. See the "kernel layer (NOT
      provided)" section of :mod:`qta_agent.netauth`, which has always said
      so; for a long time this docstring did not, and the comment at the
      enforcement point below cross-referenced a sentence that was not here.

``test_the_parent_guard_does_not_bind_the_child`` proves the second half by
entering this exact guard with no grant and then connecting from a child, so
the boundary is a measured fact rather than a caveat somebody remembered.

Every one of those is load-bearing: if the policy denies, if readiness fails,
or if the separation check refuses, the run does not complete and the
Snakemake rule fails the build. A socket opened by the TOOL is not on that
list, because nothing in this module would notice it.

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
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import actions
from .agents import (
    AgentDirectory, AgentError, AgentRole, EscalationState, PrincipalKind,
    StreamNotifier, check_separation, identity,
)
from .canonical import digest
from . import capability as _cap_actions
from .capability import Action, CapabilityLedger, issue
from .context import ContextBuilder, Tier, record_context
from .events import EventLog
from .evidence import EvidenceStore
from .execution import Executor, Limits, Outcome
from .memory import MemoryStore
from .netauth import NetworkAuthority, socket_guard
from .readpath import (
    GovernedReader,
    ReadDenied,
    ReadRequest,
    read_scope,
)
from .safeio import ReadRoot, SafeIOError, SourceChanged
from .separate_verify import verify_in_separate_process
from .policy import Effect, PolicyRequest, PolicyStore, document, rule
from .hostid import (ALIVE, GONE, ProcessIdentity, identify,
                     liveness)
from .idempotency import (IdempotencyConflict, IdempotencyLedger,
                          request_identity)
from .scheduler import (FailureClass, RETRYABLE as SCHED_RETRYABLE,
                        Scheduler)
from .tasks import (
    Lease, Task, TaskProjection, TaskRole, TaskState, TaskTransition,
    TaskTransitionError, apply_transition, check,
)
from .tools import (Determinism, Field_, OutputFile, Registry, SideEffect,
                    ToolError, ToolNotRegistered, ToolSpec)

#: Where governed Stage-10 work is allowed to write. The same subtree the
#: Stage-10 workspace guard permits, so the two agree by construction rather
#: than by two lists that must be kept in step.
WORKSPACE_PREFIX = "verification/stage10"

#: Logical name of the root governed reads are confined to. Read scopes are
#: namespaced by it, so a grant to read under this root cannot be spent
#: against a differently-rooted reader that happens to share a relative path.
READ_ROOT_ID = "repo"

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
ACT_CAP_ISSUE = _cap_actions.ACT_ISSUE
ACT_CAP_REVOKE = _cap_actions.ACT_REVOKE
ACT_EXECUTION = "task.execution"
ACT_EVIDENCE = "task.evidence"
#: A compensating action that was RUN, and the answered escalation that
#: authorized running it. Separate from task.execution because a
#: compensation is not an attempt at the work -- it is an attempt to undo it,
#: and an audit that could not tell them apart would count an undo as a
#: retry.
ACT_COMPENSATION = "task.compensation"
#: What a process that cannot import these reducers concluded about the log
#: this run wrote. Recorded whatever it concluded: "the independent verifier
#: refused" is exactly the fact an auditor must be able to find.
ACT_SEPARATE_VERIFY = "task.separate_verification"
ACT_REEXECUTION = "task.reexecution"

#: The actions THIS projection applies or deliberately passes over. Anything
#: else is another subsystem's (skipped) or unrecognised (refused).
OWNED = frozenset({ACT_TASK_CREATE, ACT_TASK_TRANSITION,
                   ACT_EXECUTION, ACT_EVIDENCE, ACT_COMPENSATION,
                   ACT_SEPARATE_VERIFY, ACT_REEXECUTION})


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
                 reason="governed Stage-10 work is permitted",
                 # CONDITIONAL, not unconditional. This path may run a tool
                 # provided its declared outputs are all accounted for and
                 # what it produced is captured as content. Neither condition
                 # is new behaviour -- both were already done -- but writing
                 # them as obligations moves them from "the code happens to"
                 # to "the authorization required it", and
                 # Decision.raise_if_denied refuses the success report while
                 # either is outstanding.
                 obligations=("record_evidence",
                              "verify_declared_outputs")),
        ))


#: Task states that prove the tool's PROCESS finished. Each is reachable
#: only through COMPLETED, so reaching one is evidence the execution ran to
#: the end. Deliberately excludes FAILED, TIMED_OUT and CANCELLED: those say
#: the attempt stopped, not that it did or did not reach an external effect
#: on its way, and treating them as settled is how an unrecorded external
#: action gets reported as "did not happen".
def _terminate_group(child: ProcessIdentity) -> str:
    """Signal an orphan's process group, never our own.

    The same guard the executor uses, for the same reason: if the recorded
    pgid is this process's group, signalling it would kill the supervisor
    doing the cleanup. That is not hypothetical here -- it happened once
    from a leftover mutation and presented as the test runner dying with
    SIGTERM.
    """
    # REFUSED, not "signalled more narrowly". The executor's own kill path
    # falls back to the bare pid when the group is unusable, and that is
    # right THERE because the pid is a child it started. Here it is wrong:
    # the identity comes from a durable record, and a record naming this
    # process -- or this process's group -- makes the fallback a SIGTERM to
    # the supervisor doing the cleanup.
    #
    # That is not hypothetical. This function shipped with the fallback, its
    # own test asserted the refusal message, the assertion PASSED, and pytest
    # then died from the signal the test had just sent it: 25 dots followed
    # by "Terminated" and exit 143. The same shape as the earlier incident
    # where a leftover mutation removed os.setsid() and the test runner
    # mysteriously died with SIGTERM.
    if child.pid == os.getpid():
        return ("refused: the record names THIS process; a cleanup that "
                "signals itself is not a cleanup")
    pgid = child.pgid
    if pgid is None:
        return (f"refused: pid {child.pid} has no recorded process group, so "
                "there is nothing that can be signalled without guessing")
    if pgid == os.getpgrp():
        return (f"refused: process group {pgid} is this process's own; "
                "signalling it would terminate the supervisor doing the "
                "recovery")
    try:
        os.killpg(pgid, signal.SIGTERM)
        return f"SIGTERM to process group {pgid}"
    except OSError as exc:
        return f"could not signal group {pgid}: {exc}"


#: Task states a dead supervisor can strand. Each has a SYSTEM-owned edge
#: back to QUEUED that requires no lease, which is what makes recovery
#: possible without impersonating the worker that vanished.
#:
#: COMPLETED is deliberately absent: it is waiting for a verifier, not
#: stuck, and moving it would be this system inventing a verdict.
_RECOVERABLE: frozenset = frozenset({
    TaskState.LEASED, TaskState.EXECUTING,
})

EXTERNAL_SETTLED: frozenset = frozenset({
    TaskState.VERIFIED, TaskState.REJECTED, TaskState.INVALIDATED,
})


#: Which subprocess module runs each registered tool. Default-deny by
#: omission, exactly like the registry: a tool with no entry here cannot be
#: launched at all. This was a hard-coded module name in three places, which
#: meant "the registry has more than one tool" and "more than one tool can
#: run" were different statements and only the first was true.
_TOOL_MODULE = {
    "stage10.emit_artifact": "qta_agent._stage10_tool",
    "stage10.digest_index": "qta_agent._stage10_index_tool",
}


def tool_argv(tool_id: str, inputs: dict, *, modules=None) -> list:
    """The argv that runs ``tool_id`` in a subprocess, or raise.

    Raising rather than defaulting is the point. A default would run the
    artifact writer for a tool_id nobody mapped, so a typo in a registry
    entry would produce a governed, capability-checked, evidence-backed
    record of the WRONG tool having run. That was not hypothetical: while
    the module name was a literal, the compensation path launched the
    artifact writer for whatever compensating tool a registry named.

    ``modules`` overrides the mapping the way ``registry`` overrides the
    tool set -- a caller that declares its own tools declares their entry
    points too, and gets the same default-deny over its own mapping rather
    than an exemption from the rule.
    """
    table = _TOOL_MODULE if modules is None else modules
    module = table.get(tool_id)
    if module is None:
        raise ToolNotRegistered(
            f"tool {tool_id!r} has no subprocess module; registered modules "
            f"are {sorted(table)}. A tool that cannot be launched is "
            "not a tool this path will guess an entry point for")
    return [sys.executable, "-m", module, json.dumps(inputs, sort_keys=True)]


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
            # The file, declared by the contract rather than discovered by a
            # sweep. The executor resolves this against the validated inputs
            # and hashes it, so "exited 0 and wrote nothing" is a FAILED run
            # here instead of a COMPLETED one with an empty artifact set.
            output_files=(OutputFile("artifact", "{out_dir}/{name}"),),
            determinism=Determinism.BYTE_IDENTICAL,
            side_effect=SideEffect.SCOPED_WRITES,
            writable_scope=(WORKSPACE_PREFIX,), timeout_s=60.0),
        ToolSpec(
            tool_id="stage10.digest_index", version="1.0.0",
            summary="hash declared workspace files into a deterministic index",
            inputs=(Field_("out_dir", "str"), Field_("name", "str"),
                    Field_("files", "list")),
            outputs=(Field_("path", "str"), Field_("sha256", "str"),
                     Field_("n_files", "int")),
            output_files=(OutputFile("index", "{out_dir}/{name}"),),
            # BYTE_IDENTICAL, and here the claim has teeth. The first tool's
            # output is a function of an argument already recorded in the
            # task, so re-running it could only disagree if the interpreter
            # did. This one's output is a function of files on disk, so a
            # re-run that agrees is evidence the workspace did not move under
            # the run -- and one that disagrees is a finding rather than a
            # false alarm about an assumption nobody measured.
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
    #: ``{obligation: evidence_digest}`` for every condition the ALLOW
    #: attached. Non-empty only on a VERIFIED run: an outcome that did not
    #: satisfy the policy's conditions does not get to claim it did.
    obligations: dict = field(default_factory=dict)
    #: The escalation raised for this run, if one was. Non-empty only on an
    #: UNCERTAIN external outcome, which is the one decision on this path
    #: that is genuinely a person's.
    escalation_id: str = ""
    #: Digest of the context manifest recorded for the run.
    context_digest: str = ""
    #: The remembered note, which is a note and not a finding.
    memory_id: str = ""
    #: The key this submission was bound under, when one was supplied.
    idempotency_key: str = ""
    #: Set when this call did NOT do the work: the task the key was already
    #: bound to, whose state and result are what is being reported.
    duplicate_of: str = ""

    @property
    def is_duplicate(self) -> bool:
        """True when this call reported existing work rather than doing it."""
        return bool(self.duplicate_of)


class GovernedStage10:
    """Runs Stage-10 work through the substrate. The production entry point."""

    def __init__(self, *, root: Path, log: EventLog, evidence: EvidenceStore,
                 registry: Registry | None = None, notifier=None):
        self.root = Path(root)
        self.log = log
        self.evidence = evidence
        self.registry = registry or stage10_registry()
        #: Entry point per tool, mutable beside :attr:`registry` for the same
        #: reason: a caller that supplies its own tools has to say how they
        #: are launched, and gets default-deny over its own mapping.
        self.tool_modules = dict(_TOOL_MODULE)
        self.executor = Executor(self.registry, workspace=self.root)
        #: Where this caller's verification has reached. See :meth:`_head_seq`.
        self._head_anchor = None

        # Every subsystem projects the SAME log. That is the arrangement the
        # action registry exists to permit, and it is what makes the audit of
        # a run a single history rather than several that have to be
        # correlated afterwards.
        self.policy = PolicyStore(self.log).load()
        # An escalation on this path blocks work until a person acts, so it
        # is DELIVERED rather than only filed. stderr because that is where
        # an operator running the workflow -- or a hosted job's log -- will
        # actually keep it; the seam is replaceable, and this is the sink
        # this repository can honestly provide.
        self.agents = AgentDirectory(
            self.log, notifier=notifier or StreamNotifier()).load()
        self.scheduler = Scheduler(self.log, policy=self.policy,
                                   policy_id=POLICY_ID,
                                   capacity={"slots": 2}).load()
        self.memory = MemoryStore(self.log, evidence=self.evidence).load()
        #: No egress grant is ever issued here. The Stage-10 tool needs none,
        #: and an authority with no grants denies everything -- which is what
        #: the socket guard enforces during execution.
        self.network = NetworkAuthority(self.log).load()
        #: Grants are PROJECTED from the log, not assembled here. A caller
        #: that builds its own CapabilitySet can put anything in it, which
        #: made the issuance event decorative -- see CapabilityLedger.
        self.capabilities = CapabilityLedger(self.log).load()
        #: Owner-scoped request identity, so a resubmission after a lost
        #: response returns the first task instead of starting a second one.
        self.idempotency = IdempotencyLedger(self.log).load()
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
    def _head_seq(self) -> int:
        """The current head, verifying only what this caller has not seen.

        WHY THIS IS NOT ``self._head_seq()``

        It was, at eighteen call sites, and one governed run made twenty-six
        full chain verifications. Measured: a run on a six-run-old log read
        3,544 records to answer "where is the head" twenty-six times, against
        391 on the first run. Per-run work grows linearly with the history and
        the total is quadratic -- which is the defect
        :meth:`~qta_agent.events.EventLog.advance` was written for, and its
        docstring says so: *"That is the quadratic defect this repository has
        already recorded twice, in a third place."* This was the third place.

        The write path was never the problem: ``append`` has always carried
        an anchor and re-checked only the tail. It was the READ of the head
        that went back to the beginning, every time.

        SAME GUARANTEE, NOT A WEAKER ONE

        The first call verifies the whole chain and fails closed. Afterwards
        every record is verified exactly once, by ``advance``, which refuses
        to move past a broken chain -- rather than the whole history being
        re-verified twenty-six times per run. ``projection()`` still verifies
        in full, because a reader that cannot say which records went through
        the gate must not hand back a state.

        WHY THE EXISTING PERFORMANCE GUARD DID NOT SEE IT

        ``test_one_governed_operation_does_not_get_slower_as_the_history_
        grows`` measures WALL TIME, and a governed run is dominated by a
        subprocess. Hashing three thousand records is microseconds against a
        1.2-second run, so the ratio stayed at 1.1 against a ceiling of 4.0
        while the work underneath it grew without bound. A time guard on a
        subprocess-dominated operation cannot see the growth of the work
        inside it, which is why the test beside this change counts RECORDS.
        """
        if self._head_anchor is None:
            report = self.log.verify()
            report.raise_if_bad()
            if report.head_seq < 0:
                return -1
            self._head_anchor = self.log.anchor_at(report.head_seq)
            return report.head_seq
        _, self._head_anchor = self.log.advance(self._head_anchor)
        return self._head_anchor.seq

    def projection(self) -> TaskProjection:
        """Rebuild task state from the verified log. Fail closed.

        The whole point of a durable lifecycle: this answers "what was this
        task doing when the machine died" by replay, not by inference.
        """
        # ONE pass, and the records that come back are the ones that were
        # checked. Verifying and then reading again looked equivalent and is
        # not: a record landing between the two calls is folded without its
        # chain link ever being checked by this call, and a forged one was
        # (D-2026-41). It is also half the file reads.
        report, events = self.log.read_verified()
        report.raise_if_bad()
        tasks: dict = {}
        seq = -1
        for ev in events:
            seq = ev.seq
            p = ev.payload
            if ev.action == ACT_TASK_CREATE:
                # The submitter is who asked for the work, and it is what the
                # policy gate was evaluated against when the task was
                # admitted. Taken from the payload alone it is a name the
                # record chose for itself, so a forged create attributes work
                # to somebody who never asked for it -- and every later
                # question about who submitted this reads the forgery back.
                # scheduler.enqueue has always checked this; task.create did
                # not.
                if p["submitter"] != ev.actor:
                    raise TaskTransitionError(
                        f"seq {ev.seq}: task {p['task_id']!r} names submitter "
                        f"{p['submitter']!r} but was appended by "
                        f"{ev.actor!r}; work is attributed to whoever asked "
                        "for it, not to whoever a record nominates")
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
                # THE SRC COMES FROM THE REPLAY, NOT FROM THE RECORD.
                #
                # This re-authorizes, and re-authorizing against a src the
                # WRITER supplied authorizes nothing: every pair the table
                # contains is available to a forger, who simply names a
                # convenient starting state. A hostile campaign moved a task
                # out of VERIFIED -- a sealed state -- by appending one record
                # claiming src=EXECUTING, and the check passed because
                # EXECUTING -> TIMED_OUT is a real edge.
                #
                # The existing forgery test missed it by choosing a pair that
                # is not an edge at all, so the record was refused for a
                # different reason and this one stayed hidden. That is the
                # "passes for the wrong reason" shape, in the test guarding
                # the property that matters most here.
                #
                # scheduler.apply and reconstruct.reconstruct both already do
                # it this way. This projection was the odd one out, and it is
                # the one on the production path.
                claimed = TaskState(p["src"])
                if task.state is not claimed:
                    raise TaskTransitionError(
                        f"seq {ev.seq}: {task.task_id!r} is "
                        f"{task.state.value}, but the record moves it from "
                        f"{claimed.value}. A transition whose starting state "
                        "the replay does not agree with was not written "
                        "through the gate; applying it would let a forger "
                        "pick any edge in the table by naming a convenient "
                        "src.")
                # AND THE EXECUTOR COMES FROM THE EXECUTION RECORD.
                #
                # Separation of duties is checked against task.executed_by,
                # and this projection used to take that from a transition
                # PAYLOAD -- a field the same actor writes. So the worker
                # holding the lease could complete its own task while naming
                # a fictitious executor, and then verify it as VERIFIER. It
                # worked: a differential comparison against reconstruct_tasks
                # disagreed at exactly the prefix between the execution
                # record and the completion, and the bypass was underneath.
                #
                # The execution record is the durable statement of who ran
                # the tool. A claim that disagrees with it is refused rather
                # than preferred.
                # AND THERE MUST BE ONE. The first version of this check
                # compared the claim only when an execution record had
                # already set task.executed_by. Omitting the execution
                # record entirely therefore left the claim unopposed, and a
                # forged history that never ran anything got to invent an
                # executor out of a string: one actor created, leased,
                # "executed", completed and VERIFIED its own task, named a
                # ghost as the executor, and both readers agreed with it.
                #
                # So the claim is compared against task.executed_by
                # including when that is None. With no execution record the
                # executor stays unset, and COMPLETED -> VERIFIED is then
                # refused by the state machine for the honest reason:
                # nothing records who ran this, so nothing can be
                # independent of them.
                claimed_by = p.get("executed_by")
                if claimed_by and claimed_by != task.executed_by:
                    raise TaskTransitionError(
                        f"seq {ev.seq}: the record names {claimed_by!r} as "
                        f"the executor of {task.task_id!r}, but the execution "
                        f"record says {task.executed_by!r}. Separation of "
                        "duties is checked against the executor, so a "
                        "transition that gets to name one could verify its "
                        "own work.")
                req = TaskTransition(
                    task_id=p["task_id"], src=task.state,
                    dst=TaskState(p["dst"]), actor=ev.actor,
                    role=TaskRole(p["role"]), at_seq=ev.seq,
                    lease_id=(lease.lease_id if lease else p.get("lease_id")),
                    executed_by=task.executed_by,
                    result_digest=p.get("result_digest"))
                # Re-authorize on replay. A transition that would be refused
                # today is not applied, so a forged log entry cannot become
                # state simply by being present.
                edge = check(req, task)
                tasks[p["task_id"]] = apply_transition(
                    task, edge, req, seq=ev.seq, lease=lease)
            elif ev.action == ACT_EXECUTION:
                # WHO RAN IT is recorded here, by the actor that ran it, and
                # this is where the projection learns it. Skipping this record
                # and reading the executor out of a later transition payload
                # is what let a worker verify its own work.
                task = tasks[p["task_id"]]
                tasks[p["task_id"]] = replace(
                    task, executed_by=ev.actor,
                    result_digest=p.get("result_digest")
                    or task.result_digest,
                    updated_seq=ev.seq)
            elif ev.action in (ACT_EVIDENCE, ACT_COMPENSATION,
                               ACT_SEPARATE_VERIFY, ACT_REEXECUTION):
                if ev.action is ACT_COMPENSATION or (
                        ev.action == ACT_COMPENSATION):
                    # A DIAGNOSTIC DUPLICATE MUST AGREE WITH ITS SOURCE.
                    #
                    # `answered_by` here is a copy of the escalation's
                    # answerer, carried so an auditor reading the
                    # compensation record can see who authorized the undo
                    # without joining two tables. The authority is the
                    # escalation; this is a convenience.
                    #
                    # A convenience that nothing checks is a field a forged
                    # record can set freely, and it names a PERSON as having
                    # authorized destroying something. So it is compared to
                    # the escalation it cites. Not folded into state -- a
                    # compensation is a fact about the task rather than a
                    # state of it -- but not taken on trust either.
                    eid = p.get("authorized_by_escalation")
                    named = p.get("answered_by")
                    if eid:
                        try:
                            esc = self.agents.escalation(eid)
                        except Exception:
                            esc = None
                        if esc is not None and named != esc.answered_by:
                            raise TaskTransitionError(
                                f"seq {ev.seq}: compensation of "
                                f"{p.get('task_id')!r} says escalation "
                                f"{eid!r} was answered by {named!r}; that "
                                f"escalation was answered by "
                                f"{esc.answered_by!r}. A record naming who "
                                "authorized an undo may repeat the "
                                "escalation and may not disagree with it")
                # A compensation does not move the task. The task's outcome
                # was and remains whatever it reached; what a compensation
                # records is that somebody tried to undo its effect, which is
                # a fact ABOUT the task rather than a state of it. Folding it
                # into the state machine would make "was compensated" and
                # "did not happen" the same answer.
                #
                # The same is true of a re-execution: it is a fact about how
                # the result was checked, and the transition it justifies is
                # recorded as a transition.
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
            lease_seqs: int = LEASE_SEQS,
            depends_on: tuple = (),
            requires_evidence: tuple = (),
            idempotency_key: str | None = None) -> GovernedRun:
        """Take one unit of work all the way through the control plane.

        ``worker`` and ``verifier`` must differ. That is not a style
        preference: the task state machine refuses the COMPLETED -> VERIFIED
        edge when they are the same, so passing one identity for both fails
        here rather than producing a verification that verified nothing.

        ``idempotency_key`` makes the submission resumable. The same
        submitter resending the same request under the same key gets the
        FIRST task back and nothing is executed a second time -- which is
        what a caller needs after a lost response or a supervisor that died
        with the request in flight. The key is scoped to this submitter and
        this tool, so it is not a string another actor can guess into.

        This is duplicate suppression and durable request identity. It is
        not exactly-once execution against anything outside this system;
        see :mod:`qta_agent.idempotency`.
        """
        if worker == verifier:
            raise ValueError(
                "worker and verifier must be different actors; an agent that "
                "verifies its own work has not verified anything")

        # The separation is checked by the DIRECTORY, against the roles those
        # identities actually hold, rather than by comparing two strings. Two
        # runs of one agent are one party here, so a worker that restarts
        # under a new instance id still cannot verify its own work.
        # The SUBMITTER is checked too, and was not before. Whoever submits
        # is the subject of the policy decision and -- once submissions can
        # be bound to a key -- the owner of a durable namespace. An
        # unregistered string could hold both. A proposer is not an
        # executor: the role checked here is the one this identity is
        # actually acting in.
        self.agents.require(submitter, AgentRole.PROPOSER)
        self.agents.require(worker, AgentRole.EXECUTOR)
        self.agents.require(verifier, AgentRole.VERIFIER)
        separation = check_separation(
            self.agents, instance_id=verifier, taking=AgentRole.VERIFIER,
            already={AgentRole.EXECUTOR: worker})
        if not separation.allowed:
            raise ValueError(f"refusing to run: {separation.reason}")

        spec = self.registry.get(tool_id)          # default deny

        # A SUPERVISOR STARTS BY RESOLVING WHAT A DEAD ONE LEFT.
        #
        # This is the production caller for recover(). It appends nothing
        # when nothing is stranded, so the common path costs one projection;
        # and putting it here rather than in __init__ keeps a constructor
        # free of durable side effects while still guaranteeing that no run
        # begins on top of an unresolved predecessor.
        self.recover()

        # THE DUPLICATE CHECK COMES FIRST, before a task exists.
        #
        # It has to. Creating the task and then discovering the key was
        # already bound would leave an orphan task per resubmission, and the
        # whole point is that a resubmission changes nothing. The lookup is
        # scoped to (submitter, tool, key), so there is no lookup that
        # reaches another actor's binding -- not "refused", not reachable.
        request_digest = request_identity(tool_id=tool_id, inputs=inputs)
        if idempotency_key:
            prior = self.idempotency.lookup(
                owner=submitter, tool_id=tool_id, key=idempotency_key)
            if prior is not None:
                if prior.request_digest != request_digest:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} is already "
                        f"bound to task {prior.task_id!r} for a different "
                        "request. A corrected request is a different "
                        "request and needs a different key.")
                return self._report_duplicate(prior, spec)

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

        # BOUND HERE: after the task exists and BEFORE anything is
        # dispatched. A crash in between is then recoverable as "already
        # submitted" rather than re-submitted, which is the case the key
        # exists for.
        #
        # The cost is deliberate: binding before validation means a rejected
        # request keeps its key. The key names A REQUEST, that request was
        # rejected, and a corrected one is a different request. Letting the
        # corrected version in under the same key would mean the key stopped
        # identifying anything.
        if idempotency_key:
            self.idempotency.bind(
                owner=submitter, tool_id=tool_id, key=idempotency_key,
                request_digest=request_digest, task_id=task_id,
                job_id=job_id)

        # Validation is a real gate: the contract is checked before anything
        # is scheduled, and a rejection is recorded rather than raised away.
        try:
            spec.validate_inputs(inputs)
        except Exception as exc:
            task = self._move(task, TaskState.REJECTED, submitter,
                              TaskRole.SUBMITTER, note=str(exc))
            return GovernedRun(task_id, task.state, "REJECTED", "", {},
                               self._head_seq(), str(exc),
                               policy_identity=decision.identity,
                               policy_digest=decision.policy_digest)

        task = self._move(task, TaskState.VALIDATED, submitter,
                          TaskRole.SUBMITTER)

        # Queued through the REAL scheduler: readiness, the lease and the
        # outcome report are the ones the scheduler enforces, not a parallel
        # implementation beside it that happens to agree today.
        # DEPENDENCIES GO TO THE SCHEDULER, not to the caller's loop.
        #
        # A caller that ordered the work itself would be sequencing it, and
        # the queue would never see a graph -- which is exactly what the row
        # said was true: "dependency graphs are exercised only in tests".
        # Passing them here means readiness is decided by reconcile() against
        # the log, so a step whose parent failed is BLOCKED by the scheduler
        # rather than skipped by a loop that happened to notice.
        self.scheduler.enqueue(job_id=job_id, work_digest=inputs_digest,
                               submitter=submitter, task_id=task_id,
                               depends_on=tuple(depends_on),
                               requires_evidence=tuple(requires_evidence),
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
                      expires_after_seq=job.lease_expires_after_seq,
                      # Recorded by the process that is about to do the
                      # work, about ITSELF. A later supervisor asks the
                      # operating system about it rather than guessing from
                      # a sequence number that stopped advancing when this
                      # process died.
                      holder_process=identify().to_record())
        task = self._move(task, TaskState.LEASED, worker, TaskRole.WORKER,
                          lease=lease)

        # The grant is minted for THIS task and THIS tool, and expires with
        # the lease. Recorded in the log so the issued set is a projection of
        # a verified history rather than something the caller asserts.
        cap_id = f"cap-{uuid.uuid4().hex[:8]}"
        head = self._head_seq()
        cap = issue(capability_id=cap_id, subject=worker,
                    action=Action.EXECUTE_TOOL, task_id=task_id,
                    tool_id=tool_id, scope=(WORKSPACE_PREFIX,),
                    issued_seq=head + 1,
                    expires_after_seq=lease.expires_after_seq,
                    issued_wall_time=time.time())
        self.capabilities.issue(cap, actor="scheduler")

        # A SEPARATE grant, for the verifier, to READ what the worker wrote.
        # Separate because reading and writing are separate authorities and
        # because the verifier is a different actor: the executor's grant
        # must not be what lets the check on its work happen. It is minted
        # here so it is in the log before the read that needs it, and it
        # expires with the same lease.
        read_cap_id = f"cap-read-{uuid.uuid4().hex[:8]}"
        read_cap = issue(capability_id=read_cap_id, subject=verifier,
                         action=Action.READ_PATHS, task_id=task_id,
                         scope=read_scope(READ_ROOT_ID, WORKSPACE_PREFIX),
                         issued_seq=self._head_seq() + 1,
                         expires_after_seq=lease.expires_after_seq,
                         issued_wall_time=time.time())
        self.capabilities.issue(read_cap, actor="scheduler")
        # Projected back out of the log. If the issuance were not recorded,
        # or were recorded with different terms, the executor would be
        # checking against something that does not exist.
        caps = self.capabilities.in_force(self._head_seq())

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
        argv = self._argv(tool_id, inputs)
        env = self._tool_environment()
        # No egress grant was issued, so the guard denies every connection
        # ATTEMPTED IN THIS PROCESS. That catches the case a declaration
        # cannot: a dependency of the SUPERVISOR reaching the network.
        #
        # It binds this process, not the child. The executor below starts a
        # subprocess, and a subprocess does not inherit a Python monkeypatch,
        # so the tool itself is not confined by this block. This comment used
        # to end "and the module says so too"; the module said the opposite
        # for the whole life of the file, and a cross-reference to a sentence
        # that does not exist is worse than no cross-reference, because it
        # reads as corroboration. The module docstring now states the
        # boundary and a test measures it.
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
            #
            # The executor gets the first word, though, because it holds the
            # fact the scheduler does not: what the tool's contract says it
            # touched. An outcome that would otherwise be retryable is
            # reported as UNSAFE_TO_RETRY when the tool declares EXTERNAL
            # effects -- the run may already have changed state nobody here
            # owns, and a retry would do it again. This is where
            # SideEffect.EXTERNAL stops being a label and starts costing a
            # retry.
            failure = {Outcome.TIMED_OUT: FailureClass.TIMEOUT,
                       Outcome.CANCELLED: FailureClass.CANCELLED,
                       }.get(result.outcome, FailureClass.PERMANENT)
            if failure in SCHED_RETRYABLE and not result.retryable:
                failure = FailureClass.UNSAFE_TO_RETRY
            job = self.scheduler.report(job_id=job_id, worker=worker,
                                        failure=failure,
                                        detail=result.reason)
            return GovernedRun(task_id, task.state, result.outcome.value,
                               result_digest, {},
                               self._head_seq(), detail,
                               job_id=job_id, job_state=job.state.value,
                               policy_identity=decision.identity,
                               policy_digest=decision.policy_digest,
                               context_digest=context.manifest.digest())

        # Capture what the tool produced as CONTENT, not as a claim about it.
        artifacts = self._capture(out_dir, task_id)

        # Two independent observers of the same bytes: the executor hashed
        # each declared output when the process ended, and the sweep above
        # hashed whatever was on disk afterwards. They must agree, and a
        # disagreement is not a bookkeeping detail -- it means the file
        # changed between the run and the capture, which is the window an
        # artifact would be swapped in.
        drift = self._check_declared_outputs(result, artifacts)
        if drift:
            # FAILED, not REJECTED, and the task machine is what settled it:
            # REJECTED is reachable only from COMPLETED because it means a
            # verifier looked at a finished result and refused it. No
            # verifier has looked yet. What happened here is that the ATTEMPT
            # did not leave a stable artifact behind, which is the worker's
            # run failing -- and moving to COMPLETED first to reach REJECTED
            # would assert a completion whose evidence we already know is
            # bad.
            task = self._move(task, TaskState.FAILED, worker,
                              TaskRole.WORKER, lease_id=lease.lease_id,
                              note=drift)
            job = self.scheduler.report(
                job_id=job_id, worker=worker,
                failure=FailureClass.EVIDENCE_FAILED, detail=drift)
            return GovernedRun(task_id, task.state, result.outcome.value,
                               result_digest, artifacts,
                               self._head_seq(), drift,
                               job_id=job_id, job_state=job.state.value,
                               policy_identity=decision.identity,
                               policy_digest=decision.policy_digest,
                               context_digest=context.manifest.digest())

        # The first obligation the ALLOW attached is now satisfiable: every
        # output the contract declared was collected by the executor AND
        # re-hashed by the capture, and the two agree. The discharge cites
        # the digest of that agreement, so an auditor asking "what closed
        # this condition" gets an artifact set rather than a boolean.
        decision = decision.discharge(
            "verify_declared_outputs",
            evidence_digest=digest({"declared": sorted(result.output_digests),
                                    "captured": artifacts}))

        task = self._move(task, TaskState.COMPLETED, worker, TaskRole.WORKER,
                          lease_id=lease.lease_id, executed_by=worker,
                          result_digest=result_digest)

        # Independent verification: a different actor, re-deriving the digests
        # from the files rather than trusting the ones just recorded.
        ok, why = self._verify_artifacts(
            artifacts, verifier=verifier, task_id=task_id,
            capability_id=read_cap_id)

        # AND A SECOND PROCESS, which is a different question.
        #
        # The check above asks whether the ARTIFACTS are what the record
        # says. This asks whether the LOG this run just wrote reconstructs
        # cleanly when read by something that cannot import the reducers
        # that wrote it. Both are needed and neither substitutes: an
        # artifact can be honest in a history that is not, and the reducers
        # in this interpreter agree with themselves for free.
        #
        # A crashed verifier is not a pass. Any non-zero exit, timeout or
        # unreadable answer refuses the run, and the reason travels with it.
        # AND THE RESULT ITSELF, not only the bytes that carry it.
        #
        # Re-deriving a digest asks whether the file changed. It cannot ask
        # whether the tool produced the right thing: the same wrong bytes
        # hash to the same wrong digest every time. For a tool that DECLARES
        # byte-identical determinism the tool can simply be run again, and
        # that is a question about the result rather than about the file.
        if ok:
            ok, again = self._reexecute_and_compare(
                tool_id=tool_id, inputs=inputs, verifier=verifier,
                task_id=task_id,
                declared={result.output_paths.get(name, name): dg
                          for name, dg in result.output_digests.items()})
            why = why + "; " + again if ok else again

        if ok:
            sep = verify_in_separate_process(self.log.path, root=self.root)
            self.log.append(
                actor=verifier, action=ACT_SEPARATE_VERIFY, target=task_id,
                payload={"task_id": task_id, **sep.to_record()})
            if not sep.ok:
                ok = False
                why = (f"independent process verification refused this run: "
                       f"{sep.reason}")

        dst = TaskState.VERIFIED if ok else TaskState.REJECTED
        task = self._move(task, dst, verifier, TaskRole.VERIFIER, note=why)

        if ok:
            # The second obligation: the run's products exist in the
            # content-addressed store and a DIFFERENT actor re-derived their
            # digests from disk. Discharged only on the verified path,
            # because a rejected verification is precisely the case where
            # the evidence was not established.
            decision = decision.discharge(
                "record_evidence",
                evidence_digest=digest(dict(sorted(artifacts.items()))))
            job = self.scheduler.report(job_id=job_id, worker=worker,
                                        detail=why, actor=verifier)
        else:
            job = self.scheduler.report(
                job_id=job_id, worker=worker,
                failure=FailureClass.VERIFICATION_FAILED, detail=why,
                actor=verifier)

        # THE OBLIGATIONS ARE LOAD-BEARING HERE.
        #
        # The receipt is a FIELD of the successful result, not a check before
        # it. Deleting either discharge above does not remove a call somebody
        # might not miss -- it makes this line raise PolicyObligationUnmet on
        # the way to reporting success, because the result cannot be built
        # without the receipt.
        #
        # Only on the verified path: a REJECTED run leaving obligations
        # outstanding is correct, since it did not do what they required.
        receipt = decision.completion_receipt() if ok else {}

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
                           self._head_seq(), why,
                           job_id=job_id, job_state=job.state.value,
                           policy_identity=decision.identity,
                           policy_digest=decision.policy_digest,
                           context_digest=context.manifest.digest(),
                           memory_id=memory_id,
                           obligations=receipt)

    # ---- multi-step production work ------------------------------------
    def run_graph(self, steps, *, submitter: str = SUBMITTER_ID) -> tuple:
        """Run several governed steps with declared dependencies between them.

        WHY THE PRODUCTION CALLER NEEDED THIS

        The governed path enqueued ONE job per run, so dependency graphs and
        multi-job scheduling were exercised only in tests and in the
        long-horizon workload -- never by a real caller. A scheduler feature
        that only tests reach is a scheduler feature nobody is relying on,
        and the row said so.

        THE DEPENDENCY IS THE SCHEDULER'S, NOT THIS LOOP'S

        Every step is enqueued FIRST, with its ``depends_on`` recorded, and
        then work is taken from :meth:`Scheduler.ready_queue` until the queue
        is empty. Ordering therefore comes from the scheduler's own readiness
        rule and from the log, not from the order this function happens to
        iterate in -- which is what makes it a real dependency rather than a
        convention the caller keeps.

        A step whose dependency FAILED is never dispatched: the scheduler
        blocks it, and this reports it as BLOCKED rather than running it
        against a parent that did not produce anything.

        ``steps`` is a sequence of ``(step_id, tool_id, inputs, depends_on)``,
        where ``depends_on`` names earlier ``step_id`` values.
        """
        steps = list(steps)
        if not steps:
            raise ValueError(
                "a graph with no steps would report success having run "
                "nothing")
        ids = [sid for sid, _t, _i, _d in steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate step ids in {ids}")
        for sid, _t, _i, deps in steps:
            unknown = [d for d in deps if d not in ids]
            if unknown:
                raise ValueError(
                    f"step {sid!r} depends on {unknown}, which this graph "
                    "does not contain; a dependency on nothing is a step "
                    "that can never become ready")

        # Enqueue EVERYTHING before running anything. A caller that enqueued
        # and ran one step at a time would be sequencing the work itself and
        # the scheduler would never see a graph.
        results: dict = {}
        planned: dict = {}
        for sid, tool_id, inputs, deps in steps:
            planned[sid] = (tool_id, inputs, tuple(deps))

        remaining = dict(planned)
        done: dict = {}
        job_of: dict = {}
        while remaining:
            runnable = [sid for sid, (_t, _i, deps) in remaining.items()
                        if all(d in done for d in deps)]
            if not runnable:
                # Every remaining step is waiting on one that did not
                # succeed. Reporting them as blocked is the honest answer;
                # running them anyway would execute work against a parent
                # that produced nothing.
                for sid, (_t, _i, deps) in sorted(remaining.items()):
                    unmet = sorted(d for d in deps if d not in done)
                    results[sid] = GovernedRun(
                        "", TaskState.CREATED, "BLOCKED", "", {},
                        self._head_seq(),
                        f"step {sid!r} was never dispatched: it depends on "
                        f"{unmet}, which did not succeed")
                break
            for sid in sorted(runnable):
                tool_id, inputs, deps = remaining.pop(sid)
                run = self.run(
                    tool_id=tool_id, inputs=inputs, submitter=submitter,
                    # The SCHEDULER is told what this waits on, by job id.
                    # It has already seen those jobs succeed, so this one is
                    # ready -- and if one had not, reconcile would refuse to
                    # make it ready and dispatch would raise rather than run
                    # work against a parent that produced nothing.
                    depends_on=tuple(job_of[d] for d in deps if d in job_of),
                    # And on the CONTENT those steps produced, so readiness
                    # is not merely "the parent job says SUCCEEDED" but "the
                    # bytes it was supposed to leave behind resolve".
                    requires_evidence=tuple(sorted(
                        dg for d in deps
                        for dg in (done[d].artifacts.values() if d in done
                                   else ()))))
                results[sid] = run
                if run.job_id:
                    job_of[sid] = run.job_id
                if run.state is TaskState.VERIFIED:
                    done[sid] = run
        return tuple((sid, results[sid]) for sid in ids if sid in results)

    # ---- compensation --------------------------------------------------
    def compensate(self, *, task_id: str, escalation_id: str,
                   actor: str = "system") -> dict:
        """Run the declared undo, because a PERSON decided it should run.

        WHY THIS IS NOT AUTOMATIC

        The case a compensation exists for is the case where nobody knows
        whether the effect happened: a supervisor died between an EXTERNAL
        action and the record of it. Running the undo automatically would
        perform an unrequested external action in exactly the situation where
        there may have been nothing to undo -- the same mistake as an
        automatic retry, pointed the other way.

        So this refuses unless an escalation for this task has been ANSWERED
        ``COMPENSATE``, by a human principal who was not the one that raised
        it. The authority for the action is a decision in the log, and the
        record cites it.

        WHAT IT DOES AND DOES NOT ESTABLISH

        It runs the compensating tool through the ordinary executor: same
        contract, same capability, same limits, same provenance. What it
        cannot establish is that the far side is now in the state the undo
        intended -- only the far side knows that, and the record says the
        compensation RAN rather than that it worked.
        """
        spec = self.registry.get(self._tool_of(task_id))
        undo = self.registry.compensator_for(spec.tool_id)
        if undo is None:
            raise ToolError(
                f"{spec.tool_id} declares a compensation and names no tool "
                f"to perform it: {spec.compensation!r}. That is an operator's "
                "action, not this system's, and reporting otherwise would be "
                "the fabrication the declaration exists to avoid")
        esc = self.agents.escalation(escalation_id)
        if esc.task_id != task_id:
            raise AgentError(
                f"escalation {escalation_id!r} is about task {esc.task_id!r}, "
                f"not {task_id!r}. An answer given about one piece of work "
                "does not authorize acting on another")
        if esc.state is not EscalationState.ANSWERED:
            raise AgentError(
                f"escalation {escalation_id!r} is {esc.state.value}; a "
                "compensation runs because somebody decided it should, and "
                "nobody has decided yet")
        if esc.answer != "COMPENSATE":
            raise AgentError(
                f"escalation {escalation_id!r} was answered "
                f"{esc.answer!r}, not 'COMPENSATE'. Running the undo anyway "
                "would perform an external action the person asked was "
                "explicitly not to perform")

        # THROUGH THE ORDINARY EXECUTOR. Same contract, same capability,
        # same limits, same provenance -- a compensation that ran outside all
        # of that would be a shell command in a runbook wearing this system's
        # name. The egress guard is applied here too: an undo is still an
        # unaudited network call if nothing stops it making one.
        cap_id = self._compensation_capability(task_id, actor)
        inputs = {"task_id": task_id}
        undo.validate_inputs(inputs)
        argv = self._argv(undo.tool_id, inputs)
        with socket_guard(self.network, actor=actor, task_id=task_id,
                          tool_id=undo.tool_id):
            result = self.executor.run(
                tool_id=undo.tool_id, actor=actor, task_id=task_id,
                inputs=inputs, argv=argv, cwd=self.root,
                capabilities=self.capabilities.in_force(
                    self._head_seq()),
                capability_id=cap_id,
                limits=Limits(wall_seconds=undo.timeout_s),
                env=self._tool_environment())
        self.log.append(
            actor=actor, action=ACT_COMPENSATION, target=task_id,
            payload={"task_id": task_id,
                     "compensated_tool": spec.tool_id,
                     "compensating_tool": undo.tool_id,
                     "contract_digest": undo.digest(),
                     "authorized_by_escalation": escalation_id,
                     "answered_by": esc.answered_by,
                     "outcome": result.outcome.value,
                     "exit_status": result.exit_status,
                     "declared_compensation": spec.compensation})
        return {"task_id": task_id, "tool_id": undo.tool_id,
                "outcome": result.outcome.value,
                "authorized_by_escalation": escalation_id,
                "answered_by": esc.answered_by,
                # Said plainly, because the alternative is a report that
                # reads like confirmation. The undo RAN; whether the far side
                # is now in the state it intended is not knowable here.
                "establishes": "the compensating tool ran to this outcome; "
                               "it does not establish the external system's "
                               "current state"}

    def _compensation_capability(self, task_id: str, actor: str) -> str:
        """A grant minted for THIS undo, on THIS task, and nothing else.

        The compensation does not borrow the original run's capability. That
        grant was for the tool that caused the effect, and reusing it would
        make "may run the thing" and "may undo the thing" the same authority
        -- so a component tricked into one would hold the other.
        """
        cap_id = f"cap-undo-{uuid.uuid4().hex[:8]}"
        undo = self.registry.compensator_for(self._tool_of(task_id))
        head = self._head_seq()
        self.capabilities.issue(
            issue(capability_id=cap_id, subject=actor,
                  action=Action.EXECUTE_TOOL, task_id=task_id,
                  tool_id=undo.tool_id, scope=(WORKSPACE_PREFIX,),
                  issued_seq=head + 1,
                  # Bounded to this one action. A compensation grant that
                  # outlived the compensation would be standing authority to
                  # touch an external system.
                  expires_after_seq=head + 4,
                  issued_wall_time=time.time()),
            actor="scheduler")
        return cap_id

    def _reverification_capability(self, task_id: str, verifier: str,
                                   tool_id: str) -> str:
        """A grant for THIS re-run, by THIS verifier, on THIS task.

        Bounded the same way the compensation grant is, and for the same
        reason: authority to re-run a tool in order to check it is not
        standing authority to run it, and a grant that outlived the check
        would be exactly that.
        """
        cap_id = f"cap-reverify-{uuid.uuid4().hex[:8]}"
        head = self._head_seq()
        self.capabilities.issue(
            issue(capability_id=cap_id, subject=verifier,
                  action=Action.EXECUTE_TOOL, task_id=task_id,
                  tool_id=tool_id, scope=(WORKSPACE_PREFIX,),
                  issued_seq=head + 1, expires_after_seq=head + 4,
                  issued_wall_time=time.time()),
            actor="scheduler")
        return cap_id

    def _argv(self, tool_id: str, inputs: dict) -> list:
        """This runner's entry point for ``tool_id``. See :func:`tool_argv`."""
        return tool_argv(tool_id, inputs, modules=self.tool_modules)

    def _tool_of(self, task_id: str) -> str:
        """Which tool a task ran, from the log rather than from a caller."""
        for ev in self.log.read():
            if ev.action == ACT_TASK_CREATE and \
                    ev.payload.get("task_id") == task_id:
                return ev.payload.get("tool_id", "")
        raise ValueError(f"no task {task_id!r} in this log")

    # ---- recovery ------------------------------------------------------
    def recover(self, *, actor: str = "system") -> tuple:
        """Resolve task records a dead supervisor left in flight.

        THE GAP THIS CLOSES, stated as it was found: the scheduler
        reconciles JOBS -- a lapsed lease returns the work to READY -- and
        nothing reconciled TASKS. A supervisor that died between the
        execution record and the COMPLETED transition left the task
        EXECUTING forever, with a lease nobody holds. The state machine had
        the edges for this from the beginning (EXECUTING -> QUEUED and
        LEASED -> QUEUED, both owned by SYSTEM and neither requiring a
        lease) and nothing drove them, which is the same shape as a defence
        with no caller.

        WHY THE WORK GOES BACK TO THE QUEUE RATHER THAN FORWARD

        A recovered task is NOT completed from its execution record, even
        when that record says the process exited 0. The COMPLETED edge
        requires a live lease and the WORKER role, and both are gone: the
        worker that held the lease is dead, and a recovery process
        finishing another party's work under a lapsed lease is exactly the
        ownership bypass the lease exists to prevent. The execution record
        survives as evidence of the attempt; what it does not do is
        authorize a transition on behalf of an actor that is no longer
        there.

        WHAT IT DELIBERATELY LEAVES ALONE

        A task sitting in COMPLETED is not stuck -- it is waiting for an
        independent verifier, and inventing that verdict is the one thing
        this whole system exists to refuse. It is reported, not moved.

        Returns one record per action, and appends NOTHING when there is
        nothing to recover, so a supervisor may call it on every start.
        """
        head = self._head_seq()
        projection = self.projection()
        # TaskProjection.expired_leases() has existed since the lifecycle was
        # written, documented as "the scheduler's input for returning
        # stranded work to the queue", and had ZERO callers -- not even a
        # test. That is why this gap existed: the question was implemented
        # and never asked.
        stranded = list(projection.expired_leases())
        seen = {t.task_id for t in stranded}
        # Two more cases expired_leases() cannot see, both of which end in a
        # lease that outlives the process holding it.
        for task in sorted(projection.tasks.values(),
                           key=lambda t: t.task_id):
            if task.state not in _RECOVERABLE or task.task_id in seen:
                continue
            if task.lease is None:
                # Should be unreachable: the edges into LEASED and EXECUTING
                # both carry a lease. Fail closed rather than assume.
                stranded.append(task)
                seen.add(task.task_id)
                continue
            # THE CASE THE SEQUENCE NUMBER CANNOT DECIDE. The lease has not
            # lapsed and never will, because the log stopped advancing when
            # its holder died. Ask the operating system instead.
            if liveness(ProcessIdentity.from_record(
                    task.lease.holder_process)) is GONE:
                stranded.append(task)
                seen.add(task.task_id)

        actions: list = []
        for task in stranded:
            ran = self._execution_record(task.task_id)
            lapsed = task.lease is None or not task.lease.is_live(head)
            proc = ProcessIdentity.from_record(
                task.lease.holder_process) if task.lease else None
            if lapsed and task.lease is not None:
                basis = ("its lease lapsed at seq "
                         f"{task.lease.expires_after_seq}")
            elif lapsed:
                basis = "it holds no lease record at all"
            else:
                pid = proc.pid if proc else "?"
                basis = (
                    f"the process that took it (pid {pid}) no longer exists "
                    "on this boot, so the sequence-number expiry it was "
                    "waiting for can never arrive -- the log that would "
                    "advance it stopped when that process did")
            why = (
                f"the supervisor holding lease "
                f"{task.lease.lease_id if task.lease else 'none'!r} did not "
                f"finish and {basis}; recovered from durable state at seq "
                f"{head}. ")
            why += (
                f"An execution record exists ({ran['outcome']}), so the tool "
                "DID run -- it is evidence of the attempt and not authority "
                "to complete the task under a lease nobody holds."
                if ran else
                "No execution record exists, so nothing observed the tool "
                "run at all.")
            moved = self._move(task, TaskState.QUEUED, actor,
                               TaskRole.SYSTEM, note=why)
            actions.append({
                "task_id": task.task_id, "from": task.state.value,
                "to": moved.state.value, "had_execution_record": bool(ran),
                "lease_lapsed_by_seq": lapsed, "at_seq": head,
                "holder_process": task.lease.holder_process
                if task.lease else None,
                "reason": why,
            })
        return tuple(actions)

    def sweep_orphans(self, *, actor: str = "system") -> tuple:
        """Terminate children a dead supervisor left running. Report the rest.

        The gap this closes said the recorded pid was "diagnostic, not a
        handle: nothing automatically signals an orphan on the next
        supervisor's behalf". It could not have been a handle before,
        because a bare pid cannot say whether the number still belongs to
        the process that was started -- so acting on it would eventually
        signal an innocent program that inherited it.

        With the child's boot id and start time in the execution record,
        the question is answerable, and the answer gates the signal:

          the SUPERVISOR must be GONE    otherwise the run is still live and
                                         this is not an orphan, it is
                                         somebody else's work
          the CHILD must be ALIVE        the same process, on the same boot,
                                         with the same start time
          then, and only then            terminate its process group

        Anything short of both answers is REPORTED and left alone. UNKNOWN
        is not permission: a record from another host says nothing about
        what is running here.
        """
        head = self._head_seq()
        projection = self.projection()
        acted: list = []
        for task in sorted(projection.tasks.values(),
                           key=lambda t: t.task_id):
            if task.state not in _RECOVERABLE:
                continue
            holder = ProcessIdentity.from_record(
                task.lease.holder_process) if task.lease else None
            lapsed = task.lease is None or not task.lease.is_live(head)
            if not lapsed and liveness(holder) is not GONE:
                continue                      # the supervisor may still work
            ran = self._execution_record(task.task_id)
            child = ProcessIdentity.from_record(
                (ran or {}).get("child_process"))
            state = liveness(child)
            if state is not ALIVE:
                acted.append({"task_id": task.task_id, "action": "REPORTED",
                              "child": (ran or {}).get("child_process"),
                              "child_liveness": state,
                              "why": "not provably the process that was "
                                     "started, so nothing here may signal it"})
                continue
            killed = _terminate_group(child)
            acted.append({"task_id": task.task_id, "action": "TERMINATED",
                          "child": child.to_record(), "child_liveness": ALIVE,
                          "signalled": killed,
                          "why": "its supervisor is gone and this is the same "
                                 "process that supervisor started"})
        return tuple(acted)

    def awaiting_verification(self) -> tuple:
        """Tasks that finished and have nobody to check them.

        Reported rather than resolved. This is the honest shape of a crash
        between COMPLETED and VERIFIED: the work is done, the verdict is
        not, and only a different actor may give it.
        """
        return tuple(t.task_id for t in
                     self.projection().in_state(TaskState.COMPLETED))

    def _execution_record(self, task_id: str):
        """The last task.execution payload for a task, or None."""
        found = None
        for ev in self.log.read():
            if ev.action == ACT_EXECUTION and \
                    ev.payload.get("task_id") == task_id:
                found = ev.payload
        return found

    # ---- helpers -------------------------------------------------------
    def _report_duplicate(self, prior, spec) -> GovernedRun:
        """Answer a resubmission from durable state. Execute nothing.

        The prior task's CURRENT state is what gets reported, read from the
        projection rather than from anything the caller supplied -- so a
        resubmission of work that has since failed reports the failure, and
        one of work still running reports that it is still running.

        THE EXTERNAL CASE IS NOT RESOLVED HERE, IT IS DECLARED

        If the first attempt reached a tool declaring SideEffect.EXTERNAL
        and has not reached a terminal state, nobody knows whether the
        external effect happened: the supervisor may have died between the
        effect and the record of it. Re-running would repeat it and
        reporting success would invent a fact. The honest answer is that the
        outcome is UNCERTAIN, said plainly, with the tool's own compensating
        action quoted so an operator has something to act on. That is a
        limit of doing this locally; exactly-once needs the far side to
        participate.
        """
        task = self.projection().get(prior.task_id)
        state = task.state if task is not None else TaskState.CREATED
        artifacts = self._artifacts_of(prior.task_id)
        job_state = ""
        if prior.job_id:
            job = self.scheduler.all_jobs().get(prior.job_id)
            job_state = job.state.value if job is not None else ""

        # SETTLED means "we know the tool's process ran to completion", not
        # "the task stopped moving". Only VERIFIED, REJECTED and INVALIDATED
        # are reachable through COMPLETED, so only those three prove the
        # execution finished. FAILED, TIMED_OUT and CANCELLED do NOT: a
        # timeout is precisely the case where nothing observed the tool
        # finish, which is the same case where an external effect may have
        # happened unrecorded.
        settled = state in EXTERNAL_SETTLED
        external = spec.side_effect is SideEffect.EXTERNAL
        escalation_id = ""
        if external and not settled:
            outcome = "UNCERTAIN"
            # A DECISION A PERSON MUST MAKE, raised as one.
            #
            # The row used to say the governed path raises no escalation
            # "because nothing in a Stage-10 artifact run is a decision a
            # human must make". That was true of the successful path and
            # false of this one. Right here the system knows something it
            # cannot resolve: an external effect may or may not have
            # happened, re-running would repeat it, reporting success would
            # invent a fact, and the tool's compensation is a DECLARATION
            # that nothing here is authorized to perform. Somebody has to
            # decide whether to compensate, and it is not this process.
            #
            # Best-effort: raising it must not turn an already-honest
            # UNCERTAIN report into a crash, so a registry that refuses (an
            # unregistered submitter, say) leaves the report intact and the
            # escalation absent rather than losing both.
            escalation_id = f"esc-{prior.task_id[5:]}-external"
            try:
                if not any(e.escalation_id == escalation_id
                           for e in self.agents.open_escalations(
                               task_id=prior.task_id)):
                    self.agents.escalate(
                        escalation_id=escalation_id, task_id=prior.task_id,
                        raised_by=prior.owner,
                        question=(
                            f"task {prior.task_id} ran a tool declaring "
                            "EXTERNAL side effects and did not reach a "
                            "settled state. Whether the effect happened is "
                            "not knowable from here. Its declared "
                            f"compensating action is: {spec.compensation}. "
                            "Compensate, or accept the effect as done?"),
                        options=("COMPENSATE", "ACCEPT_AS_DONE",
                                 "INVESTIGATE_EXTERNALLY"))
            except AgentError:
                escalation_id = ""
            reason = (
                f"resubmission of {prior.task_id}, which is still "
                f"{state.value} and ran a tool declaring EXTERNAL "
                "side effects. Whether the "
                "external effect happened is NOT KNOWN here: the first "
                "attempt may have performed it and died before recording "
                "that. Nothing was re-run, because re-running would perform "
                "it again, and nothing is being reported as successful, "
                "because nothing observed it succeed. The tool's declared "
                f"compensating action is: {spec.compensation}")
        else:
            outcome = "DUPLICATE"
            reason = (
                f"resubmission of {prior.task_id} under idempotency key "
                f"{prior.key!r}; the request is identical, so the first "
                f"submission's state ({state.value}) is what is reported and "
                "no work was started")

        return GovernedRun(
            prior.task_id, state, outcome, "", artifacts,
            self._head_seq(), reason,
            job_id=prior.job_id, job_state=job_state,
            idempotency_key=prior.key, duplicate_of=prior.task_id,
            escalation_id=escalation_id)

    def _artifacts_of(self, task_id: str) -> dict:
        """Artifacts recorded for a task, from the log rather than a cache."""
        found: dict = {}
        for ev in self.log.read():
            if ev.action == ACT_EVIDENCE and ev.payload.get("task_id") == \
                    task_id:
                found = dict(ev.payload.get("artifacts", {}))
        return found

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
            at_seq=self._head_seq())
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
        at = self._head_seq() + 1
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

    def _check_declared_outputs(self, result, artifacts: dict) -> str:
        """Agreement between the executor's digests and the capture's.

        The executor hashed each declared output the moment the process
        ended. ``_capture`` hashed what was on disk when it swept the
        directory. Same file, two readers, two moments -- so this compares a
        claim against an independent observation rather than a value against
        itself, and the only thing it can catch is the file having changed in
        between, which is the only thing worth catching here.

        Returns an empty string when they agree, or the disagreement.
        """
        for name, dg in sorted(result.output_digests.items()):
            rel = result.output_paths.get(name, name)
            found = artifacts.get(rel)
            if found is None:
                # A declared output the executor hashed and the sweep did not
                # see: it was removed, or moved, after the run ended.
                return (f"declared output {name!r} ({rel}) was hashed "
                        f"at {dg[:12]}... when the tool finished and is not "
                        "among the captured artifacts; it did not survive "
                        "to the capture")
            if found != dg:
                return (f"declared output {name!r} ({rel}) hashed "
                        f"{dg[:12]}... when the tool finished and "
                        f"{found[:12]}... when it was "
                        "captured; the bytes changed between the run and the "
                        "capture, so neither digest describes a stable "
                        "artifact")
        # A file the contract never named. The write allowlist bounded WHERE
        # the tool could write and said nothing about WHAT: an extra artifact
        # inside its own scope passed every check and appeared in no
        # provenance record.
        #
        # This is EVIDENCE_FAILED rather than a warning because the point of
        # the contract is that the run's outputs are the ones somebody
        # reviewed. A run that quietly leaves something else behind has a
        # provenance record that is incomplete, and an incomplete provenance
        # record is precisely what this subsystem exists to prevent.
        if result.undeclared_writes:
            return (f"the tool wrote {len(result.undeclared_writes)} file(s) "
                    f"its contract never declared: "
                    f"{list(result.undeclared_writes[:5])}. The writable "
                    "scope bounds WHERE a tool may write and says nothing "
                    "about WHAT; a run whose outputs are not the ones the "
                    "contract named has a provenance record that is missing "
                    "something")
        return ""

    def _reexecute_and_compare(self, *, tool_id: str, inputs: dict,
                               declared: dict, verifier: str,
                               task_id: str) -> tuple:
        """RUN THE TOOL AGAIN and compare, for a tool that claims to be
        byte-identical.

        WHAT _verify_artifacts DOES NOT ASK

        It re-derives each digest from disk through a governed read, which
        answers "are the bytes the task cited still the bytes on disk". That
        is a question about the FILE. It is not a question about the RESULT:
        the same wrong bytes hash to the same wrong digest every time, so a
        tool that produced the wrong thing passes it perfectly.

        The only stronger question available without a second implementation
        is whether the tool, given the same declared inputs, produces the
        same bytes again -- and it is only available for tools that DECLARE
        byte-identical determinism. For a NONDETERMINISTIC tool a difference
        proves nothing and this is skipped and says so, because a check that
        cannot distinguish tampering from an unproven assumption failing is
        a check that gets switched off after its first false alarm.

        The re-run writes into a scratch directory of its own. Re-running
        over the artifact would destroy the thing being verified and would
        make the comparison a comparison with itself.

        THE SCRATCH DIRECTORY COMES FROM THE TASK'S OWN INPUTS. It was read
        from an attribute on this object, which only the test fixture ever
        set: the production caller reached this line and raised
        ``AttributeError``, so the re-execution check existed for tests and
        for nothing else. That is this repository's recurring defect -- a
        field populated by nothing -- appearing inside the code that was
        written to close a gap about verification being too weak.
        """
        spec = self.registry.get(tool_id)
        if spec.determinism is not Determinism.BYTE_IDENTICAL:
            return True, (f"{tool_id} declares {spec.determinism.value}; "
                          "re-execution would compare bytes nobody promised "
                          "would match")
        import shutil

        out_dir = inputs.get("out_dir")
        if not isinstance(out_dir, str) or not out_dir:
            return False, ("the task declares no out_dir, so there is nowhere "
                           "to re-execute into that is not the artifact "
                           "itself")
        scratch_rel = f"{out_dir}/.reverify-{task_id[-8:]}"
        scratch = self.root / scratch_rel
        try:
            scratch.mkdir(parents=True, exist_ok=True)
            again = dict(inputs)
            again["out_dir"] = scratch_rel
            argv = self._argv(tool_id, again)
            # THE VERIFIER MINTS ITS OWN GRANT, and does not borrow the
            # worker's. A grant is not a bearer token -- the ledger refuses
            # one used by anybody but its subject, which is how this was
            # found -- and reusing the worker's would make "may do the work"
            # and "may check the work" the same authority, so a component
            # tricked into one would hold the other.
            verify_cap = self._reverification_capability(
                task_id, verifier, tool_id)
            fresh = self.capabilities.in_force(self._head_seq())
            with socket_guard(self.network, actor=verifier, task_id=task_id,
                              tool_id=tool_id):
                result = self.executor.run(
                    tool_id=tool_id, actor=verifier, task_id=task_id,
                    capability_id=verify_cap, capabilities=fresh,
                    inputs=again, argv=argv, cwd=self.root,
                    limits=Limits(wall_seconds=spec.timeout_s),
                    env=self._tool_environment())
            if result.outcome is not Outcome.COMPLETED:
                return False, (f"re-execution of {tool_id} did not complete "
                               f"({result.outcome.value}): {result.reason}")
            # READ THROUGH THE CONFINEMENT PRIMITIVE, not with read_bytes.
            #
            # These files were written by the very tool this check exists to
            # be harder to fool than. A tool that plants a symlink in its own
            # output directory would otherwise have the VERIFIER read
            # whatever it points at -- an arbitrary file, or a device that
            # never ends -- and report the digest under the artifact's name.
            # ReadRoot binds the read to a descriptor on the scratch
            # directory, refuses symlink components and non-regular files,
            # and bounds the size.
            produced = {}
            try:
                with ReadRoot(scratch) as root:
                    for dirpath, _dirnames, names in os.walk(
                            scratch, followlinks=False):
                        for name in sorted(names):
                            rel = (Path(dirpath) / name).relative_to(
                                scratch).as_posix()
                            produced[Path(rel).name] = root.read(
                                rel, require_unique_link=False).digest
            except SafeIOError as exc:
                return False, (
                    "re-execution wrote something the verifier will not "
                    f"read: {exc}")
            # The tool's DECLARED outputs, not the whole capture. The
            # capture sweeps the output directory, which in a graph holds
            # the previous step's artifacts too -- and a tool cannot be
            # expected to reproduce files it never claimed to write.
            cited = {Path(rel).name: dg for rel, dg in declared.items()}
            # AN EMPTY COMPARISON IS NOT AGREEMENT. Without this, a run that
            # declared no output files reaches the loop below, iterates over
            # nothing and returns "0 artifact(s) reproduced byte-for-byte" --
            # the same sentence a real comparison produces. A verifier that
            # reports success over zero items is worse than no verifier,
            # because the report reads identically either way, and this
            # repository has shipped that shape once already.
            if not cited:
                return False, (
                    "the run declared no output files, so re-execution has "
                    "nothing to compare; agreement over an empty comparison "
                    "is not agreement")
            # DELIBERATELY REDUNDANT with the per-name check below, and kept
            # for the diagnostic: "produced no files at all" and "did not
            # reproduce a.json" are different things to be told. A mutation
            # of this line cannot be killed independently, which is the
            # honest description of a refinement rather than a guard.
            if not produced:
                return False, ("re-execution produced no files, so there is "
                               "nothing to compare against the artifacts "
                               "this run cited")
            for name, dg in sorted(cited.items()):
                if name not in produced:
                    return False, (f"re-execution did not reproduce {name}, "
                                   "which the run cited as an artifact")
                if produced[name] != dg:
                    return False, (
                        f"{name} re-executed to {produced[name][:12]}... and "
                        f"the run cited {dg[:12]}...; a tool declared "
                        "BYTE_IDENTICAL produced different bytes from the "
                        "same declared inputs")
            self.log.append(
                actor=verifier, action=ACT_REEXECUTION, target=task_id,
                payload={"task_id": task_id, "tool_id": tool_id,
                         "compared": sorted(cited),
                         "determinism": spec.determinism.value})
            return True, (f"{len(cited)} artifact(s) reproduced byte-for-byte "
                          "by re-running the tool from its declared inputs")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _verify_artifacts(self, artifacts: dict, *,
                          verifier: str = VERIFIER_ID,
                          task_id: str = "",
                          capability_id: str = "") -> tuple:
        """Re-derive each digest from disk, through a GOVERNED read.

        Deliberately not "does the evidence store still hold that digest" --
        that would confirm the store's own bookkeeping. The question is
        whether the bytes the task cited are still the bytes on disk, which is
        what a later reader following the provenance would want to know.

        WHY THIS READ IN PARTICULAR IS GOVERNED

        It used to be ``path.is_file()`` and then ``path.read_bytes()``. That
        is the shape this whole subsystem exists to remove: between the two
        calls the name can become a symlink pointing anywhere, or a FIFO,
        which would have hung verification indefinitely rather than failing
        it. And this is the verification step -- the one place whose whole
        job is to be harder to fool than the code that produced the result.

        Reading through :class:`~qta_agent.readpath.GovernedReader` binds the
        read to an inode, refuses symlinks and special files, checks a
        capability the verifier actually holds, records every attempt, and
        passes the cited digest as the expectation so the identity of the
        bytes is checked by the same call that fetches them.
        """
        if not artifacts:
            return False, ("the run produced no artifacts; a completion with "
                           "nothing to point at is not verifiable")
        caps = self.capabilities.in_force(self._head_seq())
        with GovernedReader(self.log, root_id=READ_ROOT_ID,
                            root_path=self.root, capabilities=caps) as reader:
            for rel, dg in sorted(artifacts.items()):
                req = ReadRequest(actor=verifier, task_id=task_id,
                                  root_id=READ_ROOT_ID, resource=rel,
                                  purpose="verification")
                try:
                    reader.read(req, capability_id=capability_id,
                                expect_digest=dg,
                                require_unique_link=True)
                except FileNotFoundError:
                    return False, f"cited artifact {rel} is no longer on disk"
                except SourceChanged:
                    return False, (
                        f"artifact {rel} no longer hashes to the digest the "
                        "task cited")
                except ReadDenied as exc:
                    return False, (
                        f"the verifier is not authorized to read {rel}: "
                        f"{exc}")
                except (SafeIOError, OSError) as exc:
                    return False, (
                        f"cited artifact {rel} could not be read safely: "
                        f"{exc}")
                if not self.evidence.contains(dg):
                    return False, (
                        f"artifact {rel} is not resolvable as evidence")
        return True, (f"{len(artifacts)} artifact(s) re-derived from disk "
                      "through the governed read boundary and resolvable in "
                      "the evidence store")
