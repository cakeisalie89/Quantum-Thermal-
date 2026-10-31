# Formal models of seven protocols (Assurance B and Phase E)

Seven small TLA+ specifications, checked by TLC, each with deliberately broken
variants that must be refuted. Each is tied to the Python it describes by
`tests/test_formal_conformance.py`. The record is
[`results.json`](results.json) (outcomes, state counts, counterexamples) and
[`traces.jsonl`](traces.jsonl) (seeded random behaviours of the correct
models). Both are produced by `tools/formal_check.py`.

**What a "no error" here means.** TLC explored every reachable state of a
finite model (two requests, three actors, one crash) under the assumptions
the specification states, fairness included. It found no state violating a
listed invariant, and no fair behaviour violating a listed liveness property.
That is a statement about the model. **It is not a proof that the Python
implementation is correct.**

## The models

| Protocol | Spec | Code it describes | Distinct states | Safety | Liveness | Broken variants, each refuted |
|---|---|---|---|---|---|---|
| 1. Authority promotion, separation of duties | `AuthorityPromotion.tla` | `authority.EDGES`, `AuthorityStore`, `PolicyStore` | 1,121 | separation of duties; PROMOTED only from VERIFIED; promoted only under a policy in force; terminal is final | none: terminal states are final by design | proposer verifies (I4 dropped); review promotes directly; withdrawn policy promotes |
| 2. Concurrent append | `LogAppend.tla` | `EventLog.append_decided` | 6,485 | chain intact (no fork, no lost update); nothing after a torn line; no duplicate after a crash-retry; one writer at a time | every intent is recorded | no lock; append over a torn tail; non-idempotent decide; no operator repair (liveness) |
| 3. Budget reservation and settlement | `InferenceBudget.tla` | `InferenceGateway` | 20,574 | never overspent; one open and one OK attempt per request; dispatched once; nothing settled while its call is in flight; held equals the ledger | every reserved attempt is settled | budget checks only what is held (IG8); double dispatch (D-2026-165); dispatch past the lease; recovery ignores the lease; no recovery (liveness) |
| 4. Crash recovery, retry, idempotency | `CrashRetry.tla` | `governed_tasks` (binding, lease, decided execution record, `recover`, `_resume`) | 442 | one task per key; recorded once; recorded only by the lease holder; external effects at most once | the request reaches an outcome (done, or handed to a human) | rebinding a key; no lease check at record; re-running an external tool; no recovery (liveness) |
| 5. Dependency invalidation | `Invalidation.tla` | `invalidation.plan_from`, `GovernedModelRuns.settle` | 244 | every plan reaches everything resting on the withdrawn origin that still stands; only that is made STALE, and only from a state with an edge to STALE | once the origin is withdrawn, consistency is reached and kept | one level only; traversal stops at a SUPERSEDED record; no settle; over-eager plan |
| 6. Context and policy freshness at decision time | `ContextFreshness.tla` | `InferenceGateway._policy_problem` under the writer lock | 91 | every decision was made on a context whose policy was in force, and not withdrawn, when written | none | check at build only; check then act (outside the lock); ignore withdrawal |
| 7. Witness quorums against a split view | `WitnessQuorum.tla` | `witness.TreeWitness`, `TrustRoot`, `verify_tree_witnessed` | 745,361 | no split view: every two trees a quorum has ever witnessed are consistent, with four witnesses, a quorum of three and one faulty witness (one that cosigns anything, or lost its state) | none | a quorum of two, not a majority (AUD-018); a witness that accepts any tree no shorter than its own; a re-provisioned witness not counted faulty (AUD-019) |

Every correct model passes; every broken variant fails on the property it
targets (33 model runs). For a liveness violation only the verdict is
recorded: TLC checks liveness on its own timed schedule, so the number of
states it had seen, and the lasso it reports, differ between runs. TLC 1.7.4 runs with one worker and fixed seeds, so
`formal_check.py verify` reproduces the record exactly. CI runs it with the
jar fetched from the tlaplus release and refused unless its sha256 is the
pinned one.

## How the models are tied to the code

`tests/test_formal_conformance.py` (no Java needed):

- **Edge for edge.** The edge table in `AuthorityPromotion.tla` is compared
  with `authority.EDGES`: source, destination, roles and separation of duties.
- **Behaviours replayed.** TLC's seeded simulations of the correct models
  are replayed step by step against the real `AuthorityStore` and `PolicyStore`, the real
  `InferenceGateway` and the real invalidation planner. Each step must be
  accepted, with the implementation's state matching the model's.
  - Authority: the record's state.
  - Budget: what is held, each side in its own units, against the ledger the
    actions imply. One model tick is `LEASE_GRACE_S`, which is also the
    request's timeout, so the model's lease arithmetic is the gateway's.
  - Invalidation: every plan the model makes is the one `plan_from` computes
    from the same states.
