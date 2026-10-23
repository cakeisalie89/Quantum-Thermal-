# Tranche H and the assurance programme: progress record

This file is a resume aid for a new session. It is not a source of authority,
and it can be wrong: it is written by hand, after the fact. Before relying on
any row, reconcile it:

1. `git log --oneline origin/claude/scientific-python-stack-7rsq3m` names the
   commit each DONE row cites;
2. `uv run python tools/harness_status.py verify` and
   `uv run python tools/readiness.py verify` say what the derived status is;
3. the defect ledger entry a row cites says what was found and what was not.

Where this file and those disagree, they are right and this file is stale.

## State vocabulary

`DONE` (committed, full suite green, hosted CI green at the cited commit),
`COMMITTED` (committed and pushed, hosted CI not yet confirmed),
`IN_PROGRESS`, `PENDING`, `BLOCKED` (names the owner decision), and
`RULED_OUT` (names the evidence for not doing it).

## Programme

| Item | Directive section | State | Commit | Ledger |
|---|---|---|---|---|
| Re-observe the default branch's deployed security state | 3.1 | DONE: unchanged since 2026-10-17; CI-1 to CI-3 stay the owner's (O-1 to O-9) | M1 | D-2026-162 |
| Vacuous and weak test assertions | 3.2 | COMMITTED | M1 | D-2026-162 |
| Exception swallowing and test-only bypasses in trust-critical code | 3.2 | COMMITTED: none found | M1 | D-2026-162 |
| Evidence terminology | 3.3 | COMMITTED: `agent_substrate/evidence_terms.py`, `docs/EVIDENCE_TERMS.md` | M1 | D-2026-162 |
| Gate B: offline inference gateway, budgets, context binding, /2 proposals, process boundary, second reader, third status system | 4 (B) | DONE: hosted 44/44 at 76f3d5a (live inference BLOCKED on OD-1) | Gate B | D-2026-163 |
| Gate C: research records decided from verified results; bounded, resumable controller | 4 (C) | DONE: hosted 44/44 at 76f3d5a (human answers BLOCKED on OD-4) | Gate C | D-2026-164 |
| Assurance A: deterministic simulation | 5 (A) | COMMITTED: gateway and research-ledger state machines; found and repaired a double dispatch | Assurance A/B | D-2026-165 |
| Assurance B: formal state-machine models | 5 (B) | COMMITTED: six TLA+ models, 29 runs as expected, conformance replay; TLC pinned, CI-only | Assurance A/B | D-2026-165 |
| Assurance C: trust labels and information flow | 5 (C) | COMMITTED: label algebra (no transformation grants; only a registered deterministic check adds), projections, end-to-end noninterference with a control | Assurance C | D-2026-166 |
| Assurance D: proof-carrying decisions | 5 (D) | COMMITTED: proofs derived by the second reader and verified, field by field and condition by condition, in a guarded process; 16 forgeries refused | Assurance D | D-2026-167 |
| Clock set back revives an expired policy (found reconciling Assurance F) | 5 (F) | COMMITTED: every expiry judged at the latest time the history reached, in the four readers and the decision proof; snapshot v4 carries it | Clock rollback | D-2026-168 |
| Assurance F: freshness and re-observation | 5 (F) | COMMITTED: typed owner-set policies per source (no universal TTL), standing derived at a stated time, decisions rest only on usable evidence, EXPIRED appended and propagated, re-observation renews; second reader and decision proof restate it | Assurance F | D-2026-169 |
| Assurance E: transparency witnesses | 5 (E) | COMMITTED: checkpoint and cosignature protocol, a witness that cosigns only extensions (rollback and fork refused, persist before answer, no trust on first use), verification against a trust root with reach and staleness; closes M-3 in protocol. Fixture witnesses with TEST keys only; an independent witness is the owner's (O-series) | Assurance E | D-2026-170 |
| Gate D: real-model grounding experiment, offline | 4 (D) | PENDING; the live run is BLOCKED on OD-1 and OD-6 | | |
| Gate E: multi-agent coordination | 4 (E) | PENDING | | |
| Assurance H: units, quantity kinds, dimensional statements | 5 (H) | COMMITTED: exact dimensions and scales for the units the layer writes, agreeing with the FMI table; quantity kinds kept apart from dimensions; degC refused without a kind; thirteen statements of the three models' equations hold for their declared units; the claim checker converts units through a converter it is handed; SymPy, Z3 and Pint not adopted (new dependencies, no demonstrated need beyond this) | Dimensions | D-2026-177 |
| Gate F: scientific research integration | 4 (F) | PENDING | | |
| Gate G: long-horizon reliability | 4 (G) | PENDING | | |
| Assurance G: adversarial evaluation harnesses | 5 (G) | PENDING; live evaluation is BLOCKED on OD-1 | | |
| Assurance I: deployment isolation contract | 5 (I) | COMMITTED: eight dimensions, each KERNEL, APPLICATION or NOT_PROVIDED with what it does not cover; probed through the executor on this host (all hold), each probe shown to see its boundary gone; a stale network claim corrected; gVisor not adopted (no untrusted code runs); attestation stays the deployment's (RUN-3) | Isolation contract | D-2026-176 |
| Assurance J: provenance export | 5 (J) | COMMITTED: read-only, versioned PROV-JSON export built on the causal graph; a model's output only influences, verification is attributed; round-trips to the graph and events of the Gate C history; discloses what it does not represent; no validator dependency | PROV export | D-2026-175 |
| Causal assurance graph | 6.1 | COMMITTED: projection with typed recorded edges and an auditor's explain; the Gate C history withdrawn and the system's cascades, reconstructed by the second reader, agree with the graph's prediction exactly; found that a withdrawn run's notes stayed current (repaired) | Assurance graph | D-2026-171 |
| Independent policy reference model | 6.2 | COMMITTED: a test-only reference (Cedar-shaped permit and forbid sets) agrees with the engine over 2,000 generated cases and with the replay verifier over 150 recorded histories; the second reader now restates the verdict, and four forged verdicts with consistent digests are caught by both readers | Policy semantics | D-2026-172 |
| Semantic claim verdicts | 6.3 | COMMITTED: citation, admission and semantics answered apart; five verdicts; a closed grammar plus research.evaluate (DETERMINISTIC), a checker's opinion reported as ASSESSED and never established; development benchmark with FP/FN (structured labels by construction, prose labels by the author, NOT human); real-model checker NOT_RUN | Claim check | D-2026-173 |
| Composed adversarial campaigns | 7 | COMMITTED: ten campaigns registered with all seven fields; four new compositions (old evidence, poisoned memory, policy change, forged history with its checkpoint) measure 11 attempts, none accepted; seven crossed controls mutated, all killed | Campaigns | D-2026-174 |
| Final integration demonstration | 11 | PENDING | | |
| Gate H: operational and release assurance | 4 (H) | BLOCKED: every deployment control is the owner's (O-1 to O-9, OD-7) | | |

