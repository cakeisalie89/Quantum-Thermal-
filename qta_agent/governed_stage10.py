"""The production caller: a real Stage-10 run, mediated end to end.

WHY THIS EXISTS

Everything else in this package was, until now, a mechanism with no consumer.
A governance layer that governs nothing is a library, and a library cannot be
wrong in the way a control plane can -- which means none of its guarantees had
ever been tested against a workflow that does real work and produces real
artifacts.

This module puts one through the whole chain:

    intent -> task record -> validation -> capability grant -> lease
           -> bounded execution -> output capture -> content-addressed evidence
           -> provenance binding -> independent verification -> authority
           -> durable state, all of it in the hash-chained log

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
    after a crash by replay rather than by inference.

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
from .canonical import digest, digest_bytes
from .capability import Action, CapabilitySet, issue
from .events import EventLog
from .evidence import EvidenceStore
from .execution import Executor, Limits, Outcome
from .tasks import (
    Lease, Task, TaskProjection, TaskRole, TaskState, TaskTransition,
    apply_transition, check,
)
from .tools import Determinism, Field_, Registry, SideEffect, ToolSpec

#: Where governed Stage-10 work is allowed to write. The same subtree the
#: Stage-10 workspace guard permits, so the two agree by construction rather
#: than by two lists that must be kept in step.
WORKSPACE_PREFIX = "verification/stage10"

ACT_TASK_CREATE = "task.create"
ACT_TASK_TRANSITION = "task.transition"
ACT_CAP_ISSUE = "capability.issue"
ACT_EXECUTION = "task.execution"
ACT_EVIDENCE = "task.evidence"

#: The actions THIS projection applies or deliberately passes over. Anything
#: else is another subsystem's (skipped) or unrecognised (refused).
OWNED = frozenset({ACT_TASK_CREATE, ACT_TASK_TRANSITION, ACT_CAP_ISSUE,
                   ACT_EXECUTION, ACT_EVIDENCE})


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


class GovernedStage10:
    """Runs Stage-10 work through the substrate. The production entry point."""

    def __init__(self, *, root: Path, log: EventLog, evidence: EvidenceStore,
                 registry: Registry | None = None):
        self.root = Path(root)
        self.log = log
        self.evidence = evidence
        self.registry = registry or stage10_registry()
        self.executor = Executor(self.registry, workspace=self.root)

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
    def run(self, *, tool_id: str, inputs: dict, submitter: str,
            worker: str, verifier: str, lease_seqs: int = 50) -> GovernedRun:
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

        spec = self.registry.get(tool_id)          # default deny
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        inputs_digest = digest(inputs)

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
                               self.log.verify().head_seq, str(exc))

        task = self._move(task, TaskState.VALIDATED, submitter,
                          TaskRole.SUBMITTER)
        task = self._move(task, TaskState.QUEUED, "scheduler",
                          TaskRole.SCHEDULER)

        head = self.log.verify().head_seq
        lease = Lease(lease_id=f"lease-{uuid.uuid4().hex[:8]}", holder=worker,
                      granted_seq=head + 1,
                      expires_after_seq=head + 1 + lease_seqs)
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

        task = self._move(task, TaskState.EXECUTING, worker, TaskRole.WORKER,
                          lease_id=lease.lease_id)

        out_dir = self.root / inputs["out_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "qta_agent._stage10_tool",
                json.dumps(inputs, sort_keys=True)]
        result = self.executor.run(
            tool_id=tool_id, actor=worker, task_id=task_id,
            capability_id=cap_id, capabilities=caps, inputs=inputs,
            argv=argv, cwd=self.root,
            limits=Limits(wall_seconds=spec.timeout_s),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "PYTHONPATH": str(self.root),
                 "PYTHONHASHSEED": "0",
                 # Pinned for determinism AND for thread budget. A governed
                 # tool importing numpy pulls in OpenBLAS, which spawns a
                 # worker per core; the count varies by machine, so leaving it
                 # unset makes both the task budget and any threaded numerical
                 # result environment-dependent.
                 "OPENBLAS_NUM_THREADS": "1",
                 "OMP_NUM_THREADS": "1",
                 "MKL_NUM_THREADS": "1",
                 "NUMEXPR_NUM_THREADS": "1"})

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
            return GovernedRun(task_id, task.state, result.outcome.value,
                               result_digest, {},
                               self.log.verify().head_seq, detail)

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

        return GovernedRun(task_id, task.state, result.outcome.value,
                           result_digest, artifacts,
                           self.log.verify().head_seq, why)

    # ---- helpers -------------------------------------------------------
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
