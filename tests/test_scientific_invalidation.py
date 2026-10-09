"""Admission follows invalidation: what rested on a withdrawn origin goes STALE.

A scientific result is admitted on its ORIGIN -- its bundle an artefact of a
governed model run, its report an artefact of a governed check, both standing
VERIFIED where the decision stands in the log. Before this, a task
invalidated afterwards left the result VERIFIED: correct about the past (the
admission was sound when it was made, and replay still says so), silent about
the present. Reuse already refused it; nothing else knew.

Now the change is followed. ``GovernedModelRuns.invalidate_task`` moves the
task VERIFIED -> INVALIDATED through the gate, and every result citing an
artefact that task captured before its verdict goes STALE -- when its origin
no longer holds now (a second governed task that produced the same bytes
keeps it standing) -- and so does everything depending on it, by the same
transitive walk a record origin gets. ``withdraw_evidence`` is the same,
starting from an artefact: the bytes stay, what gave them authority is
invalidated. Nothing is rewritten; both readers replay the history to the
same states.

Every case starts from one genuine history, copied, so each can damage its own.
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
from qta_agent import result_rules  # noqa: E402
from qta_agent.authority import Role, State, TransitionError  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import (  # noqa: E402
    REVIEWER_ID, GovernedModelRuns, ModelRun, CheckRun, ModelRunRefused,
)
from qta_agent.governed_stage10 import SUBMITTER_ID  # noqa: E402
from qta_agent.tasks import TaskState, TaskTransitionError  # noqa: E402

WS = "verification/stage10/_pytest_invalidation"
CHECK = "thermal_1d.reduction_2d_radial_disabled"
MODEL = {"model_id": "thermal.conduction_1d", "model_version": "1.0.0"}
PROMOTER = "result-promoter"


@pytest.fixture(scope="module")
def world():
    """Two results, decided through the store: ``a`` and an unrelated
    ``b`` (other parameters, its own run and check)."""
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    (base / "genuine").mkdir(parents=True)
    g = GovernedModelRuns(root=ROOT, log=EventLog(base / "genuine" /
                                                  "log.jsonl"),
                          evidence=EvidenceStore(base / "genuine" /
                                                 "evidence"))
    out = {}
    for name, params in (("a", {"n_cells": 60, "n_eval": 20}),
                         ("b", {"n_cells": 40, "n_eval": 16})):
        run = g.propose(**MODEL, parameters=params,
                        out_dir=f"{WS}/genuine/{name}-run")
        chk = g.check(run, check_id=CHECK,
                      out_dir=f"{WS}/genuine/{name}-check")
        assert g.decide(run, chk).state is State.VERIFIED
        out[name] = (run, chk)
    yield out
    if base.exists():
        shutil.rmtree(base)


def _copy(name: str) -> Path:
    src, dst = ROOT / WS / "genuine", ROOT / WS / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copytree(src / "evidence", dst / "evidence")
    shutil.copy(src / "log.jsonl", dst / "log.jsonl")
    return dst


def _open(base: Path) -> GovernedModelRuns:
    return GovernedModelRuns(root=ROOT, log=EventLog(base / "log.jsonl"),
                             evidence=EvidenceStore(base / "evidence"))


def _replayed(base: Path, *rids) -> dict:
    """Both readers, fresh from the bytes: the states they reach, after
    checking they reach them without a refusal and agree on everything."""
    g = _open(base)
    log = EventLog(base / "log.jsonl")
    recon = rc.reconstruct(log, evidence=EvidenceStore(base / "evidence"))
    assert not recon.unauthorized and not recon.anomalies, (
        recon.unauthorized, recon.anomalies)
    assert rc.compare(g.authority, recon) == ()
    tasks = rc.reconstruct_tasks(log)
    assert not tasks.unauthorized and not tasks.anomalies
    assert rc.compare_tasks(g.gov.projection(), tasks) == ()
    states = {}
    for rid in rids:
        assert recon.records[rid]["state"] == g.authority.get(rid).state.value
        states[rid] = g.authority.get(rid).state
    return states


def _task_state(base: Path, task_id: str) -> TaskState:
    return _open(base).gov.projection().get(task_id).state


# ---- the control ----------------------------------------------------------

def test_the_genuine_history_stands(world):
    (a, _), (b, _) = world["a"], world["b"]
    base = _copy("control")
    assert _replayed(base, a.record_id, b.record_id) == {
        a.record_id: State.VERIFIED, b.record_id: State.VERIFIED}


# ---- the origin tasks -------------------------------------------------------

def test_an_invalidated_model_run_makes_its_result_stale(world):
    (a, _), (b, _) = world["a"], world["b"]
    base = _copy("producer")
    inv = _open(base).invalidate_task(a.governed.task_id,
                                      reason="the solver was wrong")
    assert inv.tasks == (a.governed.task_id,)
    assert inv.plan.affected == (a.record_id,)
    assert inv.kept == ()
    assert _replayed(base, a.record_id, b.record_id) == {
        a.record_id: State.STALE, b.record_id: State.VERIFIED}
    assert _task_state(base, a.governed.task_id) is TaskState.INVALIDATED


def test_an_invalidated_check_makes_its_result_stale(world):
    (a, chk), (b, _) = world["a"], world["b"]
    base = _copy("verifier")
    inv = _open(base).invalidate_task(chk.governed.task_id,
                                      reason="the check was miscalibrated")
    assert inv.plan.affected == (a.record_id,)
    assert _replayed(base, a.record_id, b.record_id) == {
        a.record_id: State.STALE, b.record_id: State.VERIFIED}
    assert _task_state(base, chk.governed.task_id) is TaskState.INVALIDATED


def test_the_stale_record_cites_what_changed(world):
    """``invalidated_by`` resolves, in the store, to the origin and the
    reason -- not a digest of nothing."""
    (a, chk) = world["a"]
    base = _copy("cites")
    _open(base).invalidate_task(chk.governed.task_id, reason="recalibrated")
    g = _open(base)
    rec = g.authority.get(a.record_id)
    cited = json.loads(g.evidence.get(rec.evidence["invalidated_by"]))
    assert cited == {"origin": f"task:{chk.governed.task_id}",
                     "reason": "recalibrated"}
    assert chk.governed.task_id in rec.stale_reason


# ---- withdrawn evidence -----------------------------------------------------

def test_withdrawn_verification_evidence_makes_the_result_stale(world):
    (a, chk), (b, _) = world["a"], world["b"]
    base = _copy("withdrawn-report")
    inv = _open(base).withdraw_evidence(chk.report_sha256,
                                        reason="the report was retracted")
    assert inv.origin == f"evidence:{chk.report_sha256}"
    assert inv.tasks == (chk.governed.task_id,)
    assert _replayed(base, a.record_id, b.record_id) == {
        a.record_id: State.STALE, b.record_id: State.VERIFIED}


def test_a_withdrawn_model_artefact_makes_the_result_stale(world):
    """Not the bundle: a file the model run produced that the bundle
    references. Withdrawing it invalidates the run that captured it, and
    the bundle loses its origin with it."""
    (a, _), (b, _) = world["a"], world["b"]
    field = [sha for rel, sha in a.governed.artifacts.items()
             if not rel.endswith("bundle.json")]
    assert field, a.governed.artifacts
    base = _copy("withdrawn-artefact")
    inv = _open(base).withdraw_evidence(field[0],
                                        reason="the field file is corrupt")
    assert inv.tasks == (a.governed.task_id,)
    assert inv.plan.affected == (a.record_id,)
    assert _replayed(base, a.record_id, b.record_id) == {
        a.record_id: State.STALE, b.record_id: State.VERIFIED}


def test_withdrawing_bytes_no_governed_task_vouched_for_is_refused(world):
    base = _copy("withdraw-nothing")
    g = _open(base)
    sha = g.evidence.put(b"nobody's artefact")
    head = (base / "log.jsonl").read_bytes()
    with pytest.raises(ModelRunRefused, match="no authority here"):
        g.withdraw_evidence(sha, reason="?")
    assert (base / "log.jsonl").read_bytes() == head


def test_bytes_attached_to_a_task_after_its_verdict_carry_no_authority(world):
    """A capture record naming new bytes for a task verified long ago is
    not something that task's verdict examined, so withdrawing those bytes
    finds no origin to invalidate -- and the task, and what rests on it,
    stay as they are."""
    (a, chk) = world["a"]
    base = _copy("late-capture")
    g = _open(base)
    late = g.evidence.put(b"attached after the verdict")
    EventLog(base / "log.jsonl").append(
        actor="system", action="task.evidence", target=chk.governed.task_id,
        payload={"task_id": chk.governed.task_id,
                 "artifacts": {"late/extra.bin": late}})
    with pytest.raises(ModelRunRefused, match="no authority here"):
        _open(base).withdraw_evidence(late, reason="r")
    assert _task_state(base, chk.governed.task_id) is TaskState.VERIFIED
    assert _replayed(base, a.record_id) == {a.record_id: State.VERIFIED}


# ---- how far it reaches ----------------------------------------------------

def _dependent(base: Path, on: str, rid: str = "derived-1") -> str:
    """A VERIFIED record resting on ``on`` -- a later claim built on it."""
    g = _open(base)
    report = g.evidence.put(b'{"derived": true}')
    g.authority.create(record_id=rid, kind="derived_claim",
                       proposer=SUBMITTER_ID, evidence={"basis": report},
                       depends_on=(on,))
    g.authority.transition(record_id=rid, dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)
    g.authority.transition(record_id=rid, dst=State.VERIFIED,
                           actor=REVIEWER_ID, role=Role.VERIFIER,
                           evidence={"verification_report": report})
    return rid


def test_invalidation_is_transitive(world):
    (a, chk), (b, _) = world["a"], world["b"]
    base = _copy("transitive")
    d1 = _dependent(base, a.record_id)
    d2 = _dependent(base, d1, rid="derived-2")
    inv = _open(base).invalidate_task(chk.governed.task_id, reason="r")
    assert inv.plan.affected == (a.record_id, d1, d2)
    assert inv.plan.explain(d2) == " -> ".join(
        [f"task:{chk.governed.task_id}", a.record_id, d1, d2])
    assert _replayed(base, a.record_id, d1, d2, b.record_id) == {
        a.record_id: State.STALE, d1: State.STALE, d2: State.STALE,
        b.record_id: State.VERIFIED}


def test_an_unrelated_task_changes_nothing_but_itself(world):
    """``b``'s check is invalidated; ``a`` rests on nothing of it."""
    (a, _), (b, chk_b) = world["a"], world["b"]
    base = _copy("unrelated")
    d = _dependent(base, a.record_id)
    inv = _open(base).invalidate_task(chk_b.governed.task_id, reason="r")
    assert inv.plan.affected == (b.record_id,)
    assert _replayed(base, a.record_id, d, b.record_id) == {
        a.record_id: State.VERIFIED, d: State.VERIFIED,
        b.record_id: State.STALE}


