# Verifying a QTA release

Offline: `python3 verify_release.py --zip <release.zip> --bundle <bundle_dir>` -- recomputes the zip digest, every SHA256SUMS entry, SBOM<->uv.lock, provenance subjects, policy pins (wildcards refused), secret/absolute-path scan, and the claim boundaries (PASS must be zero).

Online (only when signature bundles exist): add `--online`; the verifier refuses to treat absent signatures as success.

A signature proves origin and integrity only -- never physics or hardware validation.
