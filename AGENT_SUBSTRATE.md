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
| `store.py` | live projection, transactional through the log |
| `invalidation.py` | transitive consequence of a change |
| `reconstruct.py` | a *second* implementation, for differential verification |

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

One further defect surfaced from the matrix rather than from a test: removing
the store's file-type check made the suite **hang** on a FIFO instead of
failing, so the mutation was "killed" by a forty-minute harness timeout that
said nothing about which check was lost. The FIFO tests now bound themselves
with `SIGALRM` and fail in five seconds, and the harness reports a
timeout-kill as a defect in the test rather than as a clean result.

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

## 7. Status: built, verified, and not yet driving anything

The substrate has **no production caller**. Its only consumers today are its
own test suites, and `tests/test_agent_substrate_isolation.py` enforces that
by name: adding a consumer means editing `ALLOWED_IMPORTERS`, which makes the
addition visible in review rather than incidental.

This is stated plainly because the alternative reading — that a repository
containing an authority layer is a repository whose claims flow through one —
is false here and would be the more flattering thing to imply. What exists is
a verified mechanism. Nothing has been promoted through it, and no existing
claim, gate, document, or output in this project has been re-derived under it.

Wiring it to a real workflow is a separate change, and one that has to name
which claims it governs and who holds each role. Until then, `PROMOTED` is a
state this code can reach in its tests and nowhere else.
