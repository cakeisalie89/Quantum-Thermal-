"""Replay re-admits scientific authority; it does not remember it.

The store decides a scientific_result's edge into VERIFIED or PROMOTED under
the admission policy of ``qta_agent.result_rules``, with origin asked of the
governed task history. These tests hold REPLAY to the same rule, in two
readers that share no code for it: the store's ``load()`` and the independent
``qta_agent.reconstruct``.

Every forged history here starts from a genuine one -- thermal 1D run and
checked as governed tasks and decided through the store -- and appends to the
log directly, the way a writer that bypasses the store would. Each forgery is
refused by both readers; the genuine history is admitted by both (control).
Evidence that cannot be read is neither: the record keeps its position and
reads UNVERIFIABLE, is not canonical and is not reused.
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
from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.checkpoint import CheckpointStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import (  # noqa: E402
    CHECK_WORKER_ID, MODEL_RUN_TOOLS, REVIEWER_ID, VERIFIER_TOOLS,
    GovernedModelRuns, GovernedOrigins,
)
from qta_agent.governed_stage10 import SUBMITTER_ID  # noqa: E402
from qta_agent.store import AuthorityStore, StoreError  # noqa: E402

WS = "verification/stage10/_pytest_models_replay"
CHECK = "thermal_1d.reduction_2d_radial_disabled"
MODEL = {"model_id": "thermal.conduction_1d", "model_version": "1.0.0"}
PARAMS = {"n_cells": 60, "n_eval": 20}
PROMOTER = "result-promoter"


@pytest.fixture(scope="module")
def world():
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    (base / "genuine").mkdir(parents=True)
    log = EventLog(base / "genuine" / "log.jsonl")
    ev = EvidenceStore(base / "genuine" / "evidence")
    g = GovernedModelRuns(root=ROOT, log=log, evidence=ev)
    run = g.propose(**MODEL, parameters=PARAMS, out_dir=f"{WS}/genuine/run")
    chk = g.check(run, check_id=CHECK, out_dir=f"{WS}/genuine/check")
    assert g.decide(run, chk).state is State.VERIFIED
    yield run, chk
    if base.exists():
        shutil.rmtree(base)


def _copy(name: str) -> Path:
    """The genuine history and its evidence, copied, for one test to damage."""
    src, dst = ROOT / WS / "genuine", ROOT / WS / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copytree(src / "evidence", dst / "evidence")
    shutil.copy(src / "log.jsonl", dst / "log.jsonl")
    return dst


def _open(base: Path) -> GovernedModelRuns:
    """The store's reading of ``base``: evidence and governed origin both
    attached, as production has them."""
    return GovernedModelRuns(root=ROOT, log=EventLog(base / "log.jsonl"),
                             evidence=EvidenceStore(base / "evidence"))


def _second_reader(base: Path, *, evidence: bool = True):
    return rc.reconstruct(EventLog(base / "log.jsonl"),
                          evidence=(EvidenceStore(base / "evidence")
                                    if evidence else None))


def _put(base: Path, doc) -> str:
    return EvidenceStore(base / "evidence").put(
        json.dumps(doc, indent=1, sort_keys=True).encode())


def _doc(base: Path, sha: str) -> dict:
    return json.loads(EvidenceStore(base / "evidence").get(sha))


def _append_transition(base: Path, rid: str, *, src: str, dst: str,
                       actor: str, role: str, evidence: dict,
                       policy="scientific_result.admission/1",
                       policy_id=None) -> None:
    """A record.transition written straight to the log -- the store never
    saw it. Shaped exactly as the store writes one."""
    payload = {"record_id": rid, "src": src, "dst": dst, "role": role,
               "evidence": evidence, "policy_id": policy_id,
               "stale_reason": None, "edge_reason": "forged",
               "idempotency_key": None}
    if policy is not None:
        payload["admission_policy"] = policy
    EventLog(base / "log.jsonl").append(
        actor=actor, action="record.transition", target=rid,
        payload=payload)


def _under_review(base: Path, rid: str, bundle_sha) -> None:
    g = _open(base)
    g.authority.create(record_id=rid, kind=result_rules.KIND,
                       proposer=SUBMITTER_ID,
                       evidence=({"result_bundle": bundle_sha}
                                 if bundle_sha else {}))
    g.authority.transition(record_id=rid, dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)


def _check_task(base: Path, chk) -> str:
    return chk.governed.task_id


def _invalidate(base: Path, task_id: str) -> None:
    """VERIFIED -> INVALIDATED for a task: an edge the task machine permits
    (SYSTEM, an input changed), so the task replay accepts it."""
    EventLog(base / "log.jsonl").append(
        actor="system", action="task.transition", target=task_id,
        payload={"task_id": task_id, "src": "VERIFIED",
                 "dst": "INVALIDATED", "role": "SYSTEM",
                 "note": "an input changed"})


# ---- the control ----------------------------------------------------------

def test_the_genuine_history_is_admitted_by_both_readers(world):
    run, _ = world
    base = _copy("control")
    g = _open(base)
    rec = g.authority.get(run.record_id)
    assert rec.state is State.VERIFIED
    assert rec.admission == result_rules.ADMITTED
    assert rec.admission_basis["policy"] == result_rules.CURRENT_POLICY
    recon = _second_reader(base)
    assert recon.records[run.record_id]["admission"] == "ADMITTED"
    assert not recon.unauthorized and not recon.anomalies
    assert not recon.unverifiable
    assert rc.compare(g.authority, recon) == ()


# ---- forged histories: refused by both ------------------------------------

def _genuine_report(base, chk) -> dict:
    return _doc(base, chk.report_sha256)


def _forged(case: str, base: Path, run, chk) -> dict:
    """Set up ``case`` on ``base`` and return how the forged transition is
    made: {rid, bundle_sha, report_sha, actor, policy}."""
    rid = f"forged-{case}"
    bundle_sha = run.bundle_sha256
    report = _genuine_report(base, chk)
    report_sha = chk.report_sha256
    actor = REVIEWER_ID
    policy = result_rules.CURRENT_POLICY
    if case == "wrong-subject":
        report_sha = _put(base, {**report, "subject_digest": "c" * 64})
    elif case == "fail":
        report_sha = _put(base, {**report, "status": "FAIL"})
    elif case == "not-run":
        report_sha = _put(base, {**report, "status": "NOT_RUN"})
    elif case == "same-producer":
        bundle = _doc(base, run.bundle_sha256)
        report_sha = _put(base, {**report, "verifier_implementation_digest":
                                 bundle["implementation_digest"]})
    elif case == "experimental-validation":
        report_sha = _put(base, {**report,
                                 "establishes": "EXPERIMENTAL_VALIDATION"})
    elif case == "failed-invariant":
        bundle = json.loads(json.dumps(_doc(base, run.bundle_sha256)))
        bundle["invariants"][0]["holds"] = False
        bundle_sha = _put(base, bundle)
        report_sha = _put(base, {**report, "subject_digest": digest(bundle)})
    elif case == "forged-pass":
        # Every content rule holds: the genuine report's own fields, in
        # other bytes. No governed verification captured THESE bytes.
        report_sha = _put(base, report)
        assert report_sha != chk.report_sha256
    elif case == "self-verified":
        # The genuine report, decided by the actor that executed the check.
        actor = CHECK_WORKER_ID
    elif case == "late-evidence":
        # A well-formed PASS attached to the genuine check task AFTER its
        # verdict: the task replay ignores task.evidence, so nothing but
        # this rule stands between it and an admission.
        report_sha = _put(base, report)
        EventLog(base / "log.jsonl").append(
            actor="system", action="task.evidence",
            target=_check_task(base, chk),
            payload={"task_id": _check_task(base, chk),
                     "artifacts": {"late/verification.json": report_sha}})
    elif case == "invalidated-check":
        _invalidate(base, _check_task(base, chk))
    elif case == "unknown-policy":
        policy = "scientific_result.admission/0"
    elif case == "no-policy":
        policy = None
    elif case == "no-bundle":
        # Missing evidence, as a missing CITATION: the record never named a
        # bundle. (A cited digest that does not resolve is the other kind of
        # missing, and is UNVERIFIABLE -- see the tests below.)
        bundle_sha = None
    else:                                                # pragma: no cover
        raise AssertionError(case)
    return {"rid": rid, "bundle_sha": bundle_sha, "report_sha": report_sha,
            "actor": actor, "policy": policy}


FORGERIES = ["wrong-subject", "fail", "not-run", "same-producer",
             "experimental-validation", "failed-invariant", "forged-pass",
             "self-verified", "late-evidence", "invalidated-check",
             "unknown-policy", "no-policy", "no-bundle"]


@pytest.mark.parametrize("case", FORGERIES)
def test_a_forged_admission_is_refused_by_both_readers(world, case):
    run, chk = world
    base = _copy(f"forged-{case}")
    f = _forged(case, base, run, chk)
    _under_review(base, f["rid"], f["bundle_sha"])
    _append_transition(base, f["rid"], src="UNDER_REVIEW", dst="VERIFIED",
                       actor=f["actor"], role="VERIFIER",
                       evidence={"verification_report": f["report_sha"]},
                       policy=f["policy"])
    with pytest.raises(StoreError, match="not admissible"):
        _open(base)
    recon = _second_reader(base)
    refused = [u for u in recon.unauthorized if f["rid"] in u]
    assert refused, (case, recon.unauthorized)
    assert recon.records[f["rid"]]["state"] == "UNDER_REVIEW"
    assert recon.records[f["rid"]]["admission"] is None


def test_each_forgery_fails_for_its_own_reason(world):
    """Not vacuous: the store's refusals name different rules, so no single
    check is carrying every case."""
    run, chk = world
    reasons = {}
    for case in ("fail", "forged-pass", "self-verified", "late-evidence",
                 "invalidated-check", "unknown-policy"):
        base = _copy(f"reason-{case}")
        f = _forged(case, base, run, chk)
        _under_review(base, f["rid"], f["bundle_sha"])
        _append_transition(base, f["rid"], src="UNDER_REVIEW",
                           dst="VERIFIED", actor=f["actor"], role="VERIFIER",
                           evidence={"verification_report": f["report_sha"]},
                           policy=f["policy"])
        with pytest.raises(StoreError) as exc:
            _open(base)
        reasons[case] = str(exc.value)
    assert "reported FAIL" in reasons["fail"]
    for case in ("forged-pass", "late-evidence", "invalidated-check"):
        assert "not an artefact of any VERIFIED governed verification" \
            in reasons[case], case
    assert "executed by the proposer, the decider" in reasons["self-verified"]
    assert "not one this code can decide under" in reasons["unknown-policy"]


def test_a_forged_promotion_is_refused_by_both_readers(world):
    """PROMOTED is held to the same rule: a genuine VERIFIED record promoted
    by a line citing a report nobody's governed verification produced."""
    run, chk = world
    base = _copy("forged-promotion")
    forged = _put(base, _genuine_report(base, chk))
    _append_transition(base, run.record_id, src="VERIFIED", dst="PROMOTED",
                       actor=PROMOTER, role="PROMOTER", policy_id="p",
                       evidence={"verification_report": forged,
                                 "policy_id": "p"})
    with pytest.raises(StoreError, match="not admissible"):
        _open(base)
    recon = _second_reader(base)
    assert any(run.record_id in u and "PROMOTED" in u
               for u in recon.unauthorized)
    assert recon.records[run.record_id]["state"] == "VERIFIED"
    assert recon.canonical_ids() == ()


