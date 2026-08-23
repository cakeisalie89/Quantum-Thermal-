# Container verification status (Stage 7, honest accounting)

- **Container definition complete: YES** (Dockerfile + .dockerignore +
  container_verify.sh; **digest-pinned** base
  `python:3.12.11-slim-bookworm@sha256:519591d6…657bf7`; lockfile-frozen
  dependency install; non-root user; deterministic env: LANG/LC_ALL=C.UTF-8,
  TZ=UTC, PYTHONHASHSEED=0, BLAS/OMP threads=1).
- **Base-image digest: RESOLVED** —
  `sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7`
  (OCI image index; linux/amd64 sub-manifest
  `sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49`).
  Resolved from `registry-1.docker.io` via an anonymous pull token and
  confirmed stable across two independent requests. This was previously
  recorded as UNRESOLVED on the grounds that the sandbox had "no route to any
  container registry"; that is no longer true of this environment, and the
  digest is now pinned in the Dockerfile.

## Explicit state levels

These are the authoritative container states. Each is separately evidenced, so
a reader cannot infer runtime evidence from definition completeness.

| level | value |
|---|---|
| `CONTAINER_DEFINITION` | `STATIC_VERIFIED` |
| `BASE_DIGEST` | `RESOLVED_AND_PINNED` |
| `LOCAL_RUNTIME` | `AVAILABLE` |
| `LOCAL_BUILD` | `ATTEMPTED_BUT_BLOCKED_BY_BLOB_EGRESS` |
| `RUNTIME_BUILT` | `NO` |
| `RUNTIME_SCIENTIFICALLY_REPRODUCED` | `NO` |

- **Container build verified: NO** — and the blocker is no longer the one
  previously recorded. This document used to say a `docker` client was present
  but there was **no daemon** and `/var/run/docker.sock` did not exist. That was
  true of an earlier environment and is **false now**. What the executed
  evidence establishes in this environment:
  - `containerd` started successfully;
  - `dockerd` started successfully;
  - Docker **client and server** both report Engine **29.3.1**;
  - `docker info` succeeds — there is a working daemon;
  - the exact declared image build was **attempted**, not skipped;
  - the Docker Hub **manifest** endpoint resolves and responds;
  - **layer blob download fails**: `production.cloudfront.docker.com` is denied
    by the sandbox egress policy with **HTTP 403 on CONNECT**;
  - the base image was **not substituted** — swapping in a reachable image
    would verify a different artifact than the Dockerfile declares;
  - therefore the build **did not complete**.

  The blocker is egress policy on layer blobs, not the absence of a runtime.
- **Container execution verified: NO** — the image was never built, so nothing
  could be run from it. Not a daemon problem.
- **Container output identity verified: NO** (nothing was built to compare).
  Expected procedure once a build succeeds: run `container_verify.sh`, then
  compare the workspace outputs byte-for-byte against the **88** governed
  output hashes recorded in `final_manifest.json`. (This document previously
  said 87; the governed set is 88, as `build_hdf5_mapping.py` reports and
  `hdf5_output_mapping.json` records.) Bitwise identity across differing
  CPU/BLAS microarchitectures is **NOT** claimed in advance; it must be
  tested, not asserted.
- **Hosted path:** `.github/workflows/container-verify.yml` exists to close
  `RUNTIME_BUILT` on a GitHub-hosted runner, whose network is not subject to
  this sandbox's egress policy. It is `workflow_dispatch`-only and **has not
  run**. GitHub receives a `workflow_dispatch` only for workflows present on
  the **default branch**, so it cannot be dispatched while it exists solely on
  a feature branch; it becomes dispatchable once merged to `main`. No result is
  claimed for it here.
- **Static checks performed here:** Dockerfile parse/lint by inspection;
  `.dockerignore` excludes envs/caches/history; `container_verify.sh` is
  syntax-checked (`bash -n`) and its pytest-collection guard was executed
  against the real suite (361 tests collected, threshold 300).

## What changed in the verification script

`container_verify.sh` previously ran

    for t in tests/test_*.py; do python "$t"; done

Four test modules are pytest-only and define no `__main__` runner
(`test_authority_invariants`, `test_manifest_policy`, `test_record_parsers`,
`test_stage10_stack`). Executing those files directly runs **zero** tests and
exits 0, so the loop silently skipped **91 of 325** test functions — 28% of
the suite — while reporting success under `set -euo pipefail`.

The script now runs `python -m pytest tests/`, and first asserts that
collection yields at least 300 tests so a zero-collection or partial-collection
regression fails the container instead of passing quietly. It also now runs
`generate_manifest.py --check`, `validate_hdf5_equivalence.py` and
`ro_crate_tools.py validate`, which the original omitted.

Non-root execution and the deterministic environment controls are unchanged.
