# QTA continuation state

Captured 2026-09-03T18:03:32+00:00. This is a Step-1 continuation record. It establishes the Git, PR, worktree, and repository boundary only. It does not recover a new checkpoint, integrate a scientific model, or promote any artifact's authority.

## Authority and claim boundary

Exports and the recovery corpus remain recovery authority. The repository remains implementation authority. Throughout this record:

`discussed ≠ specified ≠ implemented ≠ integrated ≠ exercised ≠ verified ≠ accepted ≠ authoritative`

QTA remains theoretical, forecast/simulation-only, pre-hardware, validation-gated, and at scientific `PASS=0`. The locked A/B/C/D mode and methane/helium separation rules were not changed.

## Repository and active continuation checkout

| Field | Exact state at capture |
|---|---|
| Repository | `cakeisalie89/Quantum-Thermal-` |
| Active Step-1 repository root | `/workspace/scratch/12784d09e747/QTA_RECOVERY_PR16` |
| Worktree type | Linked, detached worktree placed at the PR head |
| Worktree Git dir | `/workspace/scratch/cdd815e2492a/QTA_CURRENT_MAIN/.git/worktrees/QTA_RECOVERY_PR16` |
| Common Git dir | `/workspace/scratch/cdd815e2492a/QTA_CURRENT_MAIN/.git` |
| Original standard checkout | `/workspace/scratch/cdd815e2492a/QTA_CURRENT_MAIN` |
| Default remote | `origin` |
| Fetch/push URL | `https://github.com/cakeisalie89/Quantum-Thermal-.git` |
| Authoritative base | `main` at `096fb90fff089a62e4b20513de729a3e45a69f35` |
| Base tree | `f810e9d8580edafe81fbe3956ba85c75d4c10c01` |
| Local base / remote base | both `096fb90fff089a62e4b20513de729a3e45a69f35` |
| Recovery branch | `qta-complete-corpus-recovery` |
| PR recovery head at capture | `e7d74550c8cd3b725f7d4363e1b056662ab39a3c` |
| PR recovery tree at capture | `a27aef5d209c9064e7206803d71209d4c56c2ae4` |
| Merge base | `096fb90fff089a62e4b20513de729a3e45a69f35` |
| PR lineage ahead / behind base | 4 / 0 |
| Local recovery-branch upstream | `NONE` |
| Tags on GitHub remote | none |

The active PR worktree matched the remote PR head and was clean before Step-1 files were created.

## Critical branch divergence

A local branch with the same name as the PR source branch already existed at `06a171aa4fcce0fe2a19add39f9fd0c7f844aa59`. It is not the PR head.

| Comparison | State |
|---|---|
| Local same-named branch | `06a171aa4fcce0fe2a19add39f9fd0c7f844aa59` |
| Remote/PR branch | `e7d74550c8cd3b725f7d4363e1b056662ab39a3c` |
| Common ancestor | `096fb90fff089a62e4b20513de729a3e45a69f35` |
| Local-only commits | 3 |
| PR-only commits | 4 |
| Relationship | `DIVERGED` |

No branch was reset, rebased, merged, renamed, or force-updated. The three local-only commits were captured in an exact Git bundle under `docs/recovery/evidence/` for later disposition.

## Pull request

