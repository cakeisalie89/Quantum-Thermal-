# NF-1T: scaling and integration roadmap

**This is a plan, not evidence.** No compute is spent by it. It changes no
claim, and nothing in `docs/neural/claims.json` or
`docs/neural/current_status.json` cites it. The evidence says what NF-1T
is; this document says only what would have to happen, and who would have
to authorize it, before it could be more.

## What the evidence says now

The source is `docs/neural/current_status.json`, which is derived and
re-checked by `tools/neural.py verify`.

| | development | flagship-1t |
|---|---|---|
| status | EVALUATED | DEVELOPMENT_PIPELINE_VALIDATED |
| trainable parameters | 431,784 | 996,509,217,800 |
| trained | yes: 4,000 steps, one process, CPU | **no** (`large_model_trained: false`) |
| weights allocated | yes | **no** (`real_weights_allocated: false`) |
| claims held | defined, meta-validated, parameter-verified, reload-validated, trained | defined, meta-validated, parameter-verified |
| scientific performance | one closed-form law, simulated targets, OOD degraded | UNESTABLISHED |

**The flagship is an architecture, not a trained model.** It is not a
language model and it has never been trained. Every learned output is
LEARNED_PREDICTION, NON_AUTHORITATIVE and REQUIRES_EXTERNAL_VERIFICATION.
The authority store refuses a learned record VERIFIED or PROMOTED: no
admission policy for learned models exists.

## Stages, each behind its own owner decision

| stage | what it would take | who decides | evidence it would produce |
|---|---|---|---|
| S1. A second law | a governed dataset from another admitted model (the slab, the RC network), under the same dataset provenance rule (T5) | none beyond this repository's rules: CPU, offline | a development model of that law, evaluated in and out of distribution |
| S2. A family-scale model on one GPU | a GPU and its budget | the owner (OD-6) | a training run at a size between the development model and the flagship, with its own reproducibility scope |
| S3. Distributed training | multi-host hardware, a deployment, a dataset licence | the owner (OD-6, OD-7) | DISTRIBUTED_SOFTWARE_READY exercised for real, and a training-run record per host |
| S4. The flagship | the S3 infrastructure at full size, a budget orders of magnitude larger, real weights allocated | the owner | a trained flagship checkpoint; its scientific performance stays UNESTABLISHED until checked against governed models and measurement |
| S5. An admission policy for learned outputs | a reviewed rule for when a learned prediction may stand beside, never instead of, a verified result | the owner | a policy document; until it exists, learned records cannot be VERIFIED |

## Integration with the governed research loop

- **NF-1T is not a provider of evidence.** The research loop's contracts
  (`scientific/contracts.py`) offer only models with an independent check,
  and none of them is learned.
- **As a proposal source.** A trained NF-1T could serve as one more model
  behind the inference gateway, under OD-1, its proposals
  NON_AUTHORITATIVE like any other.
- **As a surrogate.** A learned prediction could steer which governed run
  to ask for next. The run, its check and the reviewer would still decide;
  the prediction never would.
- **No part of this roadmap is built.** No stage has begun beyond the
  development model already recorded.
