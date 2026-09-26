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
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentRole, PrincipalKind, identity
from .authority import Role, State
from .canonical import canonical_bytes, digest
from .events import EventLog
from .evidence import EvidenceStore
from .result_rules import KIND as RECORD_KIND
from .result_rules import record_problems, verification_problems
from .governed_stage10 import (
    POLICY_ID, SUBMITTER_ID, VERIFIER_ID, WORKER_ID, WORKSPACE_PREFIX,
    GovernedRun, GovernedStage10,
)
from .store import AuthorityStore
from .tasks import TaskState
from .tools import (Determinism, Field_, OutputFile, Registry, SideEffect,
                    ToolSpec)

TOOL_RUN = "model.thermal.conduction_1d.run"
TOOL_CHECK = "model.independent_check"
TOOL_IDENTITY = "model.run_identity"
_TOOL_MODULES = {TOOL_RUN: "scientific._governed_run",
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


def model_registry() -> Registry:
    return Registry([
        ToolSpec(
            tool_id=TOOL_RUN, version="1.0.0",
            summary="run thermal.conduction_1d@1.0.0 and write its "
                    "ResultBundle and temperature field",
            inputs=(Field_("out_dir", "str"), Field_("model_id", "str"),
                    Field_("model_version", "str"),
                    Field_("parameters", "dict")),
            outputs=(Field_("path", "str"), Field_("sha256", "str"),
                     Field_("bundle_digest", "str"),
                     Field_("all_invariants_hold", "bool")),
            output_files=(
                OutputFile("bundle", "{out_dir}/bundle.json"),
                OutputFile("temperature_field",
                           "{out_dir}/temperature_field.bin")),
            # Measured, not assumed: the solve is deterministic on one
            # machine, and nothing time-dependent enters the bundle. The
            # verifier re-runs it and compares bytes.
            determinism=Determinism.BYTE_IDENTICAL,
            side_effect=SideEffect.SCOPED_WRITES,
            writable_scope=(WORKSPACE_PREFIX,), timeout_s=600.0),
        ToolSpec(
            tool_id=TOOL_CHECK, version="1.0.0",
            summary="run an admitted independent check on a cited bundle "
                    "and write its VerificationResult",
            inputs=(Field_("out_dir", "str"), Field_("bundle_path", "str"),
                    Field_("bundle_sha256", "str"), Field_("check_id", "str"),
                    Field_("verifier_id", "str")),
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


class GovernedModelRuns:
    def __init__(self, *, root: Path, log: EventLog,
                 evidence: EvidenceStore):
        self.root = Path(root)
        self.gov = GovernedStage10(root=root, log=log, evidence=evidence,
                                   registry=model_registry())
        self.gov.tool_modules = dict(_TOOL_MODULES)
        self.evidence = evidence
        self.authority = AuthorityStore(log, evidence=evidence).load()
        for iid, role in ((CHECK_WORKER_ID, AgentRole.EXECUTOR),
                          (REVIEWER_ID, AgentRole.VERIFIER)):
            try:
                self.gov.agents.get(iid)
            except Exception:                        # noqa: BLE001
                self.gov.agents.register(
                    identity(agent_id=iid, instance_id=iid,
                             kind=PrincipalKind.AGENT, roles={role}),
                    by="system")

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
        artefact it references must still resolve. A result is reused by its
        evidence, never by its name.
        """
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
            # The decision is re-derived too: the report the record cites
            # must still resolve and still support this bundle.
            if record_problems(rec.evidence, self.evidence.get):
                continue
            out.append(rid)
        return out

    def propose(self, *, model_id: str, model_version: str,
                parameters: dict, out_dir: str,
                submitter: str = SUBMITTER_ID,
                worker: str = WORKER_ID, reuse: bool = True) -> ModelRun:
        """PROPOSE a model result: reuse a verified one with the same run
        identity and intact evidence, or run the model under governance."""
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
        run = self.gov.run(tool_id=TOOL_RUN, inputs=inputs,
                           submitter=submitter, worker=worker,
                           verifier=VERIFIER_ID)
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
        self.authority.create(
            record_id=record_id, kind=RECORD_KIND, proposer=submitter,
            evidence={"result_bundle": sha,
                      "run_identity": self.evidence.put(
                          canonical_bytes(identity),
                          media_type="application/json")},
            policy_id=POLICY_ID)
        return ModelRun(run, rel, sha, bundle_digest, record_id,
                        submitter, worker)

    def check(self, run: ModelRun, *, check_id: str, out_dir: str,
              worker: str = CHECK_WORKER_ID) -> CheckRun:
        """Run an independent check as its own governed task."""
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
        report = self._record_of(check.report_sha256)
        problems = []
        if digest(bundle) != run.bundle_digest:
            problems.append("the bundle in evidence is not the one proposed")
        # The same rule the store enforces on the edge into VERIFIED
        # (qta_agent.result_rules): here it chooses between VERIFIED and
        # REJECTED and supplies the reasons; there it is what a caller
        # writing the transition directly cannot get past.
        problems += verification_problems(bundle, report)

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
