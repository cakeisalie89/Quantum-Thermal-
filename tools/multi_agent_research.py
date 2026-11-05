#!/usr/bin/env python3
"""Gate E: multi-agent coordination on the existing substrate.

    python tools/multi_agent_research.py            # run, print
    python tools/multi_agent_research.py --write    # record the measurement

THE TEAM, registered in the agent directory with separate roles and
separate budgets:

- a coordinator: plans, assigns by message, and asks for cancellation. It
  holds no model budget, and it allocates no budget itself: the scheduler
  publishes each researcher's, since spending authority answers to the
  capability root;
- two researchers, each with its own budget and its own assigned
  objectives;
- an independent challenger, which re-runs a shared objective's model and
  so produces its observation;
- an adversary, which tries what the others may not.

WHAT IS EXERCISED, each through the mechanism that already enforces it:

- **Bounded delegation.** An objective names its assignees (the owner's
  decision, at registration). A controller of any other agent asks
  nothing, and the ledger refuses its hypothesis. A budget is spendable
  only by its caller.
- **Duplicate suppression.** A resent assignment is the same message.
- **Cancellation.** The coordinator's cancel message, with the
  researcher's budget withdrawn by the scheduler: nothing is dispatched or
  executed for it afterwards.
- **Cross-agent invalidation.** Withdrawing the challenger's run makes
  stale a researcher's conclusion that rested on it.
- **Disagreement.** Two researchers' contradictory hypotheses on one
  shared objective are decided by the verified observation. The agents'
  own claims are recorded, and resolving them changes no record.
  Agreement is not verification.
- **Lineage.** Every agent's inference is attributed to the model it
  used. Agents sharing one lineage are not independent, and a quorum of
  them counts as one opinion.
- **A matched-budget comparison.** The same objectives, one agent against
  two, on the same total budget.

THE AGENTS ARE SCRIPTS. Their decisions are scripted; nothing here says
how a team of models behaves.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import adversarial_eval as E  # noqa: E402

from agent_substrate import inference as I  # noqa: E402
from agent_substrate import proposals as PR  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.agents import (  # noqa: E402
    AgentRole, ConflictRule, PrincipalKind, identity,
)
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.capability import CapabilityLedger  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_model import GovernedModelRuns  # noqa: E402
from agent_substrate.policy import PolicyStore  # noqa: E402
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402

SCHEMA = "multi-agent-research/1"
RESULTS = ROOT / "docs" / "assurance" / "multi_agent_results.json"
WS = "verification/stage10/_multi_agent"
COORD, RA, RB, CH, ADV = ("coordinator", "researcher-a", "researcher-b",
                          "challenger", "adversary")
ROLES = {COORD: {AgentRole.SCHEDULER}, RA: {AgentRole.PROPOSER},
         RB: {AgentRole.PROPOSER}, CH: {AgentRole.PROPOSER},
         ADV: {AgentRole.PROPOSER}}
MODEL, PROVIDER = E.MODEL, E.PROVIDER
#: calls an objective of this plan needs: a hypothesis, a run, a conclusion
CALLS_PER_OBJECTIVE = 3


def _prediction(threshold: float, op: str = "<") -> dict:
    return {"model_id": "thermal.slab_transient", "quantity": "T_at_half",
            "op": op, "value": threshold, "unit": "K"}


class Script:
    """One scripted agent: per objective, a hypothesis (or none), then a run
    if something is open, then a conclusion on its own hypothesis if the
    objective's question is its own."""

    def __init__(self, agent: str):
        self.agent = agent
        self.plans: dict = {}

    def __call__(self, req, prompt, n):
        state = next(json.loads(i["text"]) for i in
                     json.loads(prompt)["items"]
                     if i["tier"] == "TASK_STATE")
        oid = state["objective"]["id"]
        plan = self.plans[oid]
        mine = {h["id"]: h for h in state["hypotheses"]}
        hid = plan.get("hypothesis")
        if hid and hid not in mine:
            t, op = plan["threshold"], plan.get("op", "<")
            return E._reply("HYPOTHESIS", {
                "hypothesis_id": hid, "statement": f"T_at_half {op} {t} K",
                "prediction": _prediction(t, op), "depends_on": []})
        if plan.get("run") and any(h["status"] == "OPEN"
                                   for h in mine.values()):
            return E._reply("MODEL_RUN", E.SLAB_RUN)
        if plan.get("conclude") and hid in mine and \
                mine[hid]["status"] in ("SUPPORTED", "REFUTED"):
            return E._reply("CONCLUDE", {"hypothesis_id": hid,
                                         "summary": "decided"})
        return E._reply("ABSTAIN", {"reason": "nothing of mine to do"})


