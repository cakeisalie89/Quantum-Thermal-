# Release trust enforcement matrix

Every policy field, what authorizes it, what observes it, the exact comparison,
and the test that proves a wrong value fails. This document is not descriptive —
`tests/test_release_trust_enforcement.py` asserts each row, so a field that
stops being enforced breaks a test rather than quietly becoming documentation
again.

That is the failure this table exists to prevent: before this pass,
`source_repository`, `workflow_path`, `pinned_revision` and `trusted_builders`
were all present in the policy and read by nothing.

## Canonical source

| | |
|---|---|
| canonical policy | `QTA_stage9_release_verification/release_trust_policy.json` |
| schema + loader | `release_trust.py` (`load_canonical_policy`, `validate_policy`) |
| schema version | `2.0.0` |
| consumers | `build_release_artifacts.py` (copies it), `verify_release.py` (enforces it), `release_revision_gate.py` (revision semantics) |
| duplicate definitions | none — `trust_policy()` is now a loader with no values of its own |

The builder writes the canonical bytes into the bundle with
`release_trust.canonical_bytes`, and the verifier re-serializes the canonical
file with the *same* function and compares byte-for-byte. "Bundled equals
canonical" is therefore a byte comparison, not a semantic argument.

## Field matrix

| Policy field | Canonical value source | Observed runtime source | Verifier comparison | Negative test |
|---|---|---|---|---|
| `schema_version` | canonical file | bundled policy | exact `== "2.0.0"` | `test_rejects_old_schema_version` |
| `wildcards_forbidden` | canonical file | bundled policy | must be exactly `True`; plus structural wildcard scan over **every leaf** | `test_rejects_wildcard_in_any_leaf`, `test_rejects_wildcards_forbidden_false` |
| `source_repository` | canonical file | provenance `externalParameters.source_repository` **and** certificate SAN | three-way exact equality | `test_wrong_repository_fails`, `test_fork_repository_fails`, `test_same_workflow_other_repo_fails` |
| `workflow_path` | canonical file | provenance `externalParameters.workflow_path` **and** certificate SAN | three-way exact equality | `test_wrong_workflow_fails`, `test_discovery_workflow_identity_fails` |
| `authorized_ref` | canonical file (owner picks the tag name) | certificate SAN ref **and** provenance `externalParameters.authorized_ref`; `GITHUB_REF` and the tag target in the workflow | three-way exact equality | `test_wrong_tag_fails`, `test_bootstrap_tag_identity_rejected_when_final_tag_differs` |
| `signer_identity` | derived: `https://github.com/{owner}/{repo}/{workflow_path}@{authorized_ref}` | Fulcio certificate SAN | exact equality, **and** must equal the value implied by the policy's own components | `test_signer_identity_must_match_its_own_components`, `test_wrong_signer_correct_issuer_fails` |
| `oidc_issuer` | canonical file | certificate issuer | exact `== https://token.actions.githubusercontent.com` | `test_correct_signer_wrong_issuer_fails` |
| `pinned_revision` | canonical file (the **reviewed** revision `C`) | `git rev-parse HEAD` in the workflow; provenance `resolvedDependencies[].digest.gitCommit` | 40-hex; ancestor of the released commit; `!=` it; `C..A` diff touches only the policy file | `test_pinned_revision_must_be_ancestor`, `test_pinned_equal_to_release_fails`, `test_unreviewed_change_fails`, `test_pending_revision_fails` |
| `trusted_builders` | derived: `github-actions://{owner}/{repo}/{workflow_path}@{authorized_ref}` | provenance `runDetails.builder.id` | exact list membership — never substring, prefix or suffix | `test_wrong_builder_fails`, `test_local_builder_cannot_satisfy_hosted`, `test_builder_prefix_trick_fails`, `test_builder_case_variation_fails` |
| `bootstrap_state` | canonical file | — | closed vocabulary; a **resolved** policy must record an authorizing state | `test_resolved_policy_must_record_authorization` |
| *(any unknown field)* | — | bundled policy | rejected | `test_unknown_field_rejected` |
| *(any field)* | — | bundled policy | no `PENDING` anywhere, structurally, case- and whitespace-insensitive | `test_pending_anywhere_fails`, `test_pending_inside_trusted_builders_fails`, `test_pending_case_and_whitespace_tricks_fail` |

## Release index trust model

**`UNTRUSTED_ENVELOPE_METADATA`.** `release_index.json` is mutated after signing
by the finalizer, so nothing may be authorized on its word.

| Index field | Classification | Independent validation |
|---|---|---|
| `release_artifact` | integrity-critical | name/size/SHA-256 recomputed from the actual zip |
| `files[]` | integrity-critical | cross-checked against `SHA256SUMS` and against the contents of the zip |
| `claims.scientific_gate_PASS_count` | authority-critical | recomputed from `results_gate_table.csv` **inside the signed zip**; the index must also *agree*, so an understating index is a failure |
| `provenance.slsa_level_claimed` | authority-critical | any non-`NONE` value fails outright; the level cannot be promoted by index text |
| `signing_status` | consistency metadata | signed state is derived from facts (declared bundle exists, parses, verifies). `PENDING` + a bundle on disk, or `SIGNED` + no bundle, are both failures |
| `signature_bundles` | routing | path-contained (no absolute paths, no `..`, no symlink escape), no duplicates, exact `{name, bundle}` shape; the referenced bundle must exist and cryptographically verify |
| `sbom`, `provenance` file refs | routing | the referenced files are read and validated directly |

## What is deterministic and what is execution-specific

| Class | Contents | Contract |
|---|---|---|
| deterministic release payload | the source zip, `SHA256SUMS`, SBOM, the canonical policy | reproducible bytes; digests must match exactly |
| execution attestation | provenance `runDetails.metadata.execution` — run id, attempt, observed ref, context SHA | **semantic** validity only; expected to differ between runs and never compared against the policy |

The stable builder id lives in `runDetails.builder.id` and *is* compared exactly.
Keeping it apart from per-run data is what makes one authorization cover a rerun
instead of being invalidated by a new run number.

## SLSA

`NONE`. Not claimed, not authorized, and no admission criteria exist. Enforced:
any non-`NONE` value in either the index or the provenance is a verification
failure.
