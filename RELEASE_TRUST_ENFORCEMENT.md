# Release trust enforcement matrix

Every policy field, what authorizes it, what observes it, the exact comparison,
and the test that proves a wrong value fails. This document is not descriptive —
`tests/test_release_trust_enforcement.py` asserts each row, so a field that
stops being enforced breaks a test rather than quietly becoming documentation
again.

That is the failure this table exists to prevent: before this pass,
`source_repository`, `workflow_path`, `pinned_revision` and `trusted_builders`
were all present in the policy and read by nothing.

## The external trust root

**A release cannot authorize itself.** A malicious artifact can always carry a
policy naming its own signer, so the expected identity must come from outside
the artifact.

```
verify_release.py --zip Q.zip --bundle b --online \
    --trusted-policy /path/to/owner-authorized-release_trust_policy.json
# or --trusted-policy-sha256 <64-hex>
```

`--online` **fails closed without one.** There is no fallback to the working
directory, the bundle, or the archive. Independent consumers must obtain the
authorized policy (or its digest) through a channel independent of the release.

**Both modes end in the same object** — a `TrustedPolicyRoot` carrying a
resolved policy, its canonical bytes, its digest and its source:

| mode | how the root is established |
|---|---|
| `--trusted-policy PATH` | read → parse → validate resolved → canonicalize → digest. If a digest is also supplied, the two must agree. |
| `--trusted-policy-sha256 DIGEST` | the digest **is** the root. The bundle's candidate policy is promoted only once its canonical bytes hash to the supplied value; only then are its values read. Not self-authorization — the consumer supplied the hash independently, and bytes matching it are the owner's bytes. |

Digest mode uses the **bundle** copy, never the archive copy: the archive
cannot be trusted before its signature is checked, and that check needs the
identity this root supplies. The root holds the bytes it authenticated, so a
candidate file modified afterwards cannot re-enter the decision.

An earlier implementation returned `(None, None)` for a "valid" digest-only
root, so digest-only verification could not work end to end — while a
loader-level test still passed. The mutation suite now restores that shape
specifically, and 11 tests fail.

An earlier implementation compared the bundled policy against a
repository-local path and, when that path did not exist, **skipped the
comparison** — so in the independent-verification case there was no root at
all. Offline verification now says `UNANCHORED` in that situation rather than
staying silent, and is documented as structural checking only.

Three words that must not blur:

| term | meaning |
|---|---|
| **authorization policy** | pre-authorized owner intent, supplied externally |
| **authenticated artifact** | bytes whose signature verified against that policy |
| **auxiliary metadata** | shipped alongside, never independently authenticated |

## Verification phases

```
1  trust root        load external policy; derive expected identity + issuer
2  candidate         parse untrusted bundle + archive; CLASSIFIED refusals
                     across the reproduced surface  (NO authority yet)
3  authenticate      Sigstore verify zip against the EXTERNAL identity
4  authenticated     zip policy == root; bundle policy == root;
                     read signed release_binding.json; recompute payload digest
5  auxiliary         cross-check provenance / SBOM / index
6  claims            recompute PASS from the signed gate table
```

No arrow points backward. The binding is read only in phase 4 — after the
signature that authenticates it has been checked against the policy.

## Phase 2: parsing hostile input

Phase 2 reads attacker-controlled data by design. A traceback is not a
verification result: it exits non-zero, so it looks like failing closed, but it
carries no classification, abandons the remaining diagnostics, and cannot be
told apart from a broken tool. Failing closed means reaching a *decision*.

`MISSING_RELEASE_ZIP` · `UNREADABLE_RELEASE_ZIP` · `INVALID_RELEASE_ZIP` ·
`MISSING_RELEASE_INDEX` · `INVALID_RELEASE_INDEX` · `MISSING_SHA256SUMS` ·
`INVALID_SHA256SUMS` · `MISSING_SBOM` · `INVALID_SBOM` · `MISSING_PROVENANCE` ·
`INVALID_PROVENANCE` · `MISSING_TRUST_POLICY` · `EMPTY_ZIP` ·
`INVALID_ZIP_STRUCTURE` · `MISSING_REQUIRED_ZIP_MEMBER` ·
`UNREADABLE_TRUST_POLICY` · `INVALID_TRUST_POLICY`

