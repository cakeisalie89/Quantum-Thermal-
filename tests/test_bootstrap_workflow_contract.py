"""The bootstrap and container workflows must not become trust shortcuts.

release.yml cannot bootstrap its own trust: it verifies against a policy that
fails closed on PENDING, and that verification runs before upload-artifact, so
the first signing run both fails and destroys the certificate it would have been
pinned from. identity-discovery.yml breaks that loop by separating discovery
from authorization -- and the whole point is lost the moment it starts deciding
its own identity is acceptable. These tests pin that separation.

container-verify.yml is held to a different line: it must not acquire privilege
it does not need, and it must test the declared artifact rather than a
convenient substitute.

MODEL-ONLY / FORECAST-ONLY. Nothing here asserts a scientific value.
"""
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WF = os.path.join(ROOT, ".github", "workflows")
DISCOVERY = os.path.join(WF, "identity-discovery.yml")
CONTAINER = os.path.join(WF, "container-verify.yml")
RELEASE = os.path.join(WF, "release.yml")
BOOTSTRAP_DOC = os.path.join(ROOT, "SIGNING_BOOTSTRAP.md")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _on(doc):
    # PyYAML parses the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def _executable_text(path):
    """Workflow text with comment lines stripped.

    The header comments deliberately NAME the things the workflow must not do
    ("does not write release_trust_policy.json"), so a naive substring scan
    flags the documentation for describing the very prohibition it documents.
    What matters is the executable body.
    """
    out = []
    for line in _text(path).splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  #")[0] if "  #" in line else line)
    return "\n".join(out)

# ------------------------------------------------- discovery is untrusted ----

def test_discovery_is_manual_only_and_never_tag_triggered():
    """A tag trigger would put it on the release path it must stay off."""
    on = _on(_load(DISCOVERY))
    assert set(on) == {"workflow_dispatch"}, on


def test_discovery_takes_the_minimum_privilege_for_oidc():
    doc = _load(DISCOVERY)
    assert doc["permissions"] == {}, "top level must be default-deny"
    perms = doc["jobs"]["discover"]["permissions"]
    assert perms == {"contents": "read", "id-token": "write"}, perms


def test_discovery_never_writes_the_trust_policy():
    """Observation of an identity must not become authorization of it."""
    t = _executable_text(DISCOVERY)
    for forbidden in ("release_trust_policy", "signer_identity:", "oidc_issuer:",
                      "git commit", "git push", "sed -i", "tee release"):
        assert forbidden not in t, f"discovery workflow touches {forbidden!r}"


def test_discovery_does_not_sign_the_release_artifact():
    """It signs a throwaway subject so nothing can be promoted to a release."""
    t = _executable_text(DISCOVERY)
    assert "IDENTITY_DISCOVERY_SUBJECT" in t
    assert "QTA_source.zip" not in t
    assert "REL_ZIP" not in t


def test_discovery_labels_its_output_untrusted_structurally():
    """The artifact NAME carries the status, not a description someone may skip."""
    doc = _load(DISCOVERY)
    steps = doc["jobs"]["discover"]["steps"]
    uploads = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    assert uploads, "no artifact upload; the evidence would not survive"
    for u in uploads:
        assert u["with"]["name"] == "UNTRUSTED-BOOTSTRAP-IDENTITY-EVIDENCE", u
    assert "IDENTITY_DISCOVERY_ONLY" in _text(DISCOVERY)


def test_discovery_preserves_evidence_even_when_a_step_fails():
    doc = _load(DISCOVERY)
    steps = doc["jobs"]["discover"]["steps"]
    uploads = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    for u in uploads:
        assert str(u.get("if", "")).strip() == "always()", (
            "bootstrap evidence must survive an upstream failure")


def test_discovery_runs_no_verification_that_could_authorize_it():
    t = _executable_text(DISCOVERY)
    assert "verify_release.py" not in t
    assert "--online" not in t


# ------------------------------------------- the release path stays closed ----

def test_release_still_fails_closed_on_pending_pins():
    """The bootstrap must not have loosened what it exists to work around."""
    v = _text(os.path.join(ROOT, "verify_release.py"))
    assert 'value.startswith("PENDING")' in v
    assert '"*" in value' in v


def test_release_is_still_tag_triggered_only():
    on = _on(_load(RELEASE))
    assert set(on) == {"push"}, on
    assert on["push"]["tags"] == ["qta-stage*"]


def test_no_workflow_introduces_a_wildcard_identity():
    for path in (DISCOVERY, CONTAINER, RELEASE):
        t = _executable_text(path)
        for bad in ("signer_identity: '*'", 'signer_identity: "*"',
                    "--certificate-identity '*'", "certificate-identity-regexp"):
            assert bad not in t, f"{os.path.basename(path)} contains {bad!r}"



