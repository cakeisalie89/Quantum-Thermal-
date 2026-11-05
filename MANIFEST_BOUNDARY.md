# Manifest coverage boundary — provenance is not authority

Part of the Scientific AI Harness. Hashing a file records its bytes; it does
not make the file an authority. The machine-readable form of what follows is
`final_manifest.json → coverage_policy`; the generator is
`generate_manifest.py`, which is the authority.

## 1. The distinction

The project answers two different questions in two different places, and
conflating them is the error this document is meant to prevent:

| Question | Answered by | Inclusion means |
|---|---|---|
| **What bytes exist, and what were their hashes?** | `final_manifest.json` (+ detached `manifest_hash.txt`) | these bytes were present at this SHA-256 — nothing more |
| **What is governed, and what owns it?** | `AUTHORITIES.md` / `authorities.json` | this module or file is the single source of truth for a concept |

## 2. The policy

`final_manifest.json` covers **every git-tracked file**, with exactly two
exclusions:

- `final_manifest.json` — cannot hash itself;
- `manifest_hash.txt` — its detached hash, written after the manifest is
  finalized.

No other exclusion exists, and none should be added to make a diff smaller.
An unhashed tracked file is an unrecorded byte, which is the failure mode the
manifest exists to prevent. Each entry is a path, its size and its SHA-256,
in sorted path order; a field no generator writes is refused, because a
narrative field nothing re-derives is a claim with no owner.

Verify with `python3 generate_manifest.py --check` — it compares membership in
**both** directions plus every hash and size, the detached hash, the schema
and the order, and exits non-zero on drift. It also refuses an untracked,
unignored file: one commit away from being required in the manifest.

## 3. Why membership is checked both ways

The manifest was once internally consistent — every hash it listed was
correct — while silently omitting 30 tracked files that had been added
through the GitHub web interface after it was last written. Nothing detected
that, because nothing checked membership in the direction that mattered.
`tests/test_manifest_policy.py` and `tests/test_manifest_completeness.py`
hold both directions now, and `tools/mutations/manifest.json` removes each
check in turn.

## 4. Regeneration order (the derived chain is order-dependent)

Several generators hash artifacts that earlier generators rewrite, so running
them out of order produces a tree that is internally inconsistent even though
every individual step reported success. After any change to a tracked file
that a derived artifact describes:

```
python tools/neural.py status                  # the learned-model status documents
python tools/repo_inventory.py derive          # the forensic inventory's dispositions
python tools/repo_content_audit.py --write     # the content audit of the tree
python tools/harness_status.py derive          # reads the content audit's report
python tools/corpus_allowlist.py --write       # digests both status documents
python ro_crate_tools.py                       # the crate hashes the above
python generate_manifest.py                    # hashes everything, so it is last
```

The manifest covers tracked files only: stage a new file before the last
step, or it is reported as untracked rather than hashed.

`ro_crate_tools.py validate` writes its verdict under `verification/`, which
is untracked, so it can run in any order. Then verify, in any order — these
are read-only:

```
python generate_manifest.py --check
python ro_crate_tools.py validate
python tools/repo_content_audit.py --check
python tools/harness_status.py verify
```

`container_verify.sh` runs them in the image, so a mis-ordered regeneration
fails the container rather than shipping.
