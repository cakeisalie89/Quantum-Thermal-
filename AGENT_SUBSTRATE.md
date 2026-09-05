# Agent Authority Substrate — how a claim becomes canonical

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

`qta_agent/` is the authority for how claims *about* this project become
canonical. It is infrastructure, not science. Read §6 before reading anything
else into it.

The machine-readable form of this document is
`authorities.json → authorities.agent_authority_substrate`; the code is the
authority and this document mirrors it.

## 1. Why this exists

The scientific tree answers *what did the model compute?* Nothing in it
answers the questions that surround a long-running process and that such a
process cannot be trusted to answer about itself:

- what is canonical right now, and who made it so?
- what evidence supports that, and has the evidence changed since?
- if a parameter moved, what silently stopped being true?
- if the process died mid-promotion, what is the state?
- can any of it be rebuilt without trusting the running agent?

Each of those is a question about *authority*, and each has a failure mode
where the system keeps working and quietly stops being correct. That is the
class of failure this layer exists to make loud.

## 2. Layering

Each layer depends only on those above it, so the dependency graph is a line
rather than a web:

| Module | Answers |
|---|---|
| `canonical.py` | one byte representation, therefore one digest |
| `events.py` | append-only hash-chained log; the authority history |
| `authority.py` | the transition table: what may become canonical |
| `evidence.py` | content-addressed store: what a cited digest resolves to |
| `capability.py` | authority as a bounded object, not an ambient flag |
| `tools.py` | tool contracts and a default-deny registry |
| `execution.py` | kernel-bounded execution; a timeout is not a success |
| `checkpoint.py` | a cached verification result, never a second truth |
| `store.py` | live projection, transactional through the log |
| `invalidation.py` | transitive consequence of a change |
| `reconstruct.py` | a *second* implementation, for differential verification |
| `tasks.py` | durable work: state that survives the process that started it |
| `_stage10_tool.py` | the subprocess entry point a governed run executes |
| `governed_stage10.py` | **the production caller** — a real workflow, mediated end to end |

**The log is the truth and everything else is derived from it.** A lost
projection costs time, never authority.

## 3. The invariants

`authority.py` is specified as an explicit transition table rather than
scattered conditionals, so the reachable states are enumerable and the
forbidden ones are provably unreachable rather than merely unwritten.

| | Invariant | Why |
|---|---|---|
| **I1** | `PROMOTED` is reachable only from `VERIFIED` | promotion is the moment a claim becomes canonical; allowing it from `PROPOSED` makes verification optional in practice |
| **I2** | `REVOKED` and `REJECTED` are terminal | withdrawn authority that can be re-promoted is not withdrawn; recovery requires a NEW record, which leaves a trail |
| **I3** | `STALE` cannot return to `PROMOTED` directly | a dependency changed, so the prior verification no longer describes the current inputs; the path back runs through `VERIFIED` |
| **I4** | the proposer may not verify their own record | an agent that can propose and verify has no verification, only a more expensive way to assert |
| **I5** | policy cannot self-authorize | a transition is evaluated against the policy recorded when it was requested; changing policy does not retroactively bless past transitions |
| **I6** | every transition requires the evidence its edge declares, **and that evidence must resolve** | see §4 |

## 4. I6 was half a guarantee until the evidence store existed

`check()` originally enforced I6 by requiring each evidence value to be a
SHA-256 digest. That check is *syntactic*. An agent could promote a record by
citing `"a" * 64`: a perfectly well-formed digest of nothing at all.

`evidence.py` closes it. A digest is a *name*; the store is what turns a name
into bytes, and it refuses unless the bytes hash back to the name. With a
resolver attached, a fabricated citation is rejected at the gate:

```python
check(req, resolve=store.contains)                  # transition gate
AuthorityStore(log, evidence=store)                 # also at record creation
```

Creation-time enforcement matters independently: the log is append-only and
hash-chained, so a fabricated citation caught only at promotion would already
be a permanent, unremovable fact.

The resolver is *optional* rather than mandatory, for two reasons that are
not convenience:

1. the state machine must stay testable without a filesystem;
2. a replay verifying historical transitions may legitimately run against a
   store that no longer holds long-expired evidence, and forcing resolution
   there would turn an archival policy into a retroactive authority failure.

