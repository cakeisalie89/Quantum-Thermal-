# Export ↔ repository recovery ledger

This ledger is the durable reconciliation index for the total-state recovery. It is seeded from the September 2026 recovery dossier and must be updated from live repository comparison and raw evidence mining. Historical claims remain historical until independently reproduced.

## Status rules

- `PRESENT_CURRENT` / `PRESENT_EVOLVED` do not prove exact historical checkpoint identity.
- `NO_CURRENT_EVIDENCE_FOUND` is a discovery priority, not proof that the concept is wholly absent.
- `PARTIAL_OR_DIVERGENT` requires semantic/path-level comparison.
- `EVIDENCE_GAP_DO_NOT_RECONSTRUCT` must remain a gap unless new exact evidence appears.
- `HISTORICAL_UNCOMMITTED_SALVAGE_ONLY` must not be merged into a closed checkpoint identity.
- Exact historical identity is allowed only when the applicable hashes/tree/object/validator conditions are independently reproduced.
- The quarantined corrupt/truncated complete-corpus TAR is never canonical recovery input.

## Seeded recovery records

| Order | Identifier | Class | Historical outcome | Starting current-main triage | Recovery action |
|---:|---|---|---|---|---|
| 0 | JUNE-RESET-RECOVERY-1 | RESET_RECOVERY | REPORTED_RECOVERED | PRESENT_CURRENT | Preserve exact June recovery evidence; do not regress current evolved implementation. |
| 1 | JUNE-RESET-RECOVERY-2 | RESET_RECOVERY | REPORTED_RECOVERED | PRESENT_CURRENT | Preserve exact June recovery evidence; do not regress current evolved implementation. |
| 2 | JUNE-RESET-RECOVERY-3 | RESET_RECOVERY | REPORTED_RECOVERED | PRESENT_CURRENT | Preserve exact June recovery evidence; do not regress current evolved implementation. |
| 3 | JUNE-RESET-RECOVERY-4 | RESET_RECOVERY | REPORTED_RECOVERED | PRESENT_CURRENT | Preserve exact June recovery evidence; do not regress current evolved implementation. |
| 4 | Stage-9 CP0 | STAGE9_SUBCOMMIT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Preserve exact Stage-9 CP6 source baseline; map transcript chronology without fabricating Git objects. |
| 5 | Stage-9 CP1 | STAGE9_SUBCOMMIT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Preserve exact Stage-9 CP6 source baseline; map transcript chronology without fabricating Git objects. |
| 6 | Stage-9 CP2 | STAGE9_SUBCOMMIT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Preserve exact Stage-9 CP6 source baseline; map transcript chronology without fabricating Git objects. |
| 7 | Stage-9 CP3 | STAGE9_SUBCOMMIT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Preserve exact Stage-9 CP6 source baseline; map transcript chronology without fabricating the missing hash or patch boundary. |
| 8 | Stage-9 CP4 | STAGE9_SUBCOMMIT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Preserve exact Stage-9 CP6 source baseline; map transcript chronology without fabricating Git objects. |
| 9 | Stage-9 CP5 | STAGE9_SUBCOMMIT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Preserve exact Stage-9 CP6 source baseline; map transcript chronology without fabricating Git objects. |
| 10 | Stage-9 CP6 | STAGE9_CHECKPOINT | REPORTED_CLOSED | PRESENT_EVOLVED | Authenticate the exact Stage-9 source archive and compare current release/verifier semantics against it. |
| 11 | CP-M0 | CHECKPOINT | REPORTED_COMMITTED — historical Git object not supplied | PRESENT_EVOLVED | Reproduce from authenticated Stage-9 baseline only where exact recorded writes/commands survive; preserve owner-directive/repository-derivative distinction. |
| 12 | CP-M0b | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — historical Git object not supplied | PRESENT_EVOLVED | Recover only missing semantics/tests/artifacts; do not replace stronger current hardening. |
| 13 | CP-M1 | CHECKPOINT | REPORTED_IN_PROGRESS / BLOCKED_EXTERNAL — commit reported; not closed | PRESENT_EVOLVED | Diff historical directive/tool evidence against current main; keep external signing/hosted blockers explicit. |
| 14 | CP-M2 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Recover missing behavior-safe quality/coverage semantics only; do not restore old weaker tooling over current hardening. |
| 15 | CP-M2a | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Recover only missing behavior-safe quality semantics. |
| 16 | CP-M2b | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Preserve corrected mypy residual history; do not preserve superseded 103 total as final CP-M2 value. |
| 17 | CP-M3 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical geometry/material authority semantics against current registry/solver paths. |
| 18 | CP-M4 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical screening/authority semantics against current implementation. |
| 19 | CP-M5 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PRESENT_EVOLVED | Compare historical FSM state/transition tests with current `machine_fsm` implementation; recover missing semantics only. |
| 20 | CP-M6 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile registry-backed solver constants and authority-transfer ledger semantics; preserve G-033/G-034 constraints. |
| 21 | CP-M7 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical authority-transfer and screening semantics. |
| 22 | CP-M8 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Recover missing checkpoint tests/semantics while preserving current evolved implementation. |
| 23 | CP-M9 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile purge/gas result dependencies against later geometry-authority changes. |
| 24 | CP-M10 | CHECKPOINT | REPORTED_COMMITTED / preserved historical baseline | PARTIAL_OR_DIVERGENT | Preserve historical CP-M10 boundary and compare current code semantically; do not reset current main to it. |
| 25 | CP-M11 | CHECKPOINT | REPORTED_NOT_CLOSED / staged candidate lineage | PARTIAL_OR_DIVERGENT | Keep separate from the later 465-file comprehensive candidate; recover only evidenced semantics and blockers. |
| 26 | CP-M12 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Prioritize transcript reconstruction. Decompose lost `p5_p13_clarification.json`/P13 interpretation from current-surviving `mp_Edes_He4` semantics. |
| 27 | CP-M13A | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Prioritize transcript reconstruction; exact if bytes survive, otherwise controlled `RECONSTRUCTED` reimplementation. |
| 28 | CP-M13 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Prioritize transcript reconstruction; exact if bytes survive, otherwise controlled `RECONSTRUCTED` reimplementation. |
| 29 | CP-M14A | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover grid/cell-volume authority/validation semantics from raw mutation evidence. |
| 30 | CP-M14 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover grid/cell-volume authority/validation semantics from raw mutation evidence. |
| 31 | CP-M15A | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover historical workflow-readiness test semantics (`test_workflow_readiness.py`) where still missing. |
| 32 | CP-M15 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `test_evaluation_paths` / workflow-readiness semantics without weakening current readiness checks. |
| 33 | CP-M16 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `NOT_EVALUABLE` epistemic/status semantics from exact transcript evidence. |
| 34 | CP-M17 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `NOT_EVALUABLE` epistemic/status semantics from exact transcript evidence. |
| 35 | CP-M18 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover coupled-mode / surface-temperature desorption semantics from raw evidence; do not invent physics. |
| 36 | CP-M19 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `evolve_coverage`, species-accounting and 1D/2D/3D surface-coverage semantics where lost. |
| 37 | CP-M20 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `cfg_off` / disabled-path semantics from exact evidence. |
| 38 | CP-M21 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover thermal-transient / coupled-mode handoff semantics where lost. |
| 39 | CP-M22 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover the G-062B `not physically interpretable` outcome, evidence, test, and blocking propagation. |
| 40 | CP-M23A | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `NOT_ASSESSED_PROGRAM_WIDE` semantics from exact evidence. |
| 41 | CP-M23 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `NOT_ASSESSED_PROGRAM_WIDE` semantics from exact evidence. |
| 42 | CP-M24A | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Prioritize transcript reconstruction; controlled reimplementation only if exact bytes unavailable. |
| 43 | CP-M24 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Prioritize transcript reconstruction; controlled reimplementation only if exact bytes unavailable. |
| 44 | CP-M25 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `surface_model_experiment_requirements.csv` semantics and validation requirements. |
| 45 | CP-M26 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover cost/source/quote-readiness epistemic status without turning sourcing into physical validation. |
| 46 | CP-M27A (Part A) | SUFFIX_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover exact checkpoint semantics from transcript evidence. |
| 47 | CP-M27 (later reclassified CP-M27A follow-up) | CHECKPOINT_RECLASSIFIED | OWNER-RECLASSIFIED | NO_CURRENT_EVIDENCE_FOUND | Preserve owner reclassification chronology; do not invent a separate closed CP-M27 identity. |
| 48 | CP-M27→CP-M27A owner reclassification | OWNER_CORRECTION | OWNER_CORRECTION | NO_CURRENT_EVIDENCE_FOUND | Preserve correction as authority metadata. |
| 49 | CP-M28 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Prioritize transcript reconstruction. |
| 50 | CP-M29 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover Knudsen characteristic-length semantics; do not bind unresolved geometry. |
| 51 | CP-M30 (later CP-M30A) | CHECKPOINT_RECLASSIFIED | OWNER-RECLASSIFIED | NO_CURRENT_EVIDENCE_FOUND | Preserve owner reclassification; recover historical `sv.Kn_He`/10 mm diagnostic only at its real authority level. |
| 52 | CP-M30→CP-M30A owner reclassification | OWNER_CORRECTION | OWNER_CORRECTION | NO_CURRENT_EVIDENCE_FOUND | Preserve correction as authority metadata. |
| 53 | CP-M31 (later CP-M31A) | CHECKPOINT_RECLASSIFIED | OWNER-RECLASSIFIED | NO_CURRENT_EVIDENCE_FOUND | Preserve owner reclassification chronology. |
| 54 | CP-M31→CP-M31A owner reclassification | OWNER_CORRECTION | OWNER_CORRECTION | NO_CURRENT_EVIDENCE_FOUND | Preserve correction as authority metadata. |
| 55 | CP-M32 (later CP-M32A) | CHECKPOINT_RECLASSIFIED | OWNER-RECLASSIFIED | NO_CURRENT_EVIDENCE_FOUND | Preserve owner reclassification chronology. |
| 56 | CP-M32→CP-M32A owner reclassification | OWNER_CORRECTION | OWNER_CORRECTION | NO_CURRENT_EVIDENCE_FOUND | Preserve correction as authority metadata. |
| 57 | CP-M33 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical implementation with current source/material/structural authority paths. |
| 58 | CP-M34 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical implementation with current source/material/structural authority paths. |
| 59 | CP-M35 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical implementation with current source/material/structural authority paths. |
| 60 | CP-M36 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical implementation with current source/material/structural authority paths. |
| 61 | CP-M37 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical implementation with current source/material/structural authority paths. |
| 62 | CP-M38 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile historical implementation with current source/material/structural authority paths. |
| 63 | CP-M39 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Preserve finding that no support component was eligible for canonical structural binding unless later explicit authority closes it. |
| 64 | CP-M40 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | PARTIAL_OR_DIVERGENT | Reconcile material/source evidence and later revision chain. |
| 65 | CP-M40R | REVISION_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover revision semantics from transcript evidence. |
| 66 | CP-M40R2 | REVISION_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover DOI/source-evidence revision semantics. |
| 67 | CP-M40R3 | REVISION_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover exact G10CR/G11CR source-evidence handling from surviving PDF/source assets. |
| 68 | CP-M41 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Reconstruct only from raw evidence; preserve source/structural authority distinctions. |
| 69 | CP-M41R1 | REVISION_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Reconstruct only from raw evidence. |
| 70 | CP-M41R2 | REVISION_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Reconstruct only from raw evidence. |
| 71 | CP-M41R3 | REVISION_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Reconstruct only from raw evidence. |
| 72 | CP-M41R4 | REVISION_CHECKPOINT | EVIDENCE GAP | EVIDENCE_GAP_DO_NOT_RECONSTRUCT | Preserve explicit evidence gap. Do not attribute objective/files/tests/closure without new exact evidence. |
| 73 | CP-M41R5 | REVISION_CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Reconstruct only from raw evidence, keeping CP-M41R4 gap intact. |
| 74 | CP-M42 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover B008 structurally-unbound status, registry/schema/tests/reporting and blocking propagation. |
| 75 | CP-M43 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover mechanical-edge recording / no-component-bound semantics and associated artifacts. |
| 76 | CP-M44 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover `mechanical_interface_graph.json` / sidecar-only and HDF5-output-mapping semantics; compare against current `design_interface_graph.json`. |
| 77 | CP-M45 | CHECKPOINT | REPORTED_COMMITTED — Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Recover merged mechanical query/derived-artifact semantics from transcript evidence. |
| 78 | CP-M46 | CHECKPOINT | REPORTED_COMMITTED — historical Git object not supplied | NO_CURRENT_EVIDENCE_FOUND | Reconstruct to reported endpoint only if independently reproduced; target comparisons include 773 tracked, 45 suites/825 passing, 89/89 identity, PASS=0. |
| 79 | CP-M46R1 | UNCOMMITTED_ATTEMPT | HALTED / UNCOMMITTED / REPOSITORY LOST | HISTORICAL_UNCOMMITTED_SALVAGE_ONLY | Salvage the B005 literature audit separately; never merge it into closed CP-M46 identity or invent a final tree. |
| 80 | AUG-CP-M11-COMPREHENSIVE-465 | UNCOMMITTED_STAGED_CANDIDATE | REPORTED STAGED / UNCOMMITTED | NO_CURRENT_EVIDENCE_FOUND | Highest-priority exact replay target. Byte-exact recovery requires all six validators; otherwise classify as controlled reconstruction. |
| 81 | RECOVERY-CORPUS-TAR-INTEGRITY | FORENSIC_INPUT | EXPORT PAYLOAD CORRUPT/TRUNCATED | FORENSIC_INPUT_ONLY | Quarantine the exported TAR; recover from the 15 ZIPs, exact assets, V3 documents, archives and transcript shards. |
| 82 | CURRENT-MAIN-BASELINE | CURRENT_IMPLEMENTATION | CURRENT PUBLIC MAIN | PRESENT_CURRENT | Protect current evolved implementation; compare historical reconstructions against it rather than resetting it. |

