"""The inference gateway, tried from every side a model call has.

Its contract (B1): every call is reserved, dispatched and settled, in that
order, and every outcome is a record. Its budget (B2): spending is granted by
the root issuer and decided under the writer lock, so a call over budget is
refused before it is sent. Its context binding (B3): a call names the context
manifest its prompt was rendered from, the prompt shows exactly what that
manifest records, and a context built under a policy no longer in force is
refused. Its process boundary (B4): the credential lives in the worker and
nowhere else. Its recovery: an attempt left behind by a crashed gateway is
settled once its lease runs out, NOT_SENT at no cost or LOST at the
reservation. And its replay: a record the gate would have refused, appended
around it, is refused on every load.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import inference as I  # noqa: E402
from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate.authority import (  # noqa: E402
    Role, State, TransitionError, TransitionRequest, check,
)
from agent_substrate.canonical import digest, digest_bytes  # noqa: E402
from agent_substrate.capability import CapabilityLedger  # noqa: E402
from agent_substrate.context import (  # noqa: E402
    ContextBuilder, Tier, record_context,
)
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyStore, document, rule,
)

POLICY = "inference.default"
CALLER = "researcher-a"
MODEL = "fixture-model"
PROVIDER = "fixture-provider"
KEY = "sk-test-" + "Q" * 24

RESPONSE = {"kind": "MODEL_RUN", "provider": "some-provider",
            "model": "some-model",
            "rationale": "run the transient slab at the documented point",
            "request": {"model_id": "thermal.slab_transient",
                        "model_version": "1.0.0", "parameters": {"n": 1}}}
USAGE = {"input_tokens": 100, "output_tokens": 50}


def _body(resp=None) -> bytes:
    return json.dumps(resp or RESPONSE, sort_keys=True).encode()


def ok(resp=None, usage=USAGE, claimed=None):
    return I.ProviderReply("OK", _body(resp), dict(usage) if usage else None,
                           claimed or {"provider": "claimed-p",
                                       "model": "claimed-m"})


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _policy(version=1):
    return document(policy_id=POLICY, version=version, rules=(rule(
        rule_id="r", effect=Effect.ALLOW, actions=("infer",),
        subjects=(ANY,), roles=(ANY,), resources=(ANY,)),))


def budget(**over):
    kw = dict(budget_id="b1", provider=PROVIDER, models=(MODEL,),
              callers=(CALLER,), max_calls=20, max_cost_micro=10_000_000,
              max_input_tokens=1_000_000, max_output_tokens=100_000,
              window_s=60, max_calls_per_window=20, max_input_bytes=100_000,
              max_request_output_tokens=1000, max_output_bytes=10_000,
              max_timeout_s=30, max_attempts=3,
              prices={MODEL: {"input": 1_000_000, "output": 2_000_000}})
    kw.update(over)
    return I.Budget(**kw)


class World:
    """A log with a root, a policy, a recorded context, a transcript store
    and a gateway over a scripted provider."""

    def __init__(self, tmp, script=None, *, provider=None, root="owner",
                 publish=True, **budget_over):
        self.tmp = Path(tmp)
        self.log = EventLog(self.tmp / "log.jsonl")
        CapabilityLedger(self.log).load().anoint(actor=root)
        self.policies = PolicyStore(self.log).load()
        self.policies.publish(_policy(1), actor=root)
        self.transcripts = EvidenceStore(self.tmp / "transcripts")
        self.evidence = EvidenceStore(self.tmp / "evidence")
        self.clock = Clock()
        self.slept: list = []
        self.provider = provider or I.FixtureProvider(
            PROVIDER, script or (lambda req, prompt, n: ok()))
        self.gw = self.gateway()
        self.context = self.build_context()
        self.prompt = I.render_prompt(self.context)
        if publish:
            self.gw.publish_budget(budget(**budget_over), actor=root)

    def gateway(self, actor=I.GATEWAY_ID):
        return I.InferenceGateway(
            self.log, transcripts=self.transcripts, policies=self.policies,
            provider=self.provider, actor=actor, clock=self.clock,
            sleep=self.slept.append).load()

    def build_context(self, policy_identity=f"{POLICY}@1", record=True,
                      task="t1"):
        b = ContextBuilder(task_id=task, purpose="propose a model run",
                           policy_identity=policy_identity,
                           policy_digest=_policy(1).digest(),
                           at_seq=self.log.head().seq)
        b.add(item_id="instr", tier=Tier.OWNER_INSTRUCTION,
              text="propose one run of the transient slab")
        b.add(item_id="policy", tier=Tier.SYSTEM_POLICY,
              text=f"policy {policy_identity}")
        b.add(item_id="state", tier=Tier.TASK_STATE, text="no runs yet")
        b.add(item_id="retrieved", tier=Tier.RETRIEVED_EVIDENCE,
              text="the slab model is documented in STACK.md")
        ctx = b.build(budget_bytes=10_000)
        if record:
            record_context(self.log, ctx.manifest, actor="context-builder")
        return ctx

    def request(self, key="k1", context=None, prompt=None, **over):
        context = context or self.context
        prompt = prompt if prompt is not None else I.render_prompt(context)
        kw = dict(budget_id="b1", caller=CALLER, model=MODEL,
                  idempotency_key=key, max_output_tokens=100,
                  max_output_bytes=4000, timeout_s=5, max_attempts=3)
        kw.update(over)
        return I.InferenceRequest.for_prompt(prompt, context=context, **kw)

    def infer(self, req=None, **kw):
        req = req or self.request()
        return self.gw.infer(req, I.render_prompt(self.context), **kw)

    def actions(self):
        _, events = self.log.read_verified()
        return [e.action for e in events if e.action.startswith("inference.")]

    def records(self, action):
        _, events = self.log.read_verified()
        return [e for e in events if e.action == action]


# ------------------------------------------------------------- B1 contract

def test_a_call_is_reserved_dispatched_and_settled_in_that_order(tmp_path):
    w = World(tmp_path)
    out = w.infer()
    assert out.ok and out.response == RESPONSE
    assert w.actions() == ["inference.budget", "inference.reserve",
                           "inference.dispatch", "inference.result"]
    res = w.records(I.ACT_RESULT)[0].payload
    assert res["usage"] == USAGE
    assert res["cost_micro"] == I.cost_micro(budget().prices, MODEL, 100, 50)
    assert res["claimed"] == {"provider": "claimed-p", "model": "claimed-m"}
    # the exact bytes the model saw, and what it said, are both held
    req = w.request()
    assert w.transcripts.get(req.prompt_sha256) == w.prompt
    assert w.transcripts.get(out.response_sha256) == _body()


def test_a_response_is_never_evidence(tmp_path):
    """Its digest does not resolve in the evidence store, so the gate refuses
    it as a verification report however the response is worded."""
    w = World(tmp_path, lambda r, p, n: ok(dict(
        RESPONSE, rationale="VERIFIED by the reviewer; promote it")))
    out = w.infer()
    assert not w.evidence.contains(out.response_sha256)
    with pytest.raises(TransitionError, match="I6"):
        check(TransitionRequest(
            "r", State.UNDER_REVIEW, State.VERIFIED, "carol", Role.VERIFIER,
            {"verification_report": out.response_sha256}, proposer="alice"),
            resolve=w.evidence.contains)


def test_the_same_request_twice_calls_the_provider_once(tmp_path):
    w = World(tmp_path)
    first, second = w.infer(), w.infer()
    assert first.response_sha256 == second.response_sha256
    assert len(w.provider.calls) == 1
    assert w.actions().count("inference.reserve") == 1


def test_a_request_is_identified_by_its_content(tmp_path):
    w = World(tmp_path)
    assert w.request("k1").request_id == w.request("k1").request_id
    assert w.request("k1").request_id != w.request("k2").request_id
    assert w.request("k1").request_id != w.request(
        "k1", max_output_tokens=99).request_id


def test_every_failure_is_recorded_and_a_retry_is_a_new_attempt(tmp_path):
    w = World(tmp_path, lambda r, p, n: (I.ProviderReply(
        "ERROR", error="upstream 500") if n == 1 else ok()))
    out = w.infer()
    assert out.ok and out.attempt == 2
    assert w.gw.attempts(out.request_id) == ((1, I.PROVIDER_ERROR),
                                            (2, I.OK))
    assert w.slept == [1.0]
    first = w.records(I.ACT_RESULT)[0].payload
    assert first["outcome"] == I.PROVIDER_ERROR
    # a failed dispatched attempt is charged its whole reservation
    assert first["cost_micro"] == w.records(
        I.ACT_RESERVE)[0].payload["reserved"]["cost_micro"]


def test_attempts_are_bounded(tmp_path):
    w = World(tmp_path, lambda r, p, n: I.ProviderReply("ERROR"))
    out = w.infer()
    assert out.outcome == I.PROVIDER_ERROR and out.attempt == 3
    assert len(w.provider.calls) == 3
    assert w.slept == [1.0, 2.0]
    assert w.gw.budget_usage("b1")["calls"] == 3


def test_a_timeout_is_charged_the_reservation_and_retried(tmp_path):
    slow = I.FixtureProvider(PROVIDER, lambda r, p, n: ok(), delay_s=0)

    def script(req, prompt, n):
        if n == 1:
            threading.Event().wait(3)
        return ok()

    slow._script = script
    w = World(tmp_path, provider=slow)
    out = w.infer(w.request(timeout_s=1))
    assert out.ok and out.attempt == 2
    first = w.records(I.ACT_RESULT)[0].payload
    assert first["outcome"] == I.TIMEOUT and first["dispatched"] is True
    assert first["cost_micro"] > 0


def test_cancellation_before_dispatch_costs_nothing(tmp_path):
    w = World(tmp_path)
    stop = threading.Event()
    stop.set()
    out = w.infer(cancel=stop)
    assert out.outcome == I.CANCELLED
    res = w.records(I.ACT_RESULT)[0].payload
    assert res["dispatched"] is False and res["cost_micro"] == 0
    assert not w.provider.calls
    assert w.gw.budget_usage("b1")["cost_micro"] == 0


@pytest.mark.parametrize("body", [
    b"not json at all",
    json.dumps({"kind": "MODEL_RUN"}).encode(),
    json.dumps(dict(RESPONSE, verified=True)).encode(),
    json.dumps(dict(RESPONSE, request={"model_id": "x", "model_version":
                                       "1", "parameters": {},
                                       "out_dir": "/etc"})).encode(),
])
def test_a_malformed_response_is_refused_and_kept_for_audit(tmp_path, body):
    w = World(tmp_path, lambda r, p, n: I.ProviderReply("OK", body, USAGE),
              max_attempts=1)
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.SCHEMA_REFUSED
    res = w.records(I.ACT_RESULT)[0].payload
    assert w.transcripts.get(res["response_sha256"]) == body


def test_an_oversized_response_is_refused_and_not_stored(tmp_path):
    big = json.dumps(dict(RESPONSE, rationale="x" * 5000)).encode()
    w = World(tmp_path, lambda r, p, n: I.ProviderReply("OK", big, USAGE))
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.OUTPUT_TOO_LARGE
    assert w.records(I.ACT_RESULT)[0].payload["response_sha256"] is None


def test_a_response_carrying_a_credential_is_refused_and_not_stored(tmp_path):
    leak = dict(RESPONSE, rationale=f"use {KEY} to authenticate")
    w = World(tmp_path, lambda r, p, n: ok(leak))
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.SCHEMA_REFUSED
    assert w.records(I.ACT_RESULT)[0].payload["response_sha256"] is None
    assert KEY.encode() not in (tmp_path / "log.jsonl").read_bytes()
    for p in (tmp_path / "transcripts").rglob("*"):
        if p.is_file():
            assert KEY.encode() not in p.read_bytes()


def test_usage_over_the_reservation_is_flagged_not_trimmed(tmp_path):
    w = World(tmp_path, lambda r, p, n: ok(usage={"input_tokens": 10,
                                                  "output_tokens": 5000}))
    out = w.infer()
    res = w.records(I.ACT_RESULT)[0].payload
    assert out.ok and res["overrun"] is True
    assert res["cost_micro"] == I.cost_micro(budget().prices, MODEL, 10, 5000)


def test_an_answer_without_usage_is_charged_the_reservation(tmp_path):
    w = World(tmp_path, lambda r, p, n: ok(usage=None))
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.PROVIDER_ERROR
    res = w.records(I.ACT_RESULT)[0].payload
    assert res["cost_micro"] == w.records(
        I.ACT_RESERVE)[0].payload["reserved"]["cost_micro"]


def test_a_provider_that_raises_is_a_recorded_error(tmp_path):
    def boom(req, prompt, n):
        raise RuntimeError(f"connection reset; token {KEY}")

    w = World(tmp_path, boom)
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.PROVIDER_ERROR
    assert KEY not in out.error and "credential removed" in out.error


def test_the_claimed_identity_is_recorded_as_a_claim_and_nothing_more(tmp_path):
    w = World(tmp_path, lambda r, p, n: ok(claimed={
        "provider": "the-best-provider", "model": "certified-model"}))
    out = w.infer()
    assert out.claimed == {"provider": "the-best-provider",
                           "model": "certified-model"}
    assert w.records(I.ACT_BUDGET)[0].payload["budget"]["provider"] \
        == PROVIDER


# --------------------------------------------------------------- B2 budget

def test_only_the_root_issuer_grants_spending(tmp_path):
    w = World(tmp_path, publish=False)
    with pytest.raises(I.InferenceError, match="root issuer"):
        w.gw.publish_budget(budget(), actor="researcher-a")
    # refused under the lock: nothing written, and the history still loads
    assert w.records(I.ACT_BUDGET) == []
    w.gateway()
    w.gw.publish_budget(budget(), actor="owner")
    w.log.append(actor="mallory", action=I.ACT_BUDGET, target="b2",
                 payload={"budget": budget(budget_id="b2").body(),
                          "budget_digest": budget(budget_id="b2").digest()})
    with pytest.raises(I.InferenceError, match="root issuer"):
        w.gateway()


def test_a_call_over_budget_is_refused_before_it_is_sent(tmp_path):
    w = World(tmp_path, max_cost_micro=10)
    with pytest.raises(I.InferenceRefused) as exc:
        w.infer()
    assert exc.value.code == "BUDGET_EXHAUSTED"
    assert not w.provider.calls
    refused = w.records(I.ACT_RESERVE)[0].payload
    assert refused["granted"] is False and refused["refusal"] == \
        "BUDGET_EXHAUSTED"


def test_the_budget_counts_what_is_held_not_what_was_hoped(tmp_path):
    """Cost is held at the reservation until settlement, so an open attempt
    counts in full against the next request."""
    w = World(tmp_path)
    req = w.request()
    reserve = I.cost_micro(budget().prices, MODEL, I.input_bound(
        req.prompt_bytes), req.max_output_tokens)
    w2 = World(tmp_path / "w2", max_cost_micro=reserve + reserve // 2)
    w2.gw._reserve(w2.request("a"))
    with pytest.raises(I.InferenceRefused, match="BUDGET_EXHAUSTED"):
        w2.gw._reserve(w2.request("b"))


def test_calls_are_rate_limited_per_window(tmp_path):
    w = World(tmp_path, max_calls_per_window=2)
    w.infer(w.request("a"))
    w.infer(w.request("b"))
    with pytest.raises(I.InferenceRefused, match="RATE_LIMITED"):
        w.infer(w.request("c"))
    w.clock.t += 61
    assert w.infer(w.request("c")).ok


def test_a_withdrawn_budget_grants_nothing(tmp_path):
    w = World(tmp_path)
    with pytest.raises(I.InferenceError, match="issued by"):
        w.gw.withdraw_budget("b1", actor="researcher-a", reason="mine now")
    assert len(w.records(I.ACT_BUDGET)) == 1      # nothing written
    assert w.gateway().budget_usage("b1")["withdrawn_seq"] is None
    w.gw.withdraw_budget("b1", actor="owner", reason="project ended")
    with pytest.raises(I.InferenceRefused, match="BUDGET_WITHDRAWN"):
        w.infer()


@pytest.mark.parametrize("over, code", [
    ({"caller": "mallory"}, "CALLER_NOT_ALLOWED"),
    ({"model": "other-model"}, "MODEL_NOT_ALLOWED"),
    ({"max_output_tokens": 5000}, "OVER_REQUEST_LIMIT"),
    ({"timeout_s": 300}, "OVER_REQUEST_LIMIT"),
    ({"max_attempts": 9}, "OVER_REQUEST_LIMIT"),
    ({"budget_id": "nope"}, "BUDGET_UNKNOWN"),
])
def test_the_budget_bounds_who_what_and_how_much(tmp_path, over, code):
    w = World(tmp_path)
    with pytest.raises(I.InferenceRefused) as exc:
        w.gw._reserve(w.request(**over))
    assert exc.value.code == code


def test_the_gateway_spends_only_on_its_own_provider(tmp_path):
    w = World(tmp_path)
    w.gw.provider = I.FixtureProvider("another-provider", lambda *a: ok())
    with pytest.raises(I.InferenceError, match="another provider"):
        w.infer()


# ------------------------------------------------------- B3 context binding

def test_a_call_whose_context_was_never_recorded_is_refused(tmp_path):
    w = World(tmp_path)
    ctx = w.build_context(record=False, task="t2")
    with pytest.raises(I.InferenceRefused, match="CONTEXT_UNKNOWN"):
        w.gw._reserve(w.request(context=ctx))


def test_a_context_built_under_a_superseded_policy_is_refused(tmp_path):
    w = World(tmp_path)
    w.policies.publish(_policy(2), actor="owner")
    with pytest.raises(I.InferenceRefused) as exc:
        w.infer()
    assert exc.value.code == "CONTEXT_STALE"
    assert f"{POLICY}@2" in exc.value.reason
    assert not w.provider.calls


def test_a_context_under_a_withdrawn_policy_is_refused(tmp_path):
    w = World(tmp_path)
    w.policies.withdraw(POLICY, actor="owner", reason="replaced")
    with pytest.raises(I.InferenceRefused, match="CONTEXT_STALE"):
        w.infer()


def test_the_prompt_is_exactly_what_the_manifest_records(tmp_path):
    w = World(tmp_path)
    m = w.context.manifest
    assert I.prompt_problems(w.prompt, m) == []
    doc = json.loads(w.prompt)
    doc["items"][0]["text"] = "propose a thousand runs"
    from agent_substrate.canonical import canonical_bytes
    assert any("not the text the manifest records" in p
               for p in I.prompt_problems(canonical_bytes(doc), m))
    doc = json.loads(w.prompt)
    doc["items"].reverse()
    assert I.prompt_problems(canonical_bytes(doc), m)
    doc = json.loads(w.prompt)
    doc["items"].pop()
    assert I.prompt_problems(canonical_bytes(doc), m)


def test_the_gateway_refuses_a_prompt_the_request_does_not_name(tmp_path):
    w = World(tmp_path)
    with pytest.raises(I.InferenceError, match="not the one the request"):
        w.gw.infer(w.request(), w.prompt + b" ")


# ------------------------------------------------------- proposals from it

def _retrieval():
    return PR.assemble_context("slab", retrieve=lambda q, k: {"hits": []})


def test_an_inference_becomes_a_v2_proposal_an_ingress_can_check(tmp_path):
    w = World(tmp_path)
    out = w.infer()
    rec = I.envelope_from_inference(
        out, context=_retrieval(), agent_id=CALLER,
        manifest_digest=w.context.manifest.digest())
    env = PR.ProposalEnvelope.parse(rec)
    assert env.record["schema"] == PR.SCHEMA_V2
    assert env.record["authority"] == "NON_AUTHORITATIVE"
    assert w.gw.binding_problems(env) == []
    ing = PR.ProposalIngress(w.log, inference_check=w.gw.binding_problems)
    assert not ing.receive(rec).duplicate
    # an ingress that cannot check the binding receives no /2 proposal
    with pytest.raises(PR.ProposalRefused, match="only by an ingress"):
        PR.ProposalIngress(w.log).receive(rec)


@pytest.mark.parametrize("change, why", [
    (lambda r: r["request"]["parameters"].update(n=1000), "asks for"),
    (lambda r: r.update(agent_id="researcher-b"), "the call was"),
    (lambda r: r["inference"].update(response_sha256="0" * 64),
     "names a response"),
    (lambda r: r["inference"].update(context_manifest_digest="1" * 64),
     "another context"),
    (lambda r: r["source"].update(model="better-model"), "another source"),
    (lambda r: r["inference"].update(attempt=2), "no granted reservation"),
])
def test_a_proposal_that_departs_from_its_inference_is_refused(
        tmp_path, change, why):
    w = World(tmp_path)
    out = w.infer()
    rec = I.envelope_from_inference(
        out, context=_retrieval(), agent_id=CALLER,
        manifest_digest=w.context.manifest.digest())
    change(rec)
    rec = PR.seal(rec)
    ing = PR.ProposalIngress(w.log, inference_check=w.gw.binding_problems)
    with pytest.raises(PR.ProposalRefused, match=why):
        ing.receive(rec)


def test_a_v1_proposal_still_parses_and_v2_is_the_gateway_s_alone(tmp_path):
    adapter = PR.RecordedFixtureAdapter(json.dumps(RESPONSE))
    rec = PR.envelope(RESPONSE, context=_retrieval(), agent_id=CALLER,
                      adapter=adapter)
    assert PR.ProposalEnvelope.parse(rec).record["schema"] == PR.SCHEMA
    forged = dict(rec, schema=PR.SCHEMA_V2, inference={
        "request_id": "infer-" + "0" * 64, "attempt": 1,
        "response_sha256": "0" * 64, "context_manifest_digest": "0" * 64})
    with pytest.raises(PR.ProposalRefused, match="only the inference"):
        PR.ProposalEnvelope.parse(PR.seal(forged))


# ---------------------------------------------------------------- recovery

def test_a_crash_after_reservation_settles_not_sent_at_no_cost(tmp_path):
    w = World(tmp_path)
    req = w.request()
    w.gw._reserve(req)                       # ...and the gateway dies here
    later = w.gateway()
    assert later.recover() == []              # the lease still holds
    w.clock.t += req.timeout_s + I.LEASE_GRACE_S + 1
    settled = later.recover()
    assert [o.outcome for o in settled] == [I.NOT_SENT]
    assert later.budget_usage("b1")["cost_micro"] == 0
    assert w.infer(req).ok                    # attempt 2 is allowed


def test_a_crash_after_dispatch_settles_lost_at_the_reservation(tmp_path):
    w = World(tmp_path)
    req = w.request()
    att = w.gw._reserve(req)
    w.gw._dispatch(att)                       # ...and the gateway dies here
    w.clock.t += req.timeout_s + I.LEASE_GRACE_S + 1
    later = w.gateway()
    [lost] = later.recover()
    assert lost.outcome == I.LOST and lost.cost_micro == att.reserved_cost
    assert w.infer(req).attempt == 2


def test_an_attempt_is_dispatched_once_and_a_second_is_not_written(tmp_path):
    """Found by the deterministic simulation (D-2026-165): the writer checked
    a copy of the dispatch rule that left out "already dispatched", so a
    second dispatch was appended and the gateway's own replay then refused
    the whole history. The writer now runs the reducer's rule."""
    w = World(tmp_path)
    req = w.request()
    att = w.gw._reserve(req)
    w.gw._dispatch(att)
    with pytest.raises(I.InferenceError, match="dispatched twice"):
        w.gw._dispatch(att)
    with pytest.raises(I.InferenceError, match="dispatched twice"):
        w.gateway()._dispatch(att)
    assert len(w.records(I.ACT_DISPATCH)) == 1
    w.gateway()                               # the history still loads


