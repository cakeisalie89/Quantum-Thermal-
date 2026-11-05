#!/usr/bin/env python3
"""Deployment evidence: what may satisfy a DEPLOYMENT_REQUIRED readiness row.

D-2026-155. The readiness contract says a DEPLOYMENT_REQUIRED row counts
toward production readiness only "with its deployment check passing at the
deployment". ``tools/readiness.py summary`` never asked: a matrix whose rows
were all HOSTED_TESTED except three DEPLOYMENT_REQUIRED ones, with no
deployment evidence anywhere, reported PRODUCTION_READINESS_CONTRACT_MET.

A passing CI job is not evidence about a deployment, and neither is a JSON
file somebody wrote. What this accepts is narrower:

* an ATTESTATION (``harness-deployment-attestation/1``): one deployment, in
  the production environment, running one source commit, observed at a time
  and valid until a later one, with a PASS result for each check it covers;
* a SIGNATURE over the attestation's exact bytes, verified with Sigstore
  against an exact attester identity and issuer -- never a pattern;
* a DEPLOYMENT POLICY (``harness-deployment-policy/1``), provisioned by the
  owner OUTSIDE this repository and outside the attestation's directory,
  that names that identity, the deployments it may attest, the longest an
  attestation may be valid, and the trusted root by digest.

Each of these is refused: a missing piece; a policy or attestation inside
the checkout; a test, staging or CI environment; an attester identity that
is this repository's CI or agent workflow; an expired, not-yet-valid or
over-long attestation; another deployment; another commit; a check for a row
that is not DEPLOYMENT_REQUIRED, or the wrong check for its row; any result
but PASS; and a signature that does not verify.

No deployment policy exists. Creating one is the owner's action, as the
release trust root is (``docs/security/owner_actions.md``); nothing here
creates one. Until it exists every DEPLOYMENT_REQUIRED row is unmet, and the
summary says which.

    python tools/deployment_evidence.py check
    python tools/deployment_evidence.py validate --attestation A \\
        --bundle A.sigstore.json --policy P --trust-config T \\
        --commit SHA [--sigstore-python PY]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offline_verify as OV  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ATTESTATION_SCHEMA = "harness-deployment-attestation/1"
POLICY_SCHEMA = "harness-deployment-policy/1"
POLICY_LABEL = "DEPLOYMENT_TRUST_ROOT"
ENVIRONMENT = "production"
MAX_VALIDITY_DAYS = 90

#: The check that satisfies each kind of deployment requirement, named by a
#: readiness row's ``deployment_check``. What each one observes, and how a
#: deployment runs it, is DEPLOYMENT.md section 9.
CHECKS = {
    "namespace_isolation": "a tool run records user, PID and network "
                           "namespaces in ExecutionResult.isolation",
    "filesystem_confinement": "a tool reading outside its declared paths is "
                              "refused by the kernel",
    "resource_quotas": "the deployment bounds a tool's memory, tasks, disk "
                       "and bandwidth below the host's",
}

#: The verifier a production attestation must have been checked by.
SIGSTORE = "sigstore"

Refused = OV.Refused


@dataclass(frozen=True)
class DeploymentEvidence:
    """What a validated attestation establishes: these rows' checks passed
    at this deployment, for this commit, until ``not_after``, as verified by
    ``verifier``. Only ``verifier == "sigstore"`` counts toward readiness."""

    deployment_id: str
    source_commit: str
    rows: frozenset
    not_after: str
    verifier: str
    attestation_sha256: str
    policy_sha256: str


def _instant(value, what: str) -> _dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise Refused(f"{what} is not a UTC timestamp ending in Z: "
                      f"{value!r}")
    try:
        t = _dt.datetime.fromisoformat(value[:-1])
    except ValueError:
        raise Refused(f"{what} is not an ISO 8601 timestamp: {value!r}") \
            from None
    return t.replace(tzinfo=_dt.timezone.utc)


def _inside_checkout(path: Path, root: Path) -> bool:
    return not OV._outside(path, root)


def load_policy(path: Path, *, attestation_dir: Path, root: Path = ROOT,
                today: _dt.date | None = None) -> dict:
    """The owner's deployment trust policy, or Refused."""
    path = Path(path)
    if _inside_checkout(path, root):
        raise Refused("the deployment policy lies inside the repository; a "
                      "trust policy is provisioned by the owner, outside "
                      "what it judges")
    if not OV._outside(path, attestation_dir):
        raise Refused("the deployment policy lies beside the attestation it "
                      "would judge")
    raw = OV.read_bounded(path, OV.MAX_SMALL, "deployment policy")
    p = OV._json(raw, "deployment policy")
    if p.get("schema") != POLICY_SCHEMA:
        raise Refused(f"deployment policy schema {p.get('schema')!r} is not "
                      f"{POLICY_SCHEMA}")
    if p.get("label") != POLICY_LABEL:
        raise Refused(f"a deployment policy carries label {POLICY_LABEL}")
    for k in ("attester_identity", "oidc_issuer", "issued_by"):
        v = p.get(k)
        if not isinstance(v, str) or not v or "*" in v or "PENDING" in v:
            raise Refused(f"deployment policy {k} is not an exact value: "
                          f"{v!r}")
    for wf in OV.NON_RELEASE_WORKFLOWS:
        if wf in p["attester_identity"]:
            raise Refused(
                "the deployment policy names a CI validation or AI agent "
                "workflow as attester; a CI identity says a job ran, not "
                "what a deployment enforces")
    ids = p.get("deployment_ids")
    if not isinstance(ids, list) or not ids or not all(
            isinstance(x, str) and x and "*" not in x for x in ids):
        raise Refused("a deployment policy names the deployments it covers: "
                      "deployment_ids, exact")
    days = p.get("max_validity_days")
    if not isinstance(days, int) or isinstance(days, bool) \
            or not 1 <= days <= MAX_VALIDITY_DAYS:
        raise Refused(f"a deployment policy bounds an attestation's life: "
                      f"max_validity_days, 1..{MAX_VALIDITY_DAYS}")
    if not OV._HEX64.match(str(p.get("trust_config_sha256", ""))):
        raise Refused("a deployment policy pins its trusted root: "
                      "trust_config_sha256")
    try:
        not_after = _dt.date.fromisoformat(str(p.get("not_after", "")))
    except ValueError:
        raise Refused("a deployment policy says when it stops being valid: "
                      "not_after (YYYY-MM-DD)") from None
    if (today or _dt.date.today()) > not_after:
        raise Refused(f"the deployment policy expired on {not_after}")
    return {**p, "_sha256": OV._sha(raw)}


