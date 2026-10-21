"""Four processes, one budget: spending decided under the writer lock.

Two races, each lined up so every process is past spawning, importing and
opening the log before any of them starts:

* four processes each issue five distinct requests against a budget that
  allows exactly seven calls. Seven are granted between them, every other
  one is refused and the refusal recorded, and nothing held exceeds the
  budget;
* four processes ask ONE question at once. The provider is called once. The
  others find the attempt open, or find it answered, and call nothing.

Both histories replay, and the second reader agrees with the gateway about
what is held against the budget.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(ROOT / "tools"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_agent_cross_process as XP  # noqa: E402
from hangguard import PROCESS_DEADLINE_S  # noqa: E402

from agent_substrate import inference as I  # noqa: E402
from agent_substrate.capability import CapabilityLedger  # noqa: E402
from agent_substrate.context import (  # noqa: E402
    ContextBuilder, Tier, record_context,
)
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyStore, document, rule,
)
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402

RACERS = 4
EACH = 5
ALLOWED = 7
PROVIDER, MODEL, CALLER = "fixture-provider", "fixture-model", "researcher-a"
RESPONSE = {"kind": "DIAGNOSTIC", "provider": None, "model": None,
            "rationale": "a question for a person",
            "request": {"question": "is the mesh fine enough?",
                        "subject": "thermal.slab_transient@1.0.0"}}


def _build(base: Path) -> None:
    log = EventLog(base / "log.jsonl")
    CapabilityLedger(log).load().anoint(actor="owner")
    policies = PolicyStore(log).load()
    policies.publish(document(policy_id="inference.default", version=1,
                              rules=(rule(rule_id="r", effect=Effect.ALLOW,
                                          actions=("infer",),
                                          subjects=(ANY,), roles=(ANY,),
                                          resources=(ANY,)),)),
                     actor="owner")
    b = ContextBuilder(task_id="t1", purpose="ask", at_seq=log.head().seq,
                       policy_identity="inference.default@1")
    b.add(item_id="instr", tier=Tier.OWNER_INSTRUCTION, text="ask")
    ctx = b.build(budget_bytes=1000)
    record_context(log, ctx.manifest, actor="context-builder")
    gw = _gateway(base, log, policies)
    gw.publish_budget(I.Budget(
        budget_id="b1", provider=PROVIDER, models=(MODEL,),
        callers=(CALLER,), max_calls=ALLOWED, max_cost_micro=10**12,
        max_input_tokens=10**9, max_output_tokens=10**9, window_s=3600,
        max_calls_per_window=1000, max_input_bytes=10_000,
        max_request_output_tokens=100, max_output_bytes=10_000,
        max_timeout_s=30, max_attempts=1,
        prices={MODEL: {"input": 1000, "output": 1000}}), actor="owner")
    prompt = I.render_prompt(ctx)
    (base / "prompt.bin").write_bytes(prompt)
    req = I.InferenceRequest.for_prompt(
        prompt, context=ctx, budget_id="b1", caller=CALLER, model=MODEL,
        idempotency_key="template", max_output_tokens=50,
        max_output_bytes=5000, timeout_s=10, max_attempts=1)
    (base / "request.json").write_text(json.dumps(req.body()))


def _gateway(base: Path, log=None, policies=None, delay=0.0):
    log = log or EventLog(base / "log.jsonl")
    calls = base / "calls.txt"

    def script(req, prompt, n):
        fd = os.open(calls, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"{req['idempotency_key']}\n".encode())
        finally:
            os.close(fd)
        if delay:
            time.sleep(delay)
        return I.ProviderReply("OK", json.dumps(RESPONSE).encode(),
                               {"input_tokens": 10, "output_tokens": 10},
                               {"provider": None, "model": None})

    return I.InferenceGateway(
        log, transcripts=EvidenceStore(base / "transcripts"),
        policies=policies or PolicyStore(log).load(),
        provider=I.FixtureProvider(PROVIDER, script)).load()


def _request(base: Path, key: str) -> I.InferenceRequest:
    body = json.loads((base / "request.json").read_text())
    body["idempotency_key"] = key
    return I.InferenceRequest.from_body(body)


# ---- module-level workers: spawn pickles them by reference ---------------
def _spend_worker(args):
    go, base, tag = args
    base = Path(base)
    gw = _gateway(base)
    prompt = (base / "prompt.bin").read_bytes()
    XP._wait_for_start(go)
    got = []
    for i in range(EACH):
        try:
            got.append(("ok", gw.infer(_request(base, f"{tag}-{i}"),
                                       prompt).outcome))
        except I.InferenceRefused as exc:
            got.append(("refused", exc.code))
    return got


def _same_question_worker(args):
    go, base = args
    base = Path(base)
    gw = _gateway(base, delay=0.5)
    prompt = (base / "prompt.bin").read_bytes()
    XP._wait_for_start(go)
    try:
        return ("answered", gw.infer(_request(base, "same"),
                                     prompt).response_sha256)
    except I.AttemptOutstanding:
        return ("outstanding", "")


def _race(worker, args, tmp_path) -> list:
    with mp.get_context("spawn").Pool(RACERS) as pool:
        pending = pool.map_async(worker, args)
        XP._line_up(tmp_path, RACERS, pending)
        return pending.get(timeout=PROCESS_DEADLINE_S * 20)


def _agree(base: Path) -> dict:
    """Both readers replay the history and agree on what is held."""
    log = EventLog(base / "log.jsonl")
    primary = _gateway(base).budget_usage("b1")
    sub = reconstruct_subsystems(log)
    assert sub.anomalies == [], sub.anomalies
    b = sub.inference_budgets["b1"]
    assert (b["calls"], b["cost"], b["in"], b["out"]) == (
        primary["calls"], primary["cost_micro"], primary["input_tokens"],
        primary["output_tokens"])
    return primary


def test_four_processes_cannot_spend_past_one_budget(tmp_path):
    base = tmp_path / "w"
    base.mkdir()
    _build(base)
    go = XP._go(tmp_path)
    got = _race(_spend_worker, [(go, str(base), t) for t in range(RACERS)],
                tmp_path)
    flat = [r for rs in got for r in rs]
    assert sum(1 for r in flat if r == ("ok", I.OK)) == ALLOWED, flat
    assert {r for r in flat if r[0] == "refused"} == {
        ("refused", "BUDGET_EXHAUSTED")}, flat
    assert len(flat) == RACERS * EACH
    calls = (base / "calls.txt").read_text().split()
    assert len(calls) == ALLOWED == len(set(calls))
    held = _agree(base)
    assert held["calls"] == ALLOWED
    refusals = [e for e in EventLog(base / "log.jsonl").read_verified()[1]
                if e.action == I.ACT_RESERVE and not e.payload["granted"]]
    assert len(refusals) == RACERS * EACH - ALLOWED


def test_four_processes_asking_one_question_call_the_provider_once(tmp_path):
    base = tmp_path / "w"
    base.mkdir()
    _build(base)
    go = XP._go(tmp_path)
    got = _race(_same_question_worker, [(go, str(base))] * RACERS, tmp_path)
    answered = {r[1] for r in got if r[0] == "answered"}
    assert len(answered) == 1, got
    assert (base / "calls.txt").read_text().split() == ["same"], got
    _, events = EventLog(base / "log.jsonl").read_verified()
    assert sum(e.action == I.ACT_RESERVE for e in events) == 1
    assert sum(e.action == I.ACT_RESULT for e in events) == 1
    _agree(base)