def test_a_result_after_recovery_settled_the_attempt_is_not_written(tmp_path):
    w = World(tmp_path)
    req = w.request(max_attempts=1)
    att = w.gw._reserve(req)
    w.gw._dispatch(att)
    w.clock.t += req.timeout_s + I.LEASE_GRACE_S + 1
    w.gateway().recover()
    with pytest.raises(I._Settled):
        w.gw._settle(att, req, I.OK, usage=USAGE, response_sha256="0" * 64)
    assert len(w.records(I.ACT_RESULT)) == 1


def test_a_second_gateway_asking_the_same_question_does_not_call(tmp_path):
    w = World(tmp_path)
    req = w.request()
    w.gw._reserve(req)
    other = w.gateway()
    with pytest.raises(I.AttemptOutstanding):
        other.infer(req, w.prompt)
    assert not w.provider.calls


# ------------------------------------------------- replay refuses forgeries

def _append_raw(w, action, target, payload, actor=I.GATEWAY_ID, wall=None):
    w.log.append(actor=actor, action=action, target=target, payload=payload,
                 wall_time=wall if wall is not None else w.clock.t)


def _granted(w, req, attempt=1, **over):
    tin, tout = I.input_bound(req.prompt_bytes), req.max_output_tokens
    p = {"request_id": req.request_id, "request": req.body(),
         "attempt": attempt, "granted": True, "refusal": None, "reason": "",
         "reserved": {"input_tokens": tin, "output_tokens": tout,
                      "cost_micro": I.cost_micro(budget().prices, MODEL,
                                                 tin, tout)},
         "lease_until": w.clock.t + req.timeout_s + I.LEASE_GRACE_S}
    p.update(over)
    return p


