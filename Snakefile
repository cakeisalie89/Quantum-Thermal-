# The governed scientific workflow (Snakemake).
#
# The DEFAULT target, scientific_generic, runs the framework's own path: the
# end-to-end demonstration from a recorded proposal to a decided, restarted,
# reconstructed and packaged result (harness_demo), a governed model run
# checked and decided (s10_governed_model), the governed
# Stage-10 artifact path (s10_governed, s10_governed_index), and the
# scientific-stack adapters -- VTK and OpenUSD export, the read-only retrieval
# index, the FEniCSx acceptance harness, Rust parity, the FMI contract --
# closed by a check that the tracked tree was not touched. It needs no
# machine FSM, no hardware governance, no BOM and no gate table, and
# tests/test_workflow_split.py proves that from the DAG, not from this text.
#
# LEGACY QTA lives in workflow/legacy_qta.smk, included at the end, and stays
# invocable explicitly until it retires: the Stage-7 verification chain over
# qta_full_sim.py (full_verification), the Stage-8 HDF5 / RO-Crate payload
# (s8_full), the gate-table stack report with s10_full, and legacy_qta for
# all of them.
#
# Usage:
#   snakemake --cores 1                       # the generic workflow
#   snakemake --cores 1 s10_governed_model    # one governed model run
#   snakemake --cores 1 legacy_qta            # the legacy QTA pipeline
#
# Every rule writes under verification/ (the generic workspace is
# verification/stage10), never into the tracked tree. --cores 1 is the
# supported invocation: canonical generation is single-writer by design.

import hashlib, json, os, sys
from pathlib import Path

# One interpreter for every rule. The rules previously mixed bare "python3"
# with ".venv/bin/python": outside a "uv run" shell, "python3" is the system
# interpreter, which has no numpy, so every rule using it failed at import
# while the rules using the venv passed. sys.executable is whatever
# interpreter is running Snakemake, which is by construction the project
# environment.
PY = sys.executable

WS = "verification/snakemake"      # legacy QTA workspace
W10 = "verification/stage10"       # generic workspace
SRC_OUTPUTS = [l.split()[1] for l in []]  # populated at rule level
EXEMPT = {"deep_surrogate_readiness.json"}

def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

rule scientific_generic:
    # The default target. Every input is produced by a rule in this file;
    # none reaches a rule of the legacy QTA workflow file.
    input:
        f"{W10}/harness_demo_report.json",
        f"{W10}/governed_model/governed_model_run.json",
        f"{W10}/governed/governed_run.json",
        f"{W10}/governed_index/index_run.json",
        f"{W10}/canonical_untouched.json",

rule harness_demo:
    # The generic end-to-end demonstration: a recorded proposal, governed
    # retrieval context, the ingress, a governed model run, its HDF5
    # representation, independent checks, the reviewer's decision, a restart
    # from a checkpoint, reconstruction and an RO-Crate -- and, where the
    # FEniCSx and FMI runtimes are given (QTA_FENICSX_PYTHON,
    # QTA_FMI_PYTHON), the FEniCSx check admitting the result and the FMU
    # leg with its negative twin, the fault FMU, which must be REJECTED.
    # The tool exits non-zero unless its judge accepts: no vacuous success.
    # Snakemake orders this; the store decides.
    output:
        report=f"{W10}/harness_demo_report.json",
        workspace=directory(f"{W10}/harness_demo"),
    shell:
        "{PY} tools/harness_demo.py run --out {output.report}"
        " ${{QTA_FENICSX_PYTHON:+--fenicsx-python $QTA_FENICSX_PYTHON}}"
        " ${{QTA_FMI_PYTHON:+--fmi-python $QTA_FMI_PYTHON}}"

# ============ Stage-10 additive rules (scientific-stack adapters) ===========
# Visualization interchange (ParaView/VTK, OpenUSD), a read-only retrieval
# index, the staged FEniCSx acceptance harness, selective-Rust parity, and the
# deferred FMI contract. Every rule writes ONLY under verification/stage10 and
# the closing rule proves the tracked tree was not touched. Software
# verification only.


def _stage10_result():
    """One small deterministic 3D solve shared by the visualization rules."""
    from qta_multiphysics.config import default_config
    from qta_multiphysics.mesh_3d import Grid3DConfig
    from qta_multiphysics.thermal_3d_transient import solve_thermal_3d
    return solve_thermal_3d(default_config(), Grid3DConfig(nx=6, ny=6, nz=8),
                            n_eval=4)


