"""The TLA+ models and the code they describe (Assurance B).

A model check is about the model. These tests are what connect it to the
implementation, and they are the reason a model here is worth having:

* the authority edge table is compared with ``authority.EDGES``, edge for
  edge, so the model and the gate cannot describe different machines;
* behaviours of the correct models -- TLC's seeded simulations -- are
  REPLAYED against the implementation, step by step, and must be accepted,
  with the implementation's state matching the model's;
* each safety counterexample of a broken variant is replayed too, and the
  implementation must refuse it before it reaches the violating state: the
  code does not have the bug the variant has;
* the protocols too heavy to replay here (concurrent append, governed task
  recovery) are tied to named tests of the implementation that state the
  same properties, and those tests must exist.

None of this makes the implementation formally verified. A replayed trace
shows the code agrees with the model on that trace; the traces are a sample.
What it rules out is a model and a code base that have quietly drifted apart.
The record is produced by ``tools/formal_check.py``; this file needs no Java.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_inference_gateway as G  # noqa: E402

from agent_substrate import authority as A  # noqa: E402
from agent_substrate import inference as I  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.invalidation import INVALIDATABLE, plan_from  # noqa: E402
from agent_substrate.policy import (Effect, PolicyStore, document,  # noqa: E402
                                    rule)
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "formal_check", ROOT / "tools" / "formal_check.py")
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)

RESULTS = json.loads(F.RESULTS.read_text())
TRACES = F.load_traces(F.TRACES.read_text())["traces"]
MODELS = {m["spec"]: m for m in F.MODELS}


def _rows(spec, *, outcome=None):
    return [r for r in RESULTS["results"] if r["spec"] == spec
            and (outcome is None or r["outcome"] == outcome)]


def _positive(spec) -> list:
    """Behaviours of the correct model: TLC's seeded simulations."""
    return list(TRACES.get(spec, ()))


def _negative(spec) -> list:
    return [r for r in _rows(spec, outcome="VIOLATED")
            if r["violated"] != F.TEMPORAL]


def _ids(traces) -> list:
    return [f"t{i}" for i in range(len(traces))]


# ------------------------------------------------------------- the record

def test_the_record_is_complete_as_expected_and_of_these_specifications():
    assert F.problems(RESULTS, F.load_traces(F.TRACES.read_text())) == []


def test_a_specification_edited_after_its_check_is_reported(monkeypatch):
    edited = dict(F.spec_digests(), LogAppend="0" * 64)
    monkeypatch.setattr(F, "spec_digests", lambda: edited)
    assert any("LogAppend" in p and "changed" in p
               for p in F.problems(RESULTS))


def test_a_variant_that_was_not_refuted_is_reported():
    doctored = json.loads(json.dumps(RESULTS))
    row = next(r for r in doctored["results"]
               if r["variant"] == "double_dispatch")
    row.update(outcome=F.PASS, violated=None)
    assert any("double_dispatch" in p and "expected DispatchedOnce" in p
               for p in F.problems(doctored))


def test_every_broken_variant_is_caught_and_every_model_explored():
    for model in F.MODELS:
        rows = {r["variant"]: r for r in _rows(model["spec"])}
        assert rows["none"]["outcome"] == F.PASS
        assert rows["none"]["distinct_states"] > 50
        assert len(rows) >= 4, f"{model['spec']}: too few broken variants"
        assert all(r["outcome"] == "VIOLATED" for v, r in rows.items()
                   if v != "none")


@pytest.mark.parametrize("text, want", [
    ('<<"a", 1, TRUE>>', ["a", 1, True]),
    ('{"b", "a"}', {"$set": ["a", "b"]}),
    ('[r1 |-> {}, r2 |-> {"r1"}]', {"r1": {"$set": []},
                                    "r2": {"$set": ["r1"]}}),
    ('(<<"r1", 1>> :> "none" @@ <<"r1", 2>> :> 0)',
     {"$fn": [[["r1", 1], "none"], [["r1", 2], 0]]}),
    ('<<[intent |-> <<"p1", 1>>, prev |-> 0, torn |-> FALSE]>>',
     [{"intent": ["p1", 1], "prev": 0, "torn": False}]),
])
def test_tlc_values_are_read_faithfully(text, want):
    assert F.parse_value(text) == want


