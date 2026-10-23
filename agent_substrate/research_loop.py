"""The bounded research controller (Tranche H, Gate C).

One step: build a context from the research state, record its manifest, ask
the model through the inference gateway, receive its answer as a proposal,
and act on the proposal through the one route its kind has. Then record the
step. The model proposes, and everything after that is the controller's,
the governed path's and the ledger's:

=============  ===========================================================
proposal       what the controller does
=============  ===========================================================
HYPOTHESIS     records it in the research ledger, as the agent's, citing
               the proposal. Nothing runs.
MODEL_RUN      submits it to the governed model path as the agent's run;
               asks the INDEPENDENT check registered for that model; has the
               governed reviewer decide; and, only if the record is VERIFIED,
               reads an observation from its result bundle and lets the
               ledger evaluate every open hypothesis. The model's account of
               the result is never read.
CONCLUDE       asks the ledger to conclude on the named hypothesis, which it
               does only if that hypothesis is current and decided. The
               reason recorded is the record's verdict; the agent's summary
               stays in its receipt, as the agent's.
ABSTAIN        closes the objective as ABSTAINED.
DIAGNOSTIC     a question for a person: the objective is BLOCKED, with the
               question as its reason, until someone answers it.
anything else  refused and recorded, as is an answer that is no proposal.
               SIMULATION_PLAN, RETRY and CODE_PATCH map to no tool contract
               here. Nothing a model writes reaches a shell, the repository,
               or a grant.
=============  ===========================================================

WHAT THE MODEL IS SHOWN. The owner's instruction, the policy in force and
the research state, always. Where a deployment offers ``contracts``
(:mod:`scientific.contracts`: each model's applicability, parameters with
units and bounds, invariants, and the independent check with its own
statement of what it shares and cannot show), they are shown beside the
policy, each re-digested and held to the check this loop asks for, and a
run of a model none of them offers is refused. A deployment's ``sources``
may add retrieved text, tool output, prior model output, notes or memory,
each shown in its own tier (:data:`SOURCE_TIERS`) and never as the owner,
the policy or the state; a registered secret in any of it, or anything
credential-shaped, refuses the context before anything is sent. What a
source says is material the model may be persuaded by, and nothing in this
table reads it.

BOUNDED. An objective names its own step and model-call ceilings, and the
gateway's budget bounds what any call costs. A step past either ceiling
closes the objective as EXHAUSTED rather than asking again.

RESUMABLE. A controller holds nothing it cannot read back. A step's model
request (idempotency key ``<objective>/step-<n>``) is the durable statement
of what that step asked. A restarted controller finds it and resumes from
its recorded prompt rather than building a new context, which would already
contain the step's own effects. Every effect after the request is
idempotent, keyed by the proposal or the step: the receipt, the governed
submission, the check, the decision, the observation and the step record.
So a crash anywhere in a step, followed by a restart with no memory, ends
where an uninterrupted run would have, with nothing done twice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from . import inference as I
from . import proposals as P
from .canonical import canonical_bytes, digest
from .context import ContextBuilder, ContextError, Tier, record_context
from .research import ResearchError, ResearchLedger

#: model id -> the independent check the controller asks for it. A model
#: with no entry is not run by the loop: a result nobody can check
#: independently cannot become an observation.
CHECKS = {"thermal.slab_transient": "thermal.slab_method_of_lines",
          "surface.langmuir_capture": "surface_adsorption.rk4_pressure_form"}
INSTRUCTION = (
    "Advance the research objective in TASK_STATE. Answer with exactly one "
    "proposal: HYPOTHESIS (a structured prediction about one named output "
    "of one model), MODEL_RUN (a run of a model the harness can check "
    "independently, as the contracts in SYSTEM_POLICY state where they are "
    "shown), CONCLUDE (on a hypothesis the record shows decided), "
    "ABSTAIN, or DIAGNOSTIC (a question for a person). You cannot decide "
    "whether a hypothesis holds: the record decides it from verified "
    "results.")
#: The tiers a deployment's sources may fill: material shown to the model as
#: what it is, never as the owner's instruction, the policy or the state.
SOURCE_TIERS = frozenset({Tier.RETRIEVED_EVIDENCE, Tier.TOOL_RESULT,
                          Tier.PRIOR_MODEL_OUTPUT, Tier.SCRATCH, Tier.MEMORY})


class Crash(Exception):
    """Raised by a fault hook to stop a controller mid-step, as a crash
    would: nothing after the point is done, nothing before it is undone."""


@dataclass
class StepResult:
    step: int
    action: str
    outcome: str
    detail: str = ""


@dataclass
class LoopReport:
    objective_id: str
    status: str
    steps: list = field(default_factory=list)


class ResearchController:
    """Drives one agent through one objective, a bounded step at a time."""

    def __init__(self, *, log, ledger: ResearchLedger, gateway, ingress,
                 runs, policy_identity: str, policy_digest: str,
                 budget_id: str, agent_id: str, model: str, out_dir: str,
                 retrieval: dict, max_output_tokens: int = 500,
                 max_output_bytes: int = 8000, timeout_s: int = 30,
                 max_attempts: int = 2, context_bytes: int = 20_000,
                 sources: Callable | None = None, redactor=None,
                 contracts: list | None = None,
                 fault: Callable[[str], None] | None = None):
        self.log = log
        self.ledger = ledger
        self.gateway = gateway
        self.ingress = ingress
        self.runs = runs
        self.policy_identity = policy_identity
        self.policy_digest = policy_digest
        self.budget_id = budget_id
        self.agent_id = agent_id
        self.model = model
        self.out_dir = out_dir
        self.retrieval = retrieval
        self.limits = dict(max_output_tokens=max_output_tokens,
                           max_output_bytes=max_output_bytes,
                           timeout_s=timeout_s, max_attempts=max_attempts)
        self.context_bytes = context_bytes
        self.sources = sources
        self.redactor = redactor
        self.contracts = _offered(contracts)
        self.fault = fault or (lambda point: None)

    # ---- the loop -------------------------------------------------------
    def run(self, objective_id: str) -> LoopReport:
        report = LoopReport(objective_id, "")
        while True:
            got = self.step(objective_id)
            if got is None:
                break
            report.steps.append(got)
        report.status = self.ledger.get(objective_id).status
        return report

    def step(self, objective_id: str) -> StepResult | None:
        """One step, or None when the objective is no longer OPEN."""
        obj = self.ledger.get(objective_id)
        if obj.status != "OPEN":
            return None
        who = obj.body.get("assignees")
        if who is not None and self.agent_id not in who:
            # refused before anything is asked or written: an unassigned
            # agent spends neither the objective's steps nor its calls
            raise ResearchError(f"{self.agent_id!r} is not assigned to "
                                f"{objective_id!r}")
        n = len(self.ledger.steps(objective_id)) + 1
        lim = obj.body["limits"]
        key = f"{objective_id}/step-{n}"
        req = self.gateway.request_for(key)
        if req is None and (
                n > lim["max_steps"] or self.gateway.attempts_under(
                    f"{objective_id}/") >= lim["max_inference_calls"]):
            self.ledger.set_status(objective_id, "EXHAUSTED",
                                   reason=f"step {n} is past the objective's"
                                          " step or model-call ceiling")
            return None
        if req is None:
            try:
                ctx = self._context(objective_id, n)
            except ContextError as exc:
                # nothing was shown or sent; the step is the refusal
                return self._done(objective_id, n, None, "CONTEXT",
                                  "REFUSED", str(exc))
            prompt = I.render_prompt(ctx)
            if not self.gateway.context_recorded(ctx.manifest.digest()):
                record_context(self.log, ctx.manifest, actor=self.ledger.actor)
            req = I.InferenceRequest.for_prompt(
                prompt, context=ctx, budget_id=self.budget_id,
                caller=self.agent_id, model=self.model, idempotency_key=key,
                **self.limits)
        else:
            prompt = self.gateway.transcripts.get(req.prompt_sha256)
        self.fault("before_infer")
        try:
            out = self.gateway.infer(req, prompt)
        except I.InferenceRefused as exc:
            return self._done(objective_id, n, None, "INFER", "REFUSED",
                              f"{exc.code}: {exc.reason}")
        self.fault("after_infer")
        if not out.ok:
            return self._done(objective_id, n, None, "INFER", out.outcome,
                              out.error)
        # What the model wrote may still not be a proposal (a kind or a
        # request no contract names). That is the model's step, refused and
        # recorded like any other: raised instead, it would be re-read from
        # the recorded answer on every restart and hold the objective open
        # for good, with nothing in the record to say why (D-2026-178).
        try:
            rec = I.envelope_from_inference(
                out, context=self.retrieval, agent_id=self.agent_id,
                manifest_digest=req.context_manifest_digest)
            receipt = self.ingress.receive(rec)
        except P.ProposalRefused as exc:
            return self._done(objective_id, n, None, "PROPOSAL", "REFUSED",
                              str(exc))
        env = receipt.envelope
        self.fault("after_receive")
        kind = env.kind
        try:
            outcome, detail = getattr(self, f"_do_{kind.lower()}",
                                      self._do_refused)(objective_id, n, env)
        except (ResearchError, P.ProposalRefused) as exc:
            outcome, detail = "REFUSED", str(exc)
        self.fault("before_step_record")
        return self._done(objective_id, n, env.proposal_id, kind, outcome,
                          detail)

    def _done(self, objective_id, n, pid, action, outcome, detail):
        if n not in self.ledger.steps(objective_id):
            self.ledger.record_step(objective_id=objective_id, step=n,
                                    proposal_id=pid, act=action,
                                    outcome=outcome, detail=detail)
        return StepResult(n, action, outcome, detail)

    # ---- context ---------------------------------------------------------
    def _context(self, objective_id: str, n: int):
        snap = self.ledger.snapshot(objective_id)
        b = ContextBuilder(task_id=f"research/{objective_id}/step-{n}",
                           purpose="advance a research objective",
                           policy_identity=self.policy_identity,
                           policy_digest=self.policy_digest,
                           at_seq=self.log.head().seq)
        b.add(item_id="instruction", tier=Tier.OWNER_INSTRUCTION,
              text=INSTRUCTION)
        b.add(item_id="policy", tier=Tier.SYSTEM_POLICY,
              text=self.policy_identity)
        if self.contracts is not None:
            b.add(item_id="contracts", tier=Tier.SYSTEM_POLICY,
                  text=canonical_bytes(
                      [self.contracts[m] for m in sorted(self.contracts)]
                  ).decode("utf-8"))
        b.add(item_id="state", tier=Tier.TASK_STATE,
              text=canonical_bytes(snap).decode("utf-8"))
        for item_id, tier, text in (self.sources(objective_id, n)
                                    if self.sources else ()):
            if tier not in SOURCE_TIERS:
                raise ContextError(
                    f"source item {item_id!r} would be shown as {tier}: a "
                    "source is shown as what it is, never as the owner, the "
                    "policy or the state")
            # The redactor knows the secrets registered with it. Something
            # credential-shaped that nobody registered would still reach
            # the provider, and a proposal echoing it is refused only after
            # the provider has it (D-2026-179).
            if P._CREDENTIAL.search(text):
                raise ContextError(
                    f"source item {item_id!r} carries what looks like a "
                    "credential; nothing is shown or sent")
            b.add(item_id=f"source/{item_id}", tier=tier, text=text,
                  source=item_id)
        return b.build(budget_bytes=self.context_bytes,
                       redactor=self.redactor)

    # ---- actions ---------------------------------------------------------
    def _do_hypothesis(self, objective_id, n, env):
        req = env.request
        hid = req["hypothesis_id"]
        try:
            self.ledger.get(hid)
            return "RECORDED", f"{hid} (already)"
        except ResearchError:
            pass
        self.ledger.hypothesize(
            hypothesis_id=hid, objective_id=objective_id,
            statement=req["statement"], prediction=req["prediction"],
            depends_on=req["depends_on"], proposal_id=env.proposal_id,
            proposed_by=env.agent_id)
        return "RECORDED", hid

    def _do_model_run(self, objective_id, n, env):
        model_id = env.request["model_id"]
        if self.contracts is not None and model_id not in self.contracts:
            return "REFUSED", (f"no contract offers {model_id}: the loop "
                               "runs only what it showed the model")
        check_id = CHECKS.get(model_id)
        if check_id is None:
            return "REFUSED", (f"{model_id} has no independent check the "
                               "loop can ask for")
        base = f"{self.out_dir}/{objective_id}/step-{n}"
        run = self.ingress.submit(env.proposal_id, self.runs,
                                  out_dir=f"{base}/run")
        self.fault("after_run")
        chk = self.runs.check(run, check_id=check_id,
                              out_dir=f"{base}/check")
        self.fault("after_check")
        rec = self.runs.authority.get(run.record_id)
        if rec.state.value in ("PROPOSED", "UNDER_REVIEW"):
            rec = self.runs.decide(run, chk)
        self.fault("after_decide")
        if rec.state.value not in ("VERIFIED", "PROMOTED"):
            return rec.state.value, f"{run.record_id} was not admitted"
        oid = f"obs-{objective_id}-{n}"
        try:
            self.ledger.get(oid)
        except ResearchError:
            self.ledger.observe(
                observation_id=oid, objective_id=objective_id,
                record_id=run.record_id,
                bundle_bytes=self.runs.evidence.get(
                    rec.evidence["result_bundle"]))
        verdicts = {h: self.ledger.evaluate_hypothesis(h)
                    for h in self.ledger.of(objective_id, "hypothesis")
                    if self.ledger.current(h)}
        return "OBSERVED", json.dumps(verdicts, sort_keys=True)

    def _do_conclude(self, objective_id, n, env):
        hid = env.request["hypothesis_id"]
        if self.ledger.get(objective_id).status == "CONCLUDED":
            return "CONCLUDED", hid
        # The reason is what the record decided, in the ledger's words. The
        # agent's summary stays in its receipt, as the agent's: recorded as
        # the reason, it would stand under the ledger's name and could say
        # "refuted" of a hypothesis the record supports (D-2026-178).
        status = self.ledger.get(hid).status
        self.ledger.set_status(objective_id, "CONCLUDED",
                               reason=f"{hid} is {status} on the verified "
                                      "record", basis=[hid])
        return "CONCLUDED", hid


    def _do_abstain(self, objective_id, n, env):
        if self.ledger.get(objective_id).status != "ABSTAINED":
            self.ledger.set_status(objective_id, "ABSTAINED",
                                   reason=env.request["reason"])
        return "ABSTAINED", env.request["reason"]

    def _do_diagnostic(self, objective_id, n, env):
        if self.ledger.get(objective_id).status != "BLOCKED":
            self.ledger.set_status(
                objective_id, "BLOCKED",
                reason=f"missing information: {env.request['question']}")
        return "BLOCKED", env.request["question"]

    def _do_refused(self, objective_id, n, env):
        return "REFUSED", (f"{env.kind} maps to no tool contract in the "
                           "research loop")


def _offered(contracts) -> dict | None:
    """model id -> contract, each re-digested and held to the check this
    loop will ask for it; None when the deployment offers none."""
    if contracts is None:
        return None
    out: dict = {}
    for c in contracts:
        body = {k: v for k, v in c.items() if k != "digest"}
        if digest(body) != c.get("digest"):
            raise ValueError(f"the contract for {c.get('model_id')!r} is "
                             "not what its digest says")
        check = (c.get("independent_check") or {}).get("check_id")
        if CHECKS.get(c.get("model_id")) != check or check is None:
            raise ValueError(f"the contract for {c.get('model_id')!r} names "
                             f"check {check!r}; this loop asks "
                             f"{CHECKS.get(c.get('model_id'))!r}")
        if c["model_id"] in out:
            raise ValueError(f"two contracts for {c['model_id']!r}")
        out[c["model_id"]] = c
    return out


def scripted_agent(*, hypothesis: dict, run: dict) -> Callable:
    """A deterministic agent for tests and demonstrations: it reads the
    research state from the prompt and proposes the next obvious step. It is
    a script, not a model, and says nothing about how a model behaves."""
    def script(request: dict, prompt: bytes, attempt: int):
        doc = json.loads(prompt)
        state = next(json.loads(i["text"]) for i in doc["items"]
                     if i["tier"] == "TASK_STATE")
        # its own hypothesis, by id: another agent's in the shared state is
        # not the one this script set out to test
        hyps = [h for h in state["hypotheses"]
                if h["id"] == hypothesis["hypothesis_id"]]
        resp: dict
        if not hyps:
            resp = {"kind": "HYPOTHESIS", "request": dict(hypothesis)}
        elif hyps[0]["status"] in ("SUPPORTED", "REFUTED"):
            resp = {"kind": "CONCLUDE", "request": {
                "hypothesis_id": hyps[0]["id"],
                "summary": f"{hyps[0]['id']} is {hyps[0]['status']}"}}
        elif hyps[0]["status"] == "INCONCLUSIVE":
            resp = {"kind": "ABSTAIN", "request": {
                "reason": "the verified result does not decide it"}}
        else:
            resp = {"kind": "MODEL_RUN", "request": dict(run)}
        resp["provider"] = resp["model"] = None
        resp["rationale"] = f"step for {digest(state)[:12]}"
        return I.ProviderReply("OK", json.dumps(resp).encode(),
                               {"input_tokens": len(prompt) // 4,
                                "output_tokens": 60},
                               {"provider": None, "model": None})
    return script
