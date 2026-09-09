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
| COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT | 39 |
| DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS | 0 |
| INTEGRATED_BUT_INCOMPLETELY_VERIFIED | 0 |
| PARTIALLY_IMPLEMENTED / SKELETAL / PLACEHOLDER / ABSENT | 0 |
| BLOCKED | 0 |

No row is open and no residual gap is recorded. **That is not a claim that
nothing is left**, and the matrix is built so it cannot be read as one: a
closed row is one that has said what it does NOT claim and why no
engineering in this repository closes it. The 39 rows carry 94 stated
BOUNDARIES between them. The validator refuses a boundary that describes
work somebody could do here, refuses one that offers no argument, and
refuses one that explains an absent BEHAVIOUR while describing an absent
TEST -- the vocabulary is not allowed to become a way of finishing a row by
rewording it.

PASS remains 0. `automatic_gate_effect` remains NONE. Nothing in this
session moved a gate, a threshold, or the scientific state of the project;
what moved is how much of the software is verified and how precisely its
limits are stated.

## 2. What testing found

Defects found by testing rather than by review, each listed with the technique
that found it, because the techniques are not interchangeable.

| Defect | Found by |
|---|---|
| **Separation of duties could be bypassed.** The projection read `executed_by` from a transition payload — a field written by the same actor whose independence was being checked. A worker could complete its own task naming a fictitious executor, then verify it. | Differential comparison against a second reader |
| **…and the first fix closed half of it.** The claim was compared against the execution record *when there was one*. Omitting the execution record left the claim unopposed, so the cheapest forgery was to fake nothing: one actor created, leased, "executed", completed and VERIFIED its own task. The projection, the independent reconstruction and the audit index all agreed with it. | Asking the first fix's question about the branch where the answer is *nothing* |
| **The audit asked the forger whether the forgery happened.** The executor/verifier gap check exists only for a history that did not go through the gate — and it read the executor out of the forger's own payload, reporting a clean separation of duties over a log in which one actor did both jobs. | The same sweep, applied to the reader instead of the writer |
| **The scheduler's replay never checked who was reporting.** `report()` refuses an outcome from anyone but the live lease holder. That refusal lived on the write path alone, so the rejected worker appended the identical record and the next process to load the queue called the job SUCCEEDED. | Auditing each payload-named field for "what re-derives this on replay?" |
| **A recorded policy decision was folded in unread.** A forged `policy.decision` claiming ALLOW under a rule that does not exist loaded cleanly, and the audit index quoted the invented rule back as the reason something was permitted — even though the record carries the whole request and the verdict is recomputable. | The same sweep |
| **Every grant had a window with one end.** Capabilities, egress grants and secret grants checked expiry and not issuance, so a grant recorded at seq 90 answered "was this permitted at seq 20?" with yes. `issued_seq` was also the record's to name, and content-binding by digest catches tampering, not a self-consistent lie. | The same sweep |
| **A second egress grant under a live `grant_id` replaced the first** on replay, silently widening authority already in force. | The same sweep |
| **A forged record could name its own starting state.** Replay re-authorized against the `src` the record supplied, so every pair in the transition table was available to a forger. One appended line moved a task out of the sealed `VERIFIED` state. | Hostile-agent campaign |
| **The invalidation cascade cited bytes it never stored.** Against an evidence-backed store — the governed configuration — dependency invalidation could not run at all, so a record whose foundation was withdrawn could never be marked `STALE`. | Writing an audit test for the dependency case |
| **A job could be enqueued onto work that can never succeed**, and **a terminal failure did not cascade**, leaving dependents waiting forever on a dead parent. | Hypothesis state machine over the scheduler |
| **The mutation harness destroyed uncommitted work** — `git checkout` cannot tell suite damage from an edit made while it ran. | It happened, twice |
| **The model checker's own I1 path check was redundant** and a mutation deleting it survived -- correctly. "PROMOTED is entered only from VERIFIED" is a claim about one edge, and re-asserting it over paths adds nothing. The claim that no edge check can make is the one now asserted: every path to PROMOTED passes through UNDER_REVIEW first, so a STALE-to-VERIFIED shortcut could not make an invalidated record canonical again with nobody looking at it. | Mutating the checker, and believing the survivor |
| **Eight of the eleven checks in the hardware evidence gate could be deleted with nothing noticing.** That gate decides whether an unreviewed hardware claim becomes evidence, which is the thing PASS staying at zero rests on. Most survived by defence in depth: the record the existing tests used was also missing its raw file, so it was excluded one branch earlier whichever guard was removed, and every test stayed green. | The first mutation matrix ever pointed at the scientific tree |
| **Three checks in the capability module had no coverage at all**, including the mirror of a rule that WAS tested: EXECUTE_TOOL requires a tool_id, and nothing said a grant that is not EXECUTE_TOOL may not carry one. | A generated mutation sample, which knows nothing about the code and therefore has no blind spots of its own |
| **A timeout counted as coverage.** A mutation killed only by the suite's 300-second backstop was scored as killed and reported as a note, so it appeared in the count while saying nothing about which check was lost -- and cost five minutes of every run. | Re-reading what the harness does with its own verdicts |
| **The re-execution check ran for tests and for nothing else.** It read its scratch directory from `self.out_rel`, an attribute only the governed suite's fixture ever set; the Snakemake rule reached that line and raised `AttributeError`. Every governed test passed. It is this repository's recurring defect -- a field populated by nothing -- appearing inside the code written to close a gap about verification being too weak. | Running the production caller with the three arguments the workflow actually passes |
| **The compensation path launched the artifact writer for whatever compensating tool a registry named.** The subprocess module was a literal in three places, so the undo ran a tool that was not the undo; only its own missing inputs stopped it doing something. | Adding a second tool, and finding that "the registry has more than one tool" and "more than one tool can run" were different statements |
| **Re-execution reported agreement over an empty comparison.** A run that declared no output files iterated over nothing and returned "0 artifact(s) reproduced byte-for-byte" -- the same sentence a real comparison produces. | A mutation that survived, and was believed |
| **A mutation matrix scored fifteen mutations against a red baseline.** A test asserting that an ungoverned writer is refused left the forged file on disk when the guard was mutated away, so the suite stayed red for every later mutation and each was recorded as KILLED. The harness's post-run baseline check caught it; the report before that read 17/17. | The harness re-checking its own baseline after the run |
| **The cross-environment collector compared a regeneration against a regeneration.** It looked for the committed canonical copies under `outputs/`, which is gitignored — absent in CI, so 0 of 63 were compared — and which on any machine that has run the pipeline is itself a regeneration. "63 of 63 byte-identical", the sentence R59 rested on, was measured against the wrong side and could not have disagreed. | A hosted anti-vacuity step that required every number to close |
| **…and the test for it read from the same directory.** `test_the_comparison_finds_the_canonical_copies_where_they_live` took names *and* bytes out of `outputs/`, so it asked one copy whether it matched itself. Green throughout. | Fixing the collector and watching the test not notice |
| **The "8-file divergence" was a slice width.** `package_consistency_check.py` printed `stale root copies: {_drift[:8]}` with no count. A run diverging in twenty files printed eight names, and "an 8-file divergence" went into R59's blocker and was chased for weeks. | Forcing a BLAS kernel locally and seeing the checker print exactly eight names while the collector said twenty |
| **The fingerprint named the wrong kernel.** It recorded `numpy.show_config`, which reports the *build* configuration — "Haswell" on this machine — while OpenBLAS DYNAMIC_ARCH *selects* SkylakeX at load time. The one field the whole cross-environment comparison turns on was a different fact from the one it was read as. | Reading the runtime core out of the bundled library to check |
| **The diagnostic that explains a byte divergence was ordered after the check that fails on one.** `set -e` ended the container run at `package_consistency_check.py`, so the 3D comparison never executed in the one case where it mattered. | A hosted container run that finally got far enough to fail numerically |
| **The corpus scan walked into a second virtualenv.** `EXCLUDED_DIRS` named `.venv` exactly; a CI job that builds `.venv-alt` for a second interpreter put 64 site-packages `.txt` files into the corpus and the membership check refused them. The check was right and the scan was looking somewhere no reviewer would put a document. | The second-interpreter job, on its first run |
| **A correction to this session's own conclusion.** It was recorded that the hosted byte check "cannot pass" on a GitHub runner. Run `34296217403` passed it: two jobs of that run drew different machines from the `ubuntu-latest` pool, and one had AVX-512 and reproduced every committed byte while the other could not even select the kernel — exit `-4`, SIGILL. The pool is mixed, so the hosted result depends on the machine drawn, and "expected red" was wrong. | The run that was supposed to confirm the claim |
| **Two hosts at the same forced BLAS kernel still disagreed by three files**, so "it is the kernel" fitted most of the data and was not the whole answer. numpy dispatches its own element-wise SIMD loops from the CPU's features, independently of `OPENBLAS_CORETYPE`. Pinning both reproduces a GitHub runner's result on this machine exactly — 40 of 63, and the same twenty-three files by name. | Not accepting two counts that were close as an explanation |
| **The child process was recorded by pid alone under mutation, and three suites did not notice.** Boot id and start ticks are what make a recorded pid mean anything after the recording process is gone; the record still had a plausible key with a plausible number in it. | A hosted mutation matrix, and believing the survivor |

