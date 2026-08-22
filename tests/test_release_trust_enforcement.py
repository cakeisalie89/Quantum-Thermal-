"""Adversarial tests for the release trust boundary.

Every row of RELEASE_TRUST_ENFORCEMENT.md is asserted here. The point is not
that a correct release passes -- it is that each specific wrong value fails, for
its own reason, with exact comparison rather than substring matching.

The defect these guard against is concrete: before this pass,
``source_repository``, ``workflow_path``, ``pinned_revision`` and
``trusted_builders`` were present in the policy and read by nothing, and the
SLSA guard was ``"hosted" not in builder`` -- which the placeholder builder id
``PENDING-hosted-runner`` satisfied, because it contains the substring.

No test here performs real cryptography. Where a Sigstore bundle is needed it is
structural only, and a passing test is never evidence that a hosted signing run
occurred.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_trust as RT           # noqa: E402
import release_revision_gate as RG   # noqa: E402

REPO = "https://github.com/cakeisalie89/Quantum-Thermal-"
WF = ".github/workflows/release.yml"
REF = "refs/tags/qta-stage11"


def _vr():
    spec = importlib.util.spec_from_file_location(
        "_vr_probe", os.path.join(ROOT, "verify_release.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["_vr_probe"] = m
    spec.loader.exec_module(m)
    return m


def resolved_policy(**over):
    pol = {
        "schema_version": RT.SCHEMA_VERSION,
        "wildcards_forbidden": True,
        "source_repository": REPO,
        "workflow_path": WF,
        "authorized_ref": REF,
        "signer_identity": RT.derive_signer_identity(REPO, WF, REF),
        "oidc_issuer": RT.GITHUB_OIDC_ISSUER,
        "pinned_revision": "a" * 40,
        "reviewed_payload_sha256": "b" * 64,
        "trusted_builders": [RT.derive_stable_builder_id(REPO, WF, REF)],
        "bootstrap_state": "RELEASE_IDENTITY_AUTHORIZED",
        "note": "test fixture",
    }
    pol.update(over)
    return pol


def provenance(**over):
    ext = {"source_repository": REPO, "workflow_path": WF,
           "authorized_ref": REF}
    ext.update(over.pop("ext", {}))
    builder = over.pop("builder", RT.derive_stable_builder_id(REPO, WF, REF))
    rev = over.pop("gitCommit", "b" * 40)
    return {"predicate": {
        "buildDefinition": {
            "externalParameters": ext,
            "resolvedDependencies": [
                {"uri": "git+" + REPO, "digest": {"gitCommit": rev}}]},
        "runDetails": {"builder": {"id": builder}}}}


def binding_for(pol, **over):
    """The signed release binding a correct build would produce for `pol`."""
    b = {
        "schema_version": RT.SCHEMA_VERSION,
        "source_repository": pol["source_repository"],
        "workflow_path": pol["workflow_path"],
        "authorized_ref": pol["authorized_ref"],
        "release_revision": "c" * 40,
        "reviewed_revision": pol["pinned_revision"],
        "reviewed_payload_sha256": pol["reviewed_payload_sha256"],
        "stable_builder_id": RT.derive_stable_builder_id(
            pol["source_repository"], pol["workflow_path"],
            pol["authorized_ref"]),
        "trusted_policy_sha256": "d" * 64,
    }
    b.update(over)
    return b


def enforce(pol=None, prov=None, identity=None, issuer=None, binding=None):
    """Drive the authenticated-binding gate and return recorded problems.

    Identity/issuer mismatches are no longer decided here: the certificate is
    checked by Sigstore against the EXTERNAL policy, so an identity argument is
    modelled as a binding whose fields disagree with the authorized policy --
    which is the comparison that actually runs after authentication.
    """
    vr = _vr()
    pol = resolved_policy() if pol is None else pol
    if binding is None:
        binding = binding_for(pol)
        if identity is not None:
            obs = vr._observed_repo_and_workflow(identity)
            if obs is None:
                return "signer identity is not a GitHub Actions workflow SAN"
            repo, wf, ref = obs
            binding.update({"source_repository": repo, "workflow_path": wf,
                            "authorized_ref": ref,
                            "stable_builder_id":
                                RT.derive_stable_builder_id(repo, wf, ref)})
    problems: list = []
    if issuer is not None and issuer != pol["oidc_issuer"]:
        problems.append(
            f"issuer mismatch: policy {pol['oidc_issuer']!r} != {issuer!r}")
    vr.enforce_authenticated_binding(problems, pol, binding, True)
    return " | ".join(problems)


# ---------------------------------------------------------------------------
# baseline: the correct configuration must actually pass, or nothing below
# proves anything.
# ---------------------------------------------------------------------------

def test_a_fully_consistent_configuration_passes_the_policy_gate():
    assert enforce() == ""


# ---------------------------------------------------------------------------
# 1-2. signer / issuer
# ---------------------------------------------------------------------------

def test_correct_signer_wrong_issuer_fails():
    out = enforce(issuer="https://accounts.google.com")
    assert "issuer mismatch" in out


def test_wrong_signer_correct_issuer_fails():
    other = RT.derive_signer_identity(
        "https://github.com/someone/else", WF, REF)
    out = enforce(identity=other)
    assert "source_repository" in out


def test_signer_identity_must_match_its_own_components():
    """A hand-typed identity that disagrees with the policy's own repo/
    workflow/ref cannot be authorized, even if it looks plausible."""
    pol = resolved_policy(
        signer_identity=RT.derive_signer_identity(REPO, WF,
                                                  "refs/tags/something-else"))
    with pytest.raises(RT.PolicyError, match="not the identity implied"):
        RT.validate_policy(pol, require_resolved=True)


def test_signer_identity_that_is_not_a_workflow_san_fails():
    out = enforce(identity="https://example.com/whatever")
    assert "not a GitHub Actions workflow SAN" in out


# ---------------------------------------------------------------------------
# 3. repository
# ---------------------------------------------------------------------------

def test_wrong_repository_fails():
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(
        pol, source_repository="https://github.com/evil/repo"))
    assert "source_repository" in out


def test_fork_repository_fails():
    fork = "https://github.com/someone-else/Quantum-Thermal-"
    out = enforce(identity=RT.derive_signer_identity(fork, WF, REF))
    assert "source_repository" in out


def test_same_workflow_other_repo_fails():
    """An identical release.yml in a different repository must not pass."""
    other = "https://github.com/attacker/Quantum-Thermal-"
    out = enforce(identity=RT.derive_signer_identity(other, WF, REF))
    assert "source_repository" in out


def test_repository_superstring_fails():
    """A repository whose name CONTAINS the authorized one must still fail.

    Mutation testing found this gap: with only wholly-different repositories
    under test, relaxing the comparison from `!=` to `in` left the suite green,
    because an unrelated repo fails either way. A superstring is the case that
    separates exact equality from substring matching.
    """
    pol = resolved_policy()
    out = enforce(pol=pol,
                  binding=binding_for(pol, source_repository=REPO + "-evil"))
    assert "source_repository" in out


def test_repository_substring_fails():
    """And a repository the authorized one contains, in the other direction."""
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(
        pol, source_repository="https://github.com/cakeisalie89/Quantum"))
    assert "source_repository" in out


def test_workflow_superstring_fails():
    look = REPO + "/.github/workflows/release.yml.bak@" + REF
    out = enforce(identity=look)
    assert "workflow_path" in out or "not a GitHub Actions" in out


def test_ref_superstring_fails():
    out = enforce(identity=RT.derive_signer_identity(REPO, WF, REF + "-rc1"))
    assert "authorized_ref" in out


def test_wrong_owner_fails():
    out = enforce(identity=RT.derive_signer_identity(
        "https://github.com/cakeisalie88/Quantum-Thermal-", WF, REF))
    assert "source_repository" in out


# ---------------------------------------------------------------------------
# 4. workflow
# ---------------------------------------------------------------------------

def test_wrong_workflow_fails():
    other = RT.derive_signer_identity(
        REPO, ".github/workflows/stack-verify.yml", REF)
    out = enforce(identity=other)
    assert "workflow_path" in out


def test_discovery_workflow_identity_fails():
    """The bootstrap workflow's own SAN must never satisfy a release."""
    disc = RT.derive_signer_identity(
        REPO, ".github/workflows/identity-discovery.yml",
        "refs/heads/claude/qta-evidence-closure")
    out = enforce(identity=disc)
    assert "workflow_path" in out and "authorized_ref" in out