External trust root: `MISSING_TRUST_ROOT` · `UNREADABLE_TRUST_ROOT` ·
`INVALID_TRUST_ROOT`

### Validate first, then consume

Typing only the outer container was not enough. `files` was checked to be a
list, and the records inside it were first touched by

```python
{(e["name"], e["sha256"]) for e in idx["files"]}
```

— a set comprehension, which has no vocabulary for refusal. A `files` entry of
`{}`, `null`, `"a string"` or `{"name": ["x"], ...}` produced `KeyError`,
`TypeError: not subscriptable`, `TypeError: string indices` and
`TypeError: unhashable type` respectively. The same held for
`sbom["components"]` and `provenance["subject"]`, and the release ZIP path
itself was opened with a bare `zip_path.read_bytes()`.

`parse_candidate_bundle` now returns a `CandidateBundle`, and the collections
the verifier compares — `index_files`, `sbom_packages`, `subjects` — are built
**during** validation. That ordering is structural rather than conventional:
there is no accessor that yields an unvalidated record, so no later expression
can be the first place a malformed one is discovered.

Duplicate records are refused rather than absorbed. Two entries naming one file
with different digests collapse into two distinct set members, or with equal
digests into one, and either way the list's internal contradiction never
surfaces in a set comparison.

### Resource exhaustion is also a decision

Well-formed input can still be hostile by being too large or too deep.
`RecursionError` is not a `ValueError`, so a JSON document nested tens of
thousands of levels deep — a few hundred kilobytes, trivial to author — escaped
the decoder's handler entirely. Metadata size, JSON depth, and declared
uncompressed archive size are now bounded and classified; the archive bound is
read from the central directory, so a decompression bomb is refused before any
member is expanded.

Those bounds are **structural, not scientific**. No result depends on them, and
both sit orders of magnitude above the real artifact — a test asserts a healthy
release cannot reach either.

### A crash is not the only wrong answer

Two of the repairs available here were worse than the traceback they replaced,
and the tests exist to keep them out.

The archive is bytes, and `zf.read(...).decode()` is strict, so a required
member carrying invalid UTF-8 crashed the verifier. Decoding with
`errors="replace"` stops the crash — and then counts zero `PASS` rows in the
substituted text and reports **"scientific PASS count = 0"**. That turns
unreadable input into an affirmative safety claim. The count now comes back
UNKNOWN, and UNKNOWN fails: *absence of evidence is not evidence of absence.*

The same shape appeared one check earlier. When the archived `uv.lock` could
not be decoded the comparison was skipped — and fell through to the success
branch, printing **"SBOM matches uv.lock"** about a file it had never read. A
comparison that could not run is not a comparison that passed, so the success
branch is now reachable only from the branch that actually compared.

### One parser, one read, one validated byte sequence

The bundled trust policy was the last candidate structure parsed outside
`CandidateBundle`. Phase 2 checked only `path.exists()`, and **four** later
sites re-opened and re-parsed the same attacker-controlled file with four
different exception lists — so whether a hostile policy crashed the verifier
depended on which caller reached it first. A policy that was a directory
crashed `enforce_trust_policy`; one nested deeply enough to exhaust the stack
crashed both the offline path and the digest-only root.

There is now exactly one path from raw policy bytes to a validated document:

```
load_policy_document()
    regular file  →  size bound  →  read once  →  strict UTF-8
    →  JSON  →  object  →  schema
```

and exactly one authorization transition from that document to a
`TrustedPolicyRoot`. `CandidateBundle` carries the result, so the metadata
scan, the offline consistency check, the digest comparison and the
post-authentication binding all read the same retained bytes. A test asserts
the file is opened exactly **once** per run, and a second asserts the filename
appears exactly once in the module.

The single read is not an optimization. A second read is a TOCTOU window: the
bytes that were validated and the bytes that are used stop being the same
bytes, and the digest that authorized them no longer describes what is in use.

### Phase 0, and why it comes first

```
0 candidate material   read + structurally validate the bundled policy
                       NOTHING believed yet
1 trust root           external digest authenticates THOSE bytes
                       → TrustedPolicyRoot
2 candidate            the remaining bundle and archive structures
```

Phase 0 exists because the digest-only root authenticates the candidate's own
bytes. Putting the safe parse first means the digest path reuses it instead of
carrying a second policy reader. Structural parsing before the digest match is
fine; **authorization** comes only after digest equality.