### A correction to two commit messages

`2f9a977` and `8ad93cc` quote residual-gap counts that are wrong. The true
progression, read from `docs/completion_matrix.json` at each commit, is
28 rows / 35 gaps, then 31 / 25, then 33 / 19, then 34 / 16. The second
message says "25 -> 16" where the matrix said 19, and the third says
"16 -> 13" where it said 16; the first understates its starting count by
one. The matrix was correct at every commit and the prose was typed from a
stale figure. Recorded here rather than fixed in place: the commits are
pushed, and a ledger that quietly loses its own errors is the thing this
document exists not to be.

| **Coverage feedback, added and then measured, does not help here** -- and the first version of it was actively worse, choosing parents uniformly until a valid record was the parent two per cent of the time and coverage went DOWN. Over eight seeds at two budgets the fixed version is a wash (84.8 vs 84.6 transitions at 300 cases; 89.1 vs 88.5 at 600). Recorded because the expectation was the opposite, and because an earlier three-seed sample appeared to show a win. | Measuring the thing that was supposed to be an improvement |
| **Every governed operation cost the whole history.** Each reducer re-reads the log before it decides -- which is what stops a decision being made against a stale projection -- and it did so with a full read. A profile of 120 campaign cycles spent 10 of its 13 seconds inside `read()`, and doubling the campaign quadrupled its wall time. Nothing was wrong with any single operation, which is why it survived: this is the third quadratic path recorded here, and all three were found the same way. | Trying to raise the long-horizon campaign's scale, then profiling why it would not go |
| **The retry budget was never spent by a lease that lapsed** (above), found through the same campaign once it could be run long enough to reach it. | The campaign at the larger scale |
| **A refused gate left no trace.** The scheduler evaluated every policy gate without recording any of them, so a denied attempt was invisible: the log answered "what was permitted" and could not answer "what was tried". | Asking what a policy change mid-flight leaves behind |
| **Four processes each dispatched the same job, and the authority log became unreplayable.** Every append held the writer lock and verified the chain; the damage was semantic. The second record moved a job out of a state the replay had already left, so every later `load()` refused the log -- and a log that cannot be rebuilt cannot be repaired, because the history is the authority. | Four real processes racing on one job |
| **The retry budget was never spent by a lease that lapsed.** `max_attempts` was consulted only where a worker REPORTED a retryable failure. A worker that dies reports nothing, its lease lapses, the job returns to READY, and the next worker takes it -- forever. The budget bounded nothing in the failure mode it exists for. | A six-process mixed campaign, through an assertion written expecting the budget to hold |
| **`reconcile` scanned, then wrote, and could not survive the gap.** Another process dispatching a job between the scan and the write made the whole convergence pass raise, so one contended job stopped every other job from being reconciled. | The same campaign |
| **`verify()` sampled the witness AFTER reading the log** -- the same order the writer writes in -- so another process's ordinary append landed between the two samples and was reported as `TRUNCATED`: damage, for a log that was merely being written to. | The same campaign |
| **A refused lease renewal wrote its record first.** Renewal decides nothing itself; every rule lives in the reducer, on replay. So the record was appended and THEN refused: the caller saw exactly the right exception and every later `load()` hit the same refusal with nothing to catch it. | Asking what append-then-fold means when the fold refuses |
| **The event log ignored `write()`'s return value.** A short write -- fewer bytes stored, no exception -- would have left half a record on disk and advanced the independently-held witness to name it: the exact damage the witness exists to detect, manufactured by the writer. | Injecting ENOSPC and a silent short write at the file object |
| **Three of the completion matrix's own guards had no negative test.** COMPLETE with residual gaps still listed, COMPLETE with no mutation coverage, and COMPLETE with no stated limit could each be deleted with nothing noticing -- in the file that decides whether every other row is telling the truth. | Mutating the validator |
| **…and a fourth was killed by a test that would have passed by running nothing.** The boundary-negative test parametrizes over the COMPLEMENT of a set; widening that set to the whole vocabulary left it parametrized zero times, green, and asserting nothing. | The same mutation run, through a survivor |
| **Four mutations were silently broken** — two anchors matching nothing, one that no longer parsed, one a no-op — each counted as coverage while testing nothing. | A static sweep of every committed spec |