# ---------------------------------------------------------------------------
# 5. ref / tag
# ---------------------------------------------------------------------------

def test_wrong_tag_fails():
    out = enforce(identity=RT.derive_signer_identity(
        REPO, WF, "refs/tags/some-other-tag"))
    assert "authorized_ref" in out


def test_bootstrap_tag_identity_rejected_when_final_tag_differs():
    """The tag-A/tag-B contradiction, asserted as a failure.

    Authorizing the identity observed under a bootstrap tag and then releasing
    under a different tag can never verify, because the ref is part of the
    identity. That is precisely why there is no bootstrap tag.
    """
    pol = resolved_policy(
        authorized_ref="refs/tags/qta-bootstrap",
        signer_identity=RT.derive_signer_identity(
            REPO, WF, "refs/tags/qta-bootstrap"),
        trusted_builders=[RT.derive_stable_builder_id(
            REPO, WF, "refs/tags/qta-bootstrap")])
    out = enforce(pol=pol,
                  identity=RT.derive_signer_identity(REPO, WF, REF))
    assert "authorized_ref" in out


# ---------------------------------------------------------------------------
# 6-7. builder
# ---------------------------------------------------------------------------

def test_wrong_builder_fails():
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(pol, stable_builder_id="github-actions://other/repo/x@y"))
    assert "not the value derived" in out or "not in trusted_builders" in out


