# Grounding benchmark: adversarial review and falsification attempts

Tranche G, phase G6. This review is about `docs/research/GROUNDING_BENCHMARK.md`
and the code in `tools/grounding_bench/`. It was written by the same author
as both, which is the first threat to validity below. Its purpose is to
state everything that would make the benchmark's numbers mean less than
they appear to, and to record each attempt to make the treatment fail.

## Claims this review does not let stand

- **No novelty.** Checking what an agent is about to say against an
  authoritative store, and abstaining when the store cannot vouch for it,
  is ordinary practice: retrieval grounding, fact verification against a
  knowledge base, provenance checks. Nothing here is claimed to be new.
- **No generality.** Every number is for a scripted error process the
  author chose, over worlds and faults the author designed. Nothing is
  claimed about any language model. The real-model condition was not run.
- **No claim about truth.** The treatment reduces disagreement with the
  log and the evidence store, and it inherits every error they contain.
  The negative control (H8) exists to measure that.

## Threats to validity

1. **The oracle and the log agree by construction.** On an unfaulted record
   the honest history is the truth, so any reader of the log is right
   about it. The benchmark measures how sources of state behave under the
   injected faults and the agent's own errors. It does not measure whether
   the log matches the world. The `unrecorded` control is the one place
   where they differ, and there every log reader fails (H8).
2. **The error process is assumed.** Miss, corrupt, credulity, window and
   guess are parameters, not observations. The sensitivity runs move them
   one at a time. They show how much the conclusions depend on the
   parameters; they do not show that any real agent has them.
3. **E combines two mechanisms, and the run shows which one carries it.**
   With no belief, E asks the state directly and only then gates. A−E
   therefore mixes having access to the state with checking claims against
   it.
   - In the sensitivity runs, E's false-assertion rate is 0.074 under every
     one of the six error-model settings, while A's moves from 0.311 to
     0.504.
   - E's answer is the state's whenever the state can vouch, whatever the
     agent believed.
   - A−E (0.298) is the effect of answering from an independently
     reconstructed state instead of from the agent's own beliefs. It is NOT
     evidence that checking an agent's claims makes the agent's claims
     better, and must not be quoted as such.
   - A design that measured the gate alone would deny E the fallback. That
     condition was not pre-registered and was not run.
4. **H2 is close to true by construction.** On an unfaulted record, E's
   fallback to the state answers whatever belief is missing, and the state
   is right. Low over-refusal there is evidence that the gate refuses
   nothing correct when the state is correct. It is not evidence for a
   setting where the agent cannot consult the state.
5. **The question mix was chosen.** About 30% of questions target faulted
   records, far more than any realistic history would contain. Absolute
   rates, and the size of each contrast, depend on that mix. The sign of a
   paired contrast is less sensitive to it, and the per-fault pooled rates
   are reported so the reader can reweight.
6. **Forking paths.** Four pilot runs informed the design: the fault
   targeting, the negative control, and the authenticated condition with
   its compromised-writer fault. The hypotheses were fixed after the
   pilots. The confirmatory seeds are disjoint from the pilots', and the
   code was frozen in the commit that fixed the hypotheses. That protects
   against tuning to the confirmatory worlds, but not against having
   chosen the questions that the pilots suggested the treatment would win.
   H6, H7 (second half) and H8 are predictions that the treatment FAILS,
   and they are included for that reason.
7. **Claims are closed-form.** A real agent produces text, and extracting
   claims from it is a source of error that is not measured here.
8. **Test keys, post-hoc signatures.** F's attestations use TEST identities
   anybody can derive, and are written after the honest history rather
   than by each writer as it appends. An attestation covers the event
   hash, so the files are the same. The deployment question — keys held by
   distinct writers, with a vetted provider — is not touched.
9. **Exact value equality.** A value is correct only when it is exactly
   the reported value. A paraphrase or a rounding counts as false.
10. **Verification against a compromised source manufactures confident
    errors.** On `forged_valid`, E is wrong 0.505 of the time against A's
    0.274: where the agent was right and the forged log wrong, E
    "corrected" the agent. A gate is exactly as good as what it checks
    against. That is the cost side of H1, and H1's single number hides it.
11. **Failing closed costs availability.** The primary store refuses a
    whole history that holds one forged transition. Answering from it (C)
    falls back to the agent in most worlds, and refusing instead
    (C_fail_abstain) refuses 0.967 of the answerable questions. The
    independent reader's per-record refusal is what lets E keep answering.
    That is a property of this reader, and is not a general argument
    against failing closed.

## Falsification attempts

Each attempt is a way the treatment should FAIL if the benchmark is honest.
Every outcome is from the confirmatory run (30 seeds; per-fault rates
pooled; contrasts paired, with 95% bootstrap intervals).

| Attempt | Prediction if the benchmark is honest | Outcome |
|---|---|---|
| A forged transition the rules accept (`forged_valid`), unauthenticated | E is fooled whenever it asserts (H6) | E wrong 0.505 against A 0.274: E is worse than no gate |
| The same, signed with a compromised key (`forged_signed`), authenticated | F is fooled exactly as E is (H7) | F = E = 0.458; F is 0.000 on the unsigned forgery |
| The world changes and the log does not (`unrecorded`) | E does worse than A (H8) | E 1.000 against A 0.519: every log reader is wrong, and nothing leaked the oracle |
| The evidence check removed (E_no_evidence) | it reads tampered values (H5) | tampered: 0.487 against E's 0.000; overall +0.024 [0.018, 0.031] |
| Abstention removed (E_no_abstain) | false assertions return (H4) | 0.403, A's level; +0.301 [0.287, 0.316] |
| The oracle leaks to an agent | `test_an_agent_is_handed_nothing_that_knows_the_answer` fails | the test passes |
| The results are not the code's | `test_the_committed_results_reproduce_from_the_committed_code` fails | the test passes |

## What the result supports, stated once

For a scripted agent under the stated error model, in worlds written
through this substrate:

- answering from an independently reconstructed, evidence-checked state,
  and abstaining where it cannot vouch, makes fewer confident false
  statements than answering from the agent's own beliefs (0.102 against
  0.400);
- it does so at no over-refusal on questions the honest history could
  answer (close to true by construction);
- authenticating the history lowers the rate further (0.078);
- it is worse than the agent alone against an accepted forgery, and wrong
  every time about anything the log does not record.

It says nothing about language models, about checking an agent's claims as
opposed to replacing them, or about any world where the log is not the
truth.
