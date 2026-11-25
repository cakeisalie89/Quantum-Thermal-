# Supply-chain threat model

A signature proves origin and integrity only; it never validates a
scientific result, and no signature here is release authorization
(`docs/supply_chain_ci_policy.json` is CI_VALIDATION_ONLY).

**Assets.** The source archive of a commit; `uv.lock` and the SBOM derived
from it; the in-toto provenance statement; the index binding the commit,
the manifest's and the crate's digests; `SHA256SUMS` and its Sigstore
bundle; the manifest pair; the RO-Crate.

**Actors/threats -> mitigations.**
- Artifact tampering (archive, SBOM, provenance, index, checksums) -> every
  layer verified beneath the signature, each against a forgery consistent
  everywhere else, so no deleted check hides behind the one above it;
  `tools/supply_chain.py tamper` changes one thing per copy and requires
  every copy refused (tested, and mutated by `tools/mutations/supply_chain.json`).
- Substitution / wrong-subject provenance -> the provenance subjects carry
  exact digests, and the archive is checked against the commit's tree in
  both directions (D-2026-127).
- Identity spoofing (wrong repository, ref, workflow, signer or issuer) ->
  the policy pins the exact signer identity and OIDC issuer; a wildcard or
  non-GitHub policy is refused by the verifier itself (tested).
- Dependency drift / lockfile swap -> the SBOM is generated from `uv.lock`
  and cross-validated package by package against the lock (tested); the
  numeric pins (numpy 2.4.4, scipy 1.17.1) are declared in `pyproject.toml`.
- Malicious build steps -> least-privilege workflow: only the signing job
  holds `id-token: write`; no secrets in the build; NO SLSA level claimed,
  because the provenance is written by the workflow it describes.
- Secret/path leakage -> absolute host paths are refused in the crate and
  the release metadata (tested).

**Out of scope (stated).** Compromised upstream wheels beyond hash pinning;
a key ceremony for an organizational trust root (none exists); release
publication, which needs the owner's separate authorization.
