# attic/ — archived material (historical; not part of the governed project)

`delivery_artifacts/` holds the milestone delivery artifacts (git bundles,
source tarballs, patches, a legacy zip) that were hand-uploaded to the
repository during development. They are historical records only: every
substantive commit they contain is an ancestor (or tree-equivalent) of the
current main history — verified by an explicit bundle-consolidation audit
(all tips ancestry-checked; duplicates proven by SHA-256).

Known exact duplicates kept for the record:
- `QTA_machine_fsm (1).bundle`  == `QTA_machine_fsm.bundle` (browser re-download)
- `QTA_3d_layer_source.tar(1).gz` == the original 3d-layer source tarball
- `QTA_full_history-6.bundle.txt` == `QTA_full_history.bundle` (misnamed copy)
- `QTA_reconstruction`-era bundles duplicate content preserved elsewhere

Nothing in this directory is imported, executed, regenerated, listed in
`final_manifest.json`, or referenced by any checker. Safe to relocate to a
GitHub Release at any time. MODEL-ONLY project; zero PASS; forecast-only.
