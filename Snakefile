# QTA Stage-7 Snakemake workflow. Wraps the EXISTING authoritative
# commands; rewrites no scientific solver. MODEL-ONLY / FORECAST-ONLY.
# Software-verification results only — the scientific gate PASS count is
# zero and is protected by the wrapped checkers themselves.
#
# Usage:
#   snakemake -n full_verification            # dry run
#   snakemake --cores 1 full_verification     # everything
#   snakemake --cores 1 clean_workspace       # remove generated workspace
#
# Workspace: all generated artifacts go under verification/snakemake/
# (never the canonical source tree). Verification-only rules never touch
# the canonical manifest. --cores 1 is the supported invocation: canonical
# generation is single-writer by design (no parallel writes to one file).

import hashlib, json, os, sys
from pathlib import Path

# One interpreter for every rule. The rules previously mixed bare "python3"
# with ".venv/bin/python": outside a "uv run" shell, "python3" is the system
# interpreter, which has no numpy, so every rule using it failed at import
# while the rules using the venv passed. sys.executable is whatever
# interpreter is running Snakemake, which is by construction the project
# environment.
PY = sys.executable

WS = "verification/snakemake"
SRC_OUTPUTS = [l.split()[1] for l in []]  # populated at rule level
EXEMPT = {"deep_surrogate_readiness.json"}

def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

rule full_verification:
    input:
        f"{WS}/env_verified.json",
        f"{WS}/registries_validated.json",
        f"{WS}/invariants_validated.json",
        f"{WS}/tests_fast.json",
        f"{WS}/package_checker.txt",
        f"{WS}/manuscript_checker.txt",
        f"{WS}/canonical_outputs.done.json",
        f"{WS}/outputs_validated.json",
        f"{WS}/gate_table_validated.json",
        f"{WS}/roadmap_validated.txt",
        f"{WS}/manifest_verified.json",
    output:
        f"{WS}/reproducibility_report.json"
    run:
        rep = {"workflow": "stage7-snakemake", "inputs": {}}
        for f in input:
            rep["inputs"][os.path.basename(f)] = _sha(f)
        ov = json.loads(Path(f"{WS}/outputs_validated.json").read_text())
        rep["governed_output_comparison"] = ov
        rep["note"] = ("software verification only; scientific gate PASS "
                       "count remains zero")
        Path(output[0]).write_text(json.dumps(rep, indent=1, sort_keys=True))

rule env_verified:
    output: f"{WS}/env_verified.json"
    run:
        import sys, platform
        import numpy, scipy, qutip
        d = {"python": sys.version.split()[0],
             "platform": platform.machine(),
             "numpy": numpy.__version__, "scipy": scipy.__version__,
             "qutip": qutip.__version__}
        assert d["numpy"] == "2.4.4" and d["scipy"] == "1.17.1" \
            and d["qutip"] == "5.2.1", d
        Path(output[0]).write_text(json.dumps(d, indent=1, sort_keys=True))

rule registries_validated:
    input:
        "experiment_registry.json", "experiment_gate_coverage.json",
        "experiment_matrix_coverage.json", "campaign_registry.json",
        "validation_matrix_update_request.schema.json",
    output: f"{WS}/registries_validated.json"
    run:
        d = {}
        for f in input:
            doc = json.loads(Path(f).read_text())
            d[f] = {"sha256": _sha(f),
                    "schema_version": doc.get("schema_version")}
        assert all(v["schema_version"] in ("1.0.0",)
                   for k, v in d.items()
                   if not k.endswith("schema.json"))
        Path(output[0]).write_text(json.dumps(d, indent=1, sort_keys=True))

rule invariants_validated:
    output: f"{WS}/invariants_validated.json"
    shell:
        "{PY} tests/test_mode_species_3d.py > {output}.log 2>&1 && "
        "{PY} tests/test_machine_fsm.py >> {output}.log 2>&1 && "
        "{PY} -c \"import json,hashlib;"
        "json.dump({{'invariant_suites': ['mode_species_3d','machine_fsm'],"
        "'log_sha256': hashlib.sha256(open('{output}.log','rb').read())"
        ".hexdigest()}}, open('{output}','w'), indent=1)\""

rule tests_fast:
    output: f"{WS}/tests_fast.json"
    shell:
        "{PY} tests/test_stage6_roadmap.py > {output}.log 2>&1 && "
        "{PY} tests/test_hardware_governance.py >> {output}.log 2>&1 && "
        "{PY} tests/test_measurement_ingest.py >> {output}.log 2>&1 && "
        "{PY} tests/test_campaign_uncertainty.py >> {output}.log 2>&1 && "
        "{PY} -m pytest "
        "tests/test_stage7_boundary.py -q >> {output}.log 2>&1 && "
        "{PY} -c \"import json,hashlib;"
        "json.dump({{'suites': ['stage6_roadmap','hardware_governance',"
        "'measurement_ingest','campaign_uncertainty','stage7_boundary'],"
        "'log_sha256': hashlib.sha256(open('{output}.log','rb').read())"
        ".hexdigest()}}, open('{output}','w'), indent=1)\""

