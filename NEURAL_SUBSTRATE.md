# The learned-model substrate (tranche NF-1T)

`scientific_ai.neural`: one architecture family -- a scientific
feature-token transformer with mixture-of-experts feed-forward layers --
from a development model that trains on a CPU in minutes to a configuration
of about one trillion parameters.

## What may be said, exactly

> A ~1T-total-parameter, high-activation scientific MoE architecture has been
> mathematically parameterised (996,509,217,800 trainable parameters;
> 200,832,889,864 active per token, exact counts) and structurally validated
> by zero-allocation (abstract) construction. A development-scale member of
> the same architecture family (431,784 trainable parameters) has been
> trained end to end, checkpointed, reloaded and evaluated. The ~1T model
> itself has not been allocated, has not been trained, and its scientific
> performance is not established. Distributed execution has been validated in
> software on eight simulated CPU devices only.

"The 1T model" in this repository always means the configuration whose
digest `docs/neural/flagship_model_manifest.json` names, and only what
`claim_status` in that manifest says about it. A filename is not evidence.

| claim | the flagship | the development member |
|---|---|---|
| ARCHITECTURE_DEFINED | yes | yes |
| ARCHITECTURE_PARAMETER_VERIFIED | yes | yes |
| ARCHITECTURE_META_VALIDATED | yes | yes |
| DEVELOPMENT_MODEL_TRAINED | a *family member* was | yes |
| CHECKPOINT_RELOAD_VALIDATED | a *family member's* was | yes |
| DISTRIBUTED_SOFTWARE_READY | yes, simulated devices (family) | yes (family) |
| DISTRIBUTED_HARDWARE_VALIDATED | no | no |
| LARGE_MODEL_TRAINED | no | no |
| SCIENTIFIC_PERFORMANCE_ESTABLISHED | no | no |

The claims are computed (`scientific_ai/neural/claims.py`) from evidence
bound by digest to the configuration they are about; `tools/neural.py
verify` recomputes them and fails if the committed ones do not follow.

## Where it sits

    AI proposes -> governed deterministic computation executes
      -> independent verification produces evidence -> authority decides

A learned prediction is none of those. Every learned output is
`LEARNED_PREDICTION`, `NON_AUTHORITATIVE`, `REQUIRES_EXTERNAL_VERIFICATION`.
The substrate does not import the agent layer; its lifecycle is recorded in
the ONE authority log through the existing record actions
(`qta_agent/learned_lifecycle.py`), and the authority store refuses every
edge into VERIFIED or PROMOTED for a record whose kind begins with
`learned_` -- live, on replay, and in the independent second reader
(`qta_agent/learned_rules.py`, `qta_agent/reconstruct.py`). No admission
policy for learned models exists; writing one is the programme owner's
decision, in a later tranche, with its own evidence. Nothing here replaces a
governed calculation.

## Framework

JAX 0.11.2 and jaxlib 0.11.2 (Apache-2.0), CPU wheels from PyPI, pinned
exactly, as the optional extra `neural` (`uv sync --extra neural`). Why:

* reverse-mode autodiff for training; `jax.eval_shape` for validating the
  flagship by tracing the REAL init and forward code with abstract values
  (nothing allocated); `shard_map` and collectives for expert parallelism,
  exercised on simulated devices;
* PyTorch was not available: `download.pytorch.org` is refused by this
  environment's network policy, and the x86-64 PyPI wheel is the
  multi-gigabyte CUDA build;
* an EXTRA, not a dependency group, because `reference_backend/build.sh`
  exports `--all-groups`: a group would have changed the reference runtime.
  The reference export was compared byte for byte before and after.

Four packages entered `uv.lock` (jax, jaxlib, ml-dtypes, opt-einsum); no
existing version moved. The parameter engine, solver, estimators, manifests,
claims, units, tokenizer and dataset governance import no JAX at all; the
network imports it in exactly one guarded place (`model/_jax.py`).

## The architecture family

    feature tokens (one per schema feature, canonical order)
      + readout tokens
      -> L pre-norm blocks: x += attention(rmsnorm(x)); x += ffn(rmsnorm(x))
      -> rmsnorm -> mean of readout tokens -> heads

**A feature token** sums five embeddings: its identity (a row of the
feature-identity table), its value (four channels through a small MLP),
its dimension (the seven SI exponents through a linear map, plus a
dimension-class row), its context (role) and its validity (VALID or
MISSING). The value channels are `z` (the value in its declared transform,
linear or log10, standardised with TRAINING-split statistics), `sign`,
`log_mag = log10(|v|)/32` and `is_zero`: magnitude and sign stay separable
at any scale and an exact zero is not a small number.

**Units.** `docs/unit_inventory.json` decides which unit a quantity has;
`units.py` only reads the repository's spellings into SI exponents and a
factor, and refuses anything else. Every unit in the inventory parses
(`PER_ROW`, whose unit belongs to the row, is refused by rule). 0.01 K and
0.01 Pa produce the same value channels and different identity and
dimension embeddings; a value given in mK is converted, one given in Pa for a
kelvin feature is refused.

