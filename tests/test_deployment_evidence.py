"""Deployment evidence is what may satisfy a DEPLOYMENT_REQUIRED row.

D-2026-155. ``tools/readiness.py summary`` reported the production contract
MET for a matrix whose only open rows were three DEPLOYMENT_REQUIRED ones,
with no deployment evidence anywhere (measured at b9b2187). The contract
said such a row counts only "with its deployment check passing at the
deployment"; nothing asked.

Every refusal below is paired with the control it differs from by one
thing: an attestation built by ``_world`` that passes every check, signed
as far as an injected verifier can say. The injected verifier exists so the
other checks can be exercised without a deployment's signing identity --
and its evidence is marked, and never counts toward readiness.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import deployment_evidence as DE  # noqa: E402
import readiness as RD  # noqa: E402

COMMIT = "a" * 40
NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.timezone.utc)
IDENTITY = "https://deploy.example/attester@refs/tags/v1"
ISSUER = "https://issuer.example"


def _matrix() -> dict:
    return copy.deepcopy(RD.load())


def _attestation(**over) -> dict:
    doc = {"schema": DE.ATTESTATION_SCHEMA, "deployment_id": "prod-1",
           "environment": "production", "source_commit": COMMIT,
           "observed_at": "2026-10-18T00:00:00Z",
           "not_after": "2026-10-25T00:00:00Z",
           "controls": {"RUN-2": {"check": "namespace_isolation",
                                  "result": "PASS",
                                  "observation": {"namespaces":
                                                  ["user", "pid", "net"]}}}}
    doc.update(over)
    return doc


def _policy(trust: bytes, **over) -> dict:
    p = {"schema": DE.POLICY_SCHEMA, "label": DE.POLICY_LABEL,
         "issued_by": "owner", "attester_identity": IDENTITY,
         "oidc_issuer": ISSUER, "deployment_ids": ["prod-1"],
         "max_validity_days": 30,
         "trust_config_sha256": hashlib.sha256(trust).hexdigest(),
         "not_after": "2099-01-01"}
    p.update(over)
    return p


def _world(tmp_path, *, attestation=None, policy=None):
    """Attestation and bundle in one directory, the owner's policy and the
    trusted root in another -- both outside the repository."""
    dep, owner = tmp_path / "deployment", tmp_path / "owner"
    dep.mkdir()
    owner.mkdir()
    trust = b'{"trusted_root": "fixture"}'
    (owner / "trust.json").write_bytes(trust)
    (dep / "attestation.json").write_text(json.dumps(
        attestation if attestation is not None else _attestation()))
    (dep / "attestation.sigstore.json").write_text("{}")
    (owner / "policy.json").write_text(json.dumps(
        policy if policy is not None else _policy(trust)))
    return dep, owner, trust


def _signed(*_):
    return "injected: signature accepted"


def _validate(dep, owner, *, verify=_signed, commit=COMMIT, matrix=None,
              now=NOW):
    return DE.validate(
        dep / "attestation.json", dep / "attestation.sigstore.json",
        owner / "policy.json", trust_config=owner / "trust.json",
        commit=commit, matrix=matrix or _matrix(), verify_signature=verify,
        now=now)


# ---- the regression ---------------------------------------------------------

def test_unattested_deployment_rows_do_not_meet_the_contract():
    """The defect as measured: every other row hosted, three deployment rows
    with no evidence, and the summary said MET."""
    doc = _matrix()
    for r in doc["rows"]:
        if r["status"] != "DEPLOYMENT_REQUIRED":
            r["status"] = "HOSTED_TESTED"
    s = RD.summary(doc)
    assert s["contract_met"] is False
    assert s["deployment_unattested"] == ["RUN-2", "RUN-3", "RUN-4"]


def test_control_deployment_rows_attested_with_sigstore_meet_the_contract():
    """The verdict can be met, so NOT_MET above is a finding about evidence
    and not a constant. Built directly: no production attestation exists,
    and this test does not pretend one does."""
    doc = _matrix()
    for r in doc["rows"]:
        if r["status"] != "DEPLOYMENT_REQUIRED":
            r["status"] = "HOSTED_TESTED"
    ev = DE.DeploymentEvidence(
        deployment_id="prod-1", source_commit=COMMIT,
        rows=frozenset({"RUN-2", "RUN-3", "RUN-4"}), not_after="x",
        verifier=DE.SIGSTORE, attestation_sha256="0" * 64,
        policy_sha256="0" * 64)
    assert RD.summary(doc, ev)["contract_met"] is True
    partial = DE.DeploymentEvidence(**{**ev.__dict__,
                                       "rows": frozenset({"RUN-2"})})
    assert RD.summary(doc, partial)["deployment_unattested"] == [
        "RUN-3", "RUN-4"]


def test_hosted_ci_does_not_attest_a_deployment_row():
    """A deployment row is not satisfied by its tests passing hosted: the
    row keeps its status and stays unattested."""
    doc = _matrix()
    s = RD.summary(doc)
    assert "RUN-2" in s["deployment_unattested"]


# ---- validation -------------------------------------------------------------

def test_control_a_complete_attestation_validates(tmp_path):
    dep, owner, _ = _world(tmp_path)
    ev = _validate(dep, owner)
    assert ev.rows == frozenset({"RUN-2"})
    assert ev.deployment_id == "prod-1"


def test_evidence_checked_by_an_injected_verifier_never_counts(tmp_path):
    """The signature check a test can substitute is the one thing a test
    cannot substitute for production: its evidence is marked, and readiness
    does not count it."""
    dep, owner, _ = _world(tmp_path)
    ev = _validate(dep, owner)
    assert ev.verifier == "injected"
    doc = _matrix()
    assert RD.attested(doc, ev) == frozenset()
    assert "RUN-2" in RD.summary(doc, ev)["deployment_unattested"]


def test_without_a_sigstore_runtime_validation_fails_closed(tmp_path):
    dep, owner, _ = _world(tmp_path)
    with pytest.raises(DE.Refused, match="no sigstore runtime"):
        _validate(dep, owner, verify=None)


def _refuses(tmp_path, why, **kw):
    dep, owner, _ = _world(tmp_path, **kw)
    with pytest.raises(DE.Refused, match=why):
        _validate(dep, owner)


@pytest.mark.parametrize("over, why", [
    ({"environment": "test"}, "production"),
    ({"environment": "staging"}, "production"),
    ({"deployment_id": "prod-2"}, "does not cover"),
    ({"source_commit": "b" * 40}, "another implementation"),
    ({"not_after": "2026-10-18T06:00:00Z"}, "expired"),
    ({"observed_at": "2026-10-19T00:00:00Z"}, "has not happened"),
    ({"not_after": "2027-01-01T00:00:00Z"}, "30-day bound"),
    ({"observed_at": "yesterday"}, "UTC timestamp"),
    ({"controls": {}}, "covers no control"),
    ({"controls": {"RUN-2": {"check": "namespace_isolation",
                             "result": "FAIL"}}}, "did not pass"),
    ({"controls": {"RUN-2": {"check": "resource_quotas",
                             "result": "PASS"}}}, "satisfied by"),
    ({"controls": {"REL-1": {"check": "namespace_isolation",
                             "result": "PASS"}}}, "not a DEPLOYMENT_REQUIRED"),
    ({"schema": "harness-deployment-attestation/0"}, "schema"),
])
def test_an_attestation_wrong_in_one_way_is_refused(tmp_path, over, why):
    _refuses(tmp_path, why, attestation=_attestation(**over))


@pytest.mark.parametrize("over, why", [
    ({"attester_identity": "https://deploy.example/*"}, "exact"),
    ({"attester_identity":
      "https://github.com/o/r/.github/workflows/supply-chain.yml@refs/heads/x"},
     "CI validation or AI agent"),
    ({"attester_identity":
      "https://github.com/o/r/.github/workflows/claude.yml@refs/heads/x"},
     "CI validation or AI agent"),
    ({"deployment_ids": []}, "deployment_ids"),
    ({"max_validity_days": 365}, "max_validity_days"),
    ({"not_after": "2020-01-01"}, "expired"),
    ({"label": "CI_VALIDATION_ONLY"}, "label"),
    ({"trust_config_sha256": "0" * 64}, "pins"),
])
def test_a_policy_wrong_in_one_way_is_refused(tmp_path, over, why):
    trust = b'{"trusted_root": "fixture"}'
    _refuses(tmp_path, why, policy=_policy(trust, **over))


def test_a_forged_signature_is_refused(tmp_path):
    dep, owner, _ = _world(tmp_path)

    def forged(*_):
        raise DE.Refused("signature verification failed (offline)")
    with pytest.raises(DE.Refused, match="signature verification failed"):
        _validate(dep, owner, verify=forged)


def test_the_signature_is_checked_against_the_policys_identity(tmp_path):
    dep, owner, _ = _world(tmp_path)
    seen = {}

    def record(bundle, signed, signer):
        seen.update(signer, file=Path(signed).name)
    _validate(dep, owner, verify=record)
    assert seen == {"signer_identity": IDENTITY, "oidc_issuer": ISSUER,
                    "file": "attestation.json"}


def test_a_policy_beside_the_attestation_is_refused(tmp_path):
    dep, owner, _ = _world(tmp_path)
    (dep / "policy.json").write_bytes((owner / "policy.json").read_bytes())
    with pytest.raises(DE.Refused, match="beside the attestation"):
        DE.validate(dep / "attestation.json",
                    dep / "attestation.sigstore.json", dep / "policy.json",
                    trust_config=owner / "trust.json", commit=COMMIT,
                    matrix=_matrix(), verify_signature=_signed, now=NOW)


def test_evidence_inside_the_repository_is_refused(tmp_path):
    """A JSON status the repository wrote about itself is not a deployment's
    evidence, wherever it claims to come from."""
    dep, owner, _ = _world(tmp_path)
    with pytest.raises(DE.Refused, match="inside the repository"):
        DE.validate(ROOT / "docs" / "security" / "readiness_matrix.json",
                    dep / "attestation.sigstore.json",
                    owner / "policy.json", trust_config=owner / "trust.json",
                    commit=COMMIT, matrix=_matrix(),
                    verify_signature=_signed, now=NOW)
    with pytest.raises(DE.Refused, match="inside the repository"):
        DE.validate(dep / "attestation.json",
                    dep / "attestation.sigstore.json",
                    ROOT / "docs" / "supply_chain_ci_policy.json",
                    trust_config=owner / "trust.json", commit=COMMIT,
                    matrix=_matrix(), verify_signature=_signed, now=NOW)


def test_a_missing_attestation_is_refused(tmp_path):
    dep, owner, _ = _world(tmp_path)
    (dep / "attestation.json").unlink()
    with pytest.raises(DE.Refused, match="absent"):
        _validate(dep, owner)


def test_every_deployment_row_names_the_check_that_satisfies_it():
    doc = _matrix()
    rows = [r for r in doc["rows"] if r["status"] == "DEPLOYMENT_REQUIRED"]
    assert rows and all(r.get("deployment_check") in DE.CHECKS
                        for r in rows)
    rows[0]["deployment_check"] = "it_works_on_my_machine"
    assert any("names no deployment check" in p for p in RD.problems(doc))


# ---- what the repository itself must hold ---------------------------------------

def _tree(tmp_path, *, documented=tuple(DE.CHECKS), commit=()):
    import subprocess
    root = tmp_path / "repo"
    root.mkdir()
    rows = "".join(f"| `{c}` | x | y |\n" for c in documented)
    (root / "DEPLOYMENT.md").write_text(f"# D\n\n## 9. Evidence\n\n{rows}")
    for name, doc in commit:
        (root / name).write_text(json.dumps(doc))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=root, check=True, env=env,
                       capture_output=True)
    return root


def test_control_the_shipped_tree_holds_the_deployment_contract():
    assert DE.tree_problems(_matrix()) == []


def test_control_a_tree_documenting_every_check_holds(tmp_path):
    assert DE.tree_problems(_matrix(), _tree(tmp_path)) == []


def test_a_committed_attestation_or_policy_is_refused(tmp_path):
    """The case the contract exists against: evidence the repository wrote
    about its own deployment."""
    root = _tree(tmp_path, commit=[
        ("evidence.json", _attestation()),
        ("policy.json", _policy(b"x"))])
    found = DE.tree_problems(_matrix(), root)
    assert any("evidence.json" in f and DE.ATTESTATION_SCHEMA in f
               for f in found)
    assert any("policy.json" in f and DE.POLICY_SCHEMA in f for f in found)


def test_a_check_the_deployment_contract_does_not_document_is_refused(
        tmp_path):
    root = _tree(tmp_path, documented=("namespace_isolation",))
    found = DE.tree_problems(_matrix(), root)
    assert any("filesystem_confinement" in f for f in found)
    assert any("resource_quotas" in f for f in found)