### A repository-state incident, recorded because it is not engineering

Commit `eb2380c1` ("ci: temporary source snapshot for completion run") added
`.github/workflows/source-snapshot.yml` on top of the verified `951f4fa1`. It
was **not** produced by Claude Code and is **not** part of this architecture.
A different assistant, working in an environment that could not clone the
repository, added a workflow that tarred the tree and uploaded it as an
artifact so its own session could read the source.

It is recorded here rather than quietly reverted because a reader auditing
the branch will meet it, and because two things it did NOT do are worth
stating: it is not a required CI job, and it is not evidence for any row.
Nothing referenced it — not the completion matrix, not the workflow contract,
not a production caller, not a mutation spec. It was removed by a forward
commit rather than by resetting the branch, so the history stays honest about
having contained it.

One thing it did do: while it was tracked, `test_repository_manifest_is_in_sync`
failed, because a tracked file the manifest does not cover is exactly what
that guard exists to catch. The guard worked on a change nobody in this
project made, which is the only kind of test of it that counts.

There were two earlier commits of the same shape, `de4aae4d` and `637df55b`,
which added and then removed an earlier snapshot workflow. Their content
cancelled out — the trees at `cc898b47` and `637df55b` are byte-identical —
and later valid work was built on top of them, so they remain in ancestry.