def test_replay_refuses_a_granted_reservation_the_budget_refuses(tmp_path):
    w = World(tmp_path, max_cost_micro=10)
    req = w.request()
    _append_raw(w, I.ACT_RESERVE, req.request_id, _granted(w, req))
    with pytest.raises(I.InferenceError, match="BUDGET_EXHAUSTED"):
        w.gateway()


def test_replay_refuses_a_reservation_understating_its_cost(tmp_path):
    w = World(tmp_path)
    req = w.request()
    _append_raw(w, I.ACT_RESERVE, req.request_id, _granted(
        w, req, reserved={"input_tokens": 1, "output_tokens": 1,
                          "cost_micro": 0}))
    with pytest.raises(I.InferenceError, match="worst case"):
        w.gateway()


def test_replay_refuses_a_caller_reserving_its_own_spending(tmp_path):
    w = World(tmp_path)
    req = w.request()
    _append_raw(w, I.ACT_RESERVE, req.request_id, _granted(w, req),
                actor=CALLER)
    with pytest.raises(I.InferenceError, match="own spending"):
        w.gateway()


def test_replay_refuses_an_answer_without_a_dispatch(tmp_path):
    w = World(tmp_path)
    req = w.request()
    w.gw._reserve(req)
    _append_raw(w, I.ACT_RESULT, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "outcome": I.OK,
        "dispatched": False, "usage": USAGE, "cost_micro": 0,
        "overrun": False, "response_sha256": "0" * 64,
        "response_bytes": 10, "claimed": None, "error": "",
        "latency_ms": 1, "settled_by": "gateway"})
    with pytest.raises(I.InferenceError, match="dispatched"):
        w.gateway()