rule s10_viz_vtk:
    # each exporter owns its own directory: the determinism rule digests a
    # whole directory, so sharing one with another writer would make the
    # comparison depend on job scheduling
    output: f"{W10}/viz/vtk/thermal_3d_vtk_manifest.json"
    run:
        from qta_multiphysics.stack import vtk_export as V
        m = V.export_thermal_3d(_stage10_result(), f"{W10}/viz/vtk")
        assert m["automatic_gate_effect"] == "NONE"
        assert m["n_timesteps_exported"] >= 1

rule s10_viz_vtk_determinism:
    # a re-export must reproduce every byte: the .vtr/.pvd payload is the
    # visualization counterpart of the project's byte-gated CSV outputs
    input: f"{W10}/viz/vtk/thermal_3d_vtk_manifest.json"
    output: f"{W10}/viz_determinism.json"
    run:
        from qta_multiphysics.stack import vtk_export as V
        before = V.export_dir_digest(f"{W10}/viz/vtk")
        V.export_thermal_3d(_stage10_result(), f"{W10}/viz/vtk")
        after = V.export_dir_digest(f"{W10}/viz/vtk")
        Path(output[0]).write_text(json.dumps(
            {"n_files": len(before), "byte_identical_on_reexport":
             before == after, "digests": after}, indent=1, sort_keys=True))
        assert before == after

rule s10_viz_usd:
    output: f"{W10}/viz/usd/qta_domain_usd_manifest.json"
    run:
        from qta_multiphysics.stack import usd_export as U
        m = U.export_usd_scene(_stage10_result(), f"{W10}/viz/usd")
        # usd-core is optional: an absent validator reports UNAVAILABLE and
        # must never be recorded as a pass
        assert m["validation"]["availability"] in ("AVAILABLE", "UNAVAILABLE")
        if m["validation"]["availability"] == "AVAILABLE":
            assert m["validation"]["result"] == "VALID", m["validation"]

rule s10_rag_index:
    output: f"{W10}/rag/rag_index.json"
    run:
        from qta_multiphysics.stack import rag_index as R
        info = R.write_index(f"{W10}/rag")
        idx = R.load_index(output[0])
        assert idx.stale_files() == [], idx.stale_files()
        assert info["n_chunks"] > 0

rule s10_fenicsx_acceptance:
    # FEniCSx stays STAGED; what CI proves today is that the acceptance
    # harness measures zero error for an exact solver and detects second-order
    # convergence for a real discretisation
    output: f"{W10}/fem/fenicsx_acceptance.json"
    run:
        from qta_multiphysics.stack import fem_fenicsx as F
        exact = F.run_acceptance(F.analytic_reference_solver,
                                 n_cells_sequence=(10, 20))
        conv = F.run_acceptance(F.fv_reference_solver,
                                n_cells_sequence=(20, 40, 80))
        status = F.status_report(f"{W10}/fem")
        assert exact["verdict"] == "EXACT_RECOVERED", exact
        assert conv["verdict"] == "PASS", conv
        assert conv["observed_order_L2"] >= \
            F.ACCEPTANCE_CRITERIA["mms_observed_order_min"]
        assert status["adoption_status"] == "STAGED"
        Path(output[0]).parent.mkdir(parents=True, exist_ok=True)
        Path(output[0]).write_text(json.dumps(
            {"harness_self_check": exact, "order_detection": conv,
             "adoption_status": status["adoption_status"],
             "dolfinx_available": status["availability"] == "AVAILABLE"},
            indent=1, sort_keys=True))

rule s10_rust_parity:
    output: f"{W10}/rust/rust_kernel_status.json"
    run:
        from qta_multiphysics.stack import rust_kernel as R
        rep = R.status_report(f"{W10}/rust")
        assert rep["default_backend"] == "numpy"
        # a committed decision admits a kernel, never an on-host parity
        # verdict; every kernel has one, and an adopted kernel is still bit
        # identical to the NumPy reference wherever it is measured here
        assert set(rep["decisions"]) == {k["kernel"] for k in rep["kernels"]}
        for name, decision in rep["decisions"].items():
            assert decision in (f"RUST_KERNEL_{name}_ADOPTED",
                                f"RUST_KERNEL_{name}_REJECTED"), decision
        for k in rep["kernels"]:
            if k["kernel"] in rep["adopted_kernels"] and \
                    k["parity"] != "NOT_MEASURED":
                assert k["parity"] == "BIT_IDENTICAL", k

rule s10_fmi_contract:
    output: f"{W10}/fmi/fmi_readiness.json"
    run:
        from qta_multiphysics.stack import fmi_contract as F
        rep = F.write_contract(f"{W10}/fmi")
        assert rep["adoption_status"] == "DEFERRED"
        assert rep["fmu_produced"] is False and rep["ready_to_export"] is False
        names = {p.name for p in Path(f"{W10}/fmi").iterdir()}
        assert "modelDescription.xml" not in names
        assert not any(n.endswith(".fmu") for n in names)

