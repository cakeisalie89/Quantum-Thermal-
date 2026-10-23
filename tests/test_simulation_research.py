"""Deterministic simulation of the research ledger (Assurance A).

Hypothesis drives TWO ledger processes over one log through interleavings of
the operations that interact: an agent's hypothesis received and recorded, a
reviewer verifying a result, an observation read from it, a verdict, a
conclusion, a result losing its standing, propagation, retraction, an agent
ASSERTING a verdict (as a model that wants its hypothesis supported would),
steps, and crashes. Every refused write must leave the log as it was. After
every step the projection is compared with what the simulation itself
knows, and with an ORACLE that decides a prediction independently of
``research.evaluate``:

* every evaluated verdict is the oracle's verdict on the values the
  simulation put in the bundles its basis observations were read from;
* a decided hypothesis never flips to the opposite verdict in one move, and a
  record that lost its currency never regains it;
* once propagation reaches its fixed point, no current observation is read
  from a record without standing, no decided hypothesis rests on a
  non-current observation, and no conclusion on an undecided hypothesis;
* steps are contiguous and within the objective's limit;
* a fresh replay agrees with each live process, and the second reader agrees
  with the replay and finds nothing.

What this does NOT cover: the controller's own scheduling (that is
``test_research_loop.py``), real model output, or results produced by a
real model run -- bundles here are written by the simulation.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine, initialize, invariant, precondition, rule,
)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate import events as events_module  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402

THRESHOLD = 400.0
MODEL = "thermal.slab_transient"
PRED = {"model_id": MODEL, "quantity": "T_at_half", "op": "<",
        "value": THRESHOLD, "unit": "K"}
HYPS = ("h1", "h2")
RECS = ("rec-1", "rec-2", "rec-3", "rec-4")
#: below, above, and straddling the threshold at resolution 0.5
VALUES = (350.0, 450.0, 399.8)
MAX_STEPS = 4
DECIDED = {"SUPPORTED", "REFUTED"}
TERMINAL = {"STALE", "RETRACTED", "SUPERSEDED"}


def oracle(values) -> str:
    """T_at_half < 400 K, decided from (value, resolution) pairs, written
    from the prediction's meaning rather than from ``research.evaluate``."""
    if not values:
        return "INCONCLUSIVE"
    each = set()
    for v, r in values:
        if v + r < THRESHOLD:
            each.add("SUPPORTED")
        elif v - r >= THRESHOLD:
            each.add("REFUTED")
        else:
            each.add("INCONCLUSIVE")
    return each.pop() if len(each) == 1 else "INCONCLUSIVE"