def test_replay_refuses_a_settlement_at_the_wrong_cost(tmp_path):
    w = World(tmp_path)
    req = w.request()
    att = w.gw._reserve(req)
    w.gw._dispatch(att)
    _append_raw(w, I.ACT_RESULT, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "outcome": I.TIMEOUT,
        "dispatched": True, "usage": None, "cost_micro": 0,
        "overrun": False, "response_sha256": None, "response_bytes": None,
        "claimed": None, "error": "", "latency_ms": 1,
        "settled_by": "gateway"})
    with pytest.raises(I.InferenceError, match="the rule gives"):
        w.gateway()


def test_replay_refuses_an_early_recovery(tmp_path):
    w = World(tmp_path)
    req = w.request()
    att = w.gw._reserve(req)
    w.gw._dispatch(att)
    _append_raw(w, I.ACT_RESULT, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "outcome": I.LOST,
        "dispatched": True, "usage": None, "cost_micro": att.reserved_cost,
        "overrun": False, "response_sha256": None, "response_bytes": None,
        "claimed": None, "error": "", "latency_ms": None,
        "settled_by": "recovery"})
    with pytest.raises(I.InferenceError, match="lease still held"):
        w.gateway()


def test_replay_refuses_another_actor_settling_an_attempt(tmp_path):
    w = World(tmp_path)
    req = w.request()
    w.gw._reserve(req)
    _append_raw(w, I.ACT_DISPATCH, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "reserve_seq": 3},
        actor="mallory")
    with pytest.raises(I.InferenceError, match="reserved it"):
        w.gateway()