# ---- as of the transition, not as of the head -----------------------------

def test_a_check_invalidated_later_does_not_rewrite_history(world):
    """Admission is judged at the transition's position. A check task
    invalidated AFTER the result was admitted leaves the older history
    loadable and admitted by both readers -- and the result is no longer
    reused, because reuse asks whether its origin still holds NOW."""
    run, chk = world
    base = _copy("invalidated-later")
    g0 = _open(base)
    ident = g0.authority.get(run.record_id).evidence["run_identity"]
    assert run.record_id in g0.reusable(ident)             # control
    _invalidate(base, _check_task(base, chk))
    g = _open(base)
    rec = g.authority.get(run.record_id)
    assert rec.state is State.VERIFIED
    assert rec.admission == result_rules.ADMITTED
    recon = _second_reader(base)
    assert recon.records[run.record_id]["admission"] == "ADMITTED"
    assert rc.compare(g.authority, recon) == ()
    assert run.record_id not in g.reusable(ident)


# ---- evidence that cannot be read: UNVERIFIABLE, never VERIFIED -----------

def _promote(base: Path, rid: str) -> AuthorityStore:
    g = _open(base)
    g.authority.transition(
        record_id=rid, dst=State.PROMOTED, actor=PROMOTER,
        role=Role.PROMOTER, policy_id="p",
        evidence={"verification_report":
                  g.authority.get(rid).evidence["verification_report"],
                  "policy_id": "p"})
    return g


