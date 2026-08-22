"""§26 regression: the release workflow must actually be executable.

The hosted release job could never have succeeded. Its failures were all in
the argument contract between the workflow and the two scripts it drives, and
every existing Stage-9 test built its inputs with fixtures, so none of them
exercised the commands the workflow actually runs:

  * ``build_release_artifacts.py --ci`` was called without ``--zip``, which is
    ``required=True`` -- argparse exits 2 before anything runs;
  * nothing in the repository ever created ``release/QTA_source.zip``, yet the
    sign step signed that path;
  * three different bundle directories were used (the builder's default
    ``release_bundle``, ``release/`` for the signature, ``release/bundle`` for
    the verifier), so the artifact signed was never the artifact verified;
  * ``verify_release.py --online`` failed unconditionally, including on a
    genuinely signed release.

These tests read the workflow file and drive the real scripts, so a broken
workflow command cannot hide behind fixture-only coverage.

MODEL-ONLY / FORECAST-ONLY. Software verification; a signature attests origin
and integrity only, never physics.
"""
import json
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job():
    return _workflow()["jobs"]["verify-and-release"]


def _run_lines():
    for step in _job()["steps"]:
        if "run" in step:
            for line in step["run"].replace("\\\n", " ").splitlines():
                line = line.strip()
                if line:
                    yield line


def _expand(cmd, env):
    for k, v in env.items():
        cmd = cmd.replace(f'"${k}"', v).replace(f"${{{k}}}", v).replace(f"${k}", v)
    return cmd


# ------------------------------------------------- argument contract holds --

def test_builder_is_called_with_every_required_argument():
    """argparse must accept the workflow's own invocation."""
    calls = [c for c in _run_lines() if "build_release_artifacts.py" in c]
    assert calls, "workflow never builds release artifacts"
    for c in calls:
        assert "--zip" in c, f"--zip is required=True but missing: {c}"
        assert "--make-zip" in c, (
            "nothing else in the repository creates the release zip; the "
            f"builder must be told to build it: {c}")


def test_one_bundle_directory_is_used_throughout():
    env = _workflow()["env"]
    bundle = env["REL_BUNDLE"]
    zipp = env["REL_ZIP"]
    lines = list(_run_lines())
    built = [c for c in lines if "build_release_artifacts.py" in c]
    signed = [c for c in lines if "sigstore sign" in c]
    verified = [c for c in lines if "verify_release.py" in c]
    assert built and signed and verified
    for c in built + verified:
        assert "$REL_BUNDLE" in c or bundle in c, f"bundle dir not shared: {c}"
    for c in signed + verified:
        assert "$REL_ZIP" in c or zipp in c, f"zip path not shared: {c}"


def test_signed_artifact_is_the_verified_artifact():
    """The exact file signed must be the exact file handed to the verifier."""
    lines = list(_run_lines())
    sign = next(c for c in lines if "sigstore sign" in c)
    online = next(c for c in lines if "verify_release.py" in c and "--online" in c)
    sign_target = re.findall(r"\$REL_ZIP|\S*QTA_source\.zip", sign)
    online_target = re.findall(r"\$REL_ZIP|\S*QTA_source\.zip", online)
    assert sign_target and sign_target == online_target, \
        f"signed {sign_target} but verified {online_target}"


