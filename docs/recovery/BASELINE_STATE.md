# QTA forensic recovery baseline

This file freezes the starting state for the total-state recovery branch. It is evidence about the recovery operation; it does not promote any historical checkpoint claim to current implementation authority.

## Repository boundary

- Repository: `cakeisalie89/Quantum-Thermal-`
- Authoritative base branch: `main`
- Base HEAD at recovery start: `096fb90fff089a62e4b20513de729a3e45a69f35`
- Base tree: `f810e9d8580edafe81fbe3956ba85c75d4c10c01`
- Recovery branch: `qta-complete-corpus-recovery`
- Recovery branch was created from the exact base HEAD above.
- The authoritative base branch is not to be reset, rewritten, or used as the working branch for forensic reconstruction.

The base commit is the merge of PR #15, `Fix hosted container reproduction and governed build-context coverage`. That merge preserved a deliberately open hosted-container finding: the declared container had not byte-reproduced the canonical 3D outputs on the hosted runner. `RUNTIME_SCIENTIFICALLY_REPRODUCED = NO`; the container remained `STAGED`; scientific PASS remained zero.

## Current scientific state observed on `main`

Current `final_manifest.json` declares:

- total gates: 83
- `PASS`: 0
- `CONDITIONAL`: 47
- `BLOCKED`: 23
- `UNKNOWN`: 2
- `DERIVED_CHECK`: 11
- all `can_PASS_now`: `NO`
- all `measured_in_this_system`: `false`
- full-cycle Monte Carlo pass rate: `0.0%`
- validated system: `NOT AVAILABLE`
- breakthrough claim: `NOT MADE`
- canonical `tau_c`: 292 us

The current manifest also preserves the mode-separated architecture:

- Mode A: Cryogenic Baseline / Stabilization
- Mode B: Material Processing / LCVD Growth, no sensing
- Mode C: Isolation / Purge / Cryopump / Thermal Recovery
- Mode D: Quantum Sensing, NV / helium isotope
- Mode D is not simultaneous with Mode B.

These are current-repository facts, not proof of hardware validation.

## Current implementation already known to be present/evolved

The recovery dossier and live repository inspection show that `main` already contains substantial work that was historically lost in earlier resets, including current/evolved forms of:

- `qta_full_sim.py` and the canonical gate/output pipeline;
- NumPy/SciPy scientific implementation;
- non-lumped and 3D multiphysics layers;
- FSM implementation and tests;
- parameter registries and semantic tests;
- Snakemake, `pyproject.toml`, `uv.lock`, and container definitions;
- HDF5 and RO-Crate tooling;
- Stage-9 release/SBOM/provenance/verification infrastructure;
- verifier hostile-input and trust-root hardening;
- FEniCSx/SALib/OpenMDAO/read-only-RAG/Rust/FMI/VTK/OpenUSD stack-adoption layers;
- deterministic-output and manifest checking;
- default-deny CI and hosted-container work.

Therefore recovery must not blindly replay old checkpoints over stronger current code. Historical material is recovery authority; this branch remains implementation authority only for what is actually integrated here.

## Forensic input boundary

The September recovery package records 83 actionable recovery records:

- 4 June reset/reconstruction records;
- 77 Stage-9 / CP-M0 through CP-M46 programme-event records;
- 1 separate August CP-M11 465-file staged-candidate record;
- 1 recovery-corpus integrity record.

The separately exported payload named `QTA_COMPLETE_CODEX_RECOVERY_CORPUS.tar.gz` is **quarantined** as a whole-archive source:

- observed exported size: `301274421` bytes;
- observed exported SHA-256: `e81a1fefce326ba9025658fe75ecdab88423d66f6bbcafbff0523d181071fee5`;
- preserved sidecar SHA-256: `19fac920dcf651ba85e32cc9d1c84d137f966f87631ca19383545c8232d1269e`;
- the observed payload failed `gzip -t` with `unexpected end of file`.

Do not edit toward the sidecar hash and do not treat a partial extraction as canonical bytes. Mine the 15 original export ZIPs, independently surviving assets, V3 recovery documents, transcript/message indexes, source archives, Git objects, patches, and tool traces instead.

## Historical states are comparison targets, not current claims

Historical checkpoint SHAs, file counts, test counts, output counts, and reported closure states are search/reconstruction anchors only until independently reproduced. Missing historical Git objects must not be fabricated. If exact bytes cannot be established, use `RECONSTRUCTED`, `PARTIAL`, `BLOCKED`, or another weaker classification.

## Execution-environment note

The current recovery executor can read and mutate the repository through the authenticated GitHub integration. A direct unauthenticated `git clone` from the local analysis container was unavailable because that container had no DNS/network path to GitHub. Consequently this initial tranche performs repository comparison through GitHub source/commit APIs and local analysis of materialized recovery artifacts. Fresh whole-repository test execution must be recorded only when a runnable repository worktree is actually available.

## Preservation rule

No scientific implementation is changed by this baseline commit. All subsequent recovery tranches must:

1. cite/refer to their historical evidence source in the recovery ledger;
2. distinguish exact recovery from controlled reimplementation;
3. preserve stronger current hardening unless a specific regression is demonstrated;
4. run relevant verification when an executable worktree is available;
5. commit coherent tranches before moving to unrelated recovery areas;
6. keep `PASS=0` and other blockers unchanged unless fresh authoritative evidence actually earns a different state.

