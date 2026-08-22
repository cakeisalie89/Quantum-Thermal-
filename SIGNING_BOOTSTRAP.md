# Signing trust bootstrap

`release.yml` cannot establish its own trust. This document states the
circularity precisely, and the staged procedure that breaks it without turning
first-use into authorization.

Signing status remains **PENDING**. Nothing here signs anything, and no pin in
`QTA_stage9_release_verification/release_trust_policy.json` is filled by any
automated step. All four pins named below are still `PENDING` in that file at
this commit.

## The circularity

The release job runs: build → offline verify → keyless sign → online verify →
upload artifact.

`verify_release.py --online` checks the policy **before** the signature, by
design:

```python
    for field, value in (("signer_identity", identity),
                         ("oidc_issuer", issuer)):
        if not value or value.startswith("PENDING") or "*" in value:
            fail_list(problems,
                      f"trust policy {field}={value!r} is not an exact pin; "
                      "no signature can be trusted until it is")
    if problems:
        return
```

That ordering is correct — a signature proves possession of a signing identity,
never that the owner intended to trust it — and it means:

1. The first hosted signing run **must** fail online verification, because
   `oidc_issuer`, `signer_identity`, `pinned_revision` and the hosted
   `trusted_builders` entry are all still `PENDING`.
2. Because the failing verification step precedes `upload-artifact`, the job
   aborts first and the Sigstore bundle — **the only artifact carrying the real
   certificate** — is destroyed with the runner.

So the naive plan "cut one tag and the four pins fill themselves" is wrong on
both counts: the run fails, and it fails in a way that discards the evidence the
pins would have been derived from.

This is a bootstrap problem, not a cryptographic one.

## What must not be done

Having the signing run read its own certificate, write the pins, and then
declare itself verified would collapse **observation of an identity** into
**authorization of that identity**. That is trust-on-first-use wearing the
costume of verification, and it removes the only thing the policy contributes.

Equally forbidden, and each of these defeats the policy entirely:

- a wildcard in signer identity, repository, workflow or issuer;
- accepting any GitHub Actions certificate;
- "first certificate wins";
- treating `PENDING` as permissive;
- accepting an identity because the signature cryptography validated.

## The staged procedure

Trust is split into separable acts: **discovery** (Stage 1) and **evidence
capture** (Stage 2) produce observations; **authorization** (Stage 2b) confers
trust and is performed only by a human in a reviewed commit; **verification**
(Stage 3) then runs against a policy that already existed.

No single run performs more than one of these.

### Stage 1 — identity discovery (untrusted)

`.github/workflows/identity-discovery.yml`, `workflow_dispatch` only. It signs a
throwaway file — deliberately **not** the release zip, so nothing it produces
can be mistaken for or promoted into a release signature — preserves the bundle
and certificate as an artifact named

```
UNTRUSTED-BOOTSTRAP-IDENTITY-EVIDENCE
```

and prints the observed SAN and issuer. It runs no verification, so nothing
fails before the upload and the evidence survives. It reports
`IDENTITY_DISCOVERY_ONLY — NOT A TRUSTED RELEASE`, writes no pin, and touches
neither `release_trust_policy.json` nor the release path. Its permissions are
`contents: read` and `id-token: write` — the same minimum the release job needs,
and nothing more.

The artifact name carries the trust status structurally. A reader does not have
to consult a description to know the material is unauthorized.

**What Stage 1 cannot establish, and why.** The Fulcio SAN names the workflow
file that ran and the ref it ran on. A `workflow_dispatch` run of
`identity-discovery.yml` on a branch therefore yields

```
https://github.com/cakeisalie89/Quantum-Thermal-/.github/workflows/identity-discovery.yml@refs/heads/<BRANCH>
```

which differs from the release identity in **both** components — a different
workflow file and a different ref. So discovery can **never** produce the exact
`signer_identity` string that must be pinned, and any procedure that asks a
reviewer to "compare and pin what discovery observed" is asking for something
the mechanism cannot deliver.

What discovery *does* establish, which is still worth having before a tag is
ever cut:

| establishes | does not establish |
|---|---|
| the exact `oidc_issuer` (identical for every GitHub Actions run) | the exact `signer_identity` |
| that the SAN is a URI of the form `https://github.com/OWNER/REPO/.github/workflows/FILE@REF` | the `FILE` and `REF` a release run will carry |
| the repository component, verbatim | `pinned_revision` |
| the Fulcio extension layout, so a reviewer knows which fields to read | `trusted_builders[hosted]` |

The exact release identity comes from Stage 2 instead.

### Stage 2 — the first release run fails, and keeps its certificate

