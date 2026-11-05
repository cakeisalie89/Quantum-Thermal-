"""The inference records, read a second time.

``reconstruct_subsystems`` restates the gateway's rules without importing it.
A history the gateway wrote reads clean and agrees with the gateway about
what is held against each budget. A record appended around the gateway is
refused by the gateway's own replay AND reported by the second reader: two
implementations, each refusing the forgery for its own reasons.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_inference_gateway as G  # noqa: E402

from agent_substrate import inference as I  # noqa: E402
from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402


def _anomalies(w) -> list:
    return reconstruct_subsystems(w.log).anomalies


def _both_refuse(w, match_primary: str, match_second: str) -> None:
    with pytest.raises(I.InferenceError, match=match_primary):
        w.gateway()
    found = _anomalies(w)
    assert any(match_second in a for a in found), found


def test_a_history_the_gateway_wrote_reads_clean_and_agrees(tmp_path):
    def script(req, prompt, n):
        return I.ProviderReply("ERROR") if n == 1 else G.ok()

    w = G.World(tmp_path, script, max_calls_per_window=4)
    w.infer(w.request("a"))
    w.infer(w.request("b"))
    with pytest.raises(I.InferenceRefused):
        w.infer(w.request("c"))            # rate window: a and b took two
    req = w.request("d")
    w.clock.t += 120
    att = w.gw._reserve(req)
    w.gw._dispatch(att)
    w.clock.t += req.timeout_s + I.LEASE_GRACE_S + 1
    w.gateway().recover()                              # LOST
    w.gw.withdraw_budget("b1", actor="owner", reason="done")
    sub = reconstruct_subsystems(w.log)
    assert sub.anomalies == [], sub.anomalies
    held = w.gateway().budget_usage("b1")
    b = sub.inference_budgets["b1"]
    assert (b["calls"], b["cost"], b["in"], b["out"]) == (
        held["calls"], held["cost_micro"], held["input_tokens"],
        held["output_tokens"])
    assert b["withdrawn_seq"] is not None


def test_a_budget_granted_by_anyone_but_the_root(tmp_path):
    w = G.World(tmp_path, publish=False)
    body = G.budget().body()
    G._append_raw(w, I.ACT_BUDGET, "b1", {"budget": body,
                                          "budget_digest": G.budget().digest()},
                  actor="mallory")
    _both_refuse(w, "root issuer", "grants spending")


def test_a_withdrawal_by_someone_else(tmp_path):
    w = G.World(tmp_path)
    G._append_raw(w, I.ACT_BUDGET, "b1", {"budget_id": "b1",
                                          "withdraw": True, "reason": "x"},
                  actor="mallory")
    _both_refuse(w, "withdrawn by whoever", "which 'owner' issued")


def test_a_granted_reservation_over_the_budget(tmp_path):
    w = G.World(tmp_path, max_cost_micro=10)
    req = w.request()
    G._append_raw(w, I.ACT_RESERVE, req.request_id, G._granted(w, req))
    _both_refuse(w, "BUDGET_EXHAUSTED", "over the budget's totals")


def test_a_granted_reservation_for_a_stale_context(tmp_path):
    w = G.World(tmp_path)
    w.policies.publish(G._policy(2), actor="owner")
    req = w.request()
    G._append_raw(w, I.ACT_RESERVE, req.request_id, G._granted(w, req))
    _both_refuse(w, "CONTEXT_STALE", "policy no longer in force")


def test_a_granted_reservation_for_an_unrecorded_context(tmp_path):
    w = G.World(tmp_path)
    ctx = w.build_context(record=False, task="t9")
    req = w.request(context=ctx)
    G._append_raw(w, I.ACT_RESERVE, req.request_id, G._granted(w, req))
    _both_refuse(w, "CONTEXT_UNKNOWN", "no context.build")


def test_a_reservation_for_less_than_its_worst_case(tmp_path):
    w = G.World(tmp_path)
    req = w.request()
    G._append_raw(w, I.ACT_RESERVE, req.request_id, G._granted(
        w, req, reserved={"input_tokens": 1, "output_tokens": 1,
                          "cost_micro": 0}))
    _both_refuse(w, "worst case", "less than its worst")


def test_a_caller_reserving_its_own_spending(tmp_path):
    w = G.World(tmp_path)
    req = w.request()
    G._append_raw(w, I.ACT_RESERVE, req.request_id, G._granted(w, req),
                  actor=G.CALLER)
    _both_refuse(w, "own spending", "its own request")


def test_a_second_attempt_while_the_first_is_open(tmp_path):
    w = G.World(tmp_path)
    req = w.request()
    w.gw._reserve(req)
    G._append_raw(w, I.ACT_RESERVE, req.request_id, G._granted(
        w, req, attempt=2))
    _both_refuse(w, "an attempt is open", "while an attempt is open")


def test_an_answer_without_a_dispatch(tmp_path):
    w = G.World(tmp_path)
    req = w.request()
    w.gw._reserve(req)
    G._append_raw(w, I.ACT_RESULT, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "outcome": I.OK,
        "dispatched": False, "usage": G.USAGE, "cost_micro": 0,
        "overrun": False, "response_sha256": "0" * 64, "response_bytes": 1,
        "claimed": None, "error": "", "latency_ms": 1,
        "settled_by": "gateway"})
    _both_refuse(w, "dispatched", "contradicts")


def test_a_settlement_at_the_wrong_cost(tmp_path):
    w = G.World(tmp_path)
    req = w.request()
    att = w.gw._reserve(req)
    w.gw._dispatch(att)
    G._append_raw(w, I.ACT_RESULT, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "outcome": I.TIMEOUT,
        "dispatched": True, "usage": None, "cost_micro": 0,
        "overrun": False, "response_sha256": None, "response_bytes": None,
        "claimed": None, "error": "", "latency_ms": 1,
        "settled_by": "gateway"})
    _both_refuse(w, "the rule gives", "where the rule gives")


def test_a_recovery_inside_the_lease(tmp_path):
    w = G.World(tmp_path)
    req = w.request()
    att = w.gw._reserve(req)
    w.gw._dispatch(att)
    G._append_raw(w, I.ACT_RESULT, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "outcome": I.LOST,
        "dispatched": True, "usage": None, "cost_micro": att.reserved_cost,
        "overrun": False, "response_sha256": None, "response_bytes": None,
        "claimed": None, "error": "", "latency_ms": None,
        "settled_by": "recovery"})
    _both_refuse(w, "lease still held", "inside the lease")


def test_a_dispatch_by_another_actor(tmp_path):
    w = G.World(tmp_path)
    req = w.request()
    w.gw._reserve(req)
    G._append_raw(w, I.ACT_DISPATCH, req.request_id, {
        "request_id": req.request_id, "attempt": 1, "reserve_seq": 3},
        actor="mallory")
    _both_refuse(w, "reserved it", "acts on an inference attempt")


def test_a_second_settlement(tmp_path):
    w = G.World(tmp_path)
    out = w.infer()
    res = w.records(I.ACT_RESULT)[0].payload
    G._append_raw(w, I.ACT_RESULT, out.request_id, dict(res))
    _both_refuse(w, "settled twice", "settled twice")


def test_a_v2_receipt_naming_an_inference_never_granted(tmp_path):
    """The ingress refuses it; appended around the ingress, the second
    reader still finds it."""
    w = G.World(tmp_path)
    out = w.infer()
    rec = I.envelope_from_inference(
        out, context=G._retrieval(), agent_id=G.CALLER,
        manifest_digest=w.context.manifest.digest())
    rec["inference"]["request_id"] = "infer-" + "9" * 64
    rec = PR.seal(rec)
    env = PR.ProposalEnvelope.parse(rec)
    w.log.append(actor=PR.INGRESS_ID, action=PR.ACT_PROPOSAL_RECEIVE,
                 target=env.proposal_id,
                 payload={"envelope": env.record,
                          "envelope_digest": env.digest(),
                          "received_by": PR.INGRESS_ID,
                          "authority": PR.AUTHORITY})
    found = _anomalies(w)
    assert any("never granted" in a for a in found), found


def test_a_v2_receipt_in_another_agent_s_name(tmp_path):
    w = G.World(tmp_path)
    out = w.infer()
    rec = I.envelope_from_inference(
        out, context=G._retrieval(), agent_id="researcher-b",
        manifest_digest=w.context.manifest.digest())
    env = PR.ProposalEnvelope.parse(rec)
    w.log.append(actor=PR.INGRESS_ID, action=PR.ACT_PROPOSAL_RECEIVE,
                 target=env.proposal_id,
                 payload={"envelope": env.record,
                          "envelope_digest": env.digest(),
                          "received_by": PR.INGRESS_ID,
                          "authority": PR.AUTHORITY})
    found = _anomalies(w)
    assert any("asked a model" in a for a in found), found


def test_a_v2_receipt_the_gateway_wrote_reads_clean(tmp_path):
    w = G.World(tmp_path)
    out = w.infer()
    rec = I.envelope_from_inference(
        out, context=G._retrieval(), agent_id=G.CALLER,
        manifest_digest=w.context.manifest.digest())
    PR.ProposalIngress(w.log, inference_check=w.gw.binding_problems
                       ).receive(rec)
    assert _anomalies(w) == []
