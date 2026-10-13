# Scientific-AI harness: completion status

Derived by `tools/harness_status.py` from `stack.json`, the completion matrix (evidence recomputed from implementation digests), the audits re-run, and the learned-model status, against the definition of done in `docs/harness_contract.json`. Not written by hand; `verify` fails if this file differs from what the inputs give.

**Outcome: INCOMPLETE**

## Required stack

| element | status | backing rows | satisfied |
|---|---|---|---|
| authority-substrate | ADOPTED | R40 (COMPLETE, PREDATES), R42 (COMPLETE, PREDATES) | NO |
| containers | ADOPTED | R64 (COMPLETE, PREDATES) | NO |
| fenicsx | ADOPTED | R62 (COMPLETE, PREDATES) | NO |
| fmi | ADOPTED | R63 (COMPLETE, PREDATES) | NO |
| generic-scientific-layer | ADOPTED | R61 (COMPLETE, PREDATES) | NO |
| hdf5 | ADOPTED | R61 (COMPLETE, PREDATES) | NO |
| openmdao | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| openusd | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| paraview-vtk | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| proposal-ingress | ADOPTED | R66 (COMPLETE, PREDATES) | NO |
| pytest-hypothesis | ADOPTED | R50 (COMPLETE, PREDATES), R51 (COMPLETE, PREDATES) | NO |
| python-scientific-core | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| rag-read-only | ADOPTED | R35 (COMPLETE, COVERS) | yes |
| ro-crate | ADOPTED | R65 (COMPLETE, PREDATES) | NO |
| ruff-mypy-pydantic | ADOPTED | R70 (COMPLETE, PREDATES) | NO |
| rust-selective | RESOLVED | R67 (COMPLETE, COVERS) | yes |
| salib | ADOPTED | R56 (COMPLETE, PREDATES) | NO |
| slsa-sigstore | ADOPTED | R65 (COMPLETE, PREDATES) | NO |
| snakemake | ADOPTED | R55 (COMPLETE, PREDATES), R69 (COMPLETE, PREDATES) | NO |
| uv | ADOPTED | R56 (COMPLETE, PREDATES) | NO |

## Completion matrix

50 rows. By classification: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT 50. By hosted evidence: COVERS_CURRENT_IMPLEMENTATION 3, PREDATES_CURRENT_IMPLEMENTATION 47.

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

## Unsatisfied (65)

* stack authority-substrate: ADOPTED
* stack containers: ADOPTED
* stack fenicsx: ADOPTED
* stack fmi: ADOPTED
* stack generic-scientific-layer: ADOPTED
* stack hdf5: ADOPTED
* stack openmdao: ADOPTED
* stack openusd: ADOPTED
* stack paraview-vtk: ADOPTED
* stack proposal-ingress: ADOPTED
* stack pytest-hypothesis: ADOPTED
* stack python-scientific-core: ADOPTED
* stack ro-crate: ADOPTED
* stack ruff-mypy-pydantic: ADOPTED
* stack salib: ADOPTED
* stack slsa-sigstore: ADOPTED
* stack snakemake: ADOPTED
* stack uv: ADOPTED
* row R21: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R22: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R23: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R24: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R25: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R26: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R27: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R28: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R29: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R30: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R31: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R32: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R33: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R34: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R36: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R37: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R38: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R39: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R40: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R41: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R42: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R43: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R44: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R45: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R46: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R47: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R48: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R49: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R50: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R51: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R52: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R53: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R54: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R55: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R56: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R57: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R58: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R59: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R60: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R61: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R62: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R63: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R64: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R65: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R66: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R69: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R70: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps

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
