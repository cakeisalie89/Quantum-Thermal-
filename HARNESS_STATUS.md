# Scientific-AI harness: completion status

Derived by `tools/harness_status.py` from `stack.json`, the completion matrix (evidence recomputed from implementation digests), the audits re-run, and the learned-model status, against the definition of done in `docs/harness_contract.json`. Not written by hand; `verify` fails if this file differs from what the inputs give.

**Outcome: INCOMPLETE**

## Required stack

| element | status | backing rows | satisfied |
|---|---|---|---|
| authority-substrate | UNCLASSIFIED | R40 (COMPLETE, PREDATES), R42 (COMPLETE, PREDATES) | NO |
| containers | STAGED | R64 (DEEPLY_I, NEVER) | NO |
| fenicsx | STAGED | R62 (DEEPLY_I, NEVER) | NO |
| fmi | DEFERRED | R63 (DEEPLY_I, NEVER) | NO |
| generic-scientific-layer | UNCLASSIFIED | R61 (COMPLETE, NEVER) | NO |
| hdf5 | ADOPTED | R61 (COMPLETE, NEVER) | NO |
| openmdao | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| openusd | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| paraview-vtk | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| proposal-ingress | UNCLASSIFIED | R66 (COMPLETE, NEVER) | NO |
| pytest-hypothesis | ADOPTED | R50 (COMPLETE, NEVER), R51 (COMPLETE, PREDATES) | NO |
| python-scientific-core | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| rag-read-only | ADOPTED | R35 (COMPLETE, PREDATES) | NO |
| ro-crate | ADOPTED | R65 (DEEPLY_I, NEVER) | NO |
| ruff-mypy-pydantic | ADOPTED | R70 (COMPLETE, NEVER) | NO |
| rust-selective | ADOPTED_ADMISSION_MECHANISM_ONLY | R67 (DEEPLY_I, NEVER) | NO |
| salib | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| slsa-sigstore | STAGED | R65 (DEEPLY_I, NEVER) | NO |
| snakemake | ADOPTED | R55 (COMPLETE, PREDATES), R69 (DEEPLY_I, NEVER) | NO |
| uv | ADOPTED | R56 (COMPLETE, PREDATES) | NO |

## Completion matrix

50 rows. By classification: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT 42, DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS 8. By hosted evidence: COMMIT_NOT_RECORDED 4, NEVER_RUN 24, PREDATES_CURRENT_IMPLEMENTATION 22.

## Audits

* pass_semantics: clean (current_ai_semantic_leaks 0, unclassified 0, committed_report_current True)
* neural_legacy_semantics: clean (active_neural_semantic_leaks 0, active_generic_semantic_leaks 0, unclassified 0)
* framework_boundary: clean (problems 0)
* claims_boundary: clean (problems 0)
* workflow_contract: clean (problems 0)

## Facts

* legacy_qta_pass_count_is_legacy_only: holds
* learned_outputs_refused: holds
* flagship_not_allocated_or_trained: holds

## Unsatisfied (70)

* stack authority-substrate: UNCLASSIFIED
* stack containers: STAGED
* stack fenicsx: STAGED
* stack fmi: DEFERRED
* stack generic-scientific-layer: UNCLASSIFIED
* stack hdf5: ADOPTED
* stack openmdao: ADOPTED
* stack openusd: ADOPTED
* stack paraview-vtk: ADOPTED
* stack proposal-ingress: UNCLASSIFIED
* stack pytest-hypothesis: ADOPTED
* stack python-scientific-core: ADOPTED
* stack rag-read-only: ADOPTED
* stack ro-crate: ADOPTED
* stack ruff-mypy-pydantic: ADOPTED
* stack rust-selective: ADOPTED_ADMISSION_MECHANISM_ONLY
* stack salib: ADOPTED
* stack slsa-sigstore: STAGED
* stack snakemake: ADOPTED
* stack uv: ADOPTED
* row R21: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R22: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R23: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R24: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R25: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R26: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R27: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R28: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R29: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R30: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R31: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R32: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R33: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R34: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R35: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R36: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R37: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R38: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R39: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R40: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R41: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / PREDATES_CURRENT_IMPLEMENTATION / 1 gaps
* row R42: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R43: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R44: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / COMMIT_NOT_RECORDED / 0 gaps
* row R45: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / COMMIT_NOT_RECORDED / 0 gaps
* row R46: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R47: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R48: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R49: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / NEVER_RUN / 1 gaps
* row R50: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R51: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R52: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R53: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R54: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R55: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R56: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R57: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R58: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / COMMIT_NOT_RECORDED / 0 gaps
* row R59: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / COMMIT_NOT_RECORDED / 0 gaps
* row R60: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R61: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R62: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / NEVER_RUN / 1 gaps
* row R63: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / NEVER_RUN / 1 gaps
* row R64: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / NEVER_RUN / 1 gaps
* row R65: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / NEVER_RUN / 1 gaps
* row R66: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R67: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / NEVER_RUN / 1 gaps
* row R68: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps
* row R69: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS / NEVER_RUN / 1 gaps
* row R70: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / NEVER_RUN / 0 gaps

## Outside this software-completion claim

* physical-experiment (EXPERIMENTAL_SCIENCE): no physical experiment has been performed; nothing here is experimentally validated
* hardware-validation (HARDWARE): no physical hardware validation; the legacy QTA apparatus does not exist
* flagship-allocation-and-training (PAID_COMPUTE): the ~1T flagship is parameterised and meta-validated, never allocated or trained -- the research frontier of R60
* development-model-science (MODEL_RESEARCH): the development model's scientific performance is unestablished and its epistemic uncertainty not assessed -- the research frontier of R60
* distributed-accelerators (HARDWARE): no distributed accelerator hardware validation; tensor and pipeline parallelism are plans -- the research frontier of R60
* arbitrary-host-byte-identity (EPISTEMIC): byte identity of floating-point results on an arbitrary host is not guaranteed and not claimed
* historical-qta-equivalence (OWNER_DECISION): scientific equivalence of the historical QTA corpus across backends may remain NOT_ESTABLISHED; migrating it needs the owner
* learned-model-admission (OWNER_DECISION): no admission policy for learned models exists; learned outputs stay NON_AUTHORITATIVE -- the research frontier of R60
* signature-is-not-truth (EPISTEMIC): a valid signature attests origin and integrity, never scientific validity
* one-benchmark-is-not-all-models (EPISTEMIC): one FEniCSx benchmark does not verify every scientific model
