"""One view of governed execution per replay: counted, not timed.

Every scientific admission is re-decided on replay (D-2026-83), and origin
is part of it: was the report captured by a governed verification that
stands VERIFIED at the transition, the bundle by a governed model run. Each
such question built its own view -- a verified read of the whole log and a
task fold -- twice per admission. A load was therefore O(n*k) in admitted
results, and nothing measured it (plan 9.7, "Replay cost").

Now a load, a snapshot restore, a catch-up, a reuse search and an
invalidation cascade each build ONE view and answer every origin question
from it, and the independent reader builds one task replay per
reconstruction. The guards count the work: questions asked against views
built. A clock would pass a regression on a fast machine and fail a healthy
run on a busy one; a counter does neither. Each guard's probe is shown to
SEE the regression it exists for -- sharing switched off, the count is the
number of questions.
"""
from __future__ import annotations

import shutil
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import reconstruct as rc  # noqa: E402
from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.checkpoint import CheckpointStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import (  # noqa: E402
    GovernedModelRuns, GovernedOrigins,
)
from qta_agent.store import AuthorityStore  # noqa: E402

WS = "verification/stage10/_pytest_origin_view"
CHECK = "thermal_1d.reduction_2d_radial_disabled"
MODEL = {"model_id": "thermal.conduction_1d", "model_version": "1.0.0"}
PROMOTER = "result-promoter"
#: Admissions in the world: three results into VERIFIED, two of them on
#: into PROMOTED.
ADMISSIONS = 5


@pytest.fixture(scope="module")
def world():
    """Three decided results -- two with one run identity, so a reuse
    search has more than one candidate -- and two of them promoted."""
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    (base / "genuine").mkdir(parents=True)
    g = GovernedModelRuns(root=ROOT,
                          log=EventLog(base / "genuine" / "log.jsonl"),
                          evidence=EvidenceStore(base / "genuine" /
                                                 "evidence"))
    out = {}
    for name, params in (("a", {"n_cells": 60, "n_eval": 20}),
                         ("a2", {"n_cells": 60, "n_eval": 20}),
                         ("b", {"n_cells": 40, "n_eval": 16})):
        run = g.propose(**MODEL, parameters=params, reuse=False,
                        out_dir=f"{WS}/genuine/{name}-run")
        chk = g.check(run, check_id=CHECK,
                      out_dir=f"{WS}/genuine/{name}-check")
        assert g.decide(run, chk).state is State.VERIFIED
        out[name] = (run, chk)
    for name in ("a", "b"):
        rid = out[name][0].record_id
        g.authority.transition(
            record_id=rid, dst=State.PROMOTED, actor=PROMOTER,
            role=Role.PROMOTER, policy_id="p",
            evidence={"verification_report":
                      g.authority.get(rid).evidence["verification_report"],
                      "policy_id": "p"})
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


def _unshared(monkeypatch):
    """The regression: every question builds its own view again."""
    monkeypatch.setattr(GovernedOrigins, "shared",
                        lambda self: nullcontext(self))


# ---- the store's load ------------------------------------------------------

def test_a_load_builds_one_view_for_every_admission(world):
    g = _open(_copy("load"))
    assert g.origins.questions == ADMISSIONS
    assert g.origins.views_built == 1


def test_the_load_probe_sees_one_view_per_question(world, monkeypatch):
    """The same probe, sharing switched off: it reads k views where the
    fix reads one. A counter that could not tell these apart would pass
    the regression it exists to catch."""
    _unshared(monkeypatch)
    g = _open(_copy("load-unshared"))
    assert g.origins.questions == ADMISSIONS
    assert g.origins.views_built == 2 * ADMISSIONS


def test_one_question_builds_one_view(world):
    """Even outside a load: a question asks after the report's producers
    and the bundle's, and both come from the same view."""
    (a, chk) = world["a"]
    g = _open(_copy("one-question"))
    before = g.origins.views_built
    assert g.origins.problems(report_sha=chk.report_sha256,
                              bundle_sha=a.bundle_sha256,
                              proposer=a.submitter, actor="someone",
                              before_seq=None) == []
    assert g.origins.views_built - before == 1


# ---- snapshot restore and catch-up -----------------------------------------

def test_a_restore_builds_one_view_for_every_readmission(world, tmp_path):
    g = _open(_copy("restore"))
    cps = CheckpointStore(tmp_path / "cp")
    g.authority.checkpoint(cps, blobs=g.evidence)
    origins = GovernedOrigins(g.gov)
    AuthorityStore.load_from(EventLog(ROOT / WS / "restore" / "log.jsonl"),
                             cps, blobs=g.evidence, evidence=g.evidence,
                             origins=origins)
    # Three records hold an admission in the snapshot (two PROMOTED, one
    # VERIFIED); each is decided again, from one view.
    assert origins.questions == 3
    assert origins.views_built == 1