rule s10_tests:
    # runs under sys.executable, not a hard-coded .venv path, so the
    # fail-closed leg (no optional extras installed) is exercised in the
    # environment it is actually meant to prove
    output: f"{W10}/tests_stage10.json"
    run:
        import hashlib as _h, subprocess as _sp, sys as _sy
        log = Path(f"{output[0]}.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        r = _sp.run([_sy.executable, "-m", "pytest",
                     "tests/test_stage10_stack.py", "-q", "-rs"],
                    capture_output=True, text=True)
        log.write_text(r.stdout + r.stderr)
        assert r.returncode == 0, r.stdout[-2000:]
        Path(output[0]).write_text(json.dumps(
            {"suite": "stage10_stack", "interpreter": _sy.executable,
             "log_sha256": _h.sha256(log.read_bytes()).hexdigest()},
            indent=1, sort_keys=True))

rule s10_canonical_untouched:
    # The governance check for this stage: after every Stage-10 rule has run,
    # every canonical file must still match final_manifest.json byte for byte.
    input:
        f"{W10}/viz_determinism.json",
        f"{W10}/viz/usd/qta_domain_usd_manifest.json",
        f"{W10}/rag/rag_index.json", f"{W10}/fem/fenicsx_acceptance.json",
        f"{W10}/rust/rust_kernel_status.json", f"{W10}/fmi/fmi_readiness.json",
        f"{W10}/tests_stage10.json",
    output: f"{W10}/canonical_untouched.json"
    run:
        man = json.loads(Path("final_manifest.json").read_text())
        bad = [e["filename"] for e in man["files"]
               if not Path(e["filename"]).exists()
               or _sha(e["filename"]) != e["sha256"]]
        stored = Path("manifest_hash.txt").read_text().split(
            "sha256:")[1].split()[0].strip()
        rep = {"entries": len(man["files"]), "mismatches": len(bad),
               "mismatch_names": bad,
               "detached_hash_ok": stored == _sha("final_manifest.json"),
               "note": "Stage-10 adapters wrote only under verification/"}
        Path(output[0]).write_text(json.dumps(rep, indent=1, sort_keys=True))
        assert not bad and rep["detached_hash_ok"], rep

# ---- governed Stage-10 artifact production ---------------------------------
# The agent substrate's production path. Every other Stage-10 rule calls its
# adapter directly; this one goes through qta_agent, so the artifact it
# produces carries a task record, a capability grant, a bounded execution
# record, content-addressed evidence and an independent verification -- all in
# a hash-chained log that replays to the same state.
#
# It is part of s10_full deliberately. A governed path nobody runs is not a
# production path, and the point of this rule is that the ordinary Stage-10
# workflow now exercises the control plane rather than merely being able to.
#
# automatic_gate_effect = NONE. It writes one JSON artifact into the Stage-10
# workspace and cannot reach a gate, a threshold or a canonical output.

