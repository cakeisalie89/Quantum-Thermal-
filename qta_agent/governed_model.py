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

WHERE THE CONTENT RULE LIVES, AND WHERE IT DOES NOT

The authority store checks that cited evidence EXISTS, who may take which
edge, and that the verifier is not the proposer. It does not read the
report. So the rule "a scientific result is VERIFIED only on a PASS report
about THIS bundle, from code other than the producer's, with every
invariant of the bundle holding" is enforced by :meth:`decide`, the one path
this repository provides for scientific results. A caller that writes a
transition to the store directly is not stopped by it. That is recorded in
the convergence plan as a residual (move the rule into an edge validator),
not claimed closed here.

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
from .canonical import digest, is_digest
from .events import EventLog
from .evidence import EvidenceStore
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
_TOOL_MODULES = {TOOL_RUN: "scientific._governed_run",
                 TOOL_CHECK: "scientific._governed_check"}

#: The executor of the independent check. Registered here as an EXECUTOR
#: distinct from the model run's worker.
CHECK_WORKER_ID = "model-check-worker"
#: Who decides authority for a scientific result: a VERIFIER in the
#: authority sense, distinct from the proposer and from both executors.
REVIEWER_ID = "model-result-reviewer"

RECORD_KIND = "scientific_result"


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
    ])


@dataclass(frozen=True)
class ModelRun:
    governed: GovernedRun
    bundle_path: str
    bundle_sha256: str
    #: the canonical digest of the bundle record (ResultBundle.digest()).
    bundle_digest: str
    record_id: str
    submitter: str
    worker: str


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

    def propose(self, *, model_id: str, model_version: str,
                parameters: dict, out_dir: str,
                submitter: str = SUBMITTER_ID,
                worker: str = WORKER_ID) -> ModelRun:
        """Run the model under governance and PROPOSE its result."""
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
        bundle_digest = digest(self._record_of(sha))
        record_id = f"result-{bundle_digest[:24]}"
        self.authority.create(
            record_id=record_id, kind=RECORD_KIND, proposer=submitter,
            evidence={"result_bundle": sha}, policy_id=POLICY_ID)
        return ModelRun(run, rel, sha, bundle_digest, record_id,
                        submitter, worker)

    def check(self, run: ModelRun, *, check_id: str, out_dir: str,
              worker: str = CHECK_WORKER_ID) -> CheckRun:
        """Run an independent check as its own governed task."""
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
        if report.get("subject_digest") != run.bundle_digest:
            problems.append("the report is about a different bundle")
        if report.get("status") != "PASS":
            problems.append(f"the check reported {report.get('status')}")
        if report.get("producer_implementation_digest") != \
                bundle.get("implementation_digest"):
            problems.append("the report names another producer")
        vdig = report.get("verifier_implementation_digest")
        if not is_digest(vdig) or vdig == bundle.get(
                "implementation_digest"):
            problems.append("the check ran the producer's own code")
        if report.get("check_type") != "INDEPENDENT_IMPLEMENTATION":
            problems.append("the report is not an independent check")
        bad = [i["invariant_id"] for i in bundle.get("invariants", ())
               if i.get("holds") is not True]
        if not bundle.get("invariants") or bad:
            problems.append(f"invariants not holding: {bad or 'none run'}")

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