def check_attestation(doc: dict, policy: dict, matrix: dict, *,
                      commit: str, now: _dt.datetime) -> frozenset:
    """The rows ``doc`` attests, or Refused. Signature not checked here."""
    if doc.get("schema") != ATTESTATION_SCHEMA:
        raise Refused(f"attestation schema {doc.get('schema')!r} is not "
                      f"{ATTESTATION_SCHEMA}")
    if doc.get("environment") != ENVIRONMENT:
        raise Refused(f"the attestation is about a "
                      f"{doc.get('environment')!r} environment; only a "
                      "production deployment's evidence is production "
                      "evidence")
    dep = doc.get("deployment_id")
    if dep not in policy["deployment_ids"]:
        raise Refused(f"the attestation is about deployment {dep!r}, which "
                      "the deployment policy does not cover")
    if not OV._HEX40.match(str(commit)):
        raise Refused(f"the commit being judged is not a full commit id: "
                      f"{commit!r}")
    if doc.get("source_commit") != commit:
        raise Refused(f"the attestation is about commit "
                      f"{str(doc.get('source_commit'))[:12]}, not "
                      f"{commit[:12]}; evidence about another implementation "
                      "is not evidence about this one")
    observed = _instant(doc.get("observed_at"), "observed_at")
    not_after = _instant(doc.get("not_after"), "not_after")
    if observed > now:
        raise Refused(f"the attestation was observed at {doc['observed_at']}"
                      ", which has not happened yet")
    if now > not_after:
        raise Refused(f"the attestation expired at {doc['not_after']}")
    if not_after - observed > _dt.timedelta(
            days=policy["max_validity_days"]):
        raise Refused(f"the attestation claims validity past the policy's "
                      f"{policy['max_validity_days']}-day bound")
    controls = doc.get("controls")
    if not isinstance(controls, dict) or not controls:
        raise Refused("the attestation covers no control")
    rows = {r.get("id"): r for r in matrix.get("rows", [])}
    for rid, c in sorted(controls.items()):
        row = rows.get(rid)
        if row is None or row.get("status") != "DEPLOYMENT_REQUIRED":
            raise Refused(f"the attestation covers {rid!r}, which is not a "
                          "DEPLOYMENT_REQUIRED row of the readiness matrix")
        if not isinstance(c, dict):
            raise Refused(f"control {rid} is not an object")
        if c.get("check") != row.get("deployment_check"):
            raise Refused(f"control {rid} reports check {c.get('check')!r}; "
                          f"the row is satisfied by "
                          f"{row.get('deployment_check')!r}")
        if c.get("result") != "PASS":
            raise Refused(f"control {rid} did not pass: "
                          f"{c.get('result')!r}")
    return frozenset(controls)


