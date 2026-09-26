"""A verified scientific result is reused by its evidence, never by its name.

A proposal first obtains, from a governed tool, the run identity it would
have here. A prior result with that identity is reused only if the authority
layer VERIFIED or PROMOTED it AND its evidence re-derives now: the bundle and
its artefacts resolve, the bundle's own provenance carries the identity, and
the cited report still supports the bundle. Each exclusion is exercised with
a record that passes every other condition, against a control that the
genuine record is reusable.
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

from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import (  # noqa: E402
    REVIEWER_ID, TOOL_RUN, GovernedModelRuns, ModelRunRefused,
)
from qta_agent.governed_stage10 import SUBMITTER_ID  # noqa: E402
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_models_reuse"
CHECK = "thermal_1d.reduction_2d_radial_disabled"
MODEL = {"model_id": "thermal.conduction_1d", "model_version": "1.0.0"}
PARAMS = {"n_cells": 60, "n_eval": 20}


def _model_runs(log) -> int:
    report, events = log.read_verified()
    assert report.ok
    return sum(1 for e in events if e.action == "task.create"
               and e.payload.get("tool_id") == TOOL_RUN)


@pytest.fixture(scope="module")
def world():
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    log = EventLog(base / "log.jsonl")
    ev = EvidenceStore(base / "evidence")
    g = GovernedModelRuns(root=ROOT, log=log, evidence=ev)
    first = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/first")
    chk = g.check(first, check_id=CHECK, out_dir=f"{WS}/check")
    assert g.decide(first, chk).state is State.VERIFIED
    yield g, first, chk, log, ev
    if base.exists():
        shutil.rmtree(base)


def test_the_first_proposal_had_nothing_to_reuse(world):
    _, first, _, _, _ = world
    assert first.reused_from == ""
    assert first.governed.state is TaskState.VERIFIED


def test_an_identical_proposal_reuses_the_verified_result(world):
    g, first, _, log, _ = world
    runs = _model_runs(log)
    again = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/again")
    assert again.reused_from == first.record_id
    assert again.governed is None
    assert again.bundle_sha256 == first.bundle_sha256
    assert again.bundle_digest == first.bundle_digest
    assert _model_runs(log) == runs, "reuse ran the model anyway"


def test_the_identity_task_agrees_with_the_bundle(world):
    """The identity a proposal computes before running is the identity the
    run records in its own bundle: one definition, two call sites."""
    g, first, _, _, _ = world
    ident = g.identity(**MODEL, parameters=PARAMS, out_dir=f"{WS}/ident")
    assert ident == g.authority.get(first.record_id).evidence["run_identity"]


def test_other_parameters_are_recomputed(world):
    g, first, _, log, _ = world
    runs = _model_runs(log)
    other = g.propose(**MODEL, parameters={**PARAMS, "n_cells": 61},
                      out_dir=f"{WS}/other")
    assert other.reused_from == ""
    assert other.governed.state is TaskState.VERIFIED
    assert other.bundle_digest != first.bundle_digest
    assert _model_runs(log) == runs + 1


def test_reuse_can_be_declined_and_an_identical_rerun_is_a_new_claim(world):
    g, first, _, log, _ = world
    runs = _model_runs(log)
    fresh = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/fresh",
                      reuse=False)
    assert fresh.reused_from == "" and fresh.governed is not None
    assert _model_runs(log) == runs + 1
    # Same bytes (the run is byte-identical), a separate record: one record
    # per governed task, so the rerun cannot collide with the first by content.
    assert fresh.bundle_digest == first.bundle_digest
    assert fresh.record_id != first.record_id
    assert g.authority.get(fresh.record_id).state is State.PROPOSED


def test_a_reused_run_is_not_checked_or_decided_again(world):
    g, _, chk, _, _ = world
    again = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/again2")
    assert again.reused_from
    with pytest.raises(ModelRunRefused, match="already decided"):
        g.check(again, check_id=CHECK, out_dir=f"{WS}/recheck")
    with pytest.raises(ModelRunRefused, match="already decided"):
        g.decide(again, chk)


# ---- what is NOT reusable ---------------------------------------------------

def _doc(ev, sha) -> dict:
    return json.loads(ev.get(sha))


def _put(ev, doc) -> str:
    return ev.put(json.dumps(doc, sort_keys=True).encode())


def _record(world, name, *, bundle=None, report=None, state=State.VERIFIED,
            identity=None):
    """A scientific_result record taken to ``state`` through the store,
    citing ``identity`` as its run identity. Every argument left out is the
    genuine first result's."""
    g, first, chk, _, ev = world
    rec0 = g.authority.get(first.record_id)
    bundle = bundle if bundle is not None else _doc(ev, first.bundle_sha256)
    report = report if report is not None else _doc(ev, chk.report_sha256)
    report = {**report, "subject_digest": digest(bundle)}
    rid = f"{first.record_id}-{name}"
    g.authority.create(record_id=rid, kind="scientific_result",
                       proposer=SUBMITTER_ID,
                       evidence={"result_bundle": _put(ev, bundle),
                                 "run_identity":
                                     identity or rec0.evidence["run_identity"]})
    if state is State.PROPOSED:
        return rid
    g.authority.transition(record_id=rid, dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)
    if state is State.UNDER_REVIEW:
        return rid
    dst = State.VERIFIED if state is State.VERIFIED else State.REJECTED
    key = ("verification_report" if dst is State.VERIFIED
           else "rejection_reason")
    g.authority.transition(record_id=rid, dst=dst, actor=REVIEWER_ID,
                           role=Role.VERIFIER,
                           evidence={key: _put(ev, report)})
    return rid


