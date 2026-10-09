"""Each race window of section 45, opened on purpose (D-2026-115..118).

tests/test_proposal_concurrency.py races real processes and finds what it
finds; these place the competing write exactly in the window each repair
closes, so the outcome does not depend on scheduling -- and so a mutation
that reopens one is killed every time rather than when the race happens to
be lost.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    SUBMITTER_ID, GovernedStage10,
)
from qta_agent.idempotency import IdempotencyLedger  # noqa: E402
from qta_agent.tasks import TaskRole, TaskState, TaskTransitionError  # noqa: E402

WS = "verification/stage10/_pytest_decided"


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _inputs(gov, name="artifact.json"):
    return {"out_dir": gov.out_rel, "name": name,
            "payload": {"label": "MODEL_ONLY", "value": 7}}


def _actions(gov, action):
    _, events = gov.log.read_verified()
    return [e for e in events if e.action == action]


def _stranded(gov, monkeypatch, key="k"):
    """A bound task whose supervisor died before dispatch: QUEUED."""
    def died(*a, **kw):
        raise SystemExit("the supervisor died before dispatch")
    with monkeypatch.context() as m:
        m.setattr(gov.scheduler, "dispatch", died)
        with pytest.raises(SystemExit):
            gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key=key)
    (create,) = _actions(gov, "task.create")
    return create.payload["task_id"]


def test_a_task_moved_since_its_caller_read_it_is_refused_with_nothing_written(
        gov, monkeypatch):
    """The move is decided against the head it is written onto. A caller
    holding the task as it was -- another process moved it since -- is
    refused, and the log still replays."""
    tid = _stranded(gov, monkeypatch)
    stale = gov.projection().get(tid)
    gov._move(stale, TaskState.CANCELLED, SUBMITTER_ID, TaskRole.SUBMITTER,
              note="the first process")
    before = len(gov.log.read())
    with pytest.raises(TaskTransitionError, match="moved under this writer"):
        gov._move(stale, TaskState.CANCELLED, SUBMITTER_ID,
                  TaskRole.SUBMITTER, note="the second, from what it read")
    assert len(gov.log.read()) == before
    assert gov.projection().get(tid).state is TaskState.CANCELLED


def test_a_submission_that_loses_the_bind_cancels_its_orphan(gov,
                                                             monkeypatch):
    """Another process binds the key in the window between this runner's
    lookup and its bind -- after this runner caught its ledger up. The bind,
    decided under the lock against the log, answers with that binding; this
    submission cancels the task it created before anything is queued and
    answers with the bound one. One task runs."""
    other = GovernedStage10(root=ROOT, log=EventLog(gov.log.path),
                            evidence=gov.evidence)
    real = other.idempotency.lookup
    first: dict = {}

    def lookup_in_the_window(**kw):
        if not first:
            # the other process submits, binds and finishes, right here
            first["run"] = gov.run(tool_id="stage10.emit_artifact",
                                   inputs=_inputs(gov),
                                   idempotency_key="race")
            return None
        return real(**kw)

    monkeypatch.setattr(other.idempotency, "lookup", lookup_in_the_window)
    again = other.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                      idempotency_key="race")
    first_run = first["run"]
    assert again.task_id == first_run.task_id and again.is_duplicate
    creates = [e.payload["task_id"] for e in _actions(gov, "task.create")]
    assert len(creates) == 2
    orphan = next(t for t in creates if t != first_run.task_id)
    assert gov.projection().get(orphan).state is TaskState.CANCELLED
    assert len(_actions(gov, "idempotency.bind")) == 1
    assert len(_actions(gov, "task.execution")) == 1
    ledger = IdempotencyLedger(EventLog(gov.log.path)).load()
    # the second reader accepts the history the lost bind left: the orphan's
    # cancellation, the one binding, the run that took it
    from qta_agent.reconstruct import (
        compare_bindings, compare_tasks, reconstruct_subsystems,
        reconstruct_tasks,
    )
    tasks = reconstruct_tasks(gov.log)
    assert tasks.unauthorized == [] and tasks.anomalies == [], tasks
    assert compare_tasks(gov.projection(), tasks) == ()
    sub = reconstruct_subsystems(gov.log)
    assert sub.anomalies == [], sub.anomalies
    assert compare_bindings(ledger, tasks) == ()


def test_a_record_landing_just_before_the_checkpoint_is_in_its_snapshot(
        tmp_path, monkeypatch):
    """Another writer appends after this store last read and before the
    checkpoint takes the lock. The snapshot is taken under the lock, caught
    up to that head, so the checkpoint describes the position it anchors."""
    from qta_agent.checkpoint import CheckpointStore
    from qta_agent.store import AuthorityStore
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "ev")
    store = AuthorityStore(log, evidence=ev).load()
    other = AuthorityStore(EventLog(log.path), evidence=ev).load()
    sha = ev.put(b"{}", media_type="application/json")
    store.create(record_id="r1", kind="note", proposer="p",
                 evidence={"x": sha})
    real = log.append_decided

    def another_writer_first(decide, **kw):
        other.catch_up()
        other.create(record_id="r2", kind="note", proposer="p",
                     evidence={"x": sha})
        return real(decide, **kw)

    monkeypatch.setattr(log, "append_decided", another_writer_first)
    cps = CheckpointStore(tmp_path / "cps")
    cp = store.checkpoint(cps)
    monkeypatch.undo()
    _, cmp = AuthorityStore.recover_and_compare(log, cps, blobs=ev,
                                                evidence=ev)
    assert cmp["agrees"] and cmp["mode"] == "CHECKPOINT_ASSISTED", cmp
    assert cmp["checkpoint_seq"] == cp.seq


def test_a_checkpoint_whose_snapshot_misdescribes_its_anchor_is_replayed_past(
        tmp_path):
    """The shape the old race left on disk: a snapshot of one position
    pinned to a later one. The audit cannot fault it -- it parses and names
    this log's bytes -- and load_from refuses it; the restart replays from
    genesis and says so, rather than failing to start."""
    from qta_agent import checkpoint as cp_mod
    from qta_agent.canonical import canonical_bytes
    from qta_agent.checkpoint import CheckpointStore
    from qta_agent.store import AuthorityStore
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "ev")
    store = AuthorityStore(log, evidence=ev).load()
    sha = ev.put(b"{}", media_type="application/json")
    store.create(record_id="r1", kind="note", proposer="p",
                 evidence={"x": sha})
    snap = ev.put(canonical_bytes(store.snapshot()),
                  media_type="application/json")
    store.create(record_id="r2", kind="note", proposer="p",
                 evidence={"x": sha})
    cps = CheckpointStore(tmp_path / "cps")
    cps.write(cp_mod.create(log, state_digest=snap))     # pinned one later
    recovered, report = AuthorityStore.recover(log, cps, blobs=ev,
                                               evidence=ev)
    assert report["mode"] == "FULL_REPLAY" and report["healthy"] is False
    assert "does not describe" in report["refusal"]
    assert sorted(recovered.all_records()) == ["r1", "r2"]


def test_a_supervisor_appending_while_a_tool_runs_is_not_the_tools_write(
        gov, monkeypatch):
    """Another supervisor appends to the shared log while this one's tool is
    running. The log sits inside the tool's writable scope; the append is
    not the tool's and does not fail its run."""
    import qta_agent.execution as X
    real = X._collect_outputs

    def a_supervisor_appends_meanwhile(cwd, declared):
        EventLog(gov.log.path).append(
            actor="system", action="policy.decision", target="elsewhere",
            payload={"decision": {"allowed": True}, "request": {},
                     "policy_id": "x"})
        return real(cwd, declared)

    monkeypatch.setattr(X, "_collect_outputs",
                        a_supervisor_appends_meanwhile)
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))
    assert run.state is TaskState.VERIFIED, run.reason
    (ex,) = _actions(gov, "task.execution")
    assert ex.payload["undeclared_writes"] == []


