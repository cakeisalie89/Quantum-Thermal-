# CP-M11 465-file candidate — forensic recovery status

## Scope

This record concerns the separate August CP-M11 comprehensive-authority staged candidate. It is not the historical CP-M11 row in the Stage-9 → CP-M46 checkpoint chain and must not be merged into that identity.

## Target identity

The historical final report and September recovery dossier record the target as:

- HEAD: `09b39da3a91c55a13dc2ff7c2c02e2c14b6c42f1`
- staged tree: `3d00837b3c7f76e40d1d8d5f8cade314e3c4457c`
- tracked files: `465`
- staged paths: `65`
- additions / modifications: `40 / 25`
- staged-diff SHA-256: `53c0c46ed832a09271e963e43b1473da2c8a324d33b0725f038d1de58525c8c4`
- `final_manifest.json` SHA-256: `085f4b300ec1f105f095ca9e63eb0cb86047ec2991f3f039a01c8f57419925f3`

Byte-exact identity is permitted only if **all six** staged-candidate validators above independently reproduce. Expected hashes are validators, not instructions for editing toward a result.

## Exact precursor recovered in this recovery run

`QTA_CP_M11_BOUNDARY_RECOVERY_INPUTS.zip` contains:

- `QTA_CP_M10_RECOVERY.bundle`
  - SHA-256 `c7a785acb403806e8e0f0295c9e509bb69eaa33e97d9ddc6d3fdc61b00f06070`
- `QTA_CP_M11_RECOVERY_SOURCE.tar.gz`
  - SHA-256 `3c8d846c97ef97acfb12bf047d82fe1269f9b2f0bc07b720e94ea89fbf403739`
- `RESTORE_CP_M11_BOUNDARY.sh`
  - SHA-256 `300699bc248c7b9b7a890132e274896bdf648a0a7c8fbfaf951f19c27073cdfd`

The restore script was executed in an isolated local worktree during this recovery and independently reproduced:

- HEAD: `09b39da3a91c55a13dc2ff7c2c02e2c14b6c42f1`
- HEAD tree: `8e6fdd890db04d36abbb2d8d365f5ac7a21e33dd`
- branch: `qta-recovery-cp-m11`
- staged tree: `654295a72603f48af88800f5b13c6c1fc9e1f009`
- tracked: `437`
- staged / unstaged: `37 / 0`
- additions / modifications: `12 / 25`
- commits above CP-M10: `0`
- staged-diff SHA-256: `bb987d341e38b8ba273c6cee17c88954dbbe55665ebc5f6498be40317215124e`

This 437-file state is therefore an authenticated executable precursor, not a prose reconstruction.

## Historical transition evidence

The later candidate is recorded as passing through:

`0111f7949b961ddea201694c53dd902465160521`
→ `dd573a832806869c742b35b5fb8f410f00992640`
→ `162594c05040df2cd8abac5ac94e8fac67cc7c77`
→ `3d00837b3c7f76e40d1d8d5f8cade314e3c4457c`

An August salvage pass reported these surviving states:

| state | staged tree | tracked | staged | added / modified | diff SHA-256 |
|---|---|---:|---:|---:|---|
| required comprehensive candidate | `3d00837b…` | 465 | 65 | 40 / 25 | `53c0c46e…` |
| newest then-intact worktree | `0111f794…` | 446 | 46 | 21 / 25 | `cb4b05d8…` |
| independently restorable candidate | `654295a7…` | 437 | 37 | 12 / 25 | `bb987d34…` |

The same salvage pass reported that the 465-file tree object and named final comprehensive deliverables were no longer present in the accessible workspaces/object databases at that time.

## Audit of the later ChatGPT export

The complete standard ChatGPT export was re-opened directly rather than relying only on the derived message database.

The `QTA Recovery V2 Task` conversation is preserved in `conversations-003.json` with 3,701 mapping nodes. The export preserves:

- the owner directives;
- assistant progress text;
- reasoning/thought summaries;
- the complete final comprehensive-authority report;
- the final sandbox paths to the 11 comprehensive deliverables;
- the target staged-tree/diff/manifest validators.

However, for the Work execution interval that created the missing 437→465 states, the standard ChatGPT export contains **no tool-role/tool-call nodes with the original shell commands, heredoc bodies, Python file-write payloads, or patch bodies**. The relevant interval consists of user text plus assistant text/thought summaries. The final report's sandbox links are references to a vanished scratch worktree and do not carry the source bytes.

The final report names at least:

- `QTA_CP_M11_COMPREHENSIVE_AUTHORITY_AND_CLOSURE_READINESS_REPORT.md`
- `QTA_CP_M11_COORDINATE_AUTHORITY_TABLE.csv`
- `QTA_CP_M11_HARDWARE_AUTHORITY_TABLE.csv`
- `QTA_CP_M11_OWNER_NUMERICAL_DECISION_REGISTER.csv`
- `QTA_CP_M11_EXTERNAL_EVIDENCE_REGISTER.csv`
- `QTA_CP_M11_MODEL_CONTRACT_BLOCKER_REGISTER.csv`
- `QTA_CP_M11_SOLVER_REGION_BINDING_TABLE.csv`
- `QTA_CP_M11_REJECTED_FALLBACK_REGISTER.csv`
- `QTA_CP_M11_CLOSURE_GATE_MATRIX.csv`
- `QTA_CP_M11_COMPREHENSIVE_NEGATIVE_AUTHORITY_MATRIX.csv`
- `cp_m11_inline_geometry_authority.json`

Those final sandbox artifacts were not found as separately exported Library assets under those names.

## Current classification

`PARTIAL — AUTHENTICATED_437_PRECURSOR_RECOVERED; 465_EXACT_MUTATION_PAYLOADS_NOT_PRESENT_IN_STANDARD_CHATGPT_EXPORT`

This is **not** a declaration that the 465-file candidate can never be recovered. Other admissible sources remain valid if found later, including:

- an intact 446/453/465 source archive;
- exact missing source files;
- the protected rollout exported through an authorized channel;
- an exact ordered mutation transcript;
- deterministic generator inputs sufficient to reproduce all six validators.

Until such evidence appears, do not:

- reconstruct final files from prose;
- edit toward expected hashes;
- call a controlled semantic reconstruction byte-exact;
- claim the historical 465 staged tree has been restored.

The 437-file precursor remains preserved as the safest executable base for semantic reconstruction and for the separate CP-M12 checkpoint replay.
