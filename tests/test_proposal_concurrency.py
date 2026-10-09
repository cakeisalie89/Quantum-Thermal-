"""The proposal path under real concurrent processes (section 45; D-2026-115).

tests/test_agent_cross_process.py races the scheduler and the authority
store, and tests/test_agent_concurrency.py the log and the evidence store
(concurrent inserts of the same and of different content). What neither
reached is the path this programme added on top: an AI proposal received,
submitted, taken up after a crash, decided. Here the processes are real and
are lined up at a start line, so each race is a race:

* RECEIPTS -- four ingress processes receive one proposal at once: one
  receipt between them, every caller told the same id;
* CLAIMS -- four processes resubmit one stranded proposal at once (its
  supervisor died holding the lease): the attempt is taken up by exactly
  one of them, the model runs once, one record, one pickup, one verdict, and
  every other caller is refused by name rather than racing it;
* CHECKPOINTS -- four processes checkpoint the decided projection at once:
  every checkpoint whole, the restart agrees with a full replay.

What both properties rest on is the single-writer, serialized authority
append: every move of a task and every receipt is DECIDED under the log's
writer lock, against the head it is written onto (EventLog.append_decided).

WHAT THIS DOES NOT SHOW. The writer lock is an advisory flock on a local
file system. These processes share one host and one kernel; nothing here
says what flock does over NFS or any other network file system, and that
remains a stated platform boundary, not a result.
"""
from __future__ import annotations

