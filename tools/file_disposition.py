"""Every file in the repository gets exactly one disposition, by rule.

WHY THIS IS A TOOL AND NOT A SPREADSHEET

The architecture-convergence directive requires every tracked file to receive
one of eight dispositions, with no miscellaneous bucket. A hand-kept CSV
satisfies that on the day it is written and drifts from the next commit on:
a file added later has no row, and nothing says so.

So the CSV is GENERATED from an explicit rule table (first match wins), each
rule carrying its rationale and its evidence, and generation FAILS when a
file matches no rule. There is no fallback rule, because a fallback is a
miscellaneous bucket with a better name. ``--check`` (the default) says
whether the committed CSV is what the rules produce for the repository as it
is now; ``tests/test_file_disposition.py`` runs it.

What a rule's evidence can and cannot establish is in
``ARCHITECTURE_CONVERGENCE_PLAN.md`` (dispositions) and
``DEPENDENCY_CUTOVER.md`` (how the import graph was measured).

Usage::

    python tools/file_disposition.py            # check
    python tools/file_disposition.py --write    # regenerate
"""
from __future__ import annotations

import collections
import csv
import fnmatch
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import subprocess  # noqa: E402

OUT = ROOT / "FILE_DISPOSITION.csv"
FIELDS = ("path", "disposition", "phase", "rationale", "evidence")

KAI, KAH, EXG, PLG, RWG, REG, RTH, DGR = (
    "KEEP_AS_IS", "KEEP_AND_HARDEN", "EXTRACT_GENERIC", "KEEP_AS_MODEL_PLUGIN",
    "REWRITE_GENERIC", "REGENERATE", "RETIRE_TO_HISTORY",
    "DELETE_GENERATED_AND_REBUILD")

PHASE = {KAI: "-", KAH: "1 (this tranche) / ongoing", EXG: "3", PLG: "4",
         RWG: "2-7 (see rationale)", REG: "every change; new set in 7",
         RTH: "5", DGR: "6-7"}

#: Hardware-ontology outputs inside the regeneration set.
HW_OUTPUTS = frozenset({
    "best_forecast_operating_point.json",
    "cryo_stack_3d_budget.csv",
    "design_component_registry.json",
    "design_decision_ledger.json",
    "design_interface_graph.json",
    "design_validation_report.csv",
    "engineering_fixes.csv",
    "experiment_falsification_map.csv",
    "integrated_layers_summary.json",
    "interlock_table.csv",
    "machine_fsm_campaign_trace.csv",
    "machine_fsm_diagram.mmd",
    "machine_fsm_interlocks.csv",
    "machine_fsm_lifecycle_trace.csv",
    "machine_fsm_states.csv",
    "machine_fsm_summary.json",
    "machine_fsm_transitions.csv",
    "mode_recovery_3d_timeline.csv",
    "nv_eligibility_3d.json",
    "nv_spin_gate_records.csv",
    "results_gate_table.csv",
    "validation_experiment_ranking.csv",
})

HW_OUTPUT_WHY = (
    "QTA hardware-ontology output (machine FSM / interlocks / gate table /"
    " apparatus design registry / validation-experiment ranking / "
    "mode-sequenced eligibility). Directive s4 retires these as framework "
    "authority; the file stays byte-gated until Phase 5 removes its "
    "producer, then moves to history.")