def test_a_value_tlc_never_prints_is_refused():
    with pytest.raises(F.FormalError):
        F.parse_value("<<1, 2")


# ------------------------------------------- protocol 1: authority edges

def _model_edges() -> set:
    text = (F.FORMAL / "AuthorityPromotion.tla").read_text()
    block = text.split("\\* BEGIN EDGES", 1)[1].split("\\* END EDGES", 1)[0]
    edges = set()
    for m in re.finditer(r'src \|-> "(\w+)",\s*dst \|-> "(\w+)",\s*'
                         r'roles \|-> \{([^}]*)\},\s*distinct \|-> (\w+)',
                         block):
        roles = frozenset(re.findall(r'"(\w+)"', m.group(3)))
        edges.add((m.group(1), m.group(2), roles, m.group(4) == "TRUE"))
    return edges


def test_the_model_s_edges_are_the_gate_s():
    code = {(e.src.value, e.dst.value, frozenset(r.value for r in e.roles),
             e.requires_distinct_actor) for e in A.EDGES}
    assert _model_edges() == code and len(code) == 14


#: what each edge must cite, as the gate requires it
_DIG = "d" * 64


def _evidence(dst: str) -> dict:
    edge_keys = {"VERIFIED": "verification_report",
                 "REJECTED": "rejection_reason", "STALE": "invalidated_by",
                 "SUPERSEDED": "superseded_by", "REVOKED": "revocation_reason"}
    ev = {}
    if dst in edge_keys:
        ev[edge_keys[dst]] = _DIG
    if dst == "PROMOTED":
        ev = {"verification_report": _DIG, "policy_id": "promote"}
    return ev


class AuthorityReplay:
    """The real store and policy store, one record, driven by a trace."""

    def __init__(self, tmp: Path):
        self.log = EventLog(tmp / "log.jsonl")
        self.policies = PolicyStore(self.log).load()
        self.policies.publish(document(policy_id="promote", version=1,
                                       rules=(rule(rule_id="r",
                                                   effect=Effect.ALLOW,
                                                   actions=("*",),
                                                   subjects=("*",),
                                                   roles=("*",),
                                                   resources=("*",)),)),
                              actor="owner")
        self.store = AuthorityStore(self.log).load()
        self.store.bind_policies(actor="owner", publishers=("owner",))

    def step(self, act: list) -> None:
        kind = act[0]
        if kind == "Create":
            self.store.create(record_id="r", kind="result", proposer=act[1],
                              policy_id="promote")
        elif kind == "Transition":
            _, actor, role, _src, dst = act
            self.store.transition(
                record_id="r", dst=State(dst), actor=actor, role=Role(role),
                evidence=_evidence(dst),
                policy_id="promote" if dst == "PROMOTED" else None)
        elif kind == "Withdraw":
            self.policies.withdraw("promote", actor="owner",
                                   reason="the model withdrew it")
        else:
            raise AssertionError(f"unmapped action {act}")

    def state(self) -> str:
        self.store = AuthorityStore(self.log).load()
        rec = self.store.all_records().get("r")
        return rec.state.value if rec else "NONE"


@pytest.mark.parametrize("trace", _positive("AuthorityPromotion"),
                         ids=_ids(_positive("AuthorityPromotion")))
def test_authority_behaviours_of_the_model_are_the_store_s(tmp_path, trace):
    w = AuthorityReplay(tmp_path)
    for st in trace[1:]:
        w.step(st["last"])
        assert w.state() == st["state"], st["last"]
    assert reconstruct_subsystems(w.log).anomalies == []


@pytest.mark.parametrize("row", _negative("AuthorityPromotion"),
                         ids=lambda r: r["variant"])
