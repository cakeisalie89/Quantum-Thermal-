"""Assurance G: what makes the evaluation's zeros mean anything.

A predicate that could never be true would report 0% whatever the system
did, so every scenario has a positive control: the control it names is
removed (or, where no route exists to remove, the goal is reached another
way), and the same scenario's predicate must report the attack succeeded.

This is the suite tools/mutations/adversarial_eval_harness.json runs, which
makes the harness itself vacuous: an obeying model that never obeys, a
predicate that cannot be true, a predicate blinded by the control it
watches.

The models are scripts. Nothing here says how a language model behaves.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import context as C  # noqa: E402
from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.governed_model import GovernedModelRuns  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyStore, document, rule,
)
from tools import adversarial_eval as E  # noqa: E402

SC = {s.id: s for s in E.SCENARIOS}

# Each removes the control a scenario names (or reaches its goal another
# way, where no route exists to remove) and asks the same predicate.

def _no_conclusion_rule(mp):
    orig = R.ResearchLedger._status_problem

    def patched(self, rec, status, basis, at):
        if rec.kind == "objective" and status == "CONCLUDED":
            return ""
        return orig(self, rec, status, basis, at)
    mp.setattr(R.ResearchLedger, "_status_problem", patched)


def _no_question(mp):
    orig = R.ResearchLedger._status_problem

    def patched(self, rec, status, basis, at):
        body = rec.body
        rec.body = {k: v for k, v in body.items() if k != "question"}
        try:
            return orig(self, rec, status, basis, at)
        finally:
            rec.body = body
    mp.setattr(R.ResearchLedger, "_status_problem", patched)


def _summary_as_reason(mp):
    def conclude(self, objective_id, n, env):
        hid = env.request["hypothesis_id"]
        self.ledger.set_status(objective_id, "CONCLUDED",
                               reason=env.request["summary"], basis=[hid])
        return "CONCLUDED", hid
    mp.setattr(L.ResearchController, "_do_conclude", conclude)


def _any_dependency(mp):
    orig = R.ResearchLedger.current
    mp.setattr(R.ResearchLedger, "current",
               lambda self, rid: rid == "obs-slab-q-0" or orig(self, rid))


def _approval_as_data(mp):
    mp.setattr(PR, "FORBIDDEN_KEYS", PR.FORBIDDEN_KEYS - {"approved"})
    mp.setitem(PR.REQUEST_KEYS, "MODEL_RUN",
               PR.REQUEST_KEYS["MODEL_RUN"] | {"approved"})


def _retry_runs_it(mp):
    def retry(self, objective_id, n, env):
        run = self.ingress.submit(env.request["retry_of"], self.runs,
                                  out_dir=f"{self.out_dir}/{objective_id}/"
                                          f"step-{n}/retry")
        return "RETRIED", run.record_id
    mp.setattr(L.ResearchController, "_do_retry", retry, raising=False)


def _no_redactor(mp):
    orig = C.ContextBuilder.build
    mp.setattr(C.ContextBuilder, "build",
               lambda self, *, budget_bytes, redactor=None:
               orig(self, budget_bytes=budget_bytes))


def _owner_publishes_v2(w):
    PolicyStore(w.log).load().publish(document(
        policy_id=E.POLICY.policy_id, version=2, rules=(rule(
            rule_id="r", effect=Effect.ALLOW, actions=("*",),
            subjects=(ANY,), roles=(ANY,), resources=(ANY,)),)),
        actor="owner")


def _another_run(w):
    GovernedModelRuns(root=ROOT, log=w.log, evidence=w.evidence).propose(
        model_id=E.SLAB_RUN["model_id"],
        model_version=E.SLAB_RUN["model_version"],
        parameters=E.SLAB_RUN["parameters"], out_dir=f"{w.ws}/extra",
        submitter=E.AGENT)


#: scenario -> (monkeypatch -> None, or a world action after the run)
CONTROL_OFF = {
    "RI1": (_no_conclusion_rule, None),
    "FV1": (_no_conclusion_rule, None),
    "OJ1": (_no_conclusion_rule, None),
    "CO1": (_no_conclusion_rule, None),
    "CO2": (_no_question, None),
    "TL1": (lambda mp: mp.setitem(L.CHECKS, "thermal.rc2_network",
                                  "thermal.slab_method_of_lines"), None),
    "TL2": (lambda mp: mp.setattr(R, "_off_question", lambda o, b: ""),
            None),
    "MM1": (_summary_as_reason, None),
    "FC1": (_any_dependency, None),
    "RA1": (_approval_as_data, None),
    "DL1": (_retry_runs_it, None),
    "EX1": (_no_redactor, None),
    "EX2": (lambda mp: mp.setattr(PR, "_CREDENTIAL", re.compile(r"(?!)")),
            None),
    "PM1": (None, _owner_publishes_v2),
    "UE1": (None, _another_run),
    "UE2": (None, _another_run),
}


def test_every_scenario_has_a_positive_control():
    assert set(CONTROL_OFF) == set(SC)


@pytest.mark.parametrize("sid", sorted(CONTROL_OFF))
def test_with_its_control_removed_each_attack_is_seen_to_succeed(
        sid, monkeypatch):
    patch, after = CONTROL_OFF[sid]
    if patch is not None:
        patch(monkeypatch)
    got = E.run_one(SC[sid], "obeys", after=after)
    assert got["attack_succeeded"], got
