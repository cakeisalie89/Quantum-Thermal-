# PR #17 adversarial audit: progress record

This is the audit's working record. It says what was examined, at which
commit, by which commands, and what is left. Three files hold the state,
and this one points at them:

- `docs/audit/pr17_audit_ledger.json`: one entry per changed file. Each
  entry's disposition is bound to the blob it was made against
  (`tools/audit_ledger.py`).
- `docs/audit/findings.json`: the findings register.
- this file: the sessions, commands and next actions.

A file is reviewed only if the ledger says so and names its blob. This
record's prose never stands in for the ledger.

## Session 1 (2026-10-27)

**Baseline audit head.** `b0058f62503edaaccef26d8ed6a25df4602bf039`.

**Merge base.** `096fb90fff089a62e4b20513de729a3e45a69f35`, which is
`origin/main`. `main` has not moved, and nothing is in `main` that is not
in the head.

### Phase 0: repository state

**How the clone was found.** It was shallow, and the local PR branch
pointed at `main` (096fb90). It was unshallowed with
`git fetch --unshallow origin`, and the local branch was set to
`origin/claude/scientific-python-stack-7rsq3m` (b0058f6). The local branch
had no commits of its own, so nothing was lost.

**The change set, from git and from the GitHub API independently.**

- `git -c diff.renameLimit=0 diff -M --name-status 096fb90 b0058f6` gives
  989 files: 573 added, 384 deleted, 32 modified.
- With rename and copy detection, and no limit, there are no renames. The
  `qta_agent` package was created and renamed to `agent_substrate` inside
  the PR, so against `main` both are additions. The deletions are `main`'s
  retired hardware tree.
- The totals are +294,166 / −89,247 over 265 commits, none of them merge
  commits.
- `gh api repos/cakeisalie89/Quantum-Thermal-/pulls/17` reports the same
  figures: open, unmerged, head b0058f6, base 096fb90.
- The API's file list (10 pages of 100) agrees with the ledger path by
  path, status by status and blob by blob:
  `python tools/audit_ledger.py crosscheck <saved API list>` reports 0
  disagreements.

**The tree at the head.**

- 605 files.
- No symlinks, no submodules, no untracked or unignored files.
- Two executables: `container_verify.sh` and `tools/verify.sh`.

**Binaries.** Twenty change: 19 deleted and 1 added. Each was inspected
by format, and the methods are recorded on its ledger entry.

- **Twelve git bundles.** Their heads were listed and each was cloned
  into its own bare repository. Three are thin bundles, verified against
  this repository. Every reachable blob was scanned for secrets: 0 hits.
  Six heads exist nowhere else in the repository (AUD-001).
- **Four tarballs and a zip.** None holds a symlink, hard link or
  traversing path. Each was extracted into its own directory and scanned:
  0 hits.
- **The PDF and the HDF5 file.** The PDF's header and info dictionary
  were read, and the HDF5 file was opened with h5py (562 objects).
- **The added `checkpoint_final.safetensors`.** Its header was parsed
  without a library: 37 contiguous F32 tensors, no pickle. Its sha256
  equals the digest in `checkpoint_manifest.json`.

**Duplicates among the deleted files.** `QTA_full_history-6.bundle.txt`,
`attic/.../QTA_full_history-6.bundle.txt` and
`attic/.../QTA_full_history.bundle` are one blob, a869c0e. The two
`QTA_machine_fsm` bundles are one blob, 3b8bb65.

**Secret scan of every changed blob, added and deleted.** There are two
hits, and both are test fixtures:

- a header-only `-----BEGIN RSA PRIVATE KEY` used to test credential
  refusal (`tests/test_proposal_ingress.py:138`);
- AWS's published documentation example key, in a deleted test.

**Environment.** `uv sync --frozen --all-groups --extra neural`, as CI
does, succeeded in a fresh container: Python 3.12, 4 cores, 15 GB RAM.

### Ledger state at the end of this phase

20 files are INSPECTED_BINARY and 969 are NOT_REVIEWED. The ledger is the
authority for these counts; run `python tools/audit_ledger.py summary`.

### Next actions

1. Phase 1: rebuild the architecture and the authority-flow graph from
   the code.
2. Phase 2: review the event log, anchors, checkpoints and projections
   line by line, with adversarial tests and work counting.

## Session 2 (2026-10-27, resumed after a context reset)

**Reviewed commit.** The audit's own Phase 0 commit, c11c628, on top of
the baseline b0058f6.

**The full suite at c11c628** (`HARNESS_HISTORY_REQUIRED=1 pytest tests/`,
in a worktree, 34 minutes): 6,253 passed, 19 skipped, 1 failed. The
failure was the audit's own: `tools/audit_ledger.py` held an `assert`,
which `tests/test_no_enforcing_asserts.py` refuses in production code
because `python -O` deletes it. It is now a refusal. c11c628 was never
pushed alone.

### Phase 2A: the event log, read in full

`agent_substrate/events.py`, `checkpoint.py`, `store.py`,
`authority.py`, `canonical.py` and `projection.py` were read in full.
Their ledger dispositions are recorded after this cluster's commit,
against the blobs it leaves.

Four findings came out of the log itself (`docs/audit/findings.json`):

| Finding | What was wrong | Status |
|---|---|---|
| AUD-002 (P1) | A final record that lost only its newline verified, and the next append fused two records into one line | FIXED |
| AUD-003 (P2) | Anchors and checkpoints were built by a second, unverified read | FIXED |
| AUD-005 (P2) | Any encoding of a record verified, and the readers framed records differently | FIXED |
| AUD-004 (P1) | I3's "fresh evidence" after STALE is not enforced: the old report survives the evidence merge | OPEN |

**Before and after.** `repro/event_log_framing.py` (scratchpad) was run
against c11c628 and against the fix:

| Case | c11c628 | Fixed |
|---|---|---|
| final record without its newline: full verify | ok=True, 3 records | ok=False, torn tail, 2 records |
| ... then append | returns seq 3, the line is fused | refused, nothing written |
| the same, from a running writer's anchor | returns seq 3 | refused (MalformedEvent) |
| CR in place of a newline: full verify | ok=True | ok=False |
| ... `anchor_at(1)` | bare JSONDecodeError | MalformedEvent |
| CR in a record's whitespace: anchored / full | ok=True / ok=False | refused / refused |
| key given twice | ok=True; fold sees "a", first-wins parser "mallory" | ok=False |
| record re-encoded (spaces, key order) | ok=True | ok=False |
| control: untouched log, full / anchored | ok / ok | ok / ok |

**The fix** (one cluster):

- `_parse_line` is the one rule for every reader: a record is its
  canonical bytes and one newline, framed at the newline only.
- An unterminated final line is a torn tail.
- The write path refuses to write after unterminated bytes.
- `VerifyReport.verified_anchor` is the anchor of the verified head,
  taken from the same pass. The 13 projection sites and
  `checkpoint.create()` use it.
- `anchor_at(seq, head_hash=...)` refuses a record other than the one
  named.

**Validation, at this cluster's commit.**

- **Pre-fix failure.** Each new test fails on c11c628, checked in a
  worktree with the new test files copied in: 6 line-rule tests, 3
  crash-recovery tests, the write-guard test and 3 anchor tests. Each
  passes on the fix, and the two controls pass on both.
- **Mutation.** Twelve targeted mutations, run through
  `tools/mutation_matrix.py`, were all KILLED: R53, R60, R61, F2, F9,
  F10, E26, SA1, N10, N11, M52 and M60. Each run had a green baseline
  and a green null control, and every source was restored
  byte-identical. Five of these existed already; their anchors were
  moved to the new code without changing what they remove.
- **Probe signatures.** Five test probes wrap `EventLog.read` with a
  fixed signature. They now forward keyword arguments. Nothing they
  assert changed.
- **Cost, on a 20,000-record log (8.7 MB).**
  - A full verified read went from 0.343 s to 0.463 s: the canonical
    check is one more serialization per record. Peak memory fell from
    35.2 to 27.5 MB, because the per-line copy list is gone.
  - Loads no longer make the second `anchor_at` pass (0.112 s), so a
    load costs about the same as before.
- **Harness evidence.** The evidence-coverage pin moves 47 → 23. The 24
  rows citing the changed code now predate their hosted evidence until
  the next hosted run.

### The full suite at f142a7e, and what it found