**Refused inputs.** NaN (not a missing marker; missing is `None`), +-inf,
non-numbers, non-integer counts, values outside the physical domain,
non-positive values under a log transform, records with unknown or absent
features, schemas in which a target is also a feature.

**Set semantics.** Features are read by name in canonical order, and no
positional encoding is added: the input is a set. Permuting the tokens
leaves the prediction unchanged (to float rounding); padding tokens are
masked out of attention and routing.

**Attention.** Pre-norm, no biases, standard multi-head; grouped-query
attention is supported (`num_kv_heads`) and tested.

**Mixture of experts.** A float32 router scores every token against every
expert; `top_k` experts are selected (`lax.top_k`: distinct indices), their
probabilities renormalised; each expert is a SwiGLU (`3 d f` parameters).
Each expert has a buffer of `capacity` slots taken in GShard priority order
(every token's first choice, then every second choice); a choice that finds
its expert full is DROPPED and counted. `capacity_factor=None` (every member
here) drops nothing. Shared always-on experts are supported and off in the
flagship. The Switch load-balancing loss `E sum_i f_i P_i` (coefficient
0.01) is the only auxiliary objective.

**Router observability.** Every MoE layer returns the selected indices,
gates, kept mask, per-expert counts, dropped choices, entropy, the
load-balancing term, the largest load share and whether every logit was
finite. Training stops (`TrainingAborted`) on a non-finite loss or router;
the evaluation flags a layer where one expert takes more than half the
choices (`collapse_flag`), unused experts, out-of-range or duplicate indices.

**Heads.** Each target has a mean and a variance (heteroscedastic Gaussian,
in its standardised transform space). A bounded target's mean is mapped
into its bounds by a sigmoid -- a parametrisation, not a clip. The variance
is `softplus(.) + 1e-6`. A masked-feature reconstruction head (tied to the
identity table, or untied) exists for the tied-parameter accounting and is
off in every member.

**Uncertainty.** Aleatoric: the variance head, evaluated by interval
coverage at 50/80/90/95 % and the confidence-versus-error rank correlation.
Epistemic: NOT ASSESSED (one model gives no spread over models).

**OOD.** Six classes, in order: UNSUPPORTED_SCHEMA, MALFORMED_INPUT,
SCIENTIFICALLY_INVALID, EXTRAPOLATION (outside a feature's training range),
DISTRIBUTION_SHIFT (Mahalanobis distance beyond the training 0.99 quantile),
IN_DISTRIBUTION. The dataset carries a separately generated extrapolation
region; a random split is never called an extrapolation test.

**Constraints.** External, sourced, recorded, never applied to a
prediction: finite values, ranges, and the source model's own declared
invariants (bounded, capture_only, within_incidence).

## Parameters: what each number means

* **trainable_parameters** -- every tensor the optimizer updates, each
  counted once (`accounting.py`, a closed form written apart from the
  network code and compared with it category by category);
* **non_trainable_parameters** -- the training-split statistics a
  checkpoint carries (`2 x feature_vocab_size + 2 x heads`);
* **total_parameters** -- the two together; optimizer state, gradients and
  activations are memory, not parameters;
* **shared_parameters** -- every trainable parameter not in a routed
  expert;
* **active parameters per token** -- the trainable parameters on the
  inference path of one feature token plus the heads: embedding tables by
  the rows the token reads; every dense projection (attention, norms, the
  router, dense FFNs, shared experts) in full; a routed expert in full when
  the token is routed to it and not at all otherwise (`top_k` per MoE
  layer); no capacity drop. `active = shared_active + expert_active`,
  `expert_active = moe_layers x top_k x 3 d f`;
* **parameters a batch touches** -- not the active count: bounded by
  `accounting.batch_touched_upper_bound` and measured by the router
  statistics.

## The flagship

Structure, chosen and stated (`scientific_ai/neural/family.py`): hidden
8192, 64 layers, standard attention 64 x 128 (a non-autoregressive set
encoder has no key/value cache for GQA to shrink), every layer a MoE layer
of 64 routed SwiGLU experts, no shared expert, 4096 feature identities, 4
readout tokens, bfloat16 parameters and compute, float32 routing. The
budget solver (`solver.py`) then resolves the expert width and `top_k`
against 1.0T trainable (range 0.95-1.05T) and 200B active (range
150-250B), alignment 256:

| quantity | value |
|---|---|
| trainable parameters | 996,509,217,800 (-0.349 % of target) |
| non-trainable | 8,200 |
| expert parameters | 979,252,543,488 (64 layers x 64 experts x 239,075,328) |
| shared parameters | 17,256,674,312 |
| attention parameters | 17,179,869,184 |
| router parameters | 33,554,432 |
| embedding parameters (all input encoders) | 42,128,384 |
| output / uncertainty heads | 32,772 / 32,772 |
| expert hidden width | 9,728 |
| experts per MoE layer / selected per token | 64 / 12 |
| shared active parameters per token | 17,223,037,960 |
| expert active parameters per token | 183,609,851,904 (64 x 12 x 239,075,328) |
| **active parameters per token** | **200,832,889,864 (+0.416 %)** |
| routing profiles economical / standard / deep | top_k 10 / 12 / 14 = 170.2B / 200.8B / 231.4B active |

Validated by `jax.eval_shape` in about a second: the abstract tensors count
exactly this, all 64 MoE layers trace with legal routing, the heads resolve,
no JAX array is created, peak memory grows by tens of megabytes, and a real
allocation is refused twice over -- by `meta.materialize` and, independently,
by `network.init` -- unless explicitly opted into
(`allow_large=True` and `QTA_NEURAL_ALLOW_LARGE_ALLOCATION=1`, set by nothing
in this repository).

## Estimates (not measurements)

`estimates.py`, with its assumptions recorded alongside each number:

| | |
|---|---|
| parameter-only storage, bfloat16 | 1.99 TB |
| parameter-only storage, FP8 e4m3 (+ block scales) | 1.00 TB (storage estimate only; FP8 not validated) |
| parameter-only storage, float32 | 3.99 TB |
| mixed-precision Adam training state | 17.9 TB (18 bytes/parameter: bf16 copy, fp32 master, fp32 gradients, two fp32 moments), activations excluded |
| forward FLOPs per token | ~402 GFLOP |
| training FLOPs per token | ~1.2 TFLOP (x 4/3 with activation checkpointing) |
| routed traffic per token, forward, 8-way expert parallel | ~22 MB (12 experts x 8192 x 2 bytes, there and back, 64 layers) |

## Data, training and checkpoints

* **Dataset** (`source_surface_adsorption.py`): every sample is one run of
  the admitted `surface.langmuir_capture@1.0.0` model through
  `scientific.model.run_model`, with its bundle digest, parameter digest,
  implementation digest, environment and backend identity, generation time
  and validation status (INVARIANTS_HOLD -- not authority-verified). The
  bundle itself is not stored; it is re-derivable on the same backend.
* **Splits** are a hash of the provenance family and a seed; the OOD split
  is a separate temperature region (300-1000 K against 10-300 K). Leakage
  checks: duplicates, families and targets across splits, statistics from
  the training split only, target copies, design-stream reuse, OOD
  disjointness; the remaining risks are stated in the manifest.
* **Training**: Gaussian NLL + the load-balancing term; Adam with bias
  correction, global-norm clipping, warmup + cosine; the data order is a
  function of the seed and the epoch.
* **Reproducibility levels** (directive s.29) are claimed only as the
  evidence supports, for THIS backend and one process: two fresh runs are
  compared by tensor digest (BITWISE_REPRODUCIBLE), and an EXACT_RESUME of an
  interrupted run against the uninterrupted one (CHECKPOINT_REPRODUCIBLE).
  Nothing is claimed across hosts, devices or framework versions.
* **Resume semantics**: FRESH_TRAINING, EXACT_RESUME (same lineage),
  WARM_START, FINE_TUNE, CONTINUED_PRETRAINING (new runs that cite their
  source checkpoint).
* **Checkpoints** are the safetensors container layout, written and read
  with NumPy -- no pickle, so loading executes no code -- named by their
  sha256, verified before parsing, refused when their tensors are not
  exactly the configuration's. Sharded checkpoints record every shard's
  digest.

## Distributed readiness

`model/parallel.py`: generic execution profiles (CPU development, simulated
multi-device, single accelerator, multi-GPU node, B300-class node,
GB300-class rack, distributed cluster -- no provider, host or credential),
parallel plans over a (data, expert, tensor, pipeline) mesh with every
divisibility checked and every expert's owner listed, a sharding rule for
every parameter category, per-rank data partitions, and an expert-parallel
MoE layer (`shard_map` + two `all_to_all`). On eight simulated CPU devices
it reproduces the single-device layer exactly; sharded gradients,
activation checkpointing, gradient accumulation and sharded checkpoints
agree with their single-device originals
(`docs/neural/distributed_readiness.json`). None of this is a hardware
measurement.

## The hardware-era boundary

The former apparatus design's machine modes, mode sequence and gas-species
routing are not concepts of this substrate.
`tools/neural_legacy_audit.py` classifies every occurrence in the tracked
tree (the report is `docs/neural/legacy_semantic_audit.json`): zero in the
substrate, its tools, tests and evidence; the active generic modules that
still name them are reported, not changed -- their clean-up is the semantic
tranche E, which is not authorised. `tests/test_neural_boundary.py` fails
if one is introduced, by name or by import.

## Commands

    uv sync --frozen --all-groups --extra neural
    python tools/neural.py verify        # the CI step: re-derive and compare
    python tools/neural.py all           # regenerate every piece of evidence
    python tools/neural_legacy_audit.py --check
