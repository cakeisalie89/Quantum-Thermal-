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

### Next actions

1. Run the full suite at the cluster 2 commit, then push it.
2. AUD-004: test the scientific-admission rule through the governed path,
   then fix the edge evidence rule in the store and in `reconstruct.py`.
3. Record REVIEWED_IN_FULL dispositions for the six files above.
4. Phase 2C: commit a work-count instrument, measure scaling curves, and
   reduce `projection()` and `__init__`'s full reads without losing the
   full-genesis path.