#: (glob, disposition, rationale, evidence). First match wins, so a specific
#: file precedes its directory's rule. There is deliberately no catch-all.
RULES = [
    ("qta_agent/events.py", KAH,
     "Canonical event log. Phase 1.1: single-pass verified tail (advance, "
     "read_verified_from); read_verified returns only the verified prefix.",
     "defect: advance() verified one read and returned a second"),
    ("qta_agent/store.py", KAH,
     "Authority projection. Phase 1.1 (load/_fold_new/load_from on one "
     "verified snapshot) + 1.4 (reducer identity bound into snapshots).",
     "defect: verify-then-read at load, _fold_new, load_from"),
    ("qta_agent/checkpoint.py", KAH,
     "Log-position checkpoint; verify_with now returns the verified tail "
     "it checked.",
     "defect: load_from re-read the tail verify_with checked"),
    ("qta_agent/projection.py", KAH,
     "NEW in Phase 1.4: reducer identity (projection_kind, "
     "reducer_id/version/digest, schema version).",
     "directive s8 invariant"),
    ("qta_agent/context.py", KAH,
     "Phase 1.2: summary provenance bound to real source bytes.",
     "defect: phantom summarizes_item + fabricated omission"),
    ("qta_agent/agents.py", KAH,
     "Phase 1.1: load() folds only the verified snapshot.",
     "verify-then-read at load"),
    ("qta_agent/audit.py", KAH,
     "Phase 1.1: from_log() indexes only the verified snapshot.",
     "verify-then-read (two further reads)"),
    ("qta_agent/capability.py", KAH,
     "Phase 1.1: load() folds only the verified snapshot.",
     "verify-then-read at load"),
    ("qta_agent/idempotency.py", KAH,
     "Phase 1.1: load() folds only the verified snapshot.",
     "verify-then-read at load"),
    ("qta_agent/memory.py", KAH,
     "Phase 1.1: load() folds only the verified snapshot.",
     "verify-then-read at load"),
    ("qta_agent/netauth.py", KAH,
     "Phase 1.1: load() folds only the verified snapshot.",
     "verify-then-read at load"),
    ("qta_agent/policy.py", KAH,
     "Phase 1.1: load() folds only the verified snapshot.",
     "verify-then-read at load"),
    ("qta_agent/reconstruct.py", KAH,
     "Independent second reader. Phase 1.1: replays the verified snapshot "
     "whose head it reports.",
     "verify-then-read in 3 reconstructors"),
    ("qta_agent/scheduler.py", KAH,
     "Job FSM. Phase 1.1: load/_fold_new on the verified snapshot.",
     "verify-then-read at load + fallback fold"),
    ("qta_agent/secrets.py", KAH,
     "Phase 1.1: load() folds only the verified snapshot.",
     "verify-then-read at load"),
    ("qta_agent/governed_stage10.py", RWG,
     "The only production consumer of the substrate; bound to the "
     "'Stage-10' QTA stack artefacts. Phase 2 generalizes it into governed"
     " ScientificModel runs (typed request -> ResultBundle -> evidence). "
     "Hardened in Phase 1: three unverified log reads now go through one "
     "verified pass.",
     "imports qta_multiphysics.stack.workspace; docstring: 'a real "
     "Stage-10 run'"),
    ("qta_agent/_stage10_tool.py", RWG,
     "Subprocess entry for governed Stage-10 artefact emission; becomes "
     "the generic governed model-run tool in Phase 2.",
     "paired with governed_stage10.py"),
    ("qta_agent/_stage10_index_tool.py", RWG,
     "Second governed tool (digest index); generalize with "
     "governed_stage10 in Phase 2.",
     "docstring names results_gate_table.csv as a possible input"),
    ("qta_agent/*.py", KAI,
     "Generic governed-agent substrate (actions/authority/canonical/eviden"
     "ce/execution/hostid/invalidation/readpath/safeio/separate_verify/tas"
     "ks/tools). No QTA-hardware import or data reference.",
     "import closure of qta_agent reaches no hardware module "
     "(p0/reach.json)"),
    ("qta_multiphysics/fields.py", EXG,
     "Generic field containers.",
     "directive s3A"),
    ("qta_multiphysics/grids.py", EXG,
     "Generic structured grids.",
     "directive s3A"),
    ("qta_multiphysics/numerics.py", EXG,
     "Finite-volume operators + finite/stability checks; generic numerics.",
     "directive s3A; 4 Mode-letter mentions are comments"),
    ("qta_multiphysics/units.py", EXG,
     "Unit handling.",
     "directive s3A"),
    ("qta_multiphysics/exports.py", EXG,
     "Deterministic CSV/JSON export helpers.",
     "directive s3A"),
    ("qta_multiphysics/mesh_3d.py", EXG,
     "3D structured mesh.",
     "used by thermal_3d_transient and tests"),
    ("qta_multiphysics/convergence_3d.py", EXG,
     "Convergence analysis.",
     "directive s3A"),
    ("qta_multiphysics/energy_accounting_3d.py", EXG,
     "Energy-conservation accounting.",
     "directive s3A"),
    ("qta_multiphysics/reduction_checks_3d.py", EXG,
     "3D->2D/1D reduction checks (differential verification).",
     "directive s3A/s16"),
    ("qta_multiphysics/verification.py", EXG,
     "Numerical self-consistency checks -> VerificationResult producers "
     "(Phase 2/3).",
     "docstring: numerical, not physical validation"),
    ("qta_multiphysics/verification_3d.py", EXG,
     "Numerical verification for the 3D layer.",
     "directive s3A"),
    ("qta_multiphysics/uncertainty.py", RWG,
     "Monte Carlo propagation; engine is generic but the sample set is the"
     " QTA parameter registry and gate responses. Rewrite over "
     "ScientificModel (directive s12).",
     "8 Mode-letter hits; evaluates QTA gate responses"),
    ("qta_multiphysics/config.py", RWG,
     "Validate-on-construction SI config is generic; defaults are the QTA "
     "apparatus (10 mK fridge, fs laser, diamond NV). Split generic schema"
     " from the QTA reference parameter set.",
     "docstring"),
    ("qta_multiphysics/__init__.py", RWG,
     "Package entry exporting run_all (QTA orchestration). Becomes a "
     "plugin index.",
     "exports runner.run_all"),
    ("qta_multiphysics/thermal_1d.py", PLG,
     "Thermal conduction 1D. Phase 2 proving case for ScientificModel.",
     "imports only "
     "config/fields/grids/laser_source/material_models/numerics"),
    ("qta_multiphysics/thermal_2d_axisymmetric.py", PLG,
     "Thermal conduction 2D axisymmetric.",
     "model family: thermal"),
    ("qta_multiphysics/thermal_3d_transient.py", PLG,
     "Thermal conduction 3D transient.",
     "model family: thermal"),
    ("qta_multiphysics/boundaries_3d.py", PLG,
     "Boundary physics for the 3D thermal model.",
     "model family: thermal"),
    ("qta_multiphysics/laser_source.py", PLG,
     "Optical energy-deposition source model.",
     "model family: optical/source"),
    ("qta_multiphysics/laser_source_3d.py", PLG,
     "3D optical deposition.",
     "model family: optical/source"),
    ("qta_multiphysics/optical_absorption*.py", PLG,
     "Optical absorption profiles.",
     "model family: optical/source"),
    ("qta_multiphysics/surface_coverage*.py", PLG,
     "Surface adsorption/desorption kinetics.",
     "model family: surface processes; 3D variant carries Mode-phase "
     "windows to decouple in Phase 4"),
    ("qta_multiphysics/gas_transport_1d.py", PLG,
     "Gas transport 1D (Mode-letter phase labels to remove in Phase 4).",
     "model family: gas transport; 5 Mode-letter hits"),
    ("qta_multiphysics/gas_transport_2d.py", PLG,
     "Molecular-beam exposure map (reduced model).",
     "model family: gas transport"),
    ("qta_multiphysics/species_transport_3d.py", PLG,
     "Knudsen/molecular-flow regime model; per-mode species rows to "
     "decouple in Phase 4.",
     "15 Mode-letter hits"),
    ("qta_multiphysics/radiation_paths.py", PLG,
     "Radiative loads.",
     "model family: radiation"),
    ("qta_multiphysics/radiation_paths_3d.py", PLG,
     "Radiative path budget.",
     "model family: radiation"),
    ("qta_multiphysics/vibration_transfer.py", PLG,
     "Vibration transfer chain.",
     "model family: vibration"),
    ("qta_multiphysics/vibration_paths_3d.py", PLG,
     "Vibration path budget.",
     "model family: vibration"),
    ("qta_multiphysics/microwave_heating_*.py", PLG,
     "Microwave dissipation model.",
     "model family: source/heating"),
    ("qta_multiphysics/material_models.py", PLG,
     "Temperature-dependent material constitutive models (Mode-labelled "
     "gas temperatures to decouple).",
     "model family: materials; 12 Mode-letter hits"),
    ("qta_multiphysics/materials_3d.py", PLG,
     "Material properties for the 3D layer.",
     "model family: materials"),
    ("qta_multiphysics/heat_switch_3d.py", PLG,
     "Superconducting heat-switch lumped conductance: generic cryogenic "
     "component physics.",
     "directive s3B 'generic cryogenic component physics'"),
    ("qta_multiphysics/cryopanel_dynamics_3d.py", PLG,
     "Cryopanel adsorption inventory: generic cryogenic physics; B->C->D "
     "phase windows to decouple in Phase 4.",
     "8 Mode-letter hits"),
    ("qta_multiphysics/nv_spin/runner.py", RWG,
     "Enforces Mode C readiness before Mode D (machine-mode semantics) and"
     " orchestrates outputs. Rewrite as a plugin runner.",
     "11 measured_in_this_system + 11 Mode-letter hits"),
    ("qta_multiphysics/nv_spin/*.py", PLG,
     "NV S=1 spin dynamics (Hamiltonian, sequences, OU noise, config).",
     "model family: spin"),
    ("qta_multiphysics/coupled_mode_solver.py", RWG,
     "Sequenced multi-phase coupled solve with state hand-off (generic) "
     "hard-wired to Mode B->C->D. Rewrite as a declared phase schedule "
     "over plugins.",
     "23 Mode-letter hits"),
    ("qta_multiphysics/coupling_ledger_3d.py", RWG,
     "Coupling-arrow ledger with honesty statuses: generic concept, QTA "
     "channel list.",
     "20 Mode-letter hits"),
    ("qta_multiphysics/campaign_state_3d.py", RWG,
     "Adaptive-campaign carried state; walks the machine FSM. Rewrite over"
     " the task/scheduler FSMs. Holds deferred D-2026-69 (energy-ledger "
     "summation floor).",
     "imports machine_fsm path via runner_3d; 19 Mode-letter hits"),
    ("qta_multiphysics/campaign_uncertainty_3d.py", RWG,
     "Campaign-level UQ; generic engine over ScientificModel (directive "
     "s12).",
     "Mode-letter + measured flag"),
    ("qta_multiphysics/falsification_3d.py", RWG,
     "Falsification conditions: generic concept, QTA canonical bounds.",
     "11 Mode-letter hits"),
    ("qta_multiphysics/measurement_ingest_3d.py", RWG,
     "Replace with the generic Observation boundary "
     "(RAW/PROCESSED/SYNTHETIC/SIMULATION/DERIVED/CALIBRATED) -- directive"
     " s15.",
     "data_class 'SYNTHETIC' only; 21 Mode-letter hits"),
    ("qta_multiphysics/species_accounting_3d.py", RWG,
     "Mass accounting is generic; the per-mode species policy is QTA "
     "ontology.",
     "23 Mode-letter hits; 2 constant self-check asserts"),
    ("qta_multiphysics/sources_3d.py", RWG,
     "Auxiliary volumetric source channels keyed to Mode D.",
     "docstring: 'Mode D only'"),
    ("qta_multiphysics/sensitivity_3d.py", RWG,
     "One-at-a-time sensitivity on a QTA response; method generic "
     "(directive s12/s13).",
     "docstring"),
    ("qta_multiphysics/provenance_3d.py", RWG,
     "Model/parameter provenance ledgers -> model-registry provenance "
     "(directive s23).",
     "docstring"),
    ("qta_multiphysics/runner.py", RWG,
     "QTA orchestration (run_all) returning gate specs; replaced by "
     "generic workflow (Phase 6).",
     "14 measured_in_this_system hits"),
    ("qta_multiphysics/runner_3d.py", RWG,
     "QTA 3D output orchestrator; names machine_fsm_* outputs.",
     "imports machine_fsm, mode_sequence_3d, state_machine_3d"),
    ("qta_multiphysics/metrics.py", RWG,
     "Assembles QTA gate specs (retired); its DERIVED_CHECK numerical "
     "checks move to VerificationResult.",
     "docstring: 'Assemble the multiphysics gate specifications'"),
    ("qta_multiphysics/machine_fsm.py", RTH,
     "Physical machine FSM (valves/shutter/switch, Mode A/B/C/D).",
     "directive s4/s5: retire only the physical machine FSM"),
    ("qta_multiphysics/hardware_governance_3d.py", RTH,
     "Physical hardware governance / reviewer registry.",
     "directive s4"),
    ("qta_multiphysics/design/*.py", RTH,
     "Physical component design registry / interface graph / decision "
     "ledger; reads BOM.csv and interlock_table.csv.",
     "directive s4; reached from deep_expdesign.design_space (see "
     "DEPENDENCY_CUTOVER.md)"),
    ("qta_multiphysics/mode_sequence_3d.py", RTH,
     "Fixed Mode A/B/C/D sequencing of the 3D thermal core. The thermal "
     "solve it drives stays in thermal_3d_transient (plugin); the "
     "phase-schedule concept is re-expressed by the rewritten coupled "
     "solver.",
     "76 Mode-letter hits"),
    ("qta_multiphysics/state_machine_3d.py", RTH,
     "Mode -> device-state map (shutter, baffle, laser, heat switch).",
     "docstring: 'Mode-driven device state machine'"),
    ("qta_multiphysics/stage7_boundary_models.py", RTH,
     "Pydantic validation of QTA identifiers/matrix-update requests. The "
     "Pydantic-at-boundary pattern is re-established by Phase 2 schemas.",
     "validates validation_matrix update requests"),
    ("qta_multiphysics/integrated_layers.py", RTH,
     "Feeds new forecast gate records into the QTA gate system.",
     "docstring"),
    ("qta_multiphysics/nv_eligibility_3d.py", RTH,
     "NV sensing eligibility from Mode-D machine thresholds.",
     "docstring"),
    ("qta_multiphysics/future_3d.py", RTH,
     "Status module for the QTA 3D layer.",
     "docstring"),
    ("qta_multiphysics/cryo_stack_3d.py", RTH,
     "Registry of the QTA cryogenic architecture; its numeric loads are "
     "REUSED from radiation_paths (plugin), so no physics is lost. Verify "
     "no unique equation before retirement.",
     "docstring: 'loads are REUSED from the existing canonical modules'"),
    ("qta_multiphysics/expdesign/engine.py", EXG,
     "Direct nested-MC EIG engine.",
     "directive s14"),
    ("qta_multiphysics/expdesign/model.py", RWG,
     "Parses FIRST_VALIDATION_EXPERIMENTS.md + kill_gate_ranking.csv; "
     "rewrite over Observation/ScientificModel.",
     "names kill_gate_ranking.csv"),
    ("qta_multiphysics/expdesign/runner.py", RWG,
     "Writes QTA validation-experiment rankings.",
     "names validation_experiment_ranking.csv"),
    ("qta_multiphysics/expdesign/__init__.py", RWG,
     "Package docstring binds the engine to QTA candidate experiments.",
     "docstring"),
    ("qta_multiphysics/deep_expdesign/design_space.py", RWG,
     "Design space built from EXP-A..F + machine FSM constraints; imports "
     "qta_multiphysics.design (retired).",
     "import path to design.registry (reach.json)"),
    ("qta_multiphysics/deep_expdesign/parameter_space.py", RWG,
     "Latent space assembled from QTA registries (directive s14).",
     "docstring"),
    ("qta_multiphysics/deep_expdesign/simulator_adapter.py", RWG,
     "Adapter around QTA forward models -> generic ScientificModel adapter"
     " (directive s14).",
     "docstring"),
    ("qta_multiphysics/deep_expdesign/likelihood_model.py", RWG,
     "Mode-D sensing design distribution baked into the generative model.",
     "3 Mode-letter hits"),
    ("qta_multiphysics/deep_expdesign/policy.py", RWG,
     "Adaptive policy combines EIG with QTA gate criticality.",
     "docstring"),
    ("qta_multiphysics/deep_expdesign/runner.py", RWG,
     "QTA deep-layer orchestrator.",
     "machine FSM mentions"),
    ("qta_multiphysics/deep_expdesign/readiness.py", RWG,
     "Fail-closed readiness FSM is generic; stamps "
     "measured_in_this_system.",
     "1 measured flag"),
    ("qta_multiphysics/deep_expdesign/schemas.py", RWG,
     "Typed schemas are generic; carry QTA claim-boundary fields.",
     "4 measured flags"),
    ("qta_multiphysics/deep_expdesign/__init__.py", RWG,
     "Package docstring binds the layer to QTA.",
     "docstring"),
    ("qta_multiphysics/deep_expdesign/*.py", EXG,
     "Generic SBI/UQ machinery (calibration, inference, posterior model, "
     "transforms, OOD, trainer, EIG surrogate, dataset, validation, "
     "config, io).",
     "directive s14 list"),
    ("qta_multiphysics/stack/rag_index.py", KAH,
     "Read-only RAG over governed documents; retained (directive s17).",
     "digest-bound corpus allowlist"),
    ("qta_multiphysics/stack/workspace.py", EXG,
     "Output-location guard + deterministic writers; imported by the agent"
     " core.",
     "reached from qta_agent.governed_stage10"),
    ("qta_multiphysics/stack/registry.py", EXG,
     "Fail-closed Pydantic validation of stack.json.",
     "directive s2"),
    ("qta_multiphysics/stack/fem_fenicsx.py", EXG,
     "FEniCSx as independent verifier with executable acceptance contract "
     "(directive s16).",
     "docstring"),
    ("qta_multiphysics/stack/rust_kernel.py", EXG,
     "Selective Rust with bit-parity admission.",
     "directive s2"),
    ("qta_multiphysics/stack/vtk_export.py", EXG,
     "Deterministic VTK export (inspection surface).",
     "directive s2"),
    ("qta_multiphysics/stack/usd_export.py", EXG,
     "Deterministic OpenUSD export.",
     "directive s2"),
    ("qta_multiphysics/stack/sensitivity_salib.py", RWG,
     "SALib adapter over the QTA thermal ROM; refactor to consume "
     "ScientificModel (directive s13).",
     "3 Mode-letter hits"),
    ("qta_multiphysics/stack/mdao_openmdao.py", RWG,
     "OpenMDAO adapter over the QTA thermal ROM; refactor to consume "
     "ScientificModel (directive s13).",
     "docstring"),
    ("qta_multiphysics/stack/fmi_contract.py", RWG,
     "Deferred FMI contract written against the machine FSM.",
     "5 machine-FSM hits"),
    ("qta_multiphysics/stack/__init__.py", RWG,
     "Stage-10 package framing.",
     "docstring"),
    ("qta_full_sim.py", RTH,
     "Monolithic QTA orchestrator + gate table + machine interlocks "
     "(IL-01..IL-10). Not rewritten: first mine it -- extract any "
     "scientifically unique equation into the generic or model module "
     "it belongs to, each with a regression test pinning today's numbers "
     "-- and only then retire the orchestrator to history once the "
     "generic workflow (Phase 6) replaces it. Not deleted before that. "
     "Interlock asserts made explicit in Phase 1.3.",
     "168 Mode-letter hits; 0 asserts (15 at the baseline, D-2026-73)"),
    ("qta_sim_stages.py", RWG,
     "Stage-level checkpoint/restart driver around qta_full_sim; "
     "resilience pattern kept.",
     "docstring"),
    ("snakemake_sim_entry.py", RWG,
     "Snakemake wrapper around qta_full_sim (Phase 6).",
     "docstring"),
    ("Snakefile", RWG,
     "Workflow rewritten around validate -> run -> invariants -> "
     "independent verifier -> evidence (Phase 6).",
     "directive s25 Phase 6"),
    ("package_consistency_check.py", RWG,
     "Independent regeneration verifier. Byte-regeneration and fail-closed"
     " structure are kept; the 83-gate/PASS=0/BOM/manuscript checks retire"
     " (directive s4).",
     "106 Mode-letter, 69 gate, 38 BOM, 28 measured-flag hits"),
    ("manuscript_consistency_check.py", RTH,
     "QTA manuscript consistency is not a release requirement (directive "
     "s4).",
     "docstring"),
    ("build_stage6_playbooks.py", RTH,
     "Renders apparatus experiment playbooks.",
     "directive s4"),
    ("build_stage6_registries.py", RTH,
     "Builds QTA validation registries.",
     "directive s4"),
    ("stage6_preservation_check.py", RTH,
     "Asserts PASS=0 across Stage-6 registries.",
     "directive s4"),
    ("build_hdf5.py", RWG,
     "Deterministic HDF5 builder kept; the 88-output payload and "
     "PASS-count attribute retire (directive s18/Phase 7).",
     "names results_gate_table.csv; scientific_gate_PASS_count"),
    ("build_hdf5_mapping.py", RWG,
     "Output classification + unit mapping kept; the '88 governed outputs'"
     " contract retires.",
     "docstring"),
    ("validate_hdf5_equivalence.py", RWG,
     "HDF5<->source exact-equivalence validator kept; payload list "
     "retires.",
     "names results_gate_table.csv"),
    ("ro_crate_tools.py", RWG,
     "Deterministic RO-Crate builder kept; hard-coded QTA experiment "
     "registries retire.",
     "names experiment_*.json, validation_matrix.csv"),
    ("generate_manifest.py", RWG,
     "SHA-256 manifest kept; DERIVED_STATE_FIELDS (total_gates, PASS, ...)"
     " retire.",
     "DERIVED_STATE_FIELDS"),
    ("build_release_artifacts.py", RWG,
     "Release/SBOM/provenance generator kept; 'qta_claims' PASS-count "
     "block retires.",
     "scientific_gate_PASS_count"),
    ("verify_release.py", RWG,
     "Hostile/offline release verifier kept; QTA payload expectation "
     "retires.",
     "names results_gate_table.csv"),
    ("release_trust.py", KAI,
     "Release trust policy authority; no QTA payload knowledge.",
     "no hardware marker; reach.json"),
    ("release_revision_gate.py", KAI,
     "Revision-binding gate.",
     "no hardware marker"),
    ("finalize_release_signing.py", KAI,
     "Narrow post-sign finalizer.",
     "no hardware marker"),
    ("conftest.py", KAI,
     "Puts repo root on sys.path for every test module.",
     "D-2026-63"),
    ("analysis/collect_container_3d.py", RWG,
     "Container regeneration diagnostic over the QTA 3D output set.",
     "docstring"),
    ("tools/file_disposition.py", KAH,
     "NEW in Phase 0: generates and checks FILE_DISPOSITION.csv; fails on "
     "a file no rule names.",
     ""),
    ("tools/mutation_matrix.py", KAI,
     "Mutation harness (directive s20).",
     ""),
    ("tools/verified_read_guard.py", KAH,
     "NEW in the Phase-1 closure: static half of the verify-then-read "
     "guard over every production file (D-2026-76).",
     "20 sites reported on the pre-D-2026-70 tree"),
    ("tools/dependency_declarations.py", KAH,
     "NEW in the Phase-1 closure: every direct import declared in a group "
     "that ships it; derives the dependency inventory (D-2026-74/77).",
     "reports the two D-2026-74 sites on the replaced pyproject"),
    ("tools/mutation_shards.py", KAH,
     "NEW in the Phase-1 closure: deterministic mutation shards, the "
     "generated workflow matrix and its check, the shard runner.",
     "46 serial matrices took 4h27m in one job (run 36094744039)"),
    ("tools/generated_mutations.py", KAI,
     "Generated mutation operators (directive s20).",
     ""),
    ("tools/fuzz_substrate.py", KAI,
     "Substrate fuzz harness (directive s20).",
     ""),
    ("tools/independent_verify.py", KAI,
     "Subprocess second reader.",
     ""),
    ("tools/model_check.py", KAI,
     "Bounded exhaustive FSM path checking.",
     ""),
    ("tools/audit_log.py", KAI,
     "Read-only audit CLI.",
     ""),
    ("tools/identity_inventory.py", KAI,
     "Identity-bearing field inventory.",
     ""),
    ("tools/test_isolation.py", KAI,
     "Per-module collection isolation.",
     ""),
    ("tools/repo_scope.py", KAI,
     "Single definition of 'a file in this repository'.",
     ""),
    ("tools/performance_baseline.py", KAI,
     "Scaling-guard drift record.",
     ""),
    ("tools/workflow_contract.py", KAI,
     "Hosted-workflow contract checked locally.",
     ""),
    ("tools/corpus_allowlist.py", KAI,
     "RAG corpus allowlist generator/checker.",
     ""),
    ("tools/cross_env_semantics.py", KAH,
     "Reproducibility vs portability instrument (directive s19); column "
     "list is QTA outputs today.",
     "D-2026-6x"),
    ("tools/blas_kernel_sensitivity.py", KAH,
     "BLAS-kernel dependence diagnostic (directive s19).",
     "R59"),
    ("tools/resolution_inventory.py", KAH,
     "Method-resolution inventory; holds deferred D-2026-69 "
     "(artefact-as-method proxy).",
     "D-2026-66/69"),
    ("tools/unit_inventory.py", KAH,
     "Reviewed dimensions for every governed column.",
     ""),
    ("tools/clip_provenance.py", RWG,
     "Clip provenance over the QTA runners; imports "
     "integrated_layers/runner_3d.",
     "reach.json tools closure"),
    ("tools/claims_enforcement.py", RWG,
     "Claims-boundary enforcement coverage; the boundary text itself is "
     "rewritten generically.",
     "reads CLAIMS_BOUNDARY.md"),
    ("tools/completion_matrix.py", RWG,
     "Completion-matrix validator; rows tied to the 83-gate model retire "
     "(directive s4).",
     ""),
    ("tools/verify.sh", KAI,
     "Local verification entry script.",
     ""),
    ("tools/mutations/hardware_governance.json", RTH,
     "Hardware-governance mutations retire with their subject (directive "
     "s20).",
     "targets hardware_governance_3d.py"),
    ("tools/mutations/canonical_output_set.json", RWG,
     "Targets the QTA canonical-output set in "
     "package_consistency_check.py.",
     ""),
    ("tools/mutations/output_resolution.json", KAH,
     "Resolution discipline mutations (numerics + PCC).",
     ""),
    ("tools/mutations/solver_failclosed.json", KAH,
     "Scientific fail-closed mutations; the model for directive s20 "
     "numerical mutations.",
     ""),
    ("tools/mutations/agent_checkpoint.json", KAH,
     "Extended in Phase 1 (tail coherence + reducer identity).",
     ""),
    ("tools/mutations/agent_memory_context.json", KAH,
     "Extended in Phase 1.2 (summary provenance).",
     ""),
    ("tools/mutations/agent_incremental.json", KAH,
     "Extended in Phase 1.1 (advance single pass).",
     ""),
    ("tools/mutations/agent_snapshot_coherence.json", KAH,
     "NEW in Phase 1.1: verify-then-read regression operators.",
     ""),
    ("tools/mutations/verified_read_guard.json", KAH,
     "NEW in the Phase-1 closure: operators over both halves of the "
     "verify-then-read guard (D-2026-76).",
     ""),
    ("tools/mutations/dependency_declarations.json", KAH,
     "NEW in the Phase-1 closure: operators over the declaration rules and "
     "the inventory derivation.",
     ""),
    ("tools/mutations/mutation_shards.json", KAH,
     "NEW in the Phase-1 closure: operators over coverage, determinism, the "
     "aggregate's meaning and the shard runner.",
     ""),
    ("tools/mutations/enforcement_asserts.json", KAH,
     "NEW in Phase 1.3: interlock-as-assert, guard scope and count-pin "
     "operators.",
     ""),
    ("tools/mutations/agent_substrate.json", KAH,
     "M16/M20/M30 re-anchored to the single verified read (Phase 1.1).",
     ""),
    ("tools/mutations/agent_audit.json", KAH,
     "A1/Q1 re-anchored to the single verified read (Phase 1.1).",
     ""),
    ("tools/mutations/agent_execution.json", KAH,
     "L3 re-anchored to the single verified read (Phase 1.1).",
     ""),
    ("tools/mutations/*.json", KAI,
     "Mutation spec over a retained subject (agent substrate, tools, "
     "release, RAG).",
     "targets listed in the spec"),
    ("tests/test_agent_context.py", KAH,
     "Phantom-source test rewritten (it pinned the defect) + provenance "
     "tests (Phase 1.2).",
     ""),
    ("tests/test_agent_checkpoint.py", KAH,
     "Reducer-identity tests added (Phase 1.4).",
     ""),
    ("tests/test_optimized_mode_invariants.py", KAH,
     "Assert guard extended to release/root modules and pinned by count "
     "(Phase 1.3).",
     "docstring claimed a count pin the code did not implement"),
    ("tests/test_agent_snapshot_coherence.py", KAH,
     "NEW: adversarial verify-then-read tests over every projection (Phase"
     " 1.1).",
     ""),
    ("tests/test_agent_substrate_isolation.py", KAH,
     "Layering and IO_LAYER extended for qta_agent/projection.py; new test"
     " importer allowed (Phase 1).",
     ""),
    ("tests/test_verified_read_guard.py", KAH,
     "NEW: every rule of the verify-then-read guard on a breaking source "
     "and a control; the runtime refusal (D-2026-76).",
     ""),
    ("tests/test_dependency_declarations.py", KAH,
     "NEW: declarations against imports, with the replaced pyproject as "
     "the anti-vacuity control (D-2026-74).",
     ""),
    ("tests/test_mutation_shards.py", KAH,
     "NEW: sharding loses no spec; each workflow rule broken in turn.",
     ""),
    ("tests/test_agent_*.py", KAI,
     "Agent-substrate trust invariants (directive s26: preserve).",
     ""),
    ("tests/test_file_disposition.py", KAH,
     "NEW in Phase 0: every repository file has exactly one disposition, "
     "and the CSV is the generator's output.",
     ""),
    ("tests/fuzz_corpus/*", KAI,
     "Fuzz crash corpus (directive s20: retain).",
     "replayed by tests/test_agent_fuzz.py"),
    ("tests/hangguard.py", KAI,
     "Shared wall-clock bound.",
     ""),
    ("tests/hw_reviewer_fixtures.py", RTH,
     "Hardware reviewer rosters.",
     "imports hardware_governance_3d"),
    ("tests/test_hardware_governance.py", RTH,
     "Protects retired hardware-governance invariants; retire deliberately"
     " with the module (directive s26).",
     ""),
    ("tests/test_review_binding.py", RTH,
     "Hardware review binding.",
     "imports hardware_governance_3d"),
    ("tests/test_stage6_roadmap.py", RTH,
     "Hardware validation roadmap.",
     ""),
    ("tests/test_machine_fsm.py", RTH,
     "Physical machine FSM.",
     ""),
    ("tests/test_design.py", RTH,
     "Apparatus design registry.",
     ""),
    ("tests/test_record_parsers.py", RTH,
     "Gate-reference/mode parsers of the design registry.",
     ""),
    ("tests/test_bom_status_claim.py", RTH,
     "BOM claims (BOM completeness retires, directive s4).",
     ""),
    ("tests/test_stage7_boundary.py", RTH,
     "Pydantic models over QTA vocabularies.",
     ""),
    ("tests/test_integration.py", RTH,
     "integrated_layers gate feed.",
     ""),
    ("tests/test_mode_species_3d.py", RWG,
     "Mode/species safety: machine policy retires, species-mass invariants"
     " move to the rewritten accounting.",
     ""),
    ("tests/test_authority_invariants.py", RWG,
     "Authority invariants over machine FSM + qta_full_sim; keep the "
     "non-hardware ones.",
     ""),
    ("tests/test_single_source_of_truth.py", RWG,
     "Single-definition rule is generic; subject list is QTA.",
     ""),
    ("tests/test_authority_promotion_barrier.py", KAH,
     "Nothing descriptive/statistical/learned becomes authority -- a "
     "retained trust invariant.",
     ""),
    ("tests/test_authority_consistency.py", KAH,
     "authorities.json agrees with reality (retained).",
     ""),
    ("tests/test_multiphysics_core.py", PLG,
     "Thermal-1D numerical checks; Phase 2 proving-case tests.",
     ""),
    ("tests/test_thermal_3d.py", PLG,
     "3D thermal physics invariants.",
     ""),
    ("tests/test_lateral_boundary.py", PLG,
     "Boundary-value-problem identity across reductions.",
     ""),
    ("tests/test_material_floors.py", PLG,
     "Material floors.",
     ""),
    ("tests/test_nv_spin.py", PLG,
     "NV spin physics.",
     ""),
    ("tests/test_radiation_contract.py", PLG,
     "Radiation producer/consumer contract.",
     ""),
    ("tests/test_hotspot_ranking_determinism.py", PLG,
     "Deterministic hotspot ranking.",
     ""),
    ("tests/test_parameter_semantics.py", PLG,
     "Dimensional/sign semantics of parameters (mode letters to drop).",
     ""),
    ("tests/test_solver_failclosed.py", KAH,
     "A failed solve grants nothing (directive s20 model).",
     ""),
    ("tests/test_output_resolution.py", KAH,
     "Outputs state their method resolution.",
     ""),
    ("tests/test_coupled_3d.py", RWG,
     "Coupled-3D tests follow the coupled solver rewrite.",
     ""),
    ("tests/test_campaign_3d.py", RWG,
     "Campaign continuity tests follow the campaign rewrite.",
     ""),
    ("tests/test_campaign_uncertainty.py", RWG,
     "Campaign UQ determinism tests follow the UQ rewrite.",
     ""),
    ("tests/test_measurement_ingest.py", RWG,
     "Rewritten for the generic Observation boundary.",
     ""),
    ("tests/test_expdesign.py", RWG,
     "EIG tests; QTA candidate parsing retires.",
     ""),
    ("tests/test_deep_expdesign.py", RWG,
     "Deep layer foundation tests.",
     ""),
    ("tests/test_deep_expdesign_stage2.py", RWG,
     "Deep layer training/posterior tests.",
     ""),
    ("tests/test_deep_authority.py", KAH,
     "Deep layer trust boundary (retained invariant).",
     ""),
    ("tests/test_stage10_stack.py", RWG,
     "Stack adapter tests follow the adapter rewrite.",
     ""),
    ("tests/test_export_schema.py", EXG,
     "Export never drops governed fields.",
     ""),
    ("tests/test_csv_schema_governance.py", EXG,
     "Stable declared headers.",
     ""),
    ("tests/test_cross_env_semantics.py", KAH,
     "Reproducibility vs portability instrument.",
     ""),
    ("tests/test_resolution_inventory.py", KAH,
     "Resolution inventory.",
     ""),
    ("tests/test_unit_inventory.py", KAH,
     "Unit inventory.",
     ""),
    ("tests/test_identity_inventory.py", KAI,
     "Identity inventory.",
     ""),
    ("tests/test_mutation_harness.py", KAI,
     "Harness tested as an instrument.",
     ""),
    ("tests/test_generated_mutations.py", KAI,
     "Generated operators.",
     ""),
    ("tests/test_model_check.py", KAI,
     "Model checker.",
     ""),
    ("tests/test_test_isolation.py", KAI,
     "Collection isolation.",
     ""),
    ("tests/test_repo_contract.py", KAI,
     "Repository file-set contract.",
     ""),
    ("tests/test_claims_enforcement.py", RWG,
     "Follows the claims-boundary rewrite.",
     ""),
    ("tests/test_completion_matrix.py", RWG,
     "Follows the completion-matrix rewrite.",
     ""),
    ("tests/test_manifest_completeness.py", KAH,
     "Manifest completeness with an independent check (retained).",
     ""),
    ("tests/test_manifest_policy.py", RWG,
     "Coverage boundary tied to the QTA output set.",
     ""),
    ("tests/test_failed_gate_schema.py", RWG,
     "Failed-sample schema survives as generic failed-sample preservation "
     "(directive s12).",
     ""),
    ("tests/test_independent_gate_recomputation.py", RWG,
     "Independent recomputation pattern kept; QTA gates B4/D10b retire.",
     ""),
    ("tests/test_h2_pressure_authority.py", RWG,
     "Distinct-quantity rule kept; gate B4 framing retires.",
     ""),
    ("tests/test_checker_missing_outputs.py", RWG,
     "Follows package_consistency_check rewrite.",
     ""),
    ("tests/test_checker_runtime_contract.py", RWG,
     "Follows package_consistency_check rewrite.",
     ""),
    ("tests/test_container_diagnostic_fidelity.py", RWG,
     "Follows analysis/collect_container_3d rewrite.",
     ""),
    ("tests/test_container_manifest_coverage.py", KAI,
     "Container carries every governed file.",
     ""),
    ("tests/test_stage7_5_resilience.py", RWG,
     "Checkpoint/restart driver around qta_full_sim.",
     ""),
    ("tests/test_stage8_data_provenance.py", RWG,
     "HDF5/RO-Crate boundary over the QTA payload.",
     ""),
    ("tests/test_stage9_release.py", KAI,
     "Consumer release verification on a fixture release.",
     ""),
    ("tests/test_release_trust_enforcement.py", KAI,
     "Release trust boundary (directive s18).",
     ""),
    ("tests/test_release_workflow_contract.py", KAI,
     "Release workflow executable.",
     "imports yaml (undeclared, see DEPENDENCY_CUTOVER.md)"),
    ("tests/test_bootstrap_workflow_contract.py", KAI,
     "Bootstrap workflows are not trust shortcuts.",
     "imports yaml (undeclared)"),
    ("tests/test_candidate_structure.py", KAI,
     "Hostile bundle structure.",
     ""),
    ("tests/test_hostile_input_no_traceback.py", KAI,
     "Classified refusals end to end.",
     ""),
    ("tests/test_hostile_trust_policy.py", KAI,
     "Bundled policy is attacker bytes.",
     ""),
    ("tests/test_digest_trust_root.py", KAI,
     "Digest trust root.",
     ""),
    ("tests/test_external_trust_root.py", KAI,
     "External trust root.",
     ""),
    ("tests/test_online_success_path.py", KAI,
     "Online success path.",
     ""),
    ("tests/test_signing_finalizer.py", KAI,
     "Signing finalizer.",
     ""),
    ("ARCHITECTURE_CONVERGENCE_PLAN.md", KAH,
     "NEW Phase 0 artefact.",
     ""),
    ("FILE_DISPOSITION.csv", KAH,
     "NEW Phase 0 artefact (this file).",
     ""),
    ("DEPENDENCY_CUTOVER.md", KAH,
     "NEW Phase 0 artefact.",
     ""),
    ("AGENT_SUBSTRATE.md", KAH,
     "Substrate design record; updated with Phase 1 changes.",
     ""),
    ("AUTHORITIES.md", KAH,
     "Enforcement-point registry (human half).",
     ""),
    ("authorities.json", KAH,
     "Enforcement-point registry (machine half).",
     ""),
    ("docs/DEFECT_LEDGER.md", KAH,
     "Defect history preserved, not sanitized (directive s21).",
     ""),
    ("docs/SESSION_REPORT.md", KAH,
     "Session evidence record.",
     ""),
    ("docs/R59_CROSS_ENVIRONMENT_ANALYSIS.md", KAI,
     "Cross-environment lessons (directive s19).",
     ""),
    ("docs/CORPUS_RECOVERY_TRIAGE.md", KAI,
     "Corpus recovery record.",
     ""),
    ("docs/completion_matrix.json", RWG,
     "Completion model tied to QTA rows; retire the 83-gate completion "
     "model (directive s4).",
     ""),
    ("docs/corpus_allowlist.json", REG,
     "Regenerated by tools/corpus_allowlist.py --write.",
     ""),
    ("docs/*.json", REG,
     "Tool-generated inventories/baselines "
     "(identity/unit/resolution/performance/clip/blas).",
     "written by tools/*.py"),
    ("CLAIMS_BOUNDARY.md", RWG,
     "Framework claims boundary: keep 'no experimental validation from "
     "simulation'; drop QTA machine claims.",
     ""),
    ("GLOSSARY.md", RWG,
     "Framework vocabulary.",
     ""),
    ("README.md", RWG,
     "Framework README (Phase 7).",
     ""),
    ("PHYSICS_INVENTORY.md", RWG,
     "Becomes the model-registry narrative (directive s23).",
     ""),
    ("NUMERICAL_METHODS.md", PLG,
     "Numerical-methods record of the reference model plugins.",
     ""),
    ("THERMAL_3D_L3.md", PLG,
     "Thermal-3D model documentation.",
     ""),
    ("HDF5_DATA_MODEL.md", RWG,
     "HDF5 data model without the 88-output payload.",
     ""),
    ("RO_CRATE_PROFILE.md", RWG,
     "RO-Crate profile without QTA registries.",
     ""),
    ("MANIFEST_BOUNDARY.md", RWG,
     "Manifest coverage boundary for the new release set.",
     ""),
    ("RELEASE_POLICY.md", RWG,
     "Release policy without QTA payload.",
     ""),
    ("EVIDENCE_CLOSURE.md", RWG,
     "Evidence closure narrative.",
     ""),
    ("RUNTIME_RESILIENCE.md", RWG,
     "Resilience narrative around qta_full_sim stages.",
     ""),
    ("TESTING.md", KAH,
     "Test discipline record.",
     ""),
    ("STACK.md", KAH,
     "Declared scientific stack (directive s2).",
     ""),
    ("stack.json", KAI,
     "Stack adoption registry.",
     ""),
    ("INSTALL.md", KAI,
     "Installation.",
     ""),
    ("RELEASE_TRUST_ENFORCEMENT.md", KAI,
     "Release trust table.",
     ""),
    ("SIGNING_BOOTSTRAP.md", KAI,
     "Signing bootstrap.",
     ""),
    ("SUPPLY_CHAIN_THREAT_MODEL.md", KAI,
     "Supply-chain threat model.",
     ""),
    ("container_verification.md", KAI,
     "Container verification record.",
     ""),
    ("container_verify.sh", KAI,
     "Container verification script.",
     ""),
    ("Dockerfile", KAI,
     "Reproducible container.",
     ""),
    (".dockerignore", KAI,
     "Container context.",
     ""),
    (".gitignore", KAI,
     "Ignore rules.",
     ""),
    ("pyproject.toml", KAH,
     "Declare PyYAML (imported directly by two test modules, today only "
     "transitive via snakemake).",
     "uv tree"),
    ("uv.lock", REG,
     "Lockfile regenerated by uv.",
     ""),
    ("requirements.txt", REG,
     "Derived pin list.",
     ""),
    ("raw_data_standard.md", RWG,
     "Becomes the Observation schema narrative (directive s15).",
     ""),
    ("synthetic_measurements_example.json", RWG,
     "Becomes a SYNTHETIC_OBSERVATION example.",
     ""),
    ("HARDWARE_GOVERNANCE.md", RTH,
     "Hardware governance.",
     "directive s4"),
    ("MACHINE_FSM.md", RTH,
     "Physical machine FSM.",
     "directive s4"),
    ("VALIDATION_PLAN.md", RTH,
     "Hardware validation roadmap.",
     "directive s4"),
    ("FIRST_VALIDATION_EXPERIMENTS.md", RTH,
     "Apparatus validation experiments.",
     "directive s4"),
    ("GATE_TABLE_README.md", RTH,
     "83-gate table.",
     "directive s4"),
    ("MANUSCRIPT_CHANGELOG.md", RTH,
     "Manuscript history.",
     "directive s4"),
    ("MANUSCRIPT_CONSISTENCY_REPORT.md", RTH,
     "Manuscript consistency.",
     "directive s4"),
    ("SOURCE_AUDIT_STATUS.md", RTH,
     "Manuscript source audit.",
     ""),
    ("qta_manuscript_v4.tex", RTH,
     "QTA manuscript.",
     ""),
    ("qta_manuscript_v4.pdf", RTH,
     "QTA manuscript.",
     ""),
    ("EXPERIMENT_PLAYBOOKS/*", RTH,
     "Playbooks for the abandoned apparatus.",
     "directive s4"),
    ("matrix_update_examples/*", RTH,
     "Validation-matrix update examples.",
     ""),
    ("validation_matrix_update_request.schema.json", RTH,
     "Validation-matrix update schema.",
     ""),
    ("QTA_full_history-6.bundle.txt", RTH,
     "A git bundle (binary) at the root; history belongs in attic/ (a copy"
     " already lives there).",
     "'# v2 git bundle' header; rag_index names it"),
    ("attic/*", RTH,
     "Already historical: delivery bundles and patches.",
     "attic/README.md"),
    (".github/workflows/agent-substrate.yml", KAH,
     "Substrate CI; new mutation spec wired in Phase 1.",
     ""),
    (".github/workflows/release.yml", RWG,
     "Release workflow; QTA payload expectations retire (Phase 7).",
     ""),
    (".github/workflows/stack-verify.yml", RWG,
     "Runs qta_full_sim/PCC byte gate; rewritten with the workflow (Phase "
     "6).",
     ""),
    (".github/workflows/container-verify.yml", KAI,
     "Container verification.",
     ""),
    (".github/workflows/identity-discovery.yml", KAI,
     "Identity discovery.",
     ""),
    (".github/workflows/claude.yml", KAI,
     "Assistant workflow.",
     ""),
    ("rust/*", EXG,
     "Selective Rust kernels (bit-parity admission).",
     "directive s2"),
    ("QTA_stage9_release_verification/release_trust_policy.json", KAH,
     "Live trust policy: release.yml passes it as --trusted-policy and "
     "release_trust.py reads it; its identity fields stay PENDING until a "
     "real hosted signing run exists. Replaced with the release model "
     "(Phase 7).",
     "release.yml; test_repository_signing_status_remains_pending"),
    ("QTA_stage9_release_verification/*", RTH,
     "Stage-9 release verification record, historical: it is NOT "
     "regenerated per commit. Its SBOM names the Stage-9 uv.lock digest and "
     "lacks the three packages Stage 10 added (contourpy, cycler, dill); "
     "release.yml builds a fresh bundle from uv.lock at release time, and "
     "the Phase-7 set supersedes this one. Regenerating it in place would "
     "bind it to a release zip and revision that do not exist.",
     "sbom qta:uv_lock_sha256 != sha256(uv.lock) since 365b5c8"),
    ("stage7_reports/dependency_inventory.json", REG,
     "Derived from pyproject.toml + uv.lock by "
     "tools/dependency_declarations.py --write; a test holds it to them "
     "(it said 71 packages and omitted h5py before it was derived).",
     "tests/test_dependency_declarations.py"),
    ("stage7_reports/*", REG,
     "Environment/dependency/lint reports regenerated per environment.",
     ""),
    ("stage8_reports/*", REG,
     "HDF5/RO-Crate validation reports regenerated by the derived chain.",
     ""),
    ("ro-crate/ro-crate-metadata.json", REG,
     "Regenerated by ro_crate_tools.py.",
     ""),
    ("final_manifest.json", REG,
     "Regenerated by generate_manifest.py (last in the chain).",
     ""),
    ("manifest_hash.txt", REG,
     "Regenerated by generate_manifest.py.",
     ""),
    ("hdf5_output_mapping.json", REG,
     "Regenerated by build_hdf5_mapping.py.",
     ""),
    ("hdf5_schema.json", REG,
     "Regenerated by build_hdf5_mapping.py.",
     ""),
    ("qta_scientific_results.h5", REG,
     "Regenerated by build_hdf5.py.",
     ""),
    ("BOM.csv", RTH,
     "BOM completeness is not a software requirement (directive s4).",
     ""),
    ("rejected_baseline_BOM.csv", RTH,
     "Rejected BOM.",
     ""),
    ("hardware_registry.json", RTH,
     "Physical component authority.",
     ""),
    ("hardware_reviewers.json", RTH,
     "Physical reviewer registry.",
     ""),
    ("experiment_registry.json", RTH,
     "Apparatus experiment registry.",
     ""),
    ("experiment_gate_coverage.json", RTH,
     "Experiment-to-gate coverage.",
     ""),
    ("experiment_matrix_coverage.json", RTH,
     "Experiment-to-matrix coverage.",
     ""),
    ("campaign_registry.json", RTH,
     "Apparatus campaign registry.",
     ""),
    ("validation_matrix.csv", RTH,
     "Hardware validation matrix.",
     ""),
    ("kill_gate_ranking.csv", RTH,
     "Gate kill ranking.",
     ""),
    ("mode_transition_acceptance_tests.csv", RTH,
     "Mode-transition acceptance tests.",
     ""),
    ("monte_carlo_gate_failure_rates.csv", RTH,
     "Gate failure rates: gate contract retires; failed-sample concept "
     "survives in the generic UQ engine.",
     "directive s12"),
    ("risk_register.csv", RTH,
     "Apparatus risk register (gate_impact, can_PASS_now columns).",
     "header"),
    ("shielding_stack_register.csv", RTH,
     "Apparatus shielding stack.",
     ""),
    ("interface_map.csv", RTH,
     "Physical interface map.",
     ""),
    ("superseded_best_operating_point_v3.json", RTH,
     "Superseded apparatus operating point.",
     ""),
    ("source_map.csv", RTH,
     "Manuscript claim-to-source map (measured_in_this_system column).",
     "header"),
    ("source_gap_register.csv", RTH,
     "Manuscript source gaps.",
     ""),
    ("source_audit_status.txt", RTH,
     "Manuscript source audit.",
     ""),
    ("representative_source_audit.csv", RTH,
     "Manuscript source audit.",
     ""),
    ("bibliography_audit.csv", RTH,
     "Manuscript bibliography audit.",
     ""),
    ("optical_line_of_sight_audit.csv", RTH,
     "Apparatus line-of-sight audit.",
     ""),
    ("radiation_rf_leakage_budget.csv", RTH,
     "Apparatus RF leakage budget.",
     ""),
    ("cryopanel_memory_model.csv", RTH,
     "Apparatus cryopanel memory table.",
     ""),
    ("assumed_parameters.json", PLG,
     "Reference parameter set (16-field provenance schema) for the QTA "
     "reference model plugins.",
     "manifest: 'manual'"),
    ("measured_parameters.json", PLG,
     "Literature/manufacturer-spec constants for the reference plugins "
     "(none measured here).",
     "_note field"),
    ("monte_carlo_parameter_registry.csv", RWG,
     "Parameter distributions: concept kept, re-expressed as "
     "ScientificModel parameter schema (directive s12).",
     ""),
    ("monte_carlo_sensitivity_rankings.csv", DGR,
     "QTA-specific MC output contract (directive s12).",
     ""),
    ("deep_*", DGR,
     "Outputs of the deep experimental-design layer; rebuilt after its "
     "generalization (Phase 3).",
     "written by deep_expdesign/io.py"),
]

