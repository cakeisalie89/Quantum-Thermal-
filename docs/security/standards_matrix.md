# Standards matrix

How this repository's engineering relates to three external frameworks.
**No compliance, certification or attestation is claimed for any of them.**

The terms used here:

* **Provides** means the repository holds code or checks that address the
  practice, and names where they are.
* **Partial** means some of the practice is addressed and some is not.
* **Gap** means it is not addressed.
* **Org** means only the organization operating the system can address it.

## 1. SLSA (Supply-chain Levels for Software Artifacts, v1.0)

SLSA separates the *format* of provenance from the *build level* a
platform achieves. This repository produces the format and claims no level.

| | What exists | Status |
|---|---|---|
| Provenance format | `provenance.intoto.json`: an in-toto Statement v1 with the SLSA provenance v1 predicate layout. It records subjects by sha256, the source commit as the resolved dependency, `externalParameters` (repository, workflow, ref), the builder id and the invocation id | Provides |
| Provenance checked against the release | The portable verifier holds the subjects to the signed digests, the commit to the index, the builder to the policy, and the SBOM to the archive's own declarations (D-2026-143, D-2026-144) | Provides |
| `slsa_level_claimed` | `null` in every index; the verifier refuses an index that claims a level | Provides (no claim) |
| Build L1: provenance exists and is distributed | Generated, signed (by signing `SHA256SUMS`) and uploaded per run. No release distributes it yet | Partial |
| Build L2: a hosted platform generates and signs the provenance | Hosted runner, keyless signature by the job's OIDC identity. But the provenance is written by the build's own steps, not by the platform's control plane | Gap |
| Build L3: hardened, isolated build; signing secret unreachable from build steps | Not met: the build, signing and verification run in one job | Gap. The route would be a reusable-workflow provenance generator or platform attestations, in a protected, tag-triggered release workflow (owner action O-8) |
| Reproducible builds (not a SLSA level) | `docs/security/reproducibility_contract.json`: source.zip and the SBOM are byte-identical across builds; named fields of the provenance and index are run-specific; checked by two clones locally and by a second runner image in hosted CI (D-2026-145) | Provides, within the contract |

## 2. NIST SP 800-218 (Secure Software Development Framework, v1.1)

Each row is an SSDF practice and what this repository does toward it.

| Practice | This repository | Status |
|---|---|---|
| **PO.1** Define security requirements | `docs/security/findings.json`, `docs/security/readiness_matrix.json`, the completion contract (`tools/harness_status.py`) | Partial: the requirements are engineering ones; organizational policy is Org |
| **PO.2** Roles and responsibilities | Separate principals: the AI proposes, a governed task computes, an independent checker judges, a reviewer decides, a promoter promotes under the owner's policy. Release authority is the owner's (O-8) | Partial: the platform does not enforce them until owner actions O-4 and O-5 |
| **PO.3** Supporting toolchains | Actions pinned to commits; locks installed by hash; `tools/workflow_contract.py`; `typecheck_active.ini` | Provides |
| **PO.4** Criteria for security checks | Mutation specifications for every enforcement point, run in CI; a workflow contract; tests with negative controls | Provides |
| **PO.5** Secure environments | Default-deny workflow permissions; tool containment where the host allows it (D-2026-149) | Partial: branch protection, environments and Actions defaults are Org (O-4, O-7) |
| **PS.1** Protect code from tampering | Content audit; manifest over tracked files; the agent's commits signed | Gap at the platform: no protected branch, no CODEOWNERS (O-4, O-5) |
| **PS.2** Verify release integrity | Sigstore keyless signatures; a portable offline verifier; a release gate with an external, pinned policy | Partial: no release trust root exists (O-8) |
| **PS.3** Archive and protect releases | Provenance and the reproducibility contract | Gap: no releases are made |
| **PW.1** Design to meet requirements; threat modelling | Hostile cases A–H (`tests/test_hostile_composition.py`), `docs/security/pr17_inventory.json` | Provides, for this tranche's scope |
| **PW.4** Reuse well-secured components | Hash-pinned locks, a CycloneDX SBOM of every environment | Provides |
| **PW.7** Review and analyse code | ruff, mypy, content audit, adversarial review | Partial: human review is not enforced (O-4) |
| **PW.8** Test executable code | Unit, property, fuzz, mutation, state-machine, cross-process and hostile tests; the full suite in CI | Provides |
| **PW.9** Secure defaults | Default-deny policies, capabilities and permissions; isolation on by default; a digest-only run never admits | Provides |
| **RV.1** Identify vulnerabilities continuously | | Gap: secret scanning and dependency alerts are Org (O-9) |
| **RV.2** Assess and remediate | Defect ledger with priorities, statuses and fix commits | Provides (process) |
| **RV.3** Root-cause analysis | Each ledger record carries ROOT CAUSE and NOT CLAIMED sections | Provides (process) |

## 3. FedRAMP

FedRAMP authorizes a *cloud service offering operated by an organization*.
It assesses NIST SP 800-53 controls within an authorization boundary. A
code repository cannot be FedRAMP authorized. This section separates the
engineering evidence the repository can contribute to some control families
from the organizational requirements it cannot meet.

### Engineering controls the repository can contribute evidence to

| 800-53 family / control | Engineering evidence here | Status |
|---|---|---|
| AC-6 least privilege | Per-job workflow permissions held by the contract; capabilities scoped per task | Provides (engineering) |
| AU-2, AU-9, AU-10 audit events, protection, non-repudiation | Hash-chained event log, an external head witness, a second reader, signed appends | Partial: production keys are EXTERNALLY_BLOCKED |
| CM-2, CM-7 baseline configuration, least functionality | Locked environments, default-deny registries, a tool contract per tool | Provides (engineering) |
| IA-2, IA-5 identification and authentication | Authenticated-actor seam and key registry | Gap: no production registry or VETTED provider |
| SA-11 developer testing | Mutation, fuzz and hostile testing in CI | Provides (engineering) |
| SC-7 boundary protection | Network namespace per tool where available | Partial: DEPLOYMENT_REQUIRED |
| SI-7 software and information integrity | Digests and keyless signatures; the offline verifier | Partial: no release trust root |
| SR-3, SR-4 supply-chain controls, provenance | SBOM, provenance, reproducibility contract | Partial |

### Organizational requirements this repository cannot meet

* An authorization boundary.
* A System Security Plan.
* An independent assessment (3PAO).
* An authorization (agency ATO or JAB).
* A continuous-monitoring programme.
* Incident response.
* Personnel and physical security.
* FedRAMP-authorized hosting.
* FIPS 140-validated cryptographic modules. In particular, `ed25519.py` is
  a REFERENCE implementation, not validated and not constant time.

All of these are Org, and none is in progress here.
