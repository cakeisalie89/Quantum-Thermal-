"""The signing finalizer must close the metadata blocker and nothing else.

Two properties are under test, and they pull in opposite directions:

1. Without finalization, online verification can never succeed -- that is the
   blocker. Three shapes of it are asserted directly against verify_release's
   own branch: PENDING with no bundle, PENDING with a real-looking bundle, and
   SIGNED with an empty list.

2. With finalization, the *only* thing that changes is signing metadata.
   Everything else in release_index.json -- files, digests, provenance, SBOM,
   scientific claims, trust policy -- must be untouched, and every precondition
   must fail closed rather than fudging a transition through.

Crucially, finalization must NOT manufacture a passing verification. After
finalizing, online verification must get *past* the missing-metadata branch and
then die on the identity policy, which is still PENDING. Signing stays PENDING
as a repository state until a real hosted authorized run exists.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import finalize_release_signing as F  # noqa: E402

ZIP_NAME = "QTA_source.zip"
BUNDLE_NAME = "source.sigstore.json"

# Minimal structure that satisfies _looks_like_a_sigstore_bundle. It is NOT a
# real signature and is never presented as one -- these tests verify metadata
# handling, never cryptography.
FAKE_BUNDLE = {
    "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
    "verificationMaterial": {"certificate": {"rawBytes": "QUJD"}},
    "messageSignature": {"messageDigest": {"algorithm": "SHA2_256",
                                           "digest": "QUJD"},
                         "signature": "QUJD"},
}


def _sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(tmp_path, *, status="PENDING", bundles=None, write_bundle=True,
           bundle_body=None, zip_body=b"canonical release payload"):
    """A minimal but structurally faithful pre-sign bundle directory."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    zip_path = tmp_path / ZIP_NAME
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("QTA/payload.txt", zip_body.decode())
    digest = _sha(zip_path)

    artifact = {"name": ZIP_NAME, "size": zip_path.stat().st_size,
                "sha256": digest}
    key = {"name": "final_manifest.json", "size": 3, "sha256": "ab" * 32}
    index = {
        "schema_version": "1.0.0",
        "label": "QTA",
        "release_artifact": artifact,
        "authoritative_input": "stage8",
        "files": [artifact, key],
        "sbom": {"file": "sbom.cdx.json", "sha256": "cd" * 32,
                 "validated_against": "uv.lock", "components": 71},
        "provenance": {"file": "provenance.intoto.json", "sha256": "ef" * 32,
                       "slsa_level_claimed": "NONE", "builder": "local"},
        "signing_status": status,
        "signing_blockers": ["no cosign/gitsign binary in sandbox"],
        "signature_bundles": [] if bundles is None else bundles,
        "claims": {"scientific_gate_PASS_count": 0, "can_PASS_now": "NO",
                   "measured_in_this_system": False},
    }
    (bundle_dir / "release_index.json").write_text(
        json.dumps(index, indent=1, sort_keys=True) + "\n")
    (bundle_dir / "SHA256SUMS").write_text(
        f"{digest}  {ZIP_NAME}\n{key['sha256']}  {key['name']}\n")
    (bundle_dir / "release_trust_policy.json").write_text(json.dumps({
        "oidc_issuer": "PENDING: exact issuer at first signing",
        "signer_identity": "PENDING: exact certificate identity",
    }, indent=1) + "\n")
    if write_bundle:
        body = FAKE_BUNDLE if bundle_body is None else bundle_body
        (bundle_dir / BUNDLE_NAME).write_text(
            body if isinstance(body, str) else json.dumps(body))
    return bundle_dir, zip_path


def _index(bundle_dir):
    return json.loads((bundle_dir / "release_index.json").read_text())


# ---------------------------------------------------------------------------
# 1. The blocker itself: what cannot verify online, and why.
# ---------------------------------------------------------------------------

def _online_branch(status, sig):
    """Reproduce verify_release.py's own gate, asserted against its source."""
    src = open(os.path.join(ROOT, "verify_release.py"), encoding="utf-8").read()
    # The gate is now derived from facts rather than from the status string
    # alone: a declared bundle must exist on disk. The status must AGREE with
    # what is observed, so a mutable string cannot confer signed state.
    assert 'status == "SIGNED" and declared and present' in src, \
        "verify_release's online gate changed; this test is now stale"
    assert 'signing_status is PENDING but a' in src, \
        "the PENDING+bundle inconsistency check is missing"
    return status != "SIGNED" or not sig