# ---- a grant stamped past its own expiry (D-2026-117, the expiry half) ---
def _filler(log, n):
    """Another writer's records, landing between a read and an append."""
    for i in range(n):
        log.append(actor="system", action="policy.decision",
                   target=f"elsewhere-{i}",
                   payload={"decision": {"allowed": True}, "request": {},
                            "policy_id": "x"})


def _capability_grant(log):
    from qta_agent.capability import Action, CapabilityLedger, issue
    led = CapabilityLedger(log).load()
    head = len(log.read()) - 1
    cap = issue(capability_id="c1", subject="v", action=Action.EXECUTE_TOOL,
                task_id="t1", tool_id="stage10.emit_artifact",
                scope=("verification/",), issued_seq=head + 1,
                expires_after_seq=head + 4)
    return (lambda: led.issue(cap, actor="scheduler"),
            lambda: CapabilityLedger(EventLog(log.path)).load())


def _egress_grant(log):
    from qta_agent.netauth import NetworkAuthority, grant
    auth = NetworkAuthority(log).load()
    head = len(log.read()) - 1
    g = grant(grant_id="g1", subject="w", task_id="t1", tool_id="fetch",
              schemes=("https",), hosts=("api.example.com",), ports=(443,),
              methods=("GET",), issued_seq=head + 1,
              expires_after_seq=head + 4)
    return (lambda: auth.issue(g, actor="scheduler"),
            lambda: NetworkAuthority(EventLog(log.path)).load())