def test_the_store_refuses_each_broken_variant_s_counterexample(tmp_path,
                                                                 row):
    w = AuthorityReplay(tmp_path)
    for i, st in enumerate(row["trace"][1:], 1):
        try:
            w.step(st["last"])
        except Exception:                      # noqa: BLE001 - any refusal
            assert w.state() == row["trace"][i - 1]["state"]
            return
    pytest.fail(f"{row['variant']}: the store let the whole counterexample "
                "through")


# ------------------------------------------- protocol 3: budget, replayed

class BudgetReplay:
    """The gateway, with one model tick = 30 s = LEASE_GRACE_S = the
    request's timeout.

    Costs differ in units: the model reserves Reserve_ = 2 and charges an
    OK call Usage = 1; the gateway reserves R (the worst case of the
    request) and charges an OK call u (its usage). Both are checked against
    the ledger the trace's own actions imply, each in its own units, and the
    gateway's cap R + u makes its budget decision the model's Cap = 3 in
    every state either can reach: one more reservation fits exactly when
    nothing is open or failed and at most one call has been answered."""

    USAGE = {"input_tokens": 10, "output_tokens": 5}

    def __init__(self, tmp: Path):
        self.w = G.World(tmp, publish=False)
        probe = self.req("probe")
        self.R = I.cost_micro(G.budget().prices, G.MODEL,
                              I.input_bound(probe.prompt_bytes),
                              probe.max_output_tokens)
        self.u = I.cost_micro(G.budget().prices, G.MODEL, **self.USAGE)
        c = MODELS["InferenceBudget"]["constants"]
        assert (int(c["Reserve_"]), int(c["Usage"]), int(c["Cap"])) == \
            (2, 1, 3) and self.R > self.u > 0
        self.w.gw.publish_budget(G.budget(max_cost_micro=self.R + self.u,
                                          max_attempts=2), actor="owner")
        self.gw = self.w.gateway()
        self.t0 = self.w.clock.t
        self.atts: dict = {}
        #: what each attempt is, by the trace's actions: open, ok, failed
        self.kind: dict = {}

    def ledger(self, reserve, usage) -> int:
        return sum(usage if k == "ok" else reserve if k in ("open", "failed")
                   else 0 for k in self.kind.values())

    def req(self, r):
        return self.w.request(r, timeout_s=30, max_attempts=2)

    def step(self, act: list, now: int) -> None:
        self.w.clock.t = self.t0 + 30 * now
        kind = act[0]
        key = tuple(act[1:3])
        if kind == "Reserve":
            att = self.gw._reserve(self.req(act[1]))
            assert att.attempt == act[2]
            self.atts[key] = att
            self.kind[key] = "open"
        elif kind == "Dispatch":
            self.gw._dispatch(self.atts[key])
        elif kind in ("Settle", "Recover"):
            att = self.atts[key]
            sent = att.dispatch_seq is not None
            if kind == "Recover":
                outcome = I.LOST if sent else I.NOT_SENT
            else:
                outcome = {"OK": I.OK, "ERROR": I.PROVIDER_ERROR,
                           "CANCELLED": I.CANCELLED}[act[3]]
            ok = outcome == I.OK
            self.gw._settle(att, self.req(act[1]), outcome,
                            usage=dict(self.USAGE) if ok else None,
                            response_sha256=self.w.transcripts.put(G._body())
                            if ok else None,
                            recovery=kind == "Recover")
            self.kind[key] = ("ok" if ok else "failed" if sent
                              else "unsent")
        elif kind not in ("Tick", "CallEnds"):
            raise AssertionError(f"unmapped action {act}")

    def held(self) -> int:
        return self.gw.budget_usage("b1")["cost_micro"]


_REFUSED = (I.InferenceRefused, I.InferenceError, I.AttemptOutstanding,
            I._Answered, I._Settled)


@pytest.mark.parametrize("trace", _positive("InferenceBudget"),
                         ids=_ids(_positive("InferenceBudget")))
