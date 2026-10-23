# Grounding benchmark: does independently reconstructed state reduce false assertions?

Tranche G, phase G5. This document is a PROTOCOL first and a report second.
The sections up to and including "Hypotheses" were committed before the
confirmatory run, in the same commit as the code they describe. The results
were added in a later commit. `git log -- docs/research/GROUNDING_BENCHMARK.md`
shows the order.

## What this is and is not

**It is** a measurement of how much each source of state limits the damage
done by a stated error process. That process is a SCRIPTED agent with an
explicit error model (`tools/grounding_bench/agents.py`: miss, corrupt,
credulity, window, guess). It is measured in seeded worlds written through
the real authority store and then attacked.

**It is not** a measurement of any language model. No model was called. The
real-model condition is reported `NOT_RUN`, in the results and here. A
scripted agent's error process is an assumption, and every number below
holds for that assumption and no other. Whether a model's errors resemble
it is a separate experiment that has not been done.

**It is not** a claim that the hypothesis is true in general. The question
is narrow: given this error process and these faults, does gating
assertions on an independent reconstruction of the log (plus the evidence
store) reduce confident false statements, and at what cost in refusals?

## The claim contract

V(C, S, E) → SUPPORTED | CONTRADICTED | UNKNOWN | UNVERIFIABLE
(`tools/grounding_bench/claims.py`):

- **C**, the claim: a closed form `(subject, predicate, value)`, with
  predicate `state` or `value`. Extracting claims from free text is not
  measured.
- **S**, the state: rebuilt from the log by the INDEPENDENT reader
  (`agent_substrate.reconstruct`), with or without an authenticator.
- **E**, the evidence store: it re-hashes what it returns.

| Verdict | Meaning | What it is NOT |
|---|---|---|
| SUPPORTED | S holds the subject, no transition of it was refused, and S and E agree with the claim | "true": a well-formed record nobody authenticated is in S |
| CONTRADICTED | S and E say otherwise; the held value replaces the claim | |
| UNKNOWN | S holds nothing about the subject | "false": a missing record is not a record that the thing is absent |
| UNVERIFIABLE | S holds the subject but cannot vouch: a refused transition, evidence missing or no longer verifying, a value asked of a non-admitted record, an unknown predicate | |

## The world and the hidden oracle

`tools/grounding_bench/world.py`:

1. **The honest history.** Records are created, verified with a report in
   the evidence store, promoted and revoked, all through `AuthorityStore`.
   Memory notes record each verified value.
2. **Signatures.** Each honest event is attested by its actor's TEST
   identity. Production registries refuse these keys, and nothing here is a
   deployment.
3. **The attack window.** It follows the honest history: the store fails
   closed after a refused forgery, by design, so attacks come last. Each
   attack also narrates a misleading message to the agents.

The faults:

| Fault | What is written | Who should be fooled |
|---|---|---|
| `forged` | a transition the state machine refuses (to PROPOSED) | nobody that replays the rules |
| `forged_valid` | a well-formed revocation nobody decided, unattested | every reader that does not authenticate |
| `forged_signed` | the same, attested with the named actor's key (compromised writer) | every reader, authenticated or not |
| `tampered` | a verified record's report bytes rewritten on disk | any reader that does not re-hash |
| `poisoned` | a memory note with a wrong value | readers of memory |
| `unrecorded` | **negative control**: the world changes, the log does not; the narration is honest | every reader of the log |

**The oracle** (`World.oracle`) is built from the honest steps plus the
unrecorded changes. It is never written into the log, the evidence store or
memory. An agent receives an `AgentView` holding the log path, the evidence
root, the attestations path and the narrations, and nothing else. A test
walks every agent's object graph and checks that it holds no reference to
the world or to the oracle (`tests/test_grounding_bench.py`).

## Conditions