def _regen_set() -> set:
    """The committed regeneration set: what qta_full_sim.py writes.

    Recorded by build_hdf5_mapping.py in hdf5_output_mapping.json; read from
    there rather than restated, so the two cannot disagree.
    """
    m = json.loads((ROOT / "hdf5_output_mapping.json").read_text(
        encoding="utf-8"))
    return {o["path"] for o in m["outputs"]} | set(m["exempt"])


def rule_for(path: str, regen: set):
    """``(disposition, rationale, evidence)`` for ``path``, or None."""
    if "/" not in path and path in regen:
        if path in HW_OUTPUTS:
            return (RTH, HW_OUTPUT_WHY,
                    "in hdf5_output_mapping.json regeneration set")
        return (DGR, "Model output of the QTA pipeline (qta_full_sim.py -> "
                "outputs/ -> root). Old output contract; rebuilt from "
                "ScientificModel runs by the generic workflow (Phases 6-7). "
                "Byte-gated and untouched until then (directive s25 Phase 0 "
                "rule 3).", "in hdf5_output_mapping.json regeneration set")
    for pat, disp, why, ev in RULES:
        if fnmatch.fnmatchcase(path, pat):
            return disp, why, ev
    return None


def repository_files() -> list:
    """Tracked plus untracked-unignored files, attic/ INCLUDED.

    ``tools/repo_scope`` skips attic/ because nothing there is linted or
    scanned; here it is exactly what must be disposed of, so the scope is
    taken from git directly. Fails closed: no git, no answer.
    """
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached",
                        "--others", "--exclude-standard"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git could not enumerate {ROOT}: "
                         f"{r.stderr.strip()[:200]}")
    return [p for p in r.stdout.split("\0") if p]


