#!/usr/bin/env python3
"""The single authority for release trust policy: load, validate, derive.

WHY THIS EXISTS
---------------
The policy had two hand-maintained definitions: the checked-in
``QTA_stage9_release_verification/release_trust_policy.json`` and a
``trust_policy()`` function inside ``build_release_artifacts.py`` that
reconstructed the same fields and values in Python. They happened to agree, but
nothing enforced it -- the reviewed policy and the policy actually shipped in a
release bundle could diverge silently. For an authority boundary that is not
acceptable, so the checked-in file is now canonical and this module is the only
thing that reads it.

Four fields were also pure documentation: ``source_repository``,
``workflow_path``, ``pinned_revision`` and ``trusted_builders`` appeared in the
policy and were never consulted by ``verify_release.py``. And the "every PENDING
must be replaced" rule was implemented as two named-field checks, so an
unresolved value anywhere else -- including inside ``trusted_builders`` -- passed
silently. This module makes all of that structural.

WHAT IS AUTHORIZED IN ADVANCE, AND WHAT IS OBSERVED
---------------------------------------------------
Every trust-critical value must be knowable *before* the signing run it
authorizes, or the policy is authorizing something it has not seen. The values
below are all derivable from (repository, workflow path, tag name, reviewed
revision), each of which the owner fixes by hand:

    signer_identity   = https://github.com/{owner}/{repo}/{workflow_path}@{authorized_ref}
    stable builder id = github-actions://{owner}/{repo}/{workflow_path}@{authorized_ref}
    oidc_issuer       = https://token.actions.githubusercontent.com

Observed at release runtime and compared for exact equality: the certificate
SAN, the certificate issuer, the checked-out revision (recomputed with
``git rev-parse``), the ref, and the provenance builder id.

THE SELF-REFERENCE PROBLEM
--------------------------
A commit cannot contain its own SHA, so a policy committed in revision X cannot
name ``pinned_revision = X``. See ``PINNED_REVISION_SEMANTICS`` below for the
definition that avoids it: the policy names the *reviewed source revision*,
which is a strict ancestor of the released commit, and the released commit is
required to differ from it only in the authorization record.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

#: The one canonical policy. Nothing else may define policy values.
CANONICAL_POLICY_PATH = Path(
    "QTA_stage9_release_verification/release_trust_policy.json")

#: 1.0.0 -> 2.0.0 added ``authorized_ref``/``bootstrap_state`` and moved
#: enforcement from documented to checked. 2.0.0 -> 3.0.0 adds
#: ``reviewed_payload_sha256``, which changes what authorization *means*: the
#: owner now authorizes a content digest that an offline consumer can
#: recompute, rather than a Git relationship only a repository holder can
#: check. Fundamental, so the version moves and older shapes are refused.
SCHEMA_VERSION = "3.0.0"

#: Marker for a value the owner has not yet authorized. Compared
#: case-insensitively after stripping, so whitespace or case cannot smuggle an
#: unresolved value past the gate.
PENDING_MARKER = "PENDING"

GITHUB_HOST = "https://github.com"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
BUILDER_SCHEME = "github-actions://"

#: The local builder. Explicitly never trusted for a signed hosted release.
LOCAL_BUILDER_ID = "qta:local-sandbox"

REQUIRED_FIELDS = (
    "schema_version",
    "wildcards_forbidden",
    "source_repository",
    "workflow_path",
    "authorized_ref",
    "signer_identity",
    "oidc_issuer",
    "pinned_revision",
    "reviewed_payload_sha256",
    "trusted_builders",
    "bootstrap_state",
)
#: ``note`` is free prose and carries no authority; it is the only optional key.
OPTIONAL_FIELDS = ("note",)
ALLOWED_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

#: Fields that must carry an exact resolved value before any signature is
#: trusted. Distinguished from fields that are already concrete at authoring
#: time (repository, workflow path) so an error can say which is which.
TRUST_CRITICAL_FIELDS = (
    "signer_identity",
    "oidc_issuer",
    "pinned_revision",
    "authorized_ref",
    "source_repository",
    "workflow_path",
    "trusted_builders",
)

PINNED_REVISION_SEMANTICS = """\
pinned_revision is the REVIEWED SOURCE REVISION: the 40-hex commit whose tree
the owner reviewed and authorized for release.

It is NOT the released commit. The released commit is the one the release tag
points at, which necessarily also contains the authorization record naming
pinned_revision -- and a commit cannot contain its own SHA. So the two are
deliberately different objects:

    C  (pinned_revision)     reviewed content; policy still unresolved here
    A  (released commit)     child of C; identical to C except that it fills in
                             the policy. refs/tags/<TAG> points at A.