def test_release_preserves_its_untrusted_bundle_when_verification_fails():
    """The bootstrap's load-bearing step.

    Online verification MUST fail while the pins are PENDING, and it runs
    before the release upload -- so without this step the Sigstore bundle,
    the only artifact carrying the real certificate, dies with the runner.
    That is what made the loop circular in the first place.
    """
    steps = _load(RELEASE)["jobs"]["verify-and-release"]["steps"]
    preserve = [s for s in steps
                if "UNTRUSTED-RELEASE-IDENTITY-EVIDENCE"
                in str(s.get("with", {}).get("name", ""))]
    assert len(preserve) == 1, "expected exactly one untrusted-preservation step"
    step = preserve[0]
    # if: failure() -- it must run precisely when verification rejected, and
    # must not fire on a clean run where the trusted upload already happened.
    assert str(step.get("if", "")).strip() == "failure()", step.get("if")
    assert step["uses"].startswith("actions/upload-artifact@")

    # It must come after the online verification step, or it preserves nothing.
    names = [str(s.get("name", "")) for s in steps]
    online = next(i for i, n in enumerate(names) if "online" in n.lower())
    assert names.index("preserve UNTRUSTED bundle when verification fails") > online


def test_release_preservation_confers_no_trust():
    """Preserving evidence must not become authorizing it."""
    t = _executable_text(RELEASE)
    # It may not write the trust policy, and it may not flip signing status.
    assert "release_trust_policy" not in t.replace(
        "verify_release.py", ""), "release job must not write the trust policy"
    for forbidden in ('signing_status": "SIGNED"', "signing_status=SIGNED",
                      "--certificate-identity-regexp"):
        assert forbidden not in t, forbidden
    # The job still grants only checkout + OIDC.
    perms = _load(RELEASE)["jobs"]["verify-and-release"]["permissions"]
    assert perms == {"contents": "read", "id-token": "write"}, perms


def test_discovery_does_not_claim_to_supply_the_release_identity():
    """A SAN under identity-discovery.yml@refs/heads/... can never equal
    release.yml@refs/tags/..., so nothing may promise that it can."""
    doc = _text(BOOTSTRAP_DOC)
    assert "can **never** produce the exact" in doc or \
           "never** produce the exact" in doc, \
        "the document must state that discovery cannot yield signer_identity"
    # And the discovery workflow must say so in its own header.
    hdr = _text(DISCOVERY)
    assert "can NEVER equal the release identity" in hdr


def test_stage3_records_the_second_independent_blocker():
    """Filling the pins is necessary but not sufficient: signing_status stays
    PENDING and signature_bundles stays empty, so --online rejects first."""
    doc = _text(BOOTSTRAP_DOC)
    assert "necessary but not sufficient" in doc
    assert "signature_bundles" in doc and "signing_status" in doc
    # And the underlying facts must still hold, or the note is stale.
    b = _text(os.path.join(ROOT, "build_release_artifacts.py"))
    assert '"signing_status": "PENDING"' in b
    assert '"signature_bundles": []' in b
    v = _text(os.path.join(ROOT, "verify_release.py"))
    assert 'status != "SIGNED" or not sig' in v

# ------------------------------------------------ container verification ----

def test_container_verify_is_manual_and_read_only():
    doc = _load(CONTAINER)
    assert set(_on(doc)) == {"workflow_dispatch"}
    assert doc["permissions"] == {}
    perms = doc["jobs"]["container-verify"]["permissions"]
    assert perms == {"contents": "read"}, (
        "container verification needs no token, no id-token and no write scope")
    assert "id-token" not in perms


def test_container_verify_tests_the_declared_image_not_a_substitute():
    t = _executable_text(CONTAINER)
    # the base is read from the Dockerfile so the test cannot drift from it
    assert "grep -E '^FROM ' Dockerfile" in t
    assert "docker build -t qta:hosted-verify ." in t
    # and it must not quietly swap in a different base
    assert not re.search(r"FROM\s+python:", t), (
        "the workflow restates a base image instead of reading the Dockerfile")


def test_container_verify_asserts_the_non_root_user_and_no_host_venv():
    t = _executable_text(CONTAINER)
    assert '[ "$U" = "qta" ]' in t
    assert "/opt/venv" in t
    assert ".venv" not in t, "must not mount or reference a host virtualenv"


def test_container_verify_cannot_move_scientific_authority():
    """Read-only, and it writes nothing back into the repository."""
    t = _executable_text(CONTAINER)
    for forbidden in ("git push", "git commit", "generate_manifest.py",
                      "results_gate_table.csv"):
        assert forbidden not in t, f"container workflow touches {forbidden!r}"


def test_every_action_in_the_new_workflows_is_sha_pinned():
    for path in (DISCOVERY, CONTAINER):
        for m in re.finditer(r"uses:\s*([^\s@]+)@([^\s#]+)", _text(path)):
            ref = m.group(2)
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"{os.path.basename(path)}: {m.group(1)}@{ref} is not a commit SHA")


def test_the_bootstrap_document_exists_and_states_the_circularity():
    doc = os.path.join(ROOT, "SIGNING_BOOTSTRAP.md")
    assert os.path.isfile(doc)
    t = open(doc, encoding="utf-8").read()
    for required in ("circularity", "IDENTITY_DISCOVERY_ONLY",
                     "UNTRUSTED-BOOTSTRAP-IDENTITY-EVIDENCE",
                     "token.actions.githubusercontent.com", "PENDING"):
        assert required in t, f"bootstrap doc does not mention {required!r}"
    assert "trust-on-first-use" in t.lower() or "TOFU" in t


if __name__ == "__main__":
    ns = dict(globals())
    for _n, _f in ns.items():
        if _n.startswith("test_") and callable(_f):
            _f()
    print("RESULT: bootstrap and container workflow contracts hold")