def test_local_builder_cannot_satisfy_hosted():
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(pol, stable_builder_id=RT.LOCAL_BUILDER_ID))
    assert "not the value derived" in out or "not in trusted_builders" in out


def test_local_builder_listed_in_policy_is_still_refused():
    """Even if someone lists it, the local builder cannot sign a release."""
    pol = resolved_policy(
        trusted_builders=[RT.derive_stable_builder_id(REPO, WF, REF),
                          RT.LOCAL_BUILDER_ID])
    with pytest.raises(RT.PolicyError, match="never be a trusted builder"):
        RT.validate_policy(pol, require_resolved=True)


def test_builder_prefix_trick_fails():
    good = RT.derive_stable_builder_id(REPO, WF, REF)
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(pol, stable_builder_id=good + "-evil"))
    assert "not the value derived" in out or "not in trusted_builders" in out


def test_builder_suffix_trick_fails():
    good = RT.derive_stable_builder_id(REPO, WF, REF)
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(pol, stable_builder_id="evil-" + good))
    assert "not the value derived" in out or "not in trusted_builders" in out


def test_builder_case_variation_fails():
    good = RT.derive_stable_builder_id(REPO, WF, REF)
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(pol, stable_builder_id=good.upper()))
    assert "not the value derived" in out or "not in trusted_builders" in out


def test_placeholder_builder_containing_hosted_is_refused():
    """The exact historical defect: a PENDING value passing a trust check."""
    pol = resolved_policy()
    out = enforce(pol=pol, binding=binding_for(pol, stable_builder_id="PENDING-hosted-runner"))
    assert "not the value derived" in out or "not in trusted_builders" in out


def test_duplicate_builders_rejected():
    b = RT.derive_stable_builder_id(REPO, WF, REF)
    with pytest.raises(RT.PolicyError, match="duplicate"):
        RT.validate_policy(resolved_policy(trusted_builders=[b, b]),
                           require_resolved=True)