class Team:
    def __init__(self, name: str, budgets: dict):
        self.base = ROOT / WS / name
        if self.base.exists():
            shutil.rmtree(self.base)
        self.base.mkdir(parents=True)
        self.ws = f"{WS}/{name}"
        self.log = EventLog(self.base / "log.jsonl")
        self.evidence = EvidenceStore(self.base / "evidence")
        self.scripts = {a: Script(a) for a in ROLES}
        g = GovernedModelRuns(root=ROOT, log=self.log, evidence=self.evidence)
        for agent, roles in ROLES.items():
            g.gov.agents.register(identity(
                agent_id=agent, instance_id=agent, kind=PrincipalKind.AGENT,
                roles=roles), by="system")
        self.directory = g.gov.agents
        CapabilityLedger(self.log).load().anoint(actor="scheduler")
        PolicyStore(self.log).load().publish(E.POLICY, actor="owner")
        gw = self.gateway()
        for agent, calls in budgets.items():
            gw.publish_budget(_budget(agent, calls), actor="scheduler")

    def _provider(self, req, prompt, n):
        return self.scripts[req["caller"]](req, prompt, n)

    def gateway(self):
        return I.InferenceGateway(
            self.log, transcripts=EvidenceStore(self.base / "transcripts"),
            policies=PolicyStore(self.log).load(),
            provider=I.FixtureProvider(PROVIDER, self._provider)).load()

    def ledger(self):
        return R.ResearchLedger(self.log, evidence=self.evidence).load()

    def objective(self, oid: str, *, assignees: list, threshold=1000.0,
                  op="<", max_steps=6):
        self.ledger().register(
            objective_id=oid, statement=f"{oid}: is T_at_half {op} "
            f"{threshold} K at the point?", owner="owner",
            max_steps=max_steps, max_inference_calls=2 * max_steps,
            question={"prediction": _prediction(threshold, op),
                      "parameters": E.SLAB_RUN["parameters"]},
            assignees=assignees)

    def controller(self, agent: str, budget: str | None = None):
        runs = GovernedModelRuns(root=ROOT, log=self.log,
                                 evidence=self.evidence)
        gw = self.gateway()
        return L.ResearchController(
            log=self.log, ledger=R.ResearchLedger(
                self.log, evidence=runs.evidence).load(),
            gateway=gw, ingress=PR.ProposalIngress(
                self.log, inference_check=gw.binding_problems),
            runs=runs, policy_identity="research.default@1",
            policy_digest=E.POLICY.digest(),
            budget_id=budget or f"budget-{agent}", agent_id=agent,
            model=MODEL, out_dir=self.ws,
            retrieval=PR.assemble_context(
                "slab", retrieve=lambda q, k: {"hits": []}))

    def assign(self, agent: str, oid: str, plan: dict, *, kind="assign"):
        self.scripts[agent].plans[oid] = plan
        self.directory.load()
        return self.directory.send(
            message_id=f"{kind}/{oid}/{agent}", sender_instance=COORD,
            recipient_agent=agent, task_id=oid, subject=kind,
            body_digest=digest({"kind": kind, "objective": oid,
                                "plan": plan}))

    def events(self):
        report, events = self.log.read_verified()
        report.raise_if_bad()
        return events

    def clean(self):
        shutil.rmtree(self.base, ignore_errors=True)