The verifier therefore requires, at release runtime:

  1. the ref being built is exactly policy.authorized_ref;
  2. the checked-out revision (recomputed with `git rev-parse HEAD`, not read
     from an environment variable) equals the tag's target, A;
  3. policy.pinned_revision, C, is an ANCESTOR of A;
  4. the diff between C and A touches ONLY the authorization record --
     the canonical policy file. Any other change means content was released
     that the owner did not review under this authorization.

That chain pins the reviewed content without ever asking a commit to name
itself, and it is satisfiable with ordinary Git operations in the order:
review C -> commit A filling the policy -> tag A -> push tag.
"""

BOOTSTRAP_STATES = (
    "UNINITIALIZED",
    "IDENTITY_STRUCTURE_OBSERVED",
    "RELEASE_IDENTITY_AUTHORIZED",
    "TRUSTED_RELEASE_ELIGIBLE",
    "SIGNED_AND_VERIFIED",
)
#: Only a human, in a reviewed commit, may move the policy into or past this
#: state. No automated step may perform this transition -- that is the whole
#: content of the "no trust on first use" rule.
AUTHORIZING_STATES = frozenset({
    "RELEASE_IDENTITY_AUTHORIZED",
    "TRUSTED_RELEASE_ELIGIBLE",
    "SIGNED_AND_VERIFIED",
})

_SHA40 = re.compile(r"\A[0-9a-f]{40}\Z")
_REPO_URL = re.compile(
    r"\Ahttps://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"/([A-Za-z0-9._-]+)\Z")
_WORKFLOW_PATH = re.compile(r"\A\.github/workflows/[A-Za-z0-9._-]+\.ya?ml\Z")
_TAG_REF = re.compile(r"\Arefs/tags/[A-Za-z0-9._\-/]+\Z")


class PolicyError(Exception):
    """The policy is malformed, incomplete, or internally inconsistent."""


# ---------------------------------------------------------------------------
# structural scans
# ---------------------------------------------------------------------------

def _leaves(x, path="$"):
    """Every scalar leaf with its JSON path, for structural scanning."""
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, x


#: ``note`` is the one field documented as free prose carrying no authority,
#: and it necessarily discusses the PENDING rule in order to state it. It is
#: excluded from the unresolved scan by exact path -- not by weakening the
#: matcher, which still applies to every other leaf including nested ones.
_NON_AUTHORITATIVE_PATHS = frozenset({"$.note"})


def unresolved_leaves(policy) -> list[str]:
    """Paths of every authority-bearing leaf still carrying an unresolved marker.

    Structural, not a named-field list: an unresolved value nested inside
    ``trusted_builders`` is exactly as disqualifying as one in
    ``signer_identity``, and the previous two-field check let it through.
    Case- and whitespace-insensitive so ``" pending "`` cannot slip past.
    """
    out = []
    for path, v in _leaves(policy):
        if path in _NON_AUTHORITATIVE_PATHS:
            continue
        if isinstance(v, str) and PENDING_MARKER in v.strip().upper():
            out.append(path)
    return sorted(out)


def wildcard_leaves(policy) -> list[str]:
    """Paths of every leaf containing a wildcard character."""
    return sorted(p for p, v in _leaves(policy)
                  if isinstance(v, str) and ("*" in v or "?" in v))


# ---------------------------------------------------------------------------
# derivation -- every authorizable value is a pure function of the basics
# ---------------------------------------------------------------------------

def repo_slug(source_repository: str) -> str:
    """``owner/name`` from the canonical repository URL."""
    m = _REPO_URL.match(source_repository.strip())
    if not m:
        raise PolicyError(
            f"source_repository is not a canonical GitHub URL: "
            f"{source_repository!r}")
    return f"{m.group(1)}/{m.group(2)}"


def derive_signer_identity(source_repository: str, workflow_path: str,
                           authorized_ref: str) -> str:
    """The exact Fulcio SAN a GitHub Actions run of that workflow will carry.

    Deterministic from documented GitHub OIDC/Fulcio semantics, so it can be
    authorized *before* the run that presents it -- which is what makes
    prediction-then-verification possible instead of trust on first use.
    """
    return (f"{GITHUB_HOST}/{repo_slug(source_repository)}/"
            f"{workflow_path}@{authorized_ref}")


def derive_stable_builder_id(source_repository: str, workflow_path: str,
                             authorized_ref: str) -> str:
    """The stable builder identity a release from that workflow/ref represents.

    Stable means it names the authorized builder *class* -- repository,
    workflow, ref -- and carries no per-execution data. Run id, attempt and
    timing are execution metadata and belong in provenance's runDetails, never
    in the value the policy authorizes, or every run would need a new policy.
    """
    return (f"{BUILDER_SCHEME}{repo_slug(source_repository)}/"
            f"{workflow_path}@{authorized_ref}")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _require(cond, msg):
    if not cond:
        raise PolicyError(msg)


def validate_policy(policy, *, require_resolved: bool = False) -> dict:
    """Strict structural validation. Returns the policy or raises PolicyError.

    ``require_resolved=False`` validates shape only, which is what an
    unresolved (pre-authorization) policy must still satisfy.
    ``require_resolved=True`` additionally demands that every trust-critical
    value is an exact authorized value -- the gate a trusted release must pass.
    """
    _require(isinstance(policy, dict), "policy is not a JSON object")

    missing = [f for f in REQUIRED_FIELDS if f not in policy]
    _require(not missing, f"policy missing required fields: {missing}")
    unknown = sorted(set(policy) - ALLOWED_FIELDS)
    _require(not unknown,
             f"policy contains unknown fields: {unknown}. Extending the "
             "schema is a deliberate act: add the field to ALLOWED_FIELDS "
             "with validation, do not let it arrive silently.")

    _require(policy["schema_version"] == SCHEMA_VERSION,
             f"schema_version must be {SCHEMA_VERSION!r}, got "
             f"{policy['schema_version']!r}")
    _require(policy["wildcards_forbidden"] is True,
             "wildcards_forbidden must be exactly true")

    for f in ("source_repository", "workflow_path", "authorized_ref",
              "signer_identity", "oidc_issuer", "pinned_revision",
              "reviewed_payload_sha256", "bootstrap_state"):
        v = policy[f]
        _require(isinstance(v, str), f"{f} must be a string, got "
                                     f"{type(v).__name__}")
        _require(v.strip() != "", f"{f} must not be empty or whitespace")
        _require(v == v.strip(),
                 f"{f} has leading/trailing whitespace: {v!r}")

    tb = policy["trusted_builders"]
    _require(isinstance(tb, list) and tb,
             "trusted_builders must be a non-empty list")
    _require(all(isinstance(b, str) and b.strip() for b in tb),
             "every trusted_builders entry must be a non-empty string")
    _require(len(set(tb)) == len(tb),
             f"duplicate trusted_builders entries: {tb}")

    _require(policy["bootstrap_state"] in BOOTSTRAP_STATES,
             f"bootstrap_state must be one of {list(BOOTSTRAP_STATES)}, got "
             f"{policy['bootstrap_state']!r}")

    wild = wildcard_leaves(policy)
    _require(not wild, f"wildcard in trust policy (forbidden) at: {wild}")

    # source_repository is concrete from the start; a malformed one is an
    # error even before authorization.
    repo_slug(policy["source_repository"])
    _require(_WORKFLOW_PATH.match(policy["workflow_path"]),
             f"workflow_path must be .github/workflows/<name>.yml, got "
             f"{policy['workflow_path']!r}")

    if not require_resolved:
        return policy

    # ---- the resolved gate ------------------------------------------------
    unresolved = unresolved_leaves(policy)
    _require(not unresolved,
             f"policy still carries unresolved values at {unresolved}; no "
             "signature can be trusted until every one is an exact value")

    _require(_TAG_REF.match(policy["authorized_ref"]),
             f"authorized_ref must be refs/tags/<TAG>, got "
             f"{policy['authorized_ref']!r}")
    _require(_SHA40.match(policy["pinned_revision"]),
             f"pinned_revision must be a full 40-hex commit sha, got "
             f"{policy['pinned_revision']!r}")
    _require(re.fullmatch(r"[0-9a-f]{64}",
                          policy["reviewed_payload_sha256"]),
             "reviewed_payload_sha256 must be a 64-hex digest, got "
             f"{policy['reviewed_payload_sha256']!r}")
    _require(policy["oidc_issuer"] == GITHUB_OIDC_ISSUER,
             f"oidc_issuer must be exactly {GITHUB_OIDC_ISSUER!r}, got "
             f"{policy['oidc_issuer']!r}")

    expect_signer = derive_signer_identity(
        policy["source_repository"], policy["workflow_path"],
        policy["authorized_ref"])
    _require(policy["signer_identity"] == expect_signer,
             "signer_identity is not the identity implied by "
             "source_repository/workflow_path/authorized_ref.\n"
             f"  policy:  {policy['signer_identity']!r}\n"
             f"  implied: {expect_signer!r}")

    expect_builder = derive_stable_builder_id(
        policy["source_repository"], policy["workflow_path"],
        policy["authorized_ref"])
    _require(expect_builder in tb,
             "trusted_builders does not contain the stable builder id implied "
             f"by the policy: {expect_builder!r}; got {tb}")
    _require(LOCAL_BUILDER_ID not in tb,
             f"{LOCAL_BUILDER_ID!r} must never be a trusted builder for a "
             "signed hosted release")

    _require(policy["bootstrap_state"] in AUTHORIZING_STATES,
             "a resolved policy must record an authorizing bootstrap_state; "
             f"got {policy['bootstrap_state']!r}. Resolution is an act of "
             "authorization and must be recorded as one.")
    return policy


def load_canonical_policy(path: Path | None = None, *,
                          require_resolved: bool = False) -> dict:
    """Load and validate THE canonical policy. The only way to obtain one."""
    p = Path(path) if path is not None else CANONICAL_POLICY_PATH
    if not p.exists():
        raise PolicyError(f"canonical trust policy is missing: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise PolicyError(f"canonical trust policy is unreadable: {p}: {e}")
    if not raw.strip():
        raise PolicyError(f"canonical trust policy is empty: {p}")
    try:
        policy = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PolicyError(f"canonical trust policy is not valid JSON: {p}: {e}")
    return validate_policy(policy, require_resolved=require_resolved)


def canonical_bytes(policy: dict) -> bytes:
    """Deterministic serialization used both to write and to compare.

    One serializer for both sides, so "the bundled policy equals the canonical
    policy" is a byte comparison rather than a semantic argument.
    """
    return (json.dumps(policy, indent=1, sort_keys=True,
                       ensure_ascii=False) + "\n").encode("utf-8")


def is_resolved(policy: dict) -> bool:
    """True when the policy would pass the resolved gate."""
    try:
        validate_policy(policy, require_resolved=True)
    except PolicyError:
        return False
    return True


def bootstrap_state(policy: dict) -> str:
    return str(policy.get("bootstrap_state", "UNINITIALIZED"))


# ---------------------------------------------------------------------------
# the trust root, as an object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrustedPolicyRoot:
    """A resolved, authorized policy plus how it was anchored.

    Both modes must end here. Returning a bare ``(None, None)`` for a
    "valid" digest-only root -- as an earlier version did -- left the caller
    with nothing to verify against, so digest-only online verification could
    never work end to end while a loader-level test still passed. An explicit
    object makes that failure impossible to express.
    """

    policy: dict
    canonical_bytes: bytes
    sha256: str
    source: str          # "file" | "digest"

    def __post_init__(self):
        if self.source not in ("file", "digest"):
            raise PolicyError(f"unknown trust-root source {self.source!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise PolicyError(f"trust-root digest is not 64-hex: "
                              f"{self.sha256!r}")
        # A root that is not fully authorized is not a root.
        validate_policy(self.policy, require_resolved=True)


def normalize_digest(value) -> str:
    """Accept a user-supplied digest, or raise. Case and space normalized."""
    if not isinstance(value, str):
        raise PolicyError(f"digest must be a string, got "
                          f"{type(value).__name__}")
    v = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", v):
        raise PolicyError(
            f"digest must be exactly 64 hex characters, got {value!r}")
    return v


# ---------------------------------------------------------------------------
# authorization closure
# ---------------------------------------------------------------------------
#
# Determined experimentally, not guessed: filling in the policy at revision C
# and running every required deterministic regeneration changes exactly four
# tracked paths, and repeating the regeneration reaches a stable fixed point.
#
#     release_trust_policy.json                  AUTHORIZATION_INPUT
#            |
#            v
#     final_manifest.json, manifest_hash.txt     DETERMINISTIC_DERIVATIVE
#            |
#            v
#     ro-crate/ro-crate-metadata.json            DETERMINISTIC_DERIVATIVE
#                                                (records the manifest's size)
#
# The earlier design allowed only the policy file to differ between C and A.
# That is not satisfiable here: generate_manifest.py hashes every tracked file
# except its own two detached artifacts, so editing the policy necessarily
# changes the manifest, and release.yml runs `generate_manifest.py --check`
# before building. The fix is to widen the closure to the derivatives and
# require each to equal an INDEPENDENT regeneration -- not to exclude the
# policy from manifest coverage, which would remove it from governance.

AUTHORIZATION_INPUT_PATHS = frozenset({str(CANONICAL_POLICY_PATH)})

DETERMINISTIC_DERIVATIVE_PATHS = frozenset({
    "final_manifest.json",
    "manifest_hash.txt",
    "ro-crate/ro-crate-metadata.json",
})

AUTHORIZATION_CLOSURE = AUTHORIZATION_INPUT_PATHS | DETERMINISTIC_DERIVATIVE_PATHS


# ---------------------------------------------------------------------------
# reviewed payload digest
# ---------------------------------------------------------------------------

def payload_digest(files: dict) -> str:
    """A canonical digest over release payload content.

    ``files`` maps a repository-relative path to its exact bytes.

    Why this exists: Git ancestry can only be checked by someone holding the
    object database. An offline consumer verifying a signed release has the
    ZIP and nothing else. This digest is recomputable from either side -- from
    the working tree at authorization time, and from the signed archive at
    verification time -- so the owner can authorize *content* rather than a
    relationship only they can evaluate.

    The authorization closure is excluded, and that exclusion is what breaks
    the recursion: the policy records a digest of everything the policy does
    not affect, so filling the policy in cannot change the value it records.
    Ordering is by path, and each record is length-prefixed, so no combination
    of paths and digests can be re-partitioned into a different set with the
    same serialization.
    """
    h = hashlib.sha256()
    h.update(b"qta-reviewed-payload-v1\n")
    for path in sorted(files):
        if path in AUTHORIZATION_CLOSURE:
            continue
        body = files[path]
        entry = hashlib.sha256(body).hexdigest()
        line = f"{len(path)}:{path} {entry}\n".encode("utf-8")
        h.update(line)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# signed release binding
# ---------------------------------------------------------------------------

#: Name of the binding file placed inside the source ZIP. Because it is inside
#: the archive, the Sigstore signature over the archive authenticates it -- but
#: only AFTER that signature has been checked against an externally supplied
#: trusted policy. It never decides who to trust.
RELEASE_BINDING_NAME = "release_binding.json"

BINDING_REQUIRED_FIELDS = (
    "schema_version",
    "source_repository",
    "workflow_path",
    "authorized_ref",
    "release_revision",
    "reviewed_revision",
    "reviewed_payload_sha256",
    "stable_builder_id",
    "trusted_policy_sha256",
)
BINDING_ALLOWED_FIELDS = frozenset(BINDING_REQUIRED_FIELDS)


def validate_binding(binding) -> dict:
    """Strict shape validation for the signed release binding."""
    _require(isinstance(binding, dict), "release binding is not a JSON object")
    missing = [f for f in BINDING_REQUIRED_FIELDS if f not in binding]
    _require(not missing, f"release binding missing fields: {missing}")
    unknown = sorted(set(binding) - BINDING_ALLOWED_FIELDS)
    _require(not unknown, f"release binding has unknown fields: {unknown}")
    _require(binding["schema_version"] == SCHEMA_VERSION,
             f"release binding schema_version must be {SCHEMA_VERSION!r}")
    for f in ("release_revision", "reviewed_revision"):
        _require(_SHA40.match(str(binding[f])),
                 f"release binding {f} must be a 40-hex commit sha")
    for f in ("reviewed_payload_sha256", "trusted_policy_sha256"):
        _require(re.fullmatch(r"[0-9a-f]{64}", str(binding[f])),
                 f"release binding {f} must be a 64-hex sha256")
    _require(binding["release_revision"] != binding["reviewed_revision"],
             "release binding release_revision equals reviewed_revision; the "
             "released commit must be the descendant carrying the "
             "authorization record")
    return binding


def policy_digest(policy_bytes: bytes) -> str:
    """SHA-256 over the canonical policy bytes, for use as a trust anchor."""
    return hashlib.sha256(policy_bytes).hexdigest()


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    try:
        pol = load_canonical_policy()
    except PolicyError as e:
        print(f"[FAIL-CLOSED] {e}")
        sys.exit(1)
    print(f"canonical policy: {CANONICAL_POLICY_PATH}")
    print(f"  schema_version  : {pol['schema_version']}")
    print(f"  bootstrap_state : {pol['bootstrap_state']}")
    print(f"  resolved        : {is_resolved(pol)}")
    unres = unresolved_leaves(pol)
    print(f"  unresolved      : {len(unres)} {unres}")
    sys.exit(0)