- **Counterexamples refused.** Each safety counterexample of a broken variant
  is replayed, and the implementation must refuse it, writing nothing it would
  not record, before the violating state.
- **Witnesses, tree for tree.** For every pair of protocol 7's trees, a real
  `TreeWitness` holding one cosigns the other exactly when the model's witness
  does. The counterexamples of `small_quorum` and `loss_uncounted` are replayed
  with real witnesses: the first is now refused by the trust root, and the
  second needs one more faulty witness than the quorum tolerates
  (`tests/test_witness_quorum.py`).
- **Named tests for the rest.** Protocols 2 and 4 need real processes (flock,
  subprocess tools) and are not replayed here. Each of their properties is
  tied to named tests of the implementation, and the conformance test fails
  if one stops existing.

`tools/mutations/formal.json` shows the tie holds. It has ten mutations: eight
make the implementation drift from a model, and two weaken the record checker.
The conformance tests alone kill all ten.

## What the models found, and what they state

- **The model was wrong, the code right.** `ContextFreshness.tla` first let a
  new policy version clear a withdrawal. `PolicyStore` refuses to publish a
  withdrawn policy again, so the model was more permissive than the code. It
  was corrected before any result was recorded. A model is only worth what
  ties it to the code.
- **A torn tail stops every writer until an operator repairs it.** The log
  fails closed on a torn final line. TLC showed that liveness needs STRONG
  fairness for the operator's repair: a refused writer retrying keeps taking
  the lock. This is the availability cost of failing closed, stated here
  rather than hidden.
- **The lease must outlast the tool.** Protocol 4's liveness holds when a
  live worker finishes inside its lease. A stalled worker is modelled as an
  adversary, and its late record is refused. A system configured with a lease
  shorter than the tool's wall-clock limit would keep losing the race.
- **Separation of duties is two-person, not three.** I4 separates the
  proposer from the verifier and from the promoter. It does not separate the
  verifier from the promoter: one actor may do both (`withdrawn_policy`'s
  trace shows it). Requiring a third party is a policy decision for the owner.
  It is not changed here.
- **A record-origin cascade has no settle.** `settle()` finishes cascades from
  a withdrawn governed task. A cascade from a record origin
  (`apply_invalidation`) that a crash cuts short is reported by the audit, not
  finished. The model covers the task-origin protocol only.

The deterministic simulations (Assurance A, `tests/test_simulation_*.py`)
found a real defect before any of this ran. The gateway's dispatch writer
checked a copy of the dispatch rule that left out "already dispatched", so a
second dispatch was appended and the gateway's own replay then refused the
history (D-2026-165). `InferenceBudget.tla`'s `double_dispatch` variant is
that defect, and the conformance test replays its counterexample against the
fixed gateway.

- **A quorum must be a majority, and a forgetful witness is a faulty one.**
  Protocol 7 holds exactly when two quorums share more witnesses than can be
  faulty: 2q - n > f. TLC refutes a quorum of two of four with one faulty
  witness, and the trust root accepted any quorum from one to n: with a
  quorum of half or fewer, two disjoint groups of honest witnesses can each
  witness one fork (AUD-018, now refused). A witness that lost its state and
  was provisioned again cosigns a fork of what it forgot; counted as honest,
  it gives the operator a split view (AUD-019). The code cannot see that a
  witness forgot, so counting it is the trust root owner's to do.

## Not claimed

- That the Python implementation is formally verified. The specifications are
  written by hand; the conformance tests sample behaviours, they do not
  enumerate them.
- Anything about states outside the finite models: more requests, more
  actors, more crashes, other timings.
- Liveness without the fairness each specification states (for example,
  strong fairness of the operator's lock acquisition in protocol 2).
- Protection against an adversary who can write to the log file directly. The
  authenticated-history profile and the witnesses are about that.
- For protocol 7: signatures and hashes (a cosignature is its witness's alone,
  and a tree is its sequence of records), more than four witnesses or
  histories of more than three records, and the readers themselves, beyond
  accepting what a quorum witnessed.

## Running it

```
uv run python tools/formal_check.py check                 # no Java
uv run python tools/formal_check.py fetch --dest /tmp/tla  # pinned jar
uv run python tools/formal_check.py verify --jar /tmp/tla/tla2tools-1.7.4.jar
uv run python tools/formal_check.py run --jar ...         # rewrite the record
```
