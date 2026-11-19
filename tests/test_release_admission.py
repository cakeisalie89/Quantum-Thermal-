"""Release admission is its own decision, and nothing weaker satisfies it.

D-2026-143. ``verify --mode digests`` used to return ``accepted: True`` over
a candidate nothing had authenticated, and no outcome distinguished bytes
being consistent from a release being authorized. Verification now reports
five ordered levels, and ``admit`` -- the gate -- succeeds only when
RELEASE_AUTHORIZED is established against a RELEASE policy that neither
the bundle nor this checkout supplies.

Without an OIDC identity a test cannot make a Sigstore signature, so the
signature step is replaced by an injected checker where a test needs it to
pass. The checker is labelled in every level it touches, the CLI cannot
inject one, and each test that uses it has a twin in which the same
candidate is refused for the reason under test. The real signature path
runs in the hosted supply-chain job (offline-verify).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import offline_verify as OV  # noqa: E402
import supply_chain as SC  # noqa: E402

CI_POLICY = ROOT / "docs" / "supply_chain_ci_policy.json"
RELEASE_WORKFLOW = ".github/workflows/release.yml"


def _passes(bundle, sums, policy, *, mode, trust_config, sigstore_python):
    """Stands in for a valid Sigstore signature by the policy's signer."""
    return f"INJECTED ok for {policy['signer_identity']}"


def _refuses(bundle, sums, policy, *, mode, trust_config, sigstore_python):
    raise OV.Refused("signature verification failed (injected)")


def _trust_config(tmp: Path) -> Path:
    tc = tmp / "trust" / "trust_config.json"
    tc.parent.mkdir(parents=True, exist_ok=True)
    tc.write_text(json.dumps({"mediaType": "test", "trustedRoot": {}}))
    return tc


def _release_policy(tmp: Path, tc: Path, **over) -> Path:
    base = json.loads(CI_POLICY.read_text())
    p = {**base, "schema": OV.RELEASE_SCHEMA, "label": "RELEASE_TRUST_ROOT",
         "workflow_path": RELEASE_WORKFLOW, "ref": "refs/tags/v1.0.0",
         "issued_by": "the repository owner",
         "not_after": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
         "trust_config_sha256": hashlib.sha256(tc.read_bytes()).hexdigest()}
    p.update(over)
    p["signer_identity"] = (f"{p['source_repository']}/{p['workflow_path']}"
                            f"@{p['ref']}")
    p.update({k: v for k, v in over.items() if k == "signer_identity"})
    out = tmp / "policy" / "release_policy.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(p, indent=1))
    return out


def _placeholder_bundle(out: Path) -> None:
    """A signature bundle FILE, so verification reaches the signature
    step. Only an injected checker accepts it; Sigstore would not."""
    (out / OV.BUNDLE).write_text("{}")


@pytest.fixture(scope="module")
def rc(tmp_path_factory):
    out = tmp_path_factory.mktemp("rc")
    SC.build(out, allow_dirty=True)
    _placeholder_bundle(out)
    return out


@pytest.fixture(scope="module")
def release_rc(tmp_path_factory):
    """A candidate whose provenance names the release builder."""
    tmp = tmp_path_factory.mktemp("rel")
    tc = _trust_config(tmp)
    pol = _release_policy(tmp, tc)
    out = tmp / "rc"
    SC.build(out, allow_dirty=True, policy_path=pol)
    _placeholder_bundle(out)
    return out, pol, tc


# ---- digest-only never admits ---------------------------------------------

def test_a_digest_only_report_never_admits_a_release(rc):
    rep = SC.verify(rc, SC.load_policy(CI_POLICY), mode="digests")
    assert rep["release_authorized"] is False
    assert "accepted" not in rep
    assert rep["levels"]["SIGNATURE_VERIFIED"]["status"] == OV.NOT_CHECKED
    with pytest.raises(SC.Refused, match="digest-only verification never "
                                         "admits"):
        SC.admit(rc, CI_POLICY, mode="digests", trust_config=None)


def test_the_portable_cli_says_a_digest_run_is_not_signed(rc, tmp_path):
    """Exit 3: the checks it ran hold, the level asked for does not."""
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" /
                                                  "offline_verify.py"),
                        "verify", "--bundle", str(rc), "--policy",
                        str(CI_POLICY), "--mode", "digests"],
                       capture_output=True, text=True)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "REQUIRED LEVEL NOT ESTABLISHED: SIGNER_TRUSTED" in r.stdout
    rep = json.loads(r.stdout[:r.stdout.rindex("}") + 1])
    assert rep["highest_established"] == "PROVENANCE_STRUCTURE"


# ---- a CI signature never admits ------------------------------------------

def test_a_valid_ci_signature_is_trusted_and_still_not_a_release(rc,
                                                                 tmp_path):
    tc = _trust_config(tmp_path)
    rep = SC.verify(rc, SC.load_policy(CI_POLICY), mode="offline",
                    trust_config=tc, signature_checker=_passes, repo=None)
    lv = rep["levels"]
    assert lv["SIGNATURE_VERIFIED"]["status"] == OV.ESTABLISHED
    assert lv["SIGNER_TRUSTED"]["status"] == OV.ESTABLISHED
    assert "INJECTED" in lv["SIGNATURE_VERIFIED"]["detail"]
    assert lv["RELEASE_AUTHORIZED"]["status"] == OV.NOT_ESTABLISHED
    assert "CI_VALIDATION_ONLY" in lv["RELEASE_AUTHORIZED"]["detail"]
    outside = tmp_path / "ci_policy_copy.json"
    shutil.copy(CI_POLICY, outside)
    res = SC.admit(rc, outside, mode="offline", trust_config=tc,
                   signature_checker=_passes)
    assert res["admitted"] is False