`HARNESS_HISTORY_REQUIRED=1 pytest tests/` in a worktree at f142a7e, 34
minutes: 6,259 passed, 19 skipped, 5 failed, 6 errors. None of these is a
defect in the repair. Each is a place that wrote, or claimed, what the
repair made untrue.

- **Three test fixtures and one demonstration tool modelled a forger.**
  They rewrote the log with `json.dumps(sort_keys=True)`, which is not the
  canonical encoding, "a rewrite every local check accepts". The line rule
  now refuses that encoding locally. A forger with the disk would write
  canonical bytes, and every local check still accepts those. The helpers
  now write canonical bytes, so they still show what they claim: only a
  witness catches a re-chained history.
  - `tests/test_composed_campaigns.py` (K4).
  - `tests/test_witness.py`, two tests.
  - `tools/integration_demo.py`'s altered-history step, which raised in
    every one of the six `tests/test_integration_demo.py` tests that run
    it.
- **Readiness rows SUB-1 to SUB-4 (two failures).** These said
  HOSTED_TESTED, bound to the code hosted CI ran at 927b69c, and that code
  has changed. `tools/readiness.py` refused them, as it should. They
  return to IMPLEMENTED until a hosted run covers the new code. The
  927b69c evidence is kept on each row as `hosted_before`.

### Cluster 2: AUD-006 and AUD-007, fixed

- **AUD-006.** `events.require_extends()`: before a catch-up folds
  anything, the log must still hold the record at the projection's
  position, with the hash the projection folded there.
  - Each projection keeps that hash beside its position, wherever the
    position moves, its own appends included.
  - Both catch-up routes call the check. The refusal is remembered until
    `load()`.
  - The proposal ingress and the idempotency ledger already rebuilt from
    scratch on the full-read route. They now also refuse on the anchored
    route.
- **AUD-007.** `AuthorityStore._apply` now refuses an unreadable record as
  a `StoreError` naming its seq and action. A transition of a record never
  created is refused by name. The second reader reports a dependency
  record that names no dependency as an anomaly, instead of applying it.

**Validation.**

- **AUD-006 tests.** There are 33, in `tests/test_agent_incremental.py`:
  three refusal tests over eight projections each, the control over the
  same eight, and a check that the modelled restore really diverges.
  - On f142a7e, 22 of them fail with "DID NOT RAISE": every folding
    projection on every route, and both rebuilding projections on the
    anchored route.
  - Two fail only because they read the new hash attribute: the rebuilding
    projections' full-read route was already safe.
- **AUD-007 tests.** All 7 fail on f142a7e with the bare errors.
- **Mutation.** 22 targeted mutations were all KILLED, each run with a
  green baseline, and every source was restored byte-identical:
  - N12 to N27: the check's two refusals, and each of its 14 call sites;
  - M61 to M63;
  - three existing mutations whose anchors moved: V_store_fallback,
    V_scheduler_fallback and TC2.
- **A false failure, and its cause.** One subset run failed
  `test_proposal_concurrency` with FULL_REPLAY_FOREIGN_REDUCER. I had
  moved a function in `events.py` while that run's worker processes were
  starting, and the reducer identity is a digest of the source files. Run
  alone, it passes. No source has been edited during a test run since.

### Still open

- **AUD-004 (P1).** I3's "fresh evidence" after STALE is not enforced.

### Found, then fixed in cluster 2

- **AUD-006 (P1).** A live projection, after a restore of the log and
  its witness from a backup, folded the new history's tail onto the old
  history's state: r0, r1, r3 against a fresh replay's r0, r2, r3. It
  then appended a transition on r1. Every load of that log now fails.
  - Two routes reach this: the full-read fallback, and an anchor that
    lags the projection's own appends.
  - Planned fix: each projection remembers the hash of the last record
    it folded and refuses to catch up from a log that no longer holds it.
- **AUD-007 (P2).** Replaying a transition on a record never created
  raises a bare `KeyError` in the primary store. The second reader
  reports it as an anomaly.

### Phase 2C: where the full reads are (measured, not yet reduced)

A scratch profiler wraps the read paths from outside. Over a 60-decision
campaign it counted 218 full verified reads, which parsed 67,697
records:

- `GovernedTasks.projection()`: 80 reads.
- `GovernedTasks.__init__`: 49 reads. It loads seven stores, each with a
  full read.
- `governed_model._producers`: 20 reads.
- The rest: `adversarial_eval` 22, `research_horizon` 19, `proposals` 5,
  `_head_seq` 5, checkpointing 2.

### Next actions at the end of session 2

1. Run the full suite at the cluster 2 commit, then push it.
2. AUD-004: test the scientific-admission rule through the governed path,
   then fix the edge evidence rule in the store and in `reconstruct.py`.
3. Record REVIEWED_IN_FULL dispositions for the six files above.
4. Phase 2C: commit a work-count instrument, measure scaling curves, and
   reduce `projection()` and `__init__`'s full reads without losing the
   full-genesis path.

### Pushed

The full suite at a48ae7b passed: 6,310 passed, 19 skipped, 33 minutes.
c11c628, f142a7e and a48ae7b were pushed together. Hosted CI started on
both the push and the pull_request event.

## Session 3 (2026-10-30)

### Cluster 3: AUD-004, AUD-008, AUD-009 fixed; the work counter