Cut a bootstrap tag. `release.yml` signs, online verification **fails** on the
PENDING pins exactly as designed, and the job's
`preserve UNTRUSTED bundle when verification fails` step — `if: failure()`,
after the verification step — uploads the bundle as

```
UNTRUSTED-RELEASE-IDENTITY-EVIDENCE
```

This is the only step that can yield the exact release `signer_identity`,
because it is the only one signing under `release.yml` at a tag ref. The run
confers nothing: it exits non-zero, writes no pin, leaves the trust policy
untouched, and never inspects its own certificate.

Because a signed tag must never be re-pointed, this bootstrap tag is spent: the
trusted release in Stage 3 uses a **new** tag.

### Stage 2b — authorization (human, in a reviewed commit)

Compare the observed claims against the prediction below. This is what keeps the
procedure from being TOFU: the identity is **predicted from documented semantics
first**, and the runs either confirm the prediction or contradict it.
A contradiction is a finding, not something to paper over by pinning whatever
turned up.

Predicted, for a tag-triggered run of `release.yml` in this repository:

| pin | predicted value |
|---|---|
| `oidc_issuer` | `https://token.actions.githubusercontent.com` |
| `signer_identity` | `https://github.com/cakeisalie89/Quantum-Thermal-/.github/workflows/release.yml@refs/tags/<TAG>` |
| `pinned_revision` | the commit the release tag points at |
| `trusted_builders[hosted]` | the same workflow identity |

These are predictions from GitHub OIDC and Fulcio semantics, **not verified
values**. Stage 1 checks the issuer and the SAN structure; Stage 2 supplies the
exact `signer_identity`, `pinned_revision` and hosted builder. Neither run is
asked to confirm something it cannot observe.

If they match, pin the exact observed strings in a separate commit, review it,
and merge it. If they do not match, stop and investigate — a certificate that
does not carry the expected workflow, repository or ref is exactly what the
policy is for.

### Stage 3 — trusted release

> **BLOCKER, unresolved: pinning the identity is necessary but not sufficient.**
> An earlier revision of this document said to "let `release.yml` run
> unchanged." That was wrong. There is a **second, independent** blocker, and
> the release cannot verify online until it is fixed by an owner-authorized
> change:
>
> `build_release_artifacts.py` writes `release_index.json` with
> `"signing_status": "PENDING"` and `"signature_bundles": []` hardcoded. The
> signing step writes `source.sigstore.json` but updates **neither** field. And
> `verify_release.py` rejects before it ever looks at a signature:
>
> ```python
> if status != "SIGNED" or not sig:
>     fail_list(problems, "online verification requested but no "
>                          "real signature exists (status="
>                          f"{status}); absence is never success")
> ```
>
> So online verification fails **regardless of whether the pins are filled**.
>
> What is required: a post-sign step that sets `signing_status` to `SIGNED` and
> lists the produced bundle in `signature_bundles`. Scope, verified against the
> code rather than assumed: `SHA256SUMS` covers the zip plus ten key files and
> **not** `release_index.json`, and `idx["files"]` is that same set, so the
> `release_index files != SHA256SUMS set` cross-check is unaffected and **no
> integrity regeneration is needed**. This is left unimplemented deliberately —
> it changes the release path's semantics, which is an owner decision, not a
> correction.

Once that is resolved, cut a new release tag against the pinned revision.
Verification then runs against a policy the owner pre-authorized, so a pass
means the signature came from the identity that was intended, not merely from
some identity.

Then verify independently, off the runner: signature, certificate chain, issuer,
SAN, repository, workflow, ref, artifact SHA-256, source commit, SLSA predicate
and Rekor inclusion. **A cryptographically valid signature over the wrong
artifact, commit, workflow or repository is a failure, not a pass.**

Never re-point or mutate a tag that has been signed.

## Why this is not circular

The signing run never decides whether its own identity is acceptable. Discovery
fixes the issuer and the SAN structure, and the first release run — which fails,
and is preserved precisely because it fails — supplies the exact identity; a
human compares both against a prediction written down beforehand; authorization happens in a
reviewed commit; and only then does verification run against a policy that
existed before the signature it checks. Each act is performed by a different
party at a different time, and naming the two evidence artifacts
`UNTRUSTED-BOOTSTRAP-IDENTITY-EVIDENCE` and
`UNTRUSTED-RELEASE-IDENTITY-EVIDENCE` puts their status in the one place a
consumer cannot skip past. That is a strong convention, not a mechanical
guarantee: nothing prevents someone from downloading it and pinning its contents
without reading. What the design removes is any *automated* path from
observation to authorization.
