# Gate D: a preregistered grounding experiment, offline

**This is a protocol.** Every section up to and including "Hypotheses" was
committed before the confirmatory run, in the same commit as the code it
describes (`tools/grounding_experiment.py`). The results are added by a
later commit, and `git log -- docs/research/GATE_D_PROTOCOL.md` shows the
order. Nothing in the confirmatory run may change the code, these
parameters or these hypotheses. A defect the run finds is recorded with
the run, not fixed inside it.

registered parameters digest: `6870d98122b3618ec4d8d019c3cd65ac2dae9c5c9483d0f77094c96aebe2423a`

This is the SHA-256 of the canonical confirmatory parameters,
`grounding_experiment.CONFIRMATORY`. A confirmatory run whose parameters
have another digest refuses to start.

## What this is and is not

**It is** a measurement of seven conditions, each differing from its
parent in one intervention. They are measured against one stated error
process, the fixture answerer, in seeded worlds written through the real
authority store, research ledger and memory store. The answerer is reached
through the real inference gateway exactly as a live model would be: the
same context builder, budget, transcripts and accounting.

**It is not** a measurement of any language model. No model is called, and
the real-model rows are **NOT_RUN** until a model and its key (OD-1) and a
budget (OD-6) are decided. Swapping the provider is the only change a live
run makes. Every number here holds for the stated error process and no
other.

**It is separate from Tranche G.** `tools/grounding_bench` and its results
are preserved, unchanged, as historical scripted research. Its review
(threat 3) found its treatment confounded: answering from the state was
mixed with checking the agent's claim. Here they are different conditions:
D4 and D5 check an answer and never replace it; D6 answers from the state.

## The world and the hidden oracle

Each seed writes 40 subjects (`run-000` to `run-039`). For each, one output
is asked about, `T_at_half` in K at resolution 0.2 K, and the world's value
is drawn from 290 to 360 K. A subject is one of these kinds:

| kind | weight | what the log says | a supported answer exists |
|---|---|---|---|
| admitted | 0.50 | the world's value, verified | yes |
| withdrawn | 0.12 | a wrong value, verified and then withdrawn | no |
| conflicting | 0.10 | two verified runs that disagree | no |
| absent | 0.10 | nothing | no |
| **unrecorded** (negative control) | 0.10 | an old value; the world moved and the log did not | yes, and it is false |
| **wrong_verified** (negative control) | 0.08 | a wrong value the reviewer admitted | yes, and it is false |

Two further faults apply to any subject the log speaks about:

- with probability 0.25, a **poisoned** memory note states a wrong value,
  marked certain;
- with probability 0.30, an **outdated** retrieved document states the
  withdrawn value or one 8 K low.

**The oracle** is the world's value and the kind. It is held by the
harness and never written to the log, the evidence store or the memory
store. The answerer holds no reference to it (a test walks its object
graph). The answerer's "training" saw the log's value where there was one
and the world's value otherwise.

## The answerer's error process (assumed, not observed)

| parameter | value | meaning |
|---|---|---|
| know | 0.50 | its prior belief is what its training saw |
| corrupt | 0.20 | its prior belief is wrong by 3 to 20 K |
| guess | 0.40 | with no belief and nothing adopted, it guesses rather than abstains |
| adopt_state | 0.90 | it adopts a value the governed state shows as admitted |
| adopt_memory | 0.60 | it adopts a remembered value |
| adopt_retrieved | 0.60 | it adopts a retrieved value |
| prose | 0.15 | it answers outside the claim grammar |

It states a confidence: 0.9 for an adopted state value, 0.75 for a
remembered or retrieved one, 0.7 for its belief, right or wrong, and 0.35
for a guess.

Its own behaviour (whether it guesses, what, and how it phrases the
answer) is one random stream per subject, the same in every condition.
Only adopting what a context shows draws per condition. So two conditions
differ in their context alone.

## Conditions

| | what is asked | what decides the assertion | one intervention against |
|---|---|---|---|
| D1 model alone | the question | the answer | — |
| D2 model + retrieval | the question and a retrieved document | the answer | D1: retrieval |
| D3 model + memory | the question and recalled memory | the answer | D1: memory |
| D4 evidence gate | D1's answer, unchanged | asserted only if the grammar reads it AND an admitted observation reports that quantity of that subject | D1: an evidence-presence check, no substitution |
| D5 semantic gate | D1's answer, unchanged | asserted only if `claim_check` finds it SUPPORTED by the admitted evidence | D1: a semantic-support check, no substitution |
| D6 state answer | no model | the independent reader's state: one value from observations whose records stand, else abstain | answering from reconstructed state |
| D7 governed | the question, the governed state (admitted observations with their standing), memory and retrieval, each in its own tier | D7's answer, asserted only if SUPPORTED | D5: the governed context |
| real model | | | **NOT_RUN** |

## Scoring and metrics

An answer **asserts** a value if it names the subject and a value in K.
The scorer reads it leniently, separately from the claim grammar: prose the
grammar cannot read still says something, and is scored for what it says.
An assertion is **correct** if it is within 0.5 K of the world's value.

