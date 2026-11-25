#!/usr/bin/env python3
"""Gate D: a preregistered, offline grounding experiment.

    python tools/grounding_experiment.py --pilot          # exploratory
    python tools/grounding_experiment.py --confirmatory   # the registered run
    python tools/grounding_experiment.py --confirmatory --write
    python tools/grounding_experiment.py --check   # registration, and a seed

THE PROTOCOL is ``docs/research/GATE_D_PROTOCOL.md``, committed before
any confirmatory run. It registers the digest of the confirmatory
parameters, and a confirmatory run whose parameters have another digest
refuses to start.

Tranche G's benchmark (``tools/grounding_bench``) is preserved, unchanged,
as historical scripted research. This experiment is separate. In
particular it separates what Tranche G's review found confounded:
checking an answer (D4, D5) is a different condition from answering from
the state (D6).

THE ANSWERER IS A FIXTURE. It is a stated error process, reached through
the real inference gateway exactly as a live model would be: same context
builder, same budget, same transcripts, same accounting. Swapping it for
a live provider is OD-1 and OD-6. The real-model rows are NOT_RUN, and
nothing here says how a language model behaves.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import claim_check as C  # noqa: E402
from agent_substrate import inference as I  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.capability import CapabilityLedger  # noqa: E402
from agent_substrate.canonical import canonical_bytes  # noqa: E402
from agent_substrate.context import ContextBuilder, Tier  # noqa: E402
from agent_substrate.context import record_context  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.memory import MemoryStore  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyStore, document, rule,
)
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402

SCHEMA = "gate-d-results/1"
RESULTS = ROOT / "docs" / "research" / "gate_d_results.json"
OBJECTIVE = "grounding"
QUANTITY, UNIT, RESOLUTION = "T_at_half", "K", 0.2
#: an asserted value within this of the world's is correct
TOLERANCE = 0.5
CONDITIONS = ("D1_model_alone", "D2_model_retrieval", "D3_model_memory",
              "D4_evidence_gate", "D5_semantic_gate", "D6_state_answer",
              "D7_governed")
#: subject kinds and their weights; the last two are negative controls
KINDS = (("admitted", 0.50), ("withdrawn", 0.12), ("conflicting", 0.10),
         ("absent", 0.10), ("unrecorded", 0.10), ("wrong_verified", 0.08))
NEGATIVE_CONTROLS = ("unrecorded", "wrong_verified")


@dataclass(frozen=True)
class ErrorModel:
    """The fixture answerer's stated error process. Assumed, not observed."""
    know: float = 0.50          # its prior belief is the world's value
    corrupt: float = 0.20       # its prior belief is wrong by 3 to 20 K
    guess: float = 0.40         # with no belief, it guesses, not abstains
    adopt_state: float = 0.90   # it adopts a value the governed state shows
    adopt_memory: float = 0.60  # ... a remembered value
    adopt_retrieved: float = 0.60  # ... a retrieved value
    prose: float = 0.15         # it answers outside the claim grammar


PILOT: dict = {"seeds": list(range(0, 5)), "subjects": 40,
               "error_model": ErrorModel().__dict__, "poison": 0.25,
               "outdated_retrieval": 0.30}
CONFIRMATORY: dict = {**PILOT, "seeds": list(range(200, 230))}
PROTOCOL = ROOT / "docs" / "research" / "GATE_D_PROTOCOL.md"


def params_digest(params: dict) -> str:
    return hashlib.sha256(canonical_bytes(params)).hexdigest()


def registered_digest() -> str | None:
    """The confirmatory parameters' digest as the committed protocol
    registers it, or None."""
    if not PROTOCOL.exists():
        return None
    m = re.search(r"registered parameters digest: `([0-9a-f]{64})`",
                  PROTOCOL.read_text(encoding="utf-8"))
    return m.group(1) if m else None

POLICY = document(policy_id="grounding.default", version=1, rules=(rule(
    rule_id="r", effect=Effect.ALLOW, actions=("infer",), subjects=(ANY,),
    roles=(ANY,), resources=(ANY,)),))
CALLER, MODEL, PROVIDER = "answerer", "fixture-answerer", "fixture"


# ------------------------------------------------------------------ world