def _secret_grant(log):
    from qta_agent.secrets import SecretStore, grant
    store = SecretStore(log)
    store.register("api-token", "hunter2-super-secret-token-value")
    head = len(log.read()) - 1
    g = grant(grant_id="sg1", subject="w", task_id="t1", tool_id="fetch",
              secret_id="api-token", purposes=("call-api",),
              issued_seq=head + 1, expires_after_seq=head + 4)
    return (lambda: store.issue(g, actor="owner"),
            lambda: SecretStore(EventLog(log.path)).load())


@pytest.mark.parametrize("make", [_capability_grant, _egress_grant,
                                  _secret_grant],
                         ids=["capability", "egress", "secret"])
def test_a_grant_the_stamp_moves_past_its_expiry_is_refused_unwritten(
        tmp_path, make):
    """The caller computes a short expiry from the head it read; five
    records land before the append; the stamp would start the grant after
    it ended. Such a grant was never valid and every replay refuses it --
    appended, it would leave a log no runner can open. Refused under the
    lock instead, with nothing written, and the log still replays."""
    from qta_agent.capability import CapabilityError
    from qta_agent.netauth import NetworkError
    from qta_agent.secrets import SecretError
    log = EventLog(tmp_path / "log.jsonl")
    _filler(log, 1)
    issue_it, replay = make(log)
    _filler(log, 5)
    before = log.path.read_bytes()
    grants = {"capability.issue", "network.grant", "secret.grant"}
    with pytest.raises((CapabilityError, NetworkError, SecretError),
                       match="before it was issued"):
        issue_it()
    assert [e for e in log.read() if e.action in grants] == []
    assert log.path.read_bytes().startswith(before)
    replay()


def test_a_one_action_grant_lives_from_where_it_is_stamped(tmp_path):
    """The governed runner's compensation and re-verification grants are
    bounded by a lifetime, not by an expiry computed from an earlier head:
    after other writers land first, the grant is still in force for exactly
    its own position and the three after it."""
    from qta_agent.capability import Action, CapabilityLedger, issue
    log = EventLog(tmp_path / "log.jsonl")
    _filler(log, 1)
    led = CapabilityLedger(log).load()
    head = len(log.read()) - 1
    cap = issue(capability_id="c1", subject="v", action=Action.EXECUTE_TOOL,
                task_id="t1", tool_id="stage10.emit_artifact",
                scope=("verification/",), issued_seq=head + 1)
    _filler(log, 5)
    got = led.issue(cap, actor="scheduler", lifetime_seqs=4)
    (ev,) = [e for e in log.read() if e.action == "capability.issue"]
    assert got.issued_seq == ev.seq > head + 1
    assert got.expires_after_seq == ev.seq + 3
    assert ev.payload["expires_after_seq"] == ev.seq + 3
    again = CapabilityLedger(EventLog(log.path)).load()
    assert again.issued_ids() == ("c1",)
