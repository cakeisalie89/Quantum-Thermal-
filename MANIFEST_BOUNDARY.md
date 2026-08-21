# Manifest Coverage Boundary — provenance is not authority

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

This document exists because a manifest regeneration produced a surprising
diff, and a future reader should not have to reverse-engineer the boundary
from that diff. The machine-readable form of what follows is
`final_manifest.json → coverage_policy`; the generator is
`generate_manifest.py`, which is the authority.

## 1. The distinction

The project answers two different questions in two different places, and
conflating them is the error this document is meant to prevent:

| Question | Answered by | Inclusion means |
|---|---|---|
| **What bytes exist, and what were their hashes?** | `final_manifest.json` (+ detached `manifest_hash.txt`) | these bytes were present at this SHA-256 — nothing more |
| **What is governed, and what owns it?** | `AUTHORITIES.md` / `authorities.json` | this module or file is the single source of truth for a concept |

A file can be preserved and hashed without being authoritative. The clearest
existing proof is `attic/delivery_artifacts/`: `README.md` states it is "not
part of the governed project", and all 19 of its files are nonetheless fully
hashed in the manifest — and were in the 343-entry manifest too. That is not
an inconsistency; it is the boundary working as designed. **Preservation and
authority are separate concepts.**

## 2. The policy

`final_manifest.json` covers **every git-tracked file**, with exactly two
exclusions:

- `final_manifest.json` — cannot hash itself;
- `manifest_hash.txt` — its detached hash, written after the manifest is
  finalized.

No other exclusion exists, and none should be added to make a diff smaller.
An unhashed tracked file is an unrecorded byte, which is the failure mode the
manifest exists to prevent.

Verify with `python3 generate_manifest.py --check` — it compares membership in
**both** directions plus every hash and size, and exits non-zero on drift.

## 3. What the audit found

Regenerating with the repository's own generator moved the manifest from 343
to 393 entries. Of the 50 additions, 20 are files this branch created. The
other **30 were already tracked and had simply never been recorded.**

### 3.1 The old manifest was generated outside git

This is provable rather than inferred. At commit `eea2dac` — the last commit
to touch `final_manifest.json` before this branch — the committed manifest
listed three files that **did not yet exist in the repository**:
`RELEASE_POLICY.md`, `SUPPLY_CHAIN_THREAT_MODEL.md`, and `verify_release.py`.
All three arrived in the *next* commit, `8512d0b`. A manifest produced by
`generate_manifest.py` against `git ls-files` cannot list a file git does not
have. It was therefore authored in the delivery working tree and uploaded,
which is also why the 30 files that only ever existed in git — every one of
them added by a GitHub web-UI "Add files via upload" or "Create J" commit —
were never picked up.

At the current `main` tip the manifest's 343 entries all hash correctly
against committed bytes. It was accurate about what it listed and silently
incomplete about what it did not. Nothing existed to detect that.

### 3.2 Classification of the 30

| Class | Count | Files | Assessment |
|---|---|---|---|
| Historical/archive material (duplicate) | 18 | root `QTA_*.bundle`, `*.tar.gz`, `*.patch`, `QTA_repaired_complete_1D_2D_repo.zip`, `QTA_full_history-6.bundle.txt` | **byte-identical to `attic/delivery_artifacts/` copies (18/18 verified)** — the attic copies were already hashed; these are root-level duplicates from web-UI uploads |
| Provenance / security artifact | 7 | `QTA_stage9_release_verification/{RELEASE_POLICY.md, SHA256SUMS, VERIFY_INSTRUCTIONS.md, provenance.intoto.json, release_index.json, release_trust_policy.json, sbom.cdx.json}` | genuine Stage-9 release evidence — exactly the artifact set `build_release_artifacts.py` emits. **Belongs in the provenance record.** Its `RELEASE_POLICY.md` duplicates the root copy |
| Non-authoritative repository material | 2 | `stage7_reports/J`, `stage8_reports/J` | 1-byte files containing a single newline (both SHA `01ba4719…`), created by the "Create J" web-UI commits. Accidental |
| Authoritative source (duplicate) | 3 | `units.py`, `verification.py`, `vibration_transfer.py` | byte-identical copies of `qta_multiphysics/` modules. **Dead at the root**: `verification.py` and `vibration_transfer.py` open with `from .units import …` and raise `ImportError: attempted relative import with no known parent package`. Nothing imports any of them |

### 3.3 Effect of inclusion

Provenance metadata only. Verified, not assumed:

- No consumer enforces an upper bound on the manifest. `package_consistency_check.py`
  requires only that the release ZIP is a **subset** of the manifest (step e) and
  that a fixed required-file list is a subset (step 11).
- `build_release_artifacts.py` builds the release from a fixed 10-entry
  `key_files` list and an externally supplied ZIP — never from the manifest —
  so release contents, `SHA256SUMS`, and provenance subjects are unchanged.
- `ro_crate_tools.py` enumerates a fixed file tuple; crate validation is
  unaffected (30 entities, 24 referenced files, 0 problems).
- No solver, gate, threshold, or canonical output is touched. The gate table
  remains 83 gates, PASS = 0.

## 4. Decision

**The regenerated manifest is correct and is kept.** The generator's stated
contract — every tracked file minus the two detached — is the operative
semantic, is what `AUTHORITIES.md` registers, and is already what the manifest
does for `attic/`. The old manifest was stale, not narrower by design; there
is no repository evidence of an intended narrower boundary, and the one
document that describes a narrower *conceptual* scope (README on `attic/`) is
contradicted by that same manifest's own long-standing contents.