def test_lost_evidence_is_unverifiable_to_both_readers(world):
    run, chk = world
    base = _copy("lost")
    g = _promote(base, run.record_id)
    # control: promoted on admitted evidence, canonical to both readers
    assert run.record_id in g.authority.canonical()
    assert run.record_id in _second_reader(base).canonical_ids()

    EvidenceStore(base / "evidence")._blob_path(chk.report_sha256).unlink()
    g = _open(base)                      # loads: not a refusal
    rec = g.authority.get(run.record_id)
    assert rec.state is State.PROMOTED
    assert rec.admission == result_rules.UNVERIFIABLE
    assert "does not resolve" in rec.admission_basis["detail"]
    assert run.record_id not in g.authority.canonical()
    assert run.record_id not in g.reusable(rec.evidence["run_identity"])

    recon = _second_reader(base)
    assert recon.records[run.record_id]["state"] == "PROMOTED"
    assert recon.records[run.record_id]["admission"] == "UNVERIFIABLE"
    assert any(run.record_id in u for u in recon.unverifiable)
    assert recon.canonical_ids() == ()
    assert not recon.unauthorized
    assert rc.compare(g.authority, recon) == ()


def test_readers_with_no_evidence_say_unverifiable(world):
    """Neither reader has anything to read: both keep the position and
    neither admits it."""
    run, _ = world
    base = _copy("no-evidence")
    _promote(base, run.record_id)
    store = AuthorityStore(EventLog(base / "log.jsonl")).load()
    rec = store.get(run.record_id)
    assert rec.state is State.PROMOTED
    assert rec.admission == result_rules.UNVERIFIABLE
    assert store.canonical() == {}
    recon = _second_reader(base, evidence=False)
    assert recon.records[run.record_id]["admission"] == "UNVERIFIABLE"
    assert recon.canonical_ids() == ()
    assert rc.compare(store, recon) == ()


