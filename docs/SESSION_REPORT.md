# Completion report: the agent authority substrate

**Scope.** This report covers `qta_agent/`, its production caller, and the
verification around both. It says what was built, what testing established,
what it found, and what remains open.

**It is not a scientific claim.** `automatic_gate_effect` is `NONE`,
`scientific_PASS_count` is `0`, `measured_in_this_system` is `false`, and
nothing in this layer can read or write a gate. A task reaching `VERIFIED`
means a declared tool ran under kernel-enforced bounds with no network
authority, produced the bytes it claims, and a separated actor confirmed those
bytes are still on disk. That is provenance. It is not scientific validity, not
a measurement, and not hardware.

---

## 1. Where the matrix stands

`docs/completion_matrix.json` is the machine-readable authority, validated on
every test run by `tools/completion_matrix.py`.

| Classification | Rows |
|---|---:|
| COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT | 1 |
| DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS | 31 |
| INTEGRATED_BUT_INCOMPLETELY_VERIFIED | 7 |
| PARTIALLY_IMPLEMENTED / SKELETAL / PLACEHOLDER / ABSENT | 0 |
| BLOCKED | 0 |

38 of 39 rows remain open, and every open row lists what is still missing.
One row is closed. That ratio is the honest picture: this is a working control
plane with a great deal still unproven, not a finished system.

## 2. What testing found

Six defects were found by testing rather than by review. Each is listed with
the technique that found it, because the techniques are not interchangeable.

| Defect | Found by |
|---|---|
| **Separation of duties could be bypassed.** The projection read `executed_by` from a transition payload — a field written by the same actor whose independence was being checked. A worker could complete its own task naming a fictitious executor, then verify it. | Differential comparison against a second reader |
| **A forged record could name its own starting state.** Replay re-authorized against the `src` the record supplied, so every pair in the transition table was available to a forger. One appended line moved a task out of the sealed `VERIFIED` state. | Hostile-agent campaign |
| **The invalidation cascade cited bytes it never stored.** Against an evidence-backed store — the governed configuration — dependency invalidation could not run at all, so a record whose foundation was withdrawn could never be marked `STALE`. | Writing an audit test for the dependency case |
| **A job could be enqueued onto work that can never succeed**, and **a terminal failure did not cascade**, leaving dependents waiting forever on a dead parent. | Hypothesis state machine over the scheduler |
| **The mutation harness destroyed uncommitted work** — `git checkout` cannot tell suite damage from an edit made while it ran. | It happened, twice |
| **Four mutations were silently broken** — two anchors matching nothing, one that no longer parsed, one a no-op — each counted as coverage while testing nothing. | A static sweep of every committed spec |

The pattern worth keeping: mutation testing asks whether a check that *exists*
is load-bearing, and cannot ask whether one is *missing*. Property testing and
a hostile campaign ask the second question. The differential pair asks a third:
whether two readers of the same bytes agree. Each found something the others
structurally could not.

## 3. What the verification consists of

- **1,118 tests** across 25 agent suites (2,204 in the repository).
- **369 mutations** across 15 specifications, each deleting exactly one
  enforcement point; the suite must then fail. Every spec is additionally
  checked *statically* on each run: an anchor must match exactly once, a
  replacement must change something, the mutated source must still parse.
- **Property-based state machines** over the authority, task and job machines,
  plus the capability ledger.
- **Two differential pairs** — authority records and the task lifecycle — each
  a second implementation sharing no reducer with the live projection.
- **A hostile-agent campaign**: sixteen escalating attempts against one shared
  log, then questions asked of the whole history.
- **A crash at every boundary** of a governed run, with a named recovery for
  every state a crash can leave a task in.
- **Fuzzing** with a committed regression corpus.
- **Hosted CI** running all of the above on every push.

## 4. What is NOT established

Stated plainly, because a report that only lists successes is not a report.

- **Nothing scientific has been promoted through this layer.** `PROMOTED` is a
  state reached only in tests. No existing claim, gate, document or canonical
  output has been re-derived under it.
- **One governed workflow**, of the safest available kind — artifact generation
  with no gate effect. The ungoverned Stage-10 adapters remain directly
  callable, so the governed path is additive rather than the only route.
- **The substrate mediates; it does not contain.** The egress guard binds the
  parent process, not the child. The write allowlist lives at the write
  primitive, and the read confinement at the open — neither is a kernel
  sandbox. A subprocess that opens its own socket, or calls `open()` itself,
  is not stopped. `openat2(RESOLVE_BENEATH)` would make read confinement one
  atomic kernel decision instead of a per-component walk, and this Python
  exposes neither `os.openat2` nor `os.RESOLVE_BENEATH`.
- **Read paths are gated for the governed workflow and the evidence store,
  and not universally.** *This was previously listed here as ungated; that is
  no longer accurate and the change is recorded rather than the sentence
  quietly deleted.* Governed verification and every evidence resolution now go
  through a confined primitive: the authorized root is opened once as a
  descriptor, every path component is opened descriptor-relative with
  `O_NOFOLLOW`, `O_NONBLOCK` means a substituted FIFO is refused instead of
  hanging, the *opened* object must be a regular file within bounds, and a
  cited digest binds the result to content rather than to a name. What remains
  ungoverned is any read made by code that does not go through
  `GovernedReader`, and nothing forces a future caller to use it.
- **Separation of duties assumes the parties are distinct.** A compromised
  submitter, worker and verifier acting together are not modelled, and that is
  precisely the assumption that would not survive it.
- **The governed path is not atomic.** There is no two-phase commit. What is
  established instead is that every prefix is a state recovery handles.
- **Hypothesis explores; it does not exhaust.** A property surviving generated
  histories is evidence, not proof. An empty differential diff is evidence, not
  proof: both implementations could share a mistake the log cannot reveal.
- **Single Python version, single OS.** Nothing tests that these guarantees
  hold on another interpreter or kernel.
- **This report is a self-assessment.** No external reviewer has examined any
  of it.

## 5. The honest summary

The substrate does real work under real constraints, and the verification
around it found six defects that review did not — including one that defeated
the package's central claim. That is the argument for the verification, not
for the code.

What remains is most of the point: one workflow is governed, nothing
scientific has been promoted, and the layer that decides what becomes canonical
has never been asked to decide anything that matters. `PASS` remains 0, and
nothing here moves it.