This is recorded as a **provenance correction**, not a scope expansion. None
of the 30 files gains scientific standing by being hashed.

## 5. Deliberately not done here

The audit surfaced 23 files that look like repository-hygiene defects: 18
root-level duplicates of attic archives, 3 unimportable duplicate modules, and
2 accidental 1-byte files. **They are left in place.**

Deleting tracked files is an owner-level decision about the repository, not a
side effect of a manifest fix, and this project's own rule is that historical
evidence is not removed to tidy a record. The recommendation — remove the 18
root duplicates in favour of the attic copies, remove the 3 dead root module
copies in favour of `qta_multiphysics/`, and remove the two `J` files — is
offered for a separate, deliberate change. Until then they stay tracked and,
correctly, hashed.

## Repository-hygiene classification and recommended migration (§30)

**STATUS: EXECUTED under owner authorization.** This section was originally a
recommendation. The 22 paths below were deleted after re-confirming every
piece of evidence at the commit that removed them. Removal of tracked
historical material is an owner decision, and the one earlier record that
claimed such a removal had already happened turned out to be false (see
`authorities.json :: competing_sources_record`, field `withdrawn_claim`) —
so this one was re-verified rather than trusted.

Every item below is byte-preserved in `final_manifest.json` today and would
remain preserved under the recommendation, because each duplicate has an
identical copy that stays.

| Class | Items | Evidence | Recommendation |
|---|---|---|---|
| **Accidental root duplicate** | `units.py`, `verification.py`, `vibration_transfer.py` | SHA-256 identical to `qta_multiphysics/<same name>`; a repository-wide import scan finds **0** importers of the root copies; all imports are package-qualified | Delete the three root copies. Execution authority is already unambiguous, so this changes no behaviour. Requires owner authorization. |
| **Duplicate historical archive** | 18 root-level `*.bundle`, `*.zip`, `*.tar.gz`, `*.patch` files (**18,101,197 bytes**) | Each is byte-identical to its counterpart under `attic/delivery_artifacts/`, verified by `cmp` for all 18 | Delete the **root** copies only; keep `attic/delivery_artifacts/`. The historical evidence survives intact at one canonical location. Requires owner authorization. |
| **Intentional historical evidence** | everything under `attic/delivery_artifacts/` | `README.md` describes `attic/` as "not part of the governed project"; it is nonetheless fully hashed, by design | **Keep.** Do not delete. This is the preservation copy the row above depends on. |
| **Accidental one-byte file** | `stage7_reports/J`, `stage8_reports/J` | each file is exactly one byte, a lone newline; no reader, no producer, no reference anywhere in the tree — consistent with a stray shell redirect | Delete. Requires owner authorization, but carries no evidentiary content. |
| **Active source** | the remaining 15 root `*.py` entry points (`qta_full_sim.py`, `generate_manifest.py`, `package_consistency_check.py`, …) | imported or executed by the canonical pipeline, CI, or `container_verify.sh` | **Keep.** |

Applied: the first, second and fourth rows, **22 tracked files and
18,119,230 bytes**, with **no** loss of preserved bytes (every archive is
retained byte-identical under `attic/delivery_artifacts/`, and every deleted
module has its authoritative copy under `qta_multiphysics/`) and **no** change
to execution authority or to any scientific result. Tracked file count
410 → 388.

Pre-deletion re-confirmation, all passing at the deletion commit:
three root modules byte-identical to their package counterparts with **zero**
importers; **17** root archives each byte-identical to its
`attic/delivery_artifacts/` copy; both `J` files exactly one `\n` byte. No
RO-Crate entity referenced any deleted path, and `package_consistency_check.py`
reads only `QTA_submission.zip`, which does not exist and is guarded by an
existence check.

Note that `outputs/` is a regeneration target, not a mirror: a blind
`cp outputs/* .` would overwrite the tracked `deep_surrogate_readiness.json`
(status `TRAINED_NOT_TRUSTED`, produced by an opt-in `--deep` run) with the
`NOT_IMPLEMENTED` stub written by an ordinary run, destroying a governed
evidence record. Promote individual regenerated artifacts, never the directory.

## Regeneration order (the derived chain is order-dependent)

Several generators hash artifacts that earlier generators rewrite, so running
them out of order produces a tree that is internally inconsistent even though
every individual step reported success. Two orderings were got wrong during
this remediation and both were caught by the checkers rather than by review:
`generate_manifest.py` run before `validate_hdf5_equivalence.py` (which
rewrites its own report), and `ro_crate_tools.py` run before a later source
edit (leaving a stale `qta_full_sim.py` checksum in the crate).

The correct order, after any change that affects a governed output or a
hashed source file:

```
python qta_full_sim.py              # canonical outputs -> outputs/
#   promote the changed artifacts from outputs/ to the repository root
#   (individually — see the note above about outputs/ not being a mirror)
python build_hdf5_mapping.py        # inventory + schema over the outputs
python build_hdf5.py                # HDF5 artifact from the mapping
python validate_hdf5_equivalence.py # rewrites stage8_reports/…report.json
python ro_crate_tools.py            # crate hashes outputs AND sources
python generate_manifest.py         # hashes everything above, so it is last
```

Then verify, in any order:

```
python generate_manifest.py --check
python validate_hdf5_equivalence.py
python ro_crate_tools.py validate
```

`container_verify.sh` runs all three verifiers, so a mis-ordered regeneration
fails the container rather than shipping.