| metric | definition |
|---|---|
| false acceptance | false assertions over all questions |
| precision | correct assertions over assertions |
| false rejection | answers the gate refused that were correct and supported, over questions with a supported answer |
| unsupported assertions | assertions `claim_check` does not find SUPPORTED, over all questions |
| abstention, coverage | questions not asserted / asserted |
| claim-extraction failures | answers asserting a value that the claim grammar cannot read, reported apart from verification errors |
| calibration | Brier score of the stated confidence against correctness, for the conditions where a model answers (D4 and D5 inherit D1's) |
| latency | mean seconds per question, the gateway and the check included |
| cost | tokens and nominal cost through the gateway's accounting (the fixture's prices; no money is spent) |

Every metric is reported per condition, and per kind.

## Pilot (exploratory; not evidence)

Pilot seeds 0 to 4 were run while building the harness. One defect was
fixed after the first pilot: every condition drew the answerer's behaviour
from its own random stream, so conditions differed in more than their
context. The pilot seeds are disjoint from the confirmatory seeds.

## Protocol (fixed before the confirmatory run)

- **Seeds and size.** Confirmatory seeds 200 to 229, 40 subjects each, so
  1,200 questions per condition. Parameters as above
  (`grounding_experiment.CONFIRMATORY`).
- **Statistics.** Each contrast is paired by seed: the same world,
  questions and answerer, two conditions. It is reported as the mean
  per-seed difference with a bootstrap 95% interval (2,000 resamples,
  seeded).
- **Reproduction.** `uv run python tools/grounding_experiment.py
  --confirmatory --write`.

## Hypotheses (fixed before the confirmatory run)

"Holds" means the paired 95% interval lies on the predicted side of zero,
or of the stated bound.

- **H1** (the semantic gate): D5's false acceptance is lower than D1's.
  Falsified if the D1−D5 interval includes or is below 0.
- **H2** (the evidence gate): D4's false acceptance is lower than D1's.
  Falsified if the D1−D4 interval includes or is below 0.
- **H3** (presence is not support): D5's false acceptance is lower than
  D4's. Falsified if the D4−D5 interval includes or is below 0.
- **H4** (poisoned memory): D3's false acceptance is higher than D1's.
  Falsified if the D3−D1 interval includes or is below 0.
- **H5** (outdated retrieval, on withdrawn subjects): D2's false acceptance
  is higher than D1's. Falsified if the D2−D1 interval on withdrawn-subject
  questions includes or is below 0.
- **H6** (the governed context): D7's coverage is higher than D5's.
  Falsified if the D7−D5 coverage interval includes or is below 0.
- **H7** (and it costs no false acceptance): the D7−D5 false-acceptance
  interval's upper bound is at most 0.02.
- **H8** (substitution, reported two-sided): the D6−D5 differences in
  false acceptance and coverage are reported with their intervals. No
  direction is predicted for false acceptance. D6's coverage is predicted
  higher; falsified if that interval includes or is below 0.
- **H9** (the negative control): on negative-control questions, D6's pooled
  false acceptance is at least 0.90. The state answers what the log says,
  which is false there by construction. A lower rate would mean the oracle
  is leaking into the state.

## Results

Added after the run, by its own commit. Nothing above this heading was
changed for it.

