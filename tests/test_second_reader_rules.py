"""What a capture is, who the second reader asks about each record, and the
rules it had stated more widely than the primaries (AUD-062 to AUD-067).

AUD-062. An admission asks for a report captured by a governed verification
task before its verdict. Both readers took any ``task.evidence`` record
naming the task as a capture, from anyone, at any time before the verdict:
one record by another principal made a report no check produced read as
the check's, and the store admitted a result on it. A capture is the
runner's, made while the task executes.

AUD-063. Given an authenticator, the second reader refused the records whose
writers it does not attest, and decided admissions on a task replay, and
research proofs on a subsystem replay, that read the history without one.

AUD-064. The directory admits a HUMAN by the bootstrap or by an active human;
the second reader also took one admitted by a name the log never admitted,
or by a retired human, and that human's answer to an escalation.

AUD-065. A transition naming a starting state the replay does not have is
refused by the store and the task projection; the second reader judged it
from the state it had and applied it.

AUD-066. The store counts a policy version that is an integer; the second
reader counted any, and a bound promotion stood on a policy the store says
was never published.

AUD-067. An allowed read under a delegated capability whose ancestor was
revoked is one the read path refuses; the second reader asked only of the
capability the read cited.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import agents as A  # noqa: E402
from agent_substrate import capability as C  # noqa: E402
from agent_substrate import governed_tasks  # noqa: E402
from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_model import (  # noqa: E402
    VERIFIER_TOOLS, GovernedModelRuns, ModelRunRefused,
)
from agent_substrate.governed_tasks import (  # noqa: E402
    CAPTURER, SUBMITTER_ID, VERIFIER_ID, WORKER_ID, GovernedTasks,
)
from agent_substrate.policy import Effect, document, rule  # noqa: E402
from agent_substrate.separate_verify import (  # noqa: E402
    SeparateVerification,
)
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402
from agent_substrate.tasks import TaskTransitionError  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from governed_case import CHECK, MODEL, PARAMS  # noqa: E402

DIG = "d" * 64


# ============================================= a history written by hand
# A scientific result admitted under admission/1 on a governed run and a
# governed check, every record signed unless a test says otherwise. Only
# the second reader is asked of it.

class _Store(dict):
    def put(self, doc) -> str:
        b = json.dumps(doc, sort_keys=True).encode()
        sha = hashlib.sha256(b).hexdigest()
        self[sha] = b
        return sha


IMPL, VIMPL, D0 = "a" * 64, "b" * 64, "c" * 64
_BUNDLE = {"artifacts": [], "convergence": {}, "environment_digest": D0,
           "implementation_digest": IMPL,
           "invariants": [{"name": "energy", "holds": True}],
           "model_id": "thermal.slab_transient", "model_version": "1",
           "observation_kind": "SIMULATION_RESULT", "outputs": [],
           "parameter_digest": D0, "parameters": {}, "provenance": {},
           "schema_version": "1", "seeds": [], "solver_config": {},
           "warnings": []}


def _report(**over) -> dict:
    r = {"check_id": "chk", "check_type": "INDEPENDENT_IMPLEMENTATION",
         "criterion": "agree", "criterion_derivation": "stated",
         "establishes": "INDEPENDENT_NUMERICAL_AGREEMENT", "evidence": [],
         "independence": "SEPARATE_CODE", "limitations": [], "measured": {},
         "observations": [], "producer_implementation_digest": IMPL,
         "shared_components": [], "status": "PASS",
         "subject_digest": rc._sci_json_digest(_BUNDLE),
         "subject_model": "thermal.slab_transient", "threshold": 0.1,
         "verifier_id": "checker", "verifier_implementation_digest": VIMPL}
    r.update(over)
    return r


_NAMES = ("submitter", "scheduler", "checker", "runner", "verifier2",
          CAPTURER, "proposer", "reviewer", "mallory")
_WHO = {n: pr.test_identity(n) for n in _NAMES}
_KEYS = pr.KeyRegistry([s.registered() for s in _WHO.values()],
                       allow_test_keys=True)


class _World:
    def __init__(self, tmp: Path):
        self.ev = _Store()
        self.bundle = self.ev.put(_BUNDLE)
        self.report = self.ev.put(_report())
        self.fake = self.ev.put(_report(limitations=["none"]))
        self.log = EventLog(tmp / "log.jsonl")
        self.att = pr.Attestations(tmp / "log.attestations.jsonl")

    def put(self, actor, action, target, payload, signed=True):
        if signed:
            return pr.signed_append(self.log, self.att, _WHO[actor],
                                    registry=_KEYS, action=action,
                                    target=target, payload=payload)
        return self.log.append(actor=actor, action=action, target=target,
                               payload=payload)

    def move(self, actor, tid, src, dst, role, signed=True, **extra):
        self.put(actor, "task.transition", tid,
                 {"task_id": tid, "src": src, "dst": dst, "role": role,
                  **extra}, signed=signed)

    def task(self, tid, tool, executor, captured, *, verdict_signed=True,
             while_completed=None, while_executing=None):
        self.put("submitter", "task.create", tid,
                 {"task_id": tid, "tool_id": tool, "submitter": "submitter",
                  "inputs_digest": D0, "depends_on": []})
        self.move("scheduler", tid, "CREATED", "VALIDATED", "SCHEDULER")
        self.move("scheduler", tid, "VALIDATED", "QUEUED", "SCHEDULER")
        n = self.log.verify().head_seq + 1
        lease = {"lease_id": f"L-{tid}", "holder": executor,
                 "granted_seq": n, "expires_after_seq": n + 1000}
        self.move("scheduler", tid, "QUEUED", "LEASED", "SCHEDULER",
                  lease=lease)
        self.move(executor, tid, "LEASED", "EXECUTING", "WORKER",
                  lease_id=lease["lease_id"])
        self.put(executor, "task.execution", tid,
                 {"task_id": tid, "result_digest": D0})
        if while_executing:
            while_executing(self, tid)
        self.put(CAPTURER, "task.evidence", tid,
                 {"task_id": tid, "artifacts": {f"{tid}/out": captured}})
        self.move(executor, tid, "EXECUTING", "COMPLETED", "WORKER",
                  lease_id=lease["lease_id"], result_digest=D0)
        if while_completed:
            while_completed(self, tid)
        self.move("verifier2", tid, "COMPLETED", "VERIFIED", "VERIFIER",
                  signed=verdict_signed)

    def decided(self, report):
        base = {"record_id": "sr", "policy_id": None, "stale_reason": None,
                "edge_reason": "x", "idempotency_key": None}
        self.put("proposer", "record.create", "sr",
                 {"record_id": "sr", "kind": "scientific_result",
                  "proposer": "proposer",
                  "evidence": {"result_bundle": self.bundle},
                  "depends_on": [], "policy_id": None})
        self.put("reviewer", "record.transition", "sr",
                 {**base, "src": "PROPOSED", "dst": "UNDER_REVIEW",
                  "role": "VERIFIER", "evidence": {}})
        self.put("reviewer", "record.transition", "sr",
                 {**base, "src": "UNDER_REVIEW", "dst": "VERIFIED",
                  "role": "VERIFIER",
                  "evidence": {"verification_report": report},
                  "admission_policy": "scientific_result.admission/1"})
        return self

    def second(self, authenticated=False):
        auth = pr.Authenticator(self.att, _KEYS) if authenticated else None
        return rc.reconstruct(EventLog(self.log.path), evidence=self.ev,
                              authenticator=auth)


def _world(tmp_path, **chk) -> _World:
    w = _World(tmp_path)
    w.task("run", "model.thermal.slab_transient.run", "runner", w.bundle)
    w.task("chk", "model.independent_check", "checker", w.report, **chk)
    return w


def _admission(r) -> tuple:
    rec = r.records.get("sr") or {}
    return rec.get("state"), rec.get("admission")


def test_the_history_written_by_hand_is_admitted_as_written(tmp_path):
    """The control the forgeries below differ from."""
    w = _world(tmp_path)
    w.decided(w.report)
    assert _admission(w.second()) == ("VERIFIED", "ADMITTED")
    assert _admission(w.second(authenticated=True)) == (
        "VERIFIED", "ADMITTED")


# ------------------------------------------ AUD-062: what a capture is
def _launder(actor):
    def go(w, tid):
        w.put(actor, "task.evidence", tid,
              {"task_id": tid, "artifacts": {"x/report.json": w.fake}})
    return go


@pytest.mark.parametrize("when, actor", [
    ("while_completed", "mallory"),       # another principal, before the verdict
    ("while_executing", "mallory"),       # another principal, while it runs
    ("while_completed", CAPTURER),        # the runner's name, after the run
])
def test_a_report_attached_by_anyone_but_the_runner_is_no_capture(
        tmp_path, when, actor):
    w = _world(tmp_path, **{when: _launder(actor)})
    w.decided(w.fake)
    for authenticated in (False, True):
        r = w.second(authenticated)
        assert _admission(r) == ("UNDER_REVIEW", None), r.unauthorized
        assert any("captured by no governed verification" in u
                   for u in r.unauthorized), r.unauthorized
    t = rc.reconstruct_tasks(EventLog(w.log.path))
    assert [d for _, d in t.tasks["chk"]["captured"]] == [w.report]
    assert any("recorded by the runner" in u for u in t.unauthorized)


def test_the_runners_capture_while_it_executes_still_counts(tmp_path):
    w = _world(tmp_path, while_executing=_launder(CAPTURER))
    w.decided(w.fake)
    assert _admission(w.second()) == ("VERIFIED", "ADMITTED")


def test_a_proof_names_the_task_that_captured_the_bundle(tmp_path):
    """The decision proof's account of who ran the work is read from the
    capture of the bundle: the last record naming it, by anyone, until
    now. Mallory's naming it for the check made the check its producer."""
    w = _world(tmp_path, while_completed=lambda w, tid: w.put(
        "mallory", "task.evidence", tid,
        {"task_id": tid, "artifacts": {"x/bundle.json": w.bundle}}))
    w.decided(w.report)
    seq = next(e.seq for e in EventLog(w.log.path).read_verified()[1]
               if e.action == "record.transition"
               and e.payload.get("dst") == "VERIFIED")
    proof = rc.derive_decision_proof(EventLog(w.log.path), seq,
                                     evidence=w.ev)
    assert proof["execution"]["task_id"] == "run"
    assert proof["execution"]["executor"] == "runner"


