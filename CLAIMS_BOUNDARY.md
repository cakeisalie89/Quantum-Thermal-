# Claims boundary

What results produced here may never be presented as. The framework runs
numerical models, checks them, and records who decided what; none of that
is a measurement, and none of it is experimental validation unless a
comparison with measured observations says so.

Each boundary below is held by code, not by this page:
`docs/claims_boundary.json` names, for every one, the schema or rule that
enforces it and the tests that hold it, and `tools/claims_enforcement.py`
(a CI step) fails when a named module, symbol or test does not exist, when
this page does not state a boundary the registry holds, or when one of the
required boundaries is missing. A sentence nobody enforces is not listed here.

## Epistemic boundaries

| | Boundary | Held by |
|---|---|---|
| EB1 | A simulation result is not a measurement. | `scientific/observation.py` (MEASURED); `scientific/result.py` (ResultBundle) |
| EB2 | A synthetic observation is not a raw observation. | `scientific/observation.py` (SIMULATED) |
| EB3 | Numerical convergence is not physical validation. | `scientific/verification.py` (Establishes) |
| EB4 | Independent numerical agreement is not experimental validation. | `qta_agent/result_rules.py` (verification_problems); `scientific/verification.py` (Establishes) |
| EB5 | A signed or content-addressed artifact is not a scientifically correct one. | `qta_agent/result_rules.py` (admission_problems); `qta_agent/evidence.py` (EvidenceStore) |
| EB6 | An AI proposal is not a verified result. | `qta_agent/authority.py` (check) |
| EB7 | Tool completion is not an accepted scientific result. | `qta_agent/governed_model.py` (GovernedModelRuns) |
| EB8 | Retrieved RAG text is not evidence authority. | `qta_multiphysics/stack/rag_index.py` (retrieve) |
| EB9 | A surrogate prediction is not ground truth. | `scientific/observation.py` (SIMULATED); `qta_multiphysics/deep_expdesign/ood.py` (fit_ood) |
| EB10 | An optimization result is not a physical optimum. | `qta_multiphysics/stack/mdao_openmdao.py` (assert_design_variables); `scientific/observation.py` (SIMULATED) |
| EB11 | Model calibration is not model validation. | `scientific/verification.py` (Establishes); `scientific/observation.py` (MEASURED) |
| EB12 | A learned prediction is not authority: no learned record can be VERIFIED or PROMOTED. | `qta_agent/learned_rules.py` (refusal); `qta_agent/store.py` (AuthorityStore); `qta_agent/reconstruct.py` (_LEARNED_PREFIX) |
| EB13 | A meta-validated architecture is not an allocated, trained or validated model. | `scientific_ai/neural/claims.py` (evaluate); `scientific_ai/neural/model/meta.py` (materialize) |
| EB14 | A development model's training is not the flagship's. | `scientific_ai/neural/claims.py` (evaluate); `scientific_ai/neural/claims.py` (derive_status) |
| EB15 | A legacy QTA gate-table PASS count is not a status of the current harness. | `tools/pass_semantics_audit.py` (judge); `scientific_ai/neural/status.py` (build) |
| EB16 | Simulated distributed execution is not distributed hardware validation. | `scientific_ai/neural/claims.py` (SIMULATED_PROFILES); `scientific_ai/neural/model/parallel.py` (ExecutionProfile) |
| EB17 | Decision stability under numeric drift is not byte identity. | `scientific/reproduction.py` (decide) |

## Three meanings of "verified"

* **Artifact integrity** -- the same declared bytes: a digest, re-derived on
  every evidence read.
* **Scientific verification** -- a bounded numerical check passed: a
  `VerificationResult`, produced by a governed verification task.
* **Authority** -- evidence judged sufficient for a declared purpose, by a
  reviewer who is none of the parties above: a record's state, admitted
  under `scientific_result.admission/1` and re-decided on every replay.

None of them implies the next (EB5, EB7).

## What is out of scope

No hardware exists in this repository and nothing here was measured in one.
The hardware-era QTA package this framework grew out of made its own,
narrower statements -- its gate counts, its Mode B / Mode D exclusivity, its
BOM vocabulary; they are kept, unchanged and still checked by the legacy
verifier, in `docs/legacy/qta/CLAIMS_BOUNDARY.md`.
