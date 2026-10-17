#!/usr/bin/env python3
"""The forensic inventory: every file tracked at the transformation baseline,
what it was, what depended on it, and what became of it.

WHY THIS EXISTS

The owner retired the hardware-theory programme this repository grew out of
and asked for it to leave the tracked tree -- not relabelled, not moved to an
archive folder, gone -- while the generic software it had grown (the agent
substrate, the scientific layer, the learned-model substrate, the assurance
tooling) stays and is strengthened. A removal of that size is only reviewable
if every file is accounted for: what it was, why it went or stayed, what it
depended on and what depended on it, and where its capability lives now.

One record per file tracked at ``BASELINE_COMMIT``. Each carries:

* ``class`` -- one of :data:`CLASSES`, decided by the first matching rule in
  :data:`RULES` (an explicit path wins over a pattern);
* ``rationale`` and ``action`` -- why, and what is done about it;
* ``destinations`` -- for a file whose capability moved, where it lives now;
* the REAL dependencies, measured at the baseline rather than recalled:
  ``imports`` (static imports, relative imports resolved, and
  ``importlib.import_module`` of a constant), ``references`` (every other
  baseline path, dotted module name or unique file name the file's text
  names: fixtures, data files, entry points in workflows, documentation
  links, generated outputs), and the reverse of both;
* ``final_disposition`` -- a FACT about the current tree, recomputed by
  ``check``: REMOVED, RETAINED, MOVED, EXTRACTED, REIMPLEMENTED, REGENERATED,
  PROVENANCE_RESOLVED -- or PENDING while the action has not yet happened.

    python tools/repo_inventory.py derive   # needs the baseline's objects
    python tools/repo_inventory.py check    # the inventory against the tree
    python tools/repo_inventory.py check --applied   # ... nothing PENDING
    python tools/repo_inventory.py verify-derivation  # derive == committed

``check`` reads no Git history: the baseline listing and blob ids are in the
document, so a shallow checkout can run it. ``derive`` re-reads the baseline
from the object store and must reproduce the committed document byte for
byte (the hosted job that runs it fetches full history).

The inventory says what happened to files. Whether the CURRENT tree carries
any retired content is a different question, answered by
``tools/repo_content_audit.py``.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("docs") / "repository_inventory.json"
SCHEMA = "repository-inventory/1"
BASELINE_COMMIT = "b8aac891f3d767b2598acaf413c099afcefcba30"

CLASSES = {
    "REMOVE": "retired hardware theory, its outputs, documents, tests, tools, "
              "archives or a duplicate of a retained capability; leaves the "
              "tracked tree (Git history keeps it, unchanged)",
    "EXTRACT_GENERIC": "generic capability living inside a retired package "
                       "or file; moved to a generic home, its hardware "
                       "coupling cut, then the source removed",
    "PRESERVE": "current generic software, evidence or documentation; "
                "retained -- edited only to remove retired references or "
                "follow a rename",
    "REIMPLEMENT": "a capability worth keeping whose implementation was "
                   "bound to the retired programme; rebuilt over the generic "
                   "layer with its own tests, the old file removed",
    "REGENERATE": "a derived artefact (manifest, registry, status, lock); "
                  "rebuilt by its generator from the new tree, never edited "
                  "by hand",
    "REVIEW_DATA_PROVENANCE": "learned-model evidence whose provenance had to "
                              "be decided (generic or hardware-dependent) "
                              "before it could be kept",
}

DISPOSITIONS = ("REMOVED", "RETAINED", "MOVED", "EXTRACTED", "REIMPLEMENTED",
                "REGENERATED", "PROVENANCE_RESOLVED", "PENDING")

_HW = ("the retired hardware-theory programme")

# ---------------------------------------------------------------------------
# Destinations: where a moved, extracted or reimplemented capability lives.
# A destination that does not exist in the current tree leaves its source
# PENDING; ``check --applied`` refuses that.
# ---------------------------------------------------------------------------
_AGENT_RENAMES = {
    "qta_agent/governed_stage10.py": "agent_substrate/governed_tasks.py",
    "qta_agent/_stage10_tool.py": "agent_substrate/_artifact_tool.py",
    "qta_agent/_stage10_index_tool.py": "agent_substrate/_index_tool.py",
}

DESTINATIONS = {
    **_AGENT_RENAMES,
    "qta_multiphysics/stack/workspace.py": "scientific/workspace.py",
    "stage7_reports/dependency_inventory.json":
        "docs/dependency_inventory.json",
    "qta_multiphysics/stack/rag_index.py": "agent_substrate/rag_index.py",
    "qta_multiphysics/stack/registry.py": "tools/stack_registry.py",
    "qta_multiphysics/stack/vtk_export.py":
        "scientific/integrations/vtk_export.py",
    "qta_multiphysics/stack/usd_export.py":
        "scientific/integrations/usd_export.py",
    "qta_multiphysics/stack/mdao_openmdao.py":
        "scientific/integrations/openmdao_doe.py",
    "tests/test_stage10_stack.py": "tests/test_scientific_integrations.py",
    "tests/test_scientific_import_isolation.py":
        "tests/test_repo_content_audit.py",
    "tests/test_framework_boundary.py": "tests/test_repo_content_audit.py",
    "tools/framework_boundary.py": "tools/repo_content_audit.py",
    "docs/framework_boundary.json": "docs/repo_content_audit.json",
    "tools/pass_semantics_audit.py": "tools/repo_content_audit.py",
    "docs/pass_semantics_audit.json": "docs/repo_content_audit.json",
    "tools/neural_legacy_audit.py": "tools/repo_content_audit.py",
    "docs/neural/legacy_semantic_audit.json": "docs/repo_content_audit.json",
    "tools/file_disposition.py": "tools/repo_inventory.py",
    "FILE_DISPOSITION.csv": "docs/repository_inventory.json",
    "tests/test_file_disposition.py": "tests/test_repo_inventory.py",
    "tests/test_workflow_split.py": "tests/test_workflow_generic.py",
    "tests/test_bootstrap_workflow_contract.py":
        "tests/test_container_workflow_contract.py",
    "ARCHITECTURE_CONVERGENCE_PLAN.md": "ARCHITECTURE.md",
    "tools/mutations/framework_boundary.json":
        "tools/mutations/repository_content.json",
    "tests/test_optimized_mode_invariants.py":
        "tests/test_no_enforcing_asserts.py",
    "tests/test_agent_governed_stage10.py":
        "tests/test_agent_governed_tasks.py",
    "tools/mutations/stage10_authority.json":
        "tools/mutations/governed_tasks_authority.json",
}


def _dest(path: str) -> list:
    if path in DESTINATIONS:
        return [DESTINATIONS[path]]
    if path.startswith("qta_agent/"):
        return ["agent_substrate/" + path[len("qta_agent/"):]]
    return []


# ---------------------------------------------------------------------------
# Rules. First match wins; explicit paths come first. Every rule says WHY.
# ---------------------------------------------------------------------------
R = "REMOVE"
X = "EXTRACT_GENERIC"
P = "PRESERVE"
I = "REIMPLEMENT"  # noqa: E741
G = "REGENERATE"
D = "REVIEW_DATA_PROVENANCE"

EXPLICIT: dict = {
    # ---- a generic report that lived in a retired report directory ----
    "stage7_reports/dependency_inventory.json": (
        I, "the generated dependency inventory; its recorded reasons "
           "described the retired programme's uses",
        "regenerated from pyproject.toml and uv.lock under docs/"),
    # ---- the generic layer's own files bound to the retired programme ----
    "scientific/models/thermal_1d.py": (
        R, "a QTA apparatus model -- diamond k(T)/Cp(T), a Beer-Lambert "
           "laser source, a 10 mK fridge and a Kapitza contact -- wrapped as "
           "a ScientificModel; the generic transient slab "
           "(scientific/models/slab_transient.py) is the retained thermal "
           "case, with two independent checks that share no code with it",
        "remove; governed-path tests retargeted to thermal.slab_transient"),
    "scientific/models/thermal_2d.py": (
        R, "the same apparatus model in 2D axisymmetry (radial laser spot, "
           "fridge contact)", "remove"),
    "scientific/checks/reduction_2d.py": (
        R, "checks the apparatus 1D model against the apparatus 2D solver; "
           "both retired", "remove"),
    "scientific/checks/reduction_3d.py": (
        R, "checks the apparatus 2D model against the apparatus 3D solver; "
           "both retired", "remove"),
    "tests/test_scientific_thermal_1d.py": (
        R, "tests the retired apparatus 1D model", "remove"),
    "tests/test_scientific_thermal_2d.py": (
        R, "tests the retired apparatus 2D model", "remove"),
    "tools/mutations/scientific_thermal_1d.json": (
        R, "mutation spec of the retired apparatus 1D model", "remove"),
    "tools/mutations/scientific_thermal_2d.json": (
        R, "mutation spec of the retired apparatus 2D model", "remove"),
    "tools/mutations/legacy/cryopanel_operating_point.json": (
        R, "mutation spec of a retired hardware module", "remove"),
    "tools/mutations/legacy/hardware_governance.json": (
        R, "mutation spec of a retired hardware module", "remove"),
    "tools/mutations/solver_failclosed.json": (
        R, "mutation spec over the retired coupled 3D solvers", "remove"),
    "tools/mutations/cross_environment.json": (
        R, "mutation spec of the cross-environment classifier of the "
           "retired canonical corpus", "remove"),
    "tools/mutations/reference_backend.json": (
        R, "mutation spec of the reference backend that regenerated the "
           "retired corpus", "remove"),
    "tools/mutations/rust_selection.json": (
        R, "mutation spec of the Rust kernel selection; both kernels served "
           "the retired solvers and were REJECTED", "remove"),
    "tools/mutations/canonical_output_set.json": (
        R, "mutation spec of the retired canonical output set", "remove"),
    "tools/mutations/output_resolution.json": (
        R, "mutation spec of resolution statements on retired outputs",
        "remove"),
    "tools/mutations/quantity_resolution.json": (
        R, "mutation spec of resolution statements on retired outputs",
        "remove"),
    "tools/mutations/resolution_inventory.json": (
        R, "mutation spec of the inventory of retired outputs", "remove"),
    "tools/mutations/unit_inventory.json": (
        R, "mutation spec of the unit inventory of retired outputs",
        "remove"),
    "tools/mutations/reproduction_policy.json": (
        P, "mutations of the byte-reproduction verdict in "
           "scientific/reproduction.py; eleven of its twenty-nine targeted "
           "the retired witness tool, wrapper and package checker",
        "retain, those eleven dropped"),
    "tools/mutations/framework_boundary.json": (
        I, "mutation spec of the active/legacy boundary, which no longer has "
           "a legacy side", "replaced by the content audit's spec"),
    "tools/mutations/enforcement_asserts.json": (
        P, "mutations of the rule that no production code enforces with "
           "an assert (python -O deletes them); its targets were retired "
           "files", "retain, retargeted to the reimplemented rule"),
    "tools/mutations/agent_job_graph.json": (
        P, "mutations of the governed job graph; six of its twelve targeted "
           "the retired 3D container diagnostic", "retain, those six "
                                                  "dropped"),
    "tools/mutations/stage10_authority.json": (
        P, "workspace write authority and retrieval trust", "retain, renamed "
           "with the namespace"),
    "tests/test_agent_governed_stage10.py": (
        P, "the governed task lifecycle's tests", "retain, renamed with the "
                                                 "module"),
    "tests/test_optimized_mode_invariants.py": (
        I, "two halves: the gate table and machine interlocks under python "
           "-O (retired), and the rule that no production code enforces "
           "with an assert (generic)", "reimplemented as the generic rule "
                                      "over the new tree"),
    "tools/mutations/claim_hygiene.json": (
        P, "NF-1T status vocabulary; generic", "retain, retargeted"),
    # ---- stack adapters ----
    "qta_multiphysics/stack/workspace.py": (
        X, "the governed-writer workspace guard: generic filesystem "
           "confinement used by the agent's tools and the scientific "
           "runners", "move to scientific/workspace.py"),
    "qta_multiphysics/stack/rag_index.py": (
        X, "governed read-only retrieval over reviewed documents: generic",
        "move to agent_substrate/rag_index.py; its corpus loses the retired "
           "documents"),
    "qta_multiphysics/stack/registry.py": (
        X, "the stack-registry validator (stack.json against STACK.md): "
           "software assurance", "move to tools/stack_registry.py"),
    "qta_multiphysics/stack/vtk_export.py": (
        I, "a deterministic VTK RectilinearGrid writer -- generic mechanics, "
           "but it serialised the retired 3D apparatus field",
        "reimplement over a generic cell-centred field (the slab model's)"),
    "qta_multiphysics/stack/usd_export.py": (
        I, "a deterministic OpenUSD writer of the retired apparatus domain",
        "reimplement for a generic rectilinear domain"),
    "qta_multiphysics/stack/mdao_openmdao.py": (
        I, "OpenMDAO design-space exploration of the retired operating point",
        "reimplement as a design of experiments over any admitted "
           "ScientificModel, cross-checked against direct runs"),
    "qta_multiphysics/stack/sensitivity_salib.py": (
        R, "a SALib cross-check of the retired 3D sensitivity ranking; the "
           "generic SALib path is scientific/sensitivity.py -- keeping both "
           "would be two sources of truth for one primitive", "remove"),
    "qta_multiphysics/stack/fem_fenicsx.py": (
        R, "the STAGED FEniCSx adapter of the retired solvers; the adopted "
           "FEniCSx verifier is scientific/checks/fenicsx_slab.py with "
           "integrations/fenicsx", "remove (duplicate)"),
    "qta_multiphysics/stack/fmi_contract.py": (
        R, "the DEFERRED FMI export contract of the retired solvers; the "
           "adopted FMI path is scientific/fmi_boundary.py with "
           "integrations/fmi", "remove (duplicate)"),
    "qta_multiphysics/stack/rust_kernel.py": (
        R, "Rust kernel dispatch for two kernels of the retired solvers, "
           "both REJECTED by a measured decision; no retained code calls "
           "either -- an unused code path", "remove"),
    "qta_multiphysics/stack/__init__.py": (
        R, "constants of the retired stage-10 stack (a gate-effect label)",
        "remove; the moved modules carry their own boundary"),
    # ---- tooling ----
    "tools/framework_boundary.py": (
        I, "the active/transitional/legacy boundary checker; with the legacy "
           "side removed its question becomes 'does any retired content "
           "remain', which is the content audit", "replaced"),
    "docs/framework_boundary.json": (
        I, "the boundary's declared classes and exceptions", "replaced"),
    "tools/pass_semantics_audit.py": (
        I, "kept the legacy gate table's PASS out of current status; with the "
           "gate table gone the question is whether anything reads one -- "
           "the content audit's gate-dependency rule", "replaced"),
    "docs/pass_semantics_audit.json": (I, "its output", "replaced"),
    "tools/neural_legacy_audit.py": (
        I, "kept hardware-era semantics out of the learned-model substrate; "
           "now a scope of the content audit", "replaced"),
    "docs/neural/legacy_semantic_audit.json": (I, "its output", "replaced"),
    "tools/file_disposition.py": (
        I, "the previous file disposition (KEEP / RETIRE_TO_HISTORY ...); "
           "superseded by this inventory, whose classes are the owner's",
        "replaced"),
    "FILE_DISPOSITION.csv": (I, "its output", "replaced"),
    "tests/test_file_disposition.py": (I, "its tests", "replaced"),
    "tests/test_framework_boundary.py": (I, "its tests", "replaced"),
    "tests/test_scientific_import_isolation.py": (
        I, "held 'scientific imports no hardware module' per module in a "
           "fresh interpreter; with the hardware package gone the rule is "
           "repository-wide and lives in the content audit", "replaced"),
    "tests/test_stage10_stack.py": (
        I, "tests of the stack adapters, most over the retired 3D solver",
        "replaced by tests of the reimplemented integrations"),
    "tests/test_workflow_split.py": (
        P, "the generic Snakemake path; its legacy half goes with the legacy "
           "workflow", "retain the generic assertions, renamed"),
    "tests/test_bootstrap_workflow_contract.py": (
        X, "contract tests of the container workflow (generic) and of the "
           "legacy release and identity-discovery workflows (retired)",
        "keep the container tests, renamed; drop the rest"),
    "tools/rust_kernel_decision.py": (
        R, "re-derives the decision on two REJECTED kernels of the retired "
           "solvers", "remove"),
    "docs/rust_kernel_decisions.json": (
        R, "the committed REJECTED decisions; recorded in the stack registry "
           "as the reason the element was retired", "remove"),
    "tools/reference_backend.py": (
        R, "builds a fixed CPU backend to regenerate the retired canonical "
           "corpus byte for byte", "remove"),
    "tools/reproduction_witness.py": (
        R, "witnesses byte reproduction of the retired canonical corpus; the "
           "generic reproduction verdicts stay in scientific/reproduction.py",
        "remove"),
    "tools/cross_env_semantics.py": (
        R, "classifies cross-environment differences of the retired corpus",
        "remove"),
    "tools/blas_kernel_sensitivity.py": (
        R, "BLAS kernel sensitivity of the retired 3D outputs", "remove"),
    "tools/clip_provenance.py": (
        R, "provenance of clipped values in retired outputs", "remove"),
    "tools/regenerate_instrumented.py": (
        R, "instrumented regeneration of the retired corpus", "remove"),
    "tools/legacy_qta_claims.py": (
        R, "claims enforcement of the retired hardware forecast", "remove"),
    "tools/unit_inventory.py": (
        R, "unit inventory of the retired CSV outputs; the generic layer "
           "carries units on every Quantity", "remove"),
    "tools/resolution_inventory.py": (
        R, "resolution inventory of the retired outputs; the generic layer "
           "carries resolution on every Quantity", "remove"),
    "docs/unit_inventory.json": (R, "output of a removed tool", "remove"),
    "docs/resolution_inventory.json": (R, "output of a removed tool",
                                       "remove"),
    "docs/byte_reproduction_profile.json": (
        R, "witness profile of the retired corpus", "remove"),
    "docs/blas_kernel_sensitivity.json": (R, "output of a removed tool",
                                          "remove"),
    "docs/clip_provenance.json": (R, "output of a removed tool", "remove"),
    "docs/reference_backend_host_a.json": (
        R, "reference-backend regeneration record of the retired corpus",
        "remove"),
    "docs/R59_CROSS_ENVIRONMENT_ANALYSIS.md": (
        R, "analysis of cross-environment divergence of the retired 3D "
           "outputs", "remove"),
    "docs/CORPUS_RECOVERY_TRIAGE.md": (
        R, "triage of the retired canonical corpus", "remove"),
    "docs/SESSION_REPORT.md": (
        R, "a historical session report on the agent substrate, superseded "
           "by AGENT_SUBSTRATE.md and ARCHITECTURE.md; its gate-count "
           "statements describe the retired programme", "remove"),
    "docs/DEFECT_LEDGER.md": (
        P, "the defect ledger: software defects stay in full; entries whose "
           "subject was retired hardware theory keep their ID with a pointer "
           "to the baseline commit", "retain, edited"),
    "docs/identity_inventory.json": (
        G, "durable actions and identity-bearing fields of the agent "
           "substrate", "regenerate after the rename"),
    "docs/typing_scope.json": (G, "the active type-check scope",
                               "regenerate over the new tree"),
    "docs/performance_baseline.json": (
        P, "critical-path performance baseline: an append-only series of "
           "shape measurements of the substrate's guards, each keyed by the "
           "commit it was taken at, none of a retired workload",
        "retain; the next recording appends against its own commit"),
    "docs/completion_matrix.json": (
        P, "the software completion matrix; rows bound to retired evidence "
           "are retired, the rest retargeted", "retain, rebuilt"),
    "docs/harness_contract.json": (
        P, "the definition of done; its legacy-gate fact goes", "retain, "
           "rebuilt as software-only"),
    "docs/harness_status.json": (G, "derived status", "re-derive"),
    "docs/corpus_allowlist.json": (G, "the retrieval corpus allowlist",
                                   "regenerate"),
    "docs/claims_boundary.json": (P, "generic claims boundary",
                                  "retain, edited"),
    "docs/equivalence_contracts.json": (P, "generic equivalence policy",
                                        "retain"),
    "docs/supply_chain_ci_policy.json": (P, "generic supply-chain CI policy",
                                         "retain, edited"),
    "docs/neural/nf1t_inventory.json": (
        P, "the NF-1T inventory; its hardware-era entries describe files "
           "this transformation removes", "retain, edited"),
    "docs/neural/current_status.json": (
        G, "derived NF-1T status; its legacy gate-table section goes",
        "re-derive"),
    # ---- the learned-model development evidence ----
    "scientific_ai/neural/source_surface_adsorption.py": (
        D, "the dataset generator: Latin-hypercube samples of the generic "
           "Langmuir capture model over broad textbook ranges (T 10-1000 K, "
           "mass 2-200 u, pressure 1e-7-10 Pa); no apparatus constant, phase "
           "or mode. GENERIC", "retain; its closure's retired references are "
           "removed and the regenerated dataset proven byte-identical"),
    # ---- packaging and workflow ----
    "pyproject.toml": (P, "packaging; the NV spin dependency (qutip) and the "
                          "hardware package name go", "retain, edited"),
    "uv.lock": (G, "the lock", "re-lock after the dependency change"),
    "requirements.txt": (
        R, "runtime pins of the retired simulator (numpy, scipy, qutip); "
           "pyproject.toml and uv.lock are the one declaration", "remove"),
    "final_manifest.json": (G, "per-file provenance manifest", "regenerate"),
    "manifest_hash.txt": (G, "detached manifest hash", "regenerate"),
    "ro-crate/ro-crate-metadata.json": (G, "the RO-Crate", "regenerate"),
    "HARNESS_STATUS.md": (G, "derived status", "re-derive"),
    "SCIENTIFIC_AI_STATUS.md": (G, "derived NF-1T status", "re-derive"),
    "stack.json": (P, "the stack registry", "retain, rebuilt"),
    "STACK.md": (P, "the stack registry's narrative", "retain, rewritten"),
    "authorities.json": (P, "single sources of truth; legacy authorities go",
                         "retain, edited"),
    "Snakefile": (P, "the generic workflow; the legacy include and the rules "
                     "over the retired solvers go", "retain, edited"),
    "workflow/legacy_qta.smk": (R, "the retired pipeline's workflow",
                                "remove"),
    "analysis/collect_container_3d.py": (
        R, "collects retired 3D outputs from a container run", "remove"),
    "container_verification.md": (
        P, "container verification status", "retain, rewritten for the "
                                             "generic image"),
    "ARCHITECTURE_CONVERGENCE_PLAN.md": (
        I, "nineteen programme reports interleaving software work with the "
           "retired programme's history", "replaced by ARCHITECTURE.md (the "
           "current architecture); the reports stay in Git history"),
    ".github/workflows/release.yml": (
        R, "the retired programme's release (tag qta-stage*, a gate-table "
           "PASS recomputation, the QTA source archive); the generic "
           "release candidate is supply-chain.yml", "remove"),
    ".github/workflows/identity-discovery.yml": (
        R, "discovers the signing identity for the retired release policy",
        "remove"),
    ".github/workflows/reference-backend.yml": (
        R, "builds the reference backend for the retired corpus", "remove"),
}

#: Pattern rules, after EXPLICIT. (regex, class, rationale, action)
PATTERNS = [
    # retired trees, wholesale
    (r"^attic/", R, "delivery bundles, tarballs and patches of " + _HW,
     "remove"),
    (r"^EXPERIMENT_PLAYBOOKS/", R, "hardware experiment playbooks", "remove"),
    (r"^QTA_stage9_release_verification/", R,
     "the retired release's policy, SBOM, provenance and checksums",
     "remove"),
    (r"^stage[78]_reports/", R, "generated reports of the retired pipeline",
     "remove"),
    (r"^matrix_update_examples/", R,
     "requests against the hardware validation matrix", "remove"),
    (r"^docs/legacy/", R, "the retired programme's documents", "remove"),
    (r"^reference_backend/", R,
     "a fixed CPU backend built to regenerate the retired corpus", "remove"),
    (r"^rust/", R, "Rust kernels for the retired solvers, both REJECTED",
     "remove"),
    (r"^qta_multiphysics/", R,
     "the hardware-theory package: modes, species, cryopanels, NV sensing, "
     "machine FSM, governance, campaigns and the solvers built for them. "
     "Its generic numerics (finite-volume helpers, grids, unit guards) have "
     "no consumer in the retained tree: the generic slab solver implements "
     "its own closure", "remove"),
    # the agent substrate
    (r"^qta_agent/", P, "the event-sourced authority substrate: generic",
     "move to agent_substrate/ (namespace rename); protocol identifiers "
     "recorded in events are kept"),
    # scientific, learned, integrations
    (r"^scientific/", P, "the generic scientific layer", "retain"),
    (r"^scientific_ai/", P, "the learned-model substrate (NF-1T)", "retain"),
    (r"^integrations/", P,
     "pinned runtimes and sources of the generic integrations (FEniCSx, "
     "FMI, RO-Crate validator, Sigstore, proposals fixture)", "retain"),
    (r"^docs/neural/dev/", D,
     "development evidence trained on the surface-adsorption dataset; kept "
     "because the dataset is generic and unchanged", "retain unchanged"),
    (r"^docs/neural/", P, "NF-1T specification and evidence", "retain"),
    # tests
    (r"^tests/fuzz_corpus/", P, "regression corpus of the substrate fuzzer",
     "retain"),
    (r"^tests/(hangguard|neural_support)\.py$", P, "test support", "retain"),
    (r"^tests/hw_reviewer_fixtures\.py$", R,
     "reviewer rosters for the hardware-governance suites", "remove"),
    (r"^tests/test_(agent_|actor_|authenticated_|checkpoint_recovery|"
     r"decided_appends|proposal_|replay_origin|second_reader|signed_append|"
     r"task_execution|verified_read|model_check|performance_telemetry)", P,
     "agent-substrate tests", "retain (imports follow the rename)"),
    (r"^tests/test_(scientific_|governed_|backend_identity|external_models|"
     r"hdf5_bundle|ranking_equivalence|run_identity|sensitivity_cross|"
     r"harness_|reproduction_verdict|generic_consistency|surface_adsorption)",
     P, "generic scientific-layer and harness tests",
     "retain; those that drove the retired thermal model are retargeted to "
     "thermal.slab_transient"),
    (r"^tests/test_neural_", P, "NF-1T tests", "retain"),
    (r"^tests/test_(completion_matrix|mutation_|generated_mutations|"
     r"dependency_declarations|typecheck_scope|test_isolation|repo_contract|"
     r"claims_boundary|claude_workflow|ro_crate|supply_chain|manifest_|"
     r"container_manifest|identity_inventory|claim_hygiene)", P,
     "software-assurance tests", "retain, edited for the new tree"),
    (r"^tests/test_", R,
     "tests of " + _HW + ": its solvers, modes, species, hardware "
     "governance, campaigns, NV layer, gate table, BOM, legacy release "
     "trust pipeline, canonical corpus and its cross-environment tooling",
     "remove"),
    # tools
    (r"^tools/mutations/", P, "mutation specification of retained code",
     "retain (suites follow renames)"),
    (r"^tools/", P, "software-assurance tooling", "retain, edited"),
    # documents
    (r"^docs/", P, "current documentation", "retain, edited"),
    (r"^\.github/workflows/", P, "hosted CI", "retain, edited"),
    # root: the retired programme's code, outputs and documents
    (r"^(qta_full_sim|qta_sim_stages|package_consistency_check|build_hdf5|"
     r"build_hdf5_mapping|validate_hdf5_equivalence|"
     r"manuscript_consistency_check|build_stage6_playbooks|"
     r"build_stage6_registries|stage6_preservation_check|snakemake_sim_entry|"
     r"verify_release|release_trust|build_release_artifacts|"
     r"finalize_release_signing|release_revision_gate)\.py$", R,
     "the retired simulator, its stages, its package checker, its HDF5 and "
     "manuscript tooling, its stage-6 registries and its release pipeline",
     "remove"),
    (r"^qta_manuscript_v4\.(tex|pdf)$", R, "the hardware manuscript",
     "remove"),
    (r"^qta_scientific_results\.h5$", R, "the retired results in HDF5",
     "remove"),
    (r"^QTA_full_history-6\.bundle\.txt$", R, "a retired delivery bundle",
     "remove"),
    (r"^(GLOSSARY|HARDWARE_GOVERNANCE|MACHINE_FSM|MANUSCRIPT_CHANGELOG|"
     r"MANUSCRIPT_CONSISTENCY_REPORT|NUMERICAL_METHODS|PHYSICS_INVENTORY|"
     r"FIRST_VALIDATION_EXPERIMENTS|VALIDATION_PLAN|GATE_TABLE_README|"
     r"SOURCE_AUDIT_STATUS|THERMAL_3D_L3|DEPENDENCY_CUTOVER|EVIDENCE_CLOSURE|"
     r"RUNTIME_RESILIENCE|RELEASE_POLICY|RELEASE_TRUST_ENFORCEMENT)\.md$", R,
     "documentation of " + _HW + " (glossary of modes and species, hardware "
     "governance, machine FSM, manuscript, numerical methods of the retired "
     "solvers, physics inventory, validation plans, gate table, source "
     "audit, the cut-over plan, its evidence closure, its runtime "
     "resilience and its release policy)", "remove"),
    (r"^raw_data_standard\.md$", R, "raw-data standard for hardware "
                                    "measurements", "remove"),
    (r"^validation_matrix_update_request\.schema\.json$", R,
     "schema of hardware validation-matrix requests", "remove"),
    (r"^[^/]+\.(csv|txt|mmd)$", R,
     "an output, register or table of the retired programme (gate table, "
     "BOM, interlocks, budgets, profiles, Monte-Carlo, NV, machine-FSM "
     "traces, source audit)", "remove"),
    (r"^[^/]+\.json$", R,
     "an output, registry or summary of the retired programme "
     "(campaigns, experiments, design, hardware registries, 3D summaries, "
     "deep experimental design, operating points)", "remove"),
    (r"^(AGENT_SUBSTRATE|NEURAL_SUBSTRATE|AUTHORITIES|CLAIMS_BOUNDARY|"
     r"HDF5_DATA_MODEL|MANIFEST_BOUNDARY|RO_CRATE_PROFILE|SIGNING_BOOTSTRAP|"
     r"SUPPLY_CHAIN_THREAT_MODEL|TESTING|INSTALL|README)\.md$", P,
     "current documentation", "retain, edited"),
    (r"^(generate_manifest|ro_crate_tools|conftest)\.py$", P,
     "generic provenance tooling and test configuration", "retain, edited"),
    (r"^(Dockerfile|\.dockerignore|container_verify\.sh)$", P,
     "the reproducible container", "retain, edited"),
    (r"^(\.gitignore|typecheck_active\.ini)$", P, "repository configuration",
     "retain, edited"),
]
_PATTERNS = [(re.compile(p), c, r, a) for p, c, r, a in PATTERNS]


def classify(path: str) -> tuple:
    if path in EXPLICIT:
        return EXPLICIT[path]
    for rx, c, r, a in _PATTERNS:
        if rx.search(path):
            return c, r, a
    raise KeyError(f"no rule classifies {path!r}")


# ---------------------------------------------------------------------------
# Dependencies, measured at the baseline
# ---------------------------------------------------------------------------
def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          capture_output=True, text=True).stdout


def baseline_listing() -> dict:
    """{path: blob id} at the baseline commit."""
    out = {}
    for line in _git("ls-tree", "-r", BASELINE_COMMIT).splitlines():
        meta, path = line.split("\t", 1)
        _mode, kind, blob = meta.split()
        if kind == "blob":
            out[path] = blob
    return out


def baseline_blobs(listing: dict) -> dict:
    """{path: bytes}, read in one ``git cat-file --batch``."""
    paths = sorted(listing)
    req = "".join(f"{listing[p]}\n" for p in paths).encode()
    raw = subprocess.run(["git", "-C", str(ROOT), "cat-file", "--batch"],
                         input=req, check=True, capture_output=True).stdout
    out, pos = {}, 0
    for p in paths:
        nl = raw.index(b"\n", pos)
        _sha, _kind, size = raw[pos:nl].split()
        n = int(size)
        out[p] = raw[nl + 1:nl + 1 + n]
        pos = nl + 1 + n + 1
    return out


def _module(path: str) -> str:
    m = path[:-3].replace("/", ".")
    return m[:-len(".__init__")] if m.endswith(".__init__") else m


def python_imports(path: str, data: bytes, modules: dict) -> set:
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return set()
    name = _module(path)
    pkg = name if path.endswith("__init__.py") else name.rpartition(".")[0]
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg
                for _ in range(n.level - 1):
                    base = base.rpartition(".")[0]
                base = f"{base}.{n.module}" if n.module else base
            else:
                base = n.module or ""
            found.add(base)
            found.update(f"{base}.{a.name}" for a in n.names)
        elif (isinstance(n, ast.Call)
              and getattr(n.func, "attr", getattr(n.func, "id", ""))
              == "import_module" and n.args
              and isinstance(n.args[0], ast.Constant)
              and isinstance(n.args[0].value, str)):
            found.add(n.args[0].value)
    out = set()
    for d in found:
        parts = d.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in modules:
                out.add(modules[cand])
                break
    out.discard(path)
    return out


_TOKEN = re.compile(rb"[A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_]")


def text_references(path: str, data: bytes, by_path: set, by_base: dict,
                    by_module: dict) -> set:
    """Every other baseline file this file's bytes name: by path, by dotted
    module name, or by a file name unique in the baseline."""
    out = set()
    for tok in set(_TOKEN.findall(data)):
        t = tok.decode("ascii", "replace").lstrip("./")
        if t in by_path:
            out.add(t)
        elif t in by_module:
            out.add(by_module[t])
        else:
            base = t.rsplit("/", 1)[-1]
            if base in by_base:
                out.add(by_base[base])
    out.discard(path)
    return out


def derive_dependencies(listing: dict, blobs: dict) -> dict:
    paths = set(listing)
    py = sorted(p for p in paths if p.endswith(".py"))
    modules = {_module(p): p for p in py}
    counts: dict = {}
    for p in paths:
        b = p.rsplit("/", 1)[-1]
        counts[b] = counts.get(b, 0) + 1
    # unique, distinctive file names only: a name that occurs twice, or a
    # short generic one, would attribute a reference to the wrong file
    by_base = {p.rsplit("/", 1)[-1]: p for p in paths
               if counts[p.rsplit("/", 1)[-1]] == 1
               and len(p.rsplit("/", 1)[-1]) >= 8
               and "." in p.rsplit("/", 1)[-1]}
    by_module = {m: p for m, p in modules.items() if "." in m}
    deps = {}
    for p in sorted(paths):
        data = blobs[p]
        imports = (python_imports(p, data, modules) if p.endswith(".py")
                   else set())
        refs = (set() if b"\0" in data[:8192] else
                text_references(p, data, paths, by_base, by_module))
        deps[p] = {"imports": sorted(imports),
                   "references": sorted(refs - imports)}
    rev_i: dict = {p: [] for p in paths}
    rev_r: dict = {p: [] for p in paths}
    for p, d in deps.items():
        for q in d["imports"]:
            rev_i[q].append(p)
        for q in d["references"]:
            rev_r[q].append(p)
    for p in deps:
        deps[p]["imported_by"] = sorted(rev_i[p])
        deps[p]["referenced_by"] = sorted(rev_r[p])
    return deps


# ---------------------------------------------------------------------------
# Final disposition: a fact about the current tree
# ---------------------------------------------------------------------------
def current_tree(root: Path = ROOT) -> set:
    return set(subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"], check=True, capture_output=True,
        text=True).stdout.split("\0")) - {""}


def final_disposition(path: str, cls: str, dests: list, blob: str,
                      tree: set, root: Path = ROOT) -> str:
    present = path in tree and (root / path).exists()
    dests_ok = bool(dests) and all(d in tree and (root / d).exists()
                                   for d in dests)
    if cls == "REMOVE":
        return "REMOVED" if not present else "PENDING"
    if cls in ("EXTRACT_GENERIC", "REIMPLEMENT"):
        if present or not dests_ok:
            return "PENDING"
        return "EXTRACTED" if cls == "EXTRACT_GENERIC" else "REIMPLEMENTED"
    if cls == "PRESERVE":
        if dests:
            return "MOVED" if (dests_ok and not present) else "PENDING"
        return "RETAINED" if present else "PENDING"
    if cls == "REGENERATE":
        if not present:
            return "PENDING"
        return ("REGENERATED" if _blob_id((root / path).read_bytes()) != blob
                else "PENDING")
    if cls == "REVIEW_DATA_PROVENANCE":
        return "PROVENANCE_RESOLVED" if present else "PENDING"
    raise ValueError(cls)


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def build(listing: dict, deps: dict, tree: set, root: Path = ROOT) -> dict:
    files = []
    for p in sorted(listing):
        cls, why, action = classify(p)
        dests = _dest(p)
        files.append({
            "path": p, "blob": listing[p], "class": cls, "rationale": why,
            "action": action, "destinations": dests,
            **deps[p],
            "final_disposition": final_disposition(p, cls, dests, listing[p],
                                                   tree, root),
        })
    counts: dict = {}
    disp: dict = {}
    for f in files:
        counts[f["class"]] = counts.get(f["class"], 0) + 1
        disp[f["final_disposition"]] = disp.get(f["final_disposition"], 0) + 1
    cuts = dependency_cuts(files)
    return {
        "schema": SCHEMA,
        "baseline_commit": BASELINE_COMMIT,
        "label": ("Every file tracked at the baseline commit, classified by "
                  "the rules in tools/repo_inventory.py, with dependencies "
                  "measured from the baseline bytes and a final disposition "
                  "recomputed from the current tree. Git history is not "
                  "rewritten: a REMOVED file remains readable at the "
                  "baseline commit."),
        "classes": CLASSES,
        "dispositions": list(DISPOSITIONS),
        "counts": {"files": len(files),
                   "by_class": dict(sorted(counts.items())),
                   "by_final_disposition": dict(sorted(disp.items())),
                   "cut_imports": sum(c["kind"] == "import" for c in cuts),
                   "cut_references": sum(c["kind"] == "reference"
                                         for c in cuts)},
        "dependency_cuts": cuts,
        "files": files,
    }


#: Classes whose file stays at the baseline path or moves with its content:
#: what such a file imported or named among the REMOVED files is a
#: dependency the transformation had to cut.
_KEPT = ("PRESERVE", "REGENERATE", "REVIEW_DATA_PROVENANCE",
         "EXTRACT_GENERIC")


def dependency_cuts(files: list) -> list:
    """Every edge from a kept file to a removed one, by kind."""
    cls = {f["path"]: f["class"] for f in files}
    out = []
    for f in files:
        if f["class"] not in _KEPT:
            continue
        for kind, key in (("import", "imports"), ("reference", "references")):
            for q in f[key]:
                if cls.get(q) in ("REMOVE", "REIMPLEMENT"):
                    out.append({"from": f["path"], "to": q, "kind": kind})
    return out


def render(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False) \
        + "\n"


def problems(doc: dict, tree: set, root: Path = ROOT,
             applied: bool = False) -> list:
    """Why the committed inventory does not describe this tree, or []."""
    out = []
    if doc.get("schema") != SCHEMA:
        out.append(f"schema {doc.get('schema')!r} != {SCHEMA!r}")
    if doc.get("baseline_commit") != BASELINE_COMMIT:
        out.append("baseline commit differs from the tool's")
    seen = set()
    counts: dict = {}
    disp: dict = {}
    for f in doc.get("files", []):
        p = f["path"]
        if p in seen:
            out.append(f"{p}: listed twice")
        seen.add(p)
        try:
            cls, why, action = classify(p)
        except KeyError as e:
            out.append(str(e))
            continue
        if (f["class"], f["rationale"], f["action"]) != (cls, why, action):
            out.append(f"{p}: classification differs from the rules")
        if f["destinations"] != _dest(p):
            out.append(f"{p}: destinations differ from the rules")
        if not f["rationale"].strip() or not f["action"].strip():
            out.append(f"{p}: no rationale or action")
        now = final_disposition(p, cls, f["destinations"], f["blob"], tree,
                                root)
        if f["final_disposition"] != now:
            out.append(f"{p}: final_disposition {f['final_disposition']} "
                       f"but the tree says {now}")
        if applied and now == "PENDING":
            out.append(f"{p}: {cls} not yet applied ({f['action']})")
        counts[f["class"]] = counts.get(f["class"], 0) + 1
        disp[now] = disp.get(now, 0) + 1
    cuts = dependency_cuts(doc.get("files", []))
    if doc.get("dependency_cuts") != cuts:
        out.append("dependency_cuts differ from the files' dependencies")
    want = {"files": len(seen), "by_class": dict(sorted(counts.items())),
            "by_final_disposition": dict(sorted(disp.items())),
            "cut_imports": sum(c["kind"] == "import" for c in cuts),
            "cut_references": sum(c["kind"] == "reference" for c in cuts)}
    if doc.get("counts") != want:
        out.append(f"counts {doc.get('counts')} != recomputed {want}")
    return out


def derive(root: Path = ROOT) -> dict:
    listing = baseline_listing()
    blobs = baseline_blobs(listing)
    return build(listing, derive_dependencies(listing, blobs),
                 current_tree(root), root)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("derive", "check", "verify-derivation"))
    ap.add_argument("--applied", action="store_true",
                    help="check: also refuse any PENDING disposition")
    a = ap.parse_args(argv)
    path = ROOT / OUT
    if a.mode == "derive":
        doc = derive()
        path.write_text(render(doc), encoding="utf-8")
        print(f"wrote {OUT}: {doc['counts']}")
        return 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    if a.mode == "verify-derivation":
        fresh = render(derive())
        if fresh != path.read_text(encoding="utf-8"):
            print("INVENTORY DRIFT: derive from the baseline commit does not "
                  "reproduce the committed inventory")
            return 1
        print(f"INVENTORY DERIVES: {doc['counts']['files']} baseline files "
              f"reproduced from {BASELINE_COMMIT[:7]}")
        return 0
    found = problems(doc, current_tree(), ROOT, applied=a.applied)
    for p in found:
        print(f"PROBLEM: {p}")
    if found:
        return 1
    print(f"INVENTORY OK: {doc['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