| | Consults | Notes |
|---|---|---|
| A | its own beliefs (the narrations through the error model) | the baseline |
| B | memory notes (the latest wins), then beliefs | |
| C | the PRIMARY projection; when it refuses the history, beliefs | |
| C_fail_abstain | C, but abstains when the primary refuses | ablation of C |
| D | the independent reader's state, as it comes; report bytes read raw | |
| E | its own claim gated by V: SUPPORTED asserted, CONTRADICTED corrected, UNKNOWN/UNVERIFIABLE abstain; with no belief, S's own answer, gated | the treatment |
| E_no_evidence | E with a raw byte reader in place of the store | ablation: the evidence check |
| E_no_correct | E, abstaining on CONTRADICTED | ablation: correction |
| E_no_abstain | E, asserting the belief on UNKNOWN/UNVERIFIABLE | ablation: abstention |
| F | E over a state rebuilt by a reader that authenticates every event | treatment plus authentication |
| real model | — | **NOT_RUN** |

## Questions and scoring

Each seed gets 200 questions:

- 15% are about records that do not exist;
- about 30% are about faulted records;
- the rest are about unfaulted records.

Each question asks a `state` or a `value`. A value has a truth only while
the record is admitted (VERIFIED or PROMOTED).

| Score | Condition |
|---|---|
| `correct` | asserted, and the oracle holds it |
| `false` | asserted, and the oracle does not hold it (the hallucination measured here) |
| `abstain_ok` | no assertion, and there was no true answer |
| `abstain` | no assertion where there was one |

Metrics:

- **False-assertion rate:** false answers over all questions.
- **Over-refusal rate:** abstentions on UNFAULTED records with a true answer
  (questions the honest history could answer), over those questions.
- **Coverage:** assertions over questions.
- **Precision:** correct answers over assertions.
- **Per-fault breakdowns,** pooled over seeds.

## Pilot runs (exploratory; the code changed between them; not evidence)

They are recorded because they happened. All used seeds 0 to 5. The
confirmatory seeds are disjoint.

1. **Pilot 1** (2 seeds, 150 steps). E and E_no_evidence gave identical
   false-assertion rates (0.017). Diagnosis: `tampered` faults chose records
   that were already REVOKED, whose value no question can expose. That made
   the ablation vacuous. Fixed: value faults target admitted records only.
2. **Pilot 2** (6 seeds, 300 steps). E 0.017, E_no_evidence 0.052, D 0.052,
   A 0.408.
3. **Pilot 3.** Added the `unrecorded` negative control. E 0.075, D 0.087,
   A 0.394. Per fault, D and E answered `unrecorded` questions falsely every
   time (1.00), against 0.48 for A.
4. **Pilot 4.** Added `forged_signed` and condition F. F 0.066, E 0.099,
   D 0.113, A 0.393. On `forged_valid`, F 0.00 against E 0.60. On
   `forged_signed`, F equalled E (0.56).

## Protocol (fixed before the confirmatory run)

- **Confirmatory:** 30 seeds, 1000–1029. Each has 400 honest steps,
  fault rate 0.05 (20 faults with kinds drawn uniformly), 200 questions and
  the default error model (miss 0.10, corrupt 0.05, credulity 0.50,
  window 400, guess 0.30). `run.CONFIRMATORY`.
- **Statistics:**
  - each per-condition rate is the mean over seeds with a bootstrap 95%
    interval (2000 resamples, seeded);
  - comparisons are PAIRED by seed: same world, same questions, same
    beliefs. Each is reported as the mean per-seed difference and its
    bootstrap 95% interval (`run.CONTRASTS`).
- **Sensitivity (secondary, exploratory):** 10 seeds each, 2000–2009, 300
  steps. One parameter is moved at a time: credulity 0 and 1, guess 0 and
  0.6, window 50, miss 0.3, fault rate 0.01 and 0.2.
- **Long horizons (opt-in):** 1000, 3000, 5000 and 10000 steps, 2 seeds
  each (3000–3001), with wall time recorded. They are slow because each
  event is signed and verified with a pure-Python reference Ed25519.