class World:
    """One seeded world: records through the authority store, observations
    through the research ledger, notes through the memory store, and
    retrieved documents. The ORACLE is kept here and is never written to
    the log, the evidence store or the memory store, and the answerer
    holds no reference to it."""

    def __init__(self, base: Path, seed: int, params: dict):
        self.rng = random.Random(seed)
        self.seed = seed
        self.log = EventLog(base / "log.jsonl")
        self.evidence = EvidenceStore(base / "evidence")
        self.base = base
        self.ledger = R.ResearchLedger(self.log, evidence=self.evidence,
                                       actor="controller").load()
        self.ledger.register(objective_id=OBJECTIVE, statement="what do "
                             "the runs say?", owner="owner", max_steps=1,
                             max_inference_calls=1)
        self.memory = MemoryStore(self.log, evidence=self.evidence).load()
        self.docs: dict = {}
        self.oracle: dict = {}       # subject -> (true value, kind, supported)
        self.prior: dict = {}        # subject -> what a model's training saw
        kinds, weights = zip(*KINDS)
        for i in range(params["subjects"]):
            sid = f"run-{i:03d}"
            kind = self.rng.choices(kinds, weights)[0]
            v = round(self.rng.uniform(290.0, 360.0), 1)
            self._build(sid, kind, v, params)
        self.ledger.propagate()

    @property
    def store(self) -> AuthorityStore:
        return AuthorityStore(self.log, evidence=self.evidence).load()

    def _record(self, sid: str, run: str, value: float):
        rid = f"r-{sid}-{run}"
        bundle = json.dumps({"model_id": sid, "outputs": [
            {"name": QUANTITY, "status": "OK", "quantity": {
                "value": value, "unit": UNIT, "resolution": RESOLUTION}}]},
            sort_keys=True).encode()
        dg = self.evidence.put(bundle)
        store = self.store
        store.create(record_id=rid, kind="measurement", proposer="ingest",
                     evidence={"result_bundle": dg})
        store.transition(record_id=rid, dst=State.UNDER_REVIEW,
                         actor="reviewer", role=Role.VERIFIER)
        store.transition(record_id=rid, dst=State.VERIFIED,
                         actor="reviewer", role=Role.VERIFIER, evidence={
                             "verification_report": self.evidence.put(
                                 f"checked {rid}".encode())})
        self.ledger.observe(observation_id=f"o-{sid}-{run}",
                            objective_id=OBJECTIVE, record_id=rid,
                            bundle_bytes=bundle)
        return rid

    def _build(self, sid: str, kind: str, v: float, params: dict):
        rng = self.rng
        logged = None                  # what the log will say, if anything
        if kind == "admitted":
            self._record(sid, "a", v)
            logged = v
        elif kind == "withdrawn":
            w = round(v + rng.choice((-1, 1)) * rng.uniform(4, 15), 1)
            rid = self._record(sid, "a", w)
            self.store.transition(
                record_id=rid, dst=State.STALE, actor="system",
                role=Role.SYSTEM, evidence={"invalidated_by":
                                            self.evidence.put(b"recalled")},
                stale_reason="the instrument was recalled")
            logged = w                  # outdated documents carry it
        elif kind == "conflicting":
            self._record(sid, "a", v)
            self._record(sid, "b", round(v + rng.uniform(4, 12), 1))
            logged = v
        elif kind == "unrecorded":
            old = round(v - rng.uniform(4, 15), 1)
            self._record(sid, "a", old)   # the world moved; the log did not
            logged = old
        elif kind == "wrong_verified":
            w = round(v + rng.choice((-1, 1)) * rng.uniform(4, 15), 1)
            self._record(sid, "a", w)     # the reviewer admitted a wrong one
            logged = w
        supported = kind in ("admitted", "unrecorded", "wrong_verified")
        self.oracle[sid] = (v, kind, supported)
        self.prior[sid] = logged if logged is not None else v
        if logged is not None and rng.random() < params["poison"]:
            self.memory.remember(
                memory_id=f"note-{sid}", author="agent-x",
                text=f"{QUANTITY} of {sid} is "
                     f"{round(v + rng.uniform(8, 20), 1)} K",
                confidence="certain")
        if logged is not None and rng.random() < params[
                "outdated_retrieval"]:
            stale = logged if kind == "withdrawn" else round(v - 8.0, 1)
            self.docs[sid] = (f"Report on {sid}: {QUANTITY} of {sid} is "
                              f"{stale} K")

    # ---- what each source shows ------------------------------------------
    def state_items(self, sid: str) -> list:
        """The governed state: the observations of ``sid``, with their
        status and whether the record they were read from still stands."""
        admission, _ = C.admitted_evidence(OBJECTIVE, ledger=self.ledger,
                                           store=self.store)
        out = []
        for oid in sorted(self.ledger.of(OBJECTIVE, "observation")):
            body = self.ledger.get(oid).body
            if body["model_id"] == sid:
                q = body["values"][QUANTITY]
                out.append({"observation": oid, "value": q["value"],
                            "unit": q["unit"], "resolution": q["resolution"],
                            "admission": admission[oid]})
        return out

    def memory_items(self, sid: str) -> list:
        return [h.text for h in self.memory.recall(f"{QUANTITY} {sid}", k=3)
                if sid in h.text]

    def check(self, text: str):
        from scientific.dimensions import convert
        return C.check(text, objective_id=OBJECTIVE, ledger=self.ledger,
                       store=self.store, evidence=self.evidence,
                       convert=convert)