`M1` is the commit titled "Security and test-quality repairs before Gate B";
`Gate B` is the commit titled "Gate B: the inference gateway";
`Gate C` is the commit titled "Gate C: research records and a resumable controller";
`Assurance A/B` is the commit titled "Assurance A and B: simulation and TLA+ models tied to the code";
`Assurance C` is the commit titled "Assurance C: trust labels and noninterference";
`Assurance D` is the commit titled "Assurance D: proof-carrying decisions";
`Clock rollback` is the commit titled "An expiry is judged at the time the history reached";
`Assurance F` is the commit titled "Assurance F: freshness and re-observation";
`Assurance E` is the commit titled "Assurance E: independent witnesses to the log's head";
`Assurance graph` is the commit titled "The causal assurance graph; a withdrawn run's notes go stale";
`Policy semantics` is the commit titled "The second reader restates a policy verdict; a reference model checks the engine";
`Claim check` is the commit titled "Semantic claim verification: three questions apart, an opinion granted nothing";
`Campaigns` is the commit titled "Composed adversarial campaigns across the subsystems";
`PROV export` is the commit titled "A read-only W3C PROV export of the verified history";
`Isolation contract` is the commit titled "A deployment isolation contract, held to the host";
`Dimensions` is the commit titled "Units, quantity kinds and the dimensional statements of the equations".