**The store does not trust its own layout.** A blob's path is derived from its
digest, but the filesystem is not a party to the integrity claim — files can
be renamed, replaced, or symlinked by anything with write access. Every read
re-hashes and compares. `contains()` verifies by default; the cheap
present-but-unchecked answer is available only as an explicit
`verify=False`, because an unverified containment check answers "is there a
file at that path", which is the question a tamperer wants asked.

## 5. Verification discipline

Coverage says a line ran. It does not say anything would have noticed if the
line were deleted. Every enforcement point in this layer is therefore deleted
in turn, and the suite must fail:

| Matrix | Spec | Mutations | Result |
|---|---|---|---|
| state machine, log, projection, invalidation, reconstruction | `tools/mutations/agent_substrate.json` | 20 | 20 killed |
| evidence store and the gate wired to it | `tools/mutations/agent_evidence.json` | 20 | 20 killed |
| checkpointing, incremental verification, snapshots | `tools/mutations/agent_checkpoint.json` | 23 | 23 killed |

Re-run either with:

```
python3 tools/mutation_matrix.py tools/mutations/agent_substrate.json
```

The harness exits non-zero on a survivor, on an anchor that no longer matches
its source (a silently skipped mutation is a mutation that tested nothing),
and on a source it failed to restore byte-identically. It refuses to start
against a red baseline: all N mutations would "fail the suite" for the
pre-existing reason and the report would read as a perfect score.

A surviving mutation means a check is unprotected — not that it is redundant.

Five mutations survived the first run of the first matrix. Every one survived
for the same reason: the tests provoked corruptions that tripped *two* checks
at once, so deleting either left the other to fail the test. That is the
"passes for the wrong reason" failure — the suite proved that *something*
rejected the input, not that the *specific rule* did. The isolating tests at
the end of `tests/test_agent_substrate.py` each disable exactly one
invariant's worth of input, leaving every adjacent check satisfied.

Two of those five (the terminal-state guard, and the evidence-presence check)
turned out to be genuinely redundant with an adjacent check *under the current
edge table*, and cannot be killed by outcome alone. They are killed by
asserting **which rule** rejected the request, plus a structural test that
injects a hypothetical edge out of a terminal state and shows the guard still
holds. That is the property the guard actually buys: I2 stays true if the
table grows.

Three mutations survived the first run of the evidence matrix, and none of
those was redundant:

- re-putting bytes that are already stored **must not silently overwrite what
  is on disk**. With an intact store both branches write identical bytes, so
  the idempotency test could not see the difference. With a *tampered* store
  they differ completely: the guard reports the corruption, while overwriting
  erases the only evidence that anything was tampered with — during a call
  the caller believes is a no-op. Silent repair is worse than the corruption,
  because the corruption is detectable and a self-healing store is not.
- `list_digests` **must not yield a name that is not a digest**. A stray
  filename yielded from there would be passed straight back into `get`, where
  it raises — but the caller would already have reported it as evidence held.
- the pre-read size check and the streaming one both raise the same
  exception, so the outcome could not tell them apart. Their *messages*
  differ, and the streaming one says the file "grew while being read", which
  for a file that was always too large is false and sends an operator after a
  race that never happened.

Four survived the first run of the checkpoint matrix, all masked the same way
— an adjacent check fired on the same fixture. The isolating fixtures each
leave every other precondition satisfied: a rolled-back head witness on a log
whose bytes are all still present, an anchor naming a real record's real hash
at the real offsets and lying only about its position, and an anchor pointing
at a record whose *stored* hash it faithfully repeats while the record's
contents no longer produce it.

One further defect surfaced from the matrix rather than from a test: removing
the store's file-type check made the suite **hang** on a FIFO instead of
failing, so the mutation was "killed" by a forty-minute harness timeout that
said nothing about which check was lost. The FIFO tests now bound themselves
with `SIGALRM` and fail in five seconds, and the harness reports a
timeout-kill as a defect in the test rather than as a clean result.

The harness also earns its keep on refactors. Extracting the shared
per-record checks out of `verify()` moved five mutation anchors, and the run
reported **ANCHOR DRIFT — tested nothing** for each rather than quietly
scoring them. A mutation whose anchor no longer matches is not a passing
mutation; it is an absent one.

Beyond the matrices, `reconstruct.py` is a deliberately separate
implementation — different data structures, and it does not import
`AuthorityStore` — replayed and compared against the live projection, and
`tests/test_agent_substrate_properties.py` drives the whole machine under
Hypothesis with the invariants asserted on every reachable state.