def _ident(world) -> str:
    g, first, _, _, _ = world
    return g.authority.get(first.record_id).evidence["run_identity"]


def test_control_the_genuine_record_is_reusable(world):
    g, first, _, _, _ = world
    assert first.record_id in g.reusable(_ident(world))


@pytest.mark.parametrize("state", [State.PROPOSED, State.UNDER_REVIEW,
                                   State.REJECTED])
def test_an_undecided_or_rejected_result_is_not_reused(world, state):
    g = world[0]
    rid = _record(world, f"state-{state.value.lower()}", state=state)
    assert g.authority.get(rid).state is state
    assert rid not in g.reusable(_ident(world))


def test_a_record_citing_an_identity_its_bundle_does_not_carry(world):
    """The record says the identity matches; the bundle's own provenance
    says otherwise. The bundle is believed, not the citation -- and the
    record is VERIFIED, so only this check excludes it."""
    g, first, _, _, ev = world
    bundle = _doc(ev, first.bundle_sha256)
    lie = json.loads(json.dumps(bundle))
    lie["provenance"]["run_identity"]["parameter_digest"] = "a" * 64
    rid = _record(world, "forged-identity", bundle=lie)
    assert g.authority.get(rid).state is State.VERIFIED
    assert rid not in g.reusable(_ident(world))


def _move(g, rid, dst, actor, role, key, **kw):
    ev = {key: g.evidence.put(json.dumps({"why": dst.value}).encode())}
    if dst is State.PROMOTED:
        ev = {"verification_report":
              g.authority.get(rid).evidence["verification_report"],
              "policy_id": "p"}
    g.authority.transition(record_id=rid, dst=dst, actor=actor, role=role,
                           evidence=ev, **kw)


