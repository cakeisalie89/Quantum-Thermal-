#!/usr/bin/env python3
"""The production-readiness matrix, held to the tree and summarized.

docs/security/readiness_matrix.json says, capability by capability, whether
it is IMPLEMENTED (built, tested locally), HOSTED_TESTED (a hosted run at a
commit with this implementation passed), DEPLOYMENT_REQUIRED (a deployment
must provide it; DEPLOYMENT.md says how to check), EXTERNALLY_BLOCKED (an
owner action or outside credential) or UNVERIFIED. This refuses a matrix
that cites a test, a specification or a ledger record that does not exist,
a HOSTED_TESTED row without the runs and commit that make it so, a blocked
row that names no owner action, and a deployment row that does not point at
the deployment contract. The summary says whether the contract is met; it
is not met while any row is EXTERNALLY_BLOCKED or UNVERIFIED.

A HOSTED_TESTED row is bound to the code the run tested (D-2026-154). It
names its implementation files. ``attach`` re-checks each run against the
API with the completion matrix's own check and digests the implementation,
tests and mutation specifications from the commit's tree. ``check``
refuses the row once the working tree no longer matches that digest:
evidence about an older implementation is not evidence about this one.

A DEPLOYMENT_REQUIRED row counts toward the verdict only when the
deployment itself attests that its check passed there, in a signed
attestation ``tools/deployment_evidence.py`` validates against the owner's
deployment policy (D-2026-155). Hosted CI is not a deployment.

    python tools/readiness.py check
    python tools/readiness.py summary [--attestation A --bundle B \
        --policy P --trust-config T --commit SHA]
    python tools/readiness.py attach --commit SHA --rows REL-1,REL-2 \
        --runs 123,456 --what "what the runs covered"
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import completion_matrix as CM  # noqa: E402
import deployment_evidence as DE  # noqa: E402
MATRIX = ROOT / "docs" / "security" / "readiness_matrix.json"
LEDGER = ROOT / "docs" / "DEFECT_LEDGER.md"
STATUSES = ("IMPLEMENTED", "HOSTED_TESTED", "DEPLOYMENT_REQUIRED",
            "EXTERNALLY_BLOCKED", "UNVERIFIED")


def load(path: Path = MATRIX) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def evidence_paths(row: dict) -> list:
    """What a HOSTED_TESTED row's evidence is about: its implementation
    files, its tests and its mutation specifications."""
    return (list(row.get("implementation") or [])
            + list(row.get("tests") or [])
            + list(row.get("mutation_specs") or []))


def tree_digest(row: dict, root: Path = ROOT) -> str:
    """The row's evidence paths as they stand in ``root``."""
    def read(p):
        q = root / p
        return q.read_bytes() if q.is_file() else None
    return CM.implementation_digest(evidence_paths(row), read,
                                    lambda p: [])


def problems(doc: dict, root: Path = ROOT) -> list:
    out = []
    if set(doc.get("statuses", {})) != set(STATUSES):
        out.append(f"statuses are {sorted(doc.get('statuses', {}))}, not "
                   f"{sorted(STATUSES)}")
    ledger = (root / LEDGER.relative_to(ROOT)).read_text(encoding="utf-8")
    seen = set()
    for r in doc.get("rows", []):
        rid = r.get("id")
        if rid in seen:
            out.append(f"{rid}: listed twice")
        seen.add(rid)
        st = r.get("status")
        if st not in STATUSES:
            out.append(f"{rid}: status {st!r}")
            continue
        for p in r.get("tests", []) + r.get("mutation_specs", []):
            if not (root / p).is_file():
                out.append(f"{rid}: cites {p}, which does not exist")
        if not re.search(rf"^## {re.escape(r.get('ledger', ''))} ", ledger,
                         re.MULTILINE):
            out.append(f"{rid}: ledger record {r.get('ledger')!r} is not "
                       "in the defect ledger")
        note = r.get("note", "")
        if st == "HOSTED_TESTED":
            h = r.get("hosted") or {}
            if not h.get("runs") or not re.fullmatch(
                    r"[0-9a-f]{40}", str(h.get("commit", ""))):
                out.append(f"{rid}: HOSTED_TESTED names no hosted runs at a "
                           "commit")
            impl = r.get("implementation") or []
            if not impl:
                out.append(f"{rid}: HOSTED_TESTED names no implementation "
                           "files the run tested")
            for f in impl:
                if not (root / f).is_file():
                    out.append(f"{rid}: implementation {f} is not a file")
            if h.get("implementation_sha256") != tree_digest(r, root):
                out.append(
                    f"{rid}: HOSTED_TESTED, but its implementation, tests or "
                    "specifications are not what the run at "
                    f"{str(h.get('commit', ''))[:7]} tested; the evidence "
                    "predates them")
        if st in ("IMPLEMENTED", "HOSTED_TESTED") and not r.get("tests"):
            out.append(f"{rid}: {st} cites no test")
        if st == "EXTERNALLY_BLOCKED" and "owner action" not in note \
                and "production_registry" not in note:
            out.append(f"{rid}: EXTERNALLY_BLOCKED names no owner action or "
                       "external dependency")
        if st == "DEPLOYMENT_REQUIRED" and "DEPLOYMENT.md" not in note \
                and "cgroups" not in note:
            out.append(f"{rid}: DEPLOYMENT_REQUIRED does not point at the "
                       "deployment contract")
        if st == "DEPLOYMENT_REQUIRED" \
                and r.get("deployment_check") not in DE.CHECKS:
            out.append(f"{rid}: DEPLOYMENT_REQUIRED names no deployment "
                       f"check that satisfies it (one of {sorted(DE.CHECKS)})")
    return out