## 6. What this layer does **not** mean

`automatic_gate_effect = NONE`.

No module in `qta_agent/` is imported by the solvers, by `qta_full_sim.py`, or
by `metrics.py`. It cannot read or write any of the 83 gates. **PASS = 0 is
unaffected by anything it does.**

A record reaching `PROMOTED` here means a claim was proposed, independently
verified, and promoted under a recorded policy. It does **not** mean:

- a gate passed;
- anything was measured;
- hardware exists, or was validated;
- an external party certified anything.

The substrate governs the *provenance of assertions*. It has no opinion about
whether an assertion is physically true, and it never acquires one.

## 6a. Checkpoints cache a verification result, never trust

`EventLog.append` verified the whole chain before every append and
`AuthorityStore.load` verified it again, so N appends cost O(N²) hashing.
That is not an abstract concern: verification that grows without bound is
verification that someone eventually switches off, and the switch-off is
recorded nowhere. Measured at 400 records, the incremental path is 6.6× faster,
and the gap widens quadratically.

A checkpoint says: *at seq K the head hash was H, and a full verification
passed when this was written.* Using it means checking only K+1..N and taking
0..K on faith. Three rules keep that from becoming a second source of truth:

1. **Every use is recorded as weaker.** `EventLog.verify_from` returns a
   report with `prefix_verified=False` and `unverified_through=K`;
   `AuthorityStore.loaded_prefix_verified` says the same about a projection.
   Nothing in this layer can produce a report claiming a full verification it
   did not perform.
2. **The anchor is re-checked, never trusted.** The record at the anchor's
   byte offset must parse, sit at the claimed seq, carry the claimed hash, and
   re-hash to it. Byte offsets are a seek shortcut, not a trust input.
3. **Nothing here authenticates a checkpoint.** Its self-hash catches a
   truncated write or a bad disk. It catches nothing an adversary does —
   anyone who can rewrite the file can recompute the hash. A test asserts this
   *limit* rather than dressing the self-hash up as authentication. Against a
   hostile filesystem, use `EventLog.verify`, which needs no checkpoint and
   trusts nothing.

Tampering inside the checkpointed prefix is therefore invisible to the
incremental check — by construction — and found by the full one. Both halves
are asserted in one test so the limit cannot be read as a bug in one or a
guarantee in the other.

The projection snapshot is stored as **evidence**: canonical bytes in the
content-addressed store, pinned by digest from the checkpoint. The two cannot
drift, because a snapshot whose bytes changed no longer resolves to the digest
the checkpoint names.

## 6b. Authority is an object, and execution is bounded by the kernel

A boolean `may_write` answers the wrong question. The ones that matter are
*who*, *for which task*, *with which tool*, *over which paths*, and *until
when* — and a flag answers none of them. Worse, a flag is ambient: once a
component holds it, everything that component does inherits it. That is the
confused deputy in its purest form.

`capability.py` makes a grant a specific bounded object, checked against the
*request* rather than against the caller's identity. A grant for task T and
tool X gives nothing for task U or tool Y, so a component tricked into acting
on someone else's behalf fails closed instead of succeeding.

Three things it deliberately is **not**:

- **Not authentication.** `subject` is a name the issuer chose. What it gives
  you is that a grant issued *to* that name cannot be used *as* another.
- **Not a secret.** The digest is derived from public fields; anyone who can
  read a grant can recompute it. Unforgeability comes from the issuing record
  in the log — a grant that was never issued does not appear there. A test
  pins this as a limit so nobody later treats the digest as a bearer token.
- **Not expressed in wall time.** Expiry is in sequence numbers. A grant that
  expired "at 10:03" has a different answer depending on whose clock you ask;
  one that expires after seq 41 reads identically for every reader.

Scope matching is by path **component**. `stage10/probe2` is a string prefix of
`stage10/probe` and a different directory; a `startswith` check grants the
sibling. Writing this test found a second, sharper case: a scope of `"."` has
*empty* `.parts`, so it slips a traversal check and is the parent of every
relative path — the one scope value that looks narrow and is total.

`tools.py` is default-deny. An unregistered tool does not run — not "runs with
reduced privileges", not "runs and is logged". The registry is frozen after
construction, so what may run cannot depend on import order. Determinism and
side-effect class are *declared*, because whether a re-run difference means
tampering or just means the tool never promised reproducibility is not
something a verifier can work out afterwards.