def generate(files=None) -> tuple:
    """``(csv_text, unmatched, unused_rules)`` for the repository's files."""
    files = sorted(set(files if files is not None else repository_files()))
    regen = _regen_set()
    rows, unmatched = [], []
    for p in files:
        r = rule_for(p, regen)
        if r is None:
            unmatched.append(p)
            continue
        disp, why, ev = r
        rows.append({"path": p, "disposition": disp, "phase": PHASE[disp],
                     "rationale": why, "evidence": ev})
    unused = [pat for pat, *_ in RULES
              if not any(fnmatch.fnmatchcase(r["path"], pat) for r in rows)]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue(), unmatched, unused


def main(argv) -> int:
    text, unmatched, unused = generate()
    if unmatched:
        print(f"NO RULE for {len(unmatched)} file(s) -- give each a "
              f"disposition in tools/file_disposition.py: {unmatched}")
        return 1
    if unused:
        print(f"rules that match no file (stale): {unused}")
        return 1
    counts = collections.Counter(
        r["disposition"] for r in csv.DictReader(io.StringIO(text)))
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    if "--write" in argv:
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT.name}: {sum(counts.values())} files; {summary}")
        return 0
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if current != text:
        print(f"{OUT.name} is not what the rules produce for this tree; "
              "run: python tools/file_disposition.py --write")
        return 1
    print(f"file dispositions hold: {sum(counts.values())} files, each "
          f"matched by exactly one rule; {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
