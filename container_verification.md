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
| `RUNTIME_BUILT` | `YES_HOSTED` (container-verify run 37966478474, image built from `5ab44a7`) |
| `RUNTIME_SCIENTIFICALLY_REPRODUCED` | `DEMONSTRATION_EQUIVALENT_AT_DECLARED_RESOLUTION` (not byte identity; see below) |

The local values describe this development sandbox and still hold there; the
runtime values are the hosted runner's. Reproduction is not correctness: a
result reproduced in the image is the same result, not a verified one.

- **Container build verified in this sandbox: NO** (on a hosted runner: yes,
  see *Hosted path*) — and the blocker is no longer the one
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
- **Container execution verified in this sandbox: NO** — the image could not
  be built here, so nothing could be run from it. Not a daemon problem. (On a
  hosted runner it was built and run; see *Hosted path*.)
- **Container output identity verified: NOT CLAIMED** (byte identity between
  image and native runs is not claimed; the hosted comparison is under the
  equivalence policy, see *Hosted path*).
  The hosted comparison is between two runs of the same demonstration, under
  the equivalence policy; byte identity across differing CPU/BLAS
  microarchitectures is **NOT** claimed in advance.
- **Hosted path: RUN.** `.github/workflows/container-verify.yml` now runs on
  every push to the branch (it was `workflow_dispatch`-only, which GitHub
  delivers only for workflows on the **default branch**, so it could not run
  from a feature branch). On a GitHub-hosted runner, whose network is not
  subject to this sandbox's egress policy, run 37966478474 at `5ab44a7`:
  pulled the digest-pinned base and confirmed it against the served manifest;
  built the declared image (image id `sha256:9851705c…5bb242`, its layers and
  source commit recorded); confirmed the non-root user and no host virtualenv;
  ran `container_verify.sh` in the image as it then stood (its steps have
  since been reduced to the harness's own: the audits, the generic
  demonstration, the suite, the manifest and the RO-Crate) -- and ran the
  same demonstration natively. The comparison classified the slab
  result `EQUIVALENT_AT_DECLARED_RESOLUTION` with the decision stable, and
  both runs reached the same authority outcome: REJECTED, because neither has
  the FEniCSx runtime and the series check alone does not admit the slab. That
  is an equivalence of the two runs, not an admission; the admitting runs are
  the end-to-end job's, with both runtimes. Byte identity between image and
  native is not claimed.
- **Static checks performed here:** Dockerfile parse/lint by inspection;
  `.dockerignore` excludes envs/caches/history; `container_verify.sh` is
  syntax-checked (`bash -n`) and its pytest-collection guard was executed
  against the real suite (361 tests collected, threshold 300).

## What changed in the verification script

`container_verify.sh` once ran

    for t in tests/test_*.py; do python "$t"; done

Pytest-only modules define no `__main__` runner, so executing them directly
runs **zero** tests and exits 0: the loop silently skipped **91 of 325** test
functions -- 28% of the suite -- while reporting success under
`set -euo pipefail`.

The script runs `python -m pytest tests/`, and first asserts that collection
yields at least 300 tests so a zero-collection or partial-collection
regression fails the container instead of passing quietly. It announces
every step and prints a summary on exit, success or failure, because the
job-logs API serves only the tail of the log.

Non-root execution (user `harness`) and the deterministic environment
controls are unchanged; `tests/test_container_workflow_contract.py` holds
the user, the digest pin and every step the script must run.
