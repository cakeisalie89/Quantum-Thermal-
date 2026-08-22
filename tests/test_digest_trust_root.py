"""`--trusted-policy-sha256` must actually work end to end.

The defect this pins down: `load_trusted_policy` returned `(None, None)` for a
digest-only root and recorded no problem, so it *looked* valid. `_verify_sigstore`
then did `trusted_pol["signer_identity"]` on that `None` and digest-only online
verification could never work at all.

The old test only called the loader and asserted `problems == []`. It passed
against a broken architecture. Everything here therefore drives the control flow
far enough to prove a real policy object reaches Sigstore, and the mutation
suite specifically restores `(None, None)` to confirm that is now caught.

The Sigstore boundary is mocked. A passing test here is evidence about control
flow and expected-identity plumbing, never that real cryptography ran.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_trust as RT   # noqa: E402

REPO = "https://github.com/cakeisalie89/Quantum-Thermal-"
WF = ".github/workflows/release.yml"
REF = "refs/tags/qta-stage11"


def _vr(name="_vr_digest"):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "verify_release.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
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


def bundle_with(tmp_path, policy_bytes=None, name="bundle"):
    b = tmp_path / name
    b.mkdir(parents=True, exist_ok=True)
    if policy_bytes is not None:
        (b / "release_trust_policy.json").write_bytes(policy_bytes)
    return b


# ---------------------------------------------------------------------------
# 1. The end-to-end contract the old test failed to cover.
# ---------------------------------------------------------------------------

def test_digest_only_root_drives_online_identity_verification(tmp_path,
                                                              monkeypatch):
    """A digest root must supply the identity Sigstore is given.

    Drives the real control flow: loader -> root object -> _verify_sigstore,
    with only the Sigstore cryptographic boundary replaced. Asserts the
    expected identity and issuer arriving at `Identity(...)` came from the
    digest-authenticated policy, and that no NoneType path exists.
    """
    vr = _vr()
    pol = authorized_policy()
    canon = RT.canonical_bytes(pol)
    digest = RT.policy_digest(canon)
    b = bundle_with(tmp_path, canon)

    problems = []
    root = vr.load_trusted_policy(problems, None, digest, b)
    assert problems == [], problems
    assert root is not None, "digest-only root must produce a policy object"
    assert root.source == "digest"
    assert root.policy["signer_identity"] == pol["signer_identity"]
    assert root.policy["oidc_issuer"] == pol["oidc_issuer"]

    # --- mock only the cryptographic boundary -----------------------------
    seen = {}

    class _Identity:
        def __init__(self, identity, issuer):
            seen["identity"] = identity
            seen["issuer"] = issuer

    class _Verifier:
        @staticmethod
        def production():
            return _Verifier()

        def verify_artifact(self, data, bundle, policy):
            seen["verified"] = True

    class _Bundle:
        @staticmethod
        def from_json(raw):
            return object()

    monkeypatch.setitem(sys.modules, "sigstore", types.ModuleType("sigstore"))
    vmod = types.ModuleType("sigstore.verify")
    vmod.Verifier = _Verifier
    pmod = types.ModuleType("sigstore.verify.policy")
    pmod.Identity = _Identity
    mmod = types.ModuleType("sigstore.models")
    mmod.Bundle = _Bundle
    monkeypatch.setitem(sys.modules, "sigstore.verify", vmod)
    monkeypatch.setitem(sys.modules, "sigstore.verify.policy", pmod)
    monkeypatch.setitem(sys.modules, "sigstore.models", mmod)

    zp = tmp_path / "QTA_source.zip"
    zp.write_bytes(b"payload")
    (b / "source.sigstore.json").write_text("{}")

    sig_problems = []
    vr._verify_sigstore(
        sig_problems, zp, b, {},
        [{"name": "QTA_source.zip", "bundle": "source.sigstore.json"}],
        root.policy)

    assert seen.get("verified") is True, "Sigstore boundary was never reached"
    assert seen["identity"] == pol["signer_identity"], \
        "expected identity did not come from the digest-authenticated policy"
    assert seen["issuer"] == pol["oidc_issuer"]
    assert sig_problems == [], sig_problems


def test_digest_only_root_has_no_nonetype_path(tmp_path):
    """The exact shape of the old bug: a 'valid' root that is None."""
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    b = bundle_with(tmp_path, canon)
    problems = []
    root = vr.load_trusted_policy(problems, None, RT.policy_digest(canon), b)
    assert root is not None
    # Every field _verify_sigstore reads must be present and subscriptable.
    assert isinstance(root.policy, dict)
    for key in ("signer_identity", "oidc_issuer", "source_repository",
                "workflow_path", "authorized_ref",
                "reviewed_payload_sha256", "trusted_builders"):
        assert root.policy[key]


def test_a_valid_root_is_never_expressed_as_none():
    """Structural: the loader returns a root object or None-with-a-problem.

    It must not be possible to return 'success' with nothing usable, which is
    what (None, None) plus an empty problem list was.
    """
    import ast
    src = open(os.path.join(ROOT, "verify_release.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == "load_trusted_policy")
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            rendered = ast.unparse(node.value)
            assert rendered in ("None", "root"), \
                f"loader returns an ambiguous value: {rendered!r}"


# ---------------------------------------------------------------------------
# 2. Adversarial digest-root cases.
# ---------------------------------------------------------------------------

def test_correct_digest_and_matching_candidate_reaches_authorization(tmp_path):
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    b = bundle_with(tmp_path, canon)
    problems = []
    root = vr.load_trusted_policy(problems, None, RT.policy_digest(canon), b)
    assert root is not None and problems == []


def test_wrong_digest_fails_before_any_value_is_read(tmp_path):
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    b = bundle_with(tmp_path, canon)
    problems = []
    root = vr.load_trusted_policy(problems, None, "0" * 64, b)
    assert root is None
    assert any("does not match the supplied" in p for p in problems)
    assert any("Nothing in the candidate has been trusted" in p
               for p in problems)


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "0" * 63, "0" * 65,
                                 "0x" + "0" * 62, 12345])
def test_malformed_digest_fails_deterministically(tmp_path, bad):
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    b = bundle_with(tmp_path, canon)
    problems = []
    root = vr.load_trusted_policy(problems, None, bad, b)
    assert root is None
    assert problems, f"{bad!r} produced no failure"


def test_digest_is_case_and_whitespace_normalized(tmp_path):
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    b = bundle_with(tmp_path, canon)
    digest = RT.policy_digest(canon)
    problems = []
    root = vr.load_trusted_policy(problems, None,
                                  f"  {digest.upper()}  ", b)
    assert root is not None, problems


def test_correct_digest_over_unresolved_policy_fails(tmp_path):
    """Matching bytes are not authorization: the policy must be authorized."""
    vr = _vr()
    pol = authorized_policy(pinned_revision="PENDING: later")
    canon = RT.canonical_bytes(pol)
    b = bundle_with(tmp_path, canon)
    problems = []
    root = vr.load_trusted_policy(problems, None, RT.policy_digest(canon), b)
    assert root is None
    assert any("not authorized for a signed release" in p for p in problems)


def test_correct_digest_over_malformed_policy_fails(tmp_path):
    vr = _vr()
    pol = authorized_policy()
    pol["unexpected_field"] = "surprise"
    canon = RT.canonical_bytes(pol)
    b = bundle_with(tmp_path, canon)
    problems = []
    root = vr.load_trusted_policy(problems, None, RT.policy_digest(canon), b)
    assert root is None
    assert any("unknown fields" in p or "not authorized" in p
               for p in problems)


def test_file_policy_and_digest_disagreement_fails(tmp_path):
    vr = _vr()
    p = tmp_path / "pol.json"
    p.write_bytes(RT.canonical_bytes(authorized_policy()))
    problems = []
    root = vr.load_trusted_policy(problems, str(p), "0" * 64, None)
    assert root is None
    assert any("!=" in x for x in problems)


def test_file_policy_and_matching_digest_succeeds(tmp_path):
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    p = tmp_path / "pol.json"
    p.write_bytes(canon)
    problems = []
    root = vr.load_trusted_policy(problems, str(p),
                                  RT.policy_digest(canon), None)
    assert root is not None and problems == []
    assert root.source == "file"


def test_digest_only_with_missing_bundle_policy_fails_closed(tmp_path):
    vr = _vr()
    b = bundle_with(tmp_path, None)
    problems = []
    root = vr.load_trusted_policy(problems, None, "a" * 64, b)
    assert root is None
    assert any("no candidate policy" in p for p in problems)


def test_digest_only_with_malformed_bundle_policy_fails_closed(tmp_path):
    vr = _vr()
    b = bundle_with(tmp_path, b"{not json at all")
    problems = []
    root = vr.load_trusted_policy(problems, None, "a" * 64, b)
    assert root is None
    assert any("not valid JSON" in p for p in problems)


def test_digest_only_without_a_bundle_fails_closed():
    vr = _vr()
    problems = []
    root = vr.load_trusted_policy(problems, None, "a" * 64, None)
    assert root is None
    assert any("needs the bundle" in p for p in problems)


def test_candidate_modified_after_the_digest_check_is_not_reread(tmp_path):
    """The authenticated bytes are held, not re-read.

    If the implementation re-read the candidate file later, an attacker who
    can write the bundle could pass the digest check and then swap the
    contents. The root carries the bytes it authenticated.
    """
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    b = bundle_with(tmp_path, canon)
    problems = []
    root = vr.load_trusted_policy(problems, None, RT.policy_digest(canon), b)
    assert root is not None

    evil = authorized_policy(
        source_repository="https://github.com/attacker/repo",
        signer_identity=RT.derive_signer_identity(
            "https://github.com/attacker/repo", WF, REF),
        trusted_builders=[RT.derive_stable_builder_id(
            "https://github.com/attacker/repo", WF, REF)])
    (b / "release_trust_policy.json").write_bytes(RT.canonical_bytes(evil))

    assert root.canonical_bytes == canon, "root must hold authenticated bytes"
    assert root.policy["source_repository"] == REPO
    assert RT.policy_digest(root.canonical_bytes) == root.sha256


def test_zip_policy_differing_from_a_digest_root_fails_after_authentication(
        tmp_path):
    """Signature success does not excuse a different policy inside the zip."""
    vr = _vr()
    canon = RT.canonical_bytes(authorized_policy())
    other = RT.canonical_bytes(authorized_policy(pinned_revision="f" * 40))
    problems = []
    assert vr.bind_candidate_policy(
        problems, "signed-zip", other, canon,
        RT.policy_digest(canon)) is False
    assert problems


# ---------------------------------------------------------------------------
# 3. The trust-root object itself refuses to hold an unauthorized policy.
# ---------------------------------------------------------------------------

def test_root_object_refuses_an_unresolved_policy():
    canon = RT.canonical_bytes(authorized_policy(oidc_issuer="PENDING"))
    with pytest.raises(RT.PolicyError):
        RT.TrustedPolicyRoot(
            json.loads(canon), canon, RT.policy_digest(canon), "digest")


def test_root_object_refuses_a_bad_source():
    pol = authorized_policy()
    canon = RT.canonical_bytes(pol)
    with pytest.raises(RT.PolicyError, match="source"):
        RT.TrustedPolicyRoot(pol, canon, RT.policy_digest(canon), "guesswork")


def test_root_object_refuses_a_bad_digest():
    pol = authorized_policy()
    canon = RT.canonical_bytes(pol)
    with pytest.raises(RT.PolicyError, match="64-hex"):
        RT.TrustedPolicyRoot(pol, canon, "nope", "file")


# ---------------------------------------------------------------------------
# 4. Generated consumer instructions must track the CLI contract.
# ---------------------------------------------------------------------------

def test_generated_instructions_show_both_trust_root_forms(tmp_path):
    """VERIFY_INSTRUCTIONS.md must not tell consumers to just add --online."""
    import subprocess
    zp = tmp_path / "QTA_source.zip"
    out = tmp_path / "bundle"
    subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
         "--make-zip", "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=900)
    text = (out / "VERIFY_INSTRUCTIONS.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "--trusted-policy " in text
    assert "--trusted-policy-sha256" in text
    assert "independently" in low
    assert "self-authorize" in low
    assert "external trust root" in low
    assert "fails closed" in low
    # The bare instruction must be gone.
    assert "add `--online`" not in text
    assert "add --online" not in text


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