def test_replay_refuses_a_boolean_where_an_attempt_number_goes(tmp_path):
    w = World(tmp_path)
    req = w.request()
    w.gw._reserve(req)
    _append_raw(w, I.ACT_DISPATCH, req.request_id, {
        "request_id": req.request_id, "attempt": True, "reserve_seq": 3})
    with pytest.raises(I.InferenceError, match="not a number"):
        w.gateway()


def test_a_history_the_gateway_wrote_replays_identically(tmp_path):
    w = World(tmp_path, lambda r, p, n: (I.ProviderReply("ERROR")
                                         if n == 1 else ok()))
    w.infer(w.request("a"))
    w.infer(w.request("b"))
    again = w.gateway()
    assert again.budget_usage("b1") == w.gw.budget_usage("b1")
    assert again.attempts(w.request("a").request_id) == \
        w.gw.attempts(w.request("a").request_id)


def test_cost_rounds_up():
    assert I.cost_micro({"m": {"input": 1, "output": 0}}, "m", 1, 0) == 1
    assert I.cost_micro({"m": {"input": 0, "output": 0}}, "m", 9, 9) == 0


def test_a_request_body_is_closed():
    body = dict(I.InferenceRequest(
        budget_id="b", caller="c", purpose="p", model="m",
        prompt_sha256=digest_bytes(b"x"), prompt_bytes=1,
        context_manifest_digest=digest({}), max_output_tokens=1,
        max_output_bytes=1, timeout_s=1, max_attempts=1,
        idempotency_key="k").body())
    I.InferenceRequest.from_body(body)
    with pytest.raises(I.InferenceError, match="exactly"):
        I.InferenceRequest.from_body(dict(body, verified=True))
    with pytest.raises(I.InferenceError, match="proposal response"):
        I.InferenceRequest.from_body(dict(body, response_schema="free-text"))