def _budget(agent: str, calls: int) -> I.Budget:
    return I.Budget(
        budget_id=f"budget-{agent}", provider=PROVIDER, models=(MODEL,),
        callers=(agent,), max_calls=calls, max_cost_micro=10**9,
        max_input_tokens=10**8, max_output_tokens=10**7, window_s=3600,
        max_calls_per_window=calls, max_input_bytes=100_000,
        max_request_output_tokens=500, max_output_bytes=8_000,
        max_timeout_s=60, max_attempts=2,
        prices={MODEL: {"input": 1000, "output": 1000}})


def _refused(fn, *errors) -> str | None:
    """Why ``fn`` was refused, or None if it was not."""
    try:
        fn()
    except errors as exc:
        return f"{type(exc).__name__}: {exc}"[:200]
    return None


def lineage(events) -> dict:
    """agent -> the models its inference was answered by, from the log."""
    reserved = {}
    for e in events:
        if e.action == "inference.reserve":
            req = e.payload.get("request") or {}
            reserved[e.target] = (req.get("caller"), req.get("model"))
    out: dict = {}
    for e in events:
        if e.action == "inference.result" and e.target in reserved:
            caller, model = reserved[e.target]
            out.setdefault(caller, set()).add(model)
    return {a: sorted(m) for a, m in sorted(out.items())}


def independent_quorum(claims, lineages: dict, quorum: int) -> bool:
    """Whether ``quorum`` claims of one value come from that many distinct
    LINEAGES. Instances sharing a model are one opinion, however many."""
    by_value: dict = {}
    for c in claims:
        for m in lineages.get(c.by_instance, ["unknown"]):
            by_value.setdefault(c.value_digest, set()).add(m)
    return any(len(ms) >= quorum for ms in by_value.values())


# -------------------------------------------------------------- the run