@pytest.mark.parametrize("steps", [
    [(State.REVOKED, "result-promoter", Role.PROMOTER, "revocation_reason")],
    [(State.STALE, "system", Role.SYSTEM, "invalidated_by")],
    [(State.PROMOTED, "result-promoter", Role.PROMOTER, ""),
     (State.SUPERSEDED, "result-promoter", Role.PROMOTER, "superseded_by")],
    [(State.PROMOTED, "result-promoter", Role.PROMOTER, ""),
     (State.REVOKED, "result-promoter", Role.PROMOTER, "revocation_reason")],
], ids=["revoked", "stale", "superseded", "promoted-then-revoked"])
def test_a_result_withdrawn_after_verification_is_not_reused(world, steps):
    """The case only the state filter catches: the record WAS verified, so
    it still cites a report that supports its bundle, and its evidence is
    intact -- but authority has since been withdrawn from it."""
    g = world[0]
    rid = _record(world, "withdrawn-" + "-".join(s[0].value for s in steps))
    for dst, actor, role, key in steps:
        _move(g, rid, dst, actor, role, key,
              **({"policy_id": "p"} if dst is State.PROMOTED else {}))
    rec = g.authority.get(rid)
    assert rec.state is steps[-1][0]
    from qta_agent.result_rules import record_problems
    assert record_problems(rec.evidence, g.evidence.get) == [], (
        "the report no longer supports the bundle: the test would not "
        "isolate the state filter")
    assert rid not in g.reusable(_ident(world))


def test_a_record_whose_citation_disagrees_with_its_bundle(world):
    """The other direction: the bundle carries the identity, the record
    cites another. Both must name it."""
    g = world[0]
    rid = _record(world, "other-citation", identity=g.evidence.put(b"{}"))
    assert g.authority.get(rid).state is State.VERIFIED
    assert rid not in g.reusable(_ident(world))


def test_a_promoted_result_is_reusable(world):
    g, _, chk, _, _ = world
    rid = _record(world, "promoted")
    rec = g.authority.transition(
        record_id=rid, dst=State.PROMOTED, actor="result-promoter",
        role=Role.PROMOTER, policy_id="p",
        evidence={"verification_report":
                  g.authority.get(rid).evidence["verification_report"],
                  "policy_id": "p"})
    assert rec.state is State.PROMOTED
    assert rid in g.reusable(_ident(world))


def test_a_bundle_that_is_not_a_bundle_is_skipped_not_fatal(world):
    """A record citing JSON that is not a bundle is excluded, and the search
    goes on to the genuine one rather than raising."""
    g, first, _, _, ev = world
    rec0 = g.authority.get(first.record_id)
    # Sorts before the genuine record, so the search meets it first.
    rid = "result-!notabundle"
    assert rid < first.record_id
    g.authority.create(record_id=rid, kind="scientific_result",
                       proposer=SUBMITTER_ID,
                       evidence={"result_bundle": _put(ev, ["not", "a"]),
                                 "run_identity": rec0.evidence["run_identity"]})
    # Forced to VERIFIED behind the store's back. The store would refuse
    # this edge now; a record replayed from a log written before the content
    # rule existed is not re-judged on replay, and can be in this state.
    g.authority._records[rid] = dataclasses.replace(
        g.authority._records[rid], state=State.VERIFIED)
    found = g.reusable(_ident(world))
    assert rid not in found and first.record_id in found


def test_a_result_whose_artefact_no_longer_resolves(world):
    g, first, _, _, ev = world
    bundle = json.loads(json.dumps(_doc(ev, first.bundle_sha256)))
    assert bundle["artifacts"], "no artefact: the test would be vacuous"
    bundle["artifacts"][0]["digest"] = "f" * 64
    rid = _record(world, "lost-artefact", bundle=bundle)
    assert g.authority.get(rid).state is State.VERIFIED
    assert rid not in g.reusable(_ident(world))


def test_a_result_whose_report_no_longer_resolves(world):
    """The decision is re-derived too. The report the record cites is
    removed from the store after the record was VERIFIED."""
    g, _, chk, _, ev = world
    report = {**_doc(ev, chk.report_sha256), "limitations_note": "unique"}
    rid = _record(world, "lost-report", report=report)
    rec = g.authority.get(rid)
    assert rec.state is State.VERIFIED
    ev._blob_path(rec.evidence["verification_report"]).unlink()
    assert rid not in g.reusable(_ident(world))