def test_budget_behaviours_of_the_model_are_the_gateway_s(tmp_path, trace):
    b = BudgetReplay(tmp_path)
    for st in trace[1:]:
        b.step(st["last"], st["now"])
        assert st["held"] == b.ledger(2, 1), ("the model", st["last"])
        assert b.held() == b.ledger(b.R, b.u), ("the gateway", st["last"])
    fresh = b.w.gateway()
    assert fresh.budget_usage("b1") == b.gw.budget_usage("b1")
    assert reconstruct_subsystems(b.w.log).anomalies == []


@pytest.mark.parametrize("row", _negative("InferenceBudget"),
                         ids=lambda r: r["variant"])
def test_the_gateway_refuses_each_broken_variant_s_counterexample(tmp_path,
                                                                  row):
    b = BudgetReplay(tmp_path)
    for st in row["trace"][1:]:
        before, held = b.w.log.head().seq, b.held()
        try:
            b.step(st["last"], st["now"])
        except _REFUSED as exc:
            # A refused grant is recorded as a refusal; anything else
            # refused is not written at all. Either way nothing is held.
            written = b.w.records(I.ACT_RESERVE)[-1:] \
                if isinstance(exc, I.InferenceRefused) else []
            assert b.w.log.head().seq == before + len(written)
            assert all(e.payload["granted"] is False for e in written)
            assert b.held() == held
            return
    pytest.fail(f"{row['variant']}: the gateway let the whole "
                "counterexample through")


# --------------------------------------- protocol 5: invalidation planned

def _graph():
    c = MODELS["Invalidation"]["constants"]
    deps = {k: v["$set"] for k, v in F.parse_value(c["Deps"]).items()}
    roots = F.parse_value(c["Roots"])["$set"]
    return deps, roots


def _python_plan(st: dict) -> set:
    deps, roots = _graph()
    records = {rid: SimpleNamespace(state=State(s),
                                    depends_on=tuple(deps[rid]))
               for rid, s in st.items()}
    return set(plan_from(records, roots, origin="task:t").affected)


@pytest.mark.parametrize("trace", _positive("Invalidation"),
                         ids=_ids(_positive("Invalidation")))
def test_the_model_s_plans_are_the_planner_s(trace):
    """Every plan the model makes -- on withdrawal, and on each settle after
    a crash or a re-verification -- is the one agent_substrate.invalidation
    computes from the same states; and a record is marked exactly when the
    state machine has an edge for it to STALE."""
    for before, st in zip(trace, trace[1:]):
        act = st["last"]
        if act[0] in ("InvalidateOrigin", "Settle"):
            assert set(st["pending"]["$set"]) == _python_plan(before["st"])
        elif act[0] == "Mark":
            r = act[1]
            marked = st["st"][r] == "STALE" != before["st"][r]
            assert marked == (State(before["st"][r]) in INVALIDATABLE)
        elif act[0] == "Reverify":
            assert A.find_edge(State.STALE, State.UNDER_REVIEW)
            assert A.find_edge(State.UNDER_REVIEW, State.VERIFIED)


@pytest.mark.parametrize("variant", ["direct_only", "stop_at_skipped",
                                     "overeager"])
def test_the_planner_does_not_make_a_broken_variant_s_plan(variant):
    row = next(r for r in _rows("Invalidation") if r["variant"] == variant)
    before, st = next((a, b) for a, b in zip(row["trace"], row["trace"][1:])
                      if b["last"][0] == "InvalidateOrigin")
    assert set(st["pending"]["$set"]) != _python_plan(before["st"])


# ------------------------------------------ protocol 6: freshness, replayed

class FreshnessReplay:
    def __init__(self, tmp: Path):
        self.w = G.World(tmp, max_calls=50, max_calls_per_window=50)
        self.gw = self.w.gateway()
        self.ctx: dict = {}
        self.n = 0

    def step(self, act: list) -> None:
        kind = act[0]
        if kind == "Build":
            v = act[1]
            self.ctx[v] = self.w.build_context(
                policy_identity=f"{G.POLICY}@{v}", task=f"t{v}")
        elif kind == "Publish":
            self.w.policies.publish(G._policy(act[1]), actor="owner")
        elif kind == "Withdraw":
            self.w.policies.withdraw(G.POLICY, actor="owner",
                                     reason="the model withdrew it")
        elif kind in ("Decide", "Act"):
            self.n += 1
            ctx = self.ctx[act[1]]
            req = self.w.request(f"d{self.n}", context=ctx,
                                 prompt=I.render_prompt(ctx))
            att = self.gw._reserve(req)
            self.gw._settle(att, req, I.CANCELLED)
        elif kind != "Check":
            raise AssertionError(f"unmapped action {act}")


