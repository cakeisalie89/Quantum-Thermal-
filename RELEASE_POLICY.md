# Release policy -- strict, fail-closed

1. **Authoritative chain.** Every release names its exact input (filename,
   size, SHA-256) and is built from it by the pinned CI workflow.
2. **Determinism.** Inventory, SHA256SUMS, SBOM and provenance are generated
   deterministically: sorted, LF, no timestamps beyond provenance's required
   fields, no absolute paths, no randomness.
3. **Identity pinning (no wildcards -- verifier-enforced).** Trusted builder
   ids, source repository, revision, workflow path, signer identity and OIDC
   issuer are pinned exactly in `release_trust_policy.json`. Any `*` entry is
   itself a verification failure.
4. **Signing.** Keyless Sigstore (Fulcio + Rekor) via the pinned CI workflow
   only. Signature bundles ship only if signing actually occurred; absence is
   stated, never simulated. A signature attests origin and integrity -- never
   scientific validity (`CLAIMS_BOUNDARY.md`, EB5).
5. **SLSA.** No SLSA level is claimed unless a qualifying hosted build runs
   and its provenance verifies. Local provenance is labelled
   `builder.id=local-sandbox` with no level.
6. **Verification gate.** A release is valid only if the consumer verifier
   passes its offline checks (digests, inventory, SBOM against the lock,
   provenance subjects, policy pins, secret/path/claim scans) and -- when
   signing exists -- online signature verification.
7. **Scientific content.** A release carries bytes, not authority: what its
   scientific content establishes is decided by the authority store and its
   admission policy, which a release can neither grant nor revoke.

## The generic release contract (directive 15)

A release of the framework is verified by answering, from the released
bytes alone:

| Question | Answered by, today |
|---|---|
| What bytes were released? | the inventory, SHA256SUMS and manifest (`verify_release.py`, `generate_manifest.py --check`) |
| What code and environment produced them? | the provenance statement and the SBOM against the lock (`verify_release.py`); each ResultBundle's implementation and environment digests |
| Do the evidence references resolve? | `tools/generic_consistency.py` (EVIDENCE) |
| Does the event history verify? | `tools/generic_consistency.py` (EVENT_HISTORY) |
| Does independent reconstruction agree? | `tools/generic_consistency.py` (RECONSTRUCTION: the independent reader refuses nothing and agrees with the live store) |
| Are the scientific artifacts what they claim, from admitted models and checks? | `tools/generic_consistency.py` (SCIENTIFIC_ARTIFACTS, IDENTITY) |
| Do signatures and provenance verify? | `verify_release.py` online, when signing occurred |

It requires none of the 83 QTA gates, PASS=0, `results_gate_table.csv`, the
machine FSM, the BOM or Mode B/C/D.

## The transitional path

The release workflow still ships the legacy QTA payload: it regenerates it
with `qta_full_sim.py` and checks it with the legacy verifiers
(`package_consistency_check.py`, the LEGACY_QTA_VERIFIER, and
`stage6_preservation_check.py`), steps the workflow marks as legacy. That
payload's own invariants (its gate count, its governed output set, its HDF5
dataset count) are in `docs/legacy/qta/RELEASE_POLICY.md`. The declared
generic artifact set that replaces it -- governed ResultBundles,
VerificationResults, the evidence store and the event history, with their
RO-Crate and HDF5 representation -- is not yet what the workflow builds; the
convergence plan carries it.
