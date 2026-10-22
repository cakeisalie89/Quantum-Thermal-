"""Deterministic simulation of the inference gateway (Assurance A).

Hypothesis drives TWO gateway processes over one log -- each with its own
projection, as two processes have -- through interleavings of the operations
that interact: reserving, dispatching, a provider call, settling (twice, as a
duplicated response would), crashing (a process loses everything it held in
memory), the clock running past leases, recovery, and a policy change that
makes every recorded context stale. After every step the invariants are
checked against the log, and the real system is compared with an ABSTRACT
model of the protocol (the one ``docs/formal/InferenceBudget.tla`` states):

* nothing held against the budget ever exceeds it, and nothing the provider
  was asked to do was asked without a dispatch record before it;
* a request has at most one open attempt and at most one OK attempt;
* every attempt is settled at most once;
* a fresh replay agrees with each live process, and the second reader agrees
  with the replay and finds nothing;
* the abstract model and the implementation agree on what is held and on
  which attempts are open.

Seeds are derandomized, so a failure reproduces, and Hypothesis shrinks it to
a minimal sequence of operations. What this does NOT cover: real process
scheduling (the interleavings are of whole operations, each of which decides
under the writer lock), real clocks, and any provider behaviour beyond the
replies scripted here.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine, initialize, invariant, rule,
)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_inference_gateway as G  # noqa: E402

from agent_substrate import inference as I  # noqa: E402
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402

KEYS = ("q1", "q2", "q3")


class Abstract:
    """The protocol, as data: what InferenceBudget.tla's variables hold."""

    def __init__(self, max_cost: int):
        self.max_cost = max_cost
        self.held = 0
        self.open: dict = {}           # (key, n) -> reserved cost
        self.dispatched: set = set()
        self.ok: set = set()
        self.settled: set = set()

    def reserve(self, key, n, cost):
        self.held += cost
        self.open[(key, n)] = cost

    def settle(self, key, n, ok, usage_cost):
        """The settlement rule, stated here and NOT read from the
        implementation: an attempt never dispatched costs nothing, an OK one
        costs its usage, any other dispatched one its whole reservation."""
        reserved = self.open.pop((key, n))
        if (key, n) not in self.dispatched:
            cost = 0
        elif ok:
            cost = usage_cost
        else:
            cost = reserved
        self.held += cost - reserved
        self.settled.add((key, n))
        if ok:
            self.ok.add(key)


