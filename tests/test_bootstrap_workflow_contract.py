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
    # The PENDING/wildcard rules moved into release_trust and became
    # STRUCTURAL: every leaf is scanned, including nested trusted_builders
    # entries, instead of two named fields being checked.
    rt = _text(os.path.join(ROOT, "release_trust.py"))
    assert "def unresolved_leaves" in rt
    assert "def wildcard_leaves" in rt
    assert "PENDING_MARKER in v.strip().upper()" in rt
    v = _text(os.path.join(ROOT, "verify_release.py"))
    # The gate moved into the EXTERNAL trust root: an unresolved policy is
    # refused as a trust root before any signature is considered.
    assert "load_trusted_policy" in v
    assert "require_resolved=True" in v
    assert "externally supplied trust root" in v


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
                if "FAILED-RELEASE-DIAGNOSTIC-EVIDENCE"
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
    assert names.index("preserve FAILED_RELEASE_DIAGNOSTIC_EVIDENCE") > online


def test_release_preservation_confers_no_trust():
    """Preserving evidence must not become authorizing it."""
    t = _executable_text(RELEASE)
    # It may not write the trust policy, and it may not flip signing status.
    # The job may PASS the canonical policy as an explicit trust root; what it
    # must not do is write one.
    for writing in ("release_trust_policy.json <<", "> QTA_stage9",
                    "tee QTA_stage9", "sed -i"):
        assert writing not in t, f"release job must not write the policy: {writing}"
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
    assert "can never equal a release" in doc or \
        "never** produce" in doc, \
        "the document must state that discovery cannot yield signer_identity"
    # And the discovery workflow must say so in its own header.
    hdr = _text(DISCOVERY)
    assert "can NEVER equal the release identity" in hdr


def test_the_document_records_the_finalizer_as_implemented():
    """The metadata blocker is closed; the document must say so.

    This test previously asserted the blocker was still open, and it fired the
    moment the finalizer landed -- which is what it was for. It now pins the
    opposite: the finalizer exists, is wired into release.yml, and the document
    no longer claims it is unimplemented.
    """
    doc = _text(BOOTSTRAP_DOC)
    assert "implemented and wired into" in doc
    assert "unimplemented" not in doc.replace(
        "said it was unimplemented", "")
    assert os.path.isfile(os.path.join(ROOT, "finalize_release_signing.py"))
    rel = _text(RELEASE)
    assert "finalize_release_signing.py" in rel


def test_the_document_states_there_is_no_bootstrap_tag():
    """The tag-A/tag-B contradiction must be recorded as resolved."""
    doc = _text(BOOTSTRAP_DOC)
    assert "There is no bootstrap release tag" in doc
    assert "can never equal the identity presented under B" in doc or \
        "never equal the identity" in doc


def test_the_document_states_the_self_reference_resolution():
    doc = _text(BOOTSTRAP_DOC)
    assert "cannot contain its own SHA" in doc
    assert "reviewed source revision" in doc
    assert "ancestor" in doc




def test_release_step_order_is_build_sign_finalize_verify_upload():
    """The finalizer must sit between signing and online verification.

    Before signing there is nothing to record; after online verification it
    would be too late, because that is the step the metadata gates. Any other
    position silently reopens the blocker.
    """
    steps = _load(RELEASE)["jobs"]["verify-and-release"]["steps"]
    labels = [str(s.get("name", "")) or str(s.get("uses", "")) for s in steps]

    def at(pred):
        return next(i for i, n in enumerate(labels) if pred(n.lower()))

    build = at(lambda n: "build release artifacts" in n)
    offline = at(lambda n: "verify (offline)" in n)
    sign = at(lambda n: "keyless sign" in n)
    finalize = at(lambda n: "finalize signing metadata" in n)
    online = at(lambda n: "online" in n and "verify" in n)
    upload = at(lambda n: "upload-artifact" in n)
    assert build < offline < sign < finalize < online < upload, labels


def test_release_finalizer_is_invoked_with_the_signed_zip_and_bundle():
    """It must finalize the same artifact that was signed, not a rebuild."""
    steps = _load(RELEASE)["jobs"]["verify-and-release"]["steps"]
    step = next(s for s in steps
                if "finalize signing metadata" in str(s.get("name", "")))
    run = str(step.get("run", ""))
    assert "finalize_release_signing.py" in run
    assert "$REL_BUNDLE" in run and "$REL_ZIP" in run
    sign = next(s for s in steps if "keyless sign" in str(s.get("name", "")))
    assert "$REL_ZIP" in str(sign.get("run", ""))
    # Same bundle filename in both steps, or the finalizer records a file the
    # signer never produced.
    assert "source.sigstore.json" in run
    assert "source.sigstore.json" in str(sign.get("run", ""))


def test_release_finalizer_step_cannot_confer_trust():
    """Metadata finalization must not be able to fill a pin."""
    steps = _load(RELEASE)["jobs"]["verify-and-release"]["steps"]
    step = next(s for s in steps
                if "finalize signing metadata" in str(s.get("name", "")))
    run = str(step.get("run", ""))
    for forbidden in ("release_trust_policy", "signer_identity", "oidc_issuer",
                      "--force", "sed ", "jq "):
        assert forbidden not in run, f"finalize step must not use {forbidden!r}"

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
    for required in ("self-reference", "IDENTITY_DISCOVERY_ONLY",
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