def test_a_store_that_cannot_see_governed_execution_does_not_admit(world):
    """Evidence attached, origin view not: the content can be read and
    supports the result, and the store still does not say ADMITTED."""
    run, _ = world
    base = _copy("no-origins")
    store = AuthorityStore(EventLog(base / "log.jsonl"),
                           evidence=EvidenceStore(base / "evidence")).load()
    rec = store.get(run.record_id)
    assert rec.admission == result_rules.UNVERIFIABLE
    assert "governed execution" in rec.admission_basis["detail"]
    # and the second reader, which always has its own task replay, does:
    # the disagreement is what compare() is for.
    diffs = rc.compare(store, _second_reader(base))
    assert [d.field_name for d in diffs] == ["admission"]


# ---- snapshots: re-derived, not inherited ---------------------------------

def test_a_snapshot_restore_re_derives_admission(world, tmp_path):
    run, _ = world
    base = _copy("snapshot")
    g = _open(base)
    cps = CheckpointStore(tmp_path / "cp")
    g.authority.checkpoint(cps, blobs=g.evidence)
    assert g.authority.snapshot()["records"][run.record_id]["admission"] \
        == result_rules.ADMITTED
    log = EventLog(base / "log.jsonl")
    blind = AuthorityStore.load_from(log, cps, blobs=g.evidence,
                                     evidence=g.evidence)
    assert blind.loaded_prefix_verified is False     # the snapshot was used
    assert blind.get(run.record_id).admission == result_rules.UNVERIFIABLE
    seeing = AuthorityStore.load_from(log, cps, blobs=g.evidence,
                                      evidence=g.evidence,
                                      origins=GovernedOrigins(g.gov))
    assert seeing.get(run.record_id).admission == result_rules.ADMITTED