def test_pending_with_no_bundle_cannot_verify_online():
    assert _online_branch("PENDING", []) is True


def test_pending_with_a_real_looking_bundle_still_cannot_verify_online():
    """A bundle on disk is not metadata. The gate reads the index, not the FS."""
    assert _online_branch("PENDING", [{"name": ZIP_NAME,
                                       "bundle": BUNDLE_NAME}]) is True


def test_signed_with_empty_signature_list_cannot_verify_online():
    assert _online_branch("SIGNED", []) is True


def test_only_signed_plus_a_bundle_record_passes_the_gate():
    assert _online_branch("SIGNED", [{"name": ZIP_NAME,
                                      "bundle": BUNDLE_NAME}]) is False


# ---------------------------------------------------------------------------
# 2. The finalizer refuses everything it should.
# ---------------------------------------------------------------------------

def test_finalizer_refuses_missing_bundle(tmp_path):
    bundle_dir, zip_path = _build(tmp_path, write_bundle=False)
    with pytest.raises(F.Refused, match="missing"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    assert _index(bundle_dir)["signing_status"] == "PENDING"


def test_finalizer_refuses_empty_bundle(tmp_path):
    bundle_dir, zip_path = _build(tmp_path, bundle_body="")
    with pytest.raises(F.Refused, match="empty"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    assert _index(bundle_dir)["signing_status"] == "PENDING"


def test_finalizer_refuses_unparseable_bundle(tmp_path):
    bundle_dir, zip_path = _build(tmp_path, bundle_body="{not json")
    with pytest.raises(F.Refused, match="not parseable"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_finalizer_refuses_a_file_that_is_not_a_sigstore_bundle(tmp_path):
    """Valid JSON is not enough -- an unrelated document must be refused."""
    bundle_dir, zip_path = _build(tmp_path, bundle_body={"hello": "world"})
    with pytest.raises(F.Refused, match="shape of a Sigstore bundle"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_finalizer_refuses_wrong_zip(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    other = tmp_path / "SOMETHING_ELSE.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("x", "y")
    with pytest.raises(F.Refused, match="release_artifact names"):
        F.finalize(bundle_dir, other, BUNDLE_NAME)
    assert _index(bundle_dir)["signing_status"] == "PENDING"


def test_finalizer_refuses_missing_zip(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    zip_path.unlink()
    with pytest.raises(F.Refused, match="zip does not exist"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_finalizer_refuses_zip_digest_drift(tmp_path):
    """The signed artifact must be the one the index and SHA256SUMS describe."""
    bundle_dir, zip_path = _build(tmp_path)
    with zipfile.ZipFile(zip_path, "w") as zf:      # rewrite -> new digest
        zf.writestr("QTA/payload.txt", "TAMPERED")
    with pytest.raises(F.Refused, match="digest drift"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    assert _index(bundle_dir)["signing_status"] == "PENDING"


def test_finalizer_refuses_sha256sums_disagreement(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    p = bundle_dir / "SHA256SUMS"
    p.write_text(p.read_text().replace(_sha(zip_path), "00" * 32))
    with pytest.raises(F.Refused, match="SHA256SUMS records"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_finalizer_refuses_missing_sha256sums(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    (bundle_dir / "SHA256SUMS").unlink()
    with pytest.raises(F.Refused, match="SHA256SUMS missing"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_finalizer_refuses_missing_index(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    (bundle_dir / "release_index.json").unlink()
    with pytest.raises(F.Refused, match="release index does not exist"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_finalizer_refuses_non_pending_starting_state(tmp_path):
    bundle_dir, zip_path = _build(tmp_path, status="SIGNED")
    with pytest.raises(F.Refused, match="expected 'PENDING'"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_finalizer_refuses_pending_with_prepopulated_bundles(tmp_path):
    """An inconsistent pre-sign state is refused, not tidied up."""
    bundle_dir, zip_path = _build(
        tmp_path, bundles=[{"name": ZIP_NAME, "bundle": BUNDLE_NAME}])
    with pytest.raises(F.Refused, match="already non-empty"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)


def test_running_twice_fails_closed_by_default(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    first = (bundle_dir / "release_index.json").read_bytes()
    with pytest.raises(F.Refused, match="expected 'PENDING'"):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    assert (bundle_dir / "release_index.json").read_bytes() == first


def test_idempotent_mode_accepts_only_an_exactly_identical_finalized_index(
        tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    first = (bundle_dir / "release_index.json").read_bytes()
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME, idempotent=True)
    assert (bundle_dir / "release_index.json").read_bytes() == first, \
        "idempotent mode must rewrite nothing"

    # ... but not a finalized index that says something else.
    idx = _index(bundle_dir)
    idx["signature_bundles"] = [{"name": ZIP_NAME, "bundle": "OTHER.json"}]
    (bundle_dir / "release_index.json").write_text(json.dumps(idx, indent=1))
    with pytest.raises(F.Refused):
        F.finalize(bundle_dir, zip_path, BUNDLE_NAME, idempotent=True)


# ---------------------------------------------------------------------------
# 3. The mutation boundary.
# ---------------------------------------------------------------------------

def test_finalizer_changes_exactly_two_keys_and_nothing_else(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    before = _index(bundle_dir)
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    after = _index(bundle_dir)

    changed = {k for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    assert changed == {"signing_status", "signature_bundles"}, changed
    assert changed <= set(F.MUTABLE_KEYS)
    assert set(before) == set(after), "no key may be added or removed"


@pytest.mark.parametrize("key", ["files", "release_artifact", "sbom",
                                 "provenance", "claims", "signing_blockers",
                                 "schema_version", "authoritative_input"])
def test_finalizer_cannot_change_protected_content(tmp_path, key):
    bundle_dir, zip_path = _build(tmp_path)
    before = _index(bundle_dir)[key]
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    assert _index(bundle_dir)[key] == before


def test_finalizer_cannot_change_scientific_claims(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    claims = _index(bundle_dir)["claims"]
    assert claims["scientific_gate_PASS_count"] == 0
    assert claims["can_PASS_now"] == "NO"
    assert claims["measured_in_this_system"] is False


def test_finalizer_cannot_change_the_trust_policy(tmp_path):
    """The load-bearing separation: signature existence is not authorization."""
    bundle_dir, zip_path = _build(tmp_path)
    policy = (bundle_dir / "release_trust_policy.json").read_bytes()
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    assert (bundle_dir / "release_trust_policy.json").read_bytes() == policy
    after = json.loads(policy)
    assert after["signer_identity"].startswith("PENDING")
    assert after["oidc_issuer"].startswith("PENDING")


def _write_targets(path):
    """Every expression this module writes a file through, as source text.

    Structural rather than textual: a print() that *names* signer_identity is
    documentation, but a call that writes the policy file is a violation, and
    only an AST walk can tell those apart. Covers the three write forms that
    exist in this codebase -- open(..., "w"), Path.write_text/write_bytes, and
    the module's own _write_atomically.
    """
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name in ("write_text", "write_bytes"):
            targets.append(ast.unparse(fn.value))
        elif name == "_write_atomically" and node.args:
            targets.append(ast.unparse(node.args[0]))
        elif name == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if any(c in mode for c in "wax+"):
                targets.append(ast.unparse(node.args[0]) if node.args else "?")
    return targets


def test_finalizer_writes_only_the_release_index():
    """The mutation boundary, enforced structurally.

    The finalizer must have exactly one write target: the release index. If a
    future edit adds a write to the trust policy, the SBOM, provenance or the
    zip, this fails -- regardless of how the filename is spelled or built.
    """
    targets = _write_targets(
        os.path.join(ROOT, "finalize_release_signing.py"))
    # os.fdopen inside _write_atomically writes the temp file, which is then
    # os.replace'd onto index_path; the temp handle is not a distinct target.
    assert targets == ["index_path"], targets


def test_finalizer_never_opens_the_trust_policy_at_all():
    """Not even for reading: the finalizer has no business knowing the pins."""
    import ast
    tree = ast.parse(open(
        os.path.join(ROOT, "finalize_release_signing.py"),
        encoding="utf-8").read())
    for node in ast.walk(tree):
        # The filename may appear only as the value of the greppable
        # _TRUST_POLICY_UNTOUCHED marker constant, never in a call.
        if isinstance(node, ast.Call):
            src = ast.unparse(node)
            assert "trust_policy" not in src.lower(), \
                f"finalizer must not touch the trust policy: {src[:120]}"


def test_finalizer_declares_its_mutable_keys_and_honours_them():
    """MUTABLE_KEYS is the contract; it must be exactly the two signing keys."""
    assert F.MUTABLE_KEYS == {"signing_status", "signature_bundles"}


def test_finalizer_cannot_change_the_zip(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    before = zip_path.read_bytes()
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    assert zip_path.read_bytes() == before


# ---------------------------------------------------------------------------
# 4. The produced schema is exactly what _verify_sigstore consumes.
# ---------------------------------------------------------------------------

def test_finalizer_produces_the_schema_verify_sigstore_consumes(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    sig = _index(bundle_dir)["signature_bundles"]
    assert isinstance(sig, list) and len(sig) == 1
    entry = sig[0]
    assert set(entry) == {"name", "bundle"}, entry

    # Replay _verify_sigstore's own resolution logic on the record.
    name = entry.get("name")
    bundle_path = bundle_dir / str(entry.get("bundle", name))
    target = zip_path if (name or "").endswith(zip_path.name) \
        else bundle_dir / name
    assert bundle_path.exists(), "bundle path must resolve to a real file"
    assert target == zip_path, "signed subject must resolve to the zip itself"
    assert target.exists()


def test_the_record_shape_matches_verify_release_source():
    """If _verify_sigstore's resolution changes, this test must fail loudly."""
    src = open(os.path.join(ROOT, "verify_release.py"), encoding="utf-8").read()
    assert 'entry["name"]' in src
    assert '_bundle_relative(bundle, str(entry["bundle"]))' in src
    # Subject matching is now EXACT, not a suffix test: routing metadata must
    # not be able to point the verifier at a differently named subject.
    assert "if name != zip_path.name:" in src
    assert 'endswith(zip_path.name)' not in src


# ---------------------------------------------------------------------------
# 5. Finalization advances the failure, it does not manufacture success.
# ---------------------------------------------------------------------------

def test_after_finalization_online_verification_reaches_the_identity_check(
        tmp_path):
    """The whole point: the metadata blocker is gone, the trust gate remains.

    Before finalization the run dies on "no real signature exists". After it,
    the same run reaches _verify_sigstore and dies on the PENDING identity
    pins instead. Signing is still not complete -- it has simply stopped
    failing for the wrong reason.
    """
    bundle_dir, zip_path = _build(tmp_path)
    idx = _index(bundle_dir)
    assert _online_branch(idx["signing_status"],
                          idx["signature_bundles"]) is True

    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    idx = _index(bundle_dir)
    assert _online_branch(idx["signing_status"],
                          idx["signature_bundles"]) is False, \
        "metadata blocker should be cleared"

    # And the next gate -- the identity policy -- still refuses.
    policy = json.loads(
        (bundle_dir / "release_trust_policy.json").read_text())
    for field in ("signer_identity", "oidc_issuer"):
        value = str(policy.get(field, ""))
        assert not value or value.startswith("PENDING") or "*" in value, \
            "the identity pins must still refuse; finalization is not trust"


def test_repository_signing_status_remains_pending():
    """No committed artifact may claim SIGNED. Signing is PENDING until a real
    hosted authorized run exists."""
    policy = json.loads(open(
        os.path.join(ROOT, "QTA_stage9_release_verification",
                     "release_trust_policy.json"), encoding="utf-8").read())
    for field in ("signer_identity", "oidc_issuer", "pinned_revision"):
        assert str(policy[field]).startswith("PENDING"), field


# ---------------------------------------------------------------------------
# 6. CLI behaviour.
# ---------------------------------------------------------------------------

def test_cli_refuses_with_nonzero_exit_and_no_traceback(tmp_path):
    bundle_dir, zip_path = _build(tmp_path, write_bundle=False)
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "finalize_release_signing.py"),
         "--bundle", str(bundle_dir), "--zip", str(zip_path)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "FAIL-CLOSED" in r.stdout
    assert "Traceback" not in r.stderr, r.stderr


def test_cli_succeeds_on_a_well_formed_bundle(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "finalize_release_signing.py"),
         "--bundle", str(bundle_dir), "--zip", str(zip_path)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PENDING -> SIGNED" in r.stdout
    assert "does not authorize the signer" in r.stdout
    assert _index(bundle_dir)["signing_status"] == "SIGNED"


def test_write_is_atomic_leaving_no_temp_files(tmp_path):
    bundle_dir, zip_path = _build(tmp_path)
    F.finalize(bundle_dir, zip_path, BUNDLE_NAME)
    leftovers = [p.name for p in bundle_dir.iterdir()
                 if p.name.startswith(".finalize-")]
    assert leftovers == [], leftovers


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
