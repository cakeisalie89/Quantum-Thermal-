# Owner actions: controls a branch cannot deploy

These are the AI/CI isolation controls that only the repository owner can
apply, together with the observed state that makes each one necessary. None
of them is applied. Nothing in this document reports a control as deployed.

The rules this branch *can* hold, it holds and checks:
`tools/workflow_contract.py` (`ai_authority_problems`) and
`tests/test_ci_authority.py`, D-2026-146. Those rules govern files. Two
things put the deployed system outside their reach:

* A workflow started by an issue or comment event runs **from the default
  branch's file**. The author gate on this branch's
  `.github/workflows/claude.yml` has no effect until the default branch
  carries it.
* Branch protection, rulesets, environments, Actions settings, secrets and
  the app's installation permissions live in repository settings, not in
  any file.

Re-check the default branch at any time:

    uv run python tools/workflow_contract.py --ref origin/main

At the audit (default branch `096fb90`), that command reported:

    - identity-discovery.yml/discover: permissions {'contents': 'read', 'id-token': 'write'}, not {'contents': 'read'}
    - release.yml/verify-and-release: permissions {'contents': 'read', 'id-token': 'write'}, not {'contents': 'read'}
    - claude.yml: the job's condition does not require the author to be OWNER, MEMBER or COLLABORATOR; any account can start a job holding write access and the API key

## Observed state (read-only API, 2026-10-17)

| Setting | Observed | Source |
|---|---|---|
| Default branch protected | `false`; required status checks `enforcement_level: off` | `GET repos/cakeisalie89/Quantum-Thermal-/branches/main` |
| Rulesets | `[]` | `GET .../rulesets` |
| Workflows active on the default branch | `.github/workflows/release.yml`, `identity-discovery.yml`, `reference-backend.yml` and `source-snapshot.yml`, all `active`, although this branch retired them | `GET .../actions/workflows` |
| Agent job started by an integration bot | run 32391406642 (job 96498146722): started by a bot's comment, logged "No trigger was met", entered agent mode, **obtained a GitHub OIDC token**, and failed only at the app-token exchange | job log |
| Actions workflow permissions, fork-PR approval, allowed actions | **UNVERIFIED**: the session's proxy refuses `actions/permissions*` | — |
| Environments | **UNVERIFIED**: the session's proxy refuses `environments` | — |
| Installed app permissions | **UNVERIFIED**: not readable from this session | — |

## Checklist

Each item names what to do, where to do it, and how to confirm it. The order
is the order of exposure.

### O-1. Stop the ungated agent on the default branch (CI-1, P0)

Do one of the following:

* **Disable** the "Claude Code" workflow until the gate reaches the default
  branch: *Actions → Claude Code → ⋯ → Disable workflow*.
* Or bring the gated `.github/workflows/claude.yml` to the default branch
  through a reviewed pull request. Whether and how PR #17 merges is your
  decision; this file does not make it.

**Confirm.** `tools/workflow_contract.py --ref origin/main` reports no
`claude.yml` problem, or the workflow shows `disabled_manually` in
`GET .../actions/workflows`.

### O-2. Withdraw the agent's credential until O-1 holds (CI-1, P0)

*Settings → Secrets and variables → Actions*: delete `ANTHROPIC_API_KEY`, or
rotate it once O-1 is confirmed. The bot-started run above shows that the
job reaches the point of using its credentials.

### O-3. Review the agent app's installation (CI-4, P0)

*Settings → GitHub Apps → Claude → Configure*:

* restrict the installation to this repository;
* grant it no administration permission;
* never list it as a ruleset or branch-protection **bypass actor** (O-4).

The agent proposes. It does not approve, sign or release.

### O-4. Protect the default branch (CI-2, P0)

*Settings → Rules → Rulesets → New branch ruleset*, targeting the default
branch, enforcement **Active**, with **no bypass actors**:

* require a pull request before merging, with at least one approval;
* dismiss stale approvals when new commits are pushed;
* require approval of the most recent reviewable push, so whoever pushed
  last cannot be the approver;