def test_a_second_origin_keeps_the_result_standing(world):
    """A second governed check of the same bundle produced the same report
    bytes. Invalidating one check leaves the other as the report's origin,
    so the result stands -- and is reported as kept, not silently skipped.
    Withdrawing the report itself takes both."""
    (a, chk) = world["a"]
    base = _copy("second-origin")
    g = _open(base)
    run = ModelRun(None, a.bundle_path, a.bundle_sha256, a.bundle_digest,
                   a.record_id, a.submitter, a.worker)
    again = g.check(run, check_id=CHECK, out_dir=f"{WS}/second-origin/chk2")
    assert isinstance(again, CheckRun)
    assert again.report_sha256 == chk.report_sha256
    inv = g.invalidate_task(chk.governed.task_id, reason="r")
    assert inv.plan.affected == () and inv.kept == (a.record_id,)
    assert _replayed(base, a.record_id) == {a.record_id: State.VERIFIED}
    inv = _open(base).withdraw_evidence(chk.report_sha256, reason="r")
    assert inv.tasks == (again.governed.task_id,)
    assert _replayed(base, a.record_id) == {a.record_id: State.STALE}


# ---- records that cannot go stale -------------------------------------------

def test_revoked_and_rejected_results_are_left_as_they_are(world):
    """No edge leads from REVOKED or REJECTED to STALE, so they are
    reported as reached and skipped -- never moved, never an error that
    stops the rest of the cascade."""
    (a, chk) = world["a"]
    base = _copy("terminal")
    g = _open(base)
    reason = g.evidence.put(b'{"why": "withdrawn by hand"}')
    g.authority.transition(record_id=a.record_id, dst=State.REVOKED,
                           actor=PROMOTER, role=Role.PROMOTER,
                           evidence={"revocation_reason": reason})
    g.authority.create(record_id="rejected-1",
                       kind=result_rules.KIND, proposer=SUBMITTER_ID,
                       evidence={"result_bundle": a.bundle_sha256})
    g.authority.transition(record_id="rejected-1", dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)
    g.authority.transition(record_id="rejected-1", dst=State.REJECTED,
                           actor=REVIEWER_ID, role=Role.VERIFIER,
                           evidence={"rejection_reason": reason})
    inv = _open(base).invalidate_task(a.governed.task_id, reason="r")
    assert inv.plan.affected == ()
    assert inv.plan.skipped == {a.record_id: "REVOKED",
                                "rejected-1": "REJECTED"}
    assert _replayed(base, a.record_id, "rejected-1") == {
        a.record_id: State.REVOKED, "rejected-1": State.REJECTED}