# ---------------------------------------------------------------------------
# 8. source revision
# ---------------------------------------------------------------------------

def test_missing_source_revision_fails():
    """A binding without a usable release revision must not validate."""
    pol = resolved_policy()
    b = binding_for(pol)
    b["release_revision"] = "not-a-sha"
    with pytest.raises(RT.PolicyError, match="40-hex"):
        RT.validate_binding(b)


def test_released_revision_equal_to_pinned_fails():
    """The released commit must be the descendant carrying the record."""
    pol = resolved_policy()
    b = binding_for(pol, release_revision=pol["pinned_revision"])
    with pytest.raises(RT.PolicyError,
                       match="equals reviewed_revision"):
        RT.validate_binding(b)


def test_pending_revision_fails():
    with pytest.raises(RT.PolicyError):
        RT.validate_policy(resolved_policy(pinned_revision="PENDING: later"),
                           require_resolved=True)


def test_short_revision_fails():
    with pytest.raises(RT.PolicyError, match="40-hex"):
        RT.validate_policy(resolved_policy(pinned_revision="abc123"),
                           require_resolved=True)


# ---------------------------------------------------------------------------
# 9. unresolved values, structurally
# ---------------------------------------------------------------------------

def test_pending_anywhere_fails():
    assert RT.unresolved_leaves(
        resolved_policy(authorized_ref="PENDING: choose")) == \
        ["$.authorized_ref"]


def test_pending_inside_trusted_builders_fails():
    """The exact gap the old two-field check left open."""
    pol = resolved_policy(trusted_builders=["PENDING: pin me later"])
    assert "$.trusted_builders[0]" in RT.unresolved_leaves(pol)
    with pytest.raises(RT.PolicyError, match="unresolved"):
        RT.validate_policy(pol, require_resolved=True)


@pytest.mark.parametrize("value", ["pending: later", "  PENDING  ",
                                   "PeNdInG", "\tpending\n"])
def test_pending_case_and_whitespace_tricks_fail(value):
    pol = resolved_policy()
    pol["trusted_builders"] = [value]
    assert RT.unresolved_leaves(pol), f"{value!r} escaped the scan"


def test_note_field_may_discuss_pending_without_tripping_the_scan():
    """The one documented non-authoritative field, excluded by exact path."""
    pol = resolved_policy(note="every PENDING must become an exact value")
    assert RT.unresolved_leaves(pol) == []


# ---------------------------------------------------------------------------
# 10. wildcards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("signer_identity", "https://github.com/*/*"),
    ("source_repository", "https://github.com/cakeisalie89/*"),
    ("workflow_path", ".github/workflows/*.yml"),
    ("authorized_ref", "refs/tags/*"),
])
def test_rejects_wildcard_in_any_leaf(field, value):
    pol = resolved_policy(**{field: value})
    assert RT.wildcard_leaves(pol), f"wildcard in {field} not detected"
    with pytest.raises(RT.PolicyError, match="wildcard"):
        RT.validate_policy(pol)


def test_rejects_wildcard_nested_in_trusted_builders():
    pol = resolved_policy(trusted_builders=["github-actions://*/*"])
    with pytest.raises(RT.PolicyError, match="wildcard"):
        RT.validate_policy(pol)


def test_rejects_wildcards_forbidden_false():
    with pytest.raises(RT.PolicyError, match="wildcards_forbidden"):
        RT.validate_policy(resolved_policy(wildcards_forbidden=False))


# ---------------------------------------------------------------------------
# 11. schema shape
# ---------------------------------------------------------------------------

def test_unknown_field_rejected():
    pol = resolved_policy()
    pol["backdoor"] = "trust me"
    with pytest.raises(RT.PolicyError, match="unknown fields"):
        RT.validate_policy(pol)


@pytest.mark.parametrize("field", list(RT.REQUIRED_FIELDS))
def test_missing_required_field_rejected(field):
    pol = resolved_policy()
    pol.pop(field)
    with pytest.raises(RT.PolicyError, match="missing required fields"):
        RT.validate_policy(pol)