def coordinated() -> dict:
    t0 = time.monotonic()
    team = Team("team", {RA: 40, RB: 40, CH: 20, ADV: 20})
    out: dict = {}
    try:
        # the owner registers each objective with its assignees, as the
        # coordinator planned them
        for i, (agent, thr) in enumerate(((RA, 1000.0), (RA, 500.0),
                                          (RB, 400.0), (RB, 300.0))):
            oid = f"own-{i}"
            team.objective(oid, assignees=[agent], threshold=thr)
            team.assign(agent, oid, {"hypothesis": f"{oid}.h",
                                     "threshold": thr, "run": True,
                                     "conclude": True})
        resent = team.assign(RA, "own-0", team.scripts[RA].plans["own-0"])
        team.objective("shared", assignees=[RA, RB, CH], threshold=350.0,
                       max_steps=8)
        team.assign(RA, "shared", {"hypothesis": "shared.a",
                                   "threshold": 350.0, "conclude": True})
        team.assign(RB, "shared", {"hypothesis": "shared.b",
                                   "threshold": 350.0, "op": ">"})
        team.assign(CH, "shared", {"run": True})
        team.objective("later", assignees=[RB])
        team.assign(RB, "later", {"hypothesis": "later.h",
                                  "threshold": 1000.0, "run": True,
                                  "conclude": True})
        team.objective("adv-own", assignees=[ADV])
        team.assign(ADV, "adv-own", {"hypothesis": "adv.h",
                                     "threshold": 1000.0, "run": True,
                                     "conclude": True})

        # each researcher works through its deliverable assignments
        for agent in (RA, RB):
            msgs, _ = team.directory.load().deliverable(agent, agent)
            for m in msgs:
                if m.task_id.startswith("own-"):
                    team.controller(agent).run(m.task_id)
        # the shared objective: two contradictory hypotheses, the
        # challenger's run decides them, and A concludes on its own
        team.controller(RA).step("shared")
        team.controller(RB).step("shared")
        team.controller(CH).step("shared")
        team.controller(RA).step("shared")
        led = team.ledger()
        out["disagreement"] = {
            "a": led.get("shared.a").status, "b": led.get("shared.b").status,
            "objective": led.get("shared").status,
            "concluded_on": list(led.get("shared").basis)}
        # the agents' own claims, and what resolving them changes
        team.directory.load()
        for agent, hid in ((RA, "shared.a"), (RB, "shared.b")):
            team.directory.claim(
                claim_id=f"claim/{agent}", task_id="shared",
                subject="which hypothesis holds",
                value_digest=digest({"holds": hid}), by_instance=agent,
                role=AgentRole.PROPOSER)
        before = [e for e in team.events() if e.action.startswith("record.")]
        res = team.directory.load().resolve(
            task_id="shared", subject="which hypothesis holds",
            rule=ConflictRule.REQUIRE_QUORUM, quorum=2)
        after = [e for e in team.events() if e.action.startswith("record.")]
        lin = lineage(team.events())
        out["claims"] = {
            "resolved": res.resolved, "why": res.reason,
            "records_changed_by_resolution": len(after) - len(before),
            "lineages": lin,
            "independent_lineages": len({m for ms in lin.values()
                                         for m in ms}),
            "a_quorum_of_two_would_be_independent": independent_quorum(
                team.directory.claims_about("shared",
                                            "which hypothesis holds"),
                lin, 2)}

        # cancellation: the coordinator cancels B's later objective and
        # the scheduler withdraws B's budget; nothing runs for B after it
        team.assign(RB, "later", team.scripts[RB].plans["later"],
                    kind="cancel")
        gw = team.gateway()
        gw.withdraw_budget(f"budget-{RB}", actor="scheduler",
                           reason="the coordinator cancelled B's work")
        head = team.log.head()
        cut = head.seq if head is not None else -1
        team.controller(RB).run("later")
        after_cut = [e for e in team.events() if e.seq > cut]
        out["cancellation"] = {
            "later": team.ledger().get("later").status,
            "dispatched_after": sum(e.action == "inference.dispatch"
                                    for e in after_cut),
            "executed_after": sum(e.action == "task.create"
                                  for e in after_cut)}

        # cross-agent invalidation. First the challenger's TASK: its bundle
        # bytes were also produced, byte-identical, by the researchers'
        # own verified runs at the same point, so its origin still holds
        # and its result stands. Then the BYTES themselves are withdrawn:
        # every task that captured them, every agent's.
        g = GovernedModelRuns(root=ROOT, log=team.log, evidence=team.evidence)
        task = next(e.target for e in team.events()
                    if e.action == "task.create"
                    and e.payload["submitter"] == CH)
        inv = g.invalidate_task(task, reason="the challenger's run is "
                                             "withdrawn")
        after_task = team.ledger().propagate()
        sha = team.ledger().get(team.ledger().of(
            "shared", "observation")[0]).body["result_bundle"]
        inv2 = g.withdraw_evidence(sha, reason="the documented point's "
                                               "bundle is withdrawn")
        after_bytes = team.ledger().propagate()
        led = team.ledger()
        out["cross_agent_invalidation"] = {
            "task_withdrawn": {"affected": list(inv.plan.affected),
                               "moved": after_task,
                               "shared": "STALE" if "shared" in
                               after_task else "stands"},
            "bytes_withdrawn": {
                "tasks": len(inv2.tasks), "records": len(inv2.plan.affected),
                "objectives_stale": sorted(
                    o for o in after_bytes if "." not in o
                    and not o.startswith("obs-")),
                "agents_whose_conclusions_went_stale": sorted({
                    a for i, a in enumerate((RA, RA, RB, RB))
                    if led.get(f"own-{i}").status == "STALE"} | (
                    {RA} if led.get("shared").status == "STALE" else
                    set()))}}

        # the adversary's attempts, each against the mechanism that holds,
        # on an objective that is open and assigned to researcher A
        adv: dict = {}
        team.objective("a-open", assignees=[RA])
        team.scripts[ADV].plans["a-open"] = {"hypothesis": "adv.a-open",
                                             "threshold": 1.0}
        adv["steps an objective it is not assigned"] = _refused(
            lambda: team.controller(ADV).step("a-open"), R.ResearchError)
        pid = _receive(team, ADV)
        adv["records a hypothesis on another's objective"] = _refused(
            lambda: team.ledger().hypothesize(
                hypothesis_id="adv.on-a", objective_id="a-open",
                statement="s", prediction=_prediction(1000.0),
                depends_on=[], proposal_id=pid, proposed_by=ADV),
            R.ResearchError)
        team.objective("adv-spend", assignees=[ADV])
        team.scripts[ADV].plans["adv-spend"] = {
            "hypothesis": "adv-spend.h", "threshold": 1000.0}
        spent = team.controller(ADV, budget=f"budget-{RA}").step(
            "adv-spend")
        adv["spends another agent's budget"] = None if \
            spent.outcome not in ("REFUSED",) else spent.detail
        adv["claims as a reviewer"] = _refused(
            lambda: team.directory.load().claim(
                claim_id="claim/adv", task_id="shared",
                subject="which hypothesis holds",
                value_digest=digest({"holds": "shared.b"}),
                by_instance=ADV, role=AgentRole.REVIEWER), Exception)
        out["adversary"] = adv
        out["duplicate_suppression"] = {
            "resent_is_the_same_message": resent.message_id ==
            "assign/own-0/researcher-a",
            "messages_recorded": sum(e.action == "agent.message"
                                     for e in team.events())}
        out["second_reader_anomalies"] = len(
            reconstruct_subsystems(team.log).anomalies)
        out["seconds"] = round(time.monotonic() - t0, 1)
    finally:
        team.clean()
    return out


