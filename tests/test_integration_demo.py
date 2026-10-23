"""Section 11: the integration demonstration and its adversarial variants.

The demonstration runs once per module. Every lifecycle step must hold and
every attempt must be refused, and the run must end as the committed
results record (``tools/integration_demo.py --check`` asks the same in CI).

A refusal that would have happened anyway proves nothing about the control
that claims it. So for the variants whose control can be switched off by
itself, the control is switched off and the attack must succeed:

V1   the evidence store's re-hashing read
V2   the deterministic judgement of a claim
V3   the context's policy freshness at the gateway (and the independent
     process still refuses the run the stale call led to)
V11  an observation's freshness under its owner's policy
V12  the release gate
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import adversarial_eval as E  # noqa: E402
import integration_demo as D  # noqa: E402
import supply_chain as SC  # noqa: E402

from agent_substrate import claim_check as C  # noqa: E402
from agent_substrate import freshness as F  # noqa: E402
from agent_substrate import inference as I  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_model import GovernedModelRuns  # noqa: E402
from agent_substrate.governed_model import ModelRunRefused  # noqa: E402
from agent_substrate.idempotency import IdempotencyLedger  # noqa: E402
from agent_substrate.policy import PolicyStore, document  # noqa: E402


@pytest.fixture(scope="module")
def report():
    return D.run()


def _accepted(got: dict) -> list:
    return [a["attempt"] for a in got["attempts"] if a["accepted"]]


def test_every_step_of_the_lifecycle_holds(report):
    steps = report["lifecycle"]["steps"]
    assert [s["step"] for s in steps] == list(range(1, 21))
    assert [s["name"] for s in steps if not s["held"]] == []


def test_every_variant_is_refused_and_names_what_refused_it(report):
    assert len(report["variants"]) == 12
    for v in report["variants"]:
        assert _accepted(v) == [], v["id"]
        assert all(a["refused_by"] for a in v["attempts"]), v["id"]


def test_the_variants_cover_the_directive_s_twelve_categories(report):
    assert [v["category"] for v in report["variants"]] == [
        "forged evidence", "false semantic support", "stale policy",
        "compromised actor", "poisoned summary", "prompt injection",
        "concurrent writers", "lost inference response",
        "invalidated scientific result", "altered history and witness",
        "expired current-world observation",
        "unauthorized release attempt"]


def test_what_the_design_cannot_establish_is_reported_as_it_happened(
        report):
    cases = {(v["id"], c["case"]): c["outcome"] for v in report["variants"]
             for c in v["cannot_establish"]}
    # a wrong admission passes a false claim; a forged history passes a
    # trust root of the attacker's witnesses alone: shown, not hidden
    assert cases[("V2", "a claim true of a wrongly admitted reading and "
                        "false of the world")] == "SUPPORTED, ESTABLISHED"
    assert cases[("V10", "a trust root whose every witness is the "
                         "attacker's")] == "ACCEPTED"
    assert cases[("V9", "the withdrawn decision's proof")] == \
        "STILL VERIFIES"


def test_the_run_ends_as_the_committed_results_record(report):
    recorded = json.loads(D.RESULTS.read_text(encoding="utf-8"))
    assert D.projection(recorded) == D.projection(report)


def test_an_orphan_of_the_bind_race_is_not_a_second_execution(
        monkeypatch):
    """Two submissions of one proposal can race between the idempotency
    lookup and the bind. The loser creates a task and cancels it before
    anything is queued (D-2026-117). Forced here by hiding the binding
    from a second submission's lookup. V7 must count one execution, not
    two creations (D-2026-190). Only the check before the task is created
    misses the binding; the bind, decided under the writer lock, finds
    it."""
    w = D.Demo("orphan-race")
    try:
        w.step()
        w.colleague_hypothesis(E.hypothesis(D.H2, depends_on=[D.H1]))
        w.step()                              # the run, submitted once
        (env,) = w.ingress.pending(lambda pid: False)
        real, missed = IdempotencyLedger.lookup, []

        def first_misses(self, **kw):
            if not missed:
                missed.append(kw)
                return None
            return real(self, **kw)
        monkeypatch.setattr(IdempotencyLedger, "lookup", first_misses)
        runs = GovernedModelRuns(root=ROOT, log=w.log, evidence=w.evidence)
        w.ingress.submit(env.proposal_id, runs,
                         out_dir=f"{w.ws}/{D.OBJECTIVE}/step-2/run")
        events = w.events()
        created, executed = D._run_tasks(events)
        assert (len(created), len(executed)) == (2, 1)
        (orphan,) = set(created) - set(executed)
        assert any(e.action == "task.transition" and e.target == orphan
                   and e.payload["dst"] == "CANCELLED"
                   and "first" in e.payload.get("note", "")
                   for e in events)
    finally:
        w.clean()


def test_the_model_is_never_claimed(report):
    assert report["agent"].startswith("SCRIPTED")
    assert report["real_model"].startswith("NOT_RUN")


# --------------------------------------------------------- positive controls

def test_control_v1_without_the_re_hash_the_forgery_reads_back(monkeypatch):
    monkeypatch.setattr(EvidenceStore, "get",
                        lambda self, dg: self._blob_path(dg).read_bytes())
    assert "the forged bytes read back as the bundle" in \
        _accepted(D.forged_evidence())


def test_control_v2_without_the_judgement_the_opposite_is_established(
        monkeypatch):
    monkeypatch.setattr(C, "judge", lambda claim, obs, convert=None: (
        C.Verdict.SUPPORTED, "control", tuple(o["observation_id"]
                                              for o in obs)))
    assert "the opposite comparison established" in \
        _accepted(D.false_semantic_support())


def test_control_v3_without_policy_freshness_the_stale_call_is_granted(
        monkeypatch):
    """And the second line holds: the independent process restates the
    rule, finds the granted reservation, and the run it led to is
    rejected."""
    monkeypatch.setattr(I.InferenceGateway, "_policy_problem",
                        lambda self, *a, **k: None)
    w = D.Demo("stale-policy-control")
    try:
        w.step()
        v2 = document(policy_id=E.POLICY.policy_id, version=2,
                      rules=E.POLICY.rules)
        PolicyStore(w.log).load().publish(v2, actor="owner")
        published = w.log.head().seq
        with pytest.raises(ModelRunRefused,
                           match="a context built under a policy no longer "
                                 "in force"):
            w.step()
        assert any(e.action == "inference.reserve" and e.seq > published
                   and e.payload["granted"] for e in w.events())
    finally:
        w.clean()


def test_control_v11_without_freshness_the_expired_reading_decides(
        monkeypatch):
    monkeypatch.setattr(R.ResearchLedger, "_standing",
                        lambda self, oid, at: (F.Standing.FRESH, "control"))
    assert "a verdict decided on the expired reading" in \
        _accepted(D.expired_observation())


def test_control_v12_without_the_gate_the_candidate_is_admitted(
        monkeypatch):
    monkeypatch.setattr(SC, "admit", lambda *a, **k: None)
    assert "a release candidate admitted by the gate" in \
        _accepted(D.release_attempt())