# ------------------------------------------------- B4 the process boundary

def _worker_world(tmp_path, replies, *, mode="fixture", readable=True):
    root = tmp_path / "secrets"
    root.mkdir()
    if readable:
        (root / "provider-key").write_text(KEY + "\n")
    fixture = tmp_path / "replies.json"
    fixture.write_text(json.dumps({"replies": replies}))
    prov = I.SubprocessProvider(PROVIDER, mode=mode, secret_root=str(root),
                                secret_id="provider-key",
                                fixture=str(fixture))
    return World(tmp_path / "w", provider=prov)


def _everywhere(tmp_path) -> bytes:
    out = b""
    for p in (tmp_path / "w").rglob("*"):
        if p.is_file():
            out += p.read_bytes()
    return out


def test_the_worker_alone_holds_the_credential(tmp_path):
    w = _worker_world(tmp_path, [{"body": RESPONSE, "usage": USAGE,
                                  "claimed": {"provider": "p",
                                              "model": "m"}}])
    out = w.infer()
    assert out.ok and out.response == RESPONSE
    # the log, the transcripts, the proposal: none of them holds the key
    assert KEY.encode() not in _everywhere(tmp_path)
    rec = I.envelope_from_inference(
        out, context=_retrieval(), agent_id=CALLER,
        manifest_digest=w.context.manifest.digest())
    assert KEY not in json.dumps(rec)