* require review from Code Owners (O-5);
* require status checks to pass, with the branch up to date. Use the job
  names, as they appear on a run:
  * agent-substrate: `agent-substrate`, `full-suite`, `second-interpreter`,
    `dispatch-sensitivity`, `mutation-matrices`;
  * harness-integrations: `fenicsx`, `fmi`, `end-to-end`, `ro-crate`,
    `performance-telemetry`;
  * stack-verify: `stack-verify`;
  * container-verify: `container-verify`;
  * supply-chain: `sign-and-verify`, `offline-verify`,
    `rebuild-elsewhere`;
* block force pushes;
* restrict deletions.

**Confirm.** `GET .../branches/main` shows `protected: true`, and
`GET .../rulesets` lists the ruleset.

### O-5. Code owners for the trust surface (CI-4, P1)

Add `.github/CODEOWNERS` to the default branch, naming the owner for at least:

    /.github/                        @cakeisalie89
    /tools/offline_verify.py         @cakeisalie89
    /tools/supply_chain.py           @cakeisalie89
    /tools/workflow_contract.py      @cakeisalie89
    /docs/supply_chain_ci_policy.json @cakeisalie89
    /docs/security/                  @cakeisalie89
    /agent_substrate/                @cakeisalie89

This branch does not add the file, because it takes effect only on the
default branch together with O-4, and choosing who reviews is your
decision.

### O-6. Retire the default branch's leftover signing workflows (CI-3, P1)

*Actions → (workflow) → Disable workflow* for:

* `.github/workflows/release.yml` (it signs with `id-token: write` on a tag
  push);
* `.github/workflows/identity-discovery.yml` (`id-token: write`);
* `.github/workflows/reference-backend.yml`;
* `.github/workflows/source-snapshot.yml`.

Then add a **tag ruleset** (*Rulesets → New tag ruleset*) covering `v*` and
the retired programme's stage-tag pattern. Restrict creation, update and
deletion to the owner, with no bypass actors.

**Confirm.** `GET .../actions/workflows` shows each as `disabled_manually`.

### O-7. Actions defaults (CI-4, P1; current state UNVERIFIED)

*Settings → Actions → General*:

* Workflow permissions: **Read repository contents and packages
  permissions**. Every workflow here already declares `permissions: {}` and
  grants per job, so this is the backstop for a file that forgets.
* Clear **Allow GitHub Actions to create and approve pull requests**.
* Fork pull request workflows: **Require approval for all external
  contributors**.
* Actions permissions: **Allow select actions**, limited to the pinned set
  in use: `actions/checkout`, `actions/upload-artifact`,
  `actions/download-artifact`, `astral-sh/setup-uv` and
  `anthropics/claude-code-action`.

### O-8. A release trust root, if and when you want releases (P0 for any release)

No release trust root exists. `tools/offline_verify.py` refuses to treat
the CI policy as one, and refuses any release policy that names
`supply-chain.yml` (the CI signer) or `claude.yml` (the agent). To create
one:

1. Write a release workflow that signs only on a tag push and runs its
   signing job in an **environment** (for example `release`) with required
   reviewers and a deployment rule for `refs/tags/v*`.
2. Write a `harness-release-policy/1` document with these fields:
   * the exact signer identity, derived from repository, workflow path and
     tag ref;
   * the GitHub OIDC issuer;
   * label `RELEASE_TRUST_ROOT`;
   * `issued_by`;
   * `not_after`;
   * `trust_config_sha256`, the digest of the Sigstore trust configuration
     you provisioned.
3. Keep the policy **outside** this repository and outside any bundle. Both
   `admit` and the portable verifier refuse a policy found inside either.
4. Admit a candidate with
   `tools/supply_chain.py admit --dir RC --release-policy P --trust-config T --mode offline`.
   Exit 0 means admitted, and exit 3 means verified but not authorized.

Steps 1–3 are yours to take. This tranche creates no trust root and no
release.

### O-9. Repository security features (P2)

*Settings → Code security*: enable secret scanning with push protection,
and Dependabot alerts.

## What remains true after every item above

* A valid signature attests the origin and integrity of bytes. It does not
  attest that any number in them is correct.
* The agent's instructions (its system prompt) are not a control. The
  controls are the author gate, the permission sets, branch protection,
  required checks and review, and the release gate. All of them are outside
  the agent.