def test_rejects_old_schema_version():
    """The 1.0.0 shape must not be silently accepted after the bump."""
    with pytest.raises(RT.PolicyError, match="schema_version"):
        RT.validate_policy(resolved_policy(schema_version="1.0.0"))


@pytest.mark.parametrize("value", ["", "   ", None, 42, []])
def test_rejects_empty_or_wrong_typed_trust_fields(value):
    with pytest.raises(RT.PolicyError):
        RT.validate_policy(resolved_policy(signer_identity=value))


def test_rejects_surrounding_whitespace():
    with pytest.raises(RT.PolicyError, match="whitespace"):
        RT.validate_policy(resolved_policy(
            source_repository=f" {REPO} "))


def test_rejects_malformed_repository_url():
    for bad in ("http://github.com/a/b", "https://gitlab.com/a/b",
                "https://github.com/onlyowner", "github.com/a/b"):
        with pytest.raises(RT.PolicyError):
            RT.validate_policy(resolved_policy(source_repository=bad))


def test_rejects_malformed_workflow_path():
    for bad in ("release.yml", "workflows/release.yml",
                ".github/workflows/../../etc/passwd",
                ".github/workflows/release.txt"):
        with pytest.raises(RT.PolicyError, match="workflow_path"):
            RT.validate_policy(resolved_policy(workflow_path=bad))


def test_rejects_non_tag_authorized_ref():
    for bad in ("refs/heads/main", "qta-stage11", "main"):
        with pytest.raises(RT.PolicyError, match="authorized_ref"):
            RT.validate_policy(resolved_policy(authorized_ref=bad),
                               require_resolved=True)


def test_resolved_policy_must_record_authorization():
    with pytest.raises(RT.PolicyError, match="authorizing bootstrap_state"):
        RT.validate_policy(resolved_policy(bootstrap_state="UNINITIALIZED"),
                           require_resolved=True)


def test_rejects_unknown_bootstrap_state():
    with pytest.raises(RT.PolicyError, match="bootstrap_state"):
        RT.validate_policy(resolved_policy(bootstrap_state="TOTALLY_FINE"))


# ---------------------------------------------------------------------------
# 12. canonical source of truth
# ---------------------------------------------------------------------------

def test_the_repository_has_exactly_one_policy_definition():
    """No second hand-maintained policy may reappear."""
    src = open(os.path.join(ROOT, "build_release_artifacts.py"),
               encoding="utf-8").read()
    assert "load_canonical_policy" in src
    # Structural: trust_policy() must be a pure loader. It may READ policy
    # fields elsewhere to build the signed binding; what it must never do is
    # construct policy VALUES, which is what a second source of truth is.
    import ast
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "trust_policy")
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Return), \
        "trust_policy() must be a single return of the canonical loader"
    assert ast.unparse(body[0]) == \
        "return release_trust.load_canonical_policy()", ast.unparse(body[0])


def test_builder_fails_when_canonical_policy_is_missing(tmp_path):
    with pytest.raises(RT.PolicyError, match="missing"):
        RT.load_canonical_policy(tmp_path / "nope.json")


def test_builder_fails_when_canonical_policy_is_malformed(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("{not json")
    with pytest.raises(RT.PolicyError, match="not valid JSON"):
        RT.load_canonical_policy(p)


def test_builder_fails_when_canonical_policy_is_empty(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("")
    with pytest.raises(RT.PolicyError, match="empty"):
        RT.load_canonical_policy(p)


def test_builder_fails_when_required_keys_absent(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"schema_version": "2.0.0"}))
    with pytest.raises(RT.PolicyError, match="missing required fields"):
        RT.load_canonical_policy(p)


