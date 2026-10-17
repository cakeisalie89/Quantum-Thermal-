# Deployment contract

What a deployment of the Scientific AI Harness must provide, what it gets,
and how to check both. This document makes no claim that a production
deployment exists. Nothing here has been deployed beyond the hosted CI
runners. `docs/security/readiness_matrix.json` records the state of each
capability: IMPLEMENTED, HOSTED_TESTED, DEPLOYMENT_REQUIRED,
EXTERNALLY_BLOCKED or UNVERIFIED.

## 1. Modes

| Mode | Entry point | Authority it can produce | Isolation it assumes |
|---|---|---|---|
| Governed batch | `snakemake governed_model` (the production caller), `GovernedModelRuns` | records in the authority store, promotions bound to the owner's published policy (D-2026-148) | §5 |
| Demonstration | `tools/harness_demo.py run` | none beyond its own throwaway history | §5 |
| Container | `Dockerfile` (non-root user `harness`), `container_verify.sh` | the same as governed batch, inside the image | §5 and §6 |
| Release build | `tools/supply_chain.py build` on a clean commit | a candidate and nothing else: no signature, no admission | none |
| Release verification | `tools/offline_verify.py` (a portable kit of three files), `tools/supply_chain.py admit` | ADMITTED only against an external release policy | §4 |
| AI agent | `.github/workflows/claude.yml` | none: it proposes through pull requests, and others review, sign and release | owner actions O-1 to O-7 |

## 2. Dependencies

* Python 3.12 with `uv sync --frozen`, from `uv.lock`.
* Separately locked runtimes, each installed from its lock by hash:
  * `integrations/fenicsx/environment.lock` — the FEniCSx check;
  * `integrations/fmi/runtime.lock` — FMPy;
  * `integrations/supply_chain/sigstore.lock` — sigstore 4.1.0;
  * `integrations/ro_crate/validator.lock` — the community RO-Crate
    validator.
* `git`, to build a release candidate from a commit's objects.
* `util-linux` `unshare` with `--kill-child` (2.32 or later), for tool
  containment (§5).
* The release verifier needs only Python 3.11 or later (standard library)
  plus the sigstore runtime above. It needs no repository checkout
  (`docs/security/reproducibility_contract.json`, `verifier_requirements`).

## 3. Trust configuration

Each item says where it comes from and what happens without it.

* **CI validation policy.** `docs/supply_chain_ci_policy.json`, labelled
  CI_VALIDATION_ONLY. It verifies the CI signer and never authorizes a
  release.
* **Release policy.** A `harness-release-policy/1` document. It **does not
  exist**: creating it is owner action O-8. It must be provisioned outside
  the bundle and outside the checkout, and `admit` refuses either location.
  It pins the trusted root by digest, names a tag ref and a workflow other
  than `supply-chain.yml` or `claude.yml`, and expires. Without it, nothing
  is admitted.
* **Sigstore trusted root.** A ClientTrustConfig provisioned by the
  verifier's operator through TUF, rooted in the `root.json` shipped in the
  hash-pinned wheel. The hosted jobs do this with
  `ClientTrustConfig.production()`. The verifier refuses offline
  verification without one, and refuses one found inside the bundle.
* **Actor key registry.** Supplied from outside the log. For production it
  is **EXTERNALLY_BLOCKED**: `production_registry()` refuses rather than
  authenticate nothing. An `AUTHENTICATED_REQUIRED` history is unreadable
  without it.
* **Signature provider (VETTED).** Must be injected by the deployment; none
  is configured. EXTERNALLY_BLOCKED.
* **Policy authorities.** The history's `authority.policy_binding`
  declaration names them. The governed path names `owner`, and only that
  principal's published, unwithdrawn and unexpired policies promote.

## 4. Secrets

* A tool's environment is **replaced**, never inherited (`run_bounded`
  `env=`). A secret reaches a tool only through a `SecretStore` grant, and
  every access is a log record.
* `ANTHROPIC_API_KEY` exists for the agent workflow only. Until the default
  branch carries the author gate, the owner should withdraw it (owner
  action O-2).
* No signing key exists anywhere. Signing is keyless, using a job's OIDC
  identity, and only the CI signer's job holds `id-token: write` besides the
  agent's.

## 5. Tool containment and limits

`run_bounded` applies these in every mode:

