"""Independent reconstruction of durable state from the event log alone.

This module exists to answer one question without trusting the running
system: *given only the log, what is canonical?*

It is written as a SECOND implementation on purpose. It does not import
:class:`~qta_agent.store.AuthorityStore` and does not share its reducer. If
both agree, that is differential evidence -- two implementations reading the
same evidence reached the same verdict. Reusing the store's reducer here would
make the comparison circular and worthless, which is why the duplication is
deliberate rather than an oversight.

Where the store folds events into dataclasses through ``dataclasses.replace``,
this walks the log with plain dictionaries and re-derives each field from
scratch. The two disagree loudly if either has a bug.

It trusts NOTHING except the log's bytes:
  * not the live process,
  * not any snapshot,
  * not the store's projection,
  * not conversation history or a model's recollection.

Every transition is re-authorized against the state machine during replay. An
event that the machine would refuse today is reported rather than applied --
which is how a log written by a compromised or older writer, or under a since
changed policy, becomes visible instead of being silently absorbed.

TWO MACHINES, THE SAME TREATMENT

:func:`reconstruct` covers authority records. :func:`reconstruct_tasks` covers
the task lifecycle, and it exists for a reason that is not symmetry: the task
projection is the one on the PRODUCTION path, and it is the one that turned
out to re-authorize forged records against a starting state the record itself
declared. A second implementation is the defence against that class -- not
because the second one is more careful, but because two readers that disagree
say so, and a single reader with a hole says nothing at all.

The two replays differ in what they do about a refusal, and deliberately.
``governed_stage10.projection`` is ENFORCEMENT: it raises, because a reader
that cannot tell which records went through the gate must not hand back a
state. This module is DIAGNOSIS: it records the problem and keeps going, so
one bad record does not hide the twenty after it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import actions
from .authority import (
    INITIAL,
    Role,
    State,
    TransitionError,
    TransitionRequest,
    check,
)
from .events import EventLog
from .tasks import (
    INITIAL as TASK_INITIAL,
)
from .tasks import (
    TERMINAL as TASK_TERMINAL,
)
from .tasks import (
    Lease,
    Task,
    TaskRole,
    TaskState,
    TaskTransition,
    TaskTransitionError,
)
from .tasks import (
    check as task_check,
)


@dataclass
class Reconstruction:
    """The verdict, plus everything needed to argue with it."""
    #: record_id -> plain dict of reconstructed fields
    records: dict = field(default_factory=dict)
    #: Transitions the state machine would refuse if replayed today.
    unauthorized: list = field(default_factory=list)
    #: Structural problems in the log that did not stop replay.
    anomalies: list = field(default_factory=list)
    events_replayed: int = 0
    #: Events belonging to another subsystem on the same log. Counted so a
    #: reader can tell "this reconstruction saw a mixed log and ignored the
    #: parts that are not authority records" from "this log had 3 events".
    foreign_events: int = 0
    head_seq: int = -1
    head_hash: str = ""

    def canonical_ids(self) -> tuple:
        return tuple(sorted(
            rid for rid, r in self.records.items()
            if r["state"] == State.PROMOTED.value))

    def states(self) -> dict:
        return {rid: r["state"] for rid, r in self.records.items()}


#: Actions this function interprets. Everything else this package writes is
#: another subsystem's and is counted rather than treated as damage.
_AUTHORITY_ACTIONS = frozenset({"record.create", "record.transition",
                                "record.depend"})


def reconstruct(log: EventLog, *, reauthorize: bool = True) -> Reconstruction:
    """Rebuild authority state from a verified log.

    Verification comes first and is fatal: reconstructing from a chain that
    does not verify would produce a confident answer from untrusted bytes.
    """
    report = log.verify()
    report.raise_if_bad()

    out = Reconstruction(head_seq=report.head_seq, head_hash=report.head_hash)
    # Deliberately dict-of-dicts rather than the store's dataclasses.
    recs: dict = out.records

    for ev in log.read():
        out.events_replayed += 1
        p = ev.payload
        action = ev.action
        rid = p.get("record_id")

        if action == "record.create":
            if rid in recs:
                out.anomalies.append(
                    f"seq {ev.seq}: duplicate create for {rid!r}")
                continue
            recs[rid] = {
                "record_id": rid,
                "kind": p.get("kind"),
                "proposer": p.get("proposer"),
                "state": p.get("state", INITIAL.value),
                "revision": 1,
                "evidence": dict(p.get("evidence", {})),
                "depends_on": list(p.get("depends_on", [])),
                "policy_id": p.get("policy_id"),
                "created_seq": ev.seq,
                "updated_seq": ev.seq,
                "stale_reason": None,
                "history": [(ev.seq, p.get("state", INITIAL.value))],
            }

        elif action == "record.transition":
            cur = recs.get(rid)
            if cur is None:
                out.anomalies.append(
                    f"seq {ev.seq}: transition for unknown record {rid!r}")
                continue
            src_claimed = p.get("src")
            if cur["state"] != src_claimed:
                out.anomalies.append(
                    f"seq {ev.seq}: {rid} claims src {src_claimed} but replay "
                    f"has it in {cur['state']}")
            if reauthorize:
                try:
                    check(TransitionRequest(
                        record_id=rid,
                        src=State(cur["state"]),
                        dst=State(p["dst"]),
                        actor=ev.actor,
                        role=Role(p["role"]),
                        evidence={**cur["evidence"], **p.get("evidence", {})},
                        proposer=cur["proposer"],
                        policy_id=p.get("policy_id") or cur["policy_id"]))
                except (TransitionError, ValueError) as exc:
                    out.unauthorized.append(
                        f"seq {ev.seq}: {rid} {cur['state']} -> "
                        f"{p.get('dst')} "
                        f"would be refused today: {exc}")
                    # Do NOT apply. An unauthorized transition must not become
                    # canonical merely because it is present in the log.
                    continue
            cur["state"] = p["dst"]
            cur["revision"] += 1
            cur["evidence"].update(p.get("evidence", {}))
            cur["updated_seq"] = ev.seq
            if p.get("stale_reason") is not None:
                cur["stale_reason"] = p["stale_reason"]
            if p.get("policy_id") is not None:
                cur["policy_id"] = p["policy_id"]
            cur["history"].append((ev.seq, p["dst"]))

        elif action == "record.depend":
            cur = recs.get(rid)
            if cur is None:
                out.anomalies.append(
                    f"seq {ev.seq}: dependency for unknown record {rid!r}")
                continue
            for dep in p.get("depends_on", []):
                if dep not in cur["depends_on"]:
                    cur["depends_on"].append(dep)
            cur["revision"] += 1
            cur["updated_seq"] = ev.seq

        elif actions.classify(action, mine=_AUTHORITY_ACTIONS) \
                == actions.FOREIGN:
            # Another subsystem's event. Counted, not applied, and NOT an
            # anomaly: the authority records this function rebuilds are not
            # affected by it. What IS an anomaly is the case below.
            out.foreign_events += 1
        else:
            out.anomalies.append(
                f"seq {ev.seq}: unknown action {action!r}; not applied. "
                "Nothing in this package writes it, so this reconstruction "
                "is missing whatever it recorded.")
    return out


@dataclass(frozen=True)
class Divergence:
    """A disagreement between the live projection and the reconstruction."""
    record_id: str
    field_name: str
    live: object
    reconstructed: object

    def __str__(self) -> str:
        return (f"{self.record_id}.{self.field_name}: live={self.live!r} "
                f"reconstructed={self.reconstructed!r}")


@dataclass
class TaskReconstruction:
    """The task lifecycle as a second reader sees it."""
    #: task_id -> plain dict of reconstructed fields
    tasks: dict = field(default_factory=dict)
    #: (owner, tool_id, key) -> plain dict of binding fields.
    #:
    #: Keyed by the TUPLE, deliberately, rather than by the digest
    #: IdempotencyLedger uses for the same scope. Sharing that digest would
    #: make the two readers agree by construction about the one thing worth
    #: checking independently -- whether two submissions occupy the same
    #: namespace. A collision or a mis-derived scope on either side shows up
    #: here as a disagreement instead of being reproduced faithfully.
    bindings: dict = field(default_factory=dict)
    #: Transitions the machine would refuse if replayed today.
    unauthorized: list = field(default_factory=list)
    #: Structural problems that did not stop replay.
    anomalies: list = field(default_factory=list)
    events_replayed: int = 0
    foreign_events: int = 0
    head_seq: int = -1
    head_hash: str = ""

    def states(self) -> dict:
        return {tid: t["state"] for tid, t in sorted(self.tasks.items())}

    def verified_ids(self) -> tuple:
        return tuple(sorted(tid for tid, t in self.tasks.items()
                            if t["state"] == TaskState.VERIFIED.value))


def reconstruct_tasks(log: EventLog, *,
                      reauthorize: bool = True) -> TaskReconstruction:
    """Replay the task lifecycle from the log alone. Never raises on content.

    A SECOND implementation, in plain dictionaries, sharing no reducer with
    ``governed_stage10.projection``. See the module docstring for why that
    duplication is the point rather than an oversight.
    """
    report = log.verify()
    report.raise_if_bad()
    out = TaskReconstruction(head_seq=report.head_seq,
                             head_hash=report.head_hash)
    tasks: dict = {}
    bindings: dict = {}
    owned = {"task.create", "task.transition", "task.execution",
             "task.evidence", "idempotency.bind"}

    for ev in log.read():
        out.events_replayed += 1
        action = ev.action
        if action not in owned:
            kind = actions.classify(action, mine=owned)
            if kind == actions.UNKNOWN:
                out.anomalies.append(
                    f"seq {ev.seq}: unknown action {action!r}; no module in "
                    "this package writes it, so this reconstruction cannot "
                    "say what it meant")
            else:
                out.foreign_events += 1
            continue
        p = ev.payload

        if action == "idempotency.bind":
            _replay_binding(ev, p, bindings, out)
            continue

        tid = p.get("task_id", ev.target)

        if action == "task.create":
            if tid in tasks:
                out.anomalies.append(
                    f"seq {ev.seq}: task {tid!r} created twice; the second "
                    "record would silently replace the first one's history")
                continue
            tasks[tid] = {
                "task_id": tid, "tool_id": p.get("tool_id"),
                "submitter": p.get("submitter"),
                "inputs_digest": p.get("inputs_digest"),
                "state": TASK_INITIAL.value, "revision": 1,
                "executed_by": None, "result_digest": None,
                "lease": None, "depends_on": list(p.get("depends_on") or ()),
                "created_seq": ev.seq, "updated_seq": ev.seq,
                "artifacts": {}, "history": [(ev.seq, TASK_INITIAL.value)],
            }
            continue

        cur = tasks.get(tid)
        if cur is None:
            out.anomalies.append(
                f"seq {ev.seq}: {action} for unknown task {tid!r}; nothing "
                "records what was being asked for")
            continue

        if action == "task.evidence":
            arts = p.get("artifacts") or {}
            cur["artifacts"].update(arts)
            continue
        if action == "task.execution":
            cur["executed_by"] = ev.actor
            cur["result_digest"] = p.get("result_digest")
            continue

        # task.transition
        claimed = p.get("src")
        if cur["state"] != claimed:
            out.anomalies.append(
                f"seq {ev.seq}: {tid} claims src {claimed} but replay has it "
                f"in {cur['state']}")
        # The same question about the OTHER field the record gets to name.
        # Who executed the task decides who is allowed to verify it, and only
        # a task.execution record establishes it. A transition may repeat
        # that answer; it may not supply one, and it may not change it.
        claimed_by = p.get("executed_by")
        if claimed_by is not None and claimed_by != cur["executed_by"]:
            out.anomalies.append(
                f"seq {ev.seq}: {tid} names {claimed_by!r} as its executor, "
                f"but replay has {cur['executed_by']!r}; the executor comes "
                "from the execution record, so this record is naming the "
                "actor that verification has to differ from")
        lease = None
        if p.get("lease"):
            try:
                lease = Lease(**p["lease"])
            except TypeError:
                out.anomalies.append(
                    f"seq {ev.seq}: {tid} carries a lease record this build "
                    "cannot interpret")
        if reauthorize:
            # From the state THIS replay reached, never from the claim. A
            # forger who names a convenient src would otherwise have every
            # pair in the table available, which is exactly the defect the
            # production projection had.
            probe = Task(
                task_id=tid, tool_id=cur["tool_id"] or "",
                submitter=cur["submitter"] or "",
                inputs_digest=cur["inputs_digest"] or "",
                state=TaskState(cur["state"]), revision=cur["revision"],
                lease=_lease_of(cur) or lease,
                executed_by=cur["executed_by"],
                result_digest=cur["result_digest"])
            try:
                task_check(TaskTransition(
                    task_id=tid, src=TaskState(cur["state"]),
                    dst=TaskState(p["dst"]), actor=ev.actor,
                    role=TaskRole(p["role"]), at_seq=ev.seq,
                    lease_id=(p.get("lease_id")
                              or (probe.lease.lease_id if probe.lease
                                  else None)),
                    executed_by=cur["executed_by"],
                    result_digest=p.get("result_digest")), probe)
            except (TaskTransitionError, ValueError, KeyError) as exc:
                out.unauthorized.append(
                    f"seq {ev.seq}: {tid} {cur['state']} -> {p.get('dst')} "
                    f"would be refused today: {exc}")
                # Do NOT apply. Presence in the log is not authority.
                continue

        # The lease follows the task, not the record. Only the move INTO
        # LEASED carries one; the moves that need it afterwards cite its id
        # and rely on the task still holding it. Replacing the lease on every
        # transition drops it at the next step and then refuses the
        # completion -- which is what this replay did until the differential
        # test compared it against the live projection and disagreed.
        dst = TaskState(p["dst"])
        if dst is TaskState.LEASED:
            cur["lease"] = dict(p["lease"]) if p.get("lease") else None
        elif dst is TaskState.QUEUED or dst in TASK_TERMINAL:
            # Requeued or finished work holds nothing: a lease that outlives
            # the work it owned is a lease somebody else has to wait out.
            cur["lease"] = None
        cur["state"] = p["dst"]
        cur["revision"] += 1
        cur["updated_seq"] = ev.seq
        if p.get("result_digest") is not None:
            cur["result_digest"] = p["result_digest"]
        # cur["executed_by"] is NOT updated here. It was, and that single
        # line put this reader back underneath the bypass the production
        # projection had already been fixed for: the reauthorization above
        # correctly used the replayed executor, and then the payload
        # overwrote it in time for the NEXT transition to be checked against
        # the forger's choice.
        cur["history"].append((ev.seq, p["dst"]))

    out.tasks = tasks
    out.bindings = bindings
    return out


@dataclass
class SubsystemReconstruction:
    """Authority state of the subsystems that had no second reader.

    WHY THESE ARE HERE AND NOT IN THEIR OWN MODULES

    Because a second reader that lives beside the first, imports the
    first's enums and calls the first's helpers is not a second reader --
    it is the same decision run twice, agreeing with itself. This module
    sits BELOW scheduler, policy, capability, agents, memory, netauth,
    secrets and context in the declared layering, so it cannot import any
    of them even by accident. Everything below is plain strings and plain
    dicts, and every rule is restated rather than called.

    That restatement is the point and also the cost: two implementations
    can still share a misunderstanding the log cannot reveal, which is why
    an empty diff is evidence rather than proof.
    """

    jobs: dict = field(default_factory=dict)
    policies: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    net_grants: dict = field(default_factory=dict)
    secret_grants: dict = field(default_factory=dict)
    contexts: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
    events_replayed: int = 0
    head_seq: int = -1


#: Job states a job may be BORN in. Restated here rather than imported: a
#: forged enqueue naming SUCCEEDED or DISPATCHED is the attack, and a second
#: reader that asks the scheduler what counts as initial would inherit the
#: scheduler's answer along with any mistake in it.
_JOB_INITIAL = {"WAITING", "READY"}

#: Terminal job states. A transition out of one is a revival.
_JOB_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def reconstruct_subsystems(log: EventLog) -> SubsystemReconstruction:
    """Replay every remaining authority subsystem, independently.

    Never raises on content: a hostile history produces findings, not an
    exception that hides the rest of the log.
    """
    report = log.verify()
    report.raise_if_bad()
    out = SubsystemReconstruction(head_seq=report.head_seq)
    for ev in log.read():
        out.events_replayed += 1
        p = ev.payload if isinstance(ev.payload, dict) else {}
        a = ev.action
        if a == "scheduler.enqueue":
            _sub_enqueue(ev, p, out)
        elif a == "scheduler.transition":
            _sub_job_transition(ev, p, out)
        elif a == "scheduler.priority":
            _sub_priority(ev, p, out)
        elif a == "policy.publish":
            _sub_policy_publish(ev, p, out)
        elif a == "policy.decision":
            _sub_policy_decision(ev, p, out)
        elif a == "capability.issue":
            _sub_capability_issue(ev, p, out)
        elif a == "capability.revoke":
            _sub_capability_revoke(ev, p, out)
        elif a == "agent.register":
            _sub_agent_register(ev, p, out)
        elif a == "agent.retire":
            _sub_agent_retire(ev, p, out)
        elif a == "memory.write":
            _sub_memory_write(ev, p, out)
        elif a == "memory.status":
            _sub_memory_status(ev, p, out)
        elif a == "network.grant":
            _sub_grant(ev, p, out, out.net_grants, "network")
        elif a == "secret.grant":
            _sub_grant(ev, p, out, out.secret_grants, "secret")
        elif a == "context.build":
            _sub_context(ev, p, out)
    return out


def _note(out, ev, text: str) -> None:
    out.anomalies.append(f"seq {ev.seq}: {text}")


def _sub_enqueue(ev, p: dict, out) -> None:
    job = p.get("job")
    if not isinstance(job, dict):
        _note(out, ev, "enqueue carries no job record")
        return
    jid = job.get("job_id")
    if not isinstance(jid, str) or not jid:
        _note(out, ev, "enqueue names no job_id")
        return
    if jid in out.jobs:
        _note(out, ev, f"job {jid!r} enqueued twice; the second would "
                       "replace the first one's state and history")
        return
    state = job.get("state")
    if state not in _JOB_INITIAL:
        # A create introduces WORK, never a verdict. A job born SUCCEEDED
        # was never run; one born DISPATCHED arrives holding the lease that
        # the ownership check on its outcome edges would otherwise demand.
        _note(out, ev, f"job {jid!r} is enqueued directly in {state!r}; an "
                       "enqueue introduces work, not an outcome")
        return
    if job.get("submitter") != ev.actor:
        _note(out, ev, f"job {jid!r} names submitter "
                       f"{job.get('submitter')!r} but was appended by "
                       f"{ev.actor!r}")
        return
    if job.get("attempts"):
        _note(out, ev, f"job {jid!r} is enqueued with "
                       f"{job.get('attempts')} attempts already spent")
        return
    if job.get("lease_holder") or job.get("lease_id"):
        _note(out, ev, f"job {jid!r} is enqueued already holding a lease")
        return
    out.jobs[jid] = {
        "job_id": jid, "state": state,
        "work_digest": job.get("work_digest"),
        "submitter": ev.actor, "priority": job.get("priority"),
        "attempts": job.get("attempts") or 0,
        "lease_holder": job.get("lease_holder") or "",
        "lease_expires_after_seq": job.get("lease_expires_after_seq", -1),
        "idempotency_key": job.get("idempotency_key"),
        "enqueued_seq": ev.seq,
    }


def _sub_job_transition(ev, p: dict, out) -> None:
    jid = p.get("job_id")
    cur = out.jobs.get(jid)
    if cur is None:
        _note(out, ev, f"transition for unknown job {jid!r}")
        return
    src, dst = p.get("src"), p.get("dst")
    if cur["state"] != src:
        _note(out, ev, f"job {jid!r} claims src {src!r} but replay has it "
                       f"in {cur['state']!r}")
        return
    if cur["state"] in _JOB_TERMINAL:
        _note(out, ev, f"job {jid!r} leaves terminal state {src!r}")
        return
    cur["state"] = dst
    if "lease_holder" in p:
        cur["lease_holder"] = p.get("lease_holder") or ""
    if "lease_expires_after_seq" in p:
        cur["lease_expires_after_seq"] = p.get("lease_expires_after_seq", -1)
    if "attempts" in p:
        cur["attempts"] = p.get("attempts")


def _sub_priority(ev, p: dict, out) -> None:
    jid = p.get("job_id")
    cur = out.jobs.get(jid)
    if cur is None:
        _note(out, ev, f"priority change for unknown job {jid!r}")
        return
    cur["priority"] = p.get("priority")


def _sub_policy_publish(ev, p: dict, out) -> None:
    doc = p.get("document")
    if not isinstance(doc, dict):
        _note(out, ev, "policy.publish carries no document")
        return
    pid = doc.get("policy_id")
    versions = out.policies.setdefault(pid, [])
    version = doc.get("version")
    if any(v["version"] == version for v in versions):
        _note(out, ev, f"policy {pid!r} publishes version {version!r} twice")
        return
    if versions and version is not None and \
            versions[-1]["version"] is not None and \
            version < versions[-1]["version"]:
        # A downgrade republished later would answer questions about the
        # intervening range with rules that were superseded.
        _note(out, ev, f"policy {pid!r} publishes version {version!r} after "
                       f"{versions[-1]['version']!r}")
        return
    versions.append({"version": version, "digest": p.get("policy_digest"),
                     "effective_seq": ev.seq})


def _sub_policy_decision(ev, p: dict, out) -> None:
    d = p.get("decision")
    if not isinstance(d, dict):
        _note(out, ev, "policy.decision carries no decision")
        return
    out.decisions[ev.seq] = {
        "allowed": d.get("allowed"), "policy_id": d.get("policy_id"),
        "policy_digest": d.get("policy_digest"), "actor": ev.actor,
        "subject": d.get("subject"), "action": d.get("action"),
    }


def _sub_capability_issue(ev, p: dict, out) -> None:
    cid = p.get("capability_id")
    if not isinstance(cid, str) or not cid:
        _note(out, ev, "capability.issue names no capability_id")
        return
    issued = p.get("issued_seq")
    prior = out.capabilities.get(cid)
    if prior is not None:
        if prior["body"] == {k: v for k, v in p.items()
                             if k != "task_id"}:
            return                          # a retried append of the same
        _note(out, ev, f"capability {cid!r} is issued twice with different "
                       "terms; two grants sharing an id cannot be told apart")
        return
    if issued != ev.seq:
        # WHERE a grant starts is the log's to say. One appended at seq 90
        # claiming seq 5 reads as authority in force for 5..89.
        _note(out, ev, f"capability {cid!r} claims issued_seq {issued!r} at "
                       f"seq {ev.seq}; it would predate its own record")
        return
    out.capabilities[cid] = {
        "capability_id": cid, "subject": p.get("subject"),
        "action": p.get("action"), "task_id": p.get("task_id"),
        "tool_id": p.get("tool_id"), "scope": tuple(p.get("scope") or ()),
        "issued_seq": issued,
        "expires_after_seq": p.get("expires_after_seq"),
        "revoked_seq": None,
        "body": {k: v for k, v in p.items() if k != "task_id"},
    }


def _sub_capability_revoke(ev, p: dict, out) -> None:
    cid = p.get("capability_id")
    cur = out.capabilities.get(cid)
    if cur is None:
        _note(out, ev, f"revoke for unknown capability {cid!r}")
        return
    if cur["revoked_seq"] is None:
        cur["revoked_seq"] = ev.seq


def _sub_agent_register(ev, p: dict, out) -> None:
    ident = p.get("identity")
    if not isinstance(ident, dict):
        _note(out, ev, "agent.register carries no identity")
        return
    iid = ident.get("instance_id")
    if not isinstance(iid, str) or not iid:
        _note(out, ev, "agent.register names no instance_id")
        return
    if iid in out.agents:
        _note(out, ev, f"instance {iid!r} registered twice")
        return
    kind = ident.get("kind")
    by = ev.actor
    registrar = out.agents.get(by)
    if kind == "HUMAN" and registrar is not None and \
            registrar.get("kind") != "HUMAN":
        # An agent that can mint a HUMAN is one step from answering its own
        # escalation, which is both halves of the human gate at once.
        _note(out, ev, f"{by!r} is not HUMAN and registers {iid!r} as HUMAN")
        return
    out.agents[iid] = {
        "instance_id": iid, "agent_id": ident.get("agent_id"),
        "kind": kind, "roles": tuple(sorted(ident.get("roles") or ())),
        "registered_by": by, "registered_seq": ev.seq, "retired_seq": None,
    }


def _sub_agent_retire(ev, p: dict, out) -> None:
    iid = p.get("instance_id")
    cur = out.agents.get(iid)
    if cur is None:
        _note(out, ev, f"retire for unknown instance {iid!r}")
        return
    if cur["retired_seq"] is None:
        cur["retired_seq"] = ev.seq


def _sub_memory_write(ev, p: dict, out) -> None:
    entry = p.get("entry")
    if not isinstance(entry, dict):
        _note(out, ev, "memory.write carries no entry; a record this reader "
                       "cannot read is refused rather than projected")
        return
    mid = entry.get("memory_id")
    if not isinstance(mid, str) or not mid:
        _note(out, ev, "memory.write names no memory_id")
        return
    if mid in out.memory:
        _note(out, ev, f"memory {mid!r} written twice")
        return
    if entry.get("author") != ev.actor:
        _note(out, ev, f"memory {mid!r} names author {entry.get('author')!r} "
                       f"but was appended by {ev.actor!r}")
        return
    out.memory[mid] = {"memory_id": mid, "author": ev.actor,
                       "status": entry.get("status") or "ACTIVE",
                       "written_seq": ev.seq}


def _sub_memory_status(ev, p: dict, out) -> None:
    mid = p.get("memory_id")
    cur = out.memory.get(mid)
    if cur is None:
        _note(out, ev, f"status change for unknown memory {mid!r}")
        return
    new = p.get("status")
    if cur["status"] == "RETRACTED" and new != "RETRACTED":
        # A withdrawn note that can be un-withdrawn is a note whose author
        # never really withdrew it.
        _note(out, ev, f"memory {mid!r} is un-retracted to {new!r}")
        return
    cur["status"] = new


def _sub_grant(ev, p: dict, out, table: dict, what: str) -> None:
    if p.get("revoke"):
        gid = p.get("grant_id")
        cur = table.get(gid)
        if cur is None:
            _note(out, ev, f"revoke for unknown {what} grant {gid!r}")
            return
        if cur["revoked_seq"] is None:
            cur["revoked_seq"] = ev.seq
        return
    grant = p.get("grant")
    if not isinstance(grant, dict):
        _note(out, ev, f"{what}.grant carries no grant body")
        return
    gid = grant.get("grant_id") or p.get("grant_id")
    if not isinstance(gid, str) or not gid:
        _note(out, ev, f"{what} grant names no grant_id")
        return
    prior = table.get(gid)
    if prior is not None:
        if prior["digest"] == p.get("grant_digest"):
            return                          # a retried append of the same
        _note(out, ev, f"{what} grant {gid!r} is re-issued with different "
                       "terms; the live grant would be replaced by one "
                       "nobody reviewed")
        return
    table[gid] = {"grant_id": gid, "digest": p.get("grant_digest"),
                  "issued_seq": ev.seq, "revoked_seq": None,
                  "body": grant}


def _sub_context(ev, p: dict, out) -> None:
    manifest = p.get("manifest")
    digest_ = p.get("manifest_digest")
    task = (manifest or {}).get("task_id") if isinstance(manifest, dict) \
        else None
    out.contexts.setdefault(task or ev.target, []).append(
        {"digest": digest_, "seq": ev.seq})


def compare_subsystems(primary: dict, recon: SubsystemReconstruction) -> tuple:
    """Divergences between a primary projection and the second reader.

    ``primary`` is a mapping of subsystem name -> {id: {field: value}},
    extracted by the caller from the live projections. The extraction is
    the caller's because reconstruct.py may not import those layers, which
    is what keeps the two readers independent in the first place.
    """
    out: list = []
    tables = {"jobs": recon.jobs, "capabilities": recon.capabilities,
              "agents": recon.agents, "memory": recon.memory,
              "net_grants": recon.net_grants,
              "secret_grants": recon.secret_grants}
    for name, theirs in tables.items():
        mine = primary.get(name)
        if mine is None:
            continue
        for key in sorted(set(mine) | set(theirs)):
            a, b = mine.get(key), theirs.get(key)
            if a is None:
                out.append(Divergence(f"{name}/{key}", "presence", None,
                                      "present in the second reader"))
                continue
            if b is None:
                out.append(Divergence(f"{name}/{key}", "presence",
                                      "present in the projection", None))
                continue
            for fld, want in sorted(a.items()):
                got = b.get(fld)
                if got != want:
                    out.append(Divergence(f"{name}/{key}", fld, want, got))
    return tuple(out)


def _replay_binding(ev, p: dict, bindings: dict, out) -> None:
    """Project one idempotency.bind, in this reader's own words.

    Shares no code with :class:`~qta_agent.idempotency.IdempotencyLedger`.
    The rules are restated rather than imported, because a second reader
    that calls the first one's reducer is not a second reader -- it is the
    same decision, run twice, agreeing with itself.

    Records anomalies rather than raising: this module's contract is that a
    hostile history produces findings, not an exception that hides the rest
    of the log.
    """
    if not isinstance(p, dict):
        out.anomalies.append(
            f"seq {ev.seq}: idempotency binding payload is not an object")
        return
    key, tool_id = p.get("key"), p.get("tool_id")
    task_id, request_digest = p.get("task_id"), p.get("request_digest")
    for name, value in (("key", key), ("tool_id", tool_id),
                        ("task_id", task_id),
                        ("request_digest", request_digest)):
        if not isinstance(value, str) or not value:
            out.anomalies.append(
                f"seq {ev.seq}: idempotency binding has no usable {name!r}")
            return
    # The owner is the EVENT'S actor. A payload naming its own owner chose
    # whose namespace to write into.
    claimed_owner = p.get("owner")
    if claimed_owner is not None and claimed_owner != ev.actor:
        out.anomalies.append(
            f"seq {ev.seq}: binding names owner {claimed_owner!r} but was "
            f"appended by {ev.actor!r}; a lookup answered for the claimed "
            "owner would hand one actor another's task")
        return
    claimed_seq = p.get("bound_seq")
    if claimed_seq is not None and claimed_seq != ev.seq:
        out.anomalies.append(
            f"seq {ev.seq}: binding claims bound_seq {claimed_seq!r}, which "
            "backdates the moment a duplicate would first have been caught")
        return

    scope = (ev.actor, tool_id, key)
    prior = bindings.get(scope)
    if prior is not None:
        if (prior["task_id"] == task_id
                and prior["request_digest"] == request_digest):
            return                       # a retried append of the same bind
        out.anomalies.append(
            f"seq {ev.seq}: idempotency key {key!r} for {tool_id!r} is "
            f"rebound from task {prior['task_id']!r} to {task_id!r}; every "
            "later resubmission of the original request would resolve to "
            "the new work")
        return
    bindings[scope] = {
        "key": key, "owner": ev.actor, "tool_id": tool_id,
        "task_id": task_id, "request_digest": request_digest,
        "job_id": p.get("job_id", ""), "bound_seq": ev.seq,
    }


def compare_bindings(ledger, recon) -> tuple:
    """Divergences between the ledger's bindings and the second reader's.

    The interesting direction is a binding one reader holds and the other
    does not: that is a namespace the two disagree about, and the ledger is
    what decides whether work re-runs.
    """
    out: list = []
    mine = {(b.owner, b.tool_id, b.key): b
            for b in ledger.bindings().values()}
    theirs = recon.bindings
    for scope in sorted(set(mine) | set(theirs)):
        a, b = mine.get(scope), theirs.get(scope)
        label = f"{scope[0]}/{scope[1]}/{scope[2]}"
        if a is None:
            out.append(Divergence(label, "binding", None, b["task_id"]))
            continue
        if b is None:
            out.append(Divergence(label, "binding", a.task_id, None))
            continue
        for fld, x, y in (("task_id", a.task_id, b["task_id"]),
                          ("request_digest", a.request_digest,
                           b["request_digest"]),
                          ("bound_seq", a.bound_seq, b["bound_seq"])):
            if x != y:
                out.append(Divergence(label, fld, x, y))
    return tuple(out)


def _lease_of(cur: dict):
    """Rebuild the lease this replay is currently holding, if any."""
    raw = cur.get("lease")
    if not raw:
        return None
    try:
        return Lease(**raw)
    except TypeError:                      # pragma: no cover - malformed
        return None


def compare_tasks(projection, recon: TaskReconstruction) -> tuple:
    """Diff the live task projection against the independent replay.

    Empty means two implementations reading the same bytes reached the same
    verdict. Anything else is a divergence one of them has to answer for.
    """
    diffs: list = []
    live = dict(projection.tasks)
    for tid in sorted(set(live) | set(recon.tasks)):
        if tid not in live:
            diffs.append(Divergence(tid, "<presence>", "ABSENT", "present"))
            continue
        if tid not in recon.tasks:
            diffs.append(Divergence(tid, "<presence>", "present", "ABSENT"))
            continue
        lv, rc = live[tid], recon.tasks[tid]
        for name, lval, rval in (
            ("state", lv.state.value, rc["state"]),
            ("tool_id", lv.tool_id, rc["tool_id"]),
            ("submitter", lv.submitter, rc["submitter"]),
            ("inputs_digest", lv.inputs_digest, rc["inputs_digest"]),
            ("executed_by", lv.executed_by, rc["executed_by"]),
            ("result_digest", lv.result_digest, rc["result_digest"]),
            # The lease is state, not decoration. A replay that keeps a
            # finished task's lease says the work is still owned by a worker
            # that has stopped, and nothing else in this diff would notice.
            ("lease", lv.lease.to_record() if lv.lease else None,
             rc["lease"]),
        ):
            if lval != rval:
                diffs.append(Divergence(tid, name, lval, rval))
    return tuple(diffs)


def compare(store, recon: Reconstruction) -> tuple:
    """Diff a live store against an independent reconstruction.

    Returns a tuple of :class:`Divergence`. Empty means the two implementations
    agree -- the only outcome that should ever occur in a healthy system.
    """
    diffs: list = []
    live = store.all_records()
    for rid in sorted(set(live) | set(recon.records)):
        if rid not in live:
            diffs.append(Divergence(rid, "<presence>", "ABSENT", "present"))
            continue
        if rid not in recon.records:
            diffs.append(Divergence(rid, "<presence>", "present", "ABSENT"))
            continue
        lv, rc = live[rid], recon.records[rid]
        for name, lval, rval in (
            ("state", lv.state.value, rc["state"]),
            ("kind", lv.kind, rc["kind"]),
            ("proposer", lv.proposer, rc["proposer"]),
            ("revision", lv.revision, rc["revision"]),
            ("evidence", dict(lv.evidence), rc["evidence"]),
            ("depends_on", list(lv.depends_on), rc["depends_on"]),
            ("policy_id", lv.policy_id, rc["policy_id"]),
        ):
            if lval != rval:
                diffs.append(Divergence(rid, name, lval, rval))
    return tuple(diffs)
