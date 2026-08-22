"""The release must not be able to answer "who should I trust?" itself.

The defect this pins down: `verify_release.py` compared the bundled policy
against a repository-local path, and when that path did not exist it **skipped
the comparison**. Run from a directory with no QTA checkout -- precisely the
independent-verification case -- there was no trust root at all, and a bundle
carrying a policy naming its own signer would have been evaluated against
itself.

Three separate things are kept apart here, and the tests are organised by them:

  authorization policy   pre-authorized owner intent, supplied EXTERNALLY
  authenticated artifact bytes whose signature verified against that policy
  auxiliary metadata     shipped alongside, never independently authenticated

No test performs real Fulcio cryptography. Tests that describe the expected
identity handed to Sigstore are named for that contract, not for an observed
certificate -- before `verify_artifact` runs there is no observed certificate.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_trust as RT   # noqa: E402

REPO = "https://github.com/cakeisalie89/Quantum-Thermal-"
WF = ".github/workflows/release.yml"
REF = "refs/tags/qta-stage11"


def _vr():
    spec = importlib.util.spec_from_file_location(
        "_vr_root", os.path.join(ROOT, "verify_release.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["_vr_root"] = m
    spec.loader.exec_module(m)
    return m


def authorized_policy(**over):
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


def binding(**over):
    b = {
        "schema_version": RT.SCHEMA_VERSION,
        "source_repository": REPO,
        "workflow_path": WF,
        "authorized_ref": REF,
        "release_revision": "c" * 40,
        "reviewed_revision": "a" * 40,
        "reviewed_payload_sha256": "b" * 64,
        "stable_builder_id": RT.derive_stable_builder_id(REPO, WF, REF),
        "trusted_policy_sha256": "d" * 64,
    }
    b.update(over)
    return b


# ---------------------------------------------------------------------------
# 1. There must be an external trust root, and --online must demand it.
# ---------------------------------------------------------------------------

def test_online_without_a_trust_root_fails_closed():
    vr = _vr()
    problems = []
    pol, canon = vr.load_trusted_policy(problems, None, None)
    assert pol is None and canon is None
    assert any("externally supplied trust root" in p for p in problems)


def test_the_error_names_the_self_authorization_problem():
    """The message must explain WHY, not just refuse."""
    vr = _vr()
    problems = []
    vr.load_trusted_policy(problems, None, None)
    joined = " ".join(problems)
    assert "cannot authorize itself" in joined
    assert "CANDIDATE" in joined


def test_an_unresolved_external_policy_is_refused(tmp_path):
    """The trust root itself must be authorized, not merely well-formed."""
    vr = _vr()
    p = tmp_path / "pol.json"
    p.write_bytes(RT.canonical_bytes(
        authorized_policy(pinned_revision="PENDING: later")))
    problems = []
    pol, _ = vr.load_trusted_policy(problems, str(p), None)
    assert pol is None
    assert any("not authorized for a signed release" in x for x in problems)


def test_a_missing_external_policy_is_refused(tmp_path):
    vr = _vr()
    problems = []
    pol, _ = vr.load_trusted_policy(problems, str(tmp_path / "nope"), None)
    assert pol is None
    assert any("not found" in x for x in problems)


def test_digest_only_trust_root_is_accepted(tmp_path):
    """--trusted-policy-sha256 alone is a valid root: it pins bytes."""
    vr = _vr()
    problems = []
    pol, canon = vr.load_trusted_policy(problems, None, "e" * 64)
    assert problems == []


def test_policy_and_digest_must_agree(tmp_path):
    vr = _vr()
    p = tmp_path / "pol.json"
    p.write_bytes(RT.canonical_bytes(authorized_policy()))
    problems = []
    vr.load_trusted_policy(problems, str(p), "0" * 64)
    assert any("digest" in x for x in problems)


# ---------------------------------------------------------------------------
# 2. Candidate policies must bind to the external root.
# ---------------------------------------------------------------------------

def test_bundle_policy_differing_from_trust_root_is_refused():
    vr = _vr()
    trusted = RT.canonical_bytes(authorized_policy())
    evil = RT.canonical_bytes(authorized_policy(
        signer_identity=RT.derive_signer_identity(
            "https://github.com/attacker/repo", WF, REF),
        source_repository="https://github.com/attacker/repo"))
    problems = []
    assert vr.bind_candidate_policy(
        problems, "bundle", evil, trusted, None) is False
    assert any("differs from the external trusted policy" in p
               for p in problems)


def test_signed_zip_policy_differing_from_trust_root_is_refused():
    vr = _vr()
    trusted = RT.canonical_bytes(authorized_policy())
    other = RT.canonical_bytes(authorized_policy(pinned_revision="f" * 40))
    problems = []
    assert vr.bind_candidate_policy(
        problems, "signed-zip", other, trusted, None) is False


def test_matching_candidate_policy_binds():
    vr = _vr()
    trusted = RT.canonical_bytes(authorized_policy())
    problems = []
    assert vr.bind_candidate_policy(
        problems, "bundle", trusted, trusted, None) is True
    assert problems == []


def test_candidate_can_bind_against_a_digest_root():
    vr = _vr()
    trusted = RT.canonical_bytes(authorized_policy())
    problems = []
    assert vr.bind_candidate_policy(
        problems, "bundle", trusted, None,
        RT.policy_digest(trusted)) is True


def test_no_root_means_no_binding():
    vr = _vr()
    problems = []
    assert vr.bind_candidate_policy(
        problems, "bundle", b"{}", None, None) is False
    assert any("no trust root" in p for p in problems)


# ---------------------------------------------------------------------------
# 3. The signed binding is compared to the policy, never consulted first.
# ---------------------------------------------------------------------------

def test_binding_matching_the_policy_passes():
    vr = _vr()
    problems = []
    vr.enforce_authenticated_binding(problems, authorized_policy(),
                                     binding(), True)
    assert problems == []


@pytest.mark.parametrize("field,bad", [
    ("source_repository", "https://github.com/attacker/repo"),
    ("workflow_path", ".github/workflows/other.yml"),
    ("authorized_ref", "refs/tags/other"),
    ("reviewed_payload_sha256", "9" * 64),
])
def test_binding_disagreeing_with_policy_fails(field, bad):
    vr = _vr()
    problems = []
    vr.enforce_authenticated_binding(
        problems, authorized_policy(), binding(**{field: bad}), True)
    assert problems, f"{field} mismatch not caught"


def test_binding_reviewed_revision_must_match_pinned():
    vr = _vr()
    problems = []
    vr.enforce_authenticated_binding(
        problems, authorized_policy(), binding(reviewed_revision="9" * 40),
        True)
    assert any("reviewed_revision" in p for p in problems)


def test_builder_is_derived_from_authenticated_content_not_taken_on_trust():
    """A binding cannot simply assert a builder id it likes."""
    vr = _vr()
    problems = []
    vr.enforce_authenticated_binding(
        problems, authorized_policy(),
        binding(stable_builder_id="github-actions://anything/i/like@x"), True)
    assert any("not the value derived" in p for p in problems)


def test_unbound_zip_policy_is_reported():
    vr = _vr()
    problems = []
    vr.enforce_authenticated_binding(problems, authorized_policy(),
                                     binding(), False)
    assert any("not bound to the external trust root" in p for p in problems)


# ---------------------------------------------------------------------------
# 4. Unsigned provenance is never authority.
# ---------------------------------------------------------------------------

def _prov(**over):
    ext = {"source_repository": REPO, "workflow_path": WF,
           "authorized_ref": REF}
    ext.update(over.pop("ext", {}))
    rev = over.pop("gitCommit", "c" * 40)
    return {"predicate": {"buildDefinition": {
        "externalParameters": ext,
        "resolvedDependencies": [{"uri": "git+" + REPO,
                                  "digest": {"gitCommit": rev}}]},
        "runDetails": {"builder": {"id": "anything at all"}}}}


def test_provenance_is_only_cross_checked():
    vr = _vr()
    problems = []
    vr.check_auxiliary_provenance(problems, authorized_policy(), binding(),
                                  _prov())
    assert problems == []


def test_provenance_disagreement_is_reported_as_inconsistency():
    vr = _vr()
    problems = []
    vr.check_auxiliary_provenance(
        problems, authorized_policy(), binding(),
        _prov(ext={"source_repository": "https://github.com/evil/repo"}))
    assert any("provenance disagrees" in p for p in problems)
    assert any("not authority" in p for p in problems)


def test_provenance_builder_is_not_consulted_for_authorization():
    """The builder in provenance is arbitrary above; it must not matter."""
    vr = _vr()
    problems = []
    vr.check_auxiliary_provenance(problems, authorized_policy(), binding(),
                                  _prov())
    assert problems == [], "provenance builder must not affect the outcome"


def test_verifier_does_not_read_builder_from_provenance():
    src = open(os.path.join(ROOT, "verify_release.py"),
               encoding="utf-8").read()
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    assert 'runDetails"]["builder"]' not in body, \
        "authorization must not read the builder out of unsigned provenance"


# ---------------------------------------------------------------------------
# 5. Certificate semantics, named honestly.
# ---------------------------------------------------------------------------

def test_policy_to_sigstore_expected_identity_contract():
    """The expected identity handed to Sigstore comes from the TRUSTED policy.

    Named for what it is. It is not an observed certificate check -- the
    certificate is checked by Verifier.verify_artifact against exactly these
    values, and there is no observed SAN before that call.
    """
    src = open(os.path.join(ROOT, "verify_release.py"),
               encoding="utf-8").read()
    assert 'identity = str(trusted_pol["signer_identity"])' in src
    assert 'issuer = str(trusted_pol["oidc_issuer"])' in src
    assert "Identity(identity=identity, issuer=issuer)" in src


def _executable_source(path):
    """Source with docstrings and comments stripped.

    The prose legitimately explains the removed defect and must quote it; only
    executable code is scanned, so describing a prohibition cannot trip it and
    moving a real violation into a docstring cannot hide it.
    """
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        b = node.body
        if (b and isinstance(b[0], ast.Expr)
                and isinstance(b[0].value, ast.Constant)
                and isinstance(b[0].value.value, str)):
            node.body = b[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_no_policy_value_is_labelled_a_certificate_observation():
    """The removed defect: policy strings reported under a "certificate"
    heading as though independently observed."""
    body = _executable_source(os.path.join(ROOT, "verify_release.py"))
    assert '"certificate"' not in body and "'certificate'" not in body, \
        "a policy-derived value must not be labelled certificate evidence"
    assert "agrees three ways" not in body, \
        "the three-way certificate claim was not actually three-way"


# ---------------------------------------------------------------------------
# 6. Signature subject routing is exact.
# ---------------------------------------------------------------------------

def test_signature_subject_must_be_the_exact_zip_name():
    src = open(os.path.join(ROOT, "verify_release.py"),
               encoding="utf-8").read()
    assert "if name != zip_path.name:" in src
    assert "alternate subject names are" in src


# ---------------------------------------------------------------------------
# 7. Payload digest.
# ---------------------------------------------------------------------------

def test_payload_digest_excludes_the_authorization_closure():
    """This exclusion is what breaks the recursion."""
    base = {"a.py": b"x", "b.py": b"y"}
    with_closure = dict(base)
    for p in RT.AUTHORIZATION_CLOSURE:
        with_closure[p] = b"anything at all"
    assert RT.payload_digest(base) == RT.payload_digest(with_closure)


def test_payload_digest_detects_any_payload_change():
    base = {"a.py": b"x", "b.py": b"y"}
    assert RT.payload_digest(base) != RT.payload_digest(
        {"a.py": b"x", "b.py": b"CHANGED"})
    assert RT.payload_digest(base) != RT.payload_digest({"a.py": b"x"})
    assert RT.payload_digest(base) != RT.payload_digest(
        {"a.py": b"x", "b.py": b"y", "c.py": b"z"})


def test_payload_digest_is_order_independent():
    a = {"a.py": b"1", "b.py": b"2"}
    b = {"b.py": b"2", "a.py": b"1"}
    assert RT.payload_digest(a) == RT.payload_digest(b)


def test_payload_digest_cannot_be_repartitioned():
    """Length-prefixed records: no path/digest pair can be re-split."""
    assert RT.payload_digest({"ab": b"x", "c": b"y"}) != \
        RT.payload_digest({"a": b"x", "bc": b"y"})


# ---------------------------------------------------------------------------
# 8. The independent-consumer threat model, end to end.
# ---------------------------------------------------------------------------

def _bundle(tmp_path):
    zp = tmp_path / "QTA_source.zip"
    bundle = tmp_path / "bundle"
    subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
         "--make-zip", "--out", str(bundle)],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=600)
    return zp, bundle


def _run(zp, bundle, cwd, *extra):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "verify_release.py"),
         "--zip", str(zp), "--bundle", str(bundle), *extra],
        cwd=cwd, capture_output=True, text=True, timeout=600)


def test_consumer_with_no_checkout_online_refuses(tmp_path):
    """No repository, no canonical policy, only zip + bundle."""
    zp, bundle = _bundle(tmp_path)
    work = tmp_path / "elsewhere"
    work.mkdir()
    r = _run(zp, bundle, work, "--online")
    assert r.returncode == 1
    assert "externally supplied trust root" in r.stdout


def test_consumer_with_no_checkout_offline_says_it_is_unanchored(tmp_path):
    """Offline is structural only, and must say so rather than imply trust."""
    zp, bundle = _bundle(tmp_path)
    work = tmp_path / "elsewhere2"
    work.mkdir()
    r = _run(zp, bundle, work)
    assert "UNANCHORED" in r.stdout
    assert "NOT a trust root" in r.stdout or "UNANCHORED" in r.stdout


def test_attacker_modified_bundle_policy_cannot_pass_online(tmp_path):
    """A malicious policy naming the attacker's signer must not self-authorize."""
    zp, bundle = _bundle(tmp_path)
    evil = authorized_policy(
        source_repository="https://github.com/attacker/repo",
        signer_identity=RT.derive_signer_identity(
            "https://github.com/attacker/repo", WF, REF),
        trusted_builders=[RT.derive_stable_builder_id(
            "https://github.com/attacker/repo", WF, REF)])
    (bundle / "release_trust_policy.json").write_bytes(
        RT.canonical_bytes(evil))
    work = tmp_path / "elsewhere3"
    work.mkdir()
    # Even with the attacker's own policy present, no EXTERNAL root is given.
    r = _run(zp, bundle, work, "--online")
    assert r.returncode == 1
    assert "externally supplied trust root" in r.stdout


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
