"""A scientific-model result, from proposal to authority, with no self-certification.

The whole Phase-2 target line, run for real on thermal 1D:

    propose -> governed run (policy, capability, bounded subprocess,
    evidence, re-execution by a separate verifier) -> ResultBundle
    -> independent check as a separate governed task by a different
    executor -> VerificationResult as evidence -> authority decision by a
    reviewer who neither proposed nor executed anything.

Then every way that line could be short-circuited is tried: the executor
deciding, the proposer verifying, a report about another bundle, a FAIL, a
check that ran the producer's code, a check fed bytes other than the cited
ones. Each must end short of VERIFIED.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.authority import Role, State, TransitionError  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import (  # noqa: E402
    CHECK_WORKER_ID, REVIEWER_ID, TOOL_RUN, TOOL_RUN_2D, GovernedModelRuns,
    ModelRunRefused,
)
from qta_agent.governed_stage10 import (  # noqa: E402
    SUBMITTER_ID, WORKER_ID,
)
from qta_agent.store import StoreError  # noqa: E402
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_models"
CHECK = "thermal_1d.reduction_2d_radial_disabled"
PARAMS = {"n_cells": 60, "n_eval": 20}


@pytest.fixture(scope="module")
def world():
    base = ROOT / WS / "shared"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    log = EventLog(base / "log.jsonl")
    ev = EvidenceStore(base / "evidence")
    g = GovernedModelRuns(root=ROOT, log=log, evidence=ev)
    run = g.propose(model_id="thermal.conduction_1d", model_version="1.0.0",
                    parameters=PARAMS, out_dir=f"{WS}/shared/run")
    chk = g.check(run, check_id=CHECK, out_dir=f"{WS}/shared/check")
    yield g, run, chk, log, ev
    if base.exists():
        shutil.rmtree(base)


def test_the_governed_run_and_the_check_both_verified(world):
    g, run, chk, _, _ = world
    assert run.governed.state is TaskState.VERIFIED
    assert chk.governed.state is TaskState.VERIFIED
    assert g.authority.get(run.record_id).state is State.PROPOSED


def test_the_report_is_about_this_bundle_and_passes(world):
    g, run, chk, _, _ = world
    report = json.loads(g.evidence.get(chk.report_sha256))
    assert report["subject_digest"] == run.bundle_digest
    assert report["status"] == "PASS"
    assert report["independence"] == "DIFFERENT_DISCRETIZATION"
    assert report["verifier_id"] == CHECK_WORKER_ID


def test_the_executor_cannot_decide(world):
    g, run, chk, _, _ = world
    for actor in (WORKER_ID, CHECK_WORKER_ID, SUBMITTER_ID):
        with pytest.raises(ModelRunRefused, match="cannot decide"):
            g.decide(run, chk, reviewer=actor)


def test_the_proposer_cannot_verify_through_the_store(world):
    g, run, _, _, _ = world
    with pytest.raises((TransitionError, StoreError)):
        g.authority.transition(record_id=run.record_id, dst=State.VERIFIED,
                               actor=SUBMITTER_ID, role=Role.VERIFIER)


def test_the_check_is_not_run_by_the_producer(world):
    g, run, _, _, _ = world
    with pytest.raises(ModelRunRefused, match="proposed or ran"):
        g.check(run, check_id=CHECK, out_dir=f"{WS}/shared/x",
                worker=WORKER_ID)


def _fork(world, name):
    """A fresh record for the same bundle, so a decision on it does not
    consume the shared one."""
    g, run, chk, _, _ = world
    rid = f"{run.record_id}-{name}"
    g.authority.create(record_id=rid, kind="scientific_result",
                       proposer=SUBMITTER_ID,
                       evidence={"result_bundle": run.bundle_sha256})
    return dataclasses.replace(run, record_id=rid)


def _tampered(world, **changes):
    g, _, chk, _, _ = world
    rep = {**json.loads(g.evidence.get(chk.report_sha256)), **changes}
    sha = g.evidence.put(json.dumps(rep, sort_keys=True).encode())
    return dataclasses.replace(chk, report_sha256=sha)


@pytest.mark.parametrize("changes,why", [
    ({"status": "FAIL"}, "reported FAIL"),
    ({"status": "NOT_RUN"}, "reported NOT_RUN"),
    ({"subject_digest": "c" * 64}, "different bundle"),
    ({"verifier_implementation_digest": None}, "producer's own code"),
    ({"check_type": "INVARIANT"}, "not an independent check"),
    ({"producer_implementation_digest": "d" * 64}, "another producer"),
])
def test_a_report_that_does_not_support_the_result_rejects_it(
        world, changes, why):
    g, run, _, _, _ = world
    forked = _fork(world, why.split()[-1].replace("'", ""))
    if changes.get("verifier_implementation_digest", 1) is None:
        bundle = json.loads(g.evidence.get(run.bundle_sha256))
        changes = {"verifier_implementation_digest":
                   bundle["implementation_digest"]}
    rec = g.decide(forked, _tampered(world, **changes))
    assert rec.state is State.REJECTED
    reason = json.loads(g.evidence.get(rec.evidence["rejection_reason"]))
    assert any(why in p for p in reason["problems"]), reason


def test_the_real_report_verifies_the_result(world):
    """This consumes the shared record; every test after it works on a
    fork."""
    g, run, chk, log, _ = world
    rec = g.decide(run, chk)
    assert rec.state is State.VERIFIED
    assert rec.evidence["verification_report"] == chk.report_sha256
    assert rec.evidence["result_bundle"] == run.bundle_sha256
    # VERIFIED is not canonical; nothing on this path promotes.
    assert run.record_id not in g.authority.canonical()


def test_no_executor_wrote_an_authority_event(world):
    """The model's run and the check's run never touched authority: every
    record event in the log is the proposer's create or a reviewer's
    transition."""
    _, _, _, log, _ = world
    report, events = log.read_verified()
    assert report.ok
    actors = {(e.action, e.actor) for e in events
              if e.action.startswith("record.")}
    assert actors, "no authority event at all: the test would be vacuous"
    assert {a for _, a in actors} <= {SUBMITTER_ID, REVIEWER_ID}
    assert not {a for _, a in actors} & {WORKER_ID, CHECK_WORKER_ID}


def test_a_check_fed_other_bytes_refuses(world, tmp_path):
    g, run, _, _, _ = world
    wrong = dataclasses.replace(run, bundle_sha256="e" * 64)
    with pytest.raises(ModelRunRefused, match="check task was"):
        g.check(wrong, check_id=CHECK, out_dir=f"{WS}/shared/wrong")


def test_an_unadmitted_check_is_refused(world):
    g, run, _, _, _ = world
    with pytest.raises(ModelRunRefused):
        g.check(run, check_id="no.such.check", out_dir=f"{WS}/shared/none")


def test_the_bundle_evidence_is_a_simulation_result_with_no_verdict(world):
    g, run, _, _, _ = world
    bundle = json.loads(g.evidence.get(run.bundle_sha256))
    assert bundle["observation_kind"] == "SIMULATION_RESULT"
    assert not {"verified", "status", "accepted"} & set(bundle)


def test_a_bundle_whose_invariant_failed_is_rejected(world):
    """A PASS report about a bundle that does not hold its own invariants is
    not enough: the reviewer reads the bundle too."""
    g, run, chk, _, _ = world
    from qta_agent.canonical import digest
    bundle = json.loads(g.evidence.get(run.bundle_sha256))
    bundle["invariants"][0]["holds"] = False
    sha = g.evidence.put(json.dumps(bundle, sort_keys=True).encode())
    forked = dataclasses.replace(_fork(world, "invariant"),
                                 bundle_sha256=sha,
                                 bundle_digest=digest(bundle))
    report = _tampered(world, subject_digest=digest(bundle))
    rec = g.decide(forked, report)
    assert rec.state is State.REJECTED
    reason = json.loads(g.evidence.get(rec.evidence["rejection_reason"]))
    assert any("invariants not holding" in p for p in reason["problems"])


def test_a_direct_store_transition_cannot_bypass_the_content_rule(world):
    """The residual the last tranche recorded: a reviewer writing VERIFIED
    to the store directly, citing a report that exists and says FAIL. The
    store now reads the report itself."""
    g, run, _, _, _ = world
    forked = _fork(world, "direct")
    g.authority.transition(record_id=forked.record_id, dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)
    fail = _tampered(world, status="FAIL")
    with pytest.raises(TransitionError, match="does not support"):
        g.authority.transition(record_id=forked.record_id,
                               dst=State.VERIFIED, actor=REVIEWER_ID,
                               role=Role.VERIFIER,
                               evidence={"verification_report":
                                         fail.report_sha256})


def test_a_failed_run_is_never_proposed(world):
    """With reuse on, invalid parameters stop at the identity task; with it
    off, at the model run. Either way nothing is proposed."""
    g, _, _, _, _ = world
    before = set(g.authority.all_records())
    with pytest.raises(ModelRunRefused, match="identity task was"):
        g.propose(model_id="thermal.conduction_1d", model_version="1.0.0",
                  parameters={"n_cells": 5},
                  out_dir=f"{WS}/shared/badrun-reuse")
    with pytest.raises(ModelRunRefused, match="governed run was"):
        g.propose(model_id="thermal.conduction_1d", model_version="1.0.0",
                  parameters={"n_cells": 5}, reuse=False,
                  out_dir=f"{WS}/shared/badrun-noreuse")
    assert set(g.authority.all_records()) == before


def test_the_record_cites_the_identity_its_bundle_carries(world):
    from qta_agent.canonical import digest
    g, run, _, _, _ = world
    rec = g.authority.get(run.record_id)
    bundle = json.loads(g.evidence.get(run.bundle_sha256))
    identity = bundle["provenance"]["run_identity"]
    assert rec.evidence["run_identity"] == digest(identity)
    assert json.loads(g.evidence.get(rec.evidence["run_identity"])) == \
        identity
    assert identity["parameter_digest"] and identity["implementation_digest"]


# ---- a second model through the same path (Phase 4) -------------------------

T2D = {"model_id": "thermal.conduction_2d_axisymmetric",
       "model_version": "1.0.0"}
SMALL_2D = {"n_r": 16, "n_z": 24, "n_eval": 10}


@pytest.fixture(scope="module")
def world2d():
    base = ROOT / WS / "twod"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    log = EventLog(base / "log.jsonl")
    g = GovernedModelRuns(root=ROOT, log=log,
                          evidence=EvidenceStore(base / "evidence"))
    yield g, log
    if base.exists():
        shutil.rmtree(base)


def test_thermal_2d_through_the_governed_path_is_verified(world2d):
    """The second model reaches VERIFIED by the same line, checked by the 3D
    solver, and the history names the tool that ran IT."""
    g, log = world2d
    run = g.propose(**T2D, parameters={**SMALL_2D,
                                       "lateral_boundary": "adiabatic"},
                    out_dir=f"{WS}/twod/adiabatic")
    chk = g.check(run, check_id="thermal_2d.reduction_3d_adiabatic_lateral",
                  out_dir=f"{WS}/twod/check")
    report = json.loads(g.evidence.get(chk.report_sha256))
    assert report["status"] == "PASS", report
    assert g.decide(run, chk).state is State.VERIFIED
    _, events = log.read_verified()
    tools = {e.payload["tool_id"] for e in events
             if e.action == "task.create"}
    assert TOOL_RUN_2D in tools and TOOL_RUN not in tools


def test_a_cold_contact_2d_result_has_no_check_and_is_not_verified(world2d):
    """The production lateral boundary has no independent check here. The
    check says NOT_RUN, and NOT_RUN is not support."""
    g, _ = world2d
    run = g.propose(**T2D, parameters=SMALL_2D, out_dir=f"{WS}/twod/cold")
    chk = g.check(run, check_id="thermal_2d.reduction_3d_adiabatic_lateral",
                  out_dir=f"{WS}/twod/coldcheck")
    rec = g.decide(run, chk)
    assert rec.state is State.REJECTED
    reason = json.loads(g.evidence.get(rec.evidence["rejection_reason"]))
    assert any("reported NOT_RUN" in p for p in reason["problems"])


def test_a_model_with_no_governed_tool_is_refused(world):
    g, _, _, log, _ = world
    head = log.verify().head_seq
    with pytest.raises(ModelRunRefused, match="no governed tool"):
        g.propose(model_id="no.such.model", model_version="1.0.0",
                  parameters={}, out_dir=f"{WS}/shared/nosuch")
    assert log.verify().head_seq == head, "a refused model left history"

