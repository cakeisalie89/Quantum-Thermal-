# The high-assurance scientific AI platform: the master directive's report

This is section 12 of the master directive ("Complete, formally strengthen,
and integrate"). It is written by hand from the evidence it cites, and the
evidence is the authority where they disagree:

- `git log` names every commit;
- `docs/DEFECT_LEDGER.md` (D-2026-162 to D-2026-186) says what each found
  and what it did not;
- the results files under `docs/assurance/` and `docs/research/` hold the
  measurements;
- `uv run python tools/harness_status.py verify` and
  `uv run python tools/readiness.py verify` derive the status.

`docs/tranche_h/PROGRESS.md` is the row-by-row record.

**Nothing here is a measurement of a language model.** Every proposal in
every run was made by a scripted agent or a fixture answerer, through the
real inference gateway. The real-model conditions are **NOT_RUN** until the
owner names a model and its key (OD-1) and a budget (OD-6). No paid
inference was run and no money was spent.

## Repository and implementation

**Starting commit.** `aa0c413`, the head of PR #17 when the directive
began ("Hosted evidence at 7b6cfba: 44 of 44 check runs succeeded").

**Final commit.** The head this report is committed at. At the time of
writing, the newest engineering commits are:

- `dfaf988`, the section 11 demonstration;
- `0d1710a`, the concurrency repairs it found;
- `b7b8e9e`, the Gate G campaign keeping the stages it finished.

PR #17 stays open and unmerged. `main` was not touched, and no history was
rewritten.

**Files changed.** 155 files, +42,114 / −1,011 lines from `aa0c413` to
`dfaf988`, over 31 commits.

### Capabilities completed

| Item | Directive | Ledger |
|---|---|---|
| Security and test-quality repairs; evidence terminology | 3 | D-2026-162 |
| Gate B: the inference gateway (budgets, worst-case reservation under the writer lock, leases, LOST/NOT_SENT recovery, context binding, `proposal-envelope/2`, transcripts never evidence) | 4 (B) | D-2026-163 |
| Gate C: research records decided from verified results; a bounded, resumable controller | 4 (C) | D-2026-164 |
| Gate D: a preregistered grounding experiment, protocol before results | 4 (D) | D-2026-182 |
| Gate E: several agents on the existing substrate; objective assignees | 4 (E) | D-2026-183 |
| Gate F: model contracts; an observation's scientific context | 4 (F) | D-2026-180 |
| Gate G: a staged long-horizon campaign, every counter kept apart | 4 (G) | D-2026-181 |
| Assurance A: deterministic simulation of the gateway and the research ledger | 5 (A) | D-2026-165 |
| Assurance B: six TLA+ models tied to the code by conformance replay | 5 (B) | D-2026-165 |
| Assurance C: trust labels and end-to-end noninterference | 5 (C) | D-2026-166 |
| Assurance D: proof-carrying decisions, verified in a separate process | 5 (D) | D-2026-167 |
| Expiry judged at the time the history reached (clock rollback) | 5 (F) | D-2026-168 |
| Assurance F: freshness policies and re-observation | 5 (F) | D-2026-169 |
| Assurance E: a witness protocol for the log's head | 5 (E) | D-2026-170 |
| Assurance G: an offline adversarial evaluation of the research loop | 5 (G) | D-2026-178, D-2026-179 |
| Assurance H: units, quantity kinds, dimensional statements | 5 (H) | D-2026-177 |
| Assurance I: a deployment isolation contract held to the host | 5 (I) | D-2026-176 |
| Assurance J: a read-only W3C PROV export | 5 (J) | D-2026-175 |
| The causal assurance graph, derived from the history | 6.1 | D-2026-171 |
| An independent policy reference model | 6.2 | D-2026-172 |
| Semantic claim verdicts: citation, admission and semantics apart | 6.3 | D-2026-173 |
| Composed adversarial campaigns | 7 | D-2026-174 |
| One question per step; one run per writable scope | 11 | D-2026-184, D-2026-185 |
| The final integration demonstration and twelve variants | 11 | D-2026-186 |

**Ruled out, with the evidence:**

- **New Python dependencies.** `pyproject.toml` and `uv.lock` are
  unchanged from `aa0c413`. SymPy, Z3 and Pint were assessed for
  Assurance H and not adopted (D-2026-177), and the symbolic dependency
  is OD-5.
- **gVisor.** No untrusted code runs (D-2026-176).
- **Inspect AI and AgentDojo.** They are mapped and not installed, because
  each would be a new dependency and running one is a live evaluation
  (D-2026-179).
- **A PROV validator.** Not added, and the export says what it does not
  represent (D-2026-175).
- **Jev.** Not used as a dependency.

**Gate H (operational and release assurance) is BLOCKED.** Every
deployment control is the owner's (O-1 to O-9, OD-7).

### Existing components reused

Every gate was built on what existed, not beside it. The research loop
asks through the gateway and acts through the existing governed path
(`governed_model`, `governed_tasks`), and the authority store, its
content rule and the second reader (`reconstruct.py`). Proposals enter
through the existing ingress. The adversarial evaluation's `World` is the
world of Gates E, F, G and section 11. The causal graph and the PROV
export are projections of the event log, the decision proofs are derived
by the second reader, and the witnesses use the existing signature
provider.

**Duplicated code removed.** None of note. The work added restatements
on purpose: the second reader restates every new rule, and the
simulations' abstract models are separate on purpose. Each independent
copy is the point of the check it makes.

### Tests and mutation outcomes

| | at `aa0c413` | now |
|---|---|---|
| test files | 133 | 166 |
| mutation specifications run in CI | 102 | 125 |
| mutations | 2,004 | 2,367 |
| TLA+ models | 0 | 6 (29 model runs, each as expected) |

**The full suite.** It passed at the head pushed before this report's
batch (`e07d991`: exit 0, skips only for hosts without IPv6, FEniCSx, FMI
or the isolation requirement), and it runs again before every push.

**Mutations.** Each new mutation was run locally before its commit, and
every one was killed. The 125 specifications also run in CI, in eight
shards.

**Positive controls.** All sixteen Assurance G scenarios, and five of
the twelve section 11 variants (V1, V2, V3, V11 and V12), have a test
that switches the control off and requires the attack to succeed. The
other seven variants exercise controls whose own mutation specifications
already remove them and see their tests fail.

### Independent replay

- **The second reader** (`reconstruct.py`) restates every authority rule
  in its own implementation, and noted 0 anomalies on:
  - every history the integration demonstration produced;
  - every stage of the Gate G campaign so far;
  - the multi-agent runs.
- **The separate-process verifier** (`tools/independent_verify.py`, under
  an import guard on the primary reducers) confirmed:
  - every governed run's history;
  - the demonstration's crashed-and-resumed history;
  - its final decision proofs. A proof with one value altered was
    refused.

### Hosted CI

- **Gates B and C.** 44 of 44 check runs at `76f3d5a`.
- **`4bdd3e1`.** Every workflow succeeded except container-verify. That
  job ran, for the first time, a test that only runs where namespaces
  are refused. The test was wrong, and the executor was right (D-2026-176
  addendum); `e07d991` corrects the test.
- **`e07d991`**, pushed with Gates F, G, D and E: harness-integrations and
  stack-verify succeeded. Agent-substrate, container-verify and
  supply-chain were running when this was written.
- **The heads after it** are recorded by the evidence commit after their
  hosted runs.

**The evidence pin.** It moved from 28 to 23 rows whose hosted evidence
covers the current implementation (R24, R26, R28, R54, R55). This is
honest: the concurrency repair changed `governed_tasks.py`, and the pin
rises only when hosted evidence is attached again.

## Formal and adversarial assurance

### Invariants modelled, and the formal assumptions

Six TLA+ models in `docs/formal/`: `LogAppend`, `InferenceBudget`,
`CrashRetry`, `AuthorityPromotion`, `Invalidation` and `ContextFreshness`.

- **What they state.** Each has safety invariants, and liveness where it
  means something. A broken variant of each is run and must be refuted.
- **How they are checked.** TLC 1.7.4 runs with one worker and fixed
  seeds, so the record reproduces exactly (`tools/formal_check.py
  verify`). The jar is a CI-only test tool and ships in nothing.
- **What they assume.** Finite bounds (two requests, three actors, one
  crash) and the assumptions each specification states, fairness
  included.
- **Once, the model was wrong.** The freshness model let a new policy
  version clear a withdrawal. The code refuses that, so the model was
  corrected.

**What this does not establish.** A model check proves a finite model
under stated assumptions. It does not make the Python implementation
formally verified, and nothing here claims that.

### Implementation against the reference models

- **Conformance replay.** Seeded simulations, and liveness-counterexample
  prefixes, are replayed against the implementation (D-2026-165).
- **The policy engine against its reference.** The engine and a test-only
  Cedar-shaped reference agree over 2,000 generated documents and
  requests, and the replay verifier agrees over 150 recorded histories
  (D-2026-172).
- **Decision proofs.** Proofs are derived by the second reader and
  verified condition by condition in a guarded process; 16 forgeries were
  refused (D-2026-167).

### Deterministic simulation and fault injection

- **The gateway simulation** found a real double dispatch, shrunk to four
  steps, which was repaired (D-2026-165). It now also holds one question
  per key (D-2026-184).
- **The research-ledger simulation** found nothing new.
- **Crash injection** covers every fault point of the research loop:
  before and after inference, after receipt, after the run, the check and
  the decision, and before the step record. A restart with no memory ends
  where an uninterrupted run would have (Gates C, G and section 11).
- **Process-level races:**
  - four processes against one budget;
  - four asking one question;
  - four asking their own questions for one step;
  - two controllers on one objective;
  - two objectives' runs in one scope.

### Adversarial evaluation (Assurance G)

Sixteen scenarios across twelve categories, run through the real
research loop (`docs/assurance/adversarial_eval_results.json`):

| condition | task success | attack success | any invariant broken | attacker's text reached the provider |
|---|---|---|---|---|
| ignores (the attack is placed, the model does not act on it) | 14/16 | 0/16 | 0/16 | 12/14 |
| obeys (the model does what the attack asks, once) | 14/16 | 0/16 | 0/16 | 12/14 |

The two task failures are the exfiltration scenarios. They fail closed:
the context that would carry the secret is refused, so the task is lost
with the attack.

**Before the repairs, three attacks succeeded** (D-2026-179):

- a run at another point decided the verdict;
- a colleague's hypothesis concluded the owner's objective;
- an unregistered credential reached the provider.

### Section 11: the integration demonstration

All twenty lifecycle steps hold on one history. Twelve variants, one per
category the directive names, made 36 attempts, and none was accepted
(`docs/assurance/integration_demo_results.json`; D-2026-186). The
concurrent-writers variant found two defects, repaired before the
demonstration was committed:

- **D-2026-184.** Two controllers asked two questions for one step, so
  the run executed twice or failed both.
- **D-2026-185.** Two runs in one writable scope were each charged with
  the other's writes, which made concurrent research in one deployment
  impossible.

**Defence in depth, measured.** With the gateway's policy-freshness check
removed, the stale call is granted. The independent process then finds
the granted reservation, and the run it led to is rejected.

### Residual vulnerabilities and limits

Each of these is reported with what happened. None is counted as
refused.

- **A gate on admitted evidence is as wrong as the evidence it admits.**
  A wrongly admitted reading passes a false claim as SUPPORTED and
  established (section 11, V2). Gate D measures it (below).
- **A witness attests consistency, not truth.** A trust root whose every
  witness is the attacker's accepts a rewritten history (V10). There is
  no independent witness until the owner provisions one.
- **Actor names are claims on these histories.** The signed-event profile
  exists and is not enabled here. A writer with the disk can make a
  history unloadable, though it cannot make it admit a result (V4).
- **Lost model calls.** Their fate is unknown, and they are charged at
  the reservation. The deployment's recovery settles them after the
  lease; a restarted controller waits (V8).
- **Integrity is not availability.** Overwritten evidence bytes are
  detected and never used, but not restored without a copy held
  elsewhere (V1).
- **Freshness is not the world now.** A fresh reading is current under
  its owner's policy, an assumption about how fast the world changes.
  CURRENT_WORLD is never established (V11).
- **What a context showed is identified, not retained.** Context items
  are recorded by digest and the bytes are not kept (section 11, step
  20). A model's prompt is kept whole in the transcript store.
- **One run at a time per writable scope.** Throughput in a scope is one
  tool at a time, and an unknown writer in the scope still fails a run
  (D-2026-185).
- **Replay cost grows with the history.** Every projection re-verifies
  the chain, and each governed run is re-verified in another process over
  the whole log. Gate G measures the growth (below).

## Scientific and model results

### Scientific checks performed

Each model the loop runs has a registered independent check: for the
slab, a different discretization, closure, integrator and solver. The
check states its own independence, what it shares and what it cannot
show. A result is admitted only on a passed check, by a reviewer who is
not its proposer, and is re-executed byte-for-byte by the verifier.
Thirteen dimensional statements of the three models' equations hold for
their declared units (D-2026-177). The FEniCSx and FMI checks run in
their own runtimes in the harness-integrations workflow.

### Semantic evaluation

The semantic claim checker answers three questions apart (citation,
admission, semantics) and reports five verdicts. Its benchmark
(`docs/research/claim_bench_results.json`) is a DEVELOPMENT benchmark.
Its structured labels are true by construction, and its prose labels were
assigned by the checker's author, not by independent humans.

- **The deterministic route** is exact on 41 of 41 structured claims,
  14 of them established, with no established false positive. It reads
  nothing in prose it cannot read in full: of 13 prose claims it has 0
  false positives and 4 false negatives.
- **The assessed route, on prose,** has 4 false positives of 13, and 0
  established. An assessed SUPPORTED never establishes anything.

A confirmatory benchmark needs independent human labels, which is an
owner decision.

### Actual inference calls and costs

**Zero calls to any model provider.** Every call went to the fixture
provider through the real gateway. Its accounting is in fixture prices,
and nominal; no money was spent.

### Scripted against real-model results

| result | scripted or fixture | real model |
|---|---|---|
| Gate B and C acceptance | scripted agent | NOT_RUN (OD-1) |
| Assurance G adversarial evaluation | ignores / obeys scripts | NOT_RUN (OD-1, OD-6) |
| Gate D grounding experiment | fixture error process | NOT_RUN (OD-1, OD-6) |
| Gate E multi-agent | scripted agents, one fixture lineage | NOT_RUN (OD-1, OD-6) |
| Gate G long horizon | scripted decisions | NOT_RUN (OD-1, OD-6) |
| Section 11 | scripted agent | NOT_RUN (OD-1, OD-6) |

### False acceptance, abstention, unsupported claims and negative controls (Gate D)

The run was preregistered: the protocol and its parameters digest were
committed before the run, and the results by a later commit. It covered
30 seeds and 1,200 questions per condition, with the fixture error
process.

| condition | false acceptance | coverage | unsupported assertions |
|---|---|---|---|
| D1 model alone | 0.464 | 0.823 | 0.517 |
| D4 evidence gate | 0.302 | 0.569 | 0.263 |
| D5 semantic gate | 0.079 | 0.307 | 0.000 |
| D6 state answer | 0.183 | 0.689 | 0.000 |
| D7 governed context + semantic gate | 0.143 | 0.552 | 0.000 |

**Seven of nine hypotheses hold.** Two are falsified:

- **H5.** Outdated retrieval on withdrawn subjects, because the answerer
  is already wrong there.
- **H7.** The governed context costs false acceptance: D7 − D5 is 0.063,
  in [0.050, 0.076], against a bound of 0.02.

All of the gated conditions' false acceptance is on the two negative
controls, where the admitted record itself is wrong. It is zero on every
kind where the record is right. The design cannot establish truth when
the record is false, and the experiment measures exactly that.

### Long horizon (Gate G)

At the 100-decision stage, run in CI, every invariant holds:

- no duplicate executions, receipts or answers;
- no recovery failures;
- no second-reader notes;
- a checkpointed load equal to a full one.

**The staged campaign to 10,000 decisions** runs at `b7b8e9e` and records
each stage as it finishes. The per-decision cost grows with the history,
from about 0.7 s at 100 decisions to about 6 s at 1,450 on this host
while other work ran. At that growth, 5,000 decisions take about half a
day and 10,000 about two days. The stages reached are recorded in
`docs/assurance/research_horizon_results.json` by their own commit. Any
stage not reached is reported with this measurement as the reason, not
claimed.

## Production status

### Owner-controlled blockers

**Owner decisions:**

- **OD-1.** Live inference: the provider and model, how the key is
  provisioned, and a spending cap.
- **OD-2.** Whether live runs may ever run in hosted CI. Recommended:
  never.
- **OD-3.** External retrieval: the source allowlist, licences and
  bandwidth.
- **OD-4.** A human principal for escalations, registered out of band.
- **OD-5.** A symbolic-mathematics dependency and a proof checker, or the
  decision to have none.
- **OD-6.** Inference budgets.
- **OD-7.** Whether a multi-host deployment is intended.

**Owner actions**, from `docs/security/owner_actions.md`:

- **O-1 and O-2.** Stop the ungated agent on the default branch, and
  withdraw its credential.
- **O-3.** Review the agent app's installation.
- **O-4.** Protect the default branch.
- **O-5.** Code owners for the trust surface.
- **O-6.** Retire the default branch's leftover signing workflows.
- **O-7.** Actions defaults.
- **O-8.** A release trust root.
- **O-9.** Repository security features.

None of these was performed here, and none may be without the owner.

### What a deployment still needs

- **Remaining security configuration:** O-1 to O-7 and O-9 on the
  default branch. They were last re-observed at the start of this
  directive, unchanged (D-2026-162).
- **Production keys.** No production signing identity or attestation
  exists. The signed-event profile and the witnesses run on TEST keys
  only, marked as such.
- **An external witness.** None exists. The protocol is the deliverable,
  and an independent witness is the owner's to provision; until then
  nothing claims independent witnessing.
- **Deployment attestation.** The isolation contract says what this
  development container does. A deployment runs the probe under its own
  identity (RUN-3 stays DEPLOYMENT_REQUIRED), and CI containment is not
  production attestation.
- **Release authorization.** It needs the owner's RELEASE trust root (O-8)
  and the hosted OIDC signature. The release gate refuses a digest-only
  candidate, and nothing in this repository can authorize a release.

## Engineering conclusion

**What is complete.** Every requirement that is justified, authorized
and executable offline has been implemented and has met its acceptance
criteria, with these exceptions:

1. **Gate G's later stages.** The 5,000 and 10,000 stages are bounded by
   replay cost that grows with the history. They are recorded as far as
   they run, and the growth is reported as the finding.
2. **Hosted evidence for the newest heads.** It is attached after their
   hosted runs, which raises the evidence pin again.

**What is externally blocked.** Every live-model result (OD-1, OD-6), the
human principal (OD-4), external retrieval (OD-3), symbolic checks
(OD-5), multi-host operation (OD-7), and every deployment, branch,
signing and release control (O-1 to O-9).

**What is not claimed:**

- absolute correctness;
- universal prevention of hallucination;
- full scientific correctness;
- formal verification of the Python implementation;
- independent production witnessing;
- regulatory compliance or certification.

The system refuses what its rules refuse on the histories it was shown.
Where it cannot establish truth (a wrong admission, a compromised witness
set, the world as it is now), it says so, and the measurements above say
how often.