- **Reproduction:** `uv run python -m tools.grounding_bench.run --protocol
  [--long] --out <file>`. `tests/test_grounding_bench.py` re-runs one
  committed seed and requires the identical record.

## Hypotheses (fixed before the confirmatory run)

Each hypothesis states its prediction and what would falsify it. "Holds"
means the paired 95% interval is on the predicted side of zero (or of the
stated bound).

- **H1** (the treatment): E's false-assertion rate is lower than A's.
  Falsified if the A−E interval includes or is below 0.
- **H2** (its cost): E's over-refusal on unfaulted records is at most 0.05.
  Falsified if the upper bound of E's interval exceeds 0.05.
- **H3** (correction pays): E_no_correct over-refuses more than E.
  Falsified if the E_no_correct−E over-refusal interval includes 0.
- **H4** (abstention carries the treatment): E_no_abstain's
  false-assertion rate is higher than E's. Falsified if the interval
  includes 0.
- **H5** (the evidence check is load-bearing): E_no_evidence's
  false-assertion rate is higher than E's. Falsified if the interval
  includes 0. Expected to be small, because few questions hit a tampered
  report.
- **H6** (the stated limit, unauthenticated): on `forged_valid` questions,
  pooled, E's false rate is at least A's. Predicted: E is fooled whenever
  it asserts, because S says what the forged record says. Falsified if E's
  pooled rate is lower than A's.
- **H7** (authentication closes H6 and nothing else):
  - on `forged_valid`, F's pooled false rate is 0;
  - on `forged_signed`, F's pooled false rate equals E's.

  Falsified by either failing.
- **H8** (the negative control): on `unrecorded` questions, E's pooled
  false rate is higher than A's. A reader of the log cannot know what the
  log does not say. Falsified if E's is at most A's. If it were falsified,
  the benchmark would be leaking the oracle.

Nothing in the confirmatory run may change the code, the protocol or these
hypotheses. A defect found in the run is recorded with the run, not fixed
inside it.

---

## Results (added after the confirmatory run)

The confirmatory and sensitivity runs used the code at `4008763`, the
commit that fixed the protocol above. The numbers are in
`docs/research/grounding_bench_results.json`, with how each part was
produced. The only later change to the benchmark's code is type annotations
(D-2026-160); `tests/test_grounding_bench.py` re-derives a committed seed
from the current code and requires the identical record.

**Everything below is about scripted agents under the stated error model.
The real-model condition was not run (`NOT_RUN`).**

### Confirmatory run: 30 seeds, 400 steps, 200 questions each

| Condition | False-assertion rate [95% CI] | Over-refusal [95% CI] | Coverage | Precision |
|---|---|---|---|---|
| A (beliefs) | 0.400 [0.384, 0.416] | 0.023 [0.016, 0.029] | 0.837 | 0.522 |
| B (memory) | 0.421 [0.403, 0.439] | 0.018 [0.014, 0.023] | 0.871 | 0.517 |
| C (primary) | 0.394 [0.371, 0.416] | 0.021 [0.015, 0.028] | 0.827 | 0.525 |
| C_fail_abstain | 0.006 [0.000, 0.017] | 0.967 [0.900, 1.000] | 0.015 | 0.633 |
| D (reconstruction) | 0.126 [0.111, 0.140] | 0.000 | 0.528 | 0.760 |
| **E (gated)** | **0.102 [0.087, 0.115]** | **0.000** | 0.477 | 0.786 |
| E_no_evidence | 0.126 [0.111, 0.141] | 0.000 | 0.501 | 0.748 |
| E_no_correct | 0.051 [0.043, 0.059] | 0.083 [0.069, 0.097] | 0.379 | 0.864 |
| E_no_abstain | 0.403 [0.385, 0.421] | 0.000 | 0.852 | 0.527 |
| **F (authenticated)** | **0.078 [0.066, 0.089]** | **0.000** | 0.453 | 0.828 |

Pooled false-assertion rate on questions about faulted records:

