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
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import release_trust

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


def git_revision() -> str:
    """The revision actually checked out, recomputed from Git itself.

    Deliberately NOT read from GITHUB_SHA: an environment variable copied into
    four files is one claim repeated, not four independent checks. The hosted
    workflow cross-checks this value against the Actions context separately;
    here it is recomputed from the object store.
    """
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                       text=True, cwd=str(ROOT))
    if r.returncode != 0:
        return "UNKNOWN-NOT-A-GIT-CHECKOUT"
    return r.stdout.strip()


def build_provenance(subjects: list, ci: bool, policy: dict) -> dict:
    """SLSA v1 provenance recording the real source revision and builder.

    Two identities are kept apart, because conflating them is what made
    ``trusted_builders`` unenforceable:

    * the STABLE builder id names the authorized builder class -- repository,
      workflow, ref -- and is what the policy authorizes. It carries no
      per-execution data, so one authorization covers a rerun of the same
      release rather than being invalidated by a new run number.
    * EXECUTION metadata (run id, attempt, ref, revision, timing) describes one
      invocation and lives in ``runDetails.metadata``. It is expected to differ
      between runs and is never compared against the policy.

    The previous CI builder id was the literal placeholder
    ``PENDING-hosted-runner``, which additionally *contained* the substring
    "hosted" and therefore satisfied the old ``"hosted" not in builder`` SLSA
    guard -- an unresolved value passing a trust check.
    """
    rev = git_revision()
    repo = str(policy.get("source_repository", "")).strip()
    ref = str(policy.get("authorized_ref", "")).strip()

    if ci:
        try:
            builder_id = release_trust.derive_stable_builder_id(
                repo, str(policy["workflow_path"]), ref)
        except release_trust.PolicyError:
            # Policy not yet resolved: record that the builder identity is
            # unauthorized rather than inventing a plausible-looking one.
            builder_id = "UNAUTHORIZED-BUILDER-POLICY-UNRESOLVED"
    else:
        builder_id = release_trust.LOCAL_BUILDER_ID

    # Execution metadata. Present only when the runner supplies it; absent
    # locally rather than faked, so a local bundle cannot look hosted.
    execution = {k: v for k, v in {
        "runId": os.environ.get("GITHUB_RUN_ID"),
        "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "observedRef": os.environ.get("GITHUB_REF"),
        "observedWorkflowRef": os.environ.get("GITHUB_WORKFLOW_REF"),
        "contextSha": os.environ.get("GITHUB_SHA"),
    }.items() if v}

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
                    # Named fields, not free text inside a builder string, so a
                    # verifier can compare them exactly.
                    "source_repository": repo,
                    "workflow_path": str(policy.get("workflow_path", "")),
                    "authorized_ref": ref,
                },
                "resolvedDependencies": [
                    # The source itself, in the in-toto shape: a git URI plus a
                    # gitCommit digest. This is where the released revision
                    # lives -- previously it was recorded nowhere at all.
                    {"uri": f"git+{repo}" if repo else "git+UNKNOWN",
                     "digest": {"gitCommit": rev}},
                    {"uri": "uv.lock",
                     "digest": {"sha256": sha(Path("uv.lock"))}},
                    {"uri": "final_manifest.json",
                     "digest": {"sha256":
                                sha(Path("final_manifest.json"))}},
                ],
            },
            "runDetails": {
                "builder": {"id": builder_id},
                "metadata": {
                    "invocationId": (execution.get("runId")
                                     or "deterministic-local"),
                    "execution": execution,
                    "note": "NO SLSA level claimed. builder.id is the STABLE "
                            "authorized identity; everything under "
                            "'execution' is per-run data and is never "
                            "compared against the trust policy.",
                },
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
    """Load THE canonical policy. This function defines no policy values.

    It used to reconstruct every field and value in Python, duplicating
    QTA_stage9_release_verification/release_trust_policy.json. The two happened
    to agree, but nothing enforced it, so the reviewed policy and the policy
    actually shipped in a bundle could diverge silently. There is now one
    source, and this is a loader for it -- if the canonical file is missing,
    unreadable, empty, malformed, incomplete or carries unknown fields, the
    build fails rather than substituting a default.
    """
    return release_trust.load_canonical_policy()


#: Fixed timestamp for every zip entry. A release zip whose digest changes
#: because it was built a second later is not a reproducible artifact, and the
#: whole point of SHA256SUMS/provenance is that the digest is stable.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Trees excluded from the source zip: historical delivery archives and the
#: regeneration target. Everything else git tracks is source.
ZIP_EXCLUDE_PREFIXES = ("attic/", "outputs/", "release/", "release_bundle/")
ZIP_EXCLUDE_SUFFIXES = (".bundle", ".zip", ".tar.gz", ".patch")


def tracked_payload() -> dict:
    """Repository-relative path -> bytes, for every file the ZIP will carry."""
    tracked = sorted(subprocess.run(
        ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True,
        check=True).stdout.split("\n"))
    return {f: (ROOT / f).read_bytes() for f in tracked
            if f and not f.startswith(ZIP_EXCLUDE_PREFIXES)
            and not f.endswith(ZIP_EXCLUDE_SUFFIXES)}


def build_release_binding(policy: dict):
    """The release facts that need cryptographic binding, or None.

    Returns None when the policy is not yet authorized. An unresolved policy
    has no ref, no reviewed revision and no builder, so there is nothing
    truthful to bind -- and emitting a placeholder binding would be exactly the
    "unresolved value inside a trust artifact" pattern this design exists to
    prevent. Local unsigned builds therefore carry no binding, and online
    verification (which requires an authorized external policy) requires one.

    Placed inside the signed ZIP so a consumer reads them from authenticated
    bytes. It records what this build IS; it never decides what should be
    trusted -- that comes from the externally supplied policy, and this file is
    only read after the signature has been checked against it.
    """
    if not release_trust.is_resolved(policy):
        return None
    repo = str(policy["source_repository"])
    wf = str(policy["workflow_path"])
    ref = str(policy["authorized_ref"])
    builder = release_trust.derive_stable_builder_id(repo, wf, ref)
    return {
        "schema_version": release_trust.SCHEMA_VERSION,
        "source_repository": repo,
        "workflow_path": wf,
        "authorized_ref": ref,
        "release_revision": git_revision(),
        "reviewed_revision": str(policy["pinned_revision"]),
        "reviewed_payload_sha256": release_trust.payload_digest(
            tracked_payload()),
        "stable_builder_id": builder,
        "trusted_policy_sha256": release_trust.policy_digest(
            release_trust.canonical_bytes(policy)),
    }


def make_source_zip(dest: Path, prefix: str | None = None,
                    binding: dict | None = None) -> Path:
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
    root = (prefix or dest.name[:-4]
            if dest.name.endswith(".zip") else dest.name)

    # The signed release binding is a SYNTHETIC member: release metadata, not
    # scientific output, and not a tracked source file. It goes inside the
    # archive so that the Sigstore signature over the archive authenticates it
    # -- an offline consumer can then read the release facts from bytes whose
    # signature was checked, instead of from unsigned external provenance.
    # A collision with a tracked path is refused rather than silently
    # shadowing real content.
    binding_bytes = None
    if binding is not None:
        release_trust.validate_binding(binding)
        if release_trust.RELEASE_BINDING_NAME in members:
            raise SystemExit(
                f"[FAIL-CLOSED] {release_trust.RELEASE_BINDING_NAME} is a "
                "tracked file; the synthetic release binding would shadow it")
        binding_bytes = (json.dumps(binding, indent=1, sort_keys=True)
                         + "\n").encode("utf-8")
        members = sorted(members + [release_trust.RELEASE_BINDING_NAME])

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as z:
        for rel in members:
            info = zipfile.ZipInfo(f"{root}/{rel}", date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            if binding_bytes is not None and \
                    rel == release_trust.RELEASE_BINDING_NAME:
                info.external_attr = 0o644 << 16
                z.writestr(info, binding_bytes)
                continue
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

    # Load and validate the canonical policy FIRST. A missing, unreadable,
    # empty, malformed, incomplete or unknown-field policy fails the build
    # before any artifact is written -- there is no default to fall back to,
    # because a default would be a second policy definition.
    try:
        policy = trust_policy()
    except release_trust.PolicyError as e:
        print(f"[FAIL-CLOSED] canonical trust policy rejected: {e}")
        return 1

    zp = Path(a.zip)
    if a.make_zip:
        binding = build_release_binding(policy)
        make_source_zip(zp, binding=binding)
        print(f"[zip] {zp} ({zp.stat().st_size} bytes, "
              f"sha256 {sha(zp)[:16]}...)")
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
    prov = build_provenance(subjects, a.ci, policy)
    (out / "provenance.intoto.json").write_text(
        json.dumps(prov, indent=1, sort_keys=True) + "\n")
    # The bundled policy is the canonical policy, serialized by the one
    # canonical serializer, so "bundled == canonical" is a byte comparison the
    # verifier can make rather than a semantic argument.
    (out / "release_trust_policy.json").write_bytes(
        release_trust.canonical_bytes(policy))
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
        "Offline verification is STRUCTURAL ONLY. It anchors nothing: with "
        "no local canonical policy it reports UNANCHORED, and it never "
        "decides that a signer is authorized.\n\n"
        "## Online (trusted) verification\n\n"
        "Online verification REQUIRES an external trust root and fails "
        "closed without one. Either form works:\n\n"
        "```\n"
        "python3 verify_release.py --zip <release.zip> --bundle <bundle_dir> "
        "--online \\\n"
        "    --trusted-policy <owner-authorized release_trust_policy.json>\n"
        "\n"
        "python3 verify_release.py --zip <release.zip> --bundle <bundle_dir> "
        "--online \\\n"
        "    --trusted-policy-sha256 <64-hex digest of that policy>\n"
        "```\n\n"
        "**The trust material must be obtained INDEPENDENTLY of this "
        "release bundle** -- from the owner, through a channel this "
        "release does not control. The policy shipped in the bundle and "
        "the policy inside the signed zip are CANDIDATES, checked against "
        "the external root; they cannot self-authorize, because a "
        "malicious artifact can always carry a policy naming its own "
        "signer.\n\n"
        "With `--trusted-policy-sha256`, the bundle's candidate policy is "
        "promoted to the trusted policy only after its canonical bytes "
        "hash to the digest you supplied -- the digest is the "
        "authorization, not the file.\n\n"
        "The verifier refuses to treat absent signatures as success.\n\n"
        "A signature proves origin and integrity only -- never physics "
        "or hardware validation.\n")
    print(f"[release] {out}: index + SHA256SUMS ({len(entries)} entries) "
          f"+ SBOM ({len(sbom['components'])} comps, validated) + "
          f"provenance ({len(subjects)} subjects, level=NONE) + policy; "
          f"signing PENDING ({len(SIGNING_BLOCKERS)} blockers recorded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