import multiprocessing as mp
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(ROOT / "tools"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_agent_cross_process as XP  # noqa: E402
import test_proposal_crash_recovery as PCR  # noqa: E402
from hangguard import PROCESS_DEADLINE_S  # noqa: E402
from test_proposal_crash_recovery import reference  # noqa: E402,F401

from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.reconstruct import (  # noqa: E402
    reconstruct, reconstruct_subsystems, reconstruct_tasks,
)
from qta_agent.store import AuthorityStore  # noqa: E402

RACERS = 4
RX = "verification/stage10/_pytest_proposal_concurrency"

#: The refusals a loser may meet, each a named refusal of the race it lost;
#: anything else -- a KeyError, a broken chain -- is a defect, not a loss.
#: By class NAME, as the workers report them -- so a subclass is listed
#: itself: TaskMovedUnderWriter is the TaskTransitionError a loser of the
#: move race gets (D-2026-120).
REFUSALS = {"TaskTransitionError", "TaskMovedUnderWriter",
            "ModelRunRefused", "TransitionError",
            "JobTransitionError", "SchedulerError", "StoreError",
            "AuthorityError", "IdempotencyConflict", "DuplicateJob"}


def _open(base: Path):
    import harness_demo as HD
    from qta_agent import proposals as PR
    from qta_agent.governed_model import GovernedModelRuns
    from qta_multiphysics.stack.rag_index import retrieve
    HD.WS = PCR.WS
    log = EventLog(base / "log.jsonl")
    ev = EvidenceStore(base / "evidence")
    ctx = PR.assemble_context(HD.QUERY, retrieve=retrieve, k=3)
    return HD, log, ev, ctx, GovernedModelRuns


# ---- module-level workers: spawn pickles them by reference ---------------
def _receive_worker(args):
    go, base = args
    HD, log, _ev, ctx, _G = _open(Path(base))
    XP._wait_for_start(go)
    _ing, receipt = HD._receive(log, ctx, 0)
    return (receipt.envelope.proposal_id, receipt.duplicate)


def _resubmit_worker(args):
    go, base = args
    _HD, log, ev, ctx, G = _open(Path(base))
    g = G(root=ROOT, log=log, evidence=ev)
    XP._wait_for_start(go)
    try:
        _pid, run, _chk, rec = PCR._path(log, g, ctx)
        return ("decided", rec.state.value, run.governed.task_id)
    except Exception as exc:                     # noqa: BLE001 - reported
        return ("refused", type(exc).__name__, str(exc)[:300])


def _checkpoint_worker(args):
    go, base = args
    from qta_agent.checkpoint import CheckpointStore
    _HD, log, ev, _ctx, G = _open(Path(base))
    g = G(root=ROOT, log=log, evidence=ev)
    XP._wait_for_start(go)
    cp = g.checkpoint(CheckpointStore(Path(base) / "checkpoints"))
    return cp.seq


GRANTS_EACH = 12


def _grant_worker(args):
    go, base, tag = args
    import time as _t
    import uuid as _u
    from qta_agent.capability import Action, CapabilityLedger
    from qta_agent.capability import issue as cap_issue
    from qta_agent.netauth import NetworkAuthority, grant
    log = EventLog(Path(base) / "log.jsonl")
    caps = CapabilityLedger(log).load()
    net = NetworkAuthority(log).load()
    XP._wait_for_start(go)
    for i in range(GRANTS_EACH):
        caps.issue(cap_issue(
            capability_id=f"cap-{tag}-{i}-{_u.uuid4().hex[:6]}",
            subject="w", action=Action.EXECUTE_TOOL, task_id=f"t{tag}",
            tool_id="stage10.emit_artifact", scope=("verification/stage10",),
            issued_seq=0, issued_wall_time=_t.time()), actor="scheduler")
        net.issue(grant(
            grant_id=f"egress-{tag}-{i}", subject="w", task_id=f"t{tag}",
            tool_id="stage10.emit_artifact", schemes=("https",),
            hosts=("example.invalid",), ports=(443,), methods=("GET",)),
            actor="scheduler")
    return tag


def _race(worker, base: Path, tmp_path, args=None) -> list:
    go = XP._go(tmp_path)
    with mp.get_context("spawn").Pool(RACERS) as pool:
        pending = pool.map_async(worker, args or [(go, str(base))] * RACERS)
        XP._line_up(tmp_path, RACERS, pending)
        return pending.get(timeout=PROCESS_DEADLINE_S * 20)


def _clean(log) -> None:
    """The history replays, and the second reader finds nothing."""
    recon = reconstruct(log)
    tasks = reconstruct_tasks(log)
    assert recon.unauthorized == [] and recon.anomalies == [], recon
    assert tasks.unauthorized == [] and tasks.anomalies == [], tasks
    # and the rest: bindings, grants stamped under the lock, receipts
    sub = reconstruct_subsystems(log)
    assert sub.anomalies == [], sub.anomalies


def test_four_ingress_processes_receive_one_proposal_once(tmp_path):
    import harness_demo as HD
    HD.WS = RX
    base = ROOT / RX
    log, _ev, _g = HD._world(base)
    try:
        got = _race(_receive_worker, base, tmp_path)
        assert len({pid for pid, _ in got}) == 1, got
        assert sorted(dup for _, dup in got) == [False] + [True] * (
            RACERS - 1), got
        _, events = log.read_verified()
        assert sum(e.action == "proposal.receive" for e in events) == 1
        _clean(log)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_four_resubmissions_of_one_stranded_proposal_take_it_up_once(
        reference, tmp_path):  # noqa: F811 - the imported fixture
    ref = reference
    log, ev, _g = PCR._crash_at(
        ref, PCR.STAGES["lease issued"](ref, ref["events"]))
    got = _race(_resubmit_worker, ROOT / PCR.WS, tmp_path)

    decided = [r for r in got if r[0] == "decided"]
    refused = [r for r in got if r[0] == "refused"]
    assert decided, "\n".join(map(repr, got))
    assert {r[1] for r in decided} == {ref["final"]}, got
    assert {r[2] for r in decided} == {ref["task_id"]}, got
    assert {r[1] for r in refused} <= REFUSALS, refused

    _, after = log.read_verified()
    assert PCR._executions(after) == {ref["task_id"]: 1}, (
        "the stranded attempt ran more than once")
    assert sum(e.action == "proposal.receive" for e in after) == 1
    assert sum(e.action == "record.create" for e in after) == 1
    assert sum(e.action == "record.transition"
               and e.payload.get("dst") == "UNDER_REVIEW"
               for e in after) == 1
    assert sum(e.action == "record.transition"
               and e.payload.get("dst") in ("VERIFIED", "REJECTED")
               for e in after) == 1
    leases = [e for e in after if PCR._move(ref["task_id"], "LEASED")(e)]
    assert len(leases) == 2, "one lease by the dead holder, one taken up"
    AuthorityStore(log, evidence=ev).load()
    _clean(log)


def test_four_first_submissions_of_one_proposal_run_it_once(
        reference, tmp_path):  # noqa: F811 - the imported fixture
    """DUPLICATE REQUESTS: the proposal is received and nobody has
    submitted it, and four processes submit it at once. Each finds the key
    free and creates a task; one binds the key, and every other cancels its
    own orphan before anything is queued and answers with the bound task.
    One task runs; the cancelled ones never execute."""
    ref = reference
    log, ev, _g = PCR._crash_at(
        ref, PCR.STAGES["proposal accepted"](ref, ref["events"]))
    got = _race(_resubmit_worker, ROOT / PCR.WS, tmp_path)

    decided = [r for r in got if r[0] == "decided"]
    refused = [r for r in got if r[0] == "refused"]
    assert decided, "\n".join(map(repr, got))
    assert {r[1] for r in decided} == {ref["final"]}, got
    assert len({r[2] for r in decided}) == 1, got
    assert {r[1] for r in refused} <= REFUSALS, refused

    _, after = log.read_verified()
    runs = PCR._executions(after)
    assert list(runs.values()) == [1], runs
    (task_id,) = runs
    # one binding of the proposal's key, to the task that ran -- and one of
    # the check's, which is keyed by the record, the check and its directory
    bindings = [e for e in after if e.action == "idempotency.bind"]
    submitted = [e for e in bindings
                 if not e.payload["key"].startswith("check:")]
    assert len(submitted) == 1 and submitted[0].target == task_id, [
        (e.seq, e.actor, e.target, e.payload) for e in bindings]
    assert len(bindings) - len(submitted) == 1, bindings
    model = {e.payload["task_id"] for e in after
             if e.action == "task.create"
             and e.payload.get("tool_id") == PCR.MODEL_TOOL}
    tasks = reconstruct_tasks(log)
    for tid in model - {task_id}:
        assert tasks.tasks[tid]["state"] in ("CANCELLED", "CREATED"), tid
    assert sum(e.action == "record.create" for e in after) == 1
    assert sum(e.action == "record.transition"
               and e.payload.get("dst") in ("VERIFIED", "REJECTED")
               for e in after) == 1
    AuthorityStore(log, evidence=ev).load()
    _clean(log)


def test_four_processes_checkpointing_at_once_leave_whole_checkpoints(
        reference, tmp_path):  # noqa: F811 - the imported fixture
    from qta_agent.checkpoint import CheckpointStore
    from qta_agent.governed_model import GovernedModelRuns
    ref = reference
    log, ev, _g = PCR._crash_at(ref, len(ref["lines"]) - 1)
    seqs = _race(_checkpoint_worker, ROOT / PCR.WS, tmp_path)
    assert len(seqs) == RACERS

    cps = CheckpointStore(ROOT / PCR.WS / "checkpoints")
    for path in cps.root.glob("*.checkpoint.json"):
        cps.read(int(path.name.split(".")[0]))     # every one parses whole
    assert not list(cps.root.glob(".tmp-*")), "a write was left half done"
    again = GovernedModelRuns(root=ROOT, log=log, evidence=ev,
                              checkpoints=cps)
    _, cmp = AuthorityStore.recover_and_compare(
        log, cps, blobs=ev, evidence=ev, origins=again.origins)
    assert cmp["agrees"], cmp
    assert again.recovery["mode"] == "CHECKPOINT_ASSISTED"
    _, after = log.read_verified()
    assert sum(e.action == "checkpoint.state" for e in after) == RACERS
    _clean(log)


def test_grants_issued_under_contention_start_where_they_are_written(
        tmp_path):
    """A grant starts at the position its record is written, which the
    ledger stamps. Stamped from a head read before the append, any writer
    landing in between put the grant after the start it claimed -- and
    replay refuses a backdated grant on every load, so one benign race left
    a log nothing could open (D-2026-117; found mid-race by the
    resubmission test above). Four processes interleave capability and
    egress grants; every ledger then replays, and every grant starts where
    it sits."""
    from qta_agent.capability import CapabilityLedger
    from qta_agent.netauth import NetworkAuthority
    base = tmp_path / "grants"
    base.mkdir()
    go = XP._go(tmp_path)
    got = _race(_grant_worker, base, tmp_path,
                args=[(go, str(base), n) for n in range(RACERS)])
    assert sorted(got) == list(range(RACERS))
    log = EventLog(base / "log.jsonl")
    caps = CapabilityLedger(log).load()
    NetworkAuthority(log).load()
    _, events = log.read_verified()
    issued = [e for e in events if e.action == "capability.issue"]
    assert len(issued) == RACERS * GRANTS_EACH
    for e in issued:
        assert e.payload["issued_seq"] == e.seq, e.seq
    assert sum(e.action == "capability.root" for e in events) == 1
    assert len(caps.issued_ids()) == RACERS * GRANTS_EACH