def attested(doc: dict, deployment=None) -> frozenset:
    """The DEPLOYMENT_REQUIRED rows a deployment has shown are enforced
    there (D-2026-155): covered by an attestation that
    ``tools/deployment_evidence.py`` validated, and verified with Sigstore.
    Hosted CI is not a deployment, and an attestation checked by anything
    else is a test's, not production evidence."""
    if deployment is None or deployment.verifier != DE.SIGSTORE:
        return frozenset()
    return frozenset(r["id"] for r in doc["rows"]
                     if r["status"] == "DEPLOYMENT_REQUIRED"
                     and r["id"] in deployment.rows)


def summary(doc: dict, deployment=None) -> dict:
    """The verdict. Met only when no row is blocked, unverified or only
    locally tested, AND every DEPLOYMENT_REQUIRED row is attested by the
    deployment itself (``deployment``, a validated
    :class:`deployment_evidence.DeploymentEvidence`)."""
    counts = {s: 0 for s in STATUSES}
    for r in doc["rows"]:
        counts[r["status"]] += 1
    shown = attested(doc, deployment)
    unattested = sorted(r["id"] for r in doc["rows"]
                        if r["status"] == "DEPLOYMENT_REQUIRED"
                        and r["id"] not in shown)
    met = counts["EXTERNALLY_BLOCKED"] == 0 and counts["UNVERIFIED"] == 0 \
        and counts["IMPLEMENTED"] == 0 and not unattested
    return {"rows": len(doc["rows"]), "by_status": counts,
            "deployment_unattested": unattested,
            "contract_met": met,
            "verdict": ("PRODUCTION_READINESS_CONTRACT_MET" if met else
                        "PRODUCTION_READINESS_CONTRACT_NOT_MET")}


def attach(doc: dict, rows: list, commit: str, runs: list, what: str,
           fetch=None, root: Path = ROOT) -> None:
    """Mark ``rows`` HOSTED_TESTED by ``runs`` at ``commit``: every run is
    re-checked against the API, and the digest is taken from the commit's
    tree, never from the working tree."""
    import subprocess

    import attach_hosted_evidence as AH
    full = subprocess.run(["git", "rev-parse", commit], cwd=root,
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    checked = [AH.check_run(r, full, fetch or AH._gh) for r in runs]
    read, _ = AH._at_commit(full, root)
    by_id = {r["id"]: r for r in doc["rows"]}
    for rid in rows:
        row = by_id[rid]
        if not row.get("implementation"):
            raise AH.EvidenceRefused(f"{rid} names no implementation files")
        row["status"] = "HOSTED_TESTED"
        row["hosted"] = {
            "commit": full, "runs": [c["id"] for c in checked],
            "implementation_sha256": CM.implementation_digest(
                evidence_paths(row), read, lambda p: []),
            "what": what}


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["attach"]:
        import argparse
        ap = argparse.ArgumentParser(prog="readiness.py attach")
        for flag in ("--commit", "--rows", "--runs", "--what"):
            ap.add_argument(flag, required=True)
        ns = ap.parse_args(args[1:])
        doc = load()
        attach(doc, ns.rows.split(","), ns.commit, ns.runs.split(","),
               ns.what)
        MATRIX.write_text(json.dumps(doc, indent=1, ensure_ascii=False)
                          + "\n", encoding="utf-8")
        print(f"attached {ns.rows} at {ns.commit[:12]}")
        return 0
    doc = load()
    found = problems(doc)
    if args[:1] == ["check"]:
        for f in found:
            print(f"  - {f}")
        print("readiness matrix " + ("holds" if not found else
                                     f"BROKEN ({len(found)} problem(s))"))
        return 1 if found else 0
    deployment = None
    if args[:1] == ["summary"] and len(args) > 1:
        # Deployment evidence, optional. Without it every DEPLOYMENT_REQUIRED
        # row is unattested, which is the honest answer when no deployment
        # has shown anything (D-2026-155).
        import argparse
        ap = argparse.ArgumentParser(prog="readiness.py summary")
        for flag in ("--attestation", "--bundle", "--policy",
                     "--trust-config", "--commit"):
            ap.add_argument(flag, required=True)
        ap.add_argument("--sigstore-python")
        ns = ap.parse_args(args[1:])
        try:
            deployment = DE.validate(
                Path(ns.attestation), Path(ns.bundle), Path(ns.policy),
                trust_config=Path(ns.trust_config), commit=ns.commit,
                matrix=doc, sigstore_python=ns.sigstore_python)
        except DE.Refused as exc:
            print(f"deployment evidence refused: {exc}")
    s = summary(doc, deployment)
    print(json.dumps(s, indent=1, sort_keys=True))
    return 0 if not found else 1


if __name__ == "__main__":
    sys.exit(main())