rule package_checker:
    output: f"{WS}/package_checker.txt"
    shell:
        "{PY} package_consistency_check.py > {output} 2>&1 && "
        "grep -q 'RESULT: PASS' {output}"

rule manuscript_checker:
    output: f"{WS}/manuscript_checker.txt"
    shell:
        "{PY} manuscript_consistency_check.py > {output} 2>&1 && "
        "grep -q 'RESULT: PASS' {output}"

rule canonical_outputs:
    # generates a FRESH output set inside the workspace via the
    # authoritative runner-based full pipeline (identical code path to
    # qta_full_sim.py, parameterized to the workspace); the canonical
    # source tree is never written.
    output: f"{WS}/canonical_outputs.done.json"
    run:
        import subprocess, sys
        od = f"{WS}/outputs"
        r = subprocess.run([sys.executable, "snakemake_sim_entry.py", od],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
        files = sorted(os.listdir(od))
        Path(output[0]).write_text(json.dumps(
            {"n_outputs": len(files),
             "hashes": {f: _sha(os.path.join(od, f)) for f in files}},
            indent=1, sort_keys=True))

rule outputs_validated:
    input: f"{WS}/canonical_outputs.done.json"
    output: f"{WS}/outputs_validated.json"
    run:
        done = json.loads(Path(input[0]).read_text())
        same = diff = 0; mism = []
        for name, h in done["hashes"].items():
            if name in EXEMPT: continue
            if Path(name).exists() and _sha(name) == h: same += 1
            else: diff += 1; mism.append(name)
        rep = {"byte_identical": same, "mismatched": diff,
               "mismatch_names": mism}
        Path(output[0]).write_text(json.dumps(rep, indent=1,
                                              sort_keys=True))
        assert diff == 0, mism

rule gate_table_validated:
    input: "results_gate_table.csv"
    output: f"{WS}/gate_table_validated.json"
    run:
        import csv
        from collections import Counter
        rows = list(csv.DictReader(open(input[0])))
        dist = Counter(r["status"] for r in rows)
        assert dist == Counter({"CONDITIONAL": 47, "BLOCKED": 23,
                                "DERIVED_CHECK": 11, "UNKNOWN": 2}), dist
        Path(output[0]).write_text(json.dumps(
            {"n_gates": len(rows), "distribution": dict(dist),
             "scientific_PASS_count": 0}, indent=1, sort_keys=True))

rule roadmap_validated:
    output: f"{WS}/roadmap_validated.txt"
    shell:
        "{PY} stage6_preservation_check.py > {output} 2>&1 && "
        "grep -q 'RESULT: STAGE6_REQUIRED_INVARIANTS_PRESERVED' {output}"

rule manifest_verified:
    input: "final_manifest.json", "manifest_hash.txt"
    output: f"{WS}/manifest_verified.json"
    run:
        man = json.loads(Path(input[0]).read_text())
        stored = Path(input[1]).read_text().split("sha256:")[1].split()[0]
        calc = _sha(input[0])
        bad = [e["filename"] for e in man["files"]
               if not Path(e["filename"]).exists()
               or _sha(e["filename"]) != e["sha256"]]
        rep = {"entries": len(man["files"]), "mismatches": len(bad),
               "detached_hash_ok": stored.strip() == calc}
        Path(output[0]).write_text(json.dumps(rep, indent=1,
                                              sort_keys=True))
        assert not bad and rep["detached_hash_ok"]

rule clean_workspace:
    # removes ONLY the generated workspace; never authoritative sources
    shell: "rm -rf verification/snakemake"


# ===================== Stage-8 additive rules (HDF5 + RO-Crate) ==============
W8 = "verification/stage8"

rule s8_mapping_validate:
    input: "hdf5_output_mapping.json", "hdf5_schema.json"
    output: f"{W8}/mapping_validated.json"
    run:
        import json as _j
        m = _j.loads(Path(input[0]).read_text())
        sc = _j.loads(Path(input[1]).read_text())
        assert m["schema_version"] == "1.0.0" and m["n_governed"] == 88
        assert len(sc["datasets"]) == 88
        Path(output[0]).write_text(_j.dumps(
            {"mapping_sha": _sha(input[0]), "schema_sha": _sha(input[1]),
             "n_governed": 88}, indent=1, sort_keys=True))

rule s8_hdf5_build:
    input: "hdf5_output_mapping.json", "build_hdf5.py"
    output: f"{W8}/qta_scientific_results.h5"
    shell: "{PY} build_hdf5.py {output}"

rule s8_hdf5_equivalence:
    input: f"{W8}/qta_scientific_results.h5"
    output: f"{W8}/hdf5_equivalence.txt"
    shell:
        "{PY} validate_hdf5_equivalence.py {input} "
        "verification/stage8/hdf5_equivalence_report.json > {output} "
        "2>&1 && grep -q 'RESULT: EQUIVALENT' {output}"

rule s8_hdf5_rebuild_compare:
    input: f"{W8}/qta_scientific_results.h5"
    output: f"{W8}/hdf5_determinism.json"
    run:
        import json as _j, subprocess as _sp, sys as _sy
        _sp.run([PY, "build_hdf5.py",
                 f"{W8}/rebuild.h5"], check=True)
        a, b = _sha(input[0]), _sha(f"{W8}/rebuild.h5")
        Path(output[0]).write_text(_j.dumps(
            {"build_sha": a, "rebuild_sha": b,
             "binary_identity": a == b}, indent=1))
        assert a == b

rule s8_crate_build:
    # crate builds LAST: it references the equivalence report, so the
    # report must exist/settle first (build-ordering discipline)
    input: "ro_crate_tools.py", "qta_scientific_results.h5",
           f"{W8}/hdf5_equivalence.txt"
    output: f"{W8}/crate/ro-crate-metadata.json"
    shell: "{PY} ro_crate_tools.py {W8}/crate".replace("{W8}", W8)

rule s8_crate_validate:
    input: "ro-crate/ro-crate-metadata.json"
    output: f"{W8}/crate_validated.txt"
    shell:
        "{PY} ro_crate_tools.py validate > {output} 2>&1 && "
        "grep -q 'RESULT: VALID' {output}"

rule s8_crate_rebuild_compare:
    input: f"{W8}/crate/ro-crate-metadata.json",
           "ro-crate/ro-crate-metadata.json"
    output: f"{W8}/crate_determinism.json"
    run:
        import json as _j
        a, b = _sha(input[0]), _sha(input[1])
        Path(output[0]).write_text(_j.dumps(
            {"scratch_sha": a, "tree_sha": b, "identity": a == b},
            indent=1))
        assert a == b

rule s8_preservation:
    output: f"{W8}/preservation.txt"
    shell:
        "{PY} stage6_preservation_check.py > {output} 2>&1 && "
        "grep -q 'RESULT: STAGE6_REQUIRED_INVARIANTS_PRESERVED' {output}"

rule s8_report:
    input:
        f"{W8}/mapping_validated.json", f"{W8}/hdf5_equivalence.txt",
        f"{W8}/hdf5_determinism.json", f"{W8}/crate_validated.txt",
        f"{W8}/crate_determinism.json", f"{W8}/preservation.txt"
    output: f"{W8}/stage8_workflow_report.json"
    run:
        import json as _j
        Path(output[0]).write_text(_j.dumps(
            {"inputs": {Path(f).name: _sha(f) for f in input},
             "note": "software verification only; scientific gate PASS "
                     "remains zero"}, indent=1, sort_keys=True))

rule s8_full:
    input: f"{W8}/stage8_workflow_report.json"


# ============ Stage-10 additive rules (scientific-stack adapters) ===========
# Visualization interchange (ParaView/VTK, OpenUSD), a read-only retrieval
# index, the staged FEniCSx acceptance harness, selective-Rust parity, and the
# deferred FMI contract. Every rule writes ONLY under verification/stage10 and
# the closing rule proves the canonical tree was not touched. Software
# verification only; the scientific gate PASS count remains zero.
W10 = "verification/stage10"


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
        # adoption requires bit identity; anything less stays on NumPy
        for k in rep["kernels"]:
            if k.get("adopted"):
                assert k["bit_identical"] and k["max_ulp_difference"] == 0, k
            else:
                assert k["backend_in_force"] == "numpy", k

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

rule s10_report:
    input:
        f"{W10}/canonical_untouched.json", f"{W10}/viz_determinism.json",
        f"{W10}/viz/usd/qta_domain_usd_manifest.json",
        f"{W10}/rag/rag_index.json", f"{W10}/fem/fenicsx_acceptance.json",
        f"{W10}/rust/rust_kernel_status.json", f"{W10}/fmi/fmi_readiness.json",
        f"{W10}/tests_stage10.json",
    output: f"{W10}/stage10_stack_report.json"
    run:
        import csv as _csv
        from collections import Counter as _C
        dist = _C(r["status"] for r in
                  _csv.DictReader(open("results_gate_table.csv")))
        Path(output[0]).write_text(json.dumps(
            {"inputs": {Path(f).name: _sha(f) for f in input},
             "scientific_PASS_count": dist.get("PASS", 0),
             "gate_distribution": dict(dist),
             "note": "software verification only; the Stage-10 scientific "
                     "stack is additive and the gate PASS count remains "
                     "zero"}, indent=1, sort_keys=True))
        assert dist.get("PASS", 0) == 0

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
        f"{W10}/governed/out/governed_artifact.json",
        f"{W10}/governed/governed_run.json",
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

        run = gov.run(
            tool_id="stage10.emit_artifact",
            inputs={
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
            })

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

        # Authority records, if this history holds any, must be whole too.
        record_gaps = [e for e in index.audit_records() if not e.complete]
        assert not record_gaps, (
            "authority records with provenance gaps:\n"
            + "\n".join(f"  - {e.subject}: {g}"
                         for e in record_gaps for g in e.gaps))

        Path(output[1]).write_text(json.dumps({
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
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


rule s10_full:
    input:
        f"{W10}/stage10_stack_report.json",
        f"{W10}/governed/governed_run.json",


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