class ResearchSimulation(RuleBasedStateMachine):

    @initialize()
    def setup(self):
        # Durability is not what is simulated here -- a crash is a process
        # losing its memory, not the disk losing a write -- so the fsync on
        # every append is the one cost the simulation does without.
        self._fsync = events_module.os.fsync
        events_module.os.fsync = lambda fd: None
        self.tmp = Path(tempfile.mkdtemp(prefix="rssim-"))
        self.log = EventLog(self.tmp / "log.jsonl")
        self.evidence = EvidenceStore(self.tmp / "evidence")
        self.procs = [self._process(), self._process()]
        self.procs[0].register(objective_id="o1", statement="below 400 K?",
                               owner="owner", max_steps=MAX_STEPS,
                               max_inference_calls=MAX_STEPS)
        #: what the simulation itself knows, never read from the ledger
        self.record_state: dict = {}         # rid -> state it wrote
        self.record_value: dict = {}         # rid -> (value, resolution)
        self.bundle: dict = {}               # rid -> bytes
        self.obs_record: dict = {}           # oid -> rid
        self.hyps: list = []                 # hypotheses recorded
        self.basis: dict = {}
        #: research id -> status, as the last check of the log found it
        self.status: dict = {}
        self.checked_at = None
        self.steps = 0
        self.attempts = {"hypothesize": 0, "step": 0}

    def teardown(self):
        if hasattr(self, "_fsync"):
            events_module.os.fsync = self._fsync
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    def _process(self):
        return R.ResearchLedger(self.log, evidence=self.evidence).load()

    def _refused_unwritten(self, fn) -> bool:
        """Run a write. A refusal must leave the log as it was and loadable:
        a writer may refuse, it may not write what its replay refuses."""
        before = self.log.head().seq
        try:
            fn()
        except R.ResearchError:
            # an unchanged head is an unchanged history: it still loads
            assert self.log.head().seq == before, \
                "a refused research record was written"
            return True
        return False

    # ---- operations ---------------------------------------------------
    # Bounded rules: past these counts they only repeat a refusal already
    # seen, and crowd out the interleavings that matter.
    @precondition(lambda self: self.attempts["hypothesize"] < 4)
    @rule(p=st.integers(0, 1), hid=st.sampled_from(HYPS),
          deps=st.lists(st.sampled_from(HYPS), max_size=1, unique=True))
    def hypothesize(self, p, hid, deps):
        self.attempts["hypothesize"] += 1
        resp = {"kind": "HYPOTHESIS", "provider": None, "model": None,
                "rationale": f"proposal for {hid}",
                "request": {"hypothesis_id": hid, "statement": hid,
                            "prediction": dict(PRED), "depends_on": deps}}
        ctx = PR.assemble_context("q", retrieve=lambda q, k: {"hits": []})
        env = PR.ProposalEnvelope.parse(PR.envelope(
            resp, context=ctx, agent_id="agent-a",
            adapter=PR.RecordedFixtureAdapter("")))
        if env.proposal_id not in {e.target for e in
                                   self.log.read_verified()[1]}:
            self.log.append(actor=PR.INGRESS_ID,
                            action=PR.ACT_PROPOSAL_RECEIVE,
                            target=env.proposal_id,
                            payload={"envelope": env.record,
                                     "envelope_digest": env.digest(),
                                     "received_by": PR.INGRESS_ID,
                                     "authority": PR.AUTHORITY})
        if not self._refused_unwritten(lambda: self.procs[p].hypothesize(
                hypothesis_id=hid, objective_id="o1", statement=hid,
                prediction=dict(PRED), depends_on=deps,
                proposal_id=env.proposal_id, proposed_by="agent-a")):
            self.hyps.append(hid)

    def _hyps(self):
        return self.hyps

    @precondition(lambda self: len(self.record_state) < len(RECS))
    @rule(v=st.sampled_from(VALUES))
    def a_result_is_verified(self, v):
        self._verified(v)

    def _verified(self, v) -> str:
        """The governed path, as the log leaves it: a record with a result
        bundle, verified by a reviewer."""
        rid = RECS[len(self.record_state)]
        body = json.dumps({"model_id": MODEL, "outputs": [{
            "name": "T_at_half", "status": "OK",
            "quantity": {"value": v, "unit": "K", "resolution": 0.5}}]}
        ).encode()
        dg = self.evidence.put(body)
        self.log.append(actor="governed", action="record.create",
                        target=rid, payload={"record_id": rid, "evidence": {
                            "result_bundle": dg}})
        self.log.append(actor="reviewer", action="record.transition",
                        target=rid, payload={"record_id": rid,
                                             "dst": "VERIFIED"})
        self.record_state[rid] = "VERIFIED"
        self.record_value[rid] = (v, 0.5)
        self.bundle[rid] = body
        return rid

    @precondition(lambda self: set(self.bundle) - {
        r for r in self.obs_record.values()})
    @rule(p=st.integers(0, 1), i=st.integers(0, 2))
    def observe(self, p, i):
        todo = sorted(set(self.bundle) - set(self.obs_record.values()))
        self.observe_record(p, todo[i % len(todo)])

    def observe_record(self, p, rid):
        oid = "obs-" + rid
        if not self._refused_unwritten(lambda: self.procs[p].observe(
                observation_id=oid, objective_id="o1", record_id=rid,
                bundle_bytes=self.bundle[rid])):
            self.obs_record[oid] = rid
            assert self.record_state[rid] in R.STANDING

    @precondition(lambda self: "VERIFIED" in self.record_state.values())
    @rule(i=st.integers(0, 2))
    def a_result_loses_its_standing(self, i):
        live = sorted(r for r, s in self.record_state.items()
                      if s == "VERIFIED")
        self._loses_standing(live[i % len(live)])

    def _loses_standing(self, rid):
        self.log.append(actor="system", action="record.transition",
                        target=rid, payload={"record_id": rid,
                                             "dst": "STALE"})
        self.record_state[rid] = "STALE"

    def _decided(self):
        return sorted(h for h in self.hyps
                      if self.status.get(h) in DECIDED)

    # The two corners random interleaving reached too rarely for mutation
    # testing to see (SM7, SM10): a decided verdict met by evidence that
    # points the other way, and a decided verdict whose evidence goes.
    @precondition(lambda self: self._decided()
                  and len(self.record_state) < len(RECS))
    @rule(p=st.integers(0, 1), i=st.integers(0, 1))
    def the_evidence_turns(self, p, i):
        """New evidence opposes a decided verdict, and an agent asserts the
        opposite verdict on it alone. A decided hypothesis does not flip in
        one move: the assertion is refused, and evaluation finds the
        disagreement."""
        hid = self._decided()[i % len(self._decided())]
        was = self.status[hid]
        rid = self._verified(450.0 if was == "SUPPORTED" else 350.0)
        self.observe_record(p, rid)
        flip = "REFUTED" if was == "SUPPORTED" else "SUPPORTED"
        assert self._refused_unwritten(lambda: self.procs[p].set_status(
            hid, flip, reason="the latest says so", basis=["obs-" + rid])), \
            f"{hid} flipped {was} -> {flip} in one move"
        self._refused_unwritten(
            lambda: self.procs[p].evaluate_hypothesis(hid))

    @precondition(lambda self: any(
        self.record_state.get(self.obs_record.get(o)) == "VERIFIED"
        for h in self._decided() for o in self.basis.get(h, ())))
    @rule(p=st.integers(0, 1))
    def a_verdict_s_evidence_goes(self, p):
        """A result a decided verdict rests on loses its standing, and the
        loss is propagated."""
        hid = next(h for h in self._decided() if any(
            self.record_state.get(self.obs_record.get(o)) == "VERIFIED"
            for o in self.basis.get(h, ())))
        rid = next(self.obs_record[o] for o in self.basis[hid]
                   if self.record_state.get(self.obs_record.get(o))
                   == "VERIFIED")
        self._loses_standing(rid)
        self.propagate(p)

    @precondition(lambda self: self.obs_record and self._hyps())
    @rule(p=st.integers(0, 1), hid=st.sampled_from(HYPS))
    def evaluate(self, p, hid):
        if hid in self._hyps():
            self._refused_unwritten(
                lambda: self.procs[p].evaluate_hypothesis(hid))

    @precondition(lambda self: self.obs_record and self._hyps())
    @rule(p=st.integers(0, 1), hid=st.sampled_from(HYPS),
          status=st.sampled_from(sorted(R.EVALUATED)),
          obs=st.lists(st.sampled_from(RECS), min_size=1, max_size=3,
                       unique=True))
    def an_agent_asserts_a_verdict(self, p, hid, status, obs):
        """What a model that wants its hypothesis supported would do. It is
        written only if the observations it cites give that verdict."""
        basis = ["obs-" + r for r in obs if "obs-" + r in self.obs_record] \
            or ["obs-" + obs[0]]
        if not self._refused_unwritten(lambda: self.procs[p].set_status(
                hid, status, reason="asserted", basis=basis)):
            assert status == oracle([self.record_value[self.obs_record[o]]
                                     for o in basis]), (status, basis)

    @precondition(lambda self: self._hyps())
    @rule(p=st.integers(0, 1), hid=st.sampled_from(HYPS))
    def conclude(self, p, hid):
        rec = self._process()._recs.get(hid)
        was = rec.status if rec is not None else None
        if not self._refused_unwritten(lambda: self.procs[p].set_status(
                "o1", "CONCLUDED", reason=f"{hid} decided", basis=[hid])):
            assert was in DECIDED, f"concluded on {hid}, which was {was}"

    @precondition(lambda self: self._hyps())
    @rule(p=st.integers(0, 1), hid=st.sampled_from(HYPS),
          now=st.integers(0, 7))
    def retract(self, p, hid, now):
        """Rarer than the rest: a retraction ends a hypothesis for good."""
        if now:
            return
        self._refused_unwritten(lambda: self.procs[p].set_status(
            hid, "RETRACTED", reason="withdrawn by its author"))

    @precondition(lambda self: "STALE" in self.record_state.values())
    @rule(p=st.integers(0, 1))
    def propagate_lost_currency(self, p):
        self.propagate(p)

    def propagate(self, p):
        """Propagate, then check the fixed point against what the
        simulation knows of each record's standing."""
        self.procs[p].propagate()
        fresh = self._process()
        for rid, rec in fresh._recs.items():
            if rec.kind == "observation" and rec.status == "CURRENT":
                assert self.record_state[rec.body["record_id"]] in \
                    R.STANDING, f"{rid} current on a record without standing"
            if rec.kind == "hypothesis" and rec.status in DECIDED:
                assert all(fresh._recs[o].status == "CURRENT"
                           for o in rec.basis), f"{rid} rests on stale"
            if rec.kind == "objective" and rec.status == "CONCLUDED":
                assert fresh._recs[rec.basis[0]].status in DECIDED

    @precondition(lambda self: self.attempts["step"] < MAX_STEPS + 2)
    @rule(p=st.integers(0, 1))
    def step(self, p):
        self.attempts["step"] += 1
        if not self._refused_unwritten(lambda: self.procs[p].record_step(
                objective_id="o1", step=self.steps + 1, proposal_id=None,
                act="INFER", outcome="OK")):
            self.steps += 1
        assert self.steps <= MAX_STEPS

    @rule(p=st.integers(0, 1))
    def crash(self, p):
        self.procs[p] = self._process()

    # ---- invariants ---------------------------------------------------
    # One invariant over one replay: each replay of the log is the cost of a
    # step, and a failing example is shrunk by replaying it many times.
    @invariant()
    def the_record_holds(self):
        if not hasattr(self, "log"):
            return
        for proc in self.procs:
            proc._fold_new()
        head = self.log.head().seq
        if head == self.checked_at:
            # nothing was written since the last check: a fresh replay and
            # the second reader would read the same history again
            want = {rid: r.status for rid, r in self.procs[0]._recs.items()}
            assert want == self.status
            return
        self.checked_at = head
        fresh = self._process()
        for rid, rec in fresh._recs.items():
            # every evaluated verdict is the oracle's
            if rec.kind == "hypothesis" and rec.status in R.EVALUATED:
                got = [self.record_value[self.obs_record[o]]
                       for o in rec.basis]
                assert rec.status == oracle(got), (rid, rec.status, got)
            # no flip in one move, and no return from a terminal status
            seq = [s for _seq, s in rec.history] + [rec.status]
            for a, b in zip(seq, seq[1:]):
                assert {a, b} != DECIDED, (rid, seq)
                assert a not in TERMINAL, (rid, seq)
        # every reader agrees
        want = {rid: r.status for rid, r in fresh._recs.items()}
        self.status = want
        self.basis = {rid: r.basis for rid, r in fresh._recs.items()}
        for proc in self.procs:
            proc._fold_new()
            assert {rid: r.status for rid, r in proc._recs.items()} == want
        sub = reconstruct_subsystems(self.log)
        assert sub.anomalies == [], sub.anomalies
        assert {rid: r["status"] for rid, r in sub.research.items()} == want
        assert sub.research_steps.get("o1", 0) == self.steps