`--trusted-policy` goes through the same loader with `require_resolved=True`
and its own codes. A malformed external root is an `INVALID_TRUST_ROOT` — a bad
*question*, not a bad release — and never a traceback.

### What is claimed

`tests/test_hostile_input_no_traceback.py` drives the real entry point with
every shape that was *reproduced* as an uncaught exception and asserts a named
code comes back. Thirty-three mutations, each re-opening one specific hole, are killed by the
test naming that property.

The claim is about the reproduced surface, not totality: it says every
malformation the adversarial suite exercises produces a classified refusal, not
that no input anywhere can crash the verifier.

Archive structure is validated before any member is read, and nothing is
extracted: exactly one top-level root; no absolute members; no `..`; no
backslash ambiguity; no duplicate raw or normalized names; no path that is both
a file and a directory; and each required member present exactly once as a file.

This matters for the payload digest specifically. Recomputation maps members
into a dict keyed by relative path, so two members normalizing to one key would
collapse into a single entry and silently change what the digest covers. The
structural check refuses that shape before any digest is computed.

## Canonical source

| | |
|---|---|
| canonical policy | `QTA_stage9_release_verification/release_trust_policy.json` |
| schema + loader | `release_trust.py` (`load_canonical_policy`, `validate_policy`) |
| schema version | `3.0.0` |
| consumers | `build_release_artifacts.py` (copies it), `verify_release.py` (enforces it), `release_revision_gate.py` (revision semantics) |
| duplicate definitions | none — `trust_policy()` is now a loader with no values of its own |

The builder writes the canonical bytes into the bundle with
`release_trust.canonical_bytes`, and the verifier re-serializes the canonical
file with the *same* function and compares byte-for-byte. "Bundled equals
canonical" is therefore a byte comparison, not a semantic argument.

## Field matrix

| Policy field | Canonical value source | Observed runtime source | Verifier comparison | Negative test |
|---|---|---|---|---|
| `schema_version` | canonical file | bundled policy | exact `== "3.0.0"` | `test_rejects_old_schema_version` |
| `wildcards_forbidden` | canonical file | bundled policy | must be exactly `True`; plus structural wildcard scan over **every leaf** | `test_rejects_wildcard_in_any_leaf`, `test_rejects_wildcards_forbidden_false` |
| `source_repository` | canonical file | signed `release_binding.json`; also proven by the certificate, since Sigstore checked the SAN against the identity derived from this field | exact equality against the authenticated binding | `test_binding_disagreeing_with_policy_fails`, `test_wrong_repository_fails`, `test_fork_repository_fails` |
| `workflow_path` | canonical file | signed `release_binding.json`; also proven by the certificate as above | exact equality against the authenticated binding | `test_binding_disagreeing_with_policy_fails`, `test_wrong_workflow_fails` |
| `authorized_ref` | canonical file (owner picks the tag name) | signed `release_binding.json`; `GITHUB_REF` and the tag target in the workflow | exact equality against the authenticated binding | `test_binding_disagreeing_with_policy_fails`, `test_wrong_tag_fails` |
| `signer_identity` | derived: `https://github.com/{owner}/{repo}/{workflow_path}@{authorized_ref}` | the certificate Sigstore verifies — this is the **only** genuinely observed certificate value, and it is observed *by* `verify_artifact`, not parsed beforehand | `Identity(identity=…, issuer=…)` from the **external** policy | `test_policy_to_sigstore_expected_identity_contract`, `test_signer_identity_must_match_its_own_components` |
| `oidc_issuer` | canonical file | certificate issuer | exact `== https://token.actions.githubusercontent.com` | `test_correct_signer_wrong_issuer_fails` |
| `pinned_revision` | canonical file (the **reviewed** revision `C`) | signed `release_binding.json`; `git rev-parse HEAD` in the workflow | 40-hex; ancestor of the released commit; `!=` it; `C..A` diff within the authorization closure | `test_pinned_revision_must_be_an_ancestor`, `test_pinned_equal_to_release_fails`, `test_unreviewed_change_fails` |
| `reviewed_payload_sha256` | canonical file | **recomputed from the authenticated archive** | exact equality — offline-checkable with no Git checkout | `test_payload_digest_detects_any_payload_change`, `test_payload_digest_excludes_the_authorization_closure` |
| `trusted_builders` | derived from repo/workflow/ref | **derived again** from the *authenticated* binding, never read from unsigned provenance | exact list membership | `test_builder_is_derived_from_authenticated_content_not_taken_on_trust`, `test_local_builder_cannot_satisfy_hosted` |
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
| *(external `provenance.intoto.json`)* | **`UNTRUSTED_AUXILIARY_METADATA`** | generated outside the signed zip and never itself signed. Cross-checked against the signed binding for consistency; grants **no** repository, workflow, ref, builder, revision or SLSA authority |
| *(`SHA256SUMS`)* | derived redundancy | not authenticated. Convenience for consumers; every entry is also verified against the signed archive's own contents, so deleting entries removes redundancy rather than weakening authentication |
| *(`sbom.cdx.json`)* | representation | not authenticated. Validated *against `uv.lock` inside the authenticated zip* — a representation checked against authenticated source, not a trusted claim |
| `signing_status` | consistency metadata | signed state is derived from facts (declared bundle exists, parses, verifies). `PENDING` + a bundle on disk, or `SIGNED` + no bundle, are both failures |
| `signature_bundles` | routing | path-contained (no absolute paths, no `..`, no symlink escape), no duplicates, exact `{name, bundle}` shape; the referenced bundle must exist and cryptographically verify |
| `sbom`, `provenance` file refs | routing | the referenced files are read and validated directly |