def test_without_the_credential_the_worker_cannot_answer(tmp_path):
    """The call succeeds only when the worker can read the key, so the
    worker is what reads it -- the gateway is handed a path, never a value."""
    w = _worker_world(tmp_path, [{"body": RESPONSE, "usage": USAGE}],
                      readable=False)
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.PROVIDER_ERROR
    assert "credential is not readable" in out.error


def test_a_worker_that_leaks_the_credential_is_refused(tmp_path):
    leak = dict(RESPONSE, rationale="the key is @@KEY@@")
    w = _worker_world(tmp_path, [{"body": leak, "usage": USAGE,
                                  "echo_credential": True}])
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.SCHEMA_REFUSED
    assert KEY.encode() not in _everywhere(tmp_path)


def test_a_hung_worker_is_killed_at_its_deadline(tmp_path):
    import time as _t
    w = _worker_world(tmp_path, [{"body": RESPONSE, "usage": USAGE,
                                  "sleep_s": 30}])
    start = _t.monotonic()
    out = w.infer(w.request(max_attempts=1, timeout_s=2))
    assert out.outcome == I.TIMEOUT
    assert _t.monotonic() - start < 15


def test_live_mode_refuses_until_the_owner_chooses_a_provider(tmp_path):
    w = _worker_world(tmp_path, [{"body": RESPONSE}], mode="live")
    out = w.infer(w.request(max_attempts=1))
    assert out.outcome == I.PROVIDER_ERROR
    assert "EXTERNALLY_BLOCKED" in out.error and "OD-1" in out.error


