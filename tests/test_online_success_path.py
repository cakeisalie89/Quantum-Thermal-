"""The successful online path, executed end to end.

Every other suite here exercises REFUSAL. That asymmetry hid a defect that no
refusal test could reach: `verify()` bound `root` to the `TrustedPolicyRoot` in
phase 1, rebound the same name to the archive's top-level directory string in
phase 2, and then evaluated `root.sha256` in phase 4 --

    AttributeError: 'str' object has no attribute 'sha256'

Phase 4 only runs after the Sigstore signature verifies, and no test had ever
gotten there, so the entire suite passed against a verifier that could not
successfully verify anything.

A test that merely asserts `verify()` returned 0 would not have caught it
either, because a run that fails early also never reaches phase 4. This suite
therefore asserts that each authenticated stage ACTUALLY EXECUTED, by checking
for the specific evidence line each one emits and by counting calls into the
functions themselves.

ONLY the Sigstore cryptographic operation is mocked. Everything else --
policy loading, digest comparison, archive structure, payload recomputation,
binding validation, provenance cross-check, PASS recomputation -- runs for
real.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_trust as RT   # noqa: E402

POLICY_REL = str(RT.CANONICAL_POLICY_PATH)
ZIPROOT = "QTA_source"
REPO = "https://github.com/cakeisalie89/Quantum-Thermal-"
WF = ".github/workflows/release.yml"
REF = "refs/tags/qta-stage9-v1"
BUILDER = RT.derive_stable_builder_id(REPO, WF, REF)
REV_REVIEWED = "c" * 40
REV_RELEASED = "a" * 40


def _vr(name="_vr_success"):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "verify_release.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _lock_text(vr) -> bytes:
    """A uv.lock carrying exactly the pinned scientific versions."""
    return "".join(f'name = "{n}"\nversion = "{v}"\n'
                   for n, v in sorted(vr.SCI_PINS.items())).encode()


def build_signed_release(tmp_path, *, mutate_binding=None,
                         mutate_zip_policy=None, extra_zip_member=None):
    """A release that SHOULD verify: coherent, authorized, internally bound."""
    vr = _vr()
    d = tmp_path / "rel"
    b = d / "bundle"
    b.mkdir(parents=True, exist_ok=True)

    members = {
        f"{ZIPROOT}/uv.lock": _lock_text(vr),
        f"{ZIPROOT}/results_gate_table.csv":
            b"gate,status\nB4,CONDITIONAL\nD10b,BLOCKED\n",
    }

    def policy(payload_digest):
        return {
            "schema_version": RT.SCHEMA_VERSION, "wildcards_forbidden": True,
            "source_repository": REPO, "workflow_path": WF,
            "authorized_ref": REF,
            "signer_identity": RT.derive_signer_identity(REPO, WF, REF),
            "oidc_issuer": RT.GITHUB_OIDC_ISSUER,
            "pinned_revision": REV_REVIEWED,
            "reviewed_payload_sha256": payload_digest,
            "trusted_builders": [BUILDER],
            "bootstrap_state": "TRUSTED_RELEASE_ELIGIBLE"}

    # The payload digest excludes the authorization closure, so the policy can
    # carry the digest of a payload that contains the policy.
    payload = {k[len(ZIPROOT) + 1:]: v for k, v in members.items()}
    payload[POLICY_REL] = RT.canonical_bytes(policy("0" * 64))
    digest = RT.payload_digest(payload)

    pol = policy(digest)
    pol_bytes = RT.canonical_bytes(pol)
    pol_sha = RT.policy_digest(pol_bytes)

    binding = {"schema_version": RT.SCHEMA_VERSION, "source_repository": REPO,
               "workflow_path": WF, "authorized_ref": REF,
               "release_revision": REV_RELEASED,
               "reviewed_revision": REV_REVIEWED,
               "reviewed_payload_sha256": digest,
               "stable_builder_id": BUILDER,
               "trusted_policy_sha256": pol_sha}
    if mutate_binding:
        mutate_binding(binding)

    zp = d / "QTA_source.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for n, v in members.items():
            z.writestr(n, v)
        z.writestr(f"{ZIPROOT}/{POLICY_REL}",
                   mutate_zip_policy(pol_bytes) if mutate_zip_policy
                   else pol_bytes)
        z.writestr(f"{ZIPROOT}/{RT.RELEASE_BINDING_NAME}",
                   json.dumps(binding).encode())
        if extra_zip_member:
            # Inside the payload, so the reviewed digest must change.
            z.writestr(f"{ZIPROOT}/{extra_zip_member}", b"smuggled")
    zb = zp.read_bytes()
    zh = _sha(zb)

    (b / "release_trust_policy.json").write_bytes(pol_bytes)
    (b / "source.sigstore.json").write_text(json.dumps(
        {"mediaType":
         "application/vnd.dev.sigstore.bundle+json;version=0.3",
         "verificationMaterial": {}, "messageSignature": {}}))
    (b / "release_index.json").write_text(json.dumps({
        "release_artifact": {"name": "QTA_source.zip", "size": len(zb),
                             "sha256": zh},
        "files": [{"name": "QTA_source.zip", "sha256": zh}],
        "claims": {"scientific_gate_PASS_count": 0},
        "provenance": {"slsa_level_claimed": "NONE"},
        "signing_status": "SIGNED",
        "signature_bundles": [{"name": "QTA_source.zip",
                               "bundle": "source.sigstore.json"}]}))
    (b / "SHA256SUMS").write_text(f"{zh}  QTA_source.zip\n")
    (b / "sbom.cdx.json").write_text(json.dumps(
        {"components": [{"name": n, "version": v}
                        for n, v in sorted(vr.SCI_PINS.items())]}))
    (b / "provenance.intoto.json").write_text(json.dumps({
        "subject": [{"name": "QTA_source.zip", "digest": {"sha256": zh}}],
        "predicate": {"slsa_level_claimed": "NONE", "buildDefinition": {
            "externalParameters": {"source_repository": REPO,
                                   "workflow_path": WF,
                                   "authorized_ref": REF},
            "resolvedDependencies": [
                {"digest": {"gitCommit": REV_RELEASED}}]}}}))
    ext = d / "external_trusted_policy.json"
    ext.write_bytes(pol_bytes)
    return zp, b, d, ext


def run_online(tmp_path, capsys, **kw):
    """Run the full online path with ONLY the crypto mocked."""
    zp, b, d, ext = build_signed_release(tmp_path, **kw)
    vr = _vr()
    calls = {"sigstore": 0, "binding": 0, "auth_binding": 0, "aux": 0,
             "bind_policy": []}

    vr._verify_sigstore = (lambda *a, **k: calls.__setitem__(
        "sigstore", calls["sigstore"] + 1))

    real_binding = vr.read_release_binding
    real_auth = vr.enforce_authenticated_binding
    real_aux = vr.check_auxiliary_provenance
    real_bind_policy = vr.bind_candidate_policy

    def wrap_binding(*a, **k):
        calls["binding"] += 1
        return real_binding(*a, **k)

    def wrap_auth(*a, **k):
        calls["auth_binding"] += 1
        return real_auth(*a, **k)

    def wrap_aux(*a, **k):
        calls["aux"] += 1
        return real_aux(*a, **k)

    def wrap_bind_policy(problems, label, *a, **k):
        calls["bind_policy"].append(label)
        return real_bind_policy(problems, label, *a, **k)

    vr.read_release_binding = wrap_binding
    vr.enforce_authenticated_binding = wrap_auth
    vr.check_auxiliary_provenance = wrap_aux
    vr.bind_candidate_policy = wrap_bind_policy

    cwd = os.getcwd()
    os.chdir(d)                      # no local canonical policy: UNANCHORED
    try:
        rc = vr.verify(zp, b, True, str(ext), None)
    finally:
        os.chdir(cwd)
    return rc, capsys.readouterr().out, calls


# ---------------------------------------------------------------------------
# The test the directive requires.
# ---------------------------------------------------------------------------

def test_online_success_reaches_all_authenticated_phases(tmp_path, capsys):
    """Phases 3 -> 6 all execute, and the verifier returns success.

    Each assertion names one stage. Returning 0 alone would not prove any of
    them ran, because an early failure also skips them all.
    """
    rc, out, calls = run_online(tmp_path, capsys)

    # 1. external trust root established (phase 1)
    assert "external trust root loaded from file and authorized" in out, out

    # 2. candidate parsing + 3. archive structure (phase 2)
    assert "release zip digest matches index" in out, out
    assert "SBOM matches uv.lock" in out, out

    # 4. Sigstore reached exactly once (phase 3)
    assert calls["sigstore"] == 1, calls

    # 5. signed-ZIP policy comparison and 6. retained bundle policy (phase 4)
    assert calls["bind_policy"] == ["signed-zip", "bundle"], calls
    assert "signed-zip policy is byte-identical to the external trust root" \
        in out, out
    assert "bundle policy is byte-identical to the external trust root" \
        in out, out

    # 7. release_binding parsed from the authenticated archive
    assert calls["binding"] == 1, calls

    # 8. enforce_authenticated_binding ran and passed
    assert calls["auth_binding"] == 1, calls
    assert "signed release binding passes its schema" in out, out
    assert "builder derived from authenticated content and authorized" in out

    # 9. reviewed payload digest recomputed from the signed archive
    assert "reviewed payload digest matches the authorized value" in out, out

    # 10. auxiliary provenance cross-check (phase 5)
    assert calls["aux"] == 1, calls
    assert "auxiliary provenance is consistent with the signed binding" in out

    # 11. scientific PASS recomputed from the signed gate table (phase 6)
    assert "gate table inside zip: scientific PASS count = 0" in out, out

    # 12. and the verifier actually succeeds
    assert rc == 0, out


def test_trust_root_is_not_the_archive_root(tmp_path, capsys):
    """The exact defect, stated as a property.

    `trust_root` is a TrustedPolicyRoot and carries `.sha256`; `archive_root`
    is a str used as a ZIP member prefix. Binding both to one name made phase 4
    evaluate `.sha256` on a string.
    """
    vr = _vr()
    zp, b, d, ext = build_signed_release(tmp_path)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json", label="candidate",
        require_resolved=False, missing="MISSING_TRUST_POLICY",
        unreadable="UNREADABLE_TRUST_POLICY", invalid="INVALID_TRUST_POLICY")
    trust_root = vr.load_trusted_policy(problems, str(ext), None, doc)
    assert isinstance(trust_root, RT.TrustedPolicyRoot)
    assert isinstance(trust_root.sha256, str) and len(trust_root.sha256) == 64

    with zipfile.ZipFile(zp) as zf:
        archive_root = vr.validate_zip_structure(problems, zf.namelist())
    assert isinstance(archive_root, str)
    assert archive_root == ZIPROOT
    assert not hasattr(archive_root, "sha256")
    assert problems == [], problems


def test_verify_source_never_rebinds_one_name_to_both(tmp_path):
    """Structural guard: no single assignment target takes both kinds.

    Checked on the AST so a comment mentioning `root` cannot satisfy or trip
    it.
    """
    import ast
    src = open(os.path.join(ROOT, "verify_release.py")).read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "verify")
    assigned = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                            ast.Name):
            assigned.add(node.target.id)
    assert "root" not in assigned, \
        "verify() must not bind a bare `root`; use trust_root/archive_root"
    assert {"trust_root", "archive_root"} <= assigned, assigned


# ---------------------------------------------------------------------------
# The success path must still refuse when an authenticated stage disagrees.
# A verifier that returns 0 for everything would pass the test above.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,bad", [
    ("source_repository", "https://github.com/attacker/evil"),
    ("workflow_path", ".github/workflows/attack.yml"),
    ("authorized_ref", "refs/heads/main"),
    ("reviewed_payload_sha256", "d" * 64),
    ("reviewed_revision", "f" * 40),
    ("stable_builder_id", "github-actions://x/y@z"),
])
def test_authenticated_binding_disagreement_still_fails(tmp_path, capsys,
                                                        field, bad):
    rc, out, calls = run_online(
        tmp_path, capsys,
        mutate_binding=lambda d: d.__setitem__(field, bad))
    assert calls["sigstore"] == 1          # crypto still succeeded
    assert rc == 1, out                     # but authorization did not


def test_signed_content_that_is_not_the_reviewed_content_fails(tmp_path,
                                                               capsys):
    """The reviewed payload digest is load-bearing, on its own.

    An extra file smuggled into the signed archive keeps the signature valid
    (it is signed as part of the zip) and keeps every identity check happy --
    only the recomputed payload digest disagrees with the authorized value.
    Without this, deleting that comparison was caught only incidentally, by an
    unrelated test in another suite.
    """
    rc, out, calls = run_online(tmp_path, capsys,
                                extra_zip_member="smuggled.py")
    assert calls["sigstore"] == 1              # crypto succeeded
    assert calls["auth_binding"] == 1          # identity checks ran
    assert rc == 1, out
    assert "reviewed payload digest" in out and "authorized" in out, out
    assert "the signed content is not the reviewed content" in out, out
    assert "reviewed payload digest matches the authorized value" not in out


def test_a_signed_zip_policy_that_differs_from_the_root_fails(tmp_path,
                                                              capsys):
    """The zip may only ship the authorized policy."""
    def tamper(raw):
        doc = json.loads(raw.decode())
        doc["pinned_revision"] = "9" * 40
        return RT.canonical_bytes(doc)
    rc, out, calls = run_online(tmp_path, capsys, mutate_zip_policy=tamper)
    assert calls["sigstore"] == 1
    assert rc == 1
    assert "differs from the external trusted policy" in out or \
        "was not bound to the external trust root" in out, out


# ---------------------------------------------------------------------------
# Phase 5 audit (§8). Provenance is UNSIGNED: it sits beside the archive, so
# anyone can edit it while the signature over the zip stays valid. These
# nested reads are therefore attacker-controlled even on a genuinely
# authenticated release -- and they are reachable only after the signature
# verifies, which is why no refusal test had ever touched them.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("predicate,why", [
    ({"buildDefinition": "nope"}, "buildDefinition is a string"),
    ({"buildDefinition": []}, "buildDefinition is a list"),
    ({"buildDefinition": {"externalParameters": []}},
     "externalParameters is a list"),
    ({"buildDefinition": {"externalParameters": "s"}},
     "externalParameters is a string"),
    ({"buildDefinition": {"externalParameters": {},
                          "resolvedDependencies": "abc"}},
     "resolvedDependencies is a string"),
    ({"buildDefinition": {"externalParameters": {},
                          "resolvedDependencies": [None]}},
     "a dependency is null"),
    ({"buildDefinition": {"externalParameters": {},
                          "resolvedDependencies": ["str"]}},
     "a dependency is a string"),
    ({"buildDefinition": {"externalParameters": {},
                          "resolvedDependencies": [{"digest": "s"}]}},
     "a dependency digest is a string"),
    ({}, "no buildDefinition at all"),
])
def test_malformed_auxiliary_provenance_is_reported_not_crashed(predicate,
                                                                why):
    """Each of these raised AttributeError before the nested walk was guarded.

    `predicate` is typed at parse time; nothing below it was.
    """
    vr = _vr()
    binding = {"source_repository": REPO, "workflow_path": WF,
               "authorized_ref": REF, "release_revision": REV_RELEASED}
    problems = []
    vr.check_auxiliary_provenance(problems, {}, binding,
                                  {"predicate": predicate})
    assert problems, why            # reported...
    assert all("Traceback" not in p for p in problems), why   # ...not crashed


def test_consistent_auxiliary_provenance_still_reports_agreement():
    """The guard must not make every provenance look inconsistent."""
    vr = _vr()
    binding = {"source_repository": REPO, "workflow_path": WF,
               "authorized_ref": REF, "release_revision": REV_RELEASED}
    prov = {"predicate": {"buildDefinition": {
        "externalParameters": {"source_repository": REPO,
                               "workflow_path": WF, "authorized_ref": REF},
        "resolvedDependencies": [{"digest": {"gitCommit": REV_RELEASED}}]}}}
    problems = []
    vr.check_auxiliary_provenance(problems, {}, binding, prov)
    assert problems == [], problems


def test_tampered_provenance_on_a_valid_signed_release_is_caught(tmp_path,
                                                                 capsys):
    """End to end: signature still valid, provenance edited underneath it."""
    zp, b, d, ext = build_signed_release(tmp_path)
    (b / "provenance.intoto.json").write_text(json.dumps({
        "subject": json.loads(
            (b / "provenance.intoto.json").read_text())["subject"],
        "predicate": {"slsa_level_claimed": "NONE",
                      "buildDefinition": "not-an-object"}}))
    vr = _vr()
    vr._verify_sigstore = lambda *a, **k: None
    cwd = os.getcwd()
    os.chdir(d)
    try:
        rc = vr.verify(zp, b, True, str(ext), None)
    finally:
        os.chdir(cwd)
    out = capsys.readouterr().out
    assert rc == 1
    assert "auxiliary provenance disagrees" in out, out


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