def test_a_result_whose_bundle_no_longer_resolves(world):
    g, first, _, _, ev = world
    bundle = {**_doc(ev, first.bundle_sha256), "limitations_note": "unique"}
    rid = _record(world, "lost-bundle", bundle=bundle)
    ev._blob_path(g.authority.get(rid).evidence["result_bundle"]).unlink()
    assert rid not in g.reusable(_ident(world))


def test_another_kind_is_never_reused(world):
    g, first, _, _, _ = world
    rec0 = g.authority.get(first.record_id)
    g.authority.create(record_id="not-a-result", kind="stage10_artifact",
                       proposer=SUBMITTER_ID, evidence=dict(rec0.evidence))
    g.authority.transition(record_id="not-a-result", dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)
    g.authority.transition(record_id="not-a-result", dst=State.VERIFIED,
                           actor=REVIEWER_ID, role=Role.VERIFIER,
                           evidence={"verification_report":
                                     rec0.evidence["verification_report"]})
    assert "not-a-result" not in g.reusable(_ident(world))


def _copy(name) -> GovernedModelRuns:
    """The shared history and evidence, copied, so a test can damage the
    evidence without damaging the world the others use."""
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    shutil.copytree(ROOT / WS / "evidence", base / "evidence")
    shutil.copy(ROOT / WS / "log.jsonl", base / "log.jsonl")
    return GovernedModelRuns(root=ROOT, log=EventLog(base / "log.jsonl"),
                             evidence=EvidenceStore(base / "evidence"))


def test_a_result_whose_artefact_bytes_changed_is_not_reused(world):
    """Present is not intact: the artefact's file is still where it was,
    with other bytes in it. Every read re-hashes."""
    g, first, _, _, _ = world
    g2 = _copy("copy-corrupt")
    art = _doc(g2.evidence, first.bundle_sha256)["artifacts"][0]["digest"]
    path = g2.evidence._blob_path(art)
    path.unlink()
    path.write_bytes(b"not the temperature field")
    assert first.record_id not in g2.reusable(_ident(world))
    # control: the undamaged history still reuses it
    assert first.record_id in g.reusable(_ident(world))


def test_a_run_reused_after_its_evidence_went_is_recomputed(world):
    """End to end on a copy: remove the genuine result's artefact from the
    store and the same proposal runs the model again."""
    g, first, _, _, _ = world
    g2 = _copy("copy-lost")
    bundle = _doc(g2.evidence, first.bundle_sha256)
    for a in bundle["artifacts"]:
        g2.evidence._blob_path(a["digest"]).unlink()
    runs = _model_runs(g2.gov.log)
    out = g2.propose(**MODEL, parameters=PARAMS,
                     out_dir=f"{WS}/copy-lost/run")
    assert out.reused_from == ""
    assert _model_runs(g2.gov.log) == runs + 1
    # control: before the removal, this history would have reused it
    assert first.record_id in g.reusable(_ident(world))


def test_the_identity_is_this_model_these_parameters_this_environment(world):
    """What "the same run" means, pinned: the model and version, its
    implementation digest now, the VALIDATED parameters (defaults filled
    in), and the interpreter and numeric libraries."""
    from scientific.models.thermal_1d import Thermal1DModel
    from scientific.run_identity import environment_digest
    g, first, _, _, ev = world
    ident = _doc(ev, g.authority.get(first.record_id)
                 .evidence["run_identity"])
    model = Thermal1DModel()
    assert ident["model_id"] == MODEL["model_id"]
    assert ident["model_version"] == MODEL["model_version"]
    assert ident["implementation_digest"] == model.implementation_digest()
    assert ident["parameter_digest"] == digest(model.validate(PARAMS))
    assert ident["parameter_digest"] != digest(PARAMS)
    assert ident["environment_digest"] == environment_digest()
