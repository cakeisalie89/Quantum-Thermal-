#!/usr/bin/env python3
"""Deterministic release-artifact generator (Stage 9).

Produces, into a target directory: release_index.json, SHA256SUMS,
sbom.cdx.json (CycloneDX 1.5 generated from uv.lock and cross-validated
against it), provenance.intoto.json (in-toto Statement v1 with an
SLSA-provenance-v1-aligned predicate; builder.id=local-sandbox unless
--ci; NO SLSA level is ever claimed here), release_trust_policy.json
(exact identity pins; wildcards forbidden), and VERIFY_INSTRUCTIONS.md.

Signature bundles are NEVER fabricated: if Sigstore signing has not
actually run, the index records signing_status="PENDING" with the exact
blockers, and no signature file is written.

MODEL-ONLY / FORECAST-ONLY: a signature proves origin/integrity only;
scientific gate PASS remains zero.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LBL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
STAGE8 = {"name": "QTA_stage8_hdf5_provenance_source.zip",
          "size": 20680532,
          "sha256": "809baa0804cc2bda896c91706e3090354ad6a8f378771801"
                     "e9d81c13fccdcfd0"}
SIGNING_BLOCKERS = [
    "no cosign/gitsign binary in sandbox",
    "sigstore-python not installed in the frozen environment "
    "(installable, but:)",
    "Fulcio/Rekor unreachable: egress allowlist returns HTTP 403",
    "no usable OIDC identity flow without Fulcio",
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def parse_uv_lock(lock: Path) -> list:
    pkgs: list = []
    cur: dict | None = None
    for line in lock.read_text().splitlines():
        if line.strip() == "[[package]]":
            cur = {}
            pkgs.append(cur)
        elif cur is not None:
            m = re.match(r'(name|version) = "([^"]+)"', line.strip())
            if m:
                cur[m.group(1)] = m.group(2)
    out = sorted({(p["name"], p["version"]) for p in pkgs
                  if "name" in p and "version" in p})
    return [{"name": n, "version": v} for n, v in out]


def build_sbom(lock: Path) -> dict:
    comps = [{"type": "library", "name": p["name"],
              "version": p["version"],
              "purl": f"pkg:pypi/{p['name']}@{p['version']}",
              "bom-ref": f"pkg:pypi/{p['name']}@{p['version']}"}
             for p in parse_uv_lock(lock)]
    serial = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                                          "qta-sbom:" + sha(lock)))
    return {"bomFormat": "CycloneDX", "specVersion": "1.5",
            "serialNumber": serial, "version": 1,
            "metadata": {"component": {"type": "application",
                                        "name": "qta-multiphysics",
                                        "version": "0.9.0"},
                         "properties": [
                             {"name": "qta:label", "value": LBL},
                             {"name": "qta:uv_lock_sha256",
                              "value": sha(lock)}]},
            "components": comps}


def validate_sbom_against_lock(sbom: dict, lock: Path) -> list:
    want = {(p["name"], p["version"]) for p in parse_uv_lock(lock)}
    got = {(c["name"], c["version"]) for c in sbom["components"]}
    problems = []
    if want - got:
        problems.append(f"SBOM missing {sorted(want - got)[:3]}")
    if got - want:
        problems.append(f"SBOM extras {sorted(got - want)[:3]}")
    for pin in (("numpy", "2.4.4"), ("scipy", "1.17.1"),
                ("qutip", "5.2.1"), ("h5py", "3.16.0")):
        if pin not in got:
            problems.append(f"scientific pin absent from SBOM: {pin}")
    return problems


def build_provenance(subjects: list, ci: bool) -> dict:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": ("https://github.com/actions/workflow" if ci
                              else "qta:local-sandbox-build/v1"),
                "externalParameters": {
                    "authoritative_input": STAGE8,
                    "release_policy": "RELEASE_POLICY.md",
                },
                "resolvedDependencies": [
                    {"uri": "uv.lock",
                     "digest": {"sha256": sha(Path("uv.lock"))}},
                    {"uri": "final_manifest.json",
                     "digest": {"sha256":
                                sha(Path("final_manifest.json"))}},
                ],
            },
            "runDetails": {
                "builder": {"id": ("PENDING-hosted-runner" if ci else
                                   "qta:local-sandbox")},
                "metadata": {"invocationId": "deterministic-local",
                              "note": "NO SLSA level claimed; local "
                                      "provenance until a qualifying "
                                      "hosted build runs and verifies"},
            },
        },
        "qta_claims": {"scientific_gate_PASS_count": 0,
                        "can_PASS_now": "NO",
                        "measured_in_this_system": False,
                        "signature_semantics":
                            "origin and integrity only; never physics or "
                            "hardware validation"},
    }


def trust_policy() -> dict:
    return {"schema_version": "1.0.0",
            "wildcards_forbidden": True,
            "trusted_builders": ["qta:local-sandbox (unsigned local "
                                  "provenance only; carries no trust)",
                                  "PENDING: pinned hosted workflow id "
                                  "after first real run"],
            "source_repository":
                "https://github.com/cakeisalie89/Quantum-Thermal-",
            "pinned_revision": "PENDING: set at first signed release",
            "workflow_path": ".github/workflows/release.yml",
            "signer_identity": "PENDING: exact certificate identity at "
                                "first Sigstore signing",
            "oidc_issuer": "PENDING: exact issuer at first signing",
            "note": "every PENDING must be replaced by an exact value "
                    "before any signature is trusted; an asterisk "
                    "wildcard anywhere in this file is itself a "
                    "verification failure"}


#: Fixed timestamp for every zip entry. A release zip whose digest changes
#: because it was built a second later is not a reproducible artifact, and the
#: whole point of SHA256SUMS/provenance is that the digest is stable.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Trees excluded from the source zip: historical delivery archives and the
#: regeneration target. Everything else git tracks is source.
ZIP_EXCLUDE_PREFIXES = ("attic/", "outputs/", "release/", "release_bundle/")
ZIP_EXCLUDE_SUFFIXES = (".bundle", ".zip", ".tar.gz", ".patch")


def make_source_zip(dest: Path, prefix: str | None = None) -> Path:
    """Build the release zip deterministically from the git index.

    Nothing in the repository built this file, so the release workflow signed a
    path that no earlier step produced. Entries are sorted, timestamps fixed
    and external attributes normalised, so two builds of the same commit
    produce byte-identical archives.

    Every entry sits under a single top-level directory, which is the layout
    verify_release.py reads (it takes names[0]'s first path component as the
    archive root and resolves ``<root>/uv.lock`` and
    ``<root>/results_gate_table.csv`` inside it).
    """
    import subprocess
    import zipfile
    tracked = sorted(subprocess.run(
        ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True,
        check=True).stdout.split("\n"))
    members = [f for f in tracked if f
               and not f.startswith(ZIP_EXCLUDE_PREFIXES)
               and not f.endswith(ZIP_EXCLUDE_SUFFIXES)]
    root = prefix or dest.name[:-4] if dest.name.endswith(".zip") else dest.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as z:
        for rel in members:
            info = zipfile.ZipInfo(f"{root}/{rel}", date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, (ROOT / rel).read_bytes())
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True,
                    help="path to the release zip; with --make-zip it is "
                         "built here, otherwise it must already exist")
    ap.add_argument("--make-zip", action="store_true",
                    help="build the release zip deterministically from the "
                         "git index before assembling the bundle")
    ap.add_argument("--out", default="release_bundle")
    ap.add_argument("--ci", action="store_true")
    a = ap.parse_args()
    zp = Path(a.zip)
    if a.make_zip:
        make_source_zip(zp)
        print(f"[zip] {zp} ({zp.stat().st_size} bytes, sha256 {sha(zp)[:16]}...)")
    if not zp.exists():
        print(f"[FAIL-CLOSED] release zip missing: {zp} "
              "(pass --make-zip to build it)")
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    key_files = ["final_manifest.json", "manifest_hash.txt", "uv.lock",
                 "qta_scientific_results.h5",
                 "ro-crate/ro-crate-metadata.json",
                 "hdf5_output_mapping.json", "hdf5_schema.json",
                 "results_gate_table.csv", "RELEASE_POLICY.md",
                 "SUPPLY_CHAIN_THREAT_MODEL.md"]
    entries = [{"name": zp.name, "size": zp.stat().st_size,
                "sha256": sha(zp)}]
    for f in key_files:
        p = Path(f)
        entries.append({"name": f, "size": p.stat().st_size,
                        "sha256": sha(p)})
    sums = "".join(f"{e['sha256']}  {e['name']}\n"
                   for e in sorted(entries,
                                   key=lambda x: str(x["name"])))
    (out / "SHA256SUMS").write_text(sums)
    sbom = build_sbom(Path("uv.lock"))
    problems = validate_sbom_against_lock(sbom, Path("uv.lock"))
    if problems:
        print(f"[FAIL-CLOSED] SBOM validation: {problems}")
        return 1
    (out / "sbom.cdx.json").write_text(
        json.dumps(sbom, indent=1, sort_keys=True) + "\n")
    subjects = [{"name": e["name"],
                 "digest": {"sha256": e["sha256"]}} for e in entries]
    prov = build_provenance(subjects, a.ci)
    (out / "provenance.intoto.json").write_text(
        json.dumps(prov, indent=1, sort_keys=True) + "\n")
    (out / "release_trust_policy.json").write_text(
        json.dumps(trust_policy(), indent=1, sort_keys=True) + "\n")
    for doc in ("RELEASE_POLICY.md",):
        (out / doc).write_text(Path(doc).read_text())
    index = {"schema_version": "1.0.0", "label": LBL,
             "release_artifact": entries[0],
             "authoritative_input": STAGE8,
             "files": entries,
             "sbom": {"file": "sbom.cdx.json",
                      "sha256": sha(out / "sbom.cdx.json"),
                      "validated_against": "uv.lock",
                      "components": len(sbom["components"])},
             "provenance": {"file": "provenance.intoto.json",
                            "sha256":
                                sha(out / "provenance.intoto.json"),
                            "slsa_level_claimed": "NONE",
                            "builder": prov["predicate"]["runDetails"]
                            ["builder"]["id"]},
             "signing_status": "PENDING",
             "signing_blockers": SIGNING_BLOCKERS,
             "signature_bundles": [],
             "claims": {"scientific_gate_PASS_count": 0,
                         "can_PASS_now": "NO",
                         "measured_in_this_system": False}}
    (out / "release_index.json").write_text(
        json.dumps(index, indent=1, sort_keys=True) + "\n")
    (out / "VERIFY_INSTRUCTIONS.md").write_text(
        "# Verifying a QTA release\n\n"
        "Offline: `python3 verify_release.py --zip <release.zip> "
        "--bundle <bundle_dir>` -- recomputes the zip digest, every "
        "SHA256SUMS entry, SBOM<->uv.lock, provenance subjects, policy "
        "pins (wildcards refused), secret/absolute-path scan, and the "
        "claim boundaries (PASS must be zero).\n\n"
        "Online (only when signature bundles exist): add `--online`; "
        "the verifier refuses to treat absent signatures as success.\n\n"
        "A signature proves origin and integrity only -- never physics "
        "or hardware validation.\n")
    print(f"[release] {out}: index + SHA256SUMS ({len(entries)} entries) "
          f"+ SBOM ({len(sbom['components'])} comps, validated) + "
          f"provenance ({len(subjects)} subjects, level=NONE) + policy; "
          f"signing PENDING ({len(SIGNING_BLOCKERS)} blockers recorded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