class GatewaySimulation(RuleBasedStateMachine):

    @initialize(budget_calls=st.integers(min_value=1, max_value=3))
    def setup(self, budget_calls):
        self.tmp = Path(tempfile.mkdtemp(prefix="gwsim-"))
        self.replies: dict = {}
        world = G.World(self.tmp, self._reply, publish=False)
        self.world = world
        req = world.request("probe")
        self.cost = I.cost_micro(G.budget().prices, G.MODEL,
                                 I.input_bound(req.prompt_bytes),
                                 req.max_output_tokens)
        self.max_cost = self.cost * budget_calls
        world.gw.publish_budget(G.budget(max_cost_micro=self.max_cost,
                                         max_attempts=3), actor="owner")
        self.procs = [world.gateway(), world.gateway()]
        self.model = Abstract(self.max_cost)
        self.calls: list = []
        self.stale = False

    def teardown(self):
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    def _reply(self, req, prompt, n):
        self.calls.append((req["idempotency_key"], n))
        return G.ok()

    def _req(self, key):
        return self.world.request(key, max_attempts=3)

    # ---- operations ---------------------------------------------------
    @rule(p=st.integers(0, 1), key=st.sampled_from(KEYS))
    def reserve(self, p, key):
        gw, req = self.procs[p], self._req(key)
        try:
            att = gw._reserve(req)
        except (I.InferenceRefused, I.AttemptOutstanding, I._Answered):
            return
        assert not self.stale, "a stale context was granted a reservation"
        self.model.reserve(key, att.attempt, att.reserved_cost)

    def _open(self, p):
        gw = self.procs[p]
        gw._fold_new()
        return sorted((k, v) for k, v in gw._attempts.items()
                      if v[0].result is None)

    @rule(p=st.integers(0, 1), i=st.integers(0, 5))
    def dispatch_and_call(self, p, i):
        """Dispatch an open attempt, then (if it was dispatched) ask the
        provider -- the order the gateway keeps."""
        opened = self._open(p)
        if not opened:
            return
        (rid, n), (att, req) = opened[i % len(opened)]
        try:
            self.procs[p]._dispatch(att)
        except (I._Settled, I.InferenceError):
            return
        self.model.dispatched.add((req.idempotency_key, n))
        self.calls.append((req.idempotency_key, n))

    @rule(p=st.integers(0, 1), i=st.integers(0, 5), ok=st.booleans())
    def settle(self, p, i, ok):
        """Settle an open attempt; run it twice to stand for a duplicated
        response -- the second must change nothing."""
        opened = self._open(p)
        if not opened:
            return
        (rid, n), (att, req) = opened[i % len(opened)]
        sent = att.dispatch_seq is not None
        outcome = (I.OK if ok and sent else
                   I.PROVIDER_ERROR if sent else I.CANCELLED)
        usage = dict(G.USAGE) if outcome == I.OK else None
        dg = (self.world.transcripts.put(G._body()) if outcome == I.OK
              else None)
        for _ in range(2):
            try:
                self.procs[p]._settle(att, req, outcome, usage=usage,
                                      response_sha256=dg)
            except I._Settled:
                continue
            except I.InferenceError:
                return
            self.model.settle(req.idempotency_key, n, outcome == I.OK,
                              I.cost_micro(G.budget().prices, G.MODEL,
                                           G.USAGE["input_tokens"],
                                           G.USAGE["output_tokens"]))

    @rule(p=st.integers(0, 1), i=st.integers(0, 5),
          outcome=st.sampled_from(I.OUTCOMES))
    def settle_out_of_turn(self, p, i, outcome):
        """Any outcome for any open attempt, as a confused or hostile caller
        would write it. The gateway writes it only if the protocol allows
        it -- stated here, not read from the gateway -- and a refusal leaves
        the log as it was."""
        opened = self._open(p)
        if not opened:
            return
        (rid, n), (att, req) = opened[i % len(opened)]
        sent = att.dispatch_seq is not None
        lapsed = self.world.clock.t > att.lease_until
        # CANCELLED may end an attempt before or after its dispatch; every
        # other outcome says which it was
        allowed = outcome not in I.RECOVERY_OUTCOMES and (
            outcome == I.CANCELLED or (outcome in I.NEEDS_DISPATCH) == sent)
        before = self.world.log.head().seq
        try:
            self.procs[p]._settle(att, req, outcome,
                                  recovery=outcome in I.RECOVERY_OUTCOMES)
        except (I._Settled, I.InferenceError):
            assert self.world.log.head().seq == before, \
                "a refused settlement was written"
            return
        assert allowed or (outcome in I.RECOVERY_OUTCOMES and lapsed and
                           (outcome == I.LOST) == sent), (outcome, sent)
        self.model.settle(req.idempotency_key, n, outcome == I.OK, 0)

    @rule(p=st.integers(0, 1))
    def crash(self, p):
        """The process dies; a new one starts from the log."""
        self.procs[p] = self.world.gateway()

    @rule()
    def time_passes(self):
        self.world.clock.t += 5 + I.LEASE_GRACE_S + 30

    @rule(p=st.integers(0, 1))
    def recover(self, p):
        for out in self.procs[p].recover():
            key = next(r.idempotency_key for (rid, n), (a, r)
                       in self.procs[p]._attempts.items()
                       if rid == out.request_id)
            self.model.settle(key, out.attempt, False, 0)

    @rule(now=st.integers(0, 4))
    def policy_changes(self, now):
        """Rarer than the rest: after it, no context is current and nothing
        more is reserved."""
        if not now and not self.stale:
            self.world.policies.publish(G._policy(2), actor="owner")
            self.stale = True

    # ---- invariants ---------------------------------------------------
    @invariant()
    def nothing_held_exceeds_the_budget(self):
        if not hasattr(self, "world"):
            return
        fresh = self.world.gateway()
        held = fresh.budget_usage("b1")
        assert held["cost_micro"] <= self.max_cost, held
        assert held["cost_micro"] == self.model.held, (held, self.model.held)

    @invariant()
    def at_most_one_open_and_one_ok_attempt_per_request(self):
        if not hasattr(self, "world"):
            return
        fresh = self.world.gateway()
        for rid in {r for (r, _n) in fresh._attempts}:
            outs = [o for _n, o in fresh.attempts(rid)]
            assert outs.count(None) <= 1, outs
            assert outs.count(I.OK) <= 1, outs

    @invariant()
    def the_model_and_the_implementation_agree(self):
        if not hasattr(self, "world"):
            return
        fresh = self.world.gateway()
        live = {(r.idempotency_key, n) for (rid, n), (a, r)
                in fresh._attempts.items() if a.result is None}
        assert live == set(self.model.open), (live, self.model.open)
        for (rid, n), (a, r) in fresh._attempts.items():
            if a.result and a.result["outcome"] in I.NEEDS_DISPATCH:
                assert (r.idempotency_key, n) in self.model.dispatched, (
                    "an attempt settled as sent that nobody dispatched")

    @invariant()
    def every_reader_agrees(self):
        if not hasattr(self, "world"):
            return
        fresh = self.world.gateway()
        for proc in self.procs:
            proc._fold_new()
            assert proc.budget_usage("b1") == fresh.budget_usage("b1")
        sub = reconstruct_subsystems(self.world.log)
        assert sub.anomalies == [], sub.anomalies
        assert sub.inference_budgets["b1"]["cost"] == \
            fresh.budget_usage("b1")["cost_micro"]


GatewaySimulation.TestCase.settings = settings(
    max_examples=200, stateful_step_count=40, deadline=None,
    derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much])
test_gateway_simulation = GatewaySimulation.TestCase
