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
| Assurance F: freshness and re-observation | 5 (F) | PENDING | | |
| Assurance E: transparency witnesses | 5 (E) | PENDING | | |
| Gate D: real-model grounding experiment, offline | 4 (D) | PENDING; the live run is BLOCKED on OD-1 and OD-6 | | |
| Gate E: multi-agent coordination | 4 (E) | PENDING | | |
| Gate F and Assurance H: scientific integration, symbolic checks | 4 (F), 5 (H) | PENDING; new dependencies are OD-5 | | |
| Gate G: long-horizon reliability | 4 (G) | PENDING | | |
| Assurance G: adversarial evaluation harnesses | 5 (G) | PENDING; live evaluation is BLOCKED on OD-1 | | |
| Assurance I: deployment isolation contract | 5 (I) | PENDING | | |
| Assurance J: provenance export | 5 (J) | PENDING | | |
| Causal assurance graph | 6.1 | PENDING | | |
| Independent policy reference model | 6.2 | PENDING | | |
| Semantic claim verdicts | 6.3 | PENDING | | |
| Composed adversarial campaigns | 7 | PENDING | | |
| Final integration demonstration | 11 | PENDING | | |
| Gate H: operational and release assurance | 4 (H) | BLOCKED: every deployment control is the owner's (O-1 to O-9, OD-7) | | |

`M1` is the commit titled "Security and test-quality repairs before Gate B";
`Gate B` is the commit titled "Gate B: the inference gateway";
`Gate C` is the commit titled "Gate C: research records and a resumable controller";
`Assurance A/B` is the commit titled "Assurance A and B: simulation and TLA+ models tied to the code";
`Assurance C` is the commit titled "Assurance C: trust labels and noninterference";
`Assurance D` is the commit titled "Assurance D: proof-carrying decisions".