def test_a_task_not_standing_verified_cannot_be_invalidated(world):
    (a, chk) = world["a"]
    base = _copy("twice")
    _open(base).invalidate_task(chk.governed.task_id, reason="r")
    head = (base / "log.jsonl").read_bytes()
    with pytest.raises(TaskTransitionError):
        _open(base).invalidate_task(chk.governed.task_id, reason="again")
    with pytest.raises(ValueError, match="states what changed"):
        _open(base).invalidate_task(a.governed.task_id, reason="")
    with pytest.raises(ValueError, match="states what changed"):
        _open(base).gov.invalidate(a.governed.task_id, reason="")
    assert (base / "log.jsonl").read_bytes() == head


# ---- after: not reused, not re-admitted, not rewritten ----------------------

def test_a_stale_result_is_not_reused_and_not_readmitted(world):
    (a, chk) = world["a"]
    base = _copy("after")
    g0 = _open(base)
    ident = g0.authority.get(a.record_id).evidence["run_identity"]
    assert a.record_id in g0.reusable(ident)                  # control
    g0.invalidate_task(a.governed.task_id, reason="r")
    g = _open(base)
    assert a.record_id not in g.reusable(ident)
    # I3: STALE returns only through re-verification -- and re-verification
    # asks the origin again, at its own position, where the run is
    # INVALIDATED.
    g.authority.transition(record_id=a.record_id, dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)
    with pytest.raises(TransitionError,
                       match="not an artefact of any VERIFIED governed "
                             "model run"):
        g.authority.transition(
            record_id=a.record_id, dst=State.VERIFIED, actor=REVIEWER_ID,
            role=Role.VERIFIER,
            evidence={"verification_report": chk.report_sha256})