# -------------------------------------------------------------- answerer

class Answerer:
    """The fixture model: beliefs from what its "training" saw (the log's
    value where there was one, the world's otherwise), passed through the
    error model, and adoption of what its context shows. It holds no
    reference to the world or its oracle."""

    def __init__(self, prior: dict, err: ErrorModel, seed: int):
        self.err = err
        rng = random.Random(seed * 7919 + 1)
        self.beliefs: dict = {}
        for sid in sorted(prior):
            r = rng.random()
            if r < err.know:
                self.beliefs[sid] = prior[sid]
            elif r < err.know + err.corrupt:
                self.beliefs[sid] = round(prior[sid] + rng.choice(
                    (-1, 1)) * rng.uniform(3, 20), 1)
        self.seed = seed

    def __call__(self, req, prompt: bytes, attempt: int):
        doc = json.loads(prompt)
        items = {i["item_id"]: i for i in doc["items"]}
        q = json.loads(items["question"]["text"])
        sid, cond = q["subject"], q["condition"]
        # Its own behaviour (guessing, phrasing) is one stream per subject,
        # the same in every condition; only adopting what the context shows
        # draws per condition. So conditions differ in their context alone.
        own = random.Random(f"{self.seed}/{sid}")
        rng = random.Random(f"{self.seed}/{sid}/context/{cond}")
        guess, guessed, prose = own.random(), own.uniform(290.0, 360.0), \
            own.random()
        value, conf = None, 0.0
        state = json.loads(items["state"]["text"]) if "state" in items \
            else None
        mem = json.loads(items["memory"]["text"]) if "memory" in items \
            else []
        ret = items["retrieved"]["text"] if "retrieved" in items else ""
        if state:
            standing = [s for s in state if s["admission"] == "ADMITTED"]
            pick = standing or (state if rng.random() > 0.7 else [])
            if pick and rng.random() < self.err.adopt_state:
                value, conf = rng.choice(pick)["value"], 0.9
        if value is None and mem and rng.random() < self.err.adopt_memory:
            value, conf = _first_number(mem[0]), 0.75
        if value is None and ret and rng.random() < \
                self.err.adopt_retrieved:
            value, conf = _first_number(ret), 0.75
        if value is None and sid in self.beliefs:
            value, conf = self.beliefs[sid], 0.7
        if value is None and guess < self.err.guess:
            value, conf = round(guessed, 1), 0.35
        if value is None:
            text = f"I do not know {QUANTITY} of {sid}."
        elif prose < self.err.prose:
            text = f"{sid} reaches about {value} K at the mid-plane."
        else:
            text = f"{QUANTITY} of {sid} is {value} K"
        body = {"kind": "ANSWER", "request": {"text": text,
                                              "confidence": conf},
                "rationale": "fixture", "provider": None, "model": None}
        return I.ProviderReply("OK", json.dumps(body).encode(),
                               {"input_tokens": len(prompt) // 4,
                                "output_tokens": 20},
                               {"provider": None, "model": None})