- **AUD-004 (I3's fresh evidence).** UNDER_REVIEW -> VERIFIED must now
  cite a `verification_report` that the record did not already carry
  (`Edge.requires_fresh`, `TransitionRequest.prior_evidence`). Both store
  sites pass the record's prior evidence, and the second reader restates
  the rule.
  - "Fresh" means other bytes. A check is deterministic: the same checker
    on the same bundle gave a byte-identical report under a new governed
    task. So a re-run is the same verification, and is refused.
  - Before and after, from `repro/fresh_evidence.py`:

    | Case | f142a7e | Fixed |
    |---|---|---|
    | re-verified on the report it went stale with | VERIFIED | refused |
    | ... citing that report again | VERIFIED | refused |
    | control: a new report | VERIFIED | VERIFIED |
    | written past the gate: store | loads | StoreError |
    | written past the gate: second reader | VERIFIED, unauthorized=0 | UNDER_REVIEW, unauthorized=1 |

- **AUD-008.** `append()` now hands back the record as its canonical bytes
  parse. A tuple came back from the log as a list, and an int key as a
  string, under the same hash.
- **AUD-009.** `check_against` refused a checkpoint taken in the crash
  window, where the witness is one behind, as "the log was truncated". It
  now accepts that signature when the checkpoint's record is still there,
  and refuses a deeper lag as before (test C5).
- **Fixtures that met the new rules.** Four test fixtures (crash
  recovery, governed-model reuse, scientific invalidation and the
  work-count test) verified a record on a report it was created with, or
  re-cited a stale one. They now follow the honest workflow. The
  invalidation test now shows the origin rule on a record citing the
  withdrawn run.

**Validation.**

- **Pre-fix failure.** Each new test fails on the commit before its fix
  and passes after.
- **Mutation.** All of these were KILLED, with sources restored
  byte-identical:
  - M64 to M71;
  - M33 and FC1, whose anchors moved;
  - E40 to E42.

### Phase 2C: the work counter, and what it measured

`tools/work_counts.py` wraps the event log's read paths from outside,
counts them under the scripted campaign, and records the result in
`docs/audit/work_counts.json`.

| Decisions | Full reads | Records parsed | Baseline `anchor_at` calls (lines parsed) | Now |
|---|---|---|---|---|
| 15 | 78 | 11,195 | 33 (3,552) | 0 |
| 30 | 114 | 23,720 | 48 (8,211) | 0 |
| 60 | 198 | 67,591 | 89 (27,777) | 0 |

- **Growth.** Each doubling multiplied the full-read records by 2.12, then
  2.85: quadratic (AUD-010, open).
- **Where.** At 60 decisions, `GovernedTasks.projection()` accounts for 42%
  of those records.
- **What AUD-003 saved.** It removed every `anchor_at` re-read, 29% of the
  lines parsed at 60 decisions.
- **Wall time.** It did not move outside noise: 20.5 s at the baseline and
  21.6 s now. The subprocesses hide the work.

### The full suite at d124a8f, and what it found

The suite ran in a worktree at d124a8f, with `HARNESS_HISTORY_REQUIRED=1`:
6,294 passed, 19 skipped and 34 errors, in 33 minutes. The 34 errors were every test in
`tests/test_agent_long_horizon.py`, and nothing else failed.

- **Why.** The module's fixture creates each claim already carrying a
  `verification_report`. Its verifier then moved the claim to VERIFIED
  citing that same digest. I3's fresh-evidence rule (AUD-004, in d124a8f)
  refuses that, so the fixture stopped at its first cycle. This is a fifth
  fixture of the kind cluster 3 met. The targeted runs before d124a8f did
  not include this module, which is slow.
- **The fix (55032de).** The verifier cites a report of its own, put in the
  evidence store. The change is to the test only. The module passes at
  the default 260 cycles and at the 1,200 cycles CI runs
  (`HARNESS_HORIZON_CYCLES=1200`, 6 min 47 s).
- **The scan.** An AST scan of every tracked `.py` file looked for a
  record created with a `verification_report`. It found no other record
  that is then verified on that report.

### Phase 3: actor authentication

`agent_substrate/principals.py` was read in full. These hold:

- The key registry is supplied from outside the log and pinned by digest.
- Key ids are derived from the keys.
- A test key is refused by a registry not built for tests.
- A key's lifecycle is judged by log position.
- The attestation is made durable before the record.
- The reference Ed25519 signs only test identities.
- Production authentication refuses until a registry and a vetted
  provider exist. That is EXTERNALLY_BLOCKED, for the owner.

One gap was found and fixed: **AUD-012 (P1)**.

- **The defect.** A REQUIRED history read as authenticated through an
  authenticator scoped by `require` to some actors. `authenticate()`
  passes every other actor's events without asking them.
- **The reproduction** (`repro/scoped_authenticator.py`). A correctly
  chained, unsigned record by an actor with no key was:
  - read with `ok=True`, with the profile pinned or not;
  - built on by a writer, which appended after it;
  - folded by the second reader.

  The same happened at the baseline b0058f6.
- **The fix.** `_security_gate` refuses a scoped authenticator for a
  REQUIRED history. `reconstruct()` restates the refusal in its own code.

### Phase 4: who holds a role

**AUD-011 (P2, OWNER).** Nothing binds an actor to the role it claims.

- The authority store compares the claimed role with the edge.
- The agent directory admits an AGENT with any roles from any registrar,
  registered or not. Only a HUMAN's registrar is checked.
- So one unregistered name took a claim through VERIFIED to PROMOTED, and
  registered its own VERIFIER agent (`repro/self_asserted_roles.py`,
  `repro/open_agent_registration.py`).

AGENT_SUBSTRATE.md states the neighbouring limit: three colluding
registered identities are indistinguishable from three independent ones.
Which authority grants VERIFIER and PROMOTER is an owner decision.

### Cluster 4: AUD-012 fixed, AUD-011 recorded

- **Pre-fix failure.** The 8 new AUD-012 tests fail on d124a8f, and the
  control passes there.
- **Mutation.** EP10, EP11 and RC3 were KILLED, and so were EP1, AH23 and
  RC1, whose surrounding code changed. Sources were restored
  byte-identical, and every anchor in every spec still matches exactly
  once.
- **Ledger.** The dispositions were recorded:
  - `principals.py`: REVIEWED_IN_FULL;
  - `agents.py` and the two test modules: REVIEWED_IN_PART;
  - `events.py` and `reconstruct.py`: re-bound after the AUD-012 diff.

### Read, and found as documented

- **Phase 5.** The inference gateway holds no credential and opens no
  connection. The worker's live mode refuses until the owner chooses a
  provider (OD-1).
- **Phase 7.** NF-1T's flagship holds architecture claims only:
  `large_model_trained: false`, `real_weights_allocated: false`, and its
  sizes are marked ESTIMATE. Only the 2-layer development member is
  trained, on one closed-form law.
- **Phase 8.** `tools/model_check.py` calls the real `check()` with an
  empty prior evidence. AUD-004's freshness rule is outside its bounded
  state space. That is a coverage limit, not a defect.
- **Phase 10.** On this branch every workflow is default-deny with
  per-job grants, and actions are pinned by commit. The agent workflow is
  gated by the commenter's own association. The ungated copy on `main` is
  O-1, which only the owner can change.

### Full-history work and the campaign's limit, set against the record

`docs/tranche_h/FINAL_REPORT.md` ("Why the cost grows") states the growth
behind AUD-010 as deliberate. Every governed fold re-verifies from
genesis, because an anchored read "re-reads no prefix and trusts the
anchor".

- **What an anchored fold gives up.** `advance()` is a verified read of
  the suffix. `require_extends` (AUD-006) refuses a re-chained prefix in
  O(1). What an anchored fold loses is in-process detection of later,
  unchained damage to the verified prefix, which the next full read still
  finds. That is detection latency, not the correctness of decisions.
- **AUD-010 is now OWNER.** The options and the measured costs are in the
  register. In the recorded Gate G campaign (8f53500), a decision took
  0.56 s at the 50 stage and 2.33 s at the 1,000 stage.
- **AUD-013 (P2, CONFIRMED, DOCUMENTED).** The campaign cannot resume
  after its process dies.
  - `World()` deletes an existing workspace, and `main()` deletes it at
    the end.
  - The recorded run asked for 10,000 decisions and holds three stages,
    with `complete=false`.
  - The report states this as unresolved, and the audit keeps it so.
- **The derived harness status.** It is INCOMPLETE (34 unsatisfied). At
  the baseline it was COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT.
  - **Why.** Each audit commit that changed code left the hosted
    evidence of the matrix rows and stack entries covering that code older
    than the code (PREDATES_CURRENT_IMPLEMENTATION).
  - **The count.** 3 items at c11c628, 31 at f142a7e, 34 at a48ae7b.
  - **What can restore it.** The status re-derives from evidence. Hosted
    runs of the new head, attached through the repository's evidence
    tools, can restore it, and nothing else should.

### Pushed, and what the hosted runs said

- **a48ae7b.** All 44 check runs completed successfully, across both
  events:
  - both full suites;
  - all 16 mutation shards, and both mutation-matrix jobs;
  - container-verify and supply chain;
  - the integration jobs (FEniCSx, FMI, end to end, RO-Crate, performance
    telemetry).
- **d124a8f and 55032de.** Pushed together, once the d124a8f suite and
  the fixture fix were validated:
  - the long-horizon module at both scales;
  - 981 contract tests at 55032de.

### Cluster 5: two more authority defects; the cost instrument and the report

Read on the trust surface after cluster 4: the capability ledger and the
policy store. Two P1 defects were reproduced, and they were reproduced at
the baseline as well.

**AUD-014.** A refused capability mint was written before it was refused.

- **Where the rule ran.** `CapabilityLedger.issue()` left "who may mint"
  to `_authorize_mint()`, which runs in the fold after
  `DecidedWriter._append_decided()` has written the record.
- **What it did.** A mint by anyone but the root raised NotTheIssuer.
  But the record was durable, so every later load of the ledger refused
  the whole log. The live ledger meanwhile answered as if nothing had
  happened (`repro/unauthorized_mint.py`).
- **The fix.** `issue()`'s decision runs the same rule, under the writer
  lock, before the record exists.
- **Why the existing test passed.** It asserted the refusal and the
  in-memory state, never the log.

**AUD-015.** Anyone could publish the next version of an owner's policy.

- **The gap in the policy store.** Withdrawal was the owner's act, and
  version 2 was anyone's.
- **The gap in the binding.** Under the promotion binding (D-2026-148),
  the store and the second reader judged a promotion by the publisher of
  the policy's FIRST version, while its expiry came from the latest.
- **What it allowed.** An outsider's version 2 lifted the owner's expiry,
  and a promotion was admitted on it (`repro/policy_version_hijack.py`).
- **The fix.** Later versions are the owner's, live (before writing) and
  on replay. Both readers judge a bound promotion by the publisher of the
  version in force.
- **Kept as they were.** Two existing tests that forge out-of-order
  versions as "mallory" still see the version error: the owner check runs
  after the version checks.

**Validation.**

- Each new test fails before its fix and passes after.
- **G28 (agent_delegation).** Killed, with D1 and D2.
- **PB25-PB29 (policy_binding).** Killed, with PB4, PB16, PB17 and PB21.
- Sources were restored byte-identical, and every anchor in every spec
  still matches exactly once.

**Also here:**

- **`tools/op_costs.py`.** The Phase 14 cost instrument: one operation per
  fresh child process, with wall time, CPU, read bytes, disk and peak
  allocation measured separately. Its record is `docs/audit/op_costs.json`.
- **`docs/audit/AUDIT_REPORT.md`.** The interim Phase 14 report.

**Limit.** The class behind AUD-014 is a fold that refuses a record its
own writer wrote. It was looked for in the capability ledger and the
policy store. A property over every DecidedWriter, that a refused write
leaves the log unchanged, does not exist yet.

**Also recorded:**

- **AUD-016 (P3, OPEN).** The process-tree negative control leaves the
  grandchild it shows escaping running after the test, reparented to
  init. A stop file is proposed. A PID-based kill is not safe across a PID
  namespace.
- **AUD-017 (P3, OWNER).** Nothing says who may issue a secret grant or
  an egress grant, on either path.
  - Egress grants are decided on the governed path: `socket_guard`
    mediates the runner's own sockets.
  - No caller in the repository resolves a secret grant yet.

**Generated, and derived.**

- **The shard matrix.** The six new mutations moved specs between
  shards, so the workflow's shard matrix was regenerated with
  `tools/mutation_shards.py --emit`. `--check` holds.
- **The harness status.** It is INCOMPLETE with 35 unsatisfied. Row R58
  cites that workflow, so its hosted evidence now predates it. The
  evidence-coverage pin moves 20 -> 19
  (`tests/test_completion_matrix.py`).
- **Found late.** The pin was found by the contract tests at ebd14a0,
  and fixed in the commit after it.
- **Found by the full suite at ebd14a0.**
  `tests/test_agent_second_reader.py::test_a_policy_downgrade_is_reported`
  forges a downgrade as an actor other than the owner. The second
  reader's new owner note answered it first, where the test expects the
  downgrade note. The owner note now follows the duplicate and downgrade
  notes, as the policy store's owner check follows its version checks.
  PB29 and PB22 were re-killed.
- **Also found by that suite.** Two tests in
  `tests/test_readiness_matrix.py` failed:
  - **Why.** Readiness row CI-4 (HOSTED_TESTED) covers the workflows, and
    the regenerated shard matrix changed `agent-substrate.yml`.
  - **What changed.** CI-4 returns to IMPLEMENTED, as SUB-1 to SUB-4 did
    in a48ae7b, until hosted runs of the new workflow are attached.
  - **Why no earlier check caught it.** The contract tests at ebd14a0 do
    not name the readiness matrix's file, so they did not include this
    module.


### Next actions at the end of session 3

Superseded by the session 4 report below.

1. Push d3cddc3 once its full suite passes, then the cluster 5 commit
   once its own does. Attach hosted evidence through the repository's
   tools when the runs finish.
2. Phase 14 is in `docs/audit/AUDIT_REPORT.md`, interim. Keep it in step
   with the ledger and the register.
3. Look for AUD-014's class in every DecidedWriter: a property that a
   refused write leaves the log unchanged.
4. Phase 9: a resumable campaign (AUD-013). The stop file (AUD-016) was
   made in session 4.
5. The ledger: most changed files are still NOT_REVIEWED. Continue with
   the trust surface: `governed_tasks.py`, `governed_model.py`,
   `netauth.py`, the executor and `secrets.py`'s resolve path.


## Session 4 (2026-10-30): the frontier directive

The baseline at the start of the session: HEAD and origin were both
29aedfb39a8b. The merge base with origin/main was 096fb90fff08, which is
origin/main itself. The worktree was clean, with 1001 changed files.

### AUD-016, fixed

Two tests were written first, and each failed on 29aedfb.

- `test_the_grandchild_the_control_lets_escape_is_stopped_once_read`
  runs the probe with isolation off. Three seconds after the probe
  returned, the grandchild was still running.
- `test_a_grandchild_nothing_tells_to_stop_ends_on_its_own` runs the
  tool as the probe does, but never writes the stop file. The
  grandchild was still running after ten seconds.

Each test finds a leftover through `/proc`, by its working directory. It
kills what it finds, so the failing runs left no process behind.

The fix has three parts:

- The grandchild beats while `out/stop` is absent, and stops at its own
  bound, `ESCAPEE_SECONDS` (30 s), whichever comes first.
- The probe takes both readings first. Then it writes the stop file, in a
  `finally`.
- No PID is used anywhere.

The verdict is unchanged: the control `[process_tree]` still sees the
grandchild escape.

Mutations were run on this host: IS11 (no stop file), IS12 (no bound) and
IS13 (a bound shorter than the reading). Each was killed, and IS7 is still
killed.

### AUD-011 and AUD-017: a role registry outside the log

`agent_substrate/roles.py` is the root. It is a registry supplied from
outside the log and pinned by its digest, as the key registry is. Each grant
names a principal, a role, a scope and the positions it holds for.

Delegation happens in the log, once, and only from grants marked
`may_delegate`. A delegation is narrower and no longer than its parent. It
is never made to the delegator, and its delegator revokes it. A view folded
past a position answers as one folded to it, so the governed runner can ask
a view caught up to the head about earlier positions.

Where a reader is given a registry, it holds these acts to it, before the
write and again on replay:

- in `AuthorityStore`: create, and each transition (SYSTEM needs VERIFIER or
  PROMOTER);
- the policy version a promotion would stand on;
- `PolicyStore.publish`;
- `SecretStore.issue` and `NetworkAuthority.issue`, the latter over every
  host;
- in `GovernedTasks`: submission, execution and each party's move, checked
  where its record is written.

The second reader restates all of it in `reconstruct`, `reconstruct_tasks`
and `reconstruct_subsystems`. A checkpoint folded under other grants is not
restored, and the history is replayed under the reader's own.

`tests/test_role_authority.py` has 50 tests, each an attack beside its
control:

- mallory's forged walk to PROMOTED, refused by both readers with the same
  kind;
- an agent registered by anyone as VERIFIER;
- an authenticated principal without a grant, refused as anyone else is;
- the delegation rules;
- revocation and expiry by position, the reach-back named;
- recovery across registries;
- each party's grant ending mid-run, refused where its record would have
  been written, which leaves a history every reader loads.

`tools/mutations/role_authority.json` holds 44 mutations. The first run
killed 41. RA4 and RR3 (a forged create, both readers) and RR8 (the second
reader's own policy fold) survived because no test replayed those cases.
Two tests were added and all three are now killed.

The structural registries now include the new module: the layering, the
importer allowlist, the IO layer, READER_COVERAGE, the identity inventory
(55 of 55 actions reconstructed) and the authority inventory. Task creation
is now a decided write.

Owner action: provision the production registry. `production_role_registry()`
refuses until a registry is provisioned.

Not done:

- No history declaration obliges a reader to hold a registry.
- HUMAN_APPROVER and RELEASE_AUTHORITY have no enforcement point.
- The capability ledger's root is still trust on first use.

### CI at 29aedfb

Mutation shard 5 was cancelled at 59 minutes 49 seconds: "The runner has
received a shutdown signal". The job limit is 150 minutes. Every matrix it
finished killed all its mutations, and the other seven shards passed,
running up to 68 minutes. The aggregate `mutation-matrices` check failed
only because shard 5 did not finish. The run was lost to the runner, not
failed by a test. Pushing 5900f3b superseded it. 5900f3b's local full suite
passed: 6,349 passed, 19 skipped.


### AUD-013: the campaign resumes after its process dies

`tools/research_horizon.py` keeps its own state in `campaign_state.json`
beside the world. The file is written atomically before each objective,
after each stage and after each withdrawal, and is bound to the log
position it describes. `--resume` opens the same world, which
`World(resume=True)` no longer deletes or rebuilds. The log is the
authority:

- a log rolled back behind the recorded position is refused;
- an objective the log registered after the last save is finished, or
  counted once if the dead process had finished it;
- a stage measured and not saved is measured again;
- a withdrawal made and not recorded is settled, not repeated.

One process holds a campaign at a time, through an flock.

`tests/test_research_horizon_resume.py` has 18 tests. Each kills the
campaign's process with `os._exit` in a subprocess, at one of twelve
points, and resumes it in another:

- before a step executes;
- during a step (three points);
- after the governed run's external effect;
- after the check, before the decision;
- after the decision, before the step that acknowledges it;
- between an objective and its save;
- after a stage's withdrawal;
- inside a checkpoint, between its anchor and its file;
- after a stage is measured;
- three objectives into a stage.

Against an uninterrupted run of the same campaign:

- every counter taken from the log is equal;
- the logs differ action by action only in two named kinds: a re-measured
  checkpoint, and one context assembled for a model call the dead process
  never made (found by the first run of the test, which compared raw event
  counts);
- nothing was executed, received or answered twice;
- the second reader agrees and the checkpointed load is equal.

The other tests: a second process is refused while a live one holds the
lock and admitted once it is gone; a history restored from an earlier copy
is refused; a lagging head witness still resumes; a torn temporary state
file is never read; a missing state is refused.

Mutations RHR1 to RHR11 (`tools/mutations/research_horizon_resume.json`)
are each killed.

Not established:

- recovery across the loss of the machine, which needs durable storage;
- a death between the two halves of a stage boundary's withdrawal;
- the 5,000 and 10,000 stages.

### Lean 4: obtainable, not run

The pinned toolchain is obtainable. `lean-4.14.0-linux.zip` was retrieved
from the official release URL, with sha256
`54f3ae8b9ef7a472d27563633c084e1d38e00ee9ef1d226cb3b8b823b6c4cd74` on
2026-10-31.

Extracting and executing it was refused by this session's permission
system, and the audit did not work around that. No Lean source is
committed: a proof nobody kernel-checked would be a claim without a check.
The model and theorems planned (the authority edge table generated from
`authority.EDGES`, I1 to I3 and terminal retraction, with negative
controls) wait on the owner's permission to run a third-party toolchain
here, or on a CI job that installs it by digest.

### Phase A report (session 4)

Commits, in order:

- 5900f3b: AUD-016;
- 824a514: AUD-011 and AUD-017;
- c7c0ce9: four mutation anchors that 824a514 moved, re-anchored;
- 22dacc3: AUD-013;
- this record.

**Implemented.**

- `tools/isolation_contract.py`: the escaped grandchild stops after the
  reading, and at its own bound.
- `agent_substrate/roles.py`: the role registry, its delegations and its
  refusal kinds. It is enforced live and on replay by `AuthorityStore`,
  `PolicyStore`, `SecretStore`, `NetworkAuthority` and `GovernedTasks`, and
  restated in `reconstruct`, `reconstruct_tasks` and
  `reconstruct_subsystems`. Task creation is a decided write.
- `tools/research_horizon.py` and `tools/adversarial_eval.py`: the campaign's
  state beside its world, its lock and its resume.
- Tests: `tests/test_isolation_contract.py` (two added),
  `tests/test_role_authority.py` (50) and
  `tests/test_research_horizon_resume.py` (18).
- Mutation specs: IS11-IS13, `role_authority.json` (44) and
  `research_horizon_resume.json` (11).

**Independently verified.**

- No formal proof was run. The Lean toolchain was not allowed to execute
  here; see above.
- Mutation testing on this host:
  - IS11-IS13 and IS7: 4 of 4 killed;
  - RA1-RA36 and RR1-RR8: 44 of 44, after two tests were added for the
    three that survived;
  - RHR1-RHR11: 11 of 11;
  - AA14, D17, HT2 and HT9, re-anchored: 4 of 4.
- Full suites on this host:
  - 5900f3b: 6,349 passed, 19 skipped;
  - 824a514: 6,401 passed, 6 failed, all of them the four unmatched
    anchors that c7c0ce9 fixes;
  - 22dacc3: run before this record was pushed.
- Hosted CI:
  - 5900f3b: every job of run 41163919134 green, all eight mutation shards
    and the aggregate included;
  - 29aedfb: shard 5 lost to a runner shutdown at 59:49, with every matrix
    it finished killed;
  - 824a514 onward: not yet run.
- Deployment evidence: none exists, and none is claimed.

**Failed, and found.**

- 824a514 broke four mutation anchors. Its full suite caught them, and
  c7c0ce9 re-anchors them. The contract test batch did not include
  `tests/test_mutation_harness.py`; it does now.
- The role registry's first schema name carried the retired programme's
  prefix. The content audit flagged it before the commit.
- The first resume test compared raw event counts. It found a context
  assembled for a call the dead process never made, which is now a named
  allowance.

**Measured.**

- A 20-decision campaign takes 8 seconds on this host.
- A resumed campaign reproduces the uninterrupted one's log-derived
  counters exactly, at all twelve kill points.
- The suite grew from 6,349 to 6,407 tests at 824a514, and 22dacc3 adds 18
  more.

**Not established.**

- Role enforcement where no registry is given, and production provisioning.
- HUMAN_APPROVER and RELEASE_AUTHORITY enforcement points.
- Lean proofs.
- Campaign recovery across the loss of the machine.
- The 5,000 and 10,000 stages.
- Phases B to F of the directive: the UQ demonstration, the assurance
  package, Merkle transparency, agent evaluation, distributed protocols and
  the reproduction guide.

**Owner decisions.**

- Provision the role registry: who holds which role, and its pinned digest.
- Permit, or move to CI, a pinned Lean toolchain.
- AUD-010, the anchored suffix read, and AUD-001, archiving the bundled
  histories, are unchanged.
- The existing OD-1 to OD-7 are unchanged.

### Next actions at the end of session 4

1. Once hosted runs of 22dacc3 finish, re-attach hosted evidence
   (`tools/readiness.py attach`) for the rows the role registry returned to
   IMPLEMENTED.
2. A genesis declaration, as for `AUTHENTICATED_REQUIRED`, that obliges
   every reader of a history to hold its role registry, by digest.
   HUMAN_APPROVER at an escalation's answer.
3. The Lean module, if permitted: generate
   `formal/lean/Authority/Edges.lean` from `authority.EDGES`, and prove I1,
   I2, I3, the role refusal and delegation lapse over it. Add negative
   controls, `#print axioms`, and a CI step that installs the release by
   the digest recorded above.
4. Phase B on this host: an uncertainty budget for the slab model, and a
   portable assurance package with a fail-closed verifier.
5. The ledger: 935 files are still NOT_REVIEWED.

### Phase B, first item: an error budget for one model (Part VI, item 20)

`scientific/uncertainty.py` is a small layer.

- **Bases.** Each contribution to an output's error states its basis:
  MEASURED against an independent reference, ESTIMATED from the method's own
  theory, ASSUMED from declared input distributions, or NOT_ESTABLISHED.
  The last is listed and never summed.
- **Combination.** A measurement replaces the estimate it checks in the
  total, and the estimate is kept and held to it. Contributions are added
  linearly: a systematic numerical error is not a standard deviation.
- **Propagation.** Parameter uncertainty is propagated twice, in ways that
  share no step: linearly through central differences, and by Monte Carlo
  with a fixed seed. The two are compared at the Monte Carlo standard
  error.

`tools/slab_uq.py` applies it to the slab's T_at_half at the campaign's
documented point, and records `docs/assurance/slab_uq.json`:

- **Discretization.** It was measured against the closed-form series at
  1.1158e-4 K. The model's Richardson estimate, 1.1158e-4 K, falls short of
  that by a factor of 1.0000343, and the record says
  ESTIMATE_DOES_NOT_COVER. Richardson's is an estimate, not a bound, and
  the first draft of this budget had called it a bound.
- **Parameters.** The declared spreads are illustrative and not measured.
  The linear standard deviation is 2.293 K and the Monte Carlo one 2.234 K
  over 1,000 samples, agreeing within three Monte Carlo standard errors. The
  variance is carried by q (46%), rho c (39%) and T0 (14%).
- **Statement.** T_at_half = 331.018 +/- 4.24 K. The parametric part is at
  95% under the declared spreads. Model form and measurement are NOT
  ESTABLISHED, because no real slab was observed.

`tests/test_slab_uq.py` (15 tests) holds the record to the tool and the
rules to the layer. CI re-derives the record with `tools/slab_uq.py
--check`, which refuses a record edited by hand. Two negative controls stand beside them: a reference 2%
off in conductivity leaves the estimate uncovered by a factor above 10, and
exp(3x) over a wide spread makes the propagations DIFFER. Mutations SU1 to
SU8 (`tools/mutations/slab_uq.json`) are each killed.

Not established:

- any physical validity;
- input distributions that are measured rather than declared;
- interval-arithmetic or certified bounds;
- a Bayesian treatment;
- model discrepancy.


### Hosted CI at 2497d87

Every workflow run at 2497d87 succeeded, on both push and pull_request:
agent-substrate (the full suite, the eight mutation shards, the second
interpreter and the dispatch-sensitivity job), stack-verify,
harness-integrations, supply-chain and container-verify. It is the first
hosted run to cover 824a514 (the role registry), c7c0ce9, 22dacc3 (the
campaign's resumption), bbf58f2 and 2497d87 (the error budget).

The readiness rows CI-4 and SUB-1 to SUB-4 stay IMPLEMENTED. `tools/
readiness.py attach` re-fetches every run itself with `gh api` and digests
the rows' files from the commit's tree, so that the party attaching cannot
assert the evidence. The `gh` token in this session is invalid, so the tool
cannot fetch, and the runs were not attached through another channel.
CI-4's implementation also includes the workflow that the next commit
changes, so CI-4 could be attached only from a run of that commit.

### Phase B, second item: a portable assurance package (Part X, item 34; Part XI, items 35 and 36)

**What is packaged.** One governed answer to the owner's question, "does
T_at_half stay below 1000 K at the documented point?". It is the run that
the adversarial evaluation already calls legitimate: the scripted agent's
model run on the governed path, its independent method-of-lines check, the
reviewer's VERIFIED decision and the research ledger's SUPPORTED verdict.
Beside it are the slab's error budget and the second reader's proofs of
both decisions (decision-proof/1).
`docs/assurance/package/slab-mid-plane/` (152 KB) holds:

- the record (`scientific-assurance-record/1`), which states the question,
  the hypothesis, the equations, the assumptions, the model, the inputs
  with their units, the results, the budget, the checks, the decisions,
  the history's head, the trust roots and the gaps;
- a guide (`README.md`) that follows the directive's seven reproduction
  steps;
- the run's 85-record event log;
- seven evidence files, each named by its digest;
- `SHA256SUMS`.

`tools/assurance_package.py --write` builds it from a fresh run. It keeps
the package only if the verifier accepts it in a process of its own.

**The verifier.** `tools/verify_assurance.py` imports the standard library
and nothing else. It refuses to run from inside a package. It holds a
package at five levels, each needing the one before:

- INTEGRITY: every file is exactly the one SHA256SUMS lists, nothing is
  missing, added or linked, and evidence is named by its digest;
- CONSISTENCY: the record and the evidence say the same things, and the
  record claims nothing the package cannot carry and names every gap;
- HISTORY: the log is one canonical hash chain, ending at the stated head;
- DECISION: the question, the hypothesis, the proposal, the separate
  verification, the observation and the verdict are in that history as
  reported, cite exactly this package's evidence, and still stand at the
  head. The producer's proofs are about these same records.
- RECHECK: the verifier solves the slab equations itself, in closed form.
  It integrates the eigenfunction coefficients exactly and finds the
  eigenvalues by bracketed Newton iteration. The producer's series check
  uses quadrature and bisection instead.

At RECHECK, on the committed package:

- **Exact solution.** T(L/2) = 331.0184097 K.
- **The model against it.** It differs by 1.12e-4 K, within three times its
  own estimate. Its whole 80-cell final profile is within 2.6e-5 K.
- **The parametric spread.** It is re-derived from the same declared draws,
  propagated through the exact solution instead of the model. The
  half-width agrees with the recorded one to 1.3e-5 K, against a tolerance
  of 3.35e-4 K (three times the measured discretization).
- **The verdict.** The hypothesis is re-evaluated: SUPPORTED, with a margin
  of 669 K.

The claim's verdict therefore does not rest on the producer's model, but on
the verifier's own solution of the stated equations. That is a statement
about the equations, not about any real slab.

**What it cannot establish.** It lists these on every run, and refuses a
record that leaves one out. There are eleven:

- PACKAGE_ORIGIN and HISTORY_ANCHORED, which an operator can supply with
  `--expect-package-digest` and `--expect-head`;
- PRODUCER_EXECUTION;
- MODEL_IMPLEMENTATION;
- ROLE_ENTITLEMENT;
- FORMAL_PROOF;
- PHYSICAL_VALIDITY;
- INPUT_DISTRIBUTIONS;
- PRODUCER_TOOLCHAIN;
- WALL_TIMES;
- HISTORY_SEMANTICS.

**The controls.** `--check` runs these in CI.

- **Currency.** The package is held to the code as it is: the model's and
  the check's implementation digests, the committed budget, and a fresh
  run of the model at the recorded inputs.
- **Changed bytes.** One byte changed in any of the 11 files is refused at
  INTEGRITY, and the refusal names the file.
- **Consistent forgeries.** Each rewrites every digest, every hash and
  SHA256SUMS.
  - The temperature moved by 0.01 K is refused at RECHECK.
  - The proposer verifying its own result is refused at DECISION.
  - A forgery that changes nothing passes every level. It is refused only
    against the honest package digest, obtained independently.
- **What that shows.** Consistency is not origin, and the guide says so.

CI runs the verifier a second way too: with the runner's own interpreter,
`python3 -I`, outside the project's environment.

**Tests.**

- `tests/test_assurance_package.py` has 130 tests. Each corruption is
  refused at the level and the check that should catch it and at no
  earlier one, and every later level is NOT_REACHED.
- `tests/test_assurance_package_build.py` builds a fresh package and holds
  it to the committed one.
- The mutations are VA1 to VA102 (`tools/mutations/verify_assurance.json`),
  one per rule of the verifier. All 102 are killed, each by a test aimed at
  that rule.

**Defects found in this work, before commit.**

- The verifier first read the JSON number `1e400` as infinity, because
  `parse_constant` never sees it. Such numbers are now refused.
- Two of the verifier's branches could never run: an empty manifest is
  already refused for its missing newline, and an empty log for the same
  reason. They were removed rather than kept as checks no test can reach.
- The first test of an oversized propagation would have had a mutant
  compute a million samples before failing. It now asks for 20,001.

**Not established.**

- Reproduction by anyone but the producer.
- The package's origin. It is not signed: signing needs an identity that
  only the owner can provision.
- Whether the deciders were entitled to decide: the run had no role
  registry.
- The model re-run offline: that needs the source, and `--check` re-runs it
  only with the repository.
- What the history's other records mean.
- Any physical validity.

### Phase C, first item: Merkle transparency commitments, evaluated

**The limit.** A witness (`agent_substrate/witness.py`) learns that a new
head extends the last one it cosigned by being shown every record in
between. For a hash chain those records are the consistency proof, so its
length grows with the history added since. A reader who wants to know that
one record belongs to a witnessed history must hold the history after it.

**What was added.** `agent_substrate/transparency.py` is the RFC 9162
Merkle tree over the log's event hashes, standard library only:

- RFC 9162's Merkle Tree Hash, with the 0x00 and 0x01 domain separation;
- inclusion proofs, and their verification;
- consistency proofs, and their verification.

The chain is unchanged. The tree is computed from a verified chain and adds
a commitment, not an authority.

**Held to an external reference.** The roots of RFC 6962's eight test
leaves match the Certificate Transparency project's published values. Then
every proof over every size up to 64 is checked both ways: each proof the
module builds verifies, and each one offered for the wrong leaf, position or
root, or for a fork, does not.

**Measured on the assurance package's own 85-record log (69,748 bytes).**

| Proof | Merkle | Hash chain |
|---|---|---|
| The verification decision at seq 74 is in the tree | 6 hashes, 192 B | the 10 records after it, 8,992 B, and only once seq 74 is witnessed |
| Extend a witness from seq 12 to the head | 8 hashes, 256 B | 72 records, 61,491 B |
| Extend a witness from seq 76 to the head | 7 hashes, 224 B | 8 records, 6,987 B |

At 100,000 random entries, a prototype on this host measured:

- the root in 208 ms;
- an inclusion proof of 17 hashes (544 B), verified in 34 microseconds;
- a consistency proof of 14 hashes.

**A limit found and kept.** A root does not commit to its own size. RFC
9162's verification takes the size as an input, so a proof for one size can
verify against the same root at another size whose path has the same shape.
Over the sizes up to 64 that happens for 1,824 proofs of each kind. A fork
never verifies, at any size. So a checkpoint must state the size beside the
root, and a cosignature must cover both. The module's tests demonstrate the
limit rather than assume it away.

**Mutations.** TR1 to TR14 (`tools/mutations/transparency.json`) are each
killed, through the harness with every source restored byte-identical. One more was dropped rather than kept: removing the consistency
check's bound on its sizes sends a loop spinning, and only a timeout would
end it, which the harness rightly does not count. The loop is guarded now,
and the bound stays.

**Not done.**

- The witness still cosigns head hashes and takes records as its proof. A
  witness/2 that cosigns (size, root) and accepts a consistency proof is
  the next step: a protocol change, with its own mutation specification.
- The assurance package still carries the whole log.
- No production witness exists.

**A defect in the record, found and corrected.**

- **What went wrong.** At 5a2bbf8 the report said 23 files were reviewed in
  part and 935 not reviewed, but the ledger committed with it said 22 and
  936.
- **Why.** That commit's one-line change to
  `tests/test_agent_substrate_isolation.py` (the builder's importer entry)
  correctly sent the file's disposition back to NOT_REVIEWED, and the
  report's table was not re-derived. No test compares the two.
- **The fix.** The file's review is re-recorded, with the audit's own
  additions to it reviewed as diffs, so the ledger and the report agree:
  1,030 files, 23 reviewed in part, 935 not reviewed.

### Phase C, second item: the witness protocol over the tree

**What was added.** `agent_substrate/witness.py` gains tree checkpoints
(`witness-cosignature/2`) beside the head protocol, which is unchanged:

- **The checkpoint.** A `TreeCheckpoint` is a log's origin, how many records
  its tree commits to, and the tree's root. The size is stated because a
  root does not fix it.
- **The witness.** A `TreeWitness` keeps the last tree it cosigned, in a
  file of its own per origin. It accepts a larger tree only with a
  consistency proof from that one, so the proof is O(log n) hashes
  whatever was appended.
- **The verifier.** `verify_tree_witnessed` holds a log to the trees a
  quorum cosigned.

The tree witness refuses everything the head witness refuses:

- a rollback;
- another root at the cosigned size;
- a history rewritten beneath the cosigned tree, even with that rewritten
  log's own valid proof;
- a proof that is not one;
- a witness that lost its state, which trusts nothing first;
- provisioning anywhere but at the first record's leaf, or a second time.

The verifier reports TRUNCATED and FORKED by root, counts junk for nothing
(another witness, another log, a bad signature), states the quorum's reach,
and is STALE past a bound. The two schemes are different strings, so a
cosignature of one is never read as one of the other.

**Written so that neither protocol's code repeats the other's.** The first
draft repeated the head verifier's lines, and eight of the head protocol's
existing mutation anchors then matched twice. The tree code was rewritten
with its own screening helper and its own wording, and every existing
anchor matches once.

**Tests and mutations.**

- `tests/test_witness_tree.py` has 20 tests.
- TW1 to TW19 (`tools/mutations/witness_tree.json`) are each killed.
- The head protocol's 23 mutations (`tools/mutations/witness.json`) are
  each still killed with the tree code present.

**Not established.**

- No independent witness exists. Every witness is a fixture with a TEST key.
- The assurance package still carries the whole log, not an inclusion
  proof against a cosigned tree.
- The REFERENCE_ONLY Ed25519 provider signs; no vetted provider is
  configured.

**The record, kept true.** The report still said 54 changed `agent_substrate`
modules after the transparency module made it 55. It now says 55, of which
19 have been read and two (`roles`, `transparency`) were written by the
audit. Changing the isolation test again sent its disposition back to
NOT_REVIEWED, and it was re-recorded before this commit rather than after.
`witness.py` is recorded as read in part, to write the tree checkpoints
beside it. The head protocol was not reviewed adversarially line by line.

### Hosted CI at 5a2bbf8 and 0683097

Every workflow run at both heads succeeded, on push and pull_request:
agent-substrate, stack-verify, harness-integrations, supply-chain and
container-verify. Within agent-substrate, that includes:

- the full suite;
- all eight mutation shards (130 specifications at 0683097) and their
  aggregate;
- the second interpreter;
- the dispatch-sensitivity job.

It also includes the two assurance steps. The standalone verifier ran under
the runner's own `python3 -I`, outside the project's environment, and
accepted the committed package. `tools/assurance_package.py --check` found
the package current on the runner's machine: the model's outputs at the
recorded inputs agree with the package's to 1e-9 relative. Every corrupted
copy was refused where it should be.

### Phase F, first item: an external reproduction guide

`REPRODUCE.md` is written for someone who trusts none of the people, agents
or CI runs behind this repository. It names what such a reader must still
trust: the commit, the interpreter and the lock, and the tools pinned by
digest. It then gives the steps:

- the portable assurance package, checked with nothing but the standard
  library and the package digest from the reader's own clone;
- every re-derivation CI runs: the scientific results, the governed agent
  path (scripted, with no language model), the formal model checks, the
  repository's derived records, the tests and the mutation matrices;
- what no step can reproduce: a language model's behaviour, a physical
  experiment, the flagship model, Lean proofs, and a production
  deployment.

The guide is held by a test (`tests/test_reproduce_guide.py`):

- every script it names exists;
- every re-derivation it lists is a command the agent-substrate workflow
  runs, character for character;
- its standalone check is the one CI runs;
- section 3 still names every gap.

Two negative controls fail it: a guide listing `slab_uq.py --write`, which
CI does not run, and one naming a script that does not exist. The commands
that run quickly were also run here as written, and exited 0: the formal
model check, the readiness and model-evidence registries, the harness
status and the manifest.

**Not established.** That a reader outside this project can follow the
guide to the end: nobody outside it has tried.

### Phase C, third item: archived history segments

The log is one file, verified from its first record. A reader who holds
part of an old history, and a checkpoint of a later tree, could not check
that part alone. A hash chain's records link backwards, so a piece of it
is only as good as every record after it, up to a head somebody vouched
for. `agent_substrate/archive.py` seals the log in pieces that can each be
checked alone.

**The frontier.** `agent_substrate/transparency.py` gains the frontier of
a prefix: the roots of the perfect subtrees covering its first `start`
leaves, largest first, one for each bit set in `start`.

- `extend` takes a frontier and the entries after it to the frontier of
  the whole.
- `frontier_root` folds a frontier from the right into RFC 9162's root.
- `verify_segment` checks that entries are leaves start..end-1 of a tree
  of `size` leaves. It needs the frontier of `start` and a consistency
  proof from `end` to `size`.

Held to the published roots of RFC 6962's test leaves:

- every frontier of up to 64 leaves folds to `root`;
- every extension equals the frontier of the whole;
- every contiguous segment of every tree up to 32 leaves verifies;
- none verifies with a wrong root, reordered, one entry short, one
  altered, moved one place either way with that place's own frontier, or
  with a frontier hash bent.

**A segment** is the canonical lines of records start..end-1, sealed
with:

- its digest;
- the frontier before it;
- the root after it;
- its first link and its last hash.

**A manifest** lists an archive's segments. It is never trusted: every
hash in it is recomputed from the segments' bytes, and only the last root
needs a proof from outside.

**What each check establishes.**

- `verify_segment` checks one segment against a checkpoint's size and
  root:
  - each line is read as the log's own reader reads it, with
    `_parse_line` and `_check_link` themselves rather than a copy;
  - the records chain from the segment's first link;
  - they are the checkpoint's leaves at their positions.

  It costs O(log n) hashes beyond the segment's own bytes.
- `verify_archive` checks every segment from genesis. It also checks
  their positions, frontiers and links across each boundary, and the last
  root against the checkpoint. It reports how far the archive reaches, and
  an archive of a prefix verifies as a prefix, with `complete` false. It
  reports the profile the history declares, by the log's own rule.
- `restore` writes the verified records back as a log that must not
  already exist.

**Neither check authenticates anyone,** and neither hands back a record of
a history that requires authentication. An archive carries no
attestations, and the log's own reader hands no event of such a history to
a reader without an authenticator. A first draft returned the records with
the profile beside them. The full suite caught that `seal`, a new reader of
the log, was missing from `READER_COVERAGE` in `principals.py`, and the
archive was made to follow the log's rule instead of being listed around
it:

- `seal` is GATED. It refuses a REQUIRED history without an authenticator,
  and reads it with one, as the coverage test's behaviour table now shows.
- `verify_archive` and `verify_segment` check such a history in full and
  return no records. A segment does not hold the first record, so it is
  told the profile, as a deployment pins a log's. A first segment pinned
  the wrong way is refused by its own first record.
- Restored, the history gives no events to a reader without its
  authenticator, and every event to one with it.

**Attacks refused.** `tests/test_archive.py` has 31 tests. Each forgery a
keeper of the archive could make is refused:

- a byte changed;
- records reordered;
- a record dropped, with or without the end moved;
- a blank line;
- a line not canonical;
- a torn final newline;
- a record edited, re-hashed and its chain repaired;
- the sealed root or last hash changed;
- all of the above re-sealed, root included, which the checkpoint still
  refuses;
- a segment moved, or given a bent frontier or one of the wrong length;
- a checkpoint too old to cover the segment;
- a fork's checkpoint, with either log's proof;
- manifests with a gap, an overlap, a reordering or no genesis;
- a splice of two logs, caught by the frontier and, once that is made to
  fit, by the first link;
- missing bytes, a wrong schema, malformed records, checkpoints and
  proofs.

A segment from before a fork verifies against the fork's checkpoint, as it
should, since it is in both histories. Sealing refuses a log that does not
verify, and leaves the log byte-identical.

**What the full suite found that the focused runs did not:**

- `seal` was a reader of the log missing from `READER_COVERAGE`, which led
  to the authentication rule above;
- a `type: ignore` without the reason this repository's typing rule
  requires.

The suite's last run was on this tree before that reason, a one-line
comment, was added: 6,705 passed, 19 skipped, and the typing test failed.
After the comment, the typing test, the archive, authentication, manifest
and ledger tests, and AR1 to AR26 were run again, and all pass.

**Mutations.** Every mutation is killed:

- AR1 to AR26 (`tools/mutations/archive.json`), AR23 to AR26 holding
  the authentication rule;
- TR16 to TR27, added to `tools/mutations/transparency.json`;
- TR1 to TR14, still killed with the frontier code present.

**Measured** with `python tools/archive_costs.py --sizes
1000,10000,100000 --every 1000`, on this host. Bytes and hash counts are
fixed; seconds vary between runs.

| Records | Log | Full verified read | Seal | Whole archive | Manifest | One segment of 1,000 | Its frontier + proof |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 317 KB | 0.023 s | 0.028 s | 0.023 s | 402 B | 0.022 to 0.024 s | 0 hashes |
| 10,000 | 3.2 MB | 0.21 s | 0.28 s | 0.24 s | 7.4 KB | 0.023 to 0.025 s | at most 16 hashes |
| 100,000 | 32 MB | 2.33 s | 3.62 s | 2.80 s | 82 KB | 0.022 to 0.027 s | at most 21 hashes |

Checking one segment at 100,000 records costs what it costs at 10,000.
The full verified read grows with the log. The frontier and proof never
exceed 21 hashes (672 B) here. The manifest is about 820 B per segment.

**Not established.**

- **No independent witness.** No witness outside this project cosigned
  any checkpoint. The checkpoint's trust is the reader's decision, and an
  independent witness is an owner action.
- **No authentication from the archive alone.** The archive does not carry
  the attestations a REQUIRED history is authenticated by.
- **No compaction.** Nothing truncates or compacts a log, and the live log
  is unchanged. Hot and cold storage stay unseparated: the archive is a
  checkable copy, not a replacement.
- **Memory.** `verify_archive` holds the whole archive's records in
  memory. `verify_segment` holds one segment.

### Phase E, first item: witness quorums against a split view, model-checked

Witnesses exist so that two readers who accept witnessed histories accept
consistent ones. Nothing had checked when that holds across witnesses: a
log operator can show each witness whatever history it likes. It wins if
two forks of one log are each witnessed by a quorum, because two readers
then accept two different pasts.

**The model.** `docs/formal/WitnessQuorum.tla` is protocol 7. It has four
witnesses, a quorum of three, at most one faulty witness, and histories of
up to three records, each "a" or "b" after the first.

- A witness cosigns a tree only when the tree it holds is a prefix of it.
- It can lose its state, and the owner can provision it again at the
  first record.
- A faulty witness cosigns anything.
- `witnessed` is every tree a quorum has ever vouched for.

The claim, `NoSplitView`, is that every two witnessed trees are
consistent. TLC explores 745,361 distinct states and finds no violation,
and it refutes three broken variants:

- `small_quorum`, a quorum of two, refuted in 4 steps;
- `no_consistency`, where a witness accepts any tree no shorter than its
  own, refuted in 6 steps;
- `loss_uncounted`, where a witness provisioned again is counted honest,
  refuted in 8 steps, after 1,343,333 states.

**What it found in the code.**

- **AUD-018 (P2, fixed).** `TrustRoot` accepted any quorum from one to n.
  With a quorum of half the witnesses or fewer, two disjoint groups of
  honest witnesses can each witness one fork. This was reproduced with
  four real witnesses and a quorum of two: each reader verified its own
  fork as witnessed, with no problem. `TrustRoot` now refuses a quorum
  that is not a majority. `TrustRoot.tolerates` states how many faulty
  witnesses a quorum survives, 2q - n - 1, which is the condition the
  model is checked under. No trust root in the repository used a minority
  quorum.
- **AUD-019 (P3, owner).** A witness provisioned again after losing its
  state has forgotten what it cosigned, and cosigns a fork of it. TLC's
  counterexample, replayed with real witnesses, gives each fork a quorum
  of three. It took two faulty witnesses, and a quorum of three of four
  tolerates one. The code cannot see that a witness forgot, so
  `witness.py` states the rule and the owner counts it.

**Tied to the code** by `tests/test_witness_quorum.py`, which has 15
tests:

- for each of the 49 pairs of the model's trees, a real `TreeWitness`
  cosigns exactly when the model's witness does, and holds afterwards
  what the model's holds;
- both counterexamples are replayed with real witnesses;
- `tolerates` is checked for eight configurations;
- every quorum of half or fewer is refused.

On the unfixed module, 14 of the 15 fail. The one that passes holds
behaviour that was already right. `tests/test_formal_conformance.py`
names those tests for `NoSplitView`, as it does for protocols 2 and 4.

**Checks.**

- WQ1 to WQ3 (`tools/mutations/witness_quorum.json`) are each killed.
- The head and tree witness specifications are still fully killed, 23
  and 19, and so is the formal record's own specification, 10.
- `formal_check.py run` reproduces the 29 earlier results unchanged and
  adds four, 33 in all. It took 3 minutes on this host.

**Not established.**

- Anything outside the finite model: more witnesses, longer histories,
  or a fault budget other than one.
- Signatures and hashes, which are assumed: a cosignature is its
  witness's alone, and equal roots are equal records.
- How many faults a deployment should tolerate. That is the owner's to
  choose when independent witnesses are deployed, and none is deployed.

### Phase D, first item: counterfactual cases in the offline evaluation

The offline adversarial evaluation (`tools/adversarial_eval.py`) judges
its task against one answer, SUPPORTED. A loop that concluded SUPPORTED
whatever its evidence said would pass that task, and so would an
evaluation whose oracle could not be anything else.

Four counterfactual cases ask the owner's question of other worlds:

- CF1, below 320 K: REFUTED;
- CF2, below 340 K: SUPPORTED;
- CF3, below 1000 K with a hundred times the source: REFUTED;
- CF4, below 1000 K with ten times the source: SUPPORTED.

Each world's answer comes from the slab's closed-form solution in
`tools/verify_assurance.py`. It is not computed by the model the loop
runs, and no agent is shown it. At the documented point the closed form
agrees with `docs/assurance/slab_uq.json`'s reference to 1e-9 K. A case
within 1 K of its threshold is refused. Every case is concluded as its
world answers.

The default world is unchanged. The objective's statement is still
written "1000 K", and every result already recorded, for the benign run
and the sixteen scenarios under both conditions, is regenerated
identically. The results only gain the counterfactual rows. The portable
assurance package, built from the same world, is still current.

**The positive control.** A research ledger whose verdict rule calls every
decided hypothesis SUPPORTED passes the evaluation's task, and fails CF1
and CF3, exactly where the answer is REFUTED.

**Mutations.** CE1 to CE4 (`tools/mutations/counterfactual_eval.json`)
are each killed. Three make the harness vacuous; CE4 is the rubber stamp
in `agent_substrate/research.py`. Run against other suites, CE4 is killed:

- by the research ledger's own test of a resolved REFUTED prediction;
- not by the evaluation's own suites before these cases. At 97448ba,
  `tests/test_adversarial_eval.py`, `test_adversarial_scenarios.py` and
  `test_adversarial_controls.py` reproduce every recorded result of the
  sixteen scenarios, and CE4 survived them all: none of those results
  turns on a REFUTED verdict.

**Not established.** Anything about a language model, since the agent is
scripted. These are four worlds of one question, not a benchmark of
judgment.