## Authorization closure

Determined experimentally, not guessed. Filling the policy at `C` and running
every required regeneration changes exactly these paths, and repeating reaches
a stable fixed point:

| path | class |
|---|---|
| `QTA_stage9_release_verification/release_trust_policy.json` | `AUTHORIZATION_INPUT` |
| `final_manifest.json` | `DETERMINISTIC_DERIVATIVE` |
| `manifest_hash.txt` | `DETERMINISTIC_DERIVATIVE` |
| `ro-crate/ro-crate-metadata.json` | `DETERMINISTIC_DERIVATIVE` |

The earlier rule — "only the policy file may differ" — was **not satisfiable
here**: `generate_manifest.py` hashes every tracked file except its own two
detached artifacts, so editing the policy necessarily changes the manifest, and
`release.yml` runs `generate_manifest.py --check` before building. The fix is to
widen the closure and require each derivative to equal an **independent
regeneration**; excluding the policy from manifest coverage would remove it
from governance and is forbidden.

Membership alone is not enough: the gate regenerates and compares bytes, and
also requires the authorization input itself to be present, so derivative churn
alone is not an authorization.

## Why the payload digest breaks the recursion

`reviewed_payload_sha256` covers every release-payload file **except** the
closure. So the policy records a digest of everything the policy does not
affect, and filling the policy in cannot change the value it records. Verified:
the digest is byte-identical at `C` and at `A`.

```
reviewed payload ──digest──> authorization policy ──> manifest/RO-Crate ──> zip ──> signature
```

No arrow points backward.

## What is deterministic and what is execution-specific

| Class | Contents | Contract |
|---|---|---|
| deterministic release payload | the source zip, `SHA256SUMS`, SBOM, the canonical policy | reproducible bytes; digests must match exactly |
| execution attestation | provenance `runDetails.metadata.execution` — run id, attempt, observed ref, context SHA | **semantic** validity only; expected to differ between runs and never compared against the policy |

The stable builder id lives in `runDetails.builder.id` and *is* compared exactly.
Keeping it apart from per-run data is what makes one authorization cover a rerun
instead of being invalidated by a new run number.

## On `trusted_builders`

Honestly: the stable builder id is a pure function of repository, workflow and
ref — the same three values `signer_identity` is derived from. It is therefore a
**second encoding of the same authority, not an independent observation**, and
this document does not claim otherwise. What it adds is defence in depth: the
verifier derives it again from the *authenticated binding* and requires exact
list membership, so a binding that asserts some other builder id is caught even
though the certificate alone would not have caught it. If a future release model
introduces builders that are not a function of those three values, this field
becomes genuinely independent; today it is not.

## SLSA

`NONE`. Not claimed, not authorized, and no admission criteria exist. Enforced:
any non-`NONE` value in either the index or the provenance is a verification
failure.
