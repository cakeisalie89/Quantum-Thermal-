"""Gate C acceptance: a scripted agent completes a multi-step scientific
objective through the real governed path, survives a crash at any point in a
step and a reset of every in-memory object, and resumes from the log.

The objective: does the transient slab's mid-plane temperature stay below a
threshold at the documented point? The agent proposes a structured
hypothesis, then a run of the model, then a conclusion. The run is executed
as the agent's governed task, checked by an INDEPENDENT implementation (the
method of lines) run by a different executor, and decided by the reviewer.
The hypothesis is decided by the ledger from the verified result bundle. The
agent never says whether its hypothesis holds.

The agent is a SCRIPT. Nothing here says how a language model behaves.
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

from agent_substrate import inference as I  # noqa: E402
from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.agents import (  # noqa: E402
    AgentRole, PrincipalKind, identity,
)
from agent_substrate.capability import CapabilityLedger  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_model import GovernedModelRuns  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyStore, document, rule,
)
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402

WS = "verification/stage10/_pytest_research"
AGENT, MODEL, PROVIDER = "ai-researcher", "fixture-model", "fixture"
FIXTURE = ROOT / "integrations" / "proposals" / "recorded_fixture.jsonl"
SLAB = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
POLICY = document(policy_id="research.default", version=1, rules=(rule(
    rule_id="r", effect=Effect.ALLOW, actions=("infer",), subjects=(ANY,),
    roles=(ANY,), resources=(ANY,)),))


def hypothesis(threshold=1000.0, op="<"):
    return {"hypothesis_id": "h1",
            "statement": f"the mid-plane temperature stays {op} "
                         f"{threshold} K at the documented point",
            "prediction": {"model_id": "thermal.slab_transient",
                           "quantity": "T_at_half", "op": op,
                           "value": threshold, "unit": "K"},
            "depends_on": []}


class World:
    """One log and its stores. ``controller()`` builds every in-memory
    object afresh from the log: that is what a restart, or a reset of the
    context, leaves a controller with."""

    def __init__(self, name, script, *, max_steps=6, max_calls=8):
        self.base = ROOT / WS / name
        if self.base.exists():
            shutil.rmtree(self.base)
        self.base.mkdir(parents=True)
        self.ws = f"{WS}/{name}"
        self.script = script
        self.provider = I.FixtureProvider(PROVIDER, self._counted)
        self.calls = 0
        log = self.log = EventLog(self.base / "log.jsonl")
        g = GovernedModelRuns(root=ROOT, log=log,
                              evidence=EvidenceStore(self.base / "evidence"))
        g.gov.agents.register(identity(agent_id=AGENT, instance_id=AGENT,
                                       kind=PrincipalKind.AGENT,
                                       roles={AgentRole.PROPOSER}),
                              by="system")
        CapabilityLedger(log).load().anoint(actor="scheduler")
        PolicyStore(log).load().publish(POLICY, actor="owner")
        gw = self._gateway()
        gw.publish_budget(I.Budget(
            budget_id="research", provider=PROVIDER, models=(MODEL,),
            callers=(AGENT,), max_calls=50, max_cost_micro=10**9,
            max_input_tokens=10**8, max_output_tokens=10**7, window_s=3600,
            max_calls_per_window=50, max_input_bytes=100_000,
            max_request_output_tokens=500, max_output_bytes=8_000,
            max_timeout_s=60, max_attempts=2,
            prices={MODEL: {"input": 1000, "output": 1000}}),
            actor="scheduler")
        R.ResearchLedger(log).load().register(
            objective_id="slab-q", statement="does T_at_half stay below "
            "the threshold at the documented point?", owner="owner",
            max_steps=max_steps, max_inference_calls=max_calls)

    def _counted(self, req, prompt, n):
        self.calls += 1
        return self.script(req, prompt, n)

    def _gateway(self):
        return I.InferenceGateway(
            self.log, transcripts=EvidenceStore(self.base / "transcripts"),
            policies=PolicyStore(self.log).load(),
            provider=self.provider).load()

    def controller(self, fault=None):
        runs = GovernedModelRuns(root=ROOT, log=self.log,
                                 evidence=EvidenceStore(self.base /
                                                        "evidence"))
        gw = self._gateway()
        ledger = R.ResearchLedger(self.log, evidence=runs.evidence).load()
        ingress = PR.ProposalIngress(self.log,
                                     inference_check=gw.binding_problems)
        return L.ResearchController(
            log=self.log, ledger=ledger, gateway=gw, ingress=ingress,
            runs=runs, policy_identity="research.default@1",
            policy_digest=POLICY.digest(), budget_id="research",
            agent_id=AGENT, model=MODEL, out_dir=self.ws,
            retrieval=PR.assemble_context(
                "slab", retrieve=lambda q, k: {"hits": []}), fault=fault)

    def events(self):
        return self.log.read_verified()[1]

    def executions(self) -> dict:
        out: dict = {}
        for e in self.events():
            if e.action == "task.execution" and \
                    (e.payload or {}).get("phase", "complete") == "complete":
                out[e.target] = out.get(e.target, 0) + 1
        return out

    def summary(self) -> dict:
        led = R.ResearchLedger(self.log).load()
        return {"objective": led.get("slab-q").status,
                "basis": led.get("slab-q").basis,
                "h1": led.get("h1").status,
                "steps": [(s["act"], s["outcome"])
                          for _, s in sorted(led.steps("slab-q").items())],
                "records": sum(e.action == "record.create"
                               for e in self.events()),
                "observations": len(led.of("slab-q", "observation"))}

    def clean(self):
        shutil.rmtree(self.base, ignore_errors=True)


@pytest.fixture(scope="module")
def reference():
    w = World("reference", L.scripted_agent(hypothesis=hypothesis(),
                                            run=SLAB["request"]))
    report = w.controller().run("slab-q")
    yield w, report
    w.clean()


def test_a_scripted_agent_completes_a_multi_step_objective(reference):
    w, report = reference
    assert report.status == "CONCLUDED"
    assert [(s.action, s.outcome) for s in report.steps] == [
        ("HYPOTHESIS", "RECORDED"), ("MODEL_RUN", "OBSERVED"),
        ("CONCLUDE", "CONCLUDED")]
    s = w.summary()
    assert s["h1"] == "SUPPORTED" and s["basis"] == ("h1",)
    assert s["observations"] == 1
    assert w.calls == 3
    # the reason is the record's verdict, not the agent's summary
    # (D-2026-178): the summary stays in the agent's receipt
    assert R.ResearchLedger(w.log).load().get("slab-q").reason == \
        "h1 is SUPPORTED on the verified record"


def test_the_verdict_comes_from_the_verified_bundle(reference):
    w, _ = reference
    led = R.ResearchLedger(w.log).load()
    obs = led.get(led.of("slab-q", "observation")[0])
    rec = next(e for e in w.events() if e.action == "record.transition"
               and e.payload.get("dst") == "VERIFIED")
    assert obs.body["record_id"] == rec.payload["record_id"]
    assert led.get("h1").basis == (obs.body["observation_id"],)
    value = obs.body["values"]["T_at_half"]
    assert value["unit"] == "K" and value["value"] + value["resolution"] \
        < 1000.0


def test_both_readers_agree_on_the_research_record(reference):
    w, _ = reference
    sub = reconstruct_subsystems(w.log)
    assert sub.anomalies == [], sub.anomalies
    led = R.ResearchLedger(w.log).load()
    for rid in ("slab-q", "h1"):
        assert sub.research[rid]["status"] == led.get(rid).status


CRASH_POINTS = ("before_infer", "after_infer", "after_receive", "after_run",
                "after_check", "after_decide", "before_step_record")


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_a_crash_anywhere_in_a_step_resumes_without_doing_anything_twice(
        reference, point):
    """Crash at ``point`` in the MODEL_RUN step (step 2), then restart with
    every in-memory object rebuilt from the log. The end state is the
    uninterrupted run's, and no governed task executed twice."""
    ref, _ = reference
    fired = []

    def fault(p):
        if p == point and not fired and len(
                R.ResearchLedger(w.log).load().steps("slab-q")) == 1:
            fired.append(p)
            raise L.Crash(p)

    w = World(f"crash-{point}", L.scripted_agent(hypothesis=hypothesis(),
                                                 run=SLAB["request"]))
    try:
        with pytest.raises(L.Crash):
            w.controller(fault=fault).run("slab-q")
        assert fired == [point]
        report = w.controller().run("slab-q")
        assert report.status == "CONCLUDED"
        got, want = w.summary(), ref.summary()
        assert got == want, (got, want)
        assert set(w.executions().values()) == {1}, w.executions()
        assert len(w.executions()) == len(ref.executions())
        assert w.calls == ref.calls, "a model call was repeated"
        assert reconstruct_subsystems(w.log).anomalies == []
    finally:
        w.clean()