# ---- conformance: two statements of one rule ------------------------------

def test_both_readers_state_the_record_shapes_the_package_writes(world):
    run, chk = world
    base = _copy("shapes")
    bundle, report = _doc(base, run.bundle_sha256), _doc(base,
                                                         chk.report_sha256)
    assert set(bundle) == result_rules.BUNDLE_KEYS \
        == set(rc._SCI_BUNDLE_FIELDS)
    assert set(report) == result_rules.REPORT_KEYS \
        == set(rc._SCI_REPORT_FIELDS)
    assert rc._SCI_BUNDLE_FIELDS == tuple(sorted(rc._SCI_BUNDLE_FIELDS))
    assert rc._SCI_REPORT_FIELDS == tuple(sorted(rc._SCI_REPORT_FIELDS))


def test_both_readers_admit_the_same_tools_and_policies():
    assert rc._SCI_CHECK_TOOLS == VERIFIER_TOOLS
    assert rc._SCI_RUN_TOOLS == MODEL_RUN_TOOLS
    assert rc._SCI_POLICIES == {result_rules.POLICY_V1}
    assert rc._SCI_KIND == result_rules.KIND
    assert result_rules.CURRENT_POLICY in rc._SCI_POLICIES


def test_the_restated_digest_is_the_canonical_one(world):
    run, _ = world
    base = _copy("digest")
    bundle = _doc(base, run.bundle_sha256)
    assert rc._sci_json_digest(bundle) == digest(bundle) == run.bundle_digest


# ---- the auditor ----------------------------------------------------------

def _audit(base: Path, *extra) -> tuple:
    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    import contextlib
    import io

    import audit_log
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = audit_log.main([str(base / "log.jsonl"), "--json", "replay",
                               *extra])
    return code, json.loads(buf.getvalue())


def test_the_auditor_admits_with_the_evidence_and_not_without(world):
    run, _ = world
    base = _copy("audit")
    code, out = _audit(base, "--evidence", str(base / "evidence"))
    assert code == 0, out
    assert out["records"]["admission"] == {run.record_id: "ADMITTED"}
    code, out = _audit(base)
    assert code == 1, "an admission the auditor could not decide passed"
    assert out["records"]["admission"] == {run.record_id: "UNVERIFIABLE"}
    assert out["records"]["unverifiable"]


def test_the_auditor_refuses_a_forged_admission(world):
    run, chk = world
    base = _copy("audit-forged")
    f = _forged("forged-pass", base, run, chk)
    _under_review(base, f["rid"], f["bundle_sha"])
    _append_transition(base, f["rid"], src="UNDER_REVIEW", dst="VERIFIED",
                       actor=f["actor"], role="VERIFIER",
                       evidence={"verification_report": f["report_sha"]})
    code, out = _audit(base, "--evidence", str(base / "evidence"))
    assert code == 1
    assert any(f["rid"] in u for u in out["records"]["unauthorized"])


# ---- the second reader's content rule, alone ------------------------------
#
# End to end, a hand-written report is refused for its ORIGIN before its
# content matters, so the forgeries above cannot tell whether the second
# reader reads content at all. Here origin is satisfied by construction -- a
# task view in which governed tasks captured exactly these documents -- and
# each content rule is broken on its own, beside the store's statement of it.

@pytest.fixture(scope="module")
def pair():
    from scientific.checks.reduction_2d import run_check
    from scientific.model import run_model
    from scientific.models.thermal_1d import Thermal1DModel
    bundle = run_model(Thermal1DModel(), PARAMS)
    report = run_check(bundle, verifier_id="checker")
    return bundle.to_record(), report.to_record()


class _Blobs:
    def __init__(self):
        self.held = {}

    def put(self, doc) -> str:
        import hashlib
        raw = json.dumps(doc, sort_keys=True).encode()
        sha = hashlib.sha256(raw).hexdigest()
        self.held[sha] = raw
        return sha

    def get(self, sha):
        return self.held[sha]


