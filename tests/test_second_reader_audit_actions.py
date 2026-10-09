"""Ledger follow-up A: the audit trail, read a second time.

Seven durable actions had no independent reader: ``agent.message``,
``file.read``, ``network.result``, ``secret.access``, ``secret.provision``,
``task.reexecution`` and ``task.separate_verification`` (the ledger said
nine; ``agent.claim`` and ``task.compensation`` had readers since D-2026-29 --
D-2026-84). Each records what HAPPENED and changes nothing any reducer
permits, which the identity inventory says with a reason per action. This
module holds both halves of that:

* each has a second reader now, in ``qta_agent/reconstruct.py``: records
  its writer produced read clean (control), and a record its writer could
  not have produced is a finding, case by case;
* a forged record of each kind -- one every primary reader accepts, so it
  reaches the log's readers intact -- moves no authority-bearing view, and
  the second reader names it.
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

from qta_agent import reconstruct as rc  # noqa: E402
from qta_agent.agents import (  # noqa: E402
    AgentDirectory, AgentRole, PrincipalKind, identity,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import GovernedModelRuns  # noqa: E402
from qta_agent.netauth import (  # noqa: E402
    NetworkAuthority, NetworkRequest, grant as net_grant, parse_target,
)
from qta_agent.secrets import (  # noqa: E402
    MappingSecretProvider, SecretRef, SecretStore, grant as secret_grant,
)

WS = "verification/stage10/_pytest_audit_actions"
DIGEST = "ab" * 32
ACTOR, TASK, TOOL = "agent-worker-1", "task-1", "fetch.schema"


def _findings(log: EventLog) -> list:
    return (rc.reconstruct_subsystems(log).anomalies
            + rc.reconstruct_tasks(log).anomalies)


# ---- genuine histories ------------------------------------------------------

@pytest.fixture(scope="module")
def governed():
    """A governed history: tasks, capabilities, governed reads, re-executions
    and separate-process verifications, written by the real runner."""
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    (base / "genuine").mkdir(parents=True)
    g = GovernedModelRuns(root=ROOT,
                          log=EventLog(base / "genuine" / "log.jsonl"),
                          evidence=EvidenceStore(base / "genuine" / "ev"))
    g.propose(model_id="thermal.conduction_1d", model_version="1.0.0",
              parameters={"n_cells": 60, "n_eval": 20},
              out_dir=f"{WS}/genuine/run")
    yield base / "genuine"
    if base.exists():
        shutil.rmtree(base)


def _copy(governed: Path, name: str) -> EventLog:
    dst = governed.parent / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir()
    shutil.copy(governed / "log.jsonl", dst / "log.jsonl")
    return EventLog(dst / "log.jsonl")


def _last(log: EventLog, action: str):
    _, events = log.read_verified()
    return [e for e in events if e.action == action][-1]


@pytest.fixture()
def directory(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    d = AgentDirectory(log).load()
    for agent, iid, role in (("proposer", "p1", AgentRole.PROPOSER),
                             ("verifier", "v1", AgentRole.VERIFIER)):
        d.register(identity(agent_id=agent, instance_id=iid,
                            kind=PrincipalKind.AGENT, roles={role}),
                   by="system")
    d.send(message_id="m1", sender_instance="p1", recipient_agent="verifier",
           task_id="t1", subject="please check", body_digest=DIGEST)
    d.send(message_id="m2", sender_instance="v1", recipient_agent="proposer",
           task_id="t1", subject="checked", body_digest=DIGEST,
           in_reply_to="m1")
    d.send(message_id="m1", sender_instance="p1", recipient_agent="verifier",
           task_id="t1", subject="please check", body_digest=DIGEST)
    return log


@pytest.fixture()
def secrets(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    s = SecretStore(log)
    s.register("api-token", "hunter2-super-secret-token-value")
    s.issue(secret_grant(grant_id="sg1", subject=ACTOR, task_id=TASK,
                         tool_id=TOOL, secret_id="api-token",
                         purposes=("call-schema-api",)), actor="owner")
    s.resolve(SecretRef("api-token"), grant_id="sg1", actor=ACTOR,
              task_id=TASK, tool_id=TOOL, purpose="call-schema-api").reveal()
    s.provision(MappingSecretProvider({"db-pass": "correct-horse-battery"}),
                "db-pass")
    return log


def _net_req(url="https://api.example.com/v1/schema"):
    return NetworkRequest(actor=ACTOR, task_id=TASK, tool_id=TOOL,
                          target=parse_target(url, method="GET"),
                          resolved_address=None)


@pytest.fixture()
def network(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log)
    a.issue(net_grant(grant_id="g1", subject=ACTOR, task_id=TASK,
                      tool_id=TOOL, schemes=("https",),
                      hosts=("api.example.com",), ports=(443,),
                      methods=("GET",)), actor="scheduler")
    req = _net_req()
    a.record(req, a.authorize(req), actor=ACTOR)
    a.record_result(req, actor=ACTOR, outcome="OK", status=200,
                    response_digest=DIGEST)
    return log


# ---- controls: what the writers write reads clean ---------------------------

def test_control_a_governed_history_reads_clean(governed):
    log = EventLog(governed / "log.jsonl")
    s, t = rc.reconstruct_subsystems(log), rc.reconstruct_tasks(log)
    assert s.anomalies == [] and t.anomalies == []
    assert s.reads >= 2, "no governed read accepted: the control is vacuous"
    checks = [c for task in t.tasks.values() for c in task["checks"]]
    assert {a for _, a, _ in checks} == {"task.reexecution",
                                         "task.separate_verification"}


def test_control_messages_read_clean(directory):
    s = rc.reconstruct_subsystems(directory)
    assert s.anomalies == []
    assert set(s.messages) == {"m1", "m2"}


def test_control_secret_use_and_provisioning_read_clean(secrets):
    s = rc.reconstruct_subsystems(secrets)
    assert s.anomalies == []
    assert s.secret_uses == 1 and set(s.provisioned) == {"db-pass"}


def test_control_an_answered_request_reads_clean(network):
    s = rc.reconstruct_subsystems(network)
    assert s.anomalies == [] and s.net_results == 1


# ---- agent.message ------------------------------------------------------------

def _message(**over):
    rec = {"message_id": "m9", "sender_instance": "p1",
           "recipient_agent": "verifier", "task_id": "t1",
           "subject": "s", "body_digest": DIGEST, "sent_seq": -1,
           "in_reply_to": None, "delivered_to": []}
    rec.update(over)
    return rec


@pytest.mark.parametrize("actor,rec,why", [
    ("v1", _message(), "was appended by 'v1'"),
    ("p1", _message(body_digest="the whole body"), "other than a digest"),
    ("p1", _message(subject="x" * 201), "subject send() refuses"),
    ("p1", _message(in_reply_to="never-sent"), "which was never sent"),
    ("p1", _message(message_id="m1", subject="something else"),
     "re-sent with ['subject'] changed"),
    ("p1", {**_message(), "priority": "urgent"}, "malformed"),
    ("p1", {k: v for k, v in _message().items() if k != "task_id"},
     "malformed"),
])
def test_a_message_its_writer_could_not_have_sent(directory, actor, rec,
                                                  why):
    directory.append(actor=actor, action="agent.message", target="t1",
                     payload={"message": rec})
    found = rc.reconstruct_subsystems(directory).anomalies
    assert len(found) == 1 and why in found[0], found


# ---- file.read ----------------------------------------------------------------

def _forge_read(log, change):
    real = _last(log, "file.read")
    p = json.loads(json.dumps(real.payload))
    actor = change(p) or real.actor
    log.append(actor=actor, action="file.read", target=real.target,
               payload=p)


def _other_actor(p):
    p["request"]["actor"] = "intruder"
    return "intruder"


@pytest.mark.parametrize("name,change,why", [
    ("unissued", lambda p: p.update(capability_id="cap-never"),
     "this log never issued"),
    ("out-of-scope",
     lambda p: p["request"].update(root_id="elsewhere"),
     "does not permit"),
    ("other-task", lambda p: p["request"].update(task_id="task-other"),
     "does not permit"),
    ("not-the-holder", _other_actor, "does not permit"),
    ("misattributed", lambda p: p["request"].update(actor="intruder"),
     "the reader is who recorded it"),
    ("no-digest", lambda p: p["result"].update(digest=None), "no digest"),
    ("refused-with-bytes", lambda p: p.update(allowed=False),
     "bytes were read that nothing authorized"),
    ("no-verdict", lambda p: p.update(allowed="yes"),
     "whether it was allowed"),
])
def test_a_governed_read_its_writer_could_not_have_recorded(governed, name,
                                                            change, why):
    log = _copy(governed, f"read-{name}")
    _forge_read(log, change)
    found = rc.reconstruct_subsystems(log).anomalies
    assert len(found) == 1 and why in found[0], found


def test_a_read_under_a_lapsed_capability(governed):
    """Every capability in a governed history is issued with an expiry; a
    read recorded after it is a read nothing authorized."""
    log = _copy(governed, "read-lapsed")
    real = _last(log, "file.read")
    cap = rc.reconstruct_subsystems(log).capabilities[
        real.payload["capability_id"]]
    assert isinstance(cap["expires_after_seq"], int) \
        and cap["expires_after_seq"] >= 0
    while log.read_verified()[0].head_seq <= cap["expires_after_seq"]:
        log.append(actor="system", action="memory.status", target="pad",
                   payload={"padding": True})
    log.append(actor=real.actor, action="file.read", target=real.target,
               payload=real.payload)
    found = [f for f in rc.reconstruct_subsystems(log).anomalies
             if "file" in f or "capability" in f]
    assert any("expired after seq" in f for f in found), found


# ---- network.result -----------------------------------------------------------

def test_a_result_with_no_request_behind_it(network):
    req = _net_req("https://api.example.com/v2/other")
    NetworkAuthority(network).record_result(req, actor=ACTOR, outcome="OK",
                                            status=200)
    found = rc.reconstruct_subsystems(network).anomalies
    assert len(found) == 1 and "nobody authorized" in found[0], found


def test_one_request_answers_once(network):
    req = _net_req()
    NetworkAuthority(network).record_result(req, actor=ACTOR, outcome="OK")
    found = rc.reconstruct_subsystems(network).anomalies
    assert len(found) == 1 and "nobody authorized" in found[0], found


def test_a_refused_request_answers_nothing(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    a = NetworkAuthority(log)
    req = _net_req()
    decision = a.authorize(req)
    assert decision.allowed is False
    a.record(req, decision, actor=ACTOR)
    a.record_result(req, actor=ACTOR, outcome="OK", status=200)
    found = rc.reconstruct_subsystems(log).anomalies
    assert len(found) == 1 and "nobody authorized" in found[0], found


def test_another_actor_cannot_answer_my_request(network):
    req = _net_req()
    NetworkAuthority(network).record_result(req, actor="somebody-else",
                                            outcome="OK")
    found = rc.reconstruct_subsystems(network).anomalies
    assert len(found) == 1 and "'somebody-else'" in found[0], found


def test_a_response_digest_must_be_a_digest(network):
    req = _net_req()
    a = NetworkAuthority(network)
    a.record(req, a.authorize(req), actor=ACTOR)
    a.record_result(req, actor=ACTOR, outcome="OK",
                    response_digest="the response body")
    found = rc.reconstruct_subsystems(network).anomalies
    assert len(found) == 1 and "not a digest" in found[0], found


# ---- secret.access and secret.provision ---------------------------------------

def _use(**over):
    p = {"secret_id": "api-token", "grant_id": "sg1", "tool_id": TOOL,
         "purpose": "call-schema-api"}
    p.update(over)
    return p


@pytest.mark.parametrize("actor,target,over,why", [
    (ACTOR, TASK, {"grant_id": "sg-never"}, "never issued"),
    (ACTOR, TASK, {"grant_digest": DIGEST}, "cites other terms"),
    ("intruder", TASK, {}, "(subject)"),
    (ACTOR, "task-other", {}, "(task)"),
    (ACTOR, TASK, {"tool_id": "other.tool"}, "(tool)"),
    (ACTOR, TASK, {"purpose": "exfiltrate"}, "(purpose)"),
    (ACTOR, TASK, {"secret_id": "other-secret"}, "(secret)"),
])
def test_a_secret_use_no_grant_covers(secrets, actor, target, over, why):
    real = _last(secrets, "secret.access")
    payload = {**_use(grant_digest=real.payload["grant_digest"]), **over}
    secrets.append(actor=actor, action="secret.access", target=target,
                   payload=payload)
    found = rc.reconstruct_subsystems(secrets).anomalies
    assert len(found) == 1 and why in found[0], found


def test_a_secret_used_after_its_grant_was_revoked(secrets):
    SecretStore(secrets).load().revoke("sg1", actor="owner", reason="rotated")
    real = _last(secrets, "secret.access")
    secrets.append(actor=real.actor, action="secret.access",
                   target=real.target, payload=real.payload)
    found = rc.reconstruct_subsystems(secrets).anomalies
    assert len(found) == 1 and "was revoked" in found[0], found


@pytest.mark.parametrize("payload,why", [
    ({"secret_id": "db-pass", "provider": "mapping", "bytes": 21,
      "value": "correct-horse-battery"}, "never content"),
    ({"secret_id": "db-pass", "provider": "mapping", "bytes": 0},
     "an empty secret"),
    ({"secret_id": "db-pass", "provider": "mapping", "bytes": True},
     "an empty secret"),
    ({"secret_id": "", "provider": "mapping", "bytes": 9},
     "names no secret"),
])
def test_a_provisioning_record_its_writer_could_not_have_made(secrets,
                                                              payload, why):
    secrets.append(actor="deployment", action="secret.provision",
                   target="db-pass", payload=payload)
    found = rc.reconstruct_subsystems(secrets).anomalies
    assert len(found) == 1 and why in found[0], found


# ---- task.reexecution and task.separate_verification --------------------------

def _forge_check(log, action, change, *, actor=None):
    real = _last(log, action)
    p = json.loads(json.dumps(real.payload))
    change(p)
    log.append(actor=actor or real.actor, action=action, target=real.target,
               payload=p)


@pytest.mark.parametrize("action,change,actor,why", [
    ("task.reexecution", lambda p: None, None, "while the task is VERIFIED"),
    ("task.separate_verification", lambda p: None, None,
     "while the task is VERIFIED"),
])
def test_a_check_recorded_after_the_verdict(governed, action, change, actor,
                                            why):
    log = _copy(governed, f"late-{action}")
    _forge_check(log, action, change, actor=actor)
    found = rc.reconstruct_tasks(log).anomalies
    assert len(found) == 1 and why in found[0], found


def _completed_task(governed, name):
    """A copy whose history stops at a task's completion: the first
    re-execution record and everything after it removed, so the task stands
    COMPLETED and a check record is the next thing its verifier writes."""
    src = (governed / "log.jsonl").read_text().splitlines(keepends=True)
    cut = next(i for i, line in enumerate(src)
               if json.loads(line)["action"] == "task.reexecution")
    dst = governed.parent / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir()
    (dst / "log.jsonl").write_text("".join(src[:cut]))
    real = json.loads(src[cut])
    sep = next(json.loads(line) for line in src[cut:]
               if json.loads(line)["action"] == "task.separate_verification")
    return EventLog(dst / "log.jsonl"), real, sep


@pytest.mark.parametrize("change,by_executor,why", [
    (lambda p: None, True, "the task's executor"),
    (lambda p: p.update(tool_id="other.tool"), False, "and the task ran"),
    (lambda p: p.update(determinism="BEST_EFFORT"), False,
     "not declared BYTE_IDENTICAL"),
    (lambda p: p.update(compared=[]), False, "compared nothing"),
])
def test_a_reexecution_its_writer_could_not_have_recorded(governed, change,
                                                         by_executor, why):
    log, real, _ = _completed_task(governed, "reexec")
    assert rc.reconstruct_tasks(log).tasks[real["target"]]["state"] \
        == "COMPLETED"
    p = json.loads(json.dumps(real["payload"]))
    change(p)
    actor = (rc.reconstruct_tasks(log).tasks[real["target"]]["executed_by"]
             if by_executor else real["actor"])
    log.append(actor=actor, action="task.reexecution", target=real["target"],
               payload=p)
    found = rc.reconstruct_tasks(log).anomalies
    assert len(found) == 1 and why in found[0], found


@pytest.mark.parametrize("change,why", [
    (lambda p: p.update(ok=True, findings=["seq 3: something"]),
     "ok=True with 1 finding"),
    (lambda p: p.update(ok=False, findings=[]), "ok=False with 0 finding"),
    (lambda p: p.update(head_seq=0), "must cover the task's completion"),
    (lambda p: p.update(head_seq=10 ** 6), "must cover the task's completion"),
    (lambda p: p.pop("ok"), "records no verdict"),
])
def test_a_separate_verification_its_writer_could_not_have_recorded(
        governed, change, why):
    log, _, sep = _completed_task(governed, "sepv")
    p = json.loads(json.dumps(sep["payload"]))
    head = log.read_verified()[0].head_seq
    p["head_seq"] = head
    change(p)
    log.append(actor=sep["actor"], action="task.separate_verification",
               target=sep["target"], payload=p)
    found = rc.reconstruct_tasks(log).anomalies
    assert len(found) == 1 and why in found[0], found


def test_control_a_check_recorded_at_the_right_moment(governed):
    """The genuine records, replayed onto the truncated history, read clean
    -- so the cases above fail for their own reasons."""
    log, real, sep = _completed_task(governed, "check-control")
    log.append(actor=real["actor"], action="task.reexecution",
               target=real["target"], payload=real["payload"])
    p = dict(sep["payload"], head_seq=log.read_verified()[0].head_seq)
    log.append(actor=sep["actor"], action="task.separate_verification",
               target=sep["target"], payload=p)
    t = rc.reconstruct_tasks(log)
    assert t.anomalies == []
    assert len(t.tasks[real["target"]]["checks"]) == 2


# ---- the registry -------------------------------------------------------------

def test_every_durable_action_is_registered():
    """D-2026-85. ``secret.provision`` was written by SecretStore.provision
    and absent from ``actions.OWNERS``, so every reducer on a shared log
    refused the whole history as UNKNOWN the moment a secret was
    provisioned. The inventory enumerated the constant; nothing held the
    registry to it."""
    import ast
    from qta_agent import actions
    written = set()
    for f in sorted((ROOT / "qta_agent").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.startswith("ACT_")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                written.add(node.value.value)
    assert "secret.provision" in written, "the test would be vacuous"
    assert sorted(written - set(actions.OWNERS)) == []


def test_a_provisioned_secret_does_not_stop_the_other_readers(secrets):
    """The consequence, end to end: a genuine provisioning record on a
    shared log, and the readers that meet it on the way past still load."""
    from qta_agent.store import AuthorityStore
    AuthorityStore(secrets).load()
    SecretStore(secrets).load()
    NetworkAuthority(secrets).load()


# ---- none of them changes authority --------------------------------------------

def _authority_view(log: EventLog, evidence: Path) -> dict:
    """Every authority-bearing answer the primaries give about ``log``."""
    g = GovernedModelRuns(root=ROOT, log=log,
                          evidence=EvidenceStore(evidence))
    s = rc.reconstruct_subsystems(log)
    return {
        "authority": g.authority.state_digest(),
        "tasks": {tid: (t.state.value, t.executed_by)
                  for tid, t in g.gov.projection().tasks.items()},
        "capabilities": sorted(s.capabilities),
        "secret_grants": sorted(s.secret_grants),
        "net_grants": sorted(s.net_grants),
        "jobs": {j: v.get("state") for j, v in s.jobs.items()},
        "agents": sorted(s.agents),
        "claims": sorted(s.claims),
    }


def _forgeries(log: EventLog):
    """One record of each kind that every primary reader accepts and the
    second reader refuses."""
    read = _last(log, "file.read")
    reexec = _last(log, "task.reexecution")
    sep = _last(log, "task.separate_verification")
    return [
        ("agent.message", "p1", "t1",
         {"message": _message(body_digest="the whole body")}),
        ("file.read", read.actor, read.target,
         {**read.payload, "capability_id": "cap-never"}),
        ("network.result", ACTOR, TASK,
         {"target": parse_target("https://x.example/").to_record(),
          "outcome": "OK", "status": 200, "response_digest": None}),
        ("secret.access", ACTOR, TASK, _use(grant_id="sg-never")),
        ("secret.provision", "deployment", "db-pass",
         {"secret_id": "db-pass", "provider": "mapping", "bytes": 9,
          "value": "leaked"}),
        ("task.reexecution", reexec.actor, reexec.target, reexec.payload),
        ("task.separate_verification", sep.actor, sep.target,
         {**sep.payload, "findings": ["seq 1: x"]}),
    ]


def test_a_forged_audit_record_changes_no_authority(governed):
    log = _copy(governed, "no-authority")
    before = _authority_view(log, governed / "ev")
    assert before["authority"] and before["tasks"] and before["capabilities"]
    kinds = []
    for action, actor, target, payload in _forgeries(log):
        log.append(actor=actor, action=action, target=target,
                   payload=payload)
        kinds.append(action)
        assert _authority_view(log, governed / "ev") == before, action
    assert sorted(kinds) == sorted(
        {"agent.message", "file.read", "network.result", "secret.access",
         "secret.provision", "task.reexecution",
         "task.separate_verification"})
    found = _findings(log)
    assert len(found) == len(kinds), found