def test_the_gateway_s_transcripts_are_not_charged_to_a_tool():
    """D-2026-185. The gateway writes its transcript store while another
    controller's tool runs. Inside the tool's scope, a change there was
    charged to that tool and its run refused. The controller registers the
    store as a supervisor's, as the log and the evidence store are."""
    w = World("transcripts-store", L.scripted_agent(
        hypothesis=hypothesis(), run=SLAB["request"]))
    try:
        c = w.controller()
        rel = (w.base / "transcripts").resolve().relative_to(ROOT.resolve())
        assert str(rel) in c.runs.gov._supervisor_paths()
    finally:
        w.clean()


def test_the_loop_is_bounded_by_the_objective_s_ceilings():
    w = World("bounded", L.scripted_agent(hypothesis=hypothesis(),
                                          run=SLAB["request"]),
              max_steps=1)
    try:
        report = w.controller().run("slab-q")
        assert report.status == "EXHAUSTED"
        assert len(report.steps) == 1
    finally:
        w.clean()


def _always(kind, request):
    def script(req, prompt, n):
        return I.ProviderReply("OK", json.dumps({
            "kind": kind, "request": request, "provider": None,
            "model": None, "rationale": "scripted"}).encode(),
            {"input_tokens": 10, "output_tokens": 10},
            {"provider": None, "model": None})
    return script