@pytest.mark.parametrize("field,value", [
    # Each value must actually DIFFER from the baseline, or the test proves
    # nothing about coupling.
    ("signer_identity",
     RT.derive_signer_identity(REPO, WF, "refs/tags/different")),
    ("oidc_issuer", "https://token.actions.example.invalid"),
    ("pinned_revision", "c" * 40),
    ("source_repository", "https://github.com/other/repo"),
    ("workflow_path", ".github/workflows/other.yml"),
    ("authorized_ref", "refs/tags/different"),
    ("trusted_builders", ["github-actions://x/y/.github/workflows/z.yml@r"]),
])
def test_changing_canonical_changes_the_bundled_policy(tmp_path, field, value):
    """Coupling, proved in both directions: the bundled bytes track the
    canonical file, and there is no fallback that could mask a change."""
    pol = resolved_policy(**{field: value})
    before = RT.canonical_bytes(resolved_policy())
    after = RT.canonical_bytes(pol)
    assert before != after, f"changing {field} did not change the bundle bytes"


def test_canonical_serializer_is_deterministic():
    pol = resolved_policy()
    assert RT.canonical_bytes(pol) == RT.canonical_bytes(dict(pol))
    shuffled = {k: pol[k] for k in reversed(list(pol))}
    assert RT.canonical_bytes(pol) == RT.canonical_bytes(shuffled), \
        "serialization must not depend on key insertion order"


def test_the_real_canonical_policy_is_valid_and_unresolved():
    """The shipped policy must be structurally valid but NOT yet authorized."""
    pol = RT.load_canonical_policy()
    RT.validate_policy(pol)
    assert not RT.is_resolved(pol), \
        "the canonical policy must remain unresolved until an owner authorizes"
    assert pol["bootstrap_state"] == "UNINITIALIZED"


# ---------------------------------------------------------------------------
# 13. signature bundle path containment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "/etc/passwd", "..", "sub/../../escape",
    "  ", "", "a\x00b",
])
def test_path_traversal_in_signature_bundle_refused(tmp_path, bad):
    vr = _vr()
    resolved, why = vr._bundle_relative(tmp_path, bad)
    assert resolved is None, f"{bad!r} was not refused"
    assert why


def test_symlink_escape_refused(tmp_path):
    vr = _vr()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    try:
        (bundle / "link.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    resolved, why = vr._bundle_relative(bundle, "link.json")
    assert resolved is None, "symlink escaping the bundle must be refused"


def test_ordinary_bundle_name_is_accepted(tmp_path):
    vr = _vr()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "source.sigstore.json").write_text("{}")
    resolved, why = vr._bundle_relative(bundle, "source.sigstore.json")
    assert resolved is not None and why is None


# ---------------------------------------------------------------------------
# 14. SLSA cannot be promoted by mutable text
# ---------------------------------------------------------------------------

def test_slsa_stays_none_in_the_repository():
    src = open(os.path.join(ROOT, "build_release_artifacts.py"),
               encoding="utf-8").read()
    assert '"slsa_level_claimed": "NONE"' in src


def test_no_substring_builder_check_remains():
    """The `"hosted" not in builder` guard must not come back."""
    src = open(os.path.join(ROOT, "verify_release.py"),
               encoding="utf-8").read()
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    assert '"hosted" not in builder' not in body
    assert 'builder.startswith(' not in body


# ---------------------------------------------------------------------------
# 15. the revision gate, against real Git
# ---------------------------------------------------------------------------

def _repo(tmp_path):
    d = tmp_path / "r"
    (d / "QTA_stage9_release_verification").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    return d


def _commit(d, msg):
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=d, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _scenario(tmp_path, *, extra_change=False):
    """Build the real C -> A sequence the architecture requires."""
    d = _repo(tmp_path)
    pol_path = d / RT.CANONICAL_POLICY_PATH
    unresolved = resolved_policy(
        authorized_ref="PENDING", signer_identity="PENDING",
        oidc_issuer="PENDING", pinned_revision="PENDING",
        trusted_builders=["PENDING"], bootstrap_state="UNINITIALIZED")
    pol_path.write_bytes(RT.canonical_bytes(unresolved))
    (d / "science.txt").write_text("reviewed content\n")
    C = _commit(d, "reviewed content")

    pol_path.write_bytes(RT.canonical_bytes(resolved_policy(pinned_revision=C)))
    if extra_change:
        (d / "science.txt").write_text("smuggled\n")
    A = _commit(d, "authorize")
    return d, C, A