def _receive(team: Team, agent: str) -> str:
    rec = PR.envelope_record(
        {"kind": "HYPOTHESIS", "request": {
            "hypothesis_id": "adv.on-a", "statement": "s",
            "prediction": _prediction(1000.0), "depends_on": []},
         "provider": None, "model": None, "rationale": "adversary"},
        context=PR.assemble_context("slab", retrieve=lambda q, k: {
            "hits": []}), agent_id=agent, adapter_name="recorded-fixture",
        adapter_version="1")
    ing = PR.ProposalIngress(team.log)
    return ing.receive(PR.seal(rec)).envelope.proposal_id


def matched(objectives: int = 6) -> dict:
    """The same objectives, one agent against two, on one total budget."""
    total = objectives * CALLS_PER_OBJECTIVE
    out = {}
    for label, split in (("one_agent", {RA: total}),
                         ("two_agents", {RA: total // 2,
                                         RB: total - total // 2})):
        t0 = time.monotonic()
        team = Team(label, split)
        try:
            agents = sorted(split)
            for i in range(objectives):
                agent = agents[i % len(agents)]
                oid = f"m-{i}"
                team.objective(oid, assignees=[agent],
                               threshold=(1000.0, 300.0)[i % 2])
                team.assign(agent, oid, {
                    "hypothesis": f"{oid}.h",
                    "threshold": (1000.0, 300.0)[i % 2], "run": True,
                    "conclude": True})
                team.controller(agent).run(oid)
            ev = team.events()
            led = team.ledger()
            out[label] = {
                "agents": agents, "budget_calls": split,
                "concluded": sum(led.get(f"m-{i}").status == "CONCLUDED"
                                 for i in range(objectives)),
                "inference_calls": sum(e.action == "inference.result"
                                       for e in ev),
                "governed_operations": sum(e.action == "task.create"
                                           for e in ev),
                "messages": sum(e.action == "agent.message" for e in ev),
                "events": len(ev),
                "seconds": round(time.monotonic() - t0, 1)}
        finally:
            team.clean()
    return out


def run() -> dict:
    return {"schema": SCHEMA,
            "agents": "SCRIPTED; every agent shares one fixture model, so "
                      "none is independent of another",
            "coordinated": coordinated(), "matched_budget": matched()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    out = run()
    text = json.dumps(out, indent=1, sort_keys=True) + "\n"
    if args.write:
        RESULTS.write_text(text, encoding="utf-8")
    print(text)
    c = out["coordinated"]
    return 0 if (all(v for v in c["adversary"].values())
                 and c["cancellation"]["dispatched_after"] == 0
                 and c["second_reader_anomalies"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