def test_a_kind_with_no_tool_contract_executes_nothing():
    w = World("refused", _always("CODE_PATCH", {
        "patch_sha256": "a" * 64, "files": ["agent_substrate/authority.py"],
        "summary": "relax the gate"}), max_steps=2)
    try:
        report = w.controller().run("slab-q")
        assert [s.outcome for s in report.steps] == ["REFUSED", "REFUSED"]
        assert report.status == "EXHAUSTED"
        assert w.executions() == {}
    finally:
        w.clean()


@pytest.mark.parametrize("kind, request_", [
    ("POLICY_UPDATE", {"policy_id": "research.default", "allow": "*"}),
    ("CONCLUDE", {"hypothesis_id": "h1"}),            # a key missing
])
def test_an_answer_that_is_no_proposal_is_a_refused_step(kind, request_):
    """D-2026-178. Raised instead of recorded, the answer was re-read from
    the record on every restart and held the objective open for good. The
    first answer is not a proposal; the rest are the reference script's."""
    legit = L.scripted_agent(hypothesis=hypothesis(), run=SLAB["request"])
    bad = _always(kind, request_)

    def script(req, prompt, n):
        return (bad if w.calls == 1 else legit)(req, prompt, n)

    w = World(f"noproposal-{kind}", script)
    try:
        report = w.controller().run("slab-q")
        first = report.steps[0]
        assert (first.action, first.outcome) == ("PROPOSAL", "REFUSED")
        assert report.status == "CONCLUDED"
        assert w.controller().run("slab-q").steps == []      # restart: done
        assert R.ResearchLedger(w.log).load().steps("slab-q")[1][
            "proposal_id"] is None
    finally:
        w.clean()


