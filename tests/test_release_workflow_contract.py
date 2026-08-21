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


def test_unpinned_actions_are_declared_not_silently_accepted():
    """Tag-pinned actions are a known policy gap; it must stay visible."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for step in _job()["steps"]:
        uses = step.get("uses")
        if uses and "@" in uses and not re.search(r"@[0-9a-f]{40}$", uses):
            assert "TODO pin by SHA" in text, \
                f"{uses} is tag-pinned with no recorded blocker"
    assert "could NOT be resolved" in text, \
        "the pinning blocker must state why, not just that"


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
        pol = json.loads((bundle / "release_trust_policy.json").read_text())
        pol["signer_identity"] = identity or pol["signer_identity"]
        pol["oidc_issuer"] = issuer or pol["oidc_issuer"]
        (bundle / "release_trust_policy.json").write_text(
            json.dumps(pol, indent=1, sort_keys=True) + "\n")
    return zp, bundle


def _online(zp, bundle):
    return subprocess.run(
        [sys.executable, "verify_release.py", "--zip", str(zp),
         "--bundle", str(bundle), "--online"],
        cwd=str(ROOT), capture_output=True, text=True)


def test_online_rejects_absent_signature(tmp_path):
    zp = tmp_path / "QTA_source.zip"
    bundle = tmp_path / "bundle"
    subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip", str(zp),
         "--make-zip", "--out", str(bundle)],
        cwd=str(ROOT), capture_output=True, text=True, check=True)
    r = _online(zp, bundle)
    assert r.returncode == 1
    assert "absence is never success" in r.stdout


def test_online_rejects_pending_identity_pins_before_touching_signature(tmp_path):
    """Policy first: a bundle signed by anyone must not pass on PENDING pins."""
    zp, bundle = _bundle_claiming_signed(tmp_path)
    r = _online(zp, bundle)
    assert r.returncode == 1
    assert "is not an exact pin" in r.stdout


def test_online_reports_missing_tooling_as_a_blocker_not_success(tmp_path):
    zp, bundle = _bundle_claiming_signed(
        tmp_path,
        identity="https://github.com/example/repo/.github/workflows/release.yml@refs/tags/v1",
        issuer="https://token.actions.githubusercontent.com")
    r = _online(zp, bundle)
    assert r.returncode == 1
    assert "absence of tooling is never success" in r.stdout


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