def validate(attestation: Path, bundle: Path, policy_path: Path, *,
             trust_config: Path, commit: str, matrix: dict,
             sigstore_python: str | None = None,
             verify_signature: Callable | None = None,
             now: _dt.datetime | None = None,
             root: Path = ROOT) -> DeploymentEvidence:
    """Validate one signed attestation against the owner's policy, or
    Refused. ``verify_signature`` replaces Sigstore only in tests, and its
    evidence is marked so that readiness does not count it."""
    attestation, bundle = Path(attestation), Path(bundle)
    if _inside_checkout(attestation, root):
        raise Refused("the attestation lies inside the repository; a "
                      "deployment's evidence comes from the deployment")
    policy = load_policy(policy_path, attestation_dir=attestation.parent,
                         root=root)
    tc = OV.read_bounded(Path(trust_config), OV.MAX_SMALL,
                         "trust configuration")
    if OV._sha(tc) != policy["trust_config_sha256"]:
        raise Refused("the trust configuration is not the one the "
                      "deployment policy pins")
    raw = OV.read_bounded(attestation, OV.MAX_SMALL, "attestation")
    doc = OV._json(raw, "attestation")
    rows = check_attestation(
        doc, policy, matrix, commit=commit,
        now=now or _dt.datetime.now(_dt.timezone.utc))
    signer = {"signer_identity": policy["attester_identity"],
              "oidc_issuer": policy["oidc_issuer"]}
    if verify_signature is None:
        OV.sigstore_check(bundle, attestation, signer, mode="offline",
                          trust_config=Path(trust_config),
                          sigstore_python=sigstore_python)
        verifier = SIGSTORE
    else:
        verify_signature(bundle, attestation, signer)
        verifier = "injected"
    return DeploymentEvidence(
        deployment_id=doc["deployment_id"], source_commit=commit,
        rows=rows, not_after=doc["not_after"], verifier=verifier,
        attestation_sha256=OV._sha(raw), policy_sha256=policy["_sha256"])


def tree_problems(matrix: dict, root: Path = ROOT) -> list:
    """What the repository itself must hold for deployment evidence to mean
    anything: every DEPLOYMENT_REQUIRED row names a known check, every check
    is documented in DEPLOYMENT.md section 9, and no attestation or
    deployment policy is committed here -- a deployment's evidence comes
    from the deployment, and one in the tree would be self-written."""
    import subprocess
    out = []
    rows = [r for r in matrix.get("rows", [])
            if r.get("status") == "DEPLOYMENT_REQUIRED"]
    if not rows:
        out.append("no DEPLOYMENT_REQUIRED row; this check would pass over "
                   "an empty set")
    for r in rows:
        if r.get("deployment_check") not in CHECKS:
            out.append(f"{r.get('id')}: deployment_check "
                       f"{r.get('deployment_check')!r} is not one of "
                       f"{sorted(CHECKS)}")
    doc = (root / "DEPLOYMENT.md").read_text(encoding="utf-8")
    nine = doc.split("## 9.", 1)[1] if "## 9." in doc else ""
    for check in sorted(CHECKS):
        if f"`{check}`" not in nine:
            out.append(f"check {check!r} is not documented in DEPLOYMENT.md "
                       "section 9")
    tracked = subprocess.run(["git", "ls-files", "-z", "*.json"], cwd=root,
                             capture_output=True, check=True).stdout
    for name in filter(None, tracked.decode().split("\0")):
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for schema in (ATTESTATION_SCHEMA, POLICY_SCHEMA):
            if f'"{schema}"' in text:
                out.append(f"{name} declares {schema}; deployment evidence "
                           "and its trust policy are never committed here")
    return out


def main(argv=None) -> int:
    import readiness as RD
    ap = argparse.ArgumentParser(prog="deployment_evidence.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    v = sub.add_parser("validate")
    for flag in ("--attestation", "--bundle", "--policy", "--trust-config",
                 "--commit"):
        v.add_argument(flag, required=True)
    v.add_argument("--sigstore-python")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        found = tree_problems(RD.load())
        for f in found:
            print(f"  - {f}")
        print("deployment evidence contract " + (
            "holds: every deployment row names a documented check, and no "
            "attestation is committed" if not found
            else f"BROKEN ({len(found)} problem(s))"))
        return 1 if found else 0
    try:
        ev = validate(Path(a.attestation), Path(a.bundle), Path(a.policy),
                      trust_config=Path(a.trust_config), commit=a.commit,
                      matrix=RD.load(), sigstore_python=a.sigstore_python)
    except Refused as exc:
        print(f"DEPLOYMENT EVIDENCE REFUSED: {exc}")
        return 3
    print(json.dumps({"deployment_id": ev.deployment_id,
                      "rows": sorted(ev.rows), "not_after": ev.not_after,
                      "verifier": ev.verifier}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
