# Scientific-AI harness: completion status

Derived by `tools/harness_status.py` from `stack.json`, the completion matrix (evidence recomputed from implementation digests), the audits re-run, and the learned-model status, against the definition of done in `docs/harness_contract.json`. Not written by hand; `verify` fails if this file differs from what the inputs give.

**Outcome: INCOMPLETE**

## Required stack

| element | status | backing rows | satisfied |
|---|---|---|---|
| authority-substrate | ADOPTED | R40 (COMPLETE, COVERS), R42 (COMPLETE, COVERS) | yes |
| containers | ADOPTED | R64 (COMPLETE, COVERS) | yes |
| fenicsx | ADOPTED | R62 (COMPLETE, COVERS) | yes |
| fmi | ADOPTED | R63 (COMPLETE, COVERS) | yes |
| generic-scientific-layer | ADOPTED | R61 (COMPLETE, COVERS) | yes |
| hdf5 | ADOPTED | R61 (COMPLETE, COVERS) | yes |
| openmdao | ADOPTED | R56 (COMPLETE, COVERS) | yes |
| openusd | ADOPTED | R56 (COMPLETE, COVERS) | yes |
| paraview-vtk | ADOPTED | R56 (COMPLETE, COVERS) | yes |
| proposal-ingress | ADOPTED | R66 (COMPLETE, COVERS) | yes |
| pytest-hypothesis | ADOPTED | R50 (COMPLETE, COVERS), R51 (COMPLETE, PREDATES) | NO |
| python-scientific-core | ADOPTED | R56 (COMPLETE, COVERS) | yes |
| rag-read-only | ADOPTED | R35 (COMPLETE, COVERS) | yes |
| ro-crate | ADOPTED | R65 (COMPLETE, COVERS) | yes |
| ruff-mypy-pydantic | ADOPTED | R70 (COMPLETE, COVERS) | yes |
| rust-selective | RESOLVED | R67 (COMPLETE, COVERS) | yes |
| salib | ADOPTED | R56 (COMPLETE, COVERS) | yes |
| slsa-sigstore | ADOPTED | R65 (COMPLETE, COVERS) | yes |
| snakemake | ADOPTED | R55 (COMPLETE, COVERS), R69 (COMPLETE, PREDATES) | NO |
| uv | ADOPTED | R56 (COMPLETE, COVERS) | yes |

## Completion matrix

50 rows. By classification: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT 50. By hosted evidence: COVERS_CURRENT_IMPLEMENTATION 47, PREDATES_CURRENT_IMPLEMENTATION 3.

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

## Unsatisfied (5)

* stack pytest-hypothesis: ADOPTED
* stack snakemake: ADOPTED
* row R51: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R58: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps
* row R69: COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT / PREDATES_CURRENT_IMPLEMENTATION / 0 gaps

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