# ------------------------------------- AUD-063: authenticated throughout
def test_an_admission_does_not_stand_on_a_verdict_nobody_attested(tmp_path):
    w = _world(tmp_path, verdict_signed=False)
    w.decided(w.report)
    assert _admission(w.second()) == ("VERIFIED", "ADMITTED"), \
        "a legacy history read without an authenticator reads as written"
    r = w.second(authenticated=True)
    assert any("unauthenticated" in u and "verifier2" in u
               for u in r.unauthorized), r.unauthorized
    assert _admission(r) == ("UNDER_REVIEW", None)


def test_the_task_and_subsystem_readers_refuse_what_they_cannot_attest(
        tmp_path):
    w = _World(tmp_path)
    w.put("submitter", "task.create", "t",
          {"task_id": "t", "tool_id": "x", "submitter": "submitter",
           "inputs_digest": D0, "depends_on": []})
    w.put("scheduler", "task.transition", "t",
          {"task_id": "t", "src": "CREATED", "dst": "VALIDATED",
           "role": "SCHEDULER"}, signed=False)
    auth = pr.Authenticator(w.att, _KEYS)
    assert rc.reconstruct_tasks(EventLog(w.log.path)).states() == {
        "t": "VALIDATED"}
    t = rc.reconstruct_tasks(EventLog(w.log.path), authenticator=auth)
    assert t.states() == {"t": "CREATED"}
    assert any("unauthenticated" in u for u in t.unauthorized)
    w.put("scheduler", "capability.root", "root", {"issuer": "scheduler"},
          signed=False)
    s = rc.reconstruct_subsystems(EventLog(w.log.path), authenticator=auth)
    assert s.root_issuer is None
    assert any("unauthenticated" in a for a in s.anomalies)
    assert rc.reconstruct_subsystems(
        EventLog(w.log.path)).root_issuer == "scheduler"