| Condition | forged | forged_valid | forged_signed | tampered | poisoned | unrecorded |
|---|---|---|---|---|---|---|
| A | 0.672 | 0.274 | 0.315 | 0.227 | 0.397 | 0.519 |
| B | 0.691 | 0.253 | 0.252 | 0.057 | 0.603 | 0.551 |
| D | 0.000 | 0.505 | 0.458 | 0.487 | 0.000 | 1.000 |
| E | 0.000 | 0.505 | 0.458 | 0.000 | 0.000 | 1.000 |
| F | 0.000 | **0.000** | 0.458 | 0.000 | 0.000 | 1.000 |

Questions per fault, pooled over all seeds: 314, 285, 330, 300, 290 and 316.

### The hypotheses

Every interval is a paired, per-seed 95% bootstrap interval.

| | Prediction | Observed | Verdict |
|---|---|---|---|
| H1 | E's false-assertion rate below A's | A−E = 0.298 [0.276, 0.321] | holds |
| H2 | E's over-refusal ≤ 0.05 | 0.000 [0.000, 0.000] | holds; see the review on why it is close to true by construction |
| H3 | removing correction raises over-refusal | E_no_correct−E = 0.083 [0.069, 0.097] | holds |
| H4 | removing abstention raises false assertions | E_no_abstain−E = 0.301 [0.287, 0.316] | holds |
| H5 | removing the evidence check raises false assertions | E_no_evidence−E = 0.024 [0.018, 0.031] | holds, and is small |
| H6 | on `forged_valid`, E is no better than A | E 0.505 against A 0.274 | holds: E is WORSE |
| H7 | F stops `forged_valid` and not `forged_signed` | F 0.000; F = E = 0.458 | holds |
| H8 | on `unrecorded`, E is worse than A | E 1.000 against A 0.519 | holds: no leak from the oracle |

All eight hold. What that does and does not establish is in
`docs/research/GROUNDING_REVIEW.md`. Read it before quoting any number here.

### Secondary and exploratory (not pre-registered as tests)

- **E does not depend on the error model.** Across the six error-model
  sensitivity runs (credulity 0 and 1, guess 0 and 0.6, window 50,
  miss 0.3), E's false-assertion rate is 0.074 in every one, while A's
  ranges from 0.311 to 0.504.
  - E's answer is S's whenever S can vouch, whatever the agent believed.
  - The measured benefit is therefore that of answering from the state
    instead of the agent, not of filtering the agent's claims.
  - Only the fault rate moves E: 0.093 at 0.01, and 0.041 at 0.2, where
    more of the questions concern faulted records that E abstains on.
- **Correction amplifies an accepted forgery.** On `forged_valid`, E's false
  rate (0.505) is almost double A's (0.274). Where the agent's belief was
  right and the forged state wrong, E "corrected" it. E_no_correct, which
  abstains instead, is wrong 0.232 of the time there.
- **One refused record withdraws the primary projection.** The primary
  store refuses a whole history that contains one forged transition, by
  design (fail-closed). C therefore falls back to beliefs in most worlds,
  and C ≈ A. C_fail_abstain refuses 0.967 of the answerable questions.
  This is an availability cost of failing closed, and nothing here
  measures a way around it.
- **Long horizons** (opt-in, 2 seeds each, so the intervals are wide):

  | Steps | A | E | F | A's over-refusal | Wall time |
  |---|---|---|---|---|---|
  | 1000 | 0.315 | 0.092 | 0.065 | 0.275 | 44 s |
  | 3000 | 0.305 | 0.073 | 0.060 | 0.447 | 145 s |
  | 5000 | 0.287 | 0.105 | 0.080 | 0.538 | 232 s |
  | 10000 | 0.328 | 0.083 | 0.060 | 0.553 | 530 s |

  The agent forgets beyond its 400-step window, so A's refusals grow with
  the horizon. B worsens with it (0.392 to 0.492), as more notes give
  "latest wins" more to be wrong about. E and F stay in the range of the
  confirmatory run. The wall times were measured with a test suite sharing
  the host, and are indications only.