## Step-1 continuation snapshot — 2026-09-03T18:03:32+00:00

This appended snapshot supersedes the earlier execution-environment note only
for live continuation location. It does not alter the original baseline or any
scientific conclusion.

| Field | Exact state at capture |
|---|---|
| Repository root used for PR continuation | `/workspace/scratch/12784d09e747/QTA_RECOVERY_PR16` |
| Git layout | linked detached worktree |
| Worktree Git dir | `/workspace/scratch/cdd815e2492a/QTA_CURRENT_MAIN/.git/worktrees/QTA_RECOVERY_PR16` |
| Common Git dir | `/workspace/scratch/cdd815e2492a/QTA_CURRENT_MAIN/.git` |
| Authoritative base branch | `main` |
| Base SHA / tree | `096fb90fff089a62e4b20513de729a3e45a69f35` / `f810e9d8580edafe81fbe3956ba85c75d4c10c01` |
| Local base / remote base | both `096fb90fff089a62e4b20513de729a3e45a69f35` |
| Recovery branch | `qta-complete-corpus-recovery` |
| PR head / tree before Step-1 writes | `e7d74550c8cd3b725f7d4363e1b056662ab39a3c` / `a27aef5d209c9064e7206803d71209d4c56c2ae4` |
| Remote recovery SHA | `e7d74550c8cd3b725f7d4363e1b056662ab39a3c` |
| Merge base | `096fb90fff089a62e4b20513de729a3e45a69f35` |
| Ahead / behind base | 4 / 0 |
| Local recovery upstream | none |
| PR | [#16](https://github.com/cakeisalie89/Quantum-Thermal-/pull/16), open draft |
| PR target | `main` |
| PR commits / changed files | 4 / 4 |
| PR checks at captured head | `stack-verify (core)` success; `stack-verify (full)` success |
| Tracked files | 420 |
| Changed files against base | 4 |
| Pre-Step-1 staged / unstaged / untracked | 0 / 0 / 0 |

### Branch synchronization finding

The original standard checkout contains a clean local branch with the same
name at `06a171aa4fcce0fe2a19add39f9fd0c7f844aa59`. It diverges from the PR:
three local-only commits and four PR-only commits, with the base SHA above as
their merge base. It was not moved, merged, rebased, or force-pushed. An exact
bundle of the local-only commits is preserved under
`docs/recovery/evidence/`.

### Previous-session uncommitted work

Valuable uncommitted work exists outside the PR checkout:

- `QTA_CP_M11_RECOVERED`: exact 446-file intermediate, 46 staged paths
  (21 added, 25 modified), staged tree
  `0111f7949b961ddea201694c53dd902465160521`, full-index diff SHA-256
  `cb4b05d84a11844a6c06e22548696e5b6645d73c49cfbe246c2edfc81326455e`.
  This is not the missing 465-file target.
- `QTA_LATE_CHECKPOINT_REPLAY`: detached at `06a171aa…`, with 10
  unstaged tracked changes and 141 nonignored untracked paths. The work spans
  multiple late checkpoints; `CURRENT_TRANCHE_NOT_YET_ESTABLISHED`.

The original worktrees and indexes were left untouched. Exact quarantined
captures are recorded under `docs/recovery/evidence/`, with every path and
hash listed in `STEP_1_CURRENT_RECOVERY_STATE.json`.

### Environment and lock

- OS: Ubuntu 24.04.3 LTS, Linux 6.18.35 x86_64.
- Python: `/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python`,
  version 3.12.13.
- Active virtual environment: none.
- `uv`: 0.11.33; Git: 2.51.1.
- Container engines: not available.
- `uv.lock`: present, 153,054 bytes, SHA-256
  `fc319fc32d27d82e1f0ee213288e9c6567da980881c93c27501b6a9ce9ef4dfc`.
- Locked-environment state: `NOT_SYNCHRONIZED`; the offline frozen check
  reported that a `.venv` and 22 packages would need creation/installation.
  No dependency was changed.

### Continuation documents and ledger

At capture, `BASELINE_STATE.md`,
`EXPORT_REPOSITORY_RECOVERY_LEDGER.md`,
`CP_M11_465_RECOVERY_STATUS.md`, and
`CP_M12_CONTROLLED_RECONSTRUCTION.md` existed. The other expected
continuation/closure documents did not. Step 1 adds
`CONTINUATION_STATE.md` and
`STEP_1_CURRENT_RECOVERY_STATE.json`.

The ledger contains seeded orders 0 through 82. The latest PR evidence tranche
is CP-M12. CP-M13 and later reconciliation remains pending; CP-M41R4 remains an
evidence gap; CP-M46R1 remains historical uncommitted salvage only; the exact
465-file CP-M11 target remains not reproduced; and the whole-corpus TAR remains
quarantined as corrupt/truncated.

### Step-1 blockers and next operation

Current blockers are the divergent same-named local/remote branches, the
unintegrated exact 446-file CP-M11 intermediate, the multi-tranche dirty replay,
the unsynchronized runtime, and the already-recorded scientific/hardware/
external-evidence blockers. Step 1 promotes none.

Next operation:

> Protect and disposition any uncommitted previous-session work before
> beginning new recovery mutations.