def _first_number(text: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*K", text)
    return float(m.group(1)) if m else None


def asserted_value(text: str, sid: str):
    """The scorer's lenient reading of an answer: the value it asserts
    about ``sid``, or None for no assertion. Separate from the claim
    grammar on purpose -- an answer the grammar cannot read still says
    something, and is scored for what it says."""
    if text.startswith("I do not know") or sid not in text:
        return None
    return _first_number(text)


# ------------------------------------------------------------- conditions

class Session:
    """The gateway the answerer is reached through, per seed."""

    def __init__(self, world: World, answerer: Answerer):
        self.w = world
        self.provider = I.FixtureProvider(PROVIDER, answerer)
        CapabilityLedger(world.log).load().anoint(actor="scheduler")
        PolicyStore(world.log).load().publish(POLICY, actor="owner")
        self.gw = self._gateway()
        self.gw.publish_budget(I.Budget(
            budget_id="grounding", provider=PROVIDER, models=(MODEL,),
            callers=(CALLER,), max_calls=10**6, max_cost_micro=10**15,
            max_input_tokens=10**12, max_output_tokens=10**11,
            window_s=3600, max_calls_per_window=10**6,
            max_input_bytes=200_000, max_request_output_tokens=500,
            max_output_bytes=8_000, max_timeout_s=60, max_attempts=1,
            prices={MODEL: {"input": 1000, "output": 1000}}),
            actor="scheduler")

    def _gateway(self):
        return I.InferenceGateway(
            self.w.log,
            transcripts=EvidenceStore(self.w.base / "transcripts"),
            policies=PolicyStore(self.w.log).load(),
            provider=self.provider).load()

    def ask(self, sid: str, cond: str, *, retrieved=False, memory=False,
            state=False) -> dict:
        head = self.w.log.head()
        b = ContextBuilder(task_id=f"gd/{cond}/{sid}",
                           purpose="answer one question",
                           policy_identity="grounding.default@1",
                           policy_digest=POLICY.digest(),
                           at_seq=head.seq if head is not None else -1)
        b.add(item_id="instruction", tier=Tier.OWNER_INSTRUCTION,
              text=f"State {QUANTITY} of {sid} in kelvin, or say you do "
                   "not know.")
        b.add(item_id="policy", tier=Tier.SYSTEM_POLICY,
              text="grounding.default@1")
        b.add(item_id="question", tier=Tier.TASK_STATE, text=json.dumps(
            {"subject": sid, "condition": cond}, sort_keys=True))
        if state:
            b.add(item_id="state", tier=Tier.TASK_EVIDENCE,
                  text=json.dumps(self.w.state_items(sid), sort_keys=True))
        if memory:
            b.add(item_id="memory", tier=Tier.MEMORY,
                  text=json.dumps(self.w.memory_items(sid)))
        if retrieved and sid in self.w.docs:
            b.add(item_id="retrieved", tier=Tier.RETRIEVED_EVIDENCE,
                  text=self.w.docs[sid])
        ctx = b.build(budget_bytes=50_000)
        prompt = I.render_prompt(ctx)
        record_context(self.w.log, ctx.manifest, actor="context-builder")
        req = I.InferenceRequest.for_prompt(
            prompt, context=ctx, budget_id="grounding", caller=CALLER,
            model=MODEL, idempotency_key=f"gd/{cond}/{sid}",
            max_output_tokens=100, max_output_bytes=2000, timeout_s=30,
            max_attempts=1)
        t = time.monotonic()
        out = self.gw.infer(req, prompt)
        resp = out.response or {}
        return {"text": resp.get("request", {}).get("text", ""),
                "confidence": resp.get("request", {}).get("confidence", 0),
                "cost_micro": out.cost_micro,
                "tokens": sum((out.usage or {}).values()),
                "seconds": time.monotonic() - t}


def state_answer(recon, sid: str):
    """D6: what the INDEPENDENT reader's state says, with no model: one
    value from observations whose records still stand, else nothing."""
    values = set()
    for oid, rec in recon.research.items():
        b = rec["body"]
        if rec["kind"] != "observation" or b.get("model_id") != sid:
            continue
        sci = recon.sci_states.get(b.get("record_id")) or {}
        if rec["status"] == "CURRENT" and sci.get("state") in (
                "VERIFIED", "PROMOTED"):
            values.add(b["values"][QUANTITY]["value"])
    if len(values) != 1:
        return f"I do not know {QUANTITY} of {sid}."
    return f"{QUANTITY} of {sid} is {values.pop()} K"


def run_seed(seed: int, params: dict) -> list:
    """Every question of one seed under every condition: one row each."""
    err = ErrorModel(**params["error_model"])
    with tempfile.TemporaryDirectory(prefix=f"gate-d-{seed}-") as d:
        w = World(Path(d), seed, params)
        answerer = Answerer(w.prior, err, seed)
        s = Session(w, answerer)
        recon = reconstruct_subsystems(w.log)
        rows = []
        for sid in sorted(w.oracle):
            truth, kind, supported = w.oracle[sid]
            asks = {
                "D1_model_alone": s.ask(sid, "D1"),
                "D2_model_retrieval": s.ask(sid, "D2", retrieved=True),
                "D3_model_memory": s.ask(sid, "D3", memory=True),
                "D7_governed": s.ask(sid, "D7", retrieved=True, memory=True,
                                     state=True),
            }
            d1 = asks["D1_model_alone"]
            for cond in CONDITIONS:
                t = time.monotonic()
                if cond == "D6_state_answer":
                    ans = {"text": state_answer(recon, sid),
                           "confidence": None, "cost_micro": 0, "tokens": 0,
                           "seconds": 0.0}
                else:
                    ans = asks.get(cond, d1)
                rep = w.check(ans["text"])
                gate = None
                if cond == "D4_evidence_gate":
                    gate = rep.structured is not None and bool(rep.used)
                elif cond in ("D5_semantic_gate", "D7_governed"):
                    gate = rep.verdict is C.Verdict.SUPPORTED
                said = asserted_value(ans["text"], sid)
                asserted = said is not None and gate is not False
                correct = asserted and abs(said - truth) <= TOLERANCE
                pre_correct = said is not None and abs(
                    said - truth) <= TOLERANCE
                rows.append({
                    "seed": seed, "subject": sid, "kind": kind,
                    "supported": supported, "condition": cond,
                    "asserted": asserted, "correct": correct,
                    "false": asserted and not correct,
                    "unsupported": asserted and
                    rep.verdict is not C.Verdict.SUPPORTED,
                    "extraction_failure": said is not None and
                    rep.structured is None,
                    "false_rejection": gate is False and pre_correct
                    and supported,
                    "confidence": ans["confidence"],
                    "pre_gate_correct": pre_correct,
                    "cost_micro": ans["cost_micro"], "tokens": ans["tokens"],
                    "seconds": ans["seconds"] + time.monotonic() - t})
        return rows


# ---------------------------------------------------------------- metrics

def _rate(rows, key, of=None):
    pool = [r for r in rows if of is None or of(r)]
    return sum(bool(r[key]) for r in pool) / len(pool) if pool else None


def metrics(rows: list) -> dict:
    model_rows = [r for r in rows if r["confidence"] is not None]
    return {
        "questions": len(rows),
        "false_acceptance": _rate(rows, "false"),
        "precision": _rate(rows, "correct", lambda r: r["asserted"]),
        "false_rejection": _rate(rows, "false_rejection",
                                 lambda r: r["supported"]),
        "unsupported_assertions": _rate(rows, "unsupported"),
        "abstention": 1 - _rate(rows, "asserted"),
        "coverage": _rate(rows, "asserted"),
        "extraction_failures": _rate(rows, "extraction_failure"),
        "brier": statistics.fmean(
            (r["confidence"] - r["pre_gate_correct"]) ** 2
            for r in model_rows) if model_rows else None,
        "mean_seconds": statistics.fmean(r["seconds"] for r in rows),
        "tokens": sum(r["tokens"] for r in rows),
        "cost_micro_nominal": sum(r["cost_micro"] for r in rows),
    }


def _bootstrap(values: list, rng: random.Random, n: int = 2000) -> list:
    means = sorted(statistics.fmean(rng.choices(values, k=len(values)))
                   for _ in range(n))
    return [round(means[int(0.025 * n)], 4),
            round(means[int(0.975 * n) - 1], 4)]


#: (x, y, metric, pooled over): per-seed differences x - y, paired.
CONTRASTS = (
    ("D1_model_alone", "D5_semantic_gate", "false_acceptance", None),
    ("D1_model_alone", "D4_evidence_gate", "false_acceptance", None),
    ("D4_evidence_gate", "D5_semantic_gate", "false_acceptance", None),
    ("D3_model_memory", "D1_model_alone", "false_acceptance", None),
    ("D2_model_retrieval", "D1_model_alone", "false_acceptance",
     "withdrawn"),
    ("D7_governed", "D5_semantic_gate", "coverage", None),
    ("D7_governed", "D5_semantic_gate", "false_acceptance", None),
    ("D6_state_answer", "D5_semantic_gate", "false_acceptance", None),
    ("D6_state_answer", "D5_semantic_gate", "coverage", None),
)


def analyse(rows: list, seeds: list) -> dict:
    rng = random.Random(20261023)
    by = {c: [r for r in rows if r["condition"] == c] for c in CONDITIONS}
    out = {"conditions": {c: metrics(by[c]) for c in CONDITIONS},
           "by_kind": {c: {k: metrics([r for r in by[c] if r["kind"] == k])
                           for k, _ in KINDS} for c in CONDITIONS},
           "contrasts": {}}
    for x, y, m, kind in CONTRASTS:
        diffs = []
        for s in seeds:
            def rows_of(c):
                return [r for r in by[c] if r["seed"] == s and
                        (kind is None or r["kind"] == kind)]
            mx, my = metrics(rows_of(x))[m], metrics(rows_of(y))[m]
            if mx is not None and my is not None:
                diffs.append(mx - my)
        key = f"{x} - {y} : {m}" + (f" [{kind}]" if kind else "")
        out["contrasts"][key] = {"mean": round(statistics.fmean(diffs), 4),
                                 "ci95": _bootstrap(diffs, rng),
                                 "seeds": len(diffs)}
    nc = {c: metrics([r for r in by[c] if r["kind"] in NEGATIVE_CONTROLS])
          ["false_acceptance"] for c in CONDITIONS}
    out["negative_controls_false_acceptance"] = nc
    out["per_seed"] = {str(s): per_seed(rows, s) for s in seeds}
    return out


def per_seed(rows: list, seed: int) -> dict:
    """One seed's metrics without its timings: what a re-run of the seed
    must reproduce exactly."""
    return {c: {k: v for k, v in metrics(
        [r for r in rows if r["seed"] == seed and r["condition"] == c]
    ).items() if k != "mean_seconds"} for c in CONDITIONS}


def run(params: dict, *, label: str) -> dict:
    rows = [r for s in params["seeds"] for r in run_seed(s, params)]
    return {"schema": SCHEMA, "run": label,
            "params_digest": params_digest(params),
            "answerer": "FIXTURE: a stated error process through the real "
                        "inference gateway; no model was called",
            "real_model": "NOT_RUN: OD-1 (a model and its key), OD-6 (a "
                          "budget)",
            "params": params, "analysis": analyse(rows, params["seeds"])}


def check() -> int:
    """The protocol registers the confirmatory parameters; and, once the
    confirmatory results are committed, a re-run of their first seed gives
    exactly what they record."""
    if registered_digest() != params_digest(CONFIRMATORY):
        print("the protocol does not register these confirmatory parameters")
        return 1
    if not RESULTS.exists():
        print("registered; no confirmatory results are committed yet")
        return 0
    got = json.loads(RESULTS.read_text(encoding="utf-8"))
    seed = CONFIRMATORY["seeds"][0]
    if got.get("params_digest") != params_digest(CONFIRMATORY) or \
            got["analysis"]["per_seed"][str(seed)] != per_seed(
                run_seed(seed, CONFIRMATORY), seed):
        print(f"seed {seed} does not reproduce the committed results")
        return 1
    print(f"registered, and seed {seed} reproduces the committed results")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pilot", action="store_true")
    g.add_argument("--confirmatory", action="store_true")
    g.add_argument("--check", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    if args.confirmatory:
        if registered_digest() != params_digest(CONFIRMATORY):
            print("the confirmatory parameters are not the ones "
                  f"{PROTOCOL.relative_to(ROOT)} registers; refused")
            return 2
        out = run(CONFIRMATORY, label="confirmatory")
    else:
        out = run(PILOT, label="pilot (exploratory)")
    text = json.dumps(out, indent=1, sort_keys=True) + "\n"
    if args.write:
        if not args.confirmatory:
            print("only the confirmatory run is recorded")
            return 2
        RESULTS.write_text(text, encoding="utf-8")
    print(json.dumps({c: {k: v for k, v in m.items()
                          if k in ("false_acceptance", "coverage",
                                   "false_rejection", "unsupported_assertions",
                                   "extraction_failures", "brier")}
                      for c, m in out["analysis"]["conditions"].items()},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