## Live reconciliation notes

This table is a starting forensic map, not the end of corpus mining. As live GitHub comparison and transcript/source-byte recovery proceed, append detailed entries below (or create linked subsystem ledgers) recording:

- exact evidence source and location;
- historical path/symbol;
- current path/symbol;
- semantic difference;
- exact recovery vs controlled reimplementation;
- authority classification;
- commit containing recovery;
- verification result;
- unresolved blocker.

### Initial live findings

1. Current `main` is `096fb90fff089a62e4b20513de729a3e45a69f35`, not the old CP-M11 line.
2. CP-M12's historical `p5_p13_clarification.json` was not found by current default-branch code search.
3. The CP-M12-associated `mp_Edes_He4` semantic object is not wholly missing: it survives in current `qta_full_sim.py`, `parameter_registry.csv`, `source_gap_register.csv`, `assumed_parameters.json`, `monte_carlo_parameter_registry.csv`, and `tests/test_parameter_semantics.py`. Therefore CP-M12 must be decomposed into lost artifact/interpretation semantics versus already-evolved implementation rather than replayed wholesale.
4. Historical `surface_model_experiment_requirements.csv` was not found by current default-branch code search.
5. Historical `OUTCOME_C_B008_STRUCTURALLY_UNBOUND` was not found by current default-branch code search.
6. The August 465-file CP-M11 candidate has a separate six-validator byte-exact recovery gate and must not be reconstructed by editing toward hashes.