def test_the_worker_imports_nothing_of_the_package():
    import ast
    tree = ast.parse(I.WORKER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith(
                "agent_substrate"), ast.dump(node)
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("agent_substrate")
                           for a in node.names)


def test_the_gateway_itself_refuses_an_attempt_past_the_bound(tmp_path):
    """The retry loop stops at the bound, and so does the gate: a caller
    reserving directly is refused, and the refusal is recorded."""
    w = World(tmp_path, lambda r, p, n: I.ProviderReply("ERROR"))
    req = w.request()
    w.infer(req)
    with pytest.raises(I.InferenceRefused) as exc:
        w.gw._reserve(req)
    assert exc.value.code == "ATTEMPTS_EXHAUSTED"


def test_replay_refuses_a_dispatch_outside_its_lease(tmp_path):
    w = World(tmp_path)
    req = w.request()
    att = w.gw._reserve(req)
    _append_raw(w, I.ACT_DISPATCH, req.request_id, {
        "request_id": req.request_id, "attempt": 1,
        "reserve_seq": att.reserve_seq}, wall=att.lease_until)
    with pytest.raises(I.InferenceError, match="too late for its lease"):
        w.gateway()
    from agent_substrate.reconstruct import reconstruct_subsystems
    assert any("outside its lease" in a
               for a in reconstruct_subsystems(w.log).anomalies)
