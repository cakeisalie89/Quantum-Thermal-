#!/usr/bin/env python3
"""Consumer release verifier (Stage 9). Fail-closed.

Offline (default): recomputes the release-zip digest; verifies every
SHA256SUMS entry against the bundle's release_index and against the
files inside the zip; cross-validates the SBOM against the zip's
uv.lock; checks provenance subjects (name+digest) including the zip
itself; enforces the trust policy (any wildcard is a failure; PENDING
identity pins mean signatures cannot be trusted yet); scans release
metadata for secrets, absolute paths, and claim-boundary violations
(scientific PASS / performed experiments or campaigns / readiness
drift); asserts the scientific dependency pins.

Online (--online): verifies real Sigstore bundles when present; if no
signature bundle exists or tooling/network is unavailable, it reports
exactly that and FAILS the online gate -- absence is never success.

A signature proves origin and integrity only; it never validates the
physics or hardware.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

import release_trust

SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----",
    r"ghp_[A-Za-z0-9]{36}", r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"(?i)aws_secret_access_key\s*=",
]
ABS_PATHS = ["/home/", "/tmp/", "C:\\\\"]
CLAIM_VIOLATIONS = ["experiment performed", "campaign performed",
                    "hardware-validated", "PLAYBOOK_EXECUTED",
                    "readiness advanced"]
SCI_PINS = {"numpy": "2.4.4", "scipy": "1.17.1", "qutip": "5.2.1",
            "h5py": "3.16.0"}


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fail_list(problems: list, msg: str) -> None:
    problems.append(msg)
    print(f"  [FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")



def _verify_sigstore(problems, zip_path: Path, bundle: Path, idx: dict,
                     sig: list, prov: dict) -> None:
    """Real Sigstore verification against the pinned identity and issuer.

    This used to be an unconditional failure: a genuinely signed release could
    never pass --online, because the SIGNED branch called fail_list() with
    "tooling unavailable" regardless of whether the tooling was there. That
    made the online gate untestable and meaningless.

    Fail-closed order matters here. The policy is checked BEFORE the signature,
    so a bundle signed by anyone at all cannot pass while the identity pins are
    still PENDING; and a missing sigstore library is reported as a blocker, not
    silently treated as success.
    """
    policy = json.loads((bundle / "release_trust_policy.json").read_text())
    identity = str(policy.get("signer_identity", ""))
    issuer = str(policy.get("oidc_issuer", ""))

    # Full policy enforcement, not two named fields. This covers repository,
    # workflow, ref, builder, issuer, source revision and the structural
    # "no unresolved value anywhere" rule -- previously an unresolved entry
    # inside trusted_builders passed silently.
    enforce_resolved_policy(problems, policy, prov, identity, issuer)
    if problems:
        return

    try:
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity
        from sigstore.models import Bundle
    except ImportError as e:
        fail_list(problems,
                  f"sigstore verification requested but the library is not "
                  f"importable ({e.name}); absence of tooling is never "
                  "success -- install 'sigstore' in the verifying environment")
        return

    verifier = Verifier.production()
    pol = Identity(identity=identity, issuer=issuer)
    checked = 0
    seen_records: set = set()
    for entry in sig:
        if not isinstance(entry, dict) or set(entry) != {"name", "bundle"}:
            fail_list(problems,
                      f"malformed signature_bundles record: {entry!r}; "
                      "expected exactly {'name', 'bundle'}")
            continue
        key = (str(entry["name"]), str(entry["bundle"]))
        if key in seen_records:
            fail_list(problems, f"duplicate signature_bundles record: {key}")
            continue
        seen_records.add(key)
        name = entry["name"]
        # signature_bundles is mutable routing metadata; it must never make the
        # verifier read a path outside the bundle directory.
        bundle_path, why = _bundle_relative(bundle, str(entry["bundle"]))
        if bundle_path is None:
            fail_list(problems, f"refusing signature bundle path: {why}")
            continue
        if not bundle_path.exists():
            fail_list(problems,
                      f"signature bundle missing: {bundle_path.name}")
            continue
        if (name or "").endswith(zip_path.name):
            target = zip_path
        else:
            target, why = _bundle_relative(bundle, str(name))
            if target is None:
                fail_list(problems, f"refusing signed-subject path: {why}")
                continue
        if not target.exists():
            fail_list(problems, f"signed subject missing: {name}")
            continue
        try:
            b = Bundle.from_json(bundle_path.read_bytes())
            with open(target, "rb") as fh:
                verifier.verify_artifact(fh.read(), b, pol)
            checked += 1
        except Exception as e:                        # noqa: BLE001
            fail_list(problems,
                      f"Sigstore verification FAILED for {name}: "
                      f"{type(e).__name__}: {e}")
    if checked and not problems:
        ok(f"Sigstore: {checked} signature(s) verified against pinned "
           f"identity {identity!r} / issuer {issuer!r}")
    elif not checked and not problems:
        fail_list(problems, "no signature bundle could be verified")


# ---------------------------------------------------------------------------
# release-trust enforcement
# ---------------------------------------------------------------------------
#
# Four policy fields used to be pure documentation: source_repository,
# workflow_path, pinned_revision and trusted_builders appeared in the policy
# and were never read here. The functions below make each one an exact
# comparison against independently observed evidence, and cross-check the
# three places an identity appears -- policy, provenance and certificate SAN --
# rather than trusting whichever one is convenient.


def _bundle_relative(bundle: Path, name: str):
    """Resolve a bundle-relative name, refusing anything that escapes.

    signature_bundles is routing metadata and is mutable after signing, so it
    must never be able to make the verifier read an arbitrary path. Absolute
    paths, parent traversal and symlink escapes are all refused.
    """
    if not isinstance(name, str) or not name.strip():
        return None, "empty or non-string bundle name"
    if name != name.strip():
        return None, f"bundle name has surrounding whitespace: {name!r}"
    if PurePosixPath(name).is_absolute() or Path(name).is_absolute():
        return None, f"absolute path in signature_bundles: {name!r}"
    if ".." in PurePosixPath(name).parts:
        return None, f"parent traversal in signature_bundles: {name!r}"
    if "\\" in name or "\x00" in name:
        return None, f"illegal characters in signature_bundles: {name!r}"
    base = bundle.resolve()
    target = (base / name)
    try:
        resolved = target.resolve()
    except OSError as e:
        return None, f"unresolvable bundle path {name!r}: {e}"
    if resolved != base and base not in resolved.parents:
        return None, (f"signature bundle escapes the bundle directory: "
                      f"{name!r} -> {resolved}")
    return resolved, None


def _observed_repo_and_workflow(identity: str):
    """Split a GitHub Actions SAN into (repo_url, workflow_path, ref).

    Exact structural parsing, not substring matching: a SAN merely *containing*
    'github.com' or the workflow's basename proves nothing about which
    repository, workflow or ref actually signed.
    """
    m = re.match(
        r"\Ahttps://github\.com/([^/]+)/([^/]+)/"
        r"(\.github/workflows/[^@]+)@(.+)\Z", identity or "")
    if not m:
        return None
    owner, repo, wf, ref = m.groups()
    return (f"https://github.com/{owner}/{repo}", wf, ref)


def enforce_trust_policy(problems, bundle: Path, idx: dict, prov: dict,
                         *, canonical: Path | None = None) -> dict | None:
    """Full policy enforcement. Returns the validated policy, or None.

    Runs for offline verification too: a bundle whose policy is malformed or
    whose bundled copy diverges from the canonical one is defective regardless
    of whether a signature exists.
    """
    bundled_path = bundle / "release_trust_policy.json"
    if not bundled_path.exists():
        fail_list(problems, "bundle has no release_trust_policy.json")
        return None
    bundled_raw = bundled_path.read_bytes()
    try:
        pol = json.loads(bundled_raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        fail_list(problems, f"bundled trust policy is not valid JSON: {e}")
        return None

    # 1. Shape. Unknown fields, missing fields, wildcards, bad types all fail
    #    here -- including for an unresolved policy, which must still be
    #    structurally correct.
    try:
        release_trust.validate_policy(pol)
    except release_trust.PolicyError as e:
        fail_list(problems, f"bundled trust policy is invalid: {e}")
        return None
    ok("bundled trust policy passes the strict schema")

    # 2. The bundled policy must be the canonical policy, byte for byte.
    #    This is what makes a single source of truth enforceable rather than
    #    merely intended.
    canon = canonical if canonical is not None else \
        release_trust.CANONICAL_POLICY_PATH
    if Path(canon).exists():
        want = release_trust.canonical_bytes(
            json.loads(Path(canon).read_text(encoding="utf-8")))
        if bundled_raw != want:
            fail_list(problems,
                      "bundled trust policy differs from the canonical "
                      f"policy at {canon}; a release may only ship the "
                      "reviewed policy")
        else:
            ok("bundled trust policy is byte-identical to the canonical policy")
    return pol


def enforce_resolved_policy(problems, pol: dict, prov: dict,
                            identity: str, issuer: str) -> None:
    """Everything that must hold before a signature may be trusted.

    Called only on the online path, after the policy has been validated for
    shape. Each check compares an authorized value against an independently
    observed one; none of them accepts a substring or a prefix.
    """
    # --- every trust-critical value must be an exact authorized value ------
    try:
        release_trust.validate_policy(pol, require_resolved=True)
    except release_trust.PolicyError as e:
        fail_list(problems, f"trust policy is not authorized for a signed "
                            f"release: {e}")
        return
    ok("trust policy is fully resolved and self-consistent")

    pol_repo = pol["source_repository"]
    pol_wf = pol["workflow_path"]
    pol_ref = pol["authorized_ref"]

    # --- the certificate SAN, parsed structurally --------------------------
    observed = _observed_repo_and_workflow(identity)
    if observed is None:
        fail_list(problems,
                  f"signer identity is not a GitHub Actions workflow SAN: "
                  f"{identity!r}")
        return
    san_repo, san_wf, san_ref = observed

    # --- repository: policy vs provenance vs certificate -------------------
    ext = prov["predicate"]["buildDefinition"].get("externalParameters", {})
    prov_repo = str(ext.get("source_repository", ""))
    for label, value in (("provenance", prov_repo), ("certificate", san_repo)):
        if value != pol_repo:
            fail_list(problems,
                      f"repository mismatch: policy {pol_repo!r} != "
                      f"{label} {value!r}")
    if prov_repo == san_repo == pol_repo:
        ok(f"repository agrees three ways: {pol_repo}")

    # --- workflow: policy vs provenance vs certificate ---------------------
    prov_wf = str(ext.get("workflow_path", ""))
    for label, value in (("provenance", prov_wf), ("certificate", san_wf)):
        if value != pol_wf:
            fail_list(problems,
                      f"workflow mismatch: policy {pol_wf!r} != "
                      f"{label} {value!r}")
    if prov_wf == san_wf == pol_wf:
        ok(f"workflow agrees three ways: {pol_wf}")

    # --- ref/tag -----------------------------------------------------------
    prov_ref = str(ext.get("authorized_ref", ""))
    if san_ref != pol_ref:
        fail_list(problems,
                  f"ref mismatch: policy authorizes {pol_ref!r}, certificate "
                  f"carries {san_ref!r}. A signature from a different tag is "
                  "not the signature that was authorized.")
    if prov_ref != pol_ref:
        fail_list(problems,
                  f"ref mismatch: policy {pol_ref!r} != provenance "
                  f"{prov_ref!r}")
    if san_ref == prov_ref == pol_ref:
        ok(f"ref agrees three ways: {pol_ref}")

    # --- issuer ------------------------------------------------------------
    if issuer != pol["oidc_issuer"]:
        fail_list(problems, f"issuer mismatch: policy {pol['oidc_issuer']!r} "
                            f"!= observed {issuer!r}")

    # --- builder: exact membership, never substring ------------------------
    builder = str(prov["predicate"]["runDetails"]["builder"]["id"])
    authorized = list(pol["trusted_builders"])
    if builder not in authorized:
        fail_list(problems,
                  f"builder {builder!r} is not in trusted_builders "
                  f"{authorized}. Exact equality only: a lookalike, a prefix "
                  "or a suffix is a different builder.")
    elif builder == release_trust.LOCAL_BUILDER_ID:
        fail_list(problems,
                  f"{builder!r} is the local unsigned builder and can never "
                  "satisfy a hosted signed release")
    else:
        ok(f"builder is exactly authorized: {builder}")

    # --- source revision ---------------------------------------------------
    dep_rev = ""
    for dep in prov["predicate"]["buildDefinition"].get(
            "resolvedDependencies", []):
        d = dep.get("digest", {})
        if "gitCommit" in d:
            dep_rev = str(d["gitCommit"])
            break
    if not re.fullmatch(r"[0-9a-f]{40}", dep_rev):
        fail_list(problems,
                  f"provenance records no usable source revision "
                  f"(gitCommit={dep_rev!r})")
    else:
        # pinned_revision is the REVIEWED revision C; the released commit A is
        # its descendant carrying this authorization record. They are
        # deliberately different objects -- see PINNED_REVISION_SEMANTICS.
        # Equality is therefore NOT the check; ancestry is, and it can only be
        # decided where the object store is available.
        ok(f"provenance records a concrete source revision: {dep_rev[:12]}")
        if dep_rev == pol["pinned_revision"]:
            fail_list(problems,
                      "released revision equals pinned_revision; the released "
                      "commit must be the descendant that carries the "
                      "authorization record, not the reviewed revision itself")


def verify(zip_path: Path, bundle: Path, online: bool) -> int:
    problems: list = []
    idx = json.loads((bundle / "release_index.json").read_text())
    sums: dict = {}
    for line in (bundle / "SHA256SUMS").read_text().splitlines():
        h, name = line.split(None, 1)
        if name in sums:
            fail_list(problems, f"duplicate SHA256SUMS entry: {name}")
        sums[name] = h
    zb = zip_path.read_bytes()
    zh = sha_bytes(zb)
    if idx["release_artifact"]["name"] != zip_path.name or \
            idx["release_artifact"]["sha256"] != zh or \
            idx["release_artifact"]["size"] != len(zb):
        fail_list(problems, "release zip name/size/digest mismatch vs "
                             "release_index")
    else:
        ok(f"release zip digest matches index ({zh[:16]}...)")
    if sums.get(zip_path.name) != zh:
        fail_list(problems, "release zip digest mismatch vs SHA256SUMS")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zb))
        names = zf.namelist()
        bad = zf.testzip()
        if bad is not None:
            raise zipfile.BadZipFile(f"CRC failure at {bad}")
    except Exception as e:                      # corrupt structure
        fail_list(problems, f"release zip unreadable/corrupted: "
                             f"{type(e).__name__}")
        print(f"\nRESULT: {len(problems)} FAILURES")
        print("note: a signature proves origin and integrity only; "
              "never physics or hardware validation")
        return 1
    root = names[0].split("/")[0]
    seen: set = set()
    for name, h in sums.items():
        if name == zip_path.name:
            continue
        inner = f"{root}/{name}"
        if inner in seen:
            fail_list(problems, f"duplicate artifact {name}")
        seen.add(inner)
        try:
            data = zf.read(inner)
        except KeyError:
            fail_list(problems, f"missing artifact in zip: {name}")
            continue
        except Exception as e:
            fail_list(problems, f"unreadable artifact {name}: "
                                 f"{type(e).__name__}")
            continue
        if sha_bytes(data) != h:
            fail_list(problems, f"digest mismatch inside zip: {name}")
    if not problems:
        ok(f"all {len(sums) - 1} SHA256SUMS entries verified inside zip")
    idx_files = {(e["name"], e["sha256"]) for e in idx["files"]}
    sums_set = {(n, h) for n, h in sums.items()}
    if idx_files != sums_set:
        fail_list(problems, "release_index files != SHA256SUMS set")
    sbom = json.loads((bundle / "sbom.cdx.json").read_text())
    lock_txt = zf.read(f"{root}/uv.lock").decode()
    lock_pkgs = set(re.findall(
        r'name = "([^"]+)"\nversion = "([^"]+)"', lock_txt))
    sbom_pkgs = {(c["name"], c["version"]) for c in sbom["components"]}
    if lock_pkgs != sbom_pkgs:
        fail_list(problems, "SBOM/uv.lock drift: only-in-lock "
                             f"{sorted(lock_pkgs - sbom_pkgs)[:2]} "
                             "only-in-sbom "
                             f"{sorted(sbom_pkgs - lock_pkgs)[:2]}")
    else:
        ok(f"SBOM matches uv.lock ({len(sbom_pkgs)} packages)")
    for n, v in SCI_PINS.items():
        if (n, v) not in sbom_pkgs:
            fail_list(problems, f"scientific pin drift: {n}!={v}")
    prov = json.loads((bundle / "provenance.intoto.json").read_text())
    subj = {(s["name"], s["digest"]["sha256"]) for s in prov["subject"]}
    if (zip_path.name, zh) not in subj:
        fail_list(problems, "provenance subject missing/mismatching the "
                             "release zip digest")
    else:
        ok("provenance subject binds the release zip digest")
    if subj != sums_set:
        fail_list(problems, "provenance subjects != SHA256SUMS set")
    # SLSA. The old guard was `"hosted" not in builder`, a substring test --
    # and the CI builder id was the literal placeholder
    # "PENDING-hosted-runner", which contains "hosted" and therefore SATISFIED
    # it. An unresolved value passed a trust check. Any non-NONE claim now
    # requires the exact authorized builder and a fully resolved policy, so no
    # mutable string can promote the level.
    lvl = idx["provenance"].get("slsa_level_claimed")
    prov_lvl = prov["predicate"].get("slsa_level_claimed", lvl)
    if lvl not in (None, "NONE") or prov_lvl not in (None, "NONE"):
        fail_list(problems,
                  f"SLSA level claimed ({lvl!r}/{prov_lvl!r}) but no SLSA "
                  "level is authorized in this repository; admission criteria "
                  "for a level do not exist yet")
    else:
        ok("SLSA level claimed = NONE")

    pol = enforce_trust_policy(problems, bundle, idx, prov)
    if pol is None:
        pol = {}
    else:
        # The structural wildcard scan lives in release_trust.validate_policy
        # and covers every leaf, including nested list entries.
        ok("trust policy contains no wildcards")
    blob = "".join((bundle / f).read_text()
                   for f in ("release_index.json", "sbom.cdx.json",
                             "provenance.intoto.json",
                             "release_trust_policy.json"))
    for pat in SECRET_PATTERNS:
        if re.search(pat, blob):
            fail_list(problems, f"secret-pattern hit: {pat[:24]}...")
    for pat in ABS_PATHS:
        if pat in blob:
            fail_list(problems, "absolute path in release metadata: "
                                 f"{pat}")
    for pat in CLAIM_VIOLATIONS:
        if pat in blob:
            fail_list(problems, f"claim-boundary violation: '{pat}'")
    # claims[] is index text and therefore mutable; the gate table inside the
    # signed zip is the authority. Recompute from it, and additionally require
    # the index to AGREE -- an index that understates the PASS count is a
    # lying envelope, not merely a redundant one.
    gate_csv = zf.read(f"{root}/results_gate_table.csv").decode()
    n_pass = sum(1 for line in gate_csv.splitlines()[1:]
                 if re.search(r",PASS(,|$)", line))
    if n_pass:
        fail_list(problems, f"gate table inside zip has {n_pass} PASS")
    else:
        ok("gate table inside zip: scientific PASS count = 0")
    claimed = idx.get("claims", {}).get("scientific_gate_PASS_count")
    if claimed != n_pass:
        fail_list(problems,
                  f"release_index claims scientific_gate_PASS_count="
                  f"{claimed!r} but the gate table inside the zip has "
                  f"{n_pass}; the zip is authoritative")
    if claimed != 0:
        fail_list(problems, "scientific PASS count nonzero in release")
    # release_index.json is UNTRUSTED_ENVELOPE_METADATA: it is mutated after
    # signing by finalize_release_signing.py, so nothing here may confer
    # authority merely because the index says it. signing_status is treated as
    # consistency metadata -- the signed state is derived from facts (a
    # declared bundle that exists, parses and cryptographically verifies
    # against an authorized identity), and the string must AGREE with those
    # facts or the bundle is internally inconsistent.
    sig = idx.get("signature_bundles", [])
    status = idx.get("signing_status")
    declared = bool(sig)
    present = False
    if isinstance(sig, list):
        for entry in sig:
            if isinstance(entry, dict) and isinstance(entry.get("bundle"), str):
                bp, _why = _bundle_relative(bundle, entry["bundle"])
                if bp is not None and bp.exists() and bp.stat().st_size > 0:
                    present = True
    if status == "PENDING" and present:
        fail_list(problems,
                  "inconsistent bundle: signing_status is PENDING but a "
                  "signature bundle exists on disk")
    if status == "SIGNED" and not declared:
        fail_list(problems, "claims SIGNED with no bundles")
    if status == "SIGNED" and declared and not present:
        fail_list(problems,
                  "claims SIGNED and declares a bundle, but no declared "
                  "bundle exists on disk")
    if status not in ("PENDING", "SIGNED"):
        fail_list(problems, f"unknown signing_status {status!r}")

    if online:
        if not (status == "SIGNED" and declared and present):
            fail_list(problems, "online verification requested but no "
                                 "real signature exists (status="
                                 f"{status}, declared={declared}, "
                                 f"present={present}); absence is never "
                                 "success")
        else:
            _verify_sigstore(problems, zip_path, bundle, idx, sig, prov)
    elif status == "PENDING" and not problems:
        ok("signing status honestly PENDING (blockers recorded; no "
           "simulated signatures)")
    res = ("VERIFIED (offline)" if not problems
           else f"{len(problems)} FAILURES")
    print(f"\nRESULT: {res}")
    print("note: a signature proves origin and integrity only; never "
          "physics or hardware validation; software verification only")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--online", action="store_true")
    a = ap.parse_args()
    return verify(Path(a.zip), Path(a.bundle), a.online)


if __name__ == "__main__":
    sys.exit(main())