The pattern worth keeping: mutation testing asks whether a check that *exists*
is load-bearing, and cannot ask whether one is *missing*. Property testing and
a hostile campaign ask the second question. The differential pair asks a third:
whether two readers of the same bytes agree. Each found something the others
structurally could not.

The second cluster adds a fourth, which is not a technique but a question, and
the cheapest of the four: taking each field a payload names — `src`,
`executed_by`, `lease_holder`, `attempts`, the verdict, `issued_seq` — and
asking *what re-derives this on replay?* Where the answer was "nothing", the
write path's refusal was advice. Half of these were in code the previous sweep
had already examined and passed, because that sweep asked about **state** and
this one asks about **identity**.

## 3. What the verification consists of

- **1,256 tests** across 26 agent suites (2,327 in the repository).
- **429 mutations** across 16 specifications, each deleting exactly one
  enforcement point; the suite must then fail. Every spec is additionally
  checked *statically* on each run: an anchor must match exactly once, a
  replacement must change something, the mutated source must still parse.
- **Property-based state machines** over the authority, task and job machines,
  plus the capability ledger.
- **Two differential pairs** — authority records and the task lifecycle — each
  a second implementation sharing no reducer with the live projection.
- **A hostile-agent campaign**: twenty escalating attempts against one shared
  log, then questions asked of the whole history. The last four attack the
  identity fields rather than the state fields, and each is tried twice --
  through the call that refuses it, and as the record appended around that
  call.
- **A crash at every boundary** of a governed run, with a named recovery for
  every state a crash can leave a task in.
- **Fuzzing** with a committed regression corpus.
- **A read-only auditor** run by CI over the governed run's own log:
  nine named queries, a finding carried in the exit status, and
  read-only asserted by hashing the log before and after every one.
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
- **The log's `actor` field is an assertion, not an identity.** The replay
  checks now added — the lease holder reports the outcome, the executor comes
  from the execution record — raise the bar from "any string in a payload" to
  "the right name, at the right log position, inside a live lease". That is
  not authentication. Anyone who can write the log can write a name, and the
  log file remains the trust boundary it always was.
- **There is no ISSUER authority.** Any actor that can write the log can mint
  a capability, an egress grant or a secret grant. `issuer_of()` records who
  did, and is deliberately not consulted by `check()`: attribution, not
  authorization.
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
around it found thirteen defects that review did not — including one that
defeated the package's central claim, and a second cluster in code an earlier
sweep had already examined and passed. That is the argument for the
verification, not for the code, and the repetition is the more useful half of
it: a fix that closes the case you imagined is not the same as a fix that
closes the rule.

What remains is most of the point: one workflow is governed, nothing
scientific has been promoted, and the layer that decides what becomes canonical
has never been asked to decide anything that matters. `PASS` remains 0, and
nothing here moves it.