def test_nothing_already_written_changes(world):
    """The admission stays in the history, where it stands; the staleness
    is appended after it, and replay walks both."""
    (a, chk) = world["a"]
    base = _copy("prefix")
    before = (base / "log.jsonl").read_bytes()
    admitted = _open(base).authority.get(a.record_id).updated_seq
    _open(base).invalidate_task(chk.governed.task_id, reason="r")
    after = (base / "log.jsonl").read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    recon = rc.reconstruct(EventLog(base / "log.jsonl"),
                           evidence=EvidenceStore(base / "evidence"))
    history = recon.records[a.record_id]["history"]
    assert history[-2:][0] == (admitted, "VERIFIED")
    assert history[-1][1] == "STALE" and history[-1][0] > admitted


# ---- an invalidation not followed through -----------------------------------

def test_an_interrupted_invalidation_is_settled(world):
    """The task's move and the STALE records are separate appends. A writer
    that stopped between them -- here, a SYSTEM move written straight to the
    log -- leaves the result VERIFIED on a dead origin; ``settle`` finishes
    it, citing the task, and settling again changes nothing."""
    (a, chk), (b, _) = world["a"], world["b"]
    base = _copy("interrupted")
    EventLog(base / "log.jsonl").append(
        actor="system", action="task.transition",
        target=chk.governed.task_id,
        payload={"task_id": chk.governed.task_id, "src": "VERIFIED",
                 "dst": "INVALIDATED", "role": "SYSTEM",
                 "note": "moved, and then the writer stopped"})
    assert _replayed(base, a.record_id) == {a.record_id: State.VERIFIED}
    (inv,) = _open(base).settle()
    assert inv.origin == f"task:{chk.governed.task_id}"
    assert inv.plan.affected == (a.record_id,)
    assert _replayed(base, a.record_id, b.record_id) == {
        a.record_id: State.STALE, b.record_id: State.VERIFIED}
    head = (base / "log.jsonl").read_bytes()
    assert _open(base).settle() == ()
    assert (base / "log.jsonl").read_bytes() == head