# Shorter programs, more of them, and one bug at a time: a failing example
# must SHRINK within a bounded time, or the counterexample nobody can read
# is the only thing the run produces (mutation testing found the shrink of
# a 50-step program taking over five minutes).
ResearchSimulation.TestCase.settings = settings(
    max_examples=800, stateful_step_count=30, deadline=None,
    derandomize=True, database=None, report_multiple_bugs=False,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much])
test_research_simulation = ResearchSimulation.TestCase


def _decided_on_one_result(m):
    m.setup()
    m.hypothesize(0, "h1", [])
    m.a_result_is_verified(350.0)
    m.observe(0, 0)
    m.evaluate(1, "h1")
    m.the_record_holds()
    assert m.status["h1"] == "SUPPORTED"


@pytest.mark.parametrize("corner", ["the_evidence_turns",
                                    "a_verdict_s_evidence_goes"])
def test_each_corner_on_a_fixed_path(corner):
    """The corners the search was given rules for (SM7, SM10), each reached
    on one fixed path through the same machine, its rules and its invariant.

    A derandomized search explores whatever its seed and the code it drives
    lead it to, and a change to that code moves it: after Assurance F the
    search stopped reaching a decided verdict met by opposing evidence, and
    a flip in one move went unseen by this file (D-2026-169). The search
    stays; these paths do not depend on it."""
    m = ResearchSimulation()
    try:
        _decided_on_one_result(m)
        if corner == "the_evidence_turns":
            m.the_evidence_turns(1, 0)       # no flip in one move
        else:
            m.a_verdict_s_evidence_goes(0)   # no verdict without its basis
        m.the_record_holds()
        assert m.status["h1"] not in DECIDED
    finally:
        m.teardown()