def test_a_catch_up_builds_one_view_for_what_it_folds(world):
    """Another process's admissions, folded in by catch-up."""
    base = _copy("catch-up")
    origins = GovernedOrigins(_open(base).gov)
    store = AuthorityStore(EventLog(base / "log.jsonl"),
                           evidence=EvidenceStore(base / "evidence"),
                           origins=origins)
    store._fold_new()
    assert origins.questions == ADMISSIONS
    assert origins.views_built == 1


def test_an_anchored_catch_up_builds_one_view_for_what_it_folds(world):
    """The live path: a store already loaded folds only what is new, from
    its anchor. Here another writer admitted three results after it loaded
    -- a second record on ``a``'s evidence, verified and promoted, and
    ``a2`` promoted."""
    (a, chk), (a2, _) = world["a"], world["a2"]
    base = _copy("anchored")
    reader = _open(base)
    writer = _open(base)
    rid = "result-second-claim"
    writer.authority.create(
        record_id=rid, kind="scientific_result", proposer=a.submitter,
        evidence={"result_bundle": a.bundle_sha256,
                  "run_identity": writer.authority.get(
                      a.record_id).evidence["run_identity"]},
        policy_id="scientific_result.admission/1")
    writer.authority.transition(record_id=rid, dst=State.UNDER_REVIEW,
                                actor="model-result-reviewer",
                                role=Role.VERIFIER)
    writer.authority.transition(
        record_id=rid, dst=State.VERIFIED, actor="model-result-reviewer",
        role=Role.VERIFIER,
        evidence={"verification_report": chk.report_sha256})
    for r in (rid, a2.record_id):
        writer.authority.transition(
            record_id=r, dst=State.PROMOTED, actor=PROMOTER,
            role=Role.PROMOTER, policy_id="p",
            evidence={"verification_report": chk.report_sha256,
                      "policy_id": "p"})
    q0, v0 = reader.origins.questions, reader.origins.views_built
    assert reader.authority._anchor is not None
    reader.authority.catch_up()
    assert reader.authority.get(rid).state is State.PROMOTED
    assert reader.origins.questions - q0 == 3
    assert reader.origins.views_built - v0 == 1


# ---- reuse and the cascade --------------------------------------------------

def test_reuse_asks_every_candidate_of_one_view(world):
    (a, _), (a2, _) = world["a"], world["a2"]
    g = _open(_copy("reuse"))
    ident = g.authority.get(a.record_id).evidence["run_identity"]
    assert g.authority.get(a2.record_id).evidence["run_identity"] == ident
    q0, v0 = g.origins.questions, g.origins.views_built
    assert g.reusable(ident) == sorted([a.record_id, a2.record_id])
    assert g.origins.questions - q0 == 2
    assert g.origins.views_built - v0 == 1


def test_the_cascade_asks_every_result_of_one_view(world):
    """``a`` and ``a2`` were checked to the same report bytes, so
    invalidating ``a``'s check asks after both results -- and both keep an
    origin in ``a2``'s check. Two questions, one view."""
    (a, chk), (a2, _) = world["a"], world["a2"]
    g = _open(_copy("cascade"))
    q0, v0 = g.origins.questions, g.origins.views_built
    inv = g.invalidate_task(chk.governed.task_id, reason="r")
    assert sorted(inv.kept) == sorted([a.record_id, a2.record_id])
    assert g.origins.questions - q0 == 2
    assert g.origins.views_built - v0 == 1


# ---- the independent reader -------------------------------------------------

def test_the_independent_reader_replays_tasks_once(world):
    base = _copy("reader")
    recon = rc.reconstruct(EventLog(base / "log.jsonl"),
                           evidence=EvidenceStore(base / "evidence"))
    assert recon.admissions_decided == ADMISSIONS
    assert recon.task_replays == 1
    assert not recon.unauthorized and not recon.anomalies


def test_the_answers_do_not_depend_on_sharing(world, monkeypatch):
    """Sharing changes the cost and nothing else: the same history loads to
    the same records, admissions and bases either way."""
    shared = _open(_copy("same-a")).authority.snapshot()
    _unshared(monkeypatch)
    unshared = _open(_copy("same-b")).authority.snapshot()
    assert shared["records"] == unshared["records"]