`execution.py` runs tools in a subprocess, because an in-process call cannot be
bounded: it shares the caller's memory, descriptors, environment and lifetime.
CPU, address space, output size, process count and wall clock are enforced by
the kernel against a process the caller does not share, `setsid` puts it in its
own group so a timeout kills the whole tree rather than orphaning a grandchild,
and the environment is **replaced** rather than inherited — inheritance is how
a tool acquires credentials nobody granted it.

Output is capped with `RLIMIT_FSIZE` on real files rather than by a counter on
a pipe. A counter caps what you *keep*; the kernel caps what is *produced*, and
the difference is the whole point when the producer is hostile.

Three outcomes are not success, and collapsing any of them into `COMPLETED`
would be the failure that matters most:

| | |
|---|---|
| `TIMED_OUT` | it may have finished one instruction before the deadline; nothing observed it finish |
| `CANCELLED` | it stopped when asked — retryable, where a rejection is not |
| `DENIED` | it never ran, and must never be reported as a failed run |

`COMPLETED` means the process exited 0. That is a statement about the process,
not about the result; whether the output is acceptable is a verification
question answered elsewhere, deliberately by someone else.

## 6c. The production caller

Everything above was, until now, a mechanism with no consumer. A governance
layer that governs nothing is a library, and a library cannot be wrong in the
way a control plane can — none of these guarantees had ever met a workflow that
does real work and produces real artifacts.

`governed_stage10.py` puts one through the whole chain:

```
intent → task record → validation → capability grant → lease
       → bounded subprocess execution → output capture
       → content-addressed evidence → provenance binding
       → independent verification → authority → durable state
```

all of it appended to the hash-chained log, so "what was this task doing when
the machine died" is answered by **replay rather than by inference**.

The workflow is Stage-10 artifact generation: real (the Snakefile runs it, it
writes files somebody reads), safe (`automatic_gate_effect` is NONE, and it
writes only inside `verification/stage10`), and touching no scientific
authority.

What keeps it from being cosmetic:

- work runs as a bounded **subprocess**, not an in-process call — the execution
  record carries a real exit status, which a function call does not have;
- the capability is minted for *that* task and *that* tool and expires with the
  lease; the executor refuses without it;
- every produced file is hashed into the evidence store, and the completion
  cites those digests — not a summary, not a log line;
- verification is done by a **different actor** and re-derives the digests from
  the files on disk, so a completion citing bytes that are no longer there
  cannot be verified;
- replay **re-authorizes** every transition, so a forged log entry becomes a
  permanent record that it was attempted and never becomes state.

### The boundary this crosses, and the one it does not

The bridge imports `qta_multiphysics.stack.workspace`. That direction is
required — governing a workflow means calling it — and it is allowed only for
two named modules reaching one named thing, enforced by
`tests/test_agent_substrate_isolation.py`.

The other direction stays absolutely forbidden. Nothing in the scientific tree
imports `qta_agent`, and no module here may reach a solver, `metrics.py`,
`qta_full_sim.py` or an FSM — because that is the direction in which an
authority verdict could change a computed result.

The bridge writes **through** the Stage-10 write guard rather than around it, so
a governed run is subject to exactly the same allowlist as an ungoverned one.
The substrate adds authority; it does not replace the guard already there.

### What a VERIFIED task means, and what it does not

It means a declared tool ran under a bounded environment, produced the bytes it
says it produced, and a second actor confirmed those bytes are still there.
That is a statement about **provenance and nothing else**. It is not a claim
that the result is scientifically correct, that anything was measured, or that
any gate moved. PASS remains 0, and no gate is reachable from here.

## 7. Status: a production caller exists; most of the system does not yet

`PROMOTED` in the authority state machine is still a state reached only in
tests — no existing scientific claim, gate, document or canonical output has
been re-derived under it.

What HAS changed is that `qta_agent` is no longer consumer-less: a real
Stage-10 run now goes through task lifecycle, capability, bounded execution,
evidence and independent verification, and its state survives the process that
produced it.

The honest scope of that: **one workflow, of the safest available kind.** The
machine-readable status is `docs/completion_matrix.json`, validated on every
test run by `tools/completion_matrix.py`. It is the authority for what is done
and what is not, and most rows are still open.
