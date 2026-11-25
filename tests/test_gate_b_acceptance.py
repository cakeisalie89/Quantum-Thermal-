"""Gate B acceptance: a fixture model completes a governed proposal cycle.

The whole path, on one log, through the real components:

1. a context is built under the policy in force and its manifest recorded;
2. the prompt is rendered from it;
3. the gateway reserves the call against a budget the root issuer granted,
   dispatches it, and settles it;
4. the model's answer becomes a proposal-envelope/2, which the ingress
   receives only after checking it against the recorded inference;
5. the proposal is submitted to the governed model path as its agent, run,
   checked by a different executor and decided by a reviewer who is none of
   them.

Then the acceptance criteria, each checked from the record rather than taken
from the run: the context is reconstructable exactly from the log and the
transcripts; resources were bounded; a crash mid-call is recovered without
double-spending; and nothing the model said promoted anything. The model is
a FIXTURE. No language model was called, and this test says nothing about
how one behaves.
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
from agent_substrate.agents import (  # noqa: E402
    AgentRole, PrincipalKind, identity,
)
from agent_substrate.authority import State  # noqa: E402
from agent_substrate.capability import CapabilityLedger  # noqa: E402
from agent_substrate.context import (  # noqa: E402
    ContextBuilder, Tier, manifest_from_record, record_context,
)
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_model import (  # noqa: E402
    GovernedModelRuns, ModelRunRefused,
)
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyStore, document, rule,
)
from agent_substrate.reconstruct import (  # noqa: E402
    reconstruct, reconstruct_subsystems,
)

WS = "verification/stage10/_pytest_gate_b"
AGENT, MODEL, PROVIDER = "ai-proposer-gate-b", "fixture-model", "fixture"
FIXTURE = ROOT / "integrations" / "proposals" / "recorded_fixture.jsonl"
SLAB = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])


@pytest.fixture(scope="module")
def cycle():
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    log = EventLog(base / "log.jsonl")
    g = GovernedModelRuns(root=ROOT, log=log,
                          evidence=EvidenceStore(base / "evidence"))
    g.gov.agents.register(identity(agent_id=AGENT, instance_id=AGENT,
                                   kind=PrincipalKind.AGENT,
                                   roles={AgentRole.PROPOSER}), by="system")
    # the governed path mints its capabilities as "scheduler"; on this log
    # that actor is the root issuer, and so it is who grants spending
    CapabilityLedger(log).load().anoint(actor="scheduler")
    policies = PolicyStore(log).load()
    policies.publish(document(policy_id="inference.default", version=1,
                              rules=(rule(rule_id="r", effect=Effect.ALLOW,
                                          actions=("infer",),
                                          subjects=(ANY,), roles=(ANY,),
                                          resources=(ANY,)),)),
                     actor="owner")

    b = ContextBuilder(task_id="gate-b", purpose="propose a model run",
                       policy_identity="inference.default@1",
                       at_seq=log.head().seq)
    b.add(item_id="instr", tier=Tier.OWNER_INSTRUCTION,
          text="propose one run of the transient slab at a documented point")
    b.add(item_id="policy", tier=Tier.SYSTEM_POLICY,
          text="inference.default@1")
    b.add(item_id="state", tier=Tier.TASK_STATE, text="no runs yet")
    ctx = b.build(budget_bytes=10_000)
    record_context(log, ctx.manifest, actor="context-builder")
    prompt = I.render_prompt(ctx)

    seen: list = []

    def model(req, prompt_bytes, attempt):
        seen.append(prompt_bytes)
        return I.ProviderReply("OK", json.dumps(SLAB).encode(),
                               {"input_tokens": 400, "output_tokens": 120},
                               {"provider": None, "model": None})

    transcripts = EvidenceStore(base / "transcripts")
    gw = I.InferenceGateway(log, transcripts=transcripts, policies=policies,
                            provider=I.FixtureProvider(PROVIDER, model)
                            ).load()
    gw.publish_budget(I.Budget(
        budget_id="gate-b", provider=PROVIDER, models=(MODEL,),
        callers=(AGENT,), max_calls=4, max_cost_micro=50_000,
        max_input_tokens=100_000, max_output_tokens=10_000, window_s=60,
        max_calls_per_window=4, max_input_bytes=20_000,
        max_request_output_tokens=500, max_output_bytes=8_000,
        max_timeout_s=30, max_attempts=2,
        prices={MODEL: {"input": 1_000_000, "output": 4_000_000}}),
        actor="scheduler")
    req = I.InferenceRequest.for_prompt(
        prompt, context=ctx, budget_id="gate-b", caller=AGENT, model=MODEL,
        idempotency_key="gate-b-1", max_output_tokens=500,
        max_output_bytes=8000, timeout_s=10, max_attempts=2)
    out = gw.infer(req, prompt)

    retrieval = PR.assemble_context("slab", retrieve=lambda q, k: {"hits": []})
    rec = I.envelope_from_inference(out, context=retrieval, agent_id=AGENT,
                                    manifest_digest=ctx.manifest.digest())
    ing = PR.ProposalIngress(log, inference_check=gw.binding_problems)
    receipt = ing.receive(rec)
    run = ing.submit(receipt.envelope.proposal_id, g, out_dir=f"{WS}/run")
    chk = g.check(run, check_id="thermal.slab_series", out_dir=f"{WS}/check")
    yield {"log": log, "g": g, "gw": gw, "req": req, "out": out,
           "receipt": receipt, "run": run, "chk": chk, "ctx": ctx,
           "transcripts": transcripts, "seen": seen, "prompt": prompt}
    if base.exists():
        shutil.rmtree(base)


def test_the_model_s_answer_ran_under_governance_as_its_agent(cycle):
    rec = cycle["g"].authority.get(cycle["run"].record_id)
    assert rec.proposer == AGENT and rec.state is State.PROPOSED
    env = cycle["receipt"].envelope
    assert env.record["schema"] == PR.SCHEMA_V2
    assert env.record["inference"]["request_id"] == cycle["req"].request_id


def test_the_context_is_reconstructed_exactly_from_the_record(cycle):
    """From the log and the transcript store alone: the request the
    proposal names, the manifest that request names, and the prompt bytes,
    which show exactly what that manifest records."""
    log, env = cycle["log"], cycle["receipt"].envelope
    _, events = log.read_verified()
    rid = env.record["inference"]["request_id"]
    reserve = next(e for e in events if e.action == I.ACT_RESERVE
                   and e.target == rid and e.payload["granted"])
    request = reserve.payload["request"]
    built = next(e for e in events if e.action == "context.build"
                 and e.payload["manifest_digest"]
                 == request["context_manifest_digest"])
    manifest = manifest_from_record(built.payload["manifest"])
    prompt = cycle["transcripts"].get(request["prompt_sha256"])
    assert I.prompt_problems(prompt, manifest) == []
    assert prompt == cycle["seen"][0], "the bytes held are the bytes sent"
    # and the policy the context was built under was the one in force when
    # the call was reserved
    assert manifest.policy_identity == "inference.default@1"


def test_resources_were_bounded(cycle):
    held = cycle["gw"].budget_usage("gate-b")
    assert held["calls"] == 1
    assert held["cost_micro"] == I.cost_micro(
        {MODEL: {"input": 1_000_000, "output": 4_000_000}}, MODEL, 400, 120)
    assert held["cost_micro"] <= 50_000


def test_nothing_the_model_said_promoted_anything(cycle):
    g, run, chk = cycle["g"], cycle["run"], cycle["chk"]
    with pytest.raises(ModelRunRefused, match="cannot decide"):
        g.decide(run, chk, reviewer=AGENT)
    rec = g.decide(run, chk)
    # the analytic check is evidence but not what the admission policy
    # admits on, so the reviewer rejects -- and cites nothing the model said
    assert rec.state is State.REJECTED
    cited = set(g.authority.get(run.record_id).evidence.values())
    out = cycle["out"]
    assert out.response_sha256 not in cited
    assert cycle["req"].prompt_sha256 not in cited
    assert not g.evidence.contains(out.response_sha256)


def test_both_readers_agree_and_find_nothing(cycle):
    log = cycle["log"]
    assert reconstruct(log).unauthorized == []
    sub = reconstruct_subsystems(log)
    assert sub.anomalies == [], sub.anomalies
    b = sub.inference_budgets["gate-b"]
    held = cycle["gw"].budget_usage("gate-b")
    assert (b["calls"], b["cost"]) == (held["calls"], held["cost_micro"])


def test_a_crash_mid_call_is_recovered_without_double_spending(cycle):
    """A second request is reserved and dispatched and the gateway dies. A
    new gateway, once the lease has run out, settles it LOST at its
    reservation; the retry is attempt 2, and the history still reads
    clean."""
    log, gw, ctx = cycle["log"], cycle["gw"], cycle["ctx"]
    req = I.InferenceRequest.for_prompt(
        cycle["prompt"], context=ctx, budget_id="gate-b", caller=AGENT,
        model=MODEL, idempotency_key="gate-b-2", max_output_tokens=500,
        max_output_bytes=8000, timeout_s=10, max_attempts=2)
    att = gw._reserve(req)
    gw._dispatch(att)
    clock = [gw.clock() + req.timeout_s + I.LEASE_GRACE_S + 1]
    later = I.InferenceGateway(log, transcripts=cycle["transcripts"],
                               policies=gw.policies, provider=gw.provider,
                               clock=lambda: clock[0]).load()
    [lost] = later.recover()
    assert lost.outcome == I.LOST and lost.cost_micro == att.reserved_cost
    again = later.infer(req, cycle["prompt"])
    assert again.ok and again.attempt == 2
    assert later.attempts(req.request_id) == ((1, I.LOST), (2, I.OK))
    assert reconstruct_subsystems(log).anomalies == []
