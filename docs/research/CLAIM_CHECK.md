# Semantic claim verification (section 6.3)

`agent_substrate/claim_check.py` answers three questions about a claim. It
answers them separately and never merges them:

| Question | Answered by | Says nothing about |
|---|---|---|
| **Citation.** Do the cited observations exist, and do the result bytes they were read from still re-hash and still say what was recorded? | the evidence store, deterministically | whether the claim is true |
| **Admission.** Is each observation read from a record that is VERIFIED or PROMOTED (and admitted, for a scientific result), current in the research ledger, and usable under its owner's freshness policy? | the authority store and the research ledger | what the observation says |
| **Semantics.** Does what the admitted evidence says support what the claim says? | the closed grammar and `research.evaluate` (DETERMINISTIC), or a checker (ASSESSED) | whether the evidence is intact or admitted |

The semantic answer is one of five verdicts:

- **SUPPORTED**: every admitted observation that reports the output supports
  the claim, resolution included.
- **CONTRADICTED**: every one says otherwise.
- **INSUFFICIENT_EVIDENCE**: none reports it, or a resolution straddles the
  claimed value.
- **UNVERIFIABLE**: the grammar does not read the text in full, or the claim
  uses a unit the evidence is not in. Nothing is converted.
- **CONFLICTING_EVIDENCE**: admitted observations decide it both ways.

The answer is judged against every admitted observation of the objective,
not only the ones the claim cites. A claim cannot avoid a contradicting run
by leaving it out (`Report.uncited` names any it left out).

## Deterministic first; an opinion is reported, never granted

The grammar reads one sentence form only: an output of a model, an optional
verb, a comparison or a bare value, a number and a unit. If anything else is
in the sentence, nothing is read. "T_at_half of slab-1 is below 350 K, so
the panel is safe" is not read as "T_at_half of slab-1 is below 350 K".

Each claim the grammar reads is judged by the evaluation the research ledger
records verdicts with. The deterministic route therefore never says more
than the ledger would. A Hypothesis test holds it there: its SUPPORTED is
the ledger's SUPPORTED, and its CONTRADICTED is the ledger's REFUTED.

Text the grammar does not read may be passed to a checker (a model, or a
heuristic). Its answer is reported with basis ASSESSED. The claim's trust
label gains SEMANTICALLY_SUPPORTED only from the deterministic route, and
only with intact citations. `trust_labels.establish` refuses the assessed
check outright, because it is registered as ASSESSED in
`evidence_terms.CHECKS`. Nothing in the substrate imports the module, and
checking a claim writes nothing.

## The development benchmark

`tools/claim_bench.py` builds a world of real authority records, verified by
a second actor and read into the research ledger through its gates:

- one run that reports three outputs;
- two runs that disagree;
- one run whose record was later withdrawn.

It then scores three checkers. The results are in
`docs/research/claim_bench_results.json`, and `--check` reproduces them.

**Where the labels come from.**

- *Structured* claims (37) are labelled by construction. The label is chosen
  first, and the claim is then written to have it: three resolutions clear,
  half a resolution in, between two runs, or a foreign unit. The label
  follows from the generator's arithmetic, not from the checker.
- *Prose* claims (12) were labelled by the checker's author, with a reason
  each. **They are not human labels.** The prose scores are a development
  measurement. A confirmatory benchmark needs labels from people who did
  not write the checker, and recruiting them is the owner's decision.

**Results.** FP is a SUPPORTED answer to a claim labelled otherwise. FN is
another answer to a claim labelled SUPPORTED.

| Checker | Family | Exact | FP | FN | Established | Established and wrong |
|---|---|---|---|---|---|---|
| deterministic | structured | 37/37 | 0/24 | 0/13 | 12 | 0 |
| deterministic | prose | 4/12 | 0/8 | 4/4 | 0 | 0 |
| lexical (stand-in for a fallible checker) | structured | 14/37 | 22/24 | 0/13 | 0 | 0 |
| lexical | prose | 4/12 | 4/8 | 2/4 | 0 | 0 |
| combined (deterministic, then lexical for unread text) | structured | 37/37 | 0/24 | 0/13 | 12 | 0 |
| combined | prose | 4/12 | 4/8 | 2/4 | 0 | 0 |
| a real model | - | NOT_RUN | | | | |

**What it shows.**

1. **The deterministic route trades recall for precision.** It let no
   unsupported claim through in either family. It also missed every
   supported paraphrase: "about 312 K", "1200 W/m^2 at its surface",
   "hotter than slab-2". It also missed "1.2 kW/m^2", because it converts no
   unit.
2. **The fluent checker fails on plausible but unsupported claims.**
   - It passed a cause it had no evidence for: "peaks at 340 K because
     radiative losses dominate".
   - It passed an attribution nobody recorded: "according to a reviewer".
   - It passed agreement between two runs that disagree.
   - It passed the wrong output: "312.4 K at the hottest point".
   - It passed 22 of 24 wrong structured claims, because each threshold
     sits within 2% of the true value.
3. **The combination reports those passes, with basis ASSESSED, and
   establishes none of them.** Of the 12 claims established, none is wrong.
   The guarantee is the established property, not the verdict field. A
   reader that ignores `basis` is misled by the combined route exactly as
   often as by the lexical checker alone.

**What it does not show.**

- How a real model checker behaves: NOT_RUN, and live inference is the
  owner's (OD-1).
- Whether the prose labels are right: they are not human labels.
- Anything about claims outside the one sentence form. The grammar is
  deliberately narrow, and the benchmark's structured family is narrower
  still.