def test_a_research_proof_reads_its_subsystems_under_the_authenticator(
        tmp_path, monkeypatch):
    """The research proof's two subsystem replays are handed the
    authenticator its record replay is: they were handed none."""
    w = _World(tmp_path)
    w.put("reviewer", "research.status", "h1",
          {"target_id": "h1", "status": "SUPPORTED", "basis": []})
    seen = []
    real = rc.reconstruct_subsystems

    def spy(log, **kw):
        seen.append(kw.get("authenticator"))
        return real(log, **kw)

    monkeypatch.setattr(rc, "reconstruct_subsystems", spy)
    rc.derive_decision_proof(EventLog(w.log.path), 0,
                             authenticator=pr.Authenticator(w.att, _KEYS))
    # each call enters twice, once through _never_raising
    assert len(seen) >= 2 and all(a is not None for a in seen), seen


# ------------------------------------------- AUD-064: who admits a human
def _escalated(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    d = A.AgentDirectory(log).load()
    for name in ("h1", "h2"):
        d.register(A.identity(agent_id=name, instance_id=name,
                              kind=A.PrincipalKind.HUMAN,
                              roles={A.AgentRole.REVIEWER}), by=A.BOOTSTRAP)
    d.register(A.identity(agent_id="bot", instance_id="bot",
                          kind=A.PrincipalKind.AGENT,
                          roles={A.AgentRole.PROPOSER}), by="system")
    d.escalate(escalation_id="e1", task_id="t1", question="ship it?",
               raised_by="bot", options=("yes", "no"))
    return log, d


def _mint_and_answer(log, registrar):
    fake = A.identity(agent_id="fake", instance_id="fake",
                      kind=A.PrincipalKind.HUMAN,
                      roles={A.AgentRole.REVIEWER})
    log.append(actor=registrar, action=A.ACT_AGENT_REGISTER, target="fake",
               payload={"identity": fake.to_record()})
    log.append(actor="fake", action=A.ACT_ESCALATION_ANSWER, target="t1",
               payload={"escalation_id": "e1", "state": "ANSWERED",
                        "answer": "yes", "answered_by": "fake",
                        "reason": "r"})


@pytest.mark.parametrize("registrar, why", [
    ("mallory", "no principal this log admitted"),
    ("h2", "a retired human"),
])
def test_a_human_admitted_by_neither_the_bootstrap_nor_an_active_human(
        tmp_path, registrar, why):
    log, d = _escalated(tmp_path)
    if registrar == "h2":
        d.retire("h2", by="h2", reason="left")
    _mint_and_answer(log, registrar)
    with pytest.raises(A.IdentityError, match="may not register a HUMAN"):
        A.AgentDirectory(EventLog(log.path)).load()
    s = rc.reconstruct_subsystems(EventLog(log.path))
    assert "fake" not in s.agents
    assert any(why in a for a in s.anomalies), s.anomalies
    assert s.escalations["e1"]["state"] == "OPEN"


def test_a_human_admitted_by_an_active_human_still_answers(tmp_path):
    log, _ = _escalated(tmp_path)
    _mint_and_answer(log, "h1")
    assert A.AgentDirectory(EventLog(log.path)).load().escalation(
        "e1").state is A.EscalationState.ANSWERED
    s = rc.reconstruct_subsystems(EventLog(log.path))
    assert s.escalations["e1"]["answered_by"] == "fake"


# --------------------------------------- AUD-065: where a move starts
def _forged_move(log, rid, src, dst, actor, role, evidence, policy_id=None):
    payload = {"record_id": rid, "dst": dst, "role": role,
               "evidence": evidence, "policy_id": policy_id,
               "stale_reason": None, "edge_reason": "forged",
               "idempotency_key": None}
    if src is not ...:
        payload["src"] = src
    log.append(actor=actor, action="record.transition", target=rid,
               payload=payload)


def test_a_record_moved_from_where_it_is_not_is_refused_by_both(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    AuthorityStore(log).load().create(record_id="r1", kind="result",
                                      proposer="alice")
    _forged_move(log, "r1", "VERIFIED", "UNDER_REVIEW", "bob", "VERIFIER",
                 {})
    with pytest.raises(StoreError, match="moves it from"):
        AuthorityStore(EventLog(log.path)).load()
    r = rc.reconstruct(EventLog(log.path))
    assert r.records["r1"]["state"] == "PROPOSED"
    assert any("where the replay does not have it" in u
               for u in r.unauthorized), r.unauthorized


def test_a_record_naming_no_starting_state_is_judged_as_the_store_does(
        tmp_path):
    """The store asks a claimed src only when there is one."""
    log = EventLog(tmp_path / "log.jsonl")
    AuthorityStore(log).load().create(record_id="r1", kind="result",
                                      proposer="alice")
    _forged_move(log, "r1", ..., "UNDER_REVIEW", "bob", "VERIFIER", {})
    assert AuthorityStore(EventLog(log.path)).load().get(
        "r1").state is State.UNDER_REVIEW
    assert rc.reconstruct(EventLog(log.path)).records["r1"]["state"] == \
        "UNDER_REVIEW"


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / "verification/stage10/_pytest_second_rules" / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedTasks(root=ROOT, log=EventLog(base / "log.jsonl"),
                      evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"verification/stage10/_pytest_second_rules/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _run(gov, **over):
    kw = dict(tool_id="stage10.emit_artifact",
              inputs={"out_dir": gov.out_rel, "name": "a.json",
                      "payload": {"v": 1}},
              submitter=SUBMITTER_ID, worker=WORKER_ID, verifier=VERIFIER_ID)
    kw.update(over)
    return gov.run(**kw)


@pytest.mark.parametrize("src", ["COMPLETED", None])
def test_a_task_moved_from_where_it_is_not_is_refused(gov, src):
    """VERIFIED -> INVALIDATED is an edge, the system's. A record claiming
    to leave COMPLETED, or naming no state it leaves, was applied by the
    second reader from where the task was; the projection refuses both,
    and says so of the first in these words."""
    run = _run(gov)
    payload = {"task_id": run.task_id, "dst": "INVALIDATED",
               "role": "SYSTEM"}
    if src is not None:
        payload["src"] = src
    gov.log.append(actor="system", action="task.transition",
                   target=run.task_id, payload=payload)
    if src is not None:
        with pytest.raises(TaskTransitionError, match="moves it from"):
            gov.projection()
    t = rc.reconstruct_tasks(gov.log)
    assert t.states()[run.task_id] == "VERIFIED", t.unauthorized
    assert any("where the replay does not have it" in u
               for u in t.unauthorized), t.unauthorized


# ----------------------------------- AUD-066: a version is an integer
def _bound(tmp_path, version):
    log = EventLog(tmp_path / "log.jsonl")
    doc = document(policy_id="promote", version=1, rules=(rule(
        rule_id="r1", effect=Effect.ALLOW, actions=("record.promote",),
        subjects=("*",), roles=("*",), resources=("*",)),))
    body = doc.body()
    body["version"] = version
    log.append(actor="owner", action="policy.publish", target="promote",
               payload={"document": body, "policy_digest": doc.digest()})
    s = AuthorityStore(log).load()
    s.bind_policies(actor="owner", publishers=("owner",))
    s.create(record_id="r1", kind="result", proposer="alice",
             policy_id="promote")
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="bob",
                 role=Role.VERIFIER)
    s.transition(record_id="r1", dst=State.VERIFIED, actor="bob",
                 role=Role.VERIFIER, evidence={"verification_report": DIG})
    _forged_move(log, "r1", "VERIFIED", "PROMOTED", "carol", "PROMOTER",
                 {"policy_id": "promote"}, "promote")
    return log


@pytest.mark.parametrize("version", ["1", 1.0, None])
def test_a_promotion_bound_to_a_version_that_is_no_integer_is_refused(
        tmp_path, version):
    log = _bound(tmp_path, version)
    with pytest.raises(StoreError, match="was not published"):
        AuthorityStore(EventLog(log.path)).load()
    r = rc.reconstruct(EventLog(log.path))
    assert r.records["r1"]["state"] == "VERIFIED"
    assert r.canonical_ids() == ()
    assert any("not counted as published" in a for a in r.anomalies)


def test_a_promotion_bound_to_an_integer_version_stands(tmp_path):
    log = _bound(tmp_path, 1)
    assert AuthorityStore(EventLog(log.path)).load().get(
        "r1").state is State.PROMOTED
    assert rc.reconstruct(EventLog(log.path)).canonical_ids() == ("r1",)


# -------------------------------- AUD-067: a read under a revoked line
def _read(log, cid):
    log.append(actor="bob", action="file.read", target="t1",
               payload={"request": {"actor": "bob", "root_id": "ws",
                                    "resource": "data/x/y",
                                    "task_id": "t1", "tool_id": ""},
                        "allowed": True, "result": {"digest": DIG},
                        "capability_id": cid})


def _delegated(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    L = C.CapabilityLedger(log).load()
    L.issue(C.issue(capability_id="c1", subject="alice",
                    action=C.Action.READ_PATHS, task_id="t1",
                    scope=("ws/data",), issued_seq=0), actor="root")
    L.delegate(C.issue(capability_id="c2", subject="bob",
                       action=C.Action.READ_PATHS, task_id="t1",
                       scope=("ws/data/x",), issued_seq=0),
               parent_id="c1", actor="alice")
    return log, L


def _request():
    return C.Request(actor="bob", action=C.Action.READ_PATHS, task_id="t1",
                     paths=("ws/data/x/y",))


def test_a_read_under_a_child_of_a_revoked_grant_is_noted(tmp_path):
    log, L = _delegated(tmp_path)
    L.revoke("c1", actor="root", reason="withdrawn")
    with pytest.raises(C.CapabilityRevoked):
        C.CapabilityLedger(EventLog(log.path)).load().in_force().check(
            "c2", _request())
    _read(log, "c2")
    s = rc.reconstruct_subsystems(EventLog(log.path))
    assert s.reads == 0
    assert any("derives from 'c1', which was revoked" in a
               for a in s.anomalies), s.anomalies


def test_a_read_under_a_live_line_still_counts(tmp_path):
    log, _ = _delegated(tmp_path)
    _read(log, "c2")
    s = rc.reconstruct_subsystems(EventLog(log.path))
    assert s.reads == 1, s.anomalies


# ================================== AUD-062 in the primary, governed
WS = "verification/stage10/_pytest_capture_rule"


def _governed_world(base: Path, when: str, *, actor: str = "mallory",
                    separate: bool = True):
    """A governed run and its check, with ``actor`` attaching a report it
    wrote to the check task before the verdict: ``when`` is after the
    check's separate verification, as the task completes, or while it
    executes. ``separate=False`` sets the check's separate verification
    aside, to hold a rule of the store's that it would otherwise reach
    first."""
    if base.exists():
        shutil.rmtree(base)
    (base / "w").mkdir(parents=True)
    log = EventLog(base / "w" / "log.jsonl")
    ev = EvidenceStore(base / "w" / "evidence")
    g = GovernedModelRuns(root=ROOT, log=log, evidence=ev)
    held: dict = {}
    decided, append = log.append_decided, log.append

    def is_check(tid) -> bool:
        task = g.gov.projection().get(tid)
        return task is not None and task.tool_id in VERIFIER_TOOLS

    def forge(tid, execution=None) -> None:
        if execution is not None:
            # While the task executes: the report is on disk, where the
            # check wrote it, and not yet captured.
            rel = next(v for v in execution["output_paths"].values()
                       if v.endswith("verification.json"))
            genuine = json.loads((ROOT / rel).read_bytes())
        else:
            arts = {}
            for x in log.read_verified()[1]:
                if x.action == "task.evidence" and x.actor == CAPTURER and \
                        x.payload.get("task_id") == tid:
                    arts = x.payload["artifacts"]
            rel = next(k for k in arts if k.endswith("verification.json"))
            genuine = json.loads(ev.get(arts[rel]))
        fake = copy.deepcopy(genuine)
        for _, q in fake.get("agreement") or []:
            q["resolution"] = q["resolution"] / 1000.0
        held["fake"] = ev.put(json.dumps(fake, sort_keys=True).encode(),
                              media_type="application/json")
        append(actor=actor, action="task.evidence", target=tid,
               payload={"task_id": tid,
                        "artifacts": {f"{actor}/verification.json":
                                      held["fake"]}})

    def hooked_decided(decide, **kw):
        e = decided(decide, **kw)
        p = e.payload
        if when == "completed" and e.action == "task.transition" and \
                p.get("dst") == "COMPLETED" and "fake" not in held and \
                is_check(p.get("task_id", e.target)):
            forge(p.get("task_id", e.target))
        if when == "executing" and e.action == "task.execution" and \
                "fake" not in held and is_check(p.get("task_id")):
            forge(p.get("task_id"), execution=p)
        return e

    def hooked_append(**kw):
        e = append(**kw)
        if when == "after-separate" and "fake" not in held and \
                kw.get("action") == "task.separate_verification" and \
                is_check(kw["payload"]["task_id"]):
            forge(kw["payload"]["task_id"])
        return e

    log.append_decided, log.append = hooked_decided, hooked_append
    real = governed_tasks.verify_in_separate_process
    if not separate:
        governed_tasks.verify_in_separate_process = lambda *a, **k: \
            SeparateVerification(ok=True, exit_status=0,
                                 reason="set aside by this case")
    try:
        run = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/w/run")
        try:
            chk = g.check(run, check_id=CHECK, out_dir=f"{WS}/w/check")
        except ModelRunRefused as exc:
            chk = exc
    finally:
        log.append_decided, log.append = decided, append
        governed_tasks.verify_in_separate_process = real
    return run, chk, held.get("fake")


@pytest.fixture(scope="module")
def laundered():
    base = ROOT / WS
    run, chk, fake = _governed_world(base, "after-separate")
    yield base, run, chk, fake
    if base.exists():
        shutil.rmtree(base)


def _copy_world(base: Path, name: str) -> Path:
    dst = base / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir()
    shutil.copytree(base / "w" / "evidence", dst / "evidence")
    shutil.copy(base / "w" / "log.jsonl", dst / "log.jsonl")
    return dst


def _governed(d: Path) -> GovernedModelRuns:
    return GovernedModelRuns(root=ROOT, log=EventLog(d / "log.jsonl"),
                             evidence=EvidenceStore(d / "evidence"))


def test_the_store_admits_nothing_on_a_report_no_check_produced(laundered):
    base, run, chk, fake = laundered
    d = _copy_world(base, "fake")
    rec = _governed(d).decide(run, dataclasses.replace(
        chk, report_sha256=fake))
    assert rec.state is State.REJECTED
    assert _governed(d).authority.get(run.record_id).state is State.REJECTED
    r = rc.reconstruct(EventLog(d / "log.jsonl"),
                       evidence=EvidenceStore(d / "evidence"))
    assert r.records[run.record_id]["state"] == "REJECTED"


def test_the_genuine_report_is_still_admitted_beside_it(laundered):
    base, run, chk, _ = laundered
    d = _copy_world(base, "genuine")
    assert _governed(d).decide(run, chk).state is State.VERIFIED
    r = rc.reconstruct(EventLog(d / "log.jsonl"),
                       evidence=EvidenceStore(d / "evidence"))
    assert r.records[run.record_id]["admission"] == "ADMITTED"


def test_a_duplicate_is_answered_with_the_runners_capture(gov):
    first = _run(gov, idempotency_key="k1")
    gov.log.append(actor="mallory", action="task.evidence",
                   target=first.task_id,
                   payload={"task_id": first.task_id,
                            "artifacts": {"x/a.json": DIG}})
    again = _run(gov, idempotency_key="k1")
    assert again.duplicate_of == first.task_id
    assert again.artifacts == first.artifacts


def _refused_on(base: Path, run, chk, fake) -> None:
    """The store, asked to admit the result on ``fake``, refuses it, and
    holds the refusal on replay."""
    assert fake is not None
    assert not isinstance(chk, ModelRunRefused), chk
    g = _governed(base / "w")
    rec = g.decide(run, dataclasses.replace(chk, report_sha256=fake))
    assert rec.state is State.REJECTED
    assert _governed(base / "w").authority.get(run.record_id).state \
        is State.REJECTED


def test_the_runners_name_outside_the_execution_is_no_capture():
    """Under the runner's name -- which an unauthenticated log lets any
    writer use -- after the check's separate verification: a record no
    execution made. Refused for that alone: the actor is the runner's."""
    base = ROOT / (WS + "_runner_name")
    try:
        _refused_on(base, *_governed_world(base, "after-separate",
                                           actor=CAPTURER))
    finally:
        if base.exists():
            shutil.rmtree(base)


def test_anothers_record_while_the_task_executes_is_no_capture():
    """Another principal's record, made while the check task executes.
    The check's separate verification refuses such a run before its
    verdict (below); set aside, the store's own rule still refuses the
    result: what is captured is the runner's, whenever it is recorded."""
    base = ROOT / (WS + "_executing")
    try:
        _refused_on(base, *_governed_world(base, "executing",
                                           separate=False))
    finally:
        if base.exists():
            shutil.rmtree(base)


def test_a_check_whose_history_holds_anothers_capture_is_refused():
    """Attached before the check's separate verification, the record is
    one that verification finds, and the run is REJECTED: a check that
    stood on it would stand on a history its own reader refuses."""
    base = ROOT / (WS + "_early")
    try:
        _, chk, fake = _governed_world(base, "completed")
        assert fake is not None
        assert isinstance(chk, ModelRunRefused), chk
        assert "recorded by the runner" in str(chk)
    finally:
        if base.exists():
            shutil.rmtree(base)