rule s10_governed:
    output:
        # NAMED, not positional. The report used to be written to output[1],
        # and adding a second artifact in the middle silently redirected it
        # to the artifact's path -- the rule "succeeded" and its report was
        # never written. A name cannot be shifted by an insertion.
        artifact=f"{W10}/governed/out/governed_artifact.json",
        summary=f"{W10}/governed/out/governed_summary.json",
        report=f"{W10}/governed/governed_run.json",
    run:
        import json
        from pathlib import Path

        from qta_agent.events import EventLog
        from qta_agent.evidence import EvidenceStore
        from qta_agent.governed_stage10 import GovernedStage10
        from qta_agent.tasks import TaskState

        root = Path(".").resolve()
        base = root / W10 / "governed"
        base.mkdir(parents=True, exist_ok=True)

        gov = GovernedStage10(
            root=root,
            log=EventLog(base / "task_log.jsonl"),
            evidence=EvidenceStore(base / "evidence"))

        # A GRAPH, not one job. The scheduler enqueued a single job per run
        # until now, so dependency graphs and multi-job scheduling were
        # exercised only in tests -- and a scheduler feature only tests reach
        # is one nobody is relying on.
        #
        # The second step declares the first as a dependency AND requires the
        # evidence digest it produced, so readiness is decided by
        # reconcile() against the log rather than by the order this rule
        # writes the steps in.
        graph = gov.run_graph([
            ("artifact", "stage10.emit_artifact", {
                "out_dir": f"{W10}/governed/out",
                "name": "governed_artifact.json",
                "payload": {
                    "label": "MODEL_ONLY / FORECAST_ONLY",
                    "automatic_gate_effect": "NONE",
                    "produced_by": "qta_agent governed Stage-10 path",
                    "does_not_mean": (
                        "a governed run proves provenance, not scientific "
                        "validity; no gate is reachable from here and PASS "
                        "remains 0"),
                },
            }, ()),
            ("summary", "stage10.emit_artifact", {
                "out_dir": f"{W10}/governed/out",
                "name": "governed_summary.json",
                "payload": {
                    "label": "MODEL_ONLY / FORECAST_ONLY",
                    "automatic_gate_effect": "NONE",
                    "summarises": "governed_artifact.json",
                    "does_not_mean": (
                        "a summary of a governed run is still provenance; "
                        "PASS remains 0"),
                },
            }, ("artifact",)),
        ])
        by_step = dict(graph)
        assert set(by_step) == {"artifact", "summary"}, (
            f"the graph did not run both steps: {sorted(by_step)}")
        run = by_step["artifact"]
        summary = by_step["summary"]

        assert summary.state is TaskState.VERIFIED, (
            f"the dependent step ended {summary.state.value}: "
            f"{summary.reason}")

        # The dependency is the SCHEDULER's. The summary's job must record
        # the artifact's job as a prerequisite and the artifact's evidence as
        # required -- if it did not, the ordering above was this rule's
        # convention rather than the queue's rule.
        enqueues = {ev.payload["job"]["job_id"]: ev.payload["job"]
                    for ev in gov.log.read()
                    if ev.action == "scheduler.enqueue"}
        summary_job = enqueues[summary.job_id]
        assert summary_job["depends_on"] == [run.job_id], (
            "the dependent step was enqueued without naming its parent; the "
            "ordering came from this rule, not from the scheduler")
        assert summary_job["requires_evidence"], (
            "the dependent step required no evidence from its parent, so it "
            "would have been ready even if the parent produced nothing")

        # The rule FAILS if the chain did not complete. A governed path that
        # reports success on an unverified run is worse than no governed path,
        # because it launders the absence of verification into a green build.
        assert run.state is TaskState.VERIFIED, (
            f"governed run ended {run.state.value}: {run.reason}")
        assert run.artifacts, "a verified run with no artifacts proves nothing"
        assert gov.log.verify().ok, "the task log does not verify"

        # Each of these is a subsystem that is ON the path rather than beside
        # it. If any were merely available, the run would still have reached
        # VERIFIED and these assertions would not.
        from qta_agent.scheduler import JobState

        assert run.job_state == JobState.SUCCEEDED.value, (
            f"the queue record ended {run.job_state}, not SUCCEEDED; the "
            "work did not go through the scheduler")
        assert run.policy_identity and run.policy_digest, (
            "no policy decision was recorded for this run")
        assert run.context_digest, "no context manifest was recorded"
        assert run.memory_id, "no note was filed for this run"

        actions_seen = {ev.action for ev in gov.log.read()}
        required = {"policy.publish", "policy.decision", "agent.register",
                    "scheduler.enqueue", "scheduler.transition",
                    "task.create", "capability.issue", "task.execution",
                    "task.evidence", "context.build", "memory.write"}
        missing = sorted(required - actions_seen)
        assert not missing, (
            f"the governed run's history is missing {missing}; a subsystem "
            "that leaves no record was not on the path")

        # No egress grant was ever issued, and the default is no network.
        assert not [ev for ev in gov.log.read()
                    if ev.action == "network.grant"], (
            "a governed Stage-10 run needs no network and must hold no "
            "egress grant")

        # The note this run filed is a note. Its digest must not resolve as
        # evidence, or a remembered statement could support a transition.
        note = gov.memory.get(run.memory_id)
        assert not gov.evidence.contains(note.digest()), (
            "the run's memory entry resolves as evidence; nothing checked it")

        # The audit is part of the production path, not a separate tool. A
        # chain with a provenance hole fails the build: the transitions were
        # all permitted, but a hole is indistinguishable from a fabrication
        # nobody noticed, and a green build must not certify one.
        from qta_agent.audit import AuditIndex

        index = AuditIndex.from_log(gov.log)
        explanation = index.explain_task(run.task_id)
        assert explanation.complete, (
            "the governed run has provenance gaps:\n"
            + "\n".join(f"  - {g}" for g in explanation.gaps))

        # The decision that permitted the run must JOIN to a document this
        # log published. A decision naming a policy digest nobody published
        # is an assertion that a policy allowed it, and asserting that is
        # exactly what an unauthorized run would do.
        permitting = [d for d in index.decisions(allowed=True)
                      if d.detail["policy_digest"] == run.policy_digest]
        assert permitting, (
            "no recorded policy decision matches the digest this run "
            "reports; the run claims a policy permitted it and the history "
            "does not show one")
        decision = index.explain_decision(permitting[0].seq)
        assert decision.complete, (
            "the permitting decision does not join to a published policy:\n"
            + "\n".join(f"  - {g}" for g in decision.gaps))
        assert not index.denials(), (
            "a governed run recorded a policy denial and still reported "
            f"success: {[d.summary for d in index.denials()]}")

        # A SECOND reader of the same bytes, sharing no reducer with the
        # projection above. Two implementations that agree is differential
        # evidence; one implementation with a hole says nothing at all, and
        # the task projection is where a hole was actually found.
        from qta_agent.reconstruct import compare_tasks, reconstruct_tasks

        recon = reconstruct_tasks(gov.log)
        divergences = compare_tasks(gov.projection(), recon)
        assert not divergences, (
            "the live projection and an independent replay disagree:\n"
            + "\n".join(f"  - {d}" for d in divergences))
        assert not recon.unauthorized, recon.unauthorized
        assert not recon.anomalies, recon.anomalies

        # And the same for every OTHER authority subsystem. Until this
        # existed, the scheduler, policy, capability, agent, memory,
        # network, secret and context projections were compared only
        # against a fresh replay of THEMSELVES -- which shares their
        # reducer, and so cannot see a mistake the two would make together.
        from qta_agent.reconstruct import (compare_subsystems,
                                           reconstruct_subsystems)

        subs = reconstruct_subsystems(gov.log)
        assert not subs.anomalies, (
            "the second reader found authority anomalies the projections "
            "did not:\n" + "\n".join(f"  - {a}" for a in subs.anomalies))
        primary_state = {
            "jobs": {j.job_id: {"state": j.state.value,
                                "attempts": j.attempts}
                     for j in gov.scheduler.all_jobs().values()},
            "agents": {i.instance_id: {"kind": i.kind.value}
                       for i in gov.agents.instances()},
            "memory": {e.memory_id: {"author": e.author,
                                     "status": e.status.value}
                       for e in gov.memory.all_entries()},
        }
        sub_divergences = compare_subsystems(primary_state, subs)
        assert not sub_divergences, (
            "a subsystem projection and its independent reader disagree:\n"
            + "\n".join(f"  - {d}" for d in sub_divergences))

        # Authority records, if this history holds any, must be whole too.
        record_gaps = [e for e in index.audit_records() if not e.complete]
        assert not record_gaps, (
            "authority records with provenance gaps:\n"
            + "\n".join(f"  - {e.subject}: {g}"
                         for e in record_gaps for g in e.gaps))

        Path(output.report).write_text(json.dumps({
            "task_id": run.task_id,
            "state": run.state.value,
            "outcome": run.outcome,
            "result_digest": run.result_digest,
            "artifacts": run.artifacts,
            "log_head_seq": run.log_head_seq,
            "verification": run.reason,
            "job_id": run.job_id,
            "job_state": run.job_state,
            "policy": {"identity": run.policy_identity,
                       "digest": run.policy_digest},
            "context_manifest_digest": run.context_digest,
            "memory_id": run.memory_id,
            "egress_grants": 0,
            "automatic_gate_effect": "NONE",
            "scientific_PASS_count": 0,
            "does_not_mean": (
                "a VERIFIED task means a declared tool ran bounded, produced "
                "the bytes it claims, and a separated actor confirmed they "
                "are still there. That is provenance. It is not scientific "
                "validity, not a measurement, and not a gate."),
            "provenance": explanation.to_record(),
            "policy_decision": decision.to_record(),
            "policy_denials": 0,
            "authority_records_audited": len(index.records()),
            "independent_replay": {
                "agrees": True, "divergences": 0,
                "events_replayed": recon.events_replayed,
                "tasks_verified": list(recon.verified_ids()),
                "subsystems_agree": True,
                "subsystems_covered": sorted(
                    k for k, v in {
                        "jobs": subs.jobs, "policies": subs.policies,
                        "decisions": subs.decisions,
                        "capabilities": subs.capabilities,
                        "agents": subs.agents, "memory": subs.memory,
                        "net_grants": subs.net_grants,
                        "secret_grants": subs.secret_grants,
                        "contexts": subs.contexts}.items() if v),
            },
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---- the SECOND governed workflow ------------------------------------------
# Until this rule existed the matrix's honest description of R55 was "ONE
# workflow, of the safest available kind": a single rule, running a single
# tool, whose output was a function of a payload the rule itself had written
# into the request. Re-execution could not disagree with it, a missing file
# could not fail it, and the registry's default-deny set had one member.
#
# This is a different workflow, running a DIFFERENT tool, over the real
# outputs of the visualization, USD, RAG and governed-artifact rules. Its
# result is a function of bytes on disk, so:
#
#   * the verifier's re-execution is a real comparison rather than a constant
#     computed twice;
#   * a named file that is missing, unreadable or outside the workspace fails
#     the run, and a failed run never reaches verification;
#   * the read guard is on the path, not beside it.
#
# automatic_gate_effect = NONE, and this rule is where that is most worth
# saying: it READS the artifacts of five other rules and writes an index of
# what it found. An index is provenance. It is not a gate, it is not a
# manifest, no canonical output is among its inputs -- the read guard refuses
# them -- and nothing downstream of it can change the PASS count.

rule s10_governed_index:
    input:
        artifact=f"{W10}/governed/out/governed_artifact.json",
        summary=f"{W10}/governed/out/governed_summary.json",
        vtk=f"{W10}/viz/vtk/thermal_3d_vtk_manifest.json",
        usd=f"{W10}/viz/usd/qta_domain_usd_manifest.json",
        rag=f"{W10}/rag/rag_index.json",
    output:
        index=f"{W10}/governed_index/out/stage10_index.json",
        report=f"{W10}/governed_index/index_run.json",
    run:
        import json
        from pathlib import Path

        from qta_agent.events import EventLog
        from qta_agent.evidence import EvidenceStore
        from qta_agent.governed_stage10 import (ACT_REEXECUTION,
                                                GovernedStage10)
        from qta_agent.tasks import TaskState

        root = Path(".").resolve()
        base = root / W10 / "governed_index"
        base.mkdir(parents=True, exist_ok=True)

        # Its OWN log and evidence store. Sharing the first workflow's would
        # make "two workflows" one history with two entry points, and the
        # question this rule exists to answer is whether the governed path
        # works for a caller that is not the one it was written for.
        gov = GovernedStage10(
            root=root,
            log=EventLog(base / "task_log.jsonl"),
            evidence=EvidenceStore(base / "evidence"))

        files = sorted(str(Path(f).as_posix())
                       for f in (input.artifact, input.summary, input.vtk,
                                 input.usd, input.rag))
        run = gov.run(tool_id="stage10.digest_index", inputs={
            "out_dir": f"{W10}/governed_index/out",
            "name": "stage10_index.json",
            "files": files,
        })

        assert run.state is TaskState.VERIFIED, (
            f"the governed index run ended {run.state.value}: {run.reason}")
        assert run.artifacts, "a verified run with no artifacts proves nothing"
        assert gov.log.verify().ok, "the index task log does not verify"

        # THE RE-EXECUTION IS THE POINT OF THIS RULE. Verification that only
        # re-derives digests from disk confirms the bytes did not move; it
        # does not confirm the tool would produce them again. Here it can,
        # because the tool is BYTE_IDENTICAL and its inputs are files.
        reexec = [ev for ev in gov.log.read() if ev.action == ACT_REEXECUTION]
        assert reexec, (
            "the verified run recorded no re-execution; verification fell "
            "back to re-deriving digests and the rule must not report that "
            "as the stronger check")
        assert reexec[-1].payload["tool_id"] == "stage10.digest_index"
        assert "reproduced byte-for-byte" in run.reason, run.reason

        indexed = json.loads(Path(output.index).read_text(encoding="utf-8"))
        assert indexed["n_files"] == len(files), (
            f"the index covers {indexed['n_files']} of {len(files)} declared "
            "files; a partial index that reports success is the vacuous "
            "shape this repository has shipped once already")
        assert indexed["automatic_gate_effect"] == "NONE"

        # No canonical output is reachable from here, and that is checked
        # rather than asserted in prose: every indexed path is inside the
        # Stage-10 workspace, which is where the read guard confines it.
        for entry in indexed["files"]:
            assert entry["path"].startswith(W10 + "/"), (
                f"the index names {entry['path']}, which is outside the "
                "Stage-10 workspace; the substrate does not mediate "
                "canonical outputs and an index entry naming one would say "
                "it did")

        # The provenance of THIS workflow, audited by the same index the
        # first one uses. A second caller with a provenance hole is a second
        # caller nobody checked.
        from qta_agent.audit import AuditIndex

        idx = AuditIndex.from_log(gov.log)
        explanation = idx.explain_task(run.task_id)
        assert explanation.complete, (
            "the governed index run has provenance gaps:\n"
            + "\n".join(f"  - {g}" for g in explanation.gaps))
        assert not idx.denials(), (
            "a governed run recorded a policy denial and still reported "
            f"success: {[d.summary for d in idx.denials()]}")

        from qta_agent.reconstruct import compare_tasks, reconstruct_tasks

        recon = reconstruct_tasks(gov.log)
        divergences = compare_tasks(gov.projection(), recon)
        assert not divergences, (
            "the live projection and an independent replay disagree:\n"
            + "\n".join(f"  - {d}" for d in divergences))
        assert not recon.unauthorized, recon.unauthorized
        assert not recon.anomalies, recon.anomalies

        Path(output.report).write_text(json.dumps({
            "task_id": run.task_id,
            "state": run.state.value,
            "tool_id": "stage10.digest_index",
            "outcome": run.outcome,
            "result_digest": run.result_digest,
            "artifacts": run.artifacts,
            "log_head_seq": run.log_head_seq,
            "verification": run.reason,
            "job_id": run.job_id,
            "job_state": run.job_state,
            "reexecution_records": len(reexec),
            "indexed_files": indexed["n_files"],
            "workflow": "second governed workflow; independent log and "
                        "evidence store from s10_governed",
            "automatic_gate_effect": "NONE",
            "scientific_PASS_count": 0,
            "does_not_mean": (
                "an index of governed artifacts records which bytes were "
                "present when a governed run read them. It is provenance, "
                "not scientific validity; no canonical output is indexed, no "
                "gate is reachable, and PASS remains 0"),
            "provenance": explanation.to_record(),
            "independent_replay": {
                "agrees": True, "divergences": 0,
                "events_replayed": recon.events_replayed,
                "tasks_verified": list(recon.verified_ids()),
            },
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")



# ---- the governed SCIENTIFIC-MODEL path ------------------------------------
# The production caller of qta_agent.governed_model: thermal.conduction_1d at
# its declared (forecast) configuration, run as a governed task, checked by
# an independent implementation as a second governed task by a different
# executor, and decided by a reviewer through the authority store -- which
# enforces the content rule on the edge itself. Then the SAME proposal again:
# it must reuse the verified result (no second model run), and a proposal
# with other parameters must not.
#
# A VERIFIED scientific_result is a checked simulation result. It is not a
# measurement, not experimental validation, not PROMOTED, and not a gate:
# automatic_gate_effect = NONE and PASS stays 0.

rule s10_governed_model:
    output:
        report=f"{W10}/governed_model/governed_model_run.json",
    run:
        import json
        import shutil
        from pathlib import Path

        from qta_agent.authority import State
        from qta_agent.events import EventLog
        from qta_agent.evidence import EvidenceStore
        from qta_agent.governed_model import TOOL_RUN, GovernedModelRuns
        from qta_agent.tasks import TaskState

        root = Path(".").resolve()
        base = root / W10 / "governed_model"
        # One history per invocation. A history left by an earlier
        # invocation would let the first proposal reuse, and the rule would
        # no longer show both halves.
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
        log = EventLog(base / "task_log.jsonl")
        g = GovernedModelRuns(root=root, log=log,
                              evidence=EvidenceStore(base / "evidence"))
        model = {"model_id": "thermal.conduction_1d", "model_version": "1.0.0"}
        params = {}

        def model_runs():
            report, events = log.read_verified()
            assert report.ok, "the model task log does not verify"
            return sum(1 for e in events if e.action == "task.create"
                       and e.payload.get("tool_id") == TOOL_RUN)

        first = g.propose(**model, parameters=params,
                          out_dir=f"{W10}/governed_model/first")
        assert first.reused_from == "", (
            "a fresh history had something to reuse")
        assert first.governed.state is TaskState.VERIFIED
        check = g.check(first, check_id="thermal_1d.reduction_2d_radial_disabled",
                        out_dir=f"{W10}/governed_model/check")
        decided = g.decide(first, check)
        assert decided.state is State.VERIFIED, (
            f"the result was {decided.state.value}: "
            f"{decided.evidence.get('rejection_reason')}")
        assert first.record_id not in g.authority.canonical(), (
            "nothing on this path promotes; VERIFIED is not canonical")
        runs = model_runs()

        again = g.propose(**model, parameters=params,
                          out_dir=f"{W10}/governed_model/again")
        assert again.reused_from == first.record_id, (
            "an identical proposal did not reuse the verified result")
        assert model_runs() == runs, "reuse ran the model anyway"

        other = g.propose(**model, parameters={"n_cells": 201},
                          out_dir=f"{W10}/governed_model/other")
        assert other.reused_from == "", (
            "a proposal with other parameters reused a result")
        assert model_runs() == runs + 1

        # The SECOND model, by the same line: thermal 2D axisymmetric with
        # adiabatic sides, checked by the 3D Cartesian solver. At the
        # production (cold-contact) boundary there is no independent check,
        # and the rule does not pretend otherwise by running it here.
        model2 = {"model_id": "thermal.conduction_2d_axisymmetric",
                  "model_version": "1.0.0"}
        params2 = {"lateral_boundary": "adiabatic"}
        second = g.propose(**model2, parameters=params2,
                           out_dir=f"{W10}/governed_model/second")
        check2 = g.check(second,
                         check_id="thermal_2d.reduction_3d_adiabatic_lateral",
                         out_dir=f"{W10}/governed_model/check2")
        decided2 = g.decide(second, check2)
        assert decided2.state is State.VERIFIED, (
            f"the 2D result was {decided2.state.value}: "
            f"{decided2.evidence.get('rejection_reason')}")

        assert log.verify().ok, "the model task log does not verify"
        from qta_agent.audit import AuditIndex
        from qta_agent.reconstruct import compare_tasks, reconstruct_tasks

        index = AuditIndex.from_log(log)
        record_gaps = [e for e in index.audit_records() if not e.complete]
        assert not record_gaps, (
            "authority records with provenance gaps:\n"
            + "\n".join(f"  - {e.subject}: {gap}"
                         for e in record_gaps for gap in e.gaps))
        assert not index.denials(), [d.summary for d in index.denials()]
        recon = reconstruct_tasks(log)
        divergences = compare_tasks(g.gov.projection(), recon)
        assert not divergences, divergences
        assert not recon.unauthorized and not recon.anomalies
        # The authority records too, by the second reader WITH the evidence:
        # it re-decides each scientific admission in its own code, and must
        # admit both results and agree with the store field by field.
        from qta_agent.reconstruct import compare, reconstruct
        authority = reconstruct(log, evidence=g.evidence)
        assert not authority.unauthorized and not authority.anomalies, (
            authority.unauthorized + authority.anomalies)
        assert not authority.unverifiable, authority.unverifiable
        assert compare(g.authority, authority) == ()
        for rid in (first.record_id, second.record_id):
            assert authority.records[rid]["admission"] == "ADMITTED", rid
            assert g.authority.get(rid).admission == "ADMITTED", rid

        bundle = json.loads(g.evidence.get(first.bundle_sha256))
        verification = json.loads(g.evidence.get(check.report_sha256))
        Path(output.report).write_text(json.dumps({
            "model": model,
            "parameters": params,
            "record_id": first.record_id,
            "record_state": decided.state.value,
            "bundle_sha256": first.bundle_sha256,
            "bundle_digest": first.bundle_digest,
            "run_identity": decided.evidence["run_identity"],
            "invariants": {i["invariant_id"]: i["holds"]
                           for i in bundle["invariants"]},
            "independent_check": {
                "check_id": verification["check_id"],
                "status": verification["status"],
                "independence": verification["independence"],
                "report_sha256": check.report_sha256},
            "reuse": {"identical_proposal_reused": again.reused_from,
                      "other_parameters_recomputed": other.record_id,
                      "thermal_1d_model_runs": model_runs()},
            "second_model": {
                "model": model2, "parameters": params2,
                "record_id": second.record_id,
                "record_state": decided2.state.value,
                "bundle_digest": second.bundle_digest,
                "independent_check": json.loads(
                    g.evidence.get(check2.report_sha256))["check_id"]},
            "observation_kind": bundle["observation_kind"],
            "promoted": False,
            "automatic_gate_effect": "NONE",
            "scientific_PASS_count": 0,
            "does_not_mean": (
                "a VERIFIED scientific_result is a simulation result whose "
                "bundle holds its invariants and which an independent "
                "implementation agreed with within a borrowed criterion. It "
                "is a forecast at assumed inputs: not a measurement, not "
                "experimental validation, not promoted, and not a gate"),
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---- opt-in Stage-10 rules (each evaluation is a full 3D solve) ------------
# Not part of s10_full: a Sobol cross-check is ~96 solves and a DOE sweep is
# one solve per sample. Run them deliberately, as with --heavy-3d.

rule s10_uq_sobol:
    output: f"{W10}/uq/salib_sobol_cross_check.json"
    run:
        from qta_multiphysics.stack import sensitivity_salib as S
        rep = S.run_cross_check(f"{W10}/uq", method="sobol", n_base=16)
        assert rep["role"] == "CROSS_CHECK_ONLY"

rule s10_mdao_doe:
    output: f"{W10}/mdao/openmdao_doe.json"
    run:
        from qta_multiphysics.stack import mdao_openmdao as M
        rep = M.run_doe(f"{W10}/mdao", n_samples=8)
        assert rep["status"] == "NOT_A_RECOMMENDATION"


# ---- LEGACY QTA, invoked explicitly (see the header) -------------------------
include: "workflow/legacy_qta.smk"