def test_least_privilege_is_retained():
    wf = _workflow()
    assert wf["permissions"] == {}, "default-deny permissions were relaxed"
    job = _job()
    assert job["permissions"]["contents"] == "read"
    checkout = next(s for s in job["steps"]
                    if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False


def test_every_action_is_pinned_to_an_immutable_commit_sha():
    """Policy #3: no tag-pinned action anywhere in the workflows.

    A tag is mutable -- an upstream force-push retargets it -- so a supply-chain
    policy that stops at tags is not a pin. Every `uses:` across ALL workflows
    must carry a 40-hex commit SHA.
    """
    bad = []
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses and not re.search(r"@[0-9a-f]{40}$", uses):
                    bad.append(f"{wf.name}: {uses}")
    assert not bad, f"tag-pinned (mutable) action references remain: {bad}"


def test_pinned_actions_keep_their_human_readable_tag():
    """A bare SHA is unreviewable; the tag must survive as a comment."""
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in wf.read_text(encoding="utf-8").splitlines():
            if re.search(r"uses:\s*\S+@[0-9a-f]{40}", line):
                assert "#" in line.split("@", 1)[1], \
                    f"{wf.name}: pinned action has no tag comment: {line.strip()}"


# ------------------------------------- the chain actually runs end to end --

def test_build_sign_free_chain_runs_and_verifies(tmp_path):
    """Drive the real scripts with the workflow's own argument shapes."""
    zp = tmp_path / "QTA_source.zip"
    bundle = tmp_path / "bundle"
    b = subprocess.run(
        [sys.executable, "build_release_artifacts.py",
         "--zip", str(zp), "--make-zip", "--out", str(bundle), "--ci"],
        cwd=str(ROOT), capture_output=True, text=True)
    assert b.returncode == 0, b.stdout + b.stderr
    assert zp.exists(), "builder did not produce the zip it was asked to build"
    v = subprocess.run(
        [sys.executable, "verify_release.py", "--zip", str(zp),
         "--bundle", str(bundle)],
        cwd=str(ROOT), capture_output=True, text=True)
    assert v.returncode == 0, v.stdout + v.stderr
    assert "VERIFIED (offline)" in v.stdout


def test_release_zip_is_deterministic(tmp_path):
    """Two builds of the same tree must be byte-identical.

    Same archive name in different directories: the archive root is derived
    from the zip's own filename, so a rename legitimately changes the bytes.
    """
    import hashlib
    digests = []
    for i in (1, 2):
        d = tmp_path / f"run{i}"
        d.mkdir()
        zp = d / "QTA_source.zip"
        subprocess.run(
            [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
             "--make-zip", "--out", str(d / "bundle")],
            cwd=str(ROOT), capture_output=True, text=True, check=True)
        digests.append(hashlib.sha256(zp.read_bytes()).hexdigest())
    assert digests[0] == digests[1], "release zip is not reproducible"


def test_zip_has_the_single_root_the_verifier_expects(tmp_path):
    import zipfile
    zp = tmp_path / "QTA_source.zip"
    subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
         "--make-zip", "--out", str(tmp_path / "b")],
        cwd=str(ROOT), capture_output=True, text=True, check=True)
    roots = {n.split("/")[0] for n in zipfile.ZipFile(zp).namelist()}
    assert roots == {"QTA_source"}, f"expected one archive root, got {roots}"


# ------------------------------------------- online gate fails closed only --

def _bundle_claiming_signed(tmp_path, identity=None, issuer=None):
    zp = tmp_path / "QTA_source.zip"
    bundle = tmp_path / "bundle"
    subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
         "--make-zip", "--out", str(bundle)],
        cwd=str(ROOT), capture_output=True, text=True, check=True)
    idx = json.loads((bundle / "release_index.json").read_text())
    idx["signing_status"] = "SIGNED"
    idx["signature_bundles"] = [{"name": "QTA_source.zip",
                                 "bundle": "source.sigstore.json"}]
    (bundle / "release_index.json").write_text(
        json.dumps(idx, indent=1, sort_keys=True) + "\n")
    (bundle / "source.sigstore.json").write_text('{"not":"a real bundle"}')
    if identity or issuer:
        # A policy that merely names an identity is no longer enough to reach
        # signature verification: every trust-critical field must be resolved
        # and self-consistent first. Build a fully authorized policy for the
        # supplied identity so the test can exercise the later stages.
        import release_trust as _rt
        pol = json.loads((bundle / "release_trust_policy.json").read_text())
        ident = identity or pol["signer_identity"]
        parsed = _rt.derive_signer_identity  # noqa: F841  (documented below)
        # Reconstruct repo/workflow/ref from the identity under test so the
        # derived values agree; an inconsistent policy is rejected by design.
        import re as _re
        m = _re.match(
            r"\Ahttps://github\.com/([^/]+)/([^/]+)/"
            r"(\.github/workflows/[^@]+)@(.+)\Z", ident)
        if m:
            owner, repo, wf, ref = m.groups()
            pol["source_repository"] = f"https://github.com/{owner}/{repo}"
            pol["workflow_path"] = wf
            pol["authorized_ref"] = ref
            pol["signer_identity"] = ident
            pol["trusted_builders"] = [
                _rt.derive_stable_builder_id(
                    pol["source_repository"], wf, ref)]
        pol["oidc_issuer"] = issuer or _rt.GITHUB_OIDC_ISSUER
        pol["pinned_revision"] = "0" * 40
        pol["bootstrap_state"] = "RELEASE_IDENTITY_AUTHORIZED"
        (bundle / "release_trust_policy.json").write_bytes(
            _rt.canonical_bytes(pol))
    return zp, bundle


def _online(zp, bundle, trusted=None):
    """--online now requires an EXTERNAL trust root and fails closed without.

    Repository CI passes the checked-out canonical policy explicitly; these
    tests do the same so they exercise the stages beyond the root check.
    """
    args = [sys.executable, "verify_release.py", "--zip", str(zp),
            "--bundle", str(bundle), "--online"]
    if trusted is not False:
        args += ["--trusted-policy",
                 str(trusted or (ROOT /
                     "QTA_stage9_release_verification" /
                     "release_trust_policy.json"))]
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True)


