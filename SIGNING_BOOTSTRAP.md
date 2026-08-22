# Signing trust bootstrap

A trusted release is accepted only when the artifact, repository, workflow,
revision, builder, signer and issuer **all independently agree with a policy
that was authorized before the signature was accepted.** Cryptographic validity
alone is not authorization.

Signing status is **PENDING**. No pin is filled, no tag is cut, and no signature
has been produced or verified.

## What was wrong, and is now fixed

Four policy fields — `source_repository`, `workflow_path`, `pinned_revision`,
`trusted_builders` — appeared in the policy and were read by **nothing**.
`grep -c` in `verify_release.py` returned 0 for each. They documented an
intention that no code enforced.

Three further defects:

- **Two policy sources.** `build_release_artifacts.py` rebuilt the whole policy
  in Python, duplicating the checked-in file. They happened to agree; nothing
  made them agree.
- **A placeholder satisfied a trust check.** The CI builder id was the literal
  string `PENDING-hosted-runner`, and the SLSA guard was
  `if lvl != "NONE" and "hosted" not in builder`. The placeholder *contains*
  `hosted`, so an unresolved value passed the check.
- **The PENDING rule was two `if`s.** The policy said "every PENDING must be
  replaced"; the code checked `signer_identity` and `oidc_issuer` only. An
  unresolved entry inside `trusted_builders` passed silently.

## The self-reference problem, and how it is avoided

A commit cannot contain its own SHA, so a policy committed in revision `X`
cannot name `pinned_revision = X`. The earlier design implied it could.

`pinned_revision` is therefore defined as the **reviewed source revision** — not
the released commit:

```
C   pinned_revision. Reviewed content. Policy here is still unresolved.
A   the released commit. Child of C, identical except that it fills in the
    policy. refs/tags/<TAG> points at A.
```

`release_revision_gate.py` proves the relationship rather than asserting it:

1. the ref being built equals `policy.authorized_ref`;
2. `C` exists and is an **ancestor** of the checkout `A`;
3. `C != A` — `A` must be the commit carrying the authorization;
4. the `C..A` diff touches **only** the canonical policy file.

Check 4 is load-bearing. Without it, "descendant of a reviewed commit" would
authorize arbitrary later changes. It is verified working: a scratch repository
built in this exact order passes, and an authorization commit that also smuggles
an unrelated file is refused, naming the file.

Order of operations, all ordinary Git: **review `C` → commit `A` filling the
policy → tag `A` → push the tag.** No step requires knowing a hash before it
exists.

## The tag-A / tag-B contradiction, and why there is no bootstrap tag

The earlier document proposed: cut bootstrap tag A, observe the exact identity,
pin it, then release under a new tag B. That cannot work — the identity contains
the ref, so the identity observed under A can never equal the identity presented
under B. Pinning A's identity and releasing under B guarantees a mismatch.

**There is no bootstrap release tag.** The exact signer identity is a documented
deterministic function of three values the owner already controls:

```
signer_identity = https://github.com/{owner}/{repo}/{workflow_path}@{authorized_ref}
```

Nothing about it needs to be observed first. `release_trust.derive_signer_identity`
computes it, and `validate_policy(require_resolved=True)` **refuses a policy whose
`signer_identity` is not the value implied by its own
`source_repository`/`workflow_path`/`authorized_ref`** — so a hand-typed identity
that disagrees with its own components cannot be authorized.

The same holds for the builder:

```
stable builder id = github-actions://{owner}/{repo}/{workflow_path}@{authorized_ref}
```

If the prediction turns out to be wrong, the trusted run **fails verification**.
That is the correct outcome: a spent tag and no trust, rather than trust granted
to whatever turned up. It is never TOFU.

## What `identity-discovery.yml` is still for

It establishes what it genuinely can, and nothing more: the **OIDC issuer**, the
**SAN URI structure**, the **repository component**, and the **Fulcio extension
layout**. Its own certificate reads
`.../identity-discovery.yml@refs/heads/<BRANCH>` and can never equal a release
identity, so its SAN is **never** written into the policy as `signer_identity`.