def test_an_unassigned_agent_asks_nothing_and_writes_nothing():
    """Gate E: an objective that names its assignees is not stepped by any
    other agent's controller -- refused before a context is built or a
    call is made, so it spends neither the objective's steps nor its
    calls."""
    w = World("unassigned", L.scripted_agent(hypothesis=hypothesis(),
                                             run=SLAB["request"]))
    try:
        R.ResearchLedger(w.log).load().register(
            objective_id="theirs", statement="someone else's",
            owner="owner", max_steps=3, max_inference_calls=3,
            assignees=["ai-colleague"])
        before = w.log.head().seq
        with pytest.raises(R.ResearchError, match="not assigned"):
            w.controller().step("theirs")
        assert w.log.head().seq == before and w.calls == 0
    finally:
        w.clean()


def test_a_question_for_a_person_blocks_the_objective():
    w = World("blocked", _always("DIAGNOSTIC", {
        "question": "which boundary condition did the owner intend?",
        "subject": "thermal.slab_transient@1.0.0"}))
    try:
        report = w.controller().run("slab-q")
        assert report.status == "BLOCKED"
        assert "missing information" in R.ResearchLedger(
            w.log).load().get("slab-q").reason
    finally:
        w.clean()


def test_a_refuted_hypothesis_is_concluded_as_refuted():
    w = World("refuted", L.scripted_agent(hypothesis=hypothesis(250.0),
                                          run=SLAB["request"]))
    try:
        report = w.controller().run("slab-q")
        assert report.status == "CONCLUDED"
        assert w.summary()["h1"] == "REFUTED"
    finally:
        w.clean()


def test_an_agent_cannot_conclude_on_an_undecided_hypothesis():
    """It skips the run and concludes at once: the ledger refuses, the step
    records the refusal, and the objective is not concluded."""
    def script(req, prompt, n):
        state = next(json.loads(i["text"]) for i in json.loads(prompt)[
            "items"] if i["tier"] == "TASK_STATE")
        if not state["hypotheses"]:
            return L.scripted_agent(hypothesis=hypothesis(),
                                    run=SLAB["request"])(req, prompt, n)
        return _always("CONCLUDE", {"hypothesis_id": "h1",
                                    "summary": "it obviously holds"})(
            req, prompt, n)

    w = World("eager", script, max_steps=3)
    try:
        report = w.controller().run("slab-q")
        assert [s.outcome for s in report.steps][1:] == ["REFUSED",
                                                          "REFUSED"]
        assert report.status == "EXHAUSTED"
    finally:
        w.clean()


def test_invalidating_the_result_makes_the_conclusion_stale(reference):
    """Independent of the shared reference: its own world, invalidated."""
    w = World("invalidated", L.scripted_agent(hypothesis=hypothesis(),
                                              run=SLAB["request"]))
    try:
        c = w.controller()
        c.run("slab-q")
        task = next(e.target for e in w.events()
                    if e.action == "task.create"
                    and "slab_transient" in json.dumps(e.payload))
        c.runs.invalidate_task(task, reason="the boundary condition was "
                               "mistyped in the documentation")
        moved = R.ResearchLedger(w.log).load().propagate()
        assert moved[-1] == "slab-q"
        assert w.summary()["objective"] == "STALE"
        assert reconstruct_subsystems(w.log).anomalies == []
    finally:
        w.clean()


def test_a_result_that_is_not_admitted_yields_no_observation(monkeypatch):
    """With only an analytic reference check, the reviewer REJECTS the run
    (the admission policy wants an independent implementation), and the
    controller reads nothing from it: the hypothesis stays OPEN."""
    monkeypatch.setitem(L.CHECKS, "thermal.slab_transient",
                        "thermal.slab_series")
    w = World("rejected", L.scripted_agent(hypothesis=hypothesis(),
                                           run=SLAB["request"]), max_steps=2)
    try:
        report = w.controller().run("slab-q")
        assert [(s.action, s.outcome) for s in report.steps] == [
            ("HYPOTHESIS", "RECORDED"), ("MODEL_RUN", "REJECTED")]
        led = R.ResearchLedger(w.log).load()
        assert led.of("slab-q", "observation") == ()
        assert led.get("h1").status == "OPEN"
    finally:
        w.clean()