def _view(bundle_sha, report_sha, *, checker="checker", worker="worker"):
    return {
        "run": {"tool_id": "model.thermal.conduction_1d.run",
                "history": [(1, "CREATED"), (6, "VERIFIED")],
                "captured": [(4, bundle_sha)], "executions": [(3, worker)]},
        "chk": {"tool_id": "model.independent_check",
                "history": [(7, "CREATED"), (12, "VERIFIED")],
                "captured": [(10, report_sha)],
                "executions": [(9, checker)]},
    }


def _second_reader_admits(bundle, report, *, at_seq=20, **view):
    blobs = _Blobs()
    b, r = blobs.put(bundle), blobs.put(report)
    return rc._sci_admission(
        record_id="r", evidence={"result_bundle": b,
                                 "verification_report": r},
        policy=result_rules.CURRENT_POLICY, proposer="proposer",
        actor="reviewer", at_seq=at_seq, store=blobs,
        tasks=lambda: _view(b, r, **view))


def test_control_the_second_reader_admits_the_genuine_pair(pair):
    verdict, why = _second_reader_admits(*pair)
    assert verdict == "ADMITTED", why


CONTENT_CHANGES = [
    ("report", {"status": "FAIL"}),
    ("report", {"status": "NOT_RUN"}),
    ("report", {"subject_digest": "c" * 64}),
    ("report", {"check_type": "INVARIANT"}),
    ("report", {"establishes": "EXPERIMENTAL_VALIDATION"}),
    ("report", {"independence": "NONE"}),
    ("report", {"producer_implementation_digest": "d" * 64}),
    ("report", {"verifier_implementation_digest": "PRODUCER"}),
    ("report", {"verifier_implementation_digest": "not-a-digest"}),
    ("report", {"surplus": 1}),
    ("bundle", {"observation_kind": "PROCESSED_OBSERVATION"}),
    ("bundle", {"invariants": []}),
    ("bundle", {"invariants": "BROKEN"}),
    ("bundle", {"surplus": 1}),
]


def _changed(pair, which, change):
    bundle, report = (json.loads(json.dumps(d)) for d in pair)
    if change.get("verifier_implementation_digest") == "PRODUCER":
        change = {"verifier_implementation_digest":
                  bundle["implementation_digest"]}
    if which == "bundle":
        if change.get("invariants") == "BROKEN":
            bundle["invariants"][0]["holds"] = False
        else:
            bundle.update(change)
        report["subject_digest"] = digest(bundle)
    else:
        report.update(change)
    return bundle, report


@pytest.mark.parametrize("which,change", CONTENT_CHANGES)
def test_both_readers_refuse_the_same_content(pair, which, change):
    bundle, report = _changed(pair, which, change)
    assert result_rules.verification_problems(bundle, report), change
    verdict, why = _second_reader_admits(bundle, report)
    assert verdict is None and why.startswith("not so:"), (change, why)


def test_the_second_reader_re_hashes_what_the_store_returns(pair):
    """A store handing back other bytes than the digest names is not
    evidence. Re-hashed here, not trusted to the store."""
    blobs = _Blobs()
    b, r = blobs.put(pair[0]), blobs.put(pair[1])
    blobs.held[r] = json.dumps({**pair[1], "status": "PASS"},
                               indent=2).encode()
    verdict, why = rc._sci_admission(
        record_id="r", evidence={"result_bundle": b,
                                 "verification_report": r},
        policy=result_rules.CURRENT_POLICY, proposer="proposer",
        actor="reviewer", at_seq=20, store=blobs,
        tasks=lambda: _view(b, r))
    assert verdict == "UNVERIFIABLE" and "its own bytes" in why


@pytest.mark.parametrize("view,at_seq,why", [
    ({"checker": "reviewer"}, 20, "someone other than"),
    ({"checker": "proposer"}, 20, "someone other than"),
    ({"checker": "worker"}, 20, "someone other than"),
    ({}, 12, "standing VERIFIED before this transition"),
])
def test_the_second_reader_asks_who_and_when(pair, view, at_seq, why):
    verdict, reason = _second_reader_admits(*pair, at_seq=at_seq, **view)
    assert verdict is None and why in reason, reason
