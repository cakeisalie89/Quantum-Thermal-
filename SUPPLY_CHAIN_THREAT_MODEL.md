# Supply-Chain Threat Model (Stage 9)

MODEL-ONLY / FORECAST-ONLY project. A signature proves origin and
integrity only; it never validates the physics or hardware, and the
scientific gate PASS count remains zero regardless of any attestation.

**Assets.** The source archive; the 88 governed outputs; the HDF5
artifact (469 datasets) and RO-Crate; uv.lock; the manifest pair; the
release bundle (inventory, SHA256SUMS, SBOM, provenance, policy).

**Actors/threats -> mitigations.**
- Artifact tampering (zip/HDF5/crate/SBOM/manifest/checksums) -> full
  SHA-256 inventory + SHA256SUMS + manifest byte-gates + provenance
  subject digests; consumer verifier recomputes everything (tested).
- Substitution / wrong-subject provenance -> provenance subjects carry
  exact digests; verifier hard-fails on digest or name mismatch.
- Identity spoofing (wrong repo/revision/workflow/builder/signer/issuer)
  -> release policy pins exact identities; wildcard trust is refused by
  the verifier itself (tested).
- Dependency drift / lockfile swap -> SBOM is generated from uv.lock and
  cross-validated package-by-package against the shipped lock (tested);
  scientific pins additionally hard-asserted (numpy 2.4.4, scipy 1.17.1,
  qutip 5.2.1, h5py 3.16.0).
- Malicious build steps -> least-privilege CI workflow (minimal
  permissions; no secrets in build; provenance records parameters);
  local builds are labeled local and claim NO SLSA level.
- Secret/path leakage -> release scanner rejects credential patterns and
  absolute /home|/tmp|C:\\ paths in release metadata (tested).
- Claim-boundary attacks (injected "PASS"/performed-experiment text) ->
  verifier scans release metadata and refuses (tested).
- Rollback/mix-and-match -> single release index binds all artifact
  digests + the authoritative input-zip digest chain back to Stage 8.

**Out of scope (stated).** Compromised upstream PyPI wheels beyond hash
pinning; hardware attestation; key ceremony for organizational roots
(no organizational trust root exists yet -- recorded).