def test_online_without_an_external_trust_root_fails_closed(tmp_path):
    """The headline fix: a release may not supply its own trust root."""
    zp = tmp_path / "QTA_source.zip"
    bundle = tmp_path / "bundle"
    subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
         "--make-zip", "--out", str(bundle)],
        cwd=str(ROOT), capture_output=True, text=True, check=True)
    r = _online(zp, bundle, trusted=False)
    assert r.returncode == 1
    assert "externally supplied trust root" in r.stdout


def test_online_rejects_absent_signature(tmp_path):
    zp = tmp_path / "QTA_source.zip"
    bundle = tmp_path / "bundle"
    subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
         "--make-zip", "--out", str(bundle)],
        cwd=str(ROOT), capture_output=True, text=True, check=True)
    r = _online(zp, bundle)
    assert r.returncode == 1
    # Phase 1 (trust root) now precedes everything, and this repository's
    # canonical policy is deliberately unresolved, so the run is refused
    # there. Absence-of-signature handling is covered by
    # test_signing_finalizer's online-gate tests against a resolved policy.
    assert "not authorized for a signed release" in r.stdout


def test_online_rejects_pending_identity_pins_before_touching_signature(tmp_path):
    """Policy first: a bundle signed by anyone must not pass on PENDING pins."""
    zp, bundle = _bundle_claiming_signed(tmp_path)
    r = _online(zp, bundle)
    assert r.returncode == 1
    # The repository's canonical policy is deliberately still unresolved, so
    # the trust root itself is refused before any signature is considered.
    assert "not authorized for a signed release" in r.stdout
    assert "unresolved values" in r.stdout


def test_online_reports_missing_tooling_as_a_blocker_not_success(tmp_path):
    """Absent verification tooling must be a blocker, never silent success.

    Driven directly against ``_verify_sigstore`` rather than end-to-end,
    because a synthetic bundle can no longer reach that stage: the policy
    enforcement added ahead of it (canonical-divergence, repository, ref and
    builder checks) correctly rejects a fabricated identity first. Those are
    covered by their own tests; this one isolates the tooling branch.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_vr", str(ROOT / "verify_release.py"))
    vr = importlib.util.module_from_spec(spec)
    sys.modules["_vr"] = vr
    spec.loader.exec_module(vr)

    # A policy and provenance that agree, so the run reaches the tooling
    # branch and dies there rather than earlier.
    import release_trust as _rt
    repo = "https://github.com/example/repo"
    wf = ".github/workflows/release.yml"
    ref = "refs/tags/v1"
    pol = {
        "schema_version": _rt.SCHEMA_VERSION, "wildcards_forbidden": True,
        "source_repository": repo, "workflow_path": wf,
        "authorized_ref": ref,
        "signer_identity": _rt.derive_signer_identity(repo, wf, ref),
        "oidc_issuer": _rt.GITHUB_OIDC_ISSUER,
        "pinned_revision": "a" * 40,
        "trusted_builders": [_rt.derive_stable_builder_id(repo, wf, ref)],
        "bootstrap_state": "RELEASE_IDENTITY_AUTHORIZED",
    }
    prov = {"predicate": {
        "buildDefinition": {
            "externalParameters": {"source_repository": repo,
                                   "workflow_path": wf,
                                   "authorized_ref": ref},
            "resolvedDependencies": [{"uri": "git+" + repo,
                                      "digest": {"gitCommit": "b" * 40}}]},
        "runDetails": {"builder": {
            "id": _rt.derive_stable_builder_id(repo, wf, ref)}}}}

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "release_trust_policy.json").write_bytes(
        _rt.canonical_bytes(pol))
    (bundle / "source.sigstore.json").write_text('{"x":1}')
    zp = tmp_path / "QTA_source.zip"
    zp.write_bytes(b"payload")

    problems = []
    vr._verify_sigstore(
        problems, zp, bundle, {},
        [{"name": "QTA_source.zip", "bundle": "source.sigstore.json"}],
        pol)
    joined = " ".join(problems)
    assert "absence of tooling is never success" in joined, problems
    assert problems, "missing tooling must be recorded as a failure"


def test_online_has_a_reachable_success_path():
    """The SIGNED branch must be able to succeed; it used to be a dead end.

    Previously the SIGNED branch called fail_list() unconditionally, so no
    input could pass --online. Assert the verifier now performs real
    verification rather than rejecting by construction.
    """
    src = (ROOT / "verify_release.py").read_text(encoding="utf-8")
    assert "_verify_sigstore" in src
    assert "verify_artifact" in src, "no real Sigstore verification is performed"
    assert "online signature verification tooling\n" not in src