@pytest.mark.parametrize("trace", _positive("ContextFreshness"),
                         ids=_ids(_positive("ContextFreshness")))
def test_freshness_behaviours_of_the_model_are_the_gateway_s(tmp_path,
                                                             trace):
    f = FreshnessReplay(tmp_path)
    for st in trace[1:]:
        f.step(st["last"])
    assert reconstruct_subsystems(f.w.log).anomalies == []


@pytest.mark.parametrize("row", _negative("ContextFreshness"),
                         ids=lambda r: r["variant"])
def test_the_gateway_refuses_a_stale_context_where_a_variant_did_not(
        tmp_path, row):
    f = FreshnessReplay(tmp_path)
    *prefix, final = row["trace"][1:]
    for st in prefix:
        f.step(st["last"])
    with pytest.raises(I.InferenceRefused) as exc:
        f.step(final["last"])
    assert exc.value.code == "CONTEXT_STALE"


# ---------------- protocols 2 and 4: tied to the tests that state them

#: model property -> tests of the implementation stating the same property.
#: Replaying these protocols needs real processes (flock, subprocess tools);
#: the tests below do that.
BY_TESTS = {
    "LogAppend.ChainIntact": (
        "test_decided_under_lock.py", "test_agent_concurrency.py"),
    "LogAppend.NothingAfterATornLine": (
        "test_agent_fault_injection.py::"
        "test_the_log_refuses_to_grow_onto_a_partial_line",
        "test_agent_crash_recovery.py::"
        "test_a_partial_final_append_still_yields_every_complete_record"),
    "LogAppend.NoDuplicate": (
        "test_agent_idempotency.py::"
        "test_replay_accepts_a_byte_identical_retried_append",),
    "CrashRetry.OneTaskPerKey": (
        "test_agent_idempotency.py::"
        "test_resubmission_through_the_production_path_runs_the_work_once",
        "test_agent_idempotency.py::"
        "test_same_owner_same_key_same_request_returns_the_first_binding"),
    "CrashRetry.RecordedByTheLeaseHolder": (
        "test_t2_governed.py::"
        "test_an_execution_record_for_a_task_recovered_meanwhile_is_refused",
        "test_t2_governed.py::"
        "test_an_execution_record_under_a_lease_since_replaced_is_refused"),
    "CrashRetry.RecordedOnce": (
        "test_t2_governed.py::test_an_execution_record_for_a_task_finished_"
        "under_its_lease_is_refused",),
    "CrashRetry.ExternalEffectsAtMostOnce": (
        "test_agent_idempotency.py::"
        "test_a_stranded_external_task_is_uncertain_and_never_resumed",),
    "CrashRetry.ReachesAnOutcome": (
        "test_proposal_crash_recovery.py::"
        "test_recovery_from_each_stage_finishes_once_and_verified",
        "test_proposal_crash_recovery.py::"
        "test_a_lapsed_lease_is_taken_up_as_a_new_attempt"),
}


def _defined(path: Path) -> set:
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


@pytest.mark.parametrize("prop", sorted(BY_TESTS))
def test_each_unreplayed_property_is_stated_by_a_test_that_exists(prop):
    spec, name = prop.split(".")
    model = MODELS[spec]
    assert name in model["invariants"] + model["properties"], prop
    for ref in BY_TESTS[prop]:
        file, _, test = ref.partition("::")
        path = HERE / file
        assert path.is_file(), ref
        if test:
            assert test in _defined(path), ref
        else:
            assert any(n.startswith("test_") for n in _defined(path)), ref