It runs `workflow_dispatch` only, reports
`IDENTITY_DISCOVERY_ONLY — NOT A TRUSTED RELEASE`, signs a throwaway file rather
than the release zip, and preserves its output as
`UNTRUSTED-BOOTSTRAP-IDENTITY-EVIDENCE` so the trust status is structural rather
than a matter of reading a description. The value it confirms is the issuer,
`https://token.actions.githubusercontent.com`, which is identical for every
GitHub Actions run and so genuinely transferable to the release identity.

It is optional. It confirms the template the prediction instantiates; it is not a
required stage.

`release.yml` also keeps `preserve UNTRUSTED bundle when verification fails`
(`if: failure()`). With the bootstrap tag gone this is a **diagnostic aid**, not
a trust stage: if a prediction is ever wrong, the certificate that disproves it
survives for inspection instead of dying with the runner. It writes no pin and
confers nothing.

## Bootstrap state machine

`bootstrap_state` is a required policy field, validated against a closed
vocabulary:

| state | meaning | who may set it |
|---|---|---|
| `UNINITIALIZED` | no identity work done. **Current state.** | — |
| `IDENTITY_STRUCTURE_OBSERVED` | discovery ran; issuer and SAN structure confirmed | human, reviewed commit |
| `RELEASE_IDENTITY_AUTHORIZED` | exact identity, ref, revision and builder authorized | **human, reviewed commit** |
| `TRUSTED_RELEASE_ELIGIBLE` | policy resolved and consistent; tag not yet cut | human, reviewed commit |
| `SIGNED_AND_VERIFIED` | a real signature verified against this policy | human, after independent verification |

A resolved policy **must** record one of the last three states —
`validate_policy` refuses a fully-resolved policy still claiming
`UNINITIALIZED`, so resolution cannot happen without being recorded as an act of
authorization. No automated step performs any transition.

## The procedure

1. *(optional)* Dispatch `identity-discovery.yml` from the default branch.
   Confirm the issuer and SAN structure. Set `bootstrap_state` to
   `IDENTITY_STRUCTURE_OBSERVED` in a reviewed commit.
2. Choose the release tag name and the reviewed revision `C`. In a reviewed
   commit `A`, fill in `authorized_ref`, `signer_identity`, `oidc_issuer`,
   `pinned_revision = C`, `trusted_builders`, and set `bootstrap_state` to
   `RELEASE_IDENTITY_AUTHORIZED`. **`A` must change nothing else** — the
   revision gate rejects any other path in the `C..A` diff.
3. Tag `A` with the authorized tag name, once. Push it.
4. `release.yml` runs: it recomputes the revision from Git, checks it against
   the tag target and the Actions context, runs the revision gate, builds,
   verifies offline, signs, finalizes signing metadata, and verifies online
   against the policy that already existed.
5. Verify independently, off the runner: signature, certificate chain, issuer,
   SAN, repository, workflow, ref, artifact SHA-256, source revision, SLSA
   predicate and Rekor inclusion. Only then set `SIGNED_AND_VERIFIED`.

Never re-point or mutate a tag that has been signed.

## Post-sign metadata finalization

`build_release_artifacts.py` writes `signing_status: "PENDING"` and
`signature_bundles: []`; the signing step writes the Sigstore bundle and updates
neither; `verify_release.py` refuses to treat a bundle as signed without both.
Online verification therefore could not succeed regardless of the pins.

`finalize_release_signing.py` closes exactly that gap, between signing and
online verification. It is **implemented and wired into `release.yml`** — an
earlier revision of this document said it was unimplemented, which is no longer
true.

It makes one transition, `PENDING → SIGNED` plus one `signature_bundles` record,
and refuses on any precondition failure. It never reads or writes a trust-policy
field. Signature existence is not authorization: the identity gate is unchanged
and still runs afterwards.

## SLSA

**`NONE`.** No level is claimed or authorized. Admission criteria for a level do
not exist in this repository. Any non-`NONE` value in the index or the
provenance is now a verification failure, so no mutable string can promote it.