# ---- the gate's positive control, and each way it refuses ------------------

def test_control_the_gate_admits_a_release_against_an_external_policy(
        release_rc):
    """Without this the refusals below would be vacuous: a gate that admits
    nothing refuses everything for no reason."""
    out, pol, tc = release_rc
    res = SC.admit(out, pol, mode="offline", trust_config=tc,
                   signature_checker=_passes)
    assert res["admitted"] is True
    assert res["levels"]["RELEASE_AUTHORIZED"]["status"] == OV.ESTABLISHED
    assert "INJECTED" in res["levels"]["RELEASE_AUTHORIZED"]["detail"]


def test_the_same_release_is_refused_when_the_signature_fails(release_rc):
    out, pol, tc = release_rc
    with pytest.raises(SC.Refused, match="signature verification failed"):
        SC.admit(out, pol, mode="offline", trust_config=tc,
                 signature_checker=_refuses)


def test_a_release_policy_inside_the_checkout_is_refused(release_rc):
    out, pol, tc = release_rc
    inside = ROOT / "verification" / "stage10" / "_pytest_release_policy"
    inside.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy(pol, inside / "release_policy.json")
        with pytest.raises(SC.Refused, match="inside the repository"):
            SC.admit(out, inside / "release_policy.json", mode="offline",
                     trust_config=tc, signature_checker=_passes)
    finally:
        shutil.rmtree(inside)


def test_a_release_policy_inside_the_bundle_is_refused(release_rc, tmp_path):
    out, pol, tc = release_rc
    c = tmp_path / "rc"
    shutil.copytree(out, c)
    shutil.copy(pol, c / "release_policy.json")
    with pytest.raises(SC.Refused, match="inside the bundle"):
        SC.admit(c, c / "release_policy.json", mode="offline",
                 trust_config=tc, signature_checker=_passes)


def test_a_trust_config_supplied_by_the_bundle_is_refused(release_rc,
                                                          tmp_path):
    out, pol, tc = release_rc
    c = tmp_path / "rc"
    shutil.copytree(out, c)
    shutil.copy(tc, c / "trust_config.json")
    with pytest.raises(SC.Refused, match="does not supply its own trust"):
        SC.admit(c, pol, mode="offline", trust_config=c / "trust_config.json",
                 signature_checker=_passes)


def test_a_trust_config_other_than_the_pinned_one_is_refused(release_rc,
                                                             tmp_path):
    out, pol, tc = release_rc
    other = tmp_path / "other_trust_config.json"
    other.write_text(tc.read_text() + " ")
    with pytest.raises(SC.Refused, match="not the one the release policy "
                                         "pins"):
        SC.admit(out, pol, mode="offline", trust_config=other,
                 signature_checker=_passes)


@pytest.mark.parametrize("over, why", [
    ({"ref": "refs/heads/main"}, "names a tag ref"),
    ({"workflow_path": ".github/workflows/supply-chain.yml"},
     "may not name a CI validation workflow"),
    ({"workflow_path": ".github/workflows/claude.yml"},
     "or an AI agent's workflow"),
    ({"not_after": "2000-01-01"}, "expired on 2000-01-01"),
    ({"trust_config_sha256": "PIN"}, "pins its trusted root"),
    ({"issued_by": ""}, "names who issued it"),
    ({"label": "CI_VALIDATION_ONLY"}, "carries label RELEASE_TRUST_ROOT"),
    ({"not_after": "soon"}, "says when it stops being valid"),
])
def test_a_release_policy_that_cannot_authorize_is_refused(tmp_path, over,
                                                           why):
    tc = _trust_config(tmp_path)
    with pytest.raises(OV.Refused, match=why):
        OV.load_policy(_release_policy(tmp_path, tc, **over))


def test_a_policy_of_no_known_schema_is_refused(tmp_path):
    p = json.loads(CI_POLICY.read_text())
    p["schema"] = "something-else/1"
    f = tmp_path / "p.json"
    f.write_text(json.dumps(p))
    with pytest.raises(OV.Refused, match="neither"):
        OV.load_policy(f)


def test_a_ci_policy_must_say_it_validates_only(tmp_path):
    p = json.loads(CI_POLICY.read_text())
    p["label"] = "RELEASE_TRUST_ROOT"
    f = tmp_path / "p.json"
    f.write_text(json.dumps(p))
    with pytest.raises(OV.Refused, match="CI_VALIDATION_ONLY"):
        OV.load_policy(f)


# ---- the levels are ordered --------------------------------------------------

def test_a_level_is_established_only_with_every_level_before_it():
    rep = {"levels": {n: {"status": OV.ESTABLISHED} for n in OV.LEVELS}}
    assert OV.highest(rep) == "RELEASE_AUTHORIZED"
    rep["levels"]["SIGNATURE_VERIFIED"]["status"] = OV.NOT_CHECKED
    assert OV.highest(rep) == "PROVENANCE_STRUCTURE"
    assert not OV.meets(rep, "RELEASE_AUTHORIZED")
    assert OV.meets(rep, "ARTIFACT_INTEGRITY")


def test_the_cli_never_injects_a_signature_checker():
    """The injected checker is a test seam. Nothing a command line can say
    reaches it: main() passes no checker, so the CLI's signature decision
    is always Sigstore's."""
    import ast
    tree = ast.parse((ROOT / "tools" / "offline_verify.py").read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "main")
    for node in ast.walk(main):
        if isinstance(node, ast.keyword):
            assert node.arg != "signature_checker"
