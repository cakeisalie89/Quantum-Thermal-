"""A governed scientific-model run, end to end: the Phase-2 proving path.

    AI proposes               a submitter (PROPOSER) asks for a model run
    -> governed execution     the Stage-10 control plane, unchanged: policy,
                              queue, lease, capability, bounded subprocess,
                              evidence capture, integrity verification and
                              byte-identical re-execution by a separate actor
    -> ResultBundle           written by the model tool in its subprocess
    -> independent check      a SECOND governed task, run by a DIFFERENT
                              executor, whose tool runs the model's declared
                              independent check and writes a
                              VerificationResult
    -> evidence               both captured content-addressed; the check
                              cites the bundle by digest
    -> authority decides      an authority record for the result, PROPOSED by
                              the submitter; a reviewer -- not the proposer,
                              not either executor -- moves it to VERIFIED or
                              REJECTED, citing the VerificationResult

This module knows no numerics. It names the tools by module string, reads
the JSON records the tools wrote from the evidence store, and compares
digests; it imports nothing from ``scientific`` or ``qta_multiphysics``. The
model knows nothing of this module: its ``run`` has no handle on the log.

WHERE THE CONTENT RULE LIVES

"A scientific result is VERIFIED only on a PASS report about THIS bundle,
from code other than the producer's, with every invariant of the bundle
holding" is enforced by the authority store on the edge itself
(:mod:`qta_agent.result_rules`), so a caller writing the transition directly
is refused as :meth:`decide` would refuse it. :meth:`decide` applies the same
function to choose between VERIFIED and REJECTED and to record the reasons.

REUSE

A proposal first asks a governed tool for the run identity it would have
here (model, implementation digest, validated parameters, environment). A
result with that identity is reused only if the authority layer VERIFIED or
PROMOTED it and its evidence re-derives now: the bundle and every artefact
it references still resolve (each read re-hashes), the bundle's OWN
provenance carries the identity, and the cited report still supports the
bundle under the content rule. Anything else is recomputed. A reused run is
already decided; it is not checked or decided again.

PROMOTION IS NOT HERE. VERIFIED is not canonical; PROMOTED is, and only a
PROMOTER distinct from the verifier can take that edge. This path makes no
promotion decision.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentRole, PrincipalKind, identity
from .authority import Role, State
from .canonical import canonical_bytes, digest
from .events import EventLog
from .evidence import EvidenceStore
from .invalidation import (INVALIDATABLE, InvalidationPlan, apply_plan,
                           plan_from)
from . import result_rules
from .result_rules import KIND as RECORD_KIND
from .result_rules import record_problems
from .governed_stage10 import (
    ACT_EVIDENCE, ACT_EXECUTION, ACT_TASK_TRANSITION, POLICY_ID,
    SUBMITTER_ID, VERIFIER_ID, WORKER_ID, WORKSPACE_PREFIX, GovernedRun,
    GovernedStage10,
)
from .store import AuthorityStore, UnknownRecord
from .tasks import TaskState
from .tools import (Determinism, Field_, OutputFile, Registry, SideEffect,
                    ToolSpec)

TOOL_RUN = "model.thermal.conduction_1d.run"
TOOL_RUN_2D = "model.thermal.conduction_2d_axisymmetric.run"
TOOL_RUN_SLAB = "model.thermal.slab_transient.run"
TOOL_RUN_RC2 = "model.thermal.rc2_network.run"
#: An external model: an FMI 3.0 FMU executed in its own runtime. Its
#: bundle is a SIMULATION_RESULT like any model's and enters the same
#: evidence boundary -- checked by an admitted independent check, decided
#: by a reviewer -- and its FMU file is cited by digest, as a bundle is.
TOOL_RUN_FMU = "model.fmi.thermal_rc2.run"
TOOL_CHECK = "model.independent_check"
TOOL_IDENTITY = "model.run_identity"
#: Which governed tool runs which model. One tool per model, so a policy
#: decision and the task history name the model that ran; a model with no
#: tool here is refused before anything is submitted.
MODEL_TOOLS = {"thermal.conduction_1d": TOOL_RUN,
               "thermal.conduction_2d_axisymmetric": TOOL_RUN_2D,
               "thermal.slab_transient": TOOL_RUN_SLAB,
               "thermal.rc2_network": TOOL_RUN_RC2}
#: Models whose run writes a temperature field beside the bundle.
_FIELD_MODELS = frozenset({"thermal.conduction_1d",
                           "thermal.conduction_2d_axisymmetric",
                           "thermal.slab_transient"})
_TOOL_MODULES = {TOOL_RUN: "scientific._governed_run",
                 TOOL_RUN_2D: "scientific._governed_run",
                 TOOL_RUN_SLAB: "scientific._governed_run",
                 TOOL_RUN_RC2: "scientific._governed_run",
                 TOOL_RUN_FMU: "scientific._governed_fmu",
                 TOOL_CHECK: "scientific._governed_check",
                 TOOL_IDENTITY: "scientific._governed_identity"}

#: The executor of the independent check. Registered here as an EXECUTOR
#: distinct from the model run's worker.
CHECK_WORKER_ID = "model-check-worker"
#: Who decides authority for a scientific result: a VERIFIER in the
#: authority sense, distinct from the proposer and from both executors.
REVIEWER_ID = "model-result-reviewer"



class ModelRunRefused(ValueError):
    pass


def _model_run_tool(tool_id: str, model_id: str) -> ToolSpec:
    field = model_id in _FIELD_MODELS
    return ToolSpec(
            tool_id=tool_id, version="1.0.0",
            summary=f"run {model_id}@1.0.0 and write its ResultBundle"
                    + (" and temperature field" if field else ""),
            inputs=(Field_("out_dir", "str"), Field_("model_id", "str"),
                    Field_("model_version", "str"),
                    Field_("parameters", "dict")),
            outputs=(Field_("path", "str"), Field_("sha256", "str"),
                     Field_("bundle_digest", "str"),
                     Field_("all_invariants_hold", "bool")),
            output_files=(
                OutputFile("bundle", "{out_dir}/bundle.json"),
                *((OutputFile("temperature_field",
                              "{out_dir}/temperature_field.bin"),)
                  if field else ())),
            # Measured, not assumed: the solve is deterministic on one
            # machine, and nothing time-dependent enters the bundle. The
            # verifier re-runs it and compares bytes.
            determinism=Determinism.BYTE_IDENTICAL,
            side_effect=SideEffect.SCOPED_WRITES,
            writable_scope=(WORKSPACE_PREFIX,), timeout_s=600.0)


def model_registry() -> Registry:
    return Registry([
        *(_model_run_tool(tool, model) for model, tool in MODEL_TOOLS.items()),
        ToolSpec(
            tool_id=TOOL_RUN_FMU, version="1.0.0",
            summary="run the thermal_rc2 FMI 3.0 FMU, cited by digest with "
                    "its build record, in a declared FMI runtime, and write "
                    "its ResultBundle",
            inputs=(Field_("out_dir", "str"), Field_("fmu_path", "str"),
                    Field_("fmu_sha256", "str"),
                    Field_("build_record_path", "str"),
                    Field_("build_record_sha256", "str"),
                    Field_("parameters", "dict"), Field_("step_s", "float"),
                    Field_("runtime_python", "str")),
            outputs=(Field_("path", "str"), Field_("sha256", "str"),
                     Field_("bundle_digest", "str"),
                     Field_("all_invariants_hold", "bool")),
            output_files=(OutputFile("bundle", "{out_dir}/bundle.json"),),
            determinism=Determinism.BYTE_IDENTICAL,
            side_effect=SideEffect.SCOPED_WRITES,
            writable_scope=(WORKSPACE_PREFIX,), timeout_s=600.0),
        ToolSpec(
            tool_id=TOOL_CHECK, version="1.0.0",
            summary="run an admitted independent check on a cited bundle "
                    "and write its VerificationResult",
            inputs=(Field_("out_dir", "str"), Field_("bundle_path", "str"),
                    Field_("bundle_sha256", "str"), Field_("check_id", "str"),
                    Field_("verifier_id", "str"),
                    # a check that needs an external runtime (FEniCSx) is
                    # told which interpreter, as a declared and recorded
                    # input -- the governed environment inherits nothing
                    Field_("runtime_python", "str", required=False)),
            outputs=(Field_("path", "str"), Field_("sha256", "str"),
                     Field_("status", "str")),
            output_files=(OutputFile("verification",
                                     "{out_dir}/verification.json"),),
            determinism=Determinism.BYTE_IDENTICAL,
            side_effect=SideEffect.SCOPED_WRITES,
            writable_scope=(WORKSPACE_PREFIX,), timeout_s=900.0),
        ToolSpec(
            tool_id=TOOL_IDENTITY, version="1.0.0",
            summary="write the run identity a proposed model run would have "
                    "here; runs nothing",
            inputs=(Field_("out_dir", "str"), Field_("model_id", "str"),
                    Field_("model_version", "str"),
                    Field_("parameters", "dict")),
            outputs=(Field_("path", "str"), Field_("sha256", "str"),
                     Field_("identity_digest", "str")),
            output_files=(OutputFile("identity", "{out_dir}/identity.json"),),
            determinism=Determinism.BYTE_IDENTICAL,
            side_effect=SideEffect.SCOPED_WRITES,
            writable_scope=(WORKSPACE_PREFIX,), timeout_s=120.0),
    ])


@dataclass(frozen=True)
class ModelRun:
    #: the governed task that computed it; None when the result was reused.
    governed: GovernedRun | None
    bundle_path: str
    bundle_sha256: str
    #: the canonical digest of the bundle record (ResultBundle.digest()).
    bundle_digest: str
    record_id: str
    submitter: str
    worker: str
    #: the verified record this run was reused from, when it was.
    reused_from: str = ""


@dataclass(frozen=True)
class CheckRun:
    governed: GovernedRun
    report_path: str
    report_sha256: str
    worker: str


#: The governed tools whose captured artefacts count as a VerificationResult's
#: origin, and as a ResultBundle's. Restated as data in qta_agent.reconstruct;
#: a conformance test holds the two to each other.
VERIFIER_TOOLS = frozenset({TOOL_CHECK})
MODEL_RUN_TOOLS = frozenset(MODEL_TOOLS.values()) | {TOOL_RUN_FMU}


class GovernedOrigins:
    """Where a scientific result's evidence came from, read from the governed
    task history in the same log. The store's view of governed execution.

    ORIGIN, NOT CONTENT. The content rule asks whether a report SAYS the
    right things; a document written into the evidence store by hand can
    say all of them. This asks whether the report is an artefact that an
    authorized governed verification task captured -- a VERIFIED task of an
    admitted verifier tool, completed before the transition -- and whether
    the bundle is an artefact of a VERIFIED governed model run. And it asks
    WHO: the verification's executor must be none of the proposer, the
    decider, or an executor of a run that produced the bundle.

    Tasks come from the task projection, which re-authorizes every task
    transition on replay; artefacts from the ``task.evidence`` records of the
    same verified read. Every question is AS OF the transition's position:
    the task stood VERIFIED there, and the artefact was captured before the
    task's own verdict. Judged at the head instead, a check task invalidated
    after the result was verified would make the log's older history refuse
    to load -- a later fact rewriting what an earlier decision rested on.

    What it cannot see: actors in this log are names, not keys. A writer able
    to append a complete, rule-abiding governed lifecycle under other names
    is refused by no replay here; that residual is every record's in this
    log, not this rule's alone.
    """

    def __init__(self, gov, *, verifier_tools=VERIFIER_TOOLS,
                 model_tools=MODEL_RUN_TOOLS):
        self.gov = gov
        self.verifier_tools = frozenset(verifier_tools)
        self.model_tools = frozenset(model_tools)
        #: Work counters, for the guards that count instead of timing:
        #: origin questions answered, and views BUILT -- each one verified
        #: read of the whole log and one task fold, the part that costs.
        self.questions = 0
        self.views_built = 0
        self._shared = None

    @contextmanager
    def shared(self):
        """Answer every origin question in the block from ONE view.

        A load re-decides every scientific admission in the log, and each
        question built its own view: a verified read of the whole log and a
        task fold, twice per admission -- O(n*k) in admitted results. In the
        block, the first question builds the view and the rest reuse it.
        The view is of the whole history and every question is still asked
        AS OF its own position, so sharing changes the cost and not the
        answer -- provided nothing writes TASK history inside the block,
        which no caller here does. Nested blocks share the outer view.
        """
        if self._shared is not None:
            yield self
            return
        self._shared = []
        try:
            yield self
        finally:
            self._shared = None

    def _view(self):
        """``(projection, when, captured)`` from ONE verified read of the
        log, so no record can land between the three.

        ``when[task_id]`` is ``(verified_seq, left_seq, executor)``: the
        position of the task's move into VERIFIED, of its move out of it
        (None while it is still there), and who had executed it at the
        verdict. The projection has re-authorized every task
        transition in this read -- it raises otherwise -- so these positions
        are of transitions the task machine admits. ``captured[sha]`` lists
        ``(seq, task_id)`` for every ``task.evidence`` record naming ``sha``.
        """
        if self._shared:
            return self._shared[0]
        report, events = self.gov.log.read_verified()
        report.raise_if_bad()
        captured: dict = {}
        when: dict = {}
        ran: dict = {}
        for ev in events:
            p = ev.payload
            if ev.action == ACT_EVIDENCE:
                tid = p.get("task_id")
                for sha in (p.get("artifacts") or {}).values():
                    captured.setdefault(sha, []).append((ev.seq, tid))
            elif ev.action == ACT_EXECUTION:
                ran[p.get("task_id")] = ev.actor
            elif ev.action == ACT_TASK_TRANSITION:
                tid = p.get("task_id")
                if p.get("dst") == TaskState.VERIFIED.value:
                    # Who ran the work THIS verdict judged: an execution
                    # record appended after the verdict does not change it.
                    when[tid] = (ev.seq, None, ran.get(tid))
                elif p.get("src") == TaskState.VERIFIED.value \
                        and tid in when:
                    when[tid] = (when[tid][0], ev.seq, when[tid][2])
        view = (self.gov._project(events), when, captured)
        self.views_built += 1
        if self._shared is not None:
            self._shared.append(view)
        return view

    def _producers(self, sha: str, tools: frozenset, before_seq) -> list:
        """Tasks of ``tools`` that produced ``sha`` and stood VERIFIED at
        ``before_seq`` (None: now).

        Produced means CAPTURED BEFORE THE TASK WAS VERIFIED: the artefacts a
        governed verification examined are the ones captured before its
        verdict. The projection does not fold ``task.evidence`` records, so a
        record naming a new artefact for a task verified long ago is refused
        here rather than attached to somebody else's verdict.
        """
        projection, when, captured = self._view()
        horizon = float("inf") if before_seq is None else before_seq
        out = []
        for seq, tid in captured.get(sha, ()):
            task = projection.tasks.get(tid)
            if task is None or task.tool_id not in tools or tid not in when:
                continue
            verified, left, executor = when[tid]
            if not (seq < verified < horizon):
                continue
            if left is not None and left < horizon:
                continue
            out.append((tid, executor))
        return out

    def captured_by(self, sha: str) -> tuple:
        """Tasks standing VERIFIED now that captured ``sha`` before their
        own verdict: the governed origins an artefact's authority comes
        from."""
        _, when, captured = self._view()
        return tuple(sorted({
            tid for seq, tid in captured.get(sha, ())
            if tid in when and when[tid][1] is None and seq < when[tid][0]}))

    def artefacts_of(self, task_ids) -> frozenset:
        """Every artefact ``task_ids`` captured before their own verdicts:
        what those verdicts lent authority to."""
        ids = frozenset(task_ids)
        _, when, captured = self._view()
        return frozenset(
            sha for sha, seen in captured.items() for seq, tid in seen
            if tid in ids and tid in when and seq < when[tid][0])

    def problems(self, *, report_sha: str, bundle_sha: str, proposer: str,
                 actor: str, before_seq) -> list:
        self.questions += 1
        with self.shared():
            checks = self._producers(report_sha, self.verifier_tools,
                                     before_seq)
            runs = self._producers(bundle_sha, self.model_tools, before_seq)
        problems = []
        if not checks:
            problems.append(
                f"the report {report_sha[:12]} is not an artefact of any "
                "VERIFIED governed verification task completed before this "
                "transition; a well-formed report nobody's governed "
                "verification produced is not evidence")
        if not runs:
            problems.append(
                f"the bundle {bundle_sha[:12]} is not an artefact of any "
                "VERIFIED governed model run completed before this "
                "transition")
        if checks and runs:
            run_executors = {ran for _, ran in runs}
            if not any(ran is not None and ran not in (proposer, actor)
                       and ran not in run_executors
                       for _, ran in checks):
                problems.append(
                    "every verification that produced this report was "
                    "executed by the proposer, the decider, or an executor "
                    "of the model run it checks")
        return problems


@dataclass(frozen=True)
class Invalidation:
    """What following one change through the governed history did."""

    #: What changed: ``task:<id>`` or ``evidence:<digest>``.
    origin: str
    #: Tasks moved VERIFIED -> INVALIDATED, in order.
    tasks: tuple
    #: The authority plan applied: the records made STALE, each with the
    #: path from the origin that reached it, and those it reached that had
    #: no edge to STALE (rejected, revoked, undecided) left as they were.
    plan: InvalidationPlan
    #: Results citing an artefact of those tasks whose origin still holds:
    #: another governed task standing VERIFIED produced the same bytes.
    kept: tuple


class GovernedModelRuns:
    def __init__(self, *, root: Path, log: EventLog,
                 evidence: EvidenceStore, checkpoints=None):
        """``checkpoints``: a :class:`~qta_agent.checkpoint.CheckpointStore`
        to RESTART from. The authority projection is then recovered by
        :meth:`AuthorityStore.recover` -- the checkpoint audit decides
        between a checkpoint-assisted load and a full replay, and
        :attr:`recovery` says which, and why. Without one, a full replay."""
        self.root = Path(root)
        self.gov = GovernedStage10(root=root, log=log, evidence=evidence,
                                   registry=model_registry())
        self.gov.tool_modules = dict(_TOOL_MODULES)
        self.evidence = evidence
        self.origins = GovernedOrigins(self.gov)
        self.recovery = None
        if checkpoints is not None:
            self.authority, self.recovery = AuthorityStore.recover(
                log, checkpoints, blobs=evidence, evidence=evidence,
                origins=self.origins)
        else:
            self.authority = AuthorityStore(log, evidence=evidence,
                                            origins=self.origins).load()
        for iid, role in ((CHECK_WORKER_ID, AgentRole.EXECUTOR),
                          (REVIEWER_ID, AgentRole.VERIFIER)):
            try:
                self.gov.agents.get(iid)
            except Exception:                        # noqa: BLE001
                self.gov.agents.register(
                    identity(agent_id=iid, instance_id=iid,
                             kind=PrincipalKind.AGENT, roles={role}),
                    by="system")

    def checkpoint(self, checkpoints, *, actor: str = "system"):
        """Checkpoint the authority projection: a cached verification
        result a later restart may begin from, never a second truth."""
        return self.authority.checkpoint(checkpoints, blobs=self.evidence,
                                         actor=actor)

    # -- helpers ----------------------------------------------------------

    def _record_of(self, sha: str) -> dict:
        """A JSON record the tools wrote, read from the evidence store by
        the digest the capture recorded -- not from the workspace, which may
        have moved since."""
        return json.loads(self.evidence.get(sha))

    # -- the path ---------------------------------------------------------

    def identity(self, *, model_id: str, model_version: str,
                 parameters: dict, out_dir: str,
                 submitter: str = SUBMITTER_ID,
                 worker: str = WORKER_ID) -> str:
        """The digest of the run identity this proposal would have here,
        computed by a governed tool on the scientific side and captured as
        evidence (canonical bytes, so the digest is the record's)."""
        gr = self.gov.run(tool_id=TOOL_IDENTITY, inputs={
            "out_dir": out_dir, "model_id": model_id,
            "model_version": model_version, "parameters": parameters},
            submitter=submitter, worker=worker, verifier=VERIFIER_ID)
        if gr.state is not TaskState.VERIFIED:
            raise ModelRunRefused(f"the identity task was {gr.state.value}: "
                                  f"{gr.reason}")
        sha = gr.artifacts.get(f"{out_dir}/identity.json")
        if sha is None:
            raise ModelRunRefused("no captured identity")
        return self.evidence.put(canonical_bytes(self._record_of(sha)),
                                 media_type="application/json")

    def reusable(self, identity_digest: str) -> list:
        """Records a proposal with this identity may reuse, in record-id
        order; any one of them is a valid reuse.

        Only a result the authority layer has VERIFIED or PROMOTED, and only
        on evidence re-derived now. The record must cite this identity AND
        the bundle must carry it in its OWN provenance -- a record whose
        citation and bundle disagree is not believed either way. The bundle
        must still be in the store (every read re-hashes it), and every
        artefact it references must still resolve. The record must have been
        ADMITTED when this history was read, its report must still support
        its bundle, and both must still be the artefacts of governed tasks
        standing VERIFIED now. A result is reused by its evidence, never by
        its name.
        """
        with self.origins.shared():
            return self._reusable(identity_digest)

    def _reusable(self, identity_digest: str) -> list:
        out = []
        for rid, rec in sorted(self.authority.all_records().items()):
            if (rec.kind != RECORD_KIND
                    or rec.state not in (State.VERIFIED, State.PROMOTED)
                    or rec.evidence.get("run_identity") != identity_digest):
                continue
            try:
                bundle = self._record_of(rec.evidence["result_bundle"])
                recorded = bundle["provenance"]["run_identity"]
                artifacts = [a["digest"] for a in bundle["artifacts"]]
            except Exception:                        # noqa: BLE001
                # Unreadable, or not a bundle: not reusable, and not a
                # reason to stop looking at the others.
                continue
            if digest(recorded) != identity_digest:
                continue
            if not all(self.evidence.contains(a) for a in artifacts):
                continue
            # The decision is re-derived too: the record must have been
            # ADMITTED when this history was read, and the report it cites
            # must still resolve and still support this bundle now.
            if rec.admission != result_rules.ADMITTED:
                continue
            try:
                if record_problems(rec.evidence, self.evidence.get):
                    continue
            except result_rules.EvidenceUnavailable:
                continue
            # AND ITS ORIGIN STILL HOLDS NOW. Admission was judged where the
            # transition stands in the log; a check task invalidated since
            # leaves that history admitted, and leaves nothing to reuse.
            basis = rec.admission_basis or {}
            if self.origins.problems(
                    report_sha=rec.evidence["verification_report"],
                    bundle_sha=rec.evidence["result_bundle"],
                    proposer=rec.proposer, actor=basis.get("actor", ""),
                    before_seq=None):
                continue
            out.append(rid)
        return out

    def propose(self, *, model_id: str, model_version: str,
                parameters: dict, out_dir: str,
                submitter: str = SUBMITTER_ID,
                worker: str = WORKER_ID, reuse: bool = True,
                idempotency_key: str | None = None) -> ModelRun:
        """PROPOSE a model result: reuse a verified one with the same run
        identity and intact evidence, or run the model under governance.

        ``idempotency_key`` binds the submission durably (the proposal
        ingress passes the proposal's id): a resubmission returns the first
        task rather than computing twice."""
        if model_id not in MODEL_TOOLS:
            raise ModelRunRefused(f"no governed tool runs {model_id!r}")
        if reuse:
            ident = self.identity(model_id=model_id,
                                  model_version=model_version,
                                  parameters=parameters,
                                  out_dir=f"{out_dir}-identity",
                                  submitter=submitter, worker=worker)
            prior = self.reusable(ident)
            if prior:
                rec = self.authority.get(prior[0])
                sha = rec.evidence["result_bundle"]
                return ModelRun(None, "", sha, digest(self._record_of(sha)),
                                rec.record_id, rec.proposer, "",
                                reused_from=rec.record_id)
        inputs = {"out_dir": out_dir, "model_id": model_id,
                  "model_version": model_version, "parameters": parameters}
        run = self.gov.run(tool_id=MODEL_TOOLS[model_id], inputs=inputs,
                           submitter=submitter, worker=worker,
                           verifier=VERIFIER_ID,
                           idempotency_key=idempotency_key)
        return self._record_run(run, out_dir, submitter, worker)

    def propose_fmu(self, *, fmu_path: str, fmu_sha256: str,
                    build_record_path: str, build_record_sha256: str,
                    parameters: dict, step_s: float, runtime_python: str,
                    out_dir: str, submitter: str = SUBMITTER_ID,
                    worker: str = WORKER_ID,
                    idempotency_key: str | None = None) -> ModelRun:
        """PROPOSE an external model's result: run the FMU, cited by
        digest, under governance. Never reused: its identity is the FMU's
        bytes and runtime, which no catalog model computes."""
        inputs = {"out_dir": out_dir, "fmu_path": fmu_path,
                  "fmu_sha256": fmu_sha256,
                  "build_record_path": build_record_path,
                  "build_record_sha256": build_record_sha256,
                  "parameters": parameters, "step_s": float(step_s),
                  "runtime_python": runtime_python}
        run = self.gov.run(tool_id=TOOL_RUN_FMU, inputs=inputs,
                           submitter=submitter, worker=worker,
                           verifier=VERIFIER_ID,
                           idempotency_key=idempotency_key)
        return self._record_run(run, out_dir, submitter, worker)

    def _record_run(self, run, out_dir: str, submitter: str,
                    worker: str) -> ModelRun:
        if run.state is not TaskState.VERIFIED:
            raise ModelRunRefused(f"the governed run was {run.state.value}: "
                                  f"{run.reason}")
        rel = f"{out_dir}/bundle.json"
        sha = run.artifacts.get(rel)
        if sha is None:
            raise ModelRunRefused(f"no captured bundle at {rel}")
        bundle = self._record_of(sha)
        bundle_digest = digest(bundle)
        identity = (bundle.get("provenance") or {}).get("run_identity")
        if not isinstance(identity, dict):
            raise ModelRunRefused("the bundle records no run identity")
        # One record per governed task: an identical rerun is a second
        # claim, and must not collide with the first by content.
        record_id = f"result-{run.task_id}"
        try:
            prior = self.authority.get(record_id)
        except UnknownRecord:
            prior = None
        if prior is not None:
            # an idempotent resubmission returned the FIRST task; its record
            # already exists and is returned, not created a second time
            if prior.evidence.get("result_bundle") != sha:
                raise ModelRunRefused(f"{record_id} exists for another "
                                      "bundle")
            return ModelRun(run, rel, sha, bundle_digest, record_id,
                            prior.proposer, worker)
        self.authority.create(
            record_id=record_id, kind=RECORD_KIND, proposer=submitter,
            evidence={"result_bundle": sha,
                      "run_identity": self.evidence.put(
                          canonical_bytes(identity),
                          media_type="application/json")},
            policy_id=POLICY_ID)
        return ModelRun(run, rel, sha, bundle_digest, record_id,
                        submitter, worker)

    def invalidate_task(self, task_id: str, *, reason: str,
                        actor: str = "system") -> Invalidation:
        """A governed task's verdict no longer stands; what rested on it
        goes STALE.

        The task moves VERIFIED -> INVALIDATED through the gate. A
        scientific result citing an artefact that task captured before its
        verdict -- the bundle of a model run, the report of a check -- goes
        STALE when its origin no longer holds now, and so does every record
        depending on it, transitively. Nothing is rewritten: the admission
        stays in the history, judged where it stands, and the staleness is a
        later record citing what changed.
        """
        return self._invalidate(f"task:{task_id}", (task_id,),
                                reason=reason, actor=actor)

    def withdraw_evidence(self, sha: str, *, reason: str,
                          actor: str = "system") -> Invalidation:
        """Withdraw an artefact: a bundle, a report, or any file a model run
        produced that its bundle rests on.

        The bytes are content-addressed history and stay. What gave them
        authority is the governed task that captured them, so withdrawing
        them is invalidating every task standing VERIFIED that did -- and
        then following that as :meth:`invalidate_task` does.
        """
        tasks = self.origins.captured_by(sha)
        if not tasks:
            raise ModelRunRefused(
                f"no governed task standing VERIFIED captured {sha[:12]}; "
                "there is no authority here to withdraw")
        return self._invalidate(f"evidence:{sha}", tasks, reason=reason,
                                actor=actor)

    def settle(self, *, actor: str = "system") -> tuple:
        """Finish invalidations that were not followed through.

        The task's move and the records it makes STALE are separate appends,
        so a writer that stopped between them -- or one that moved the task
        and never followed it -- leaves a result VERIFIED on an origin that
        no longer holds. Reuse already refuses such a result; this makes the
        record say so. Every INVALIDATED task is followed again, citing
        itself as the origin; a task already followed through reaches
        nothing, so settling twice is settling once.
        """
        out = []
        for task in self.gov.projection().in_state(TaskState.INVALIDATED):
            inv = self._follow(
                f"task:{task.task_id}", (task.task_id,), actor=actor,
                reason="an invalidation not followed through when it was "
                       "made")
            if inv.plan.affected:
                out.append(inv)
        return tuple(out)

    def _invalidate(self, origin: str, task_ids: tuple, *, reason: str,
                    actor: str) -> Invalidation:
        if not reason:
            raise ValueError("an invalidation states what changed")
        for tid in task_ids:
            self.gov.invalidate(tid, reason=f"{origin}: {reason}",
                                actor=actor)
        return self._follow(origin, task_ids, reason=reason, actor=actor)

    def _follow(self, origin: str, task_ids: tuple, *, reason: str,
                actor: str) -> Invalidation:
        """Make STALE what rested on ``task_ids``, now INVALIDATED."""
        with self.origins.shared():
            return self._follow_shared(origin, task_ids, reason=reason,
                                       actor=actor)

    def _follow_shared(self, origin: str, task_ids: tuple, *, reason: str,
                       actor: str) -> Invalidation:
        withdrawn = self.origins.artefacts_of(task_ids)
        self.authority.catch_up()
        roots, kept = [], []
        for rid, rec in sorted(self.authority.all_records().items()):
            cited = {rec.evidence.get("result_bundle"),
                     rec.evidence.get("verification_report")}
            if rec.kind != RECORD_KIND or not cited & withdrawn:
                continue
            if rec.state in INVALIDATABLE:
                # Asked now, as reuse asks it: another governed task
                # standing VERIFIED may have produced the same bytes, and
                # then the result still has an origin.
                basis = rec.admission_basis or {}
                lost = self.origins.problems(
                    report_sha=rec.evidence.get("verification_report", ""),
                    bundle_sha=rec.evidence.get("result_bundle", ""),
                    proposer=rec.proposer, actor=basis.get("actor", ""),
                    before_seq=None)
                if not lost:
                    kept.append(rid)
                    continue
            roots.append(rid)
        plan = plan_from(self.authority.all_records(), roots, origin=origin)
        apply_plan(self.authority, plan, reason=reason, actor=actor)
        return Invalidation(origin, tuple(task_ids), plan, tuple(kept))

    def check(self, run: ModelRun, *, check_id: str, out_dir: str,
              worker: str = CHECK_WORKER_ID,
              runtime_python: str = "") -> CheckRun:
        """Run an independent check as its own governed task.

        ``runtime_python`` names the interpreter of a check that runs in
        its own environment (FEniCSx); it becomes a recorded input of the
        task, so the history says which runtime verified the result."""
        if run.reused_from:
            raise ModelRunRefused(f"{run.record_id} was reused, and is "
                                  "already decided; there is nothing to check")
        if worker in (run.worker, run.submitter):
            raise ModelRunRefused("the independent check must not be "
                                  "executed by whoever proposed or ran the "
                                  "work it checks")
        inputs = {"out_dir": out_dir, "bundle_path": run.bundle_path,
                  "bundle_sha256": run.bundle_sha256, "check_id": check_id,
                  "verifier_id": worker}
        if runtime_python:
            inputs["runtime_python"] = runtime_python
        gr = self.gov.run(tool_id=TOOL_CHECK, inputs=inputs,
                          submitter=SUBMITTER_ID, worker=worker,
                          verifier=VERIFIER_ID)
        if gr.state is not TaskState.VERIFIED:
            raise ModelRunRefused(f"the check task was {gr.state.value}: "
                                  f"{gr.reason}")
        rel = f"{out_dir}/verification.json"
        sha = gr.artifacts.get(rel)
        if sha is None:
            raise ModelRunRefused(f"no captured verification at {rel}")
        return CheckRun(gr, rel, sha, worker)

    def decide(self, run: ModelRun, check: CheckRun, *,
               reviewer: str = REVIEWER_ID):
        """The authority decision, by a reviewer, from the evidence."""
        if run.reused_from:
            raise ModelRunRefused(f"{run.record_id} was reused, and is "
                                  "already decided")
        if reviewer in (run.worker, check.worker, run.submitter):
            raise ModelRunRefused(
                f"{reviewer} proposed or executed this work; it cannot "
                "decide on it")
        self.gov.agents.require(reviewer, AgentRole.VERIFIER)
        bundle = self._record_of(run.bundle_sha256)
        problems = []
        if digest(bundle) != run.bundle_digest:
            problems.append("the bundle in evidence is not the one proposed")
        # The same admission the store enforces on the edge into VERIFIED
        # (qta_agent.result_rules), content AND origin: here it chooses
        # between VERIFIED and REJECTED and supplies the reasons; there it is
        # what a caller writing the transition directly cannot get past.
        problems += result_rules.admission_problems(
            result_rules.CURRENT_POLICY,
            {"result_bundle": run.bundle_sha256,
             "verification_report": check.report_sha256},
            fetch=self.evidence.get, origins=self.origins,
            proposer=run.submitter, actor=reviewer, before_seq=None)

        # Resumable: a decision interrupted after the pickup (a crash
        # between the two appends) finds the record UNDER_REVIEW and goes on
        # to the verdict, which I4 still holds apart from the proposer.
        if self.authority.get(run.record_id).state is not State.UNDER_REVIEW:
            self.authority.transition(record_id=run.record_id,
                                      dst=State.UNDER_REVIEW, actor=reviewer,
                                      role=Role.VERIFIER)
        if not problems:
            return self.authority.transition(
                record_id=run.record_id, dst=State.VERIFIED, actor=reviewer,
                role=Role.VERIFIER,
                evidence={"verification_report": check.report_sha256})
        reason = self.evidence.put(json.dumps(
            {"record_id": run.record_id, "problems": problems},
            sort_keys=True).encode(), media_type="application/json")
        return self.authority.transition(
            record_id=run.record_id, dst=State.REJECTED, actor=reviewer,
            role=Role.VERIFIER, evidence={"rejection_reason": reason,
                                          "verification_report":
                                              check.report_sha256})
