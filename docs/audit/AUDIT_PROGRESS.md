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

### Next actions

1. Push the cluster 4 commit once its full suite passes, and attach
   hosted evidence through the repository's tools when the runs finish.
2. Phase 14: the trust-boundary matrix (operations against the checks
   each requires), the per-operation cost report, the test and evidence
   report, the claims-boundary report, the owner decisions (OD-1 to OD-7,
   O-1 to O-9, and AUD-001, AUD-010, AUD-011) and the 12-dimension
   readiness assessment.
3. Phase 9: a resumable campaign (AUD-013), if it is taken on here.
4. The ledger: most changed files are still NOT_REVIEWED. Continue with
   the trust surface: `governed_tasks.py`, `governed_model.py`,
   `secrets.py`, `capability.py`, `netauth.py`, and the executor.
