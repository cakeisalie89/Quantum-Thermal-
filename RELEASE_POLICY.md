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

The release path still ships the legacy QTA payload, whose own invariants
(its gate count, its governed output set, its HDF5 dataset count) are in
`docs/legacy/qta/RELEASE_POLICY.md` and are still checked by the legacy
verifier. The generic release contract replaces that payload as the generic
workflow lands (`ARCHITECTURE_CONVERGENCE_PLAN.md`).