| Bound | Default | Enforced by |
|---|---|---|
| wall clock | per tool contract (`timeout_s`) | the executor: SIGTERM to the group, then SIGKILL |
| CPU seconds | 120 | `RLIMIT_CPU` |
| address space | 2 GiB | `RLIMIT_AS` |
| output bytes | 8 MiB | `RLIMIT_FSIZE` |
| additional tasks | 512 over current use | `RLIMIT_NPROC` |
| open descriptors | 256 | `RLIMIT_NOFILE` (D-2026-149) |
| core dumps | none | `RLIMIT_CORE` |
| environment | replaced | `Popen(env=...)` |

**Kernel namespaces (D-2026-149).** Where the host allows unprivileged user
namespaces, the tool also runs in its own user, PID and network namespaces,
under `_ns_init.py`:

* every process it starts dies with it, including any that called
  `setsid()`;
* it can reach no network, not even the host's loopback.

Each run records what it got in `ExecutionResult.isolation`. **Check the
host** before relying on it:

    uv run python -c "from agent_substrate.execution import namespace_isolation; print(namespace_isolation()[1])"

If the answer is `{"namespaces": [], ...}`, the bounds are the process
group and the rlimits alone. Provide the missing isolation at the
deployment layer:

* a container with no network for tool workers;
* a PID limit;
* a cgroup that is killed as a unit.

Hosts known to refuse unprivileged namespaces:

* Ubuntu 24.04 with `kernel.apparmor_restrict_unprivileged_userns=1`. The
  CI jobs relax this on their throwaway runners; a production host should
  instead provide an AppArmor profile for `unshare`, or the container
  isolation above.
* Container runtimes whose seccomp profile denies `unshare(CLONE_NEWUSER)`.

**Not provided by this code. These are DEPLOYMENT_REQUIRED:**

* confinement of filesystem READS (finding R-3), by a read-only root
  filesystem with only the workspace writable, a mount namespace that binds
  only the workspace, or an LSM profile;
* GPU, disk-space and network-bandwidth quotas;
* memory accounting beyond `RLIMIT_AS`, by cgroup `memory.max`.

## 6. Container privileges

The image runs as the non-root user `harness`. `container-verify.yml`
asserts this and mounts no host virtualenv. It needs no capabilities.
Kernel containment of tools inside the container needs unprivileged user
namespaces available to that user (see §5). Do not grant `CAP_SYS_ADMIN`
to obtain them; use container-level isolation instead.

## 7. Monitoring

| Signal | Command or API | What a failure means |
|---|---|---|
| completion and evidence state | `uv run python tools/harness_status.py verify` | a requirement's hosted evidence predates its implementation |
| history consistency | `tools/generic_consistency.py`, `tools/independent_verify.py` (separate process) | the second reader disagrees with the store |
| head witness | `EventLog.head()` against the external witness | truncation or substitution of the log tail |
| checkpoint audit | `AuthorityStore.recover` (reports `mode`, `healthy`) | a checkpoint the log does not pin, or from another reducer |
| undelivered escalations | `AgentDirectory.undelivered()` | a person was not told about a decision only they can make |
| containment achieved | `ExecutionResult.isolation` in each execution record | tools ran without kernel namespaces |
| workflow authority | `uv run python tools/workflow_contract.py --ref origin/main` | the deployed agent or a job holds more than its stated authority |

## 8. Recovery

* **Process restart.** `AuthorityStore.recover` loads from the newest usable
  checkpoint, or replays from genesis and says why. Stranded durable
  requests resume under their idempotency keys.
* **Interrupted writes.** Every authority-bearing write decides under the
  writer lock (`append_decided`). A crash leaves either the record or
  nothing, and never a record that a later load refuses (D-2026-147,
  D-2026-148). The writers still outside the lock are listed in finding
  T-2.
* **Trust services down.**
  * With the TUF or Rekor endpoints unreachable, online verification fails
    closed.
  * Offline verification still works from the bundle's inclusion proof
    against a previously provisioned trust configuration.
  * With the key registry unavailable, an authenticated history cannot be
    read; it is not read as unauthenticated.
* **Revoking a policy.** The owner calls `PolicyStore.withdraw`. Promotions
  under the policy stop at the next write and on every replay. Earlier
  promotions stand.
* **Revoking a release.** Not implemented: there is no release yet, and no
  revocation channel. It is part of owner action O-8's trust root.
