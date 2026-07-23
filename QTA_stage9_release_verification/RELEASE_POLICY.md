# Release Policy (Stage 9) -- strict, fail-closed

1. **Authoritative chain.** Every release names its exact input archive
   (filename + size + SHA-256). Stage-9 input:
   QTA_stage8_hdf5_provenance_source.zip / 20,680,532 B /
   809baa0804cc2bda896c91706e3090354ad6a8f378771801e9d81c13fccdcfd0.
2. **Determinism.** Inventory, SHA256SUMS, SBOM and provenance are
   generated deterministically (sorted, LF, no timestamps beyond
   provenance's required fields, no absolute paths, no UU randomness).
3. **Identity pinning (no wildcards -- verifier-enforced).** Trusted
   builder ids, source repository, revision, workflow path, signer
   identity and OIDC issuer are pinned EXACTLY in
   release_trust_policy.json. Any "*" entry is itself a verification
   failure.
4. **Signing.** Keyless Sigstore (Fulcio+Rekor) via the pinned CI
   workflow only. Signature bundles ship ONLY if signing actually
   occurred; absence is stated, never simulated. A signature attests
   origin/integrity only -- never scientific validity.
5. **SLSA.** No SLSA level is claimed unless a qualifying hosted build
   actually runs and its provenance verifies. Local provenance is
   labeled builder.id=local-sandbox with no level.
6. **Verification gate.** A release is valid only if the consumer
   verifier passes offline checks (digests, inventory, SBOM<->lock,
   provenance subjects, policy pins, secret/path/claim scans) and --
   when signing exists -- online signature verification.
7. **Scientific invariants.** PASS=0, can_PASS_now=NO,
   measured_in_this_system=false, automatic_gate_effect=NONE,
   automatic_application=false; 88 governed outputs and 469 HDF5
   datasets byte-preserved; releases that violate any of these are
   invalid regardless of signatures.
