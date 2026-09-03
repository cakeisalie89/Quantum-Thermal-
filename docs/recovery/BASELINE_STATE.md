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