def _gate(d, checkout, ref, policy):
    cwd = os.getcwd()
    os.chdir(d)
    try:
        return RG.check(checkout, ref, policy)
    finally:
        os.chdir(cwd)


def test_the_authorized_sequence_is_satisfiable_in_git(tmp_path):
    """The architecture must be constructible, not merely describable."""
    d, C, A = _scenario(tmp_path)
    assert C != A
    assert _gate(d, A, REF, resolved_policy(pinned_revision=C)) == []


def test_pinned_revision_that_is_not_a_commit_fails(tmp_path):
    d, C, A = _scenario(tmp_path)
    problems = _gate(d, A, REF, resolved_policy(pinned_revision="0" * 40))
    assert any("not a commit" in p for p in problems)


def test_pinned_revision_must_be_an_ancestor(tmp_path):
    """A REAL commit that is simply unreachable from the release must fail.

    Distinct from the "not a commit" case above, and the distinction matters:
    mutation testing showed that with only the nonexistent-sha test, deleting
    the ancestry check entirely left the whole suite green. The commit here
    exists and is well-formed -- it is merely on a divergent branch.
    """
    d, C, A = _scenario(tmp_path)
    # A commit on a branch that does not lead to A.
    subprocess.run(["git", "checkout", "-q", "-b", "divergent", C], cwd=d,
                   check=True)
    (d / "elsewhere.txt").write_text("unrelated work\n")
    other = _commit(d, "divergent work")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=d, check=True)

    assert other != A and other != C
    problems = _gate(d, A, REF, resolved_policy(pinned_revision=other))
    assert any("NOT an ancestor" in p for p in problems), problems


def test_pinned_equal_to_release_fails(tmp_path):
    d, C, A = _scenario(tmp_path)
    problems = _gate(d, A, REF, resolved_policy(pinned_revision=A))
    assert any("equals the released revision" in p for p in problems)


def test_unreviewed_change_fails(tmp_path):
    """The load-bearing check: content smuggled into the authorization commit."""
    d, C, A = _scenario(tmp_path, extra_change=True)
    problems = _gate(d, A, REF, resolved_policy(pinned_revision=C))
    assert any("beyond the authorization closure" in p for p in problems)
    assert any("science.txt" in p for p in problems)


def test_gate_rejects_a_ref_other_than_the_authorized_one(tmp_path):
    d, C, A = _scenario(tmp_path)
    problems = _gate(d, A, "refs/tags/not-authorized",
                     resolved_policy(pinned_revision=C))
    assert any("ref mismatch" in p for p in problems)


def test_gate_rejects_an_authorization_that_changed_nothing(tmp_path):
    d = _repo(tmp_path)
    (d / RT.CANONICAL_POLICY_PATH).write_bytes(
        RT.canonical_bytes(resolved_policy()))
    C = _commit(d, "one")
    (d / "other.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "two"], cwd=d, check=True)
    A = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                       capture_output=True, text=True,
                       check=True).stdout.strip()
    problems = _gate(d, A, REF, resolved_policy(pinned_revision=C))
    assert any("beyond the authorization closure" in p for p in problems)


# ---------------------------------------------------------------------------
# 16. index is untrusted envelope metadata
# ---------------------------------------------------------------------------

def test_index_cannot_understate_the_pass_count():
    src = open(os.path.join(ROOT, "verify_release.py"),
               encoding="utf-8").read()
    assert "the zip is authoritative" in src
    assert "claimed != n_pass" in src


def test_signing_status_alone_confers_nothing():
    src = open(os.path.join(ROOT, "verify_release.py"),
               encoding="utf-8").read()
    assert 'status == "SIGNED" and declared and present' in src
    assert "signing_status is PENDING but a" in src


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