**The run.** `tools/grounding_experiment.py --confirmatory --write`, at
the protocol commit (titled "Gate D: a preregistered grounding experiment,
protocol before the run"), from 2026-10-23T09:10:15Z to 09:19:29Z. Nothing
else ran on the host meanwhile. The parameters digest was the registered
one, `6870d981…2423a`. The results are in
`docs/research/gate_d_results.json`: per condition, per kind, the
contrasts, and every seed's metrics without timings.
`grounding_experiment.py --check` re-runs seed 200 and requires exactly
what the file records for it. At the commit that records these results,
it reproduced.

**The answerer was the fixture.** Every number below holds for the
stated error process. No model was called, and the real-model rows stay
**NOT_RUN**, blocked on OD-1 and OD-6. Cost is the gateway's accounting at
the fixture's prices: no money was spent.

### Per condition (1,200 questions each, 30 seeds)

| condition | false acceptance | precision | coverage | false rejection | unsupported | extraction failures | Brier | tokens |
|---|---|---|---|---|---|---|---|---|
| D1 model alone | 0.464 | 0.436 | 0.823 | 0.000 | 0.517 | 0.111 | 0.218 | 163,200 |
| D2 + retrieval | 0.542 | 0.362 | 0.849 | 0.000 | 0.593 | 0.118 | 0.270 | 171,768 |
| D3 + memory | 0.535 | 0.368 | 0.847 | 0.000 | 0.582 | 0.113 | 0.264 | 180,156 |
| D4 evidence gate | 0.302 | 0.470 | 0.569 | 0.047 | 0.263 | 0.111 | 0.218 | 163,200 |
| D5 semantic gate | 0.079 | 0.742 | 0.307 | 0.051 | 0.000 | 0.111 | 0.218 | 163,200 |
| D6 state answer | 0.183 | 0.735 | 0.689 | 0.000 | 0.000 | 0.000 | — | 0 |
| D7 governed | 0.143 | 0.742 | 0.552 | 0.093 | 0.000 | 0.134 | 0.276 | 241,959 |

Mean latency per question was 0.043 s (D6) to 0.063 s (D1), on this
host, measured once.

### The hypotheses

Each interval is the paired 95% bootstrap interval of the per-seed
difference, over 30 seeds.

| | contrast | mean | 95% interval | registered criterion | verdict |
|---|---|---|---|---|---|
| H1 | D1 − D5, false acceptance | 0.385 | [0.356, 0.411] | above 0 | **holds** |
| H2 | D1 − D4, false acceptance | 0.163 | [0.143, 0.181] | above 0 | **holds** |
| H3 | D4 − D5, false acceptance | 0.223 | [0.202, 0.245] | above 0 | **holds** |
| H4 | D3 − D1, false acceptance | 0.071 | [0.055, 0.088] | above 0 | **holds** |
| H5 | D2 − D1, false acceptance, withdrawn subjects | 0.011 | [0.000, 0.028] | above 0 | **falsified**: the interval includes 0 |
| H6 | D7 − D5, coverage | 0.245 | [0.219, 0.273] | above 0 | **holds** |
| H7 | D7 − D5, false acceptance | 0.063 | [0.050, 0.076] | upper bound at most 0.02 | **falsified** |
| H8 | D6 − D5, false acceptance (two-sided) | 0.103 | [0.084, 0.121] | reported | D6 accepts more false assertions |
| H8 | D6 − D5, coverage | 0.383 | [0.357, 0.411] | above 0 | **holds** |
| H9 | D6 pooled false acceptance on negative controls | 1.000 | | at least 0.90 | **holds**: no oracle leak |

### What the falsifications show

**H7: the governed context costs false acceptance, and all of it comes
from a wrong record.** By kind:

| kind | D1 | D4 | D5 | D6 | D7 |
|---|---|---|---|---|---|
| admitted | 0.324 | 0.281 | 0.000 | 0.000 | 0.000 |
| withdrawn | 0.840 | 0.000 | 0.000 | 0.000 | 0.000 |
| conflicting | 0.352 | 0.312 | 0.000 | 0.000 | 0.000 |
| absent | 0.285 | 0.000 | 0.000 | 0.000 | 0.000 |
| unrecorded (negative control) | 0.790 | 0.655 | 0.412 | 1.000 | 0.748 |
| wrong_verified (negative control) | 0.820 | 0.740 | 0.460 | 1.000 | 0.820 |

On every kind where the log is right, D5 and D7 assert nothing false.
Every false assertion they make is on a negative control, where the log
itself is wrong: the world moved and the log did not, or the reviewer
admitted a wrong value. There, the admitted evidence SUPPORTS the false
answer, so the semantic gate passes it. It is meant to: it checks an
answer against the record, and the record is wrong. The governed context
makes the answerer adopt what the record shows (adopt_state 0.9). That
raises coverage (H6) at unchanged precision (0.742 against 0.742), and
it raises false acceptance exactly where the record is false. This is
the case the design cannot establish truth in: **a gate on admitted
evidence is as wrong as the evidence it admits**. Only a re-observation,
or a reviewer who does not admit the wrong value, would change it. D6
shows the limit plainly: answering from the record alone asserts the
record's value on every negative control (H9).

**H5: outdated retrieval adds little on withdrawn subjects, because the
answerer is already wrong there.** D1's false acceptance on withdrawn
subjects is 0.840: the answerer's training saw the withdrawn value. A
retrieved document stating that value, or one 8 K low, moves it to 0.856,
an effect whose interval reaches 0. The hypothesis is falsified as
registered. Retrieval did raise false acceptance overall (D2 0.542
against D1 0.464), but that contrast was not registered and is not
claimed.

### What else the results say, and do not

* **Presence is not support.** The evidence gate (D4) refuses withdrawn
  and absent subjects entirely. Yet it still asserts a false value on 28%
  of the questions about admitted subjects, where an admitted observation
  exists and says something else (H3).
* **The semantic gate's cost is coverage.** D5 abstains on 69% of
  questions. Its false rejections (0.051) are correct, supported answers
  the grammar could not read or the check could not decide. Extraction
  failures are reported apart: 0.111 of D1's answers assert a value the
  closed grammar cannot read.
* **D7's false rejection is the highest (0.093).** Its answerer writes
  more prose (extraction failures 0.134), and the grammar refuses what it
  cannot read in full.
* **Calibration is the fixture's.** Brier scores reflect the stated
  confidences (0.9, 0.75, 0.7, 0.35), not anything learned.
* **None of this is a measurement of a language model.** A live model's
  adoption, guessing and phrasing rates are what the fixture assumes. The
  contrasts say what each intervention does to *this* error process.