| Field | State at capture |
|---|---|
| PR | [#16](https://github.com/cakeisalie89/Quantum-Thermal-/pull/16) |
| Title | QTA total-state forensic recovery and reconstruction |
| State | Open, draft |
| Source → target | `qta-complete-corpus-recovery` → `main` |
| Head SHA | `e7d74550c8cd3b725f7d4363e1b056662ab39a3c` |
| Base SHA | `096fb90fff089a62e4b20513de729a3e45a69f35` |
| Commits | 4 |
| Changed files | 4 |
| Diff | +464 / -0 |
| Mergeability reported by GitHub | `clean` |
| Reviews / review comments / issue comments | 0 / 0 / 0 |
| Unresolved review threads | 0 |
| Checks | `stack-verify (core)`: success; `stack-verify (full)`: success |

Those checks apply to `e7d74550c8cd3b725f7d4363e1b056662ab39a3c`; they are software/workflow evidence, not scientific acceptance.

## Recovery commits already on PR #16

| SHA | Parent | Timestamp | Subject | Files | Evidence status |
|---|---|---|---|---:|---|
| `28850d095886c7e9c58bd730a49795ee67e74b4b` | `096fb90fff089a62e4b20513de729a3e45a69f35` | 2026-09-03T12:52:03-04:00 | recovery: capture current main and forensic input baseline | 1 | DOCUMENTARY_ONLY |
| `774f58fa95aff730935e453990e08e3bac438f97` | `28850d095886c7e9c58bd730a49795ee67e74b4b` | 2026-09-03T12:54:03-04:00 | recovery: seed export repository reconciliation ledger | 1 | DOCUMENTARY_ONLY |
| `8e268a7e97966b42062944d0a9d3835abe711cff` | `774f58fa95aff730935e453990e08e3bac438f97` | 2026-09-03T13:11:50-04:00 | recovery: record CP-M11 465 exact-recovery boundary | 1 | RECORDED_HISTORICAL_AND_REPLAY_EVIDENCE; NOT_FRESHLY_RERUN_STEP_1 |
| `e7d74550c8cd3b725f7d4363e1b056662ab39a3c` | `8e268a7e97966b42062944d0a9d3835abe711cff` | 2026-09-03T13:12:36-04:00 | recovery: record CP-M12 controlled source reconstruction | 1 | RECORDED_REPLAY_RESULTS; NOT_FRESHLY_RERUN_STEP_1 |

The latest completed PR tranche before Step 1 is the documentary CP-M12 controlled source reconstruction at `e7d74550c8cd3b725f7d4363e1b056662ab39a3c`. Its commit records replay evidence; Step 1 did not independently reproduce the historical CP-M12 Git object or rerun the full historical verification set.

## Separate local-only committed recovery lineage

| SHA | Parent | Timestamp | Subject | Files / lines |
|---|---|---|---|---|
| `7aab477397b19d9f723b7d91f7a05c04c47120a6` | `096fb90fff089a62e4b20513de729a3e45a69f35` | 2026-09-03T12:12:16Z | recovery: restore CP-M3 through CP-M22 authority layers | 110; +15849/-64 |
| `5b41f80d6f1311971b7faba684316b1aa2cb7b25` | `7aab477397b19d9f723b7d91f7a05c04c47120a6` | 2026-09-03T12:15:27Z | recovery: enforce mechanical authority and restore structural kernels | 22; +1852/-22 |
| `06a171aa4fcce0fe2a19add39f9fd0c7f844aa59` | `5b41f80d6f1311971b7faba684316b1aa2cb7b25` | 2026-09-03T12:30:54Z | recovery: integrate governed mechanical graph and Stage-8 provenance | 44; +6236/-166 |

Combined against `main`, this lineage changes 175 files (+23,937/-252). It is valuable committed work, but it is `NOT_INTEGRATED` into PR #16 and was not scientifically reviewed or accepted in Step 1.

## Current working-tree state

### PR-head continuation worktree

Before Step-1 documentation/evidence was written:

- staged: none;
- unstaged: none;
- untracked: none;
- conflicts: none;
- deleted tracked files: none;
- submodule changes: none;
- ignored recovery-relevant files: none.

Only the Step-1 state files and quarantined preservation evidence were subsequently added for the dedicated Step-1 commit.

### Original same-named local branch checkout

`/workspace/scratch/cdd815e2492a/QTA_CURRENT_MAIN` is clean at `06a171aa4fcce0fe2a19add39f9fd0c7f844aa59`. It contains the three local-only commits above and has no upstream. It remains untouched.

### CP-M11 staged recovery checkout

| Field | Exact state |
|---|---|
| Path | `/workspace/scratch/cdd815e2492a/QTA_CP_M11_RECOVERED` |
| Branch / HEAD | `qta-recovery-cp-m11` / `09b39da3a91c55a13dc2ff7c2c02e2c14b6c42f1` |
| HEAD tree | `8e6fdd890db04d36abbb2d8d365f5ac7a21e33dd` |
| Staged tree | `0111f7949b961ddea201694c53dd902465160521` |
| Tracked files | 446 |
| Staged | 46 (21 added, 25 modified) |
| Unstaged / untracked | 0 / 0 |
| Full-index staged-diff SHA-256 | `cb4b05d84a11844a6c06e22548696e5b6645d73c49cfbe246c2edfc81326455e` |
| `final_manifest.json` SHA-256 | `6b15afc276e0e9f31e621c0d0195b94a179b9733380acbf00582afbe0d1f940c` |
| Classification | `EXACT_446_INTERMEDIATE; VALUABLE; NOT_THE_465_TARGET; NOT_INTEGRATED` |

This is an exact 446-file intermediate, not the missing 465-file target. It is newer than the authenticated 437-file state emphasized by the current PR continuation documents. It remains staged and uncommitted in its original checkout. The complete full-index diff is preserved as compressed base64 evidence, but none of its scientific changes was applied to the PR branch.

Every staged path follows. SHA-256, byte size, type, subsystem, and disposition for each path are in `STEP_1_CURRENT_RECOVERY_STATE.json`.

#### cross-cutting authority ledger (7)

```text
STAGED	ARCHITECTURE_GAP_REGISTER.csv	registry_report_or_generated_artifact
STAGED	AUTHORITIES.md	documentation_or_recovery_evidence
STAGED	COUPLING_LEDGER_NOTES.md	documentation_or_recovery_evidence
STAGED	QTA_CP_M11_AUTHORITY_TRANSITION_LEDGER.csv	registry_report_or_generated_artifact
STAGED	authorities.json	registry_report_or_generated_artifact
STAGED	cp_m11_inline_geometry_authority.json	registry_report_or_generated_artifact
STAGED	tests/test_cp_m11_inline_geometry_authority.py	test_source
```

#### CP-M32 program-state reconciliation (1)

```text
STAGED	IMPLEMENTATION_STATUS.json	registry_report_or_generated_artifact
```

#### cross-cutting late-checkpoint replay (7)

```text
STAGED	QTA_CP_M11_CLOSURE_READINESS_MATRIX.csv	registry_report_or_generated_artifact
STAGED	QTA_CP_M11_CONTROLLED_DESIGN_REQUIREMENTS.csv	registry_report_or_generated_artifact
STAGED	QTA_CP_M11_GAMMA_ENGINEERING_REQUEST.md	documentation_or_recovery_evidence
STAGED	QTA_CP_M11_INLINE_GEOMETRY_EXECUTION_REPORT.md	documentation_or_recovery_evidence
STAGED	QTA_CP_M11_SYMBOLIC_AXIAL_STACK.csv	registry_report_or_generated_artifact
STAGED	tests/test_evaluation_paths.py	test_source
STAGED	validate_cp_m11_inline_geometry.py	generator_or_verifier_source
```

#### CP-M11 parameter-screening authority (15)

```text
STAGED	build_evaluation_coverage_matrix.py	generator_or_verifier_source
STAGED	qta_multiphysics/screening/run.py	scientific_or_governance_source
STAGED	sa_coverage_matrix.csv	registry_report_or_generated_artifact
STAGED	sa_coverage_matrix.json	registry_report_or_generated_artifact
STAGED	sa_exclusion_report.csv	registry_report_or_generated_artifact
STAGED	sa_failed_samples.json	registry_report_or_generated_artifact
STAGED	sa_morris_design.json	registry_report_or_generated_artifact
STAGED	sa_morris_ranking.csv	registry_report_or_generated_artifact
STAGED	sa_morris_results.json	registry_report_or_generated_artifact
STAGED	sa_parameter_inventory.json	registry_report_or_generated_artifact
STAGED	sa_screening_summary.json	registry_report_or_generated_artifact
STAGED	sa_sobol_indices.json	registry_report_or_generated_artifact
STAGED	sa_sobol_selection.json	registry_report_or_generated_artifact
STAGED	sa_summary.md	documentation_or_recovery_evidence
STAGED	tests/test_parameter_screening.py	test_source
```

#### CP-M11 P5 vacuum-surface / inline-geometry (12)

```text
STAGED	build_p5_vacuum_surface.py	generator_or_verifier_source
STAGED	p5_g040_reclassification.csv	registry_report_or_generated_artifact
STAGED	p5_inspection_report.json	registry_report_or_generated_artifact
STAGED	qta_multiphysics/vacuum_surface/__init__.py	scientific_or_governance_source
STAGED	qta_multiphysics/vacuum_surface/contract.py	scientific_or_governance_source
STAGED	qta_multiphysics/vacuum_surface/model.py	scientific_or_governance_source
STAGED	region_inventory_timeseries.csv	registry_report_or_generated_artifact
STAGED	tests/test_vacuum_surface.py	test_source
STAGED	vacuum_benchmark_report.csv	registry_report_or_generated_artifact
STAGED	vacuum_observable_registry.json	registry_report_or_generated_artifact
STAGED	vacuum_parameter_contract.csv	registry_report_or_generated_artifact
STAGED	vacuum_transport_summary.json	registry_report_or_generated_artifact
```

#### manifest / RO-Crate provenance (3)

```text
STAGED	final_manifest.json	provenance_or_integrity_artifact
STAGED	manifest_hash.txt	detached_integrity_record
STAGED	ro-crate/ro-crate-metadata.json	provenance_or_integrity_artifact
```

#### FSM authority and safety evidence (1)

```text
STAGED	fsm_safety_invariants.json	registry_report_or_generated_artifact
```


### Late-checkpoint replay checkout

| Field | Exact state |
|---|---|
| Path | `/workspace/scratch/cdd815e2492a/QTA_LATE_CHECKPOINT_REPLAY` |
| HEAD | detached at `06a171aa4fcce0fe2a19add39f9fd0c7f844aa59` |
| HEAD tree | `a6c1a0dc88d3efa0658866a4bbb0d650933b18f2` |
| Tracked files | 555 |
| Staged | 0 |
| Unstaged modified | 10 |
| Untracked nonignored | 141 (476426 bytes) |
| Ignored paths | 142, including 90 generated `outputs/` files |
| Tracked full-index diff SHA-256 | `bccec006c00f92ec6e716ab06910790f121cc6b3c7b6820bad4fddcc12bb1733` |
| Untracked inventory digest | `4ec4e36e9dec4fbad39c08c4d8262646d5f39454cc5d4a697fe39411b931ff5d` |
| Classification | `VALUABLE_BUT_MULTI_TRANCHE; CURRENT_TRANCHE_NOT_YET_ESTABLISHED; NOT_INTEGRATED` |

The files span multiple checkpoint/review topics. Commit completeness and acceptance are not established, so the correct current label is:

`CURRENT_TRANCHE_NOT_YET_ESTABLISHED`

Every unstaged tracked path follows.

#### cross-cutting authority ledger (3)

```text
UNSTAGED_MODIFIED	AUTHORITIES.md	documentation_or_recovery_evidence
UNSTAGED_MODIFIED	authorities.json	registry_report_or_generated_artifact
UNSTAGED_MODIFIED	tests/test_g062b_surface_authority.py	test_source
```

#### coupled-mode canonical/reporting surface (5)

```text
UNSTAGED_MODIFIED	coupled_mode_recovery_metrics.csv	registry_report_or_generated_artifact
UNSTAGED_MODIFIED	coupled_mode_state_summary.json	registry_report_or_generated_artifact
UNSTAGED_MODIFIED	multiphysics_summary.json	registry_report_or_generated_artifact
UNSTAGED_MODIFIED	qta_multiphysics/coupled_mode_solver.py	scientific_or_governance_source
UNSTAGED_MODIFIED	results_gate_table.csv	registry_report_or_generated_artifact
```

#### cross-cutting late-checkpoint replay (1)

```text
UNSTAGED_MODIFIED	qta_full_sim.py	scientific_or_governance_source
```

#### surface-model interpretation authority (1)

```text
UNSTAGED_MODIFIED	surface_model_p13_interpretation.csv	registry_report_or_generated_artifact
```


Every nonignored untracked path follows.

#### cross-cutting authority ledger (3)

```text
UNTRACKED	ARCHITECTURE_GAP_REGISTER.csv	registry_report_or_generated_artifact
UNTRACKED	COUPLING_LEDGER_NOTES.md	documentation_or_recovery_evidence
UNTRACKED	surface_decision_authority_record.json	registry_report_or_generated_artifact
```

#### CP-M32 program-state reconciliation (4)

```text
UNTRACKED	IMPLEMENTATION_STATUS.json	registry_report_or_generated_artifact
UNTRACKED	build_cp_m32_program_state.py	generator_or_verifier_source
UNTRACKED	tests/test_cp_m32_program_state.py	test_source
UNTRACKED	verification_count_reconciliation.json	registry_report_or_generated_artifact
```

#### CP-M23 surface-literature and interpretation review (16)

```text
UNTRACKED	build_cp_m23_literature_review.py	generator_or_verifier_source
UNTRACKED	cp_m23_access_record.json	registry_report_or_generated_artifact
UNTRACKED	cp_m23_candidate_sources.csv	registry_report_or_generated_artifact
UNTRACKED	cp_m23_rejected_sources.csv	registry_report_or_generated_artifact
UNTRACKED	cp_m23_search_ledger.csv	registry_report_or_generated_artifact
UNTRACKED	cp_m23_verified_sources.csv	registry_report_or_generated_artifact
UNTRACKED	surface_model_10mK_literature_assessment.json	registry_report_or_generated_artifact
UNTRACKED	surface_model_exact_match_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	surface_review_brief.md	documentation_or_recovery_evidence
UNTRACKED	surface_review_engagement_protocol.json	registry_report_or_generated_artifact
UNTRACKED	surface_review_evidence_pack.json	registry_report_or_generated_artifact
UNTRACKED	surface_review_expertise_profile.csv	registry_report_or_generated_artifact
UNTRACKED	surface_review_questions.csv	registry_report_or_generated_artifact
UNTRACKED	surface_review_readiness_gate.csv	registry_report_or_generated_artifact
UNTRACKED	surface_review_response_schema.json	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m23_literature.py	test_source
```

#### CP-M25 experiment requirements and planning (15)

```text
UNTRACKED	build_cp_m25_work_package.py	generator_or_verifier_source
UNTRACKED	cp_m25_experiment_requirements_source.json	registry_report_or_generated_artifact
UNTRACKED	experimental_acceptance_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	experimental_budget_scenarios.json	registry_report_or_generated_artifact
UNTRACKED	experimental_cost_model.csv	registry_report_or_generated_artifact
UNTRACKED	experimental_cost_source_ledger.csv	registry_report_or_generated_artifact
UNTRACKED	experimental_data_contract.json	registry_report_or_generated_artifact
UNTRACKED	experimental_executive_summary.md	documentation_or_recovery_evidence
UNTRACKED	experimental_funding_milestones.csv	registry_report_or_generated_artifact
UNTRACKED	experimental_ingestion_firewall.json	registry_report_or_generated_artifact
UNTRACKED	experimental_risk_register.csv	registry_report_or_generated_artifact
UNTRACKED	experimental_safety_review_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	experimental_stage_gate_plan.csv	registry_report_or_generated_artifact
UNTRACKED	experimental_work_breakdown_structure.csv	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m25_work_package.py	test_source
```

#### CP-M26 M1 facility/quote planning (16)

```text
UNTRACKED	build_cp_m26_m1_package.py	generator_or_verifier_source
UNTRACKED	cp_m26_blocker_reconciliation.json	registry_report_or_generated_artifact
UNTRACKED	m1_candidate_facilities.csv	registry_report_or_generated_artifact
UNTRACKED	m1_capability_grading.csv	registry_report_or_generated_artifact
UNTRACKED	m1_capability_inquiry.md	documentation_or_recovery_evidence
UNTRACKED	m1_capability_specification.csv	registry_report_or_generated_artifact
UNTRACKED	m1_correspondence_provenance_schema.json	registry_report_or_generated_artifact
UNTRACKED	m1_methane_isotope_decision.csv	registry_report_or_generated_artifact
UNTRACKED	m1_outreach_selection.json	registry_report_or_generated_artifact
UNTRACKED	m1_quote_normalization_schema.json	registry_report_or_generated_artifact
UNTRACKED	m1_request_for_quotation.md	documentation_or_recovery_evidence
UNTRACKED	m1_stage_gate_refinement.csv	registry_report_or_generated_artifact
UNTRACKED	m1_surface_decision_gate.json	registry_report_or_generated_artifact
UNTRACKED	m1_surface_definition_record.json	registry_report_or_generated_artifact
UNTRACKED	m1_surface_option_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m26_m1_package.py	test_source
```

#### CP-M27 scope/review correction (3)

```text
UNTRACKED	build_cp_m27_review_package.py	generator_or_verifier_source
UNTRACKED	cp_m27_scope_correction.json	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m27_review_package.py	test_source
```

#### CP-M28 model-path audit (8)

```text
UNTRACKED	build_cp_m28_path_audit.py	generator_or_verifier_source
UNTRACKED	model_path_10mK_assessment.json	registry_report_or_generated_artifact
UNTRACKED	model_path_audit_summary.json	registry_report_or_generated_artifact
UNTRACKED	model_path_authority_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	model_path_distinctions.csv	registry_report_or_generated_artifact
UNTRACKED	model_path_inventory.json	registry_report_or_generated_artifact
UNTRACKED	model_path_source_ledger.csv	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m28_path_audit.py	test_source
```

#### CP-M29 characteristic-length authority (6)

```text
UNTRACKED	build_cp_m29_char_length.py	generator_or_verifier_source
UNTRACKED	characteristic_length_assessment.json	registry_report_or_generated_artifact
UNTRACKED	characteristic_length_conflict.json	registry_report_or_generated_artifact
UNTRACKED	characteristic_length_inventory.csv	registry_report_or_generated_artifact
UNTRACKED	cp_m29_prior_statement_correction.json	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m29_char_length.py	test_source
```

#### CP-M30 / D9 authority and disclosure audit (11)

```text
UNTRACKED	build_cp_m30_d9_audit.py	generator_or_verifier_source
UNTRACKED	d9_audit_summary.json	registry_report_or_generated_artifact
UNTRACKED	d9_change_impact_analysis.json	registry_report_or_generated_artifact
UNTRACKED	d9_dependency_chain.csv	registry_report_or_generated_artifact
UNTRACKED	d9_disclosure_correction_record.json	registry_report_or_generated_artifact
UNTRACKED	d9_gate_definition.json	registry_report_or_generated_artifact
UNTRACKED	d9_robustness_assessment.json	registry_report_or_generated_artifact
UNTRACKED	d9_sensitivity_analysis.csv	registry_report_or_generated_artifact
UNTRACKED	d9_status_and_disclosure_audit.json	registry_report_or_generated_artifact
UNTRACKED	p5_p13_clarification.json	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m30_d9_audit.py	test_source
```

#### CP-M34 vibration semantics (21)

```text
UNTRACKED	build_cp_m34_vib_semantics.py	generator_or_verifier_source
UNTRACKED	tests/test_cp_m34_vib_semantics.py	test_source
UNTRACKED	vibration_benchmark_report.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_ceff_dimensional_audit.json	registry_report_or_generated_artifact
UNTRACKED	vibration_decay_semantics.json	registry_report_or_generated_artifact
UNTRACKED	vibration_excitation_authority.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_falsification_report.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_frequency_contract.json	registry_report_or_generated_artifact
UNTRACKED	vibration_literal_audit.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_metric_falsification.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_metric_interpretation_guard.json	registry_report_or_generated_artifact
UNTRACKED	vibration_metric_validation.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_model_inventory.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_nv_handoff.json	registry_report_or_generated_artifact
UNTRACKED	vibration_observable_registry.json	registry_report_or_generated_artifact
UNTRACKED	vibration_p13_readiness.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_parameter_contract.csv	registry_report_or_generated_artifact
UNTRACKED	vibration_power_model_authority.json	registry_report_or_generated_artifact
UNTRACKED	vibration_q_convention_audit.json	registry_report_or_generated_artifact
UNTRACKED	vibration_quantity_vocabulary.json	registry_report_or_generated_artifact
UNTRACKED	vibration_settling_authority.json	registry_report_or_generated_artifact
```

#### CP-M35 dwell audit (2)

```text
UNTRACKED	build_cp_m35_dwell_audit.py	generator_or_verifier_source
UNTRACKED	tests/test_cp_m35_dwell_audit.py	test_source
```

#### CP-M36 FSM dwell authority (20)

```text
UNTRACKED	build_cp_m36_dwell_authority.py	generator_or_verifier_source
UNTRACKED	fsm_dwell_authority_slots.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_concept_inventory.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_correction_decision.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_dependency_graph.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_falsification.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_guard_classification.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_invalid_domain_contract.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_invalid_input_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_operand_audit.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_option_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_programme_owner_decision.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_provenance_audit.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_purpose_audit.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_structural_dependency_map.json	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_truth_table.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_dwell_validation.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_elapsed_time_inventory.csv	registry_report_or_generated_artifact
UNTRACKED	fsm_legacy_guard_disposition.json	registry_report_or_generated_artifact
UNTRACKED	tests/test_cp_m36_dwell_authority.py	test_source
```

#### G-061 schema migration (5)

```text
UNTRACKED	build_g061_migration.py	generator_or_verifier_source
UNTRACKED	legacy_v1_key_projection.json	registry_report_or_generated_artifact
UNTRACKED	qta_multiphysics/schema_migration/v1_to_v2.py	scientific_or_governance_source
UNTRACKED	schema_migration_v1_to_v2.json	registry_report_or_generated_artifact
UNTRACKED	tests/test_g061_schema_migration.py	test_source
```

#### P8 structural audit (5)

```text
UNTRACKED	build_p8_structural_audit.py	generator_or_verifier_source
UNTRACKED	p8_structural_source_inventory.json	registry_report_or_generated_artifact
UNTRACKED	structural_capability_matrix.csv	registry_report_or_generated_artifact
UNTRACKED	structural_identity_graph.json	registry_report_or_generated_artifact
UNTRACKED	tests/test_p8_structural_audit.py	test_source
```

#### CP-M33 scope correction (1)

```text
UNTRACKED	cp_m33_scope_correction.json	registry_report_or_generated_artifact
```

#### cross-cutting late-checkpoint replay (2)

```text
UNTRACKED	p13_count_scope_clarification.json	registry_report_or_generated_artifact
UNTRACKED	verify_program_suites.py	generator_or_verifier_source
```

#### surface-model interpretation authority (2)

```text
UNTRACKED	surface_model_p13_counts.json	registry_report_or_generated_artifact
UNTRACKED	surface_model_parameter_ingestion_decisions.csv	registry_report_or_generated_artifact
```

#### CP-M31 disclosure (1)

```text
UNTRACKED	tests/test_cp_m31_disclosure.py	test_source
```


Ignored generated outputs and caches were not imported. Their counts and aggregate digests are recorded in the machine-readable state. The tracked diff and all 141 nonignored untracked paths were captured as quarantined evidence.

## Preservation evidence

| Artifact | Encoded SHA-256 | Decoded identity | Purpose |
|---|---|---|---|
| `docs/recovery/evidence/STEP_1_LOCAL_DIVERGENT_06A171_GIT_BUNDLE.gz.b64` | `9234f474d9b1acfff575376feb102f1a7867f8cce7104721a33aa2052a6a83c2` | `324d1bf1a75d49474e9c6f7862ea24ccc39788e7c36bf81bb1ba0acc80481ce9` | Exact Git bundle of three local-only recovery commits; prerequisite base 096fb90. |
| `docs/recovery/evidence/STEP_1_CP_M11_446_STAGED_FULL_INDEX.patch.gz.b64` | `f638bade3ba18fbdb8707e4e274e3747c175667628109a55b5bbb8ac635f3641` | `cb4b05d84a11844a6c06e22548696e5b6645d73c49cfbe246c2edfc81326455e` | Exact compressed full-index staged diff for the 446-file CP-M11 intermediate. |
| `docs/recovery/evidence/STEP_1_LATE_REPLAY_TRACKED_FULL_INDEX.patch.gz.b64` | `6e6500238e2f736ee8857e50625ad2ff912f6d8b713477fb33a335cfb4849119` | `bccec006c00f92ec6e716ab06910790f121cc6b3c7b6820bad4fddcc12bb1733` | Exact compressed full-index diff for 10 unstaged tracked late-replay paths. |
| `docs/recovery/evidence/STEP_1_LATE_REPLAY_UNTRACKED.tar.gz.b64` | `1f4af0d99e8d7269ee0309c2b425d06ffe5572100752229608ee7c750f1750c2` | `2b40700cf19e290e1919934c0084f8f07c876ebba53d1652d5b0f3ced119a659` | Deterministic tar capture of all 141 nonignored untracked late-replay paths. |

These files are evidence containers only. Their contents are not implementation authority and must not be applied without the next step's explicit classification/disposition decision.

## Existing recovery documents

| Document | State at capture | Latest commit |
|---|---|---|
| `docs/recovery/BASELINE_STATE.md` | PRESENT | `28850d095886c7e9c58bd730a49795ee67e74b4b` |
| `docs/recovery/CONTINUATION_STATE.md` | ABSENT_AT_CAPTURE | — |
| `docs/recovery/EXPORT_REPOSITORY_RECOVERY_LEDGER.md` | PRESENT | `774f58fa95aff730935e453990e08e3bac438f97` |
| `docs/recovery/CP_M11_465_RECOVERY_STATUS.md` | PRESENT | `8e268a7e97966b42062944d0a9d3835abe711cff` |
| `docs/recovery/CP_M12_CONTROLLED_RECONSTRUCTION.md` | PRESENT | `e7d74550c8cd3b725f7d4363e1b056662ab39a3c` |
| `docs/recovery/CHECKPOINT_RECONSTRUCTION.md` | ABSENT_AT_CAPTURE | — |
| `docs/recovery/LOST_WORKTREE_RECONSTRUCTION.md` | ABSENT_AT_CAPTURE | — |
| `docs/recovery/HARDWARE_AUTHORITY.md` | ABSENT_AT_CAPTURE | — |
| `docs/recovery/UNRESOLVED_AUTHORITY.md` | ABSENT_AT_CAPTURE | — |
| `docs/recovery/FINAL_VERIFICATION.md` | ABSENT_AT_CAPTURE | — |
| `docs/recovery/STEP_1_CURRENT_RECOVERY_STATE.json` | CREATED_BY_STEP_1 | — |

The committed CP-M11 status document is incomplete as a live-continuation locator because an exact 446-file staged intermediate is now locally present. Its historical 437/465 statements were not rewritten in Step 1; this continuation record supplies the current Git fact without changing scientific conclusions.

## Ledger position

- Seeded ledger orders: 0 through 82.
- Highest seeded order: 82, `CURRENT-MAIN-BASELINE`.
- Latest checkpoint with a PR evidence tranche: CP-M12.
- Explicit special/blocking records include CP-M41R4 (evidence gap), CP-M46R1 (historical uncommitted salvage only), the missing exact 465-file CP-M11 candidate, and the quarantined corrupt/truncated whole-corpus TAR.
- The ledger has not been expanded with new historical recovery claims in Step 1.
- Local-only commits and dirty replay files have not been marked recovered merely because bytes exist.

## Repository scale at capture

| Measure | Count |
|---|---:|
| Tracked files | 420 |
| Directories represented by tracked paths | 22 |
| Python/Rust source files | 178 |
| Test files | 53 |
| Recovery files | 4 |
| Changed files against `main` | 4 |
| HDF5 governed mappings | 88 |
| Canonical generated-set contract | 89 |
| `final_manifest.json` entries | 414 |

Historical file/test counts remain comparison anchors only.

## Current project-artifact inventory

Present at PR head:

- `qta_full_sim.py`
- `results_gate_table.csv`
- `monte_carlo_summary.csv`
- `best_forecast_operating_point.json`
- `assumed_parameters.json`
- `failed_gate_samples.csv`
- `final_manifest.json`
- `manifest_hash.txt`
- `pyproject.toml`
- `uv.lock`
- `Snakefile`
- `qta_scientific_results.h5`
- `ro-crate/ro-crate-metadata.json`
- `QTA_stage9_release_verification/sbom.cdx.json`
- `QTA_stage9_release_verification/provenance.intoto.json`
- `verify_release.py`
- `parameter_registry.csv`
- `monte_carlo_parameter_registry.csv`
- `deep_model_manifest.json`
- `design_component_registry.json`

Expected-name inventory findings:

- `best_operating_point.json`: absent under this exact name
- `output_sync_report.txt`: absent under this exact name
- `geometry_registry.json`: absent under this exact name
- `model_registry.json`: absent under this exact name

No absence was converted into historical nonexistence. Current alternatives were recorded without silently equating them.

## Environment at capture

- Ubuntu 24.04.3 LTS, Linux 6.18.35, x86_64.
- Shell Python: `/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python`, 3.12.13.
- Active virtual environment: none.
- `uv`: 0.11.33.
- Git: 2.51.1.
- Docker/Podman/nerdctl/Apptainer/Singularity: unavailable.
- `uv.lock`: present, 153054 bytes, SHA-256 `fc319fc32d27d82e1f0ee213288e9c6567da980881c93c27501b6a9ce9ef4dfc`.
- Environment synchronization: `NOT_SYNCHRONIZED`. The offline frozen check reported that a project `.venv` and 22 packages would need to be created/installed; it did not modify the environment.
- System packages do not match the lock: NumPy 2.3.5 vs locked 2.4.4, SciPy 1.17.0 vs locked 1.17.1; QuTiP, h5py, pytest, Hypothesis, Ruff, mypy, and Snakemake are absent.
- The ignored sibling `.venv` has dangling Python links and is not accepted as the current environment.

No dependency was installed, upgraded, or downgraded.

## Verification surface

Available primary commands are recorded in the machine-readable state. The intended serial full route is:

```sh
uv run python qta_full_sim.py
uv run python package_consistency_check.py
uv run python stage6_preservation_check.py
uv run python generate_manifest.py --check
uv run python -m pytest tests/ -q
```

The repository also defines `full_verification`, `s8_full`, and `s10_full` Snakemake targets. Named CP-M8, CP-M9, CP-M11, and later CP-M suites are not present at this PR head.

Fresh Step-1 read-only observations:

- manifest: 414 entries, detached hash matches, 0 missing, 0 mismatched;
- Stage-6 semantic preservation: 41 required invariants reported preserved; archive byte lineage explicitly not verified;
- PR-head Stage-10 checks: core success and full success;
- full pytest, package regeneration/checker, HDF5 equivalence, and RO-Crate validation: `NOT_RUN_STEP_1`.

A probe showed that `generate_manifest.py --help` is not side-effect-free and regenerated the manifest. The exact PR-head bytes of both affected files were immediately restored and verified with zero Git diff. This trap is now documented; no canonical output drift remains.

## Additional Git forensics

- No stashes were present in the three standard repositories.
- No remote tags were present.
- The CP-M11 repository has local closure tags for CP-M9 and CP-M10.
- The original current-main object store exposed 15 unreachable blobs; no unreachable commits.
- The CP-M11 object store exposed 2 unreachable trees and 8 unreachable blobs.
- The late-replay object store exposed 7 unreachable commits, including the four PR-only recovery commits and three hosted-container diagnostic commits. They were noted, not restored.
- `github_evidence_mirror.git` has a pack/index mismatch and is not reliable as a live checkout.
- `github_evidence/embedded_objects.git` has no refs, a pack/index mismatch, and two observed unreachable commits.
- Historical bundles, source archives, dossier directories, and extracted corpus directories exist nearby. They were not imported or mined in Step 1.

## Known blockers

- Local branch named qta-complete-corpus-recovery diverges from PR #16 and must not be force-updated or silently merged.
- Exact 465-file CP-M11 staged candidate remains NOT_REPRODUCED; an exact 446-file intermediate is present locally.
- Late-checkpoint replay spans multiple unstaged/untracked tranches and requires deliberate classification.
- Whole recovery-corpus TAR remains quarantined as corrupt/truncated per committed evidence.
- Current active worktree has no synchronized locked environment.
- Hardware, controlled-drawing, owner-design, external-evidence, and model-admission blockers already recorded in the ledger remain blockers; Step 1 promotes none.

## Next immediate operation

> Protect and disposition any uncommitted previous-session work before beginning new recovery mutations.

Do not begin new checkpoint recovery, merge a lineage, apply an evidence patch, regenerate canonical outputs, or promote authority before that operation is explicitly controlled by Step 2.
