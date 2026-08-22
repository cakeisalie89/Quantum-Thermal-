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
                     sig: list, trusted_pol: dict) -> None:
    """Cryptographically authenticate the zip against the EXTERNAL trust root.

    The expected identity and issuer come from ``trusted_pol`` -- the policy the
    consumer supplied independently -- never from the bundle or the archive.

    On the certificate: Sigstore's ``Identity`` policy is what checks the
    presented certificate's SAN and issuer, and it is given the exact expected
    values from the trusted policy. An earlier version parsed the expected
    identity out of the policy and reported the result as an observed
    "certificate" value under a three-way agreement heading. That was wrong:
    before ``verify_artifact`` runs there is no observed certificate at all,
    only an expectation. The certificate check is exactly this call, and it is
    named for what it is.
    """
    identity = str(trusted_pol["signer_identity"])
    issuer = str(trusted_pol["oidc_issuer"])

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

        # Exact subject name, not a suffix match. The finalizer writes the
        # exact basename; accepting anything ending in it would let routing
        # metadata point the verifier at a differently-named subject.
        name = str(entry["name"])
        if name != zip_path.name:
            fail_list(problems,
                      f"signature subject {name!r} is not the release zip "
                      f"{zip_path.name!r}; alternate subject names are "
                      "refused")
            continue
        bundle_path, why = _bundle_relative(bundle, str(entry["bundle"]))
        if bundle_path is None:
            fail_list(problems, f"refusing signature bundle path: {why}")
            continue
        if not bundle_path.exists():
            fail_list(problems,
                      f"signature bundle missing: {bundle_path.name}")
            continue
        try:
            b = Bundle.from_json(bundle_path.read_bytes())
            with open(zip_path, "rb") as fh:
                verifier.verify_artifact(fh.read(), b, pol)
            checked += 1
        except Exception as e:                        # noqa: BLE001
            fail_list(problems,
                      f"Sigstore verification FAILED for {name}: "
                      f"{type(e).__name__}: {e}")
    if checked and not problems:
        ok(f"Sigstore verified {checked} signature(s): the certificate "
           f"presented by the signer matched the expected identity "
           f"{identity!r} and issuer {issuer!r} from the EXTERNAL trusted "
           "policy")
    elif not checked and not problems:
        fail_list(problems, "no signature bundle could be verified")


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


def load_trusted_policy(problems, trusted_policy, trusted_sha256):
    """THE external trust root. Returns (policy, canonical_bytes) or (None, None).

    A release cannot authorize itself. A malicious artifact can always carry a
    policy naming its own signer, so the expected identity must come from
    OUTSIDE the artifact -- supplied by the consumer through a channel the
    release does not control.

    The previous implementation compared the bundled policy against a
    repository-local path and, when that path did not exist, SKIPPED the
    comparison. Run from a directory with no QTA checkout -- which is exactly
    the independent-verification case -- there was no trust root at all.
    """
    if not trusted_policy and not trusted_sha256:
        fail_list(problems,
                  "--online requires an externally supplied trust root: pass "
                  "--trusted-policy PATH (an owner-authorized policy obtained "
                  "independently of this release) or --trusted-policy-sha256. "
                  "The policy inside the bundle or the zip is a CANDIDATE, "
                  "never the root: an artifact cannot authorize itself.")
        return None, None
    if not trusted_policy:
        # A digest alone is a valid root: it pins the bytes without shipping
        # them, and the candidate policy is checked against it below.
        return None, None
    tp = Path(trusted_policy)
    if not tp.exists():
        fail_list(problems, f"--trusted-policy not found: {tp}")
        return None, None
    try:
        raw = tp.read_text(encoding="utf-8")
        pol = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        fail_list(problems, f"--trusted-policy unreadable/invalid: {e}")
        return None, None
    try:
        release_trust.validate_policy(pol, require_resolved=True)
    except release_trust.PolicyError as e:
        fail_list(problems,
                  f"the supplied trusted policy is not authorized for a "
                  f"signed release: {e}")
        return None, None
    canon = release_trust.canonical_bytes(pol)
    if trusted_sha256:
        got = release_trust.policy_digest(canon)
        if got != str(trusted_sha256).strip().lower():
            fail_list(problems,
                      f"--trusted-policy digest {got} != "
                      f"--trusted-policy-sha256 {trusted_sha256}")
            return None, None
    ok("external trust root loaded and authorized")
    return pol, canon


def bind_candidate_policy(problems, label: str, raw: bytes,
                          trusted_canon: bytes, trusted_sha256):
    """A candidate policy copy must equal the external trust root exactly."""
    if trusted_canon is not None:
        if raw != trusted_canon:
            fail_list(problems,
                      f"{label} policy differs from the external trusted "
                      "policy; a release may only ship the authorized policy")
            return False
    elif trusted_sha256:
        got = release_trust.policy_digest(raw)
        if got != str(trusted_sha256).strip().lower():
            fail_list(problems,
                      f"{label} policy digest {got} != trusted "
                      f"{trusted_sha256}")
            return False
    else:
        fail_list(problems, f"no trust root to bind {label} policy against")
        return False
    ok(f"{label} policy is byte-identical to the external trust root")
    return True


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
    # OFFLINE ONLY. This is a repository-local consistency check, not a trust
    # root: it says "this bundle matches the policy in this checkout". If the
    # checkout is absent -- the independent-consumer case -- there is nothing
    # to anchor to, and that is REPORTED rather than silently skipped, which
    # is what the previous implementation did. Trusted verification never
    # relies on this path; it requires --trusted-policy (see
    # load_trusted_policy) and fails closed without one.
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
            ok("bundled policy matches this checkout's canonical policy "
               "(local consistency only; NOT a trust root)")
    else:
        ok(f"no local canonical policy at {canon}: bundled policy is "
           "UNANCHORED here. Offline verification checks structure only; "
           "use --online --trusted-policy for an authorization decision.")
    return pol


def enforce_authenticated_binding(problems, pol: dict, binding: dict,
                                  zip_policy_ok: bool) -> None:
    """Compare the SIGNED release binding against the external trusted policy.

    Called only after the Sigstore signature over the zip has verified, so the
    binding's bytes are authenticated. Order matters and is not negotiable:

        trusted policy -> expected identity -> verify signature ->
        NOW read binding -> compare binding to policy

    Never binding -> decide who to trust -> verify against that. That would be
    artifact self-authorization.

    The values here are genuinely observed release facts. The repository,
    workflow and ref are ALSO proven by the certificate, because Sigstore
    checked the SAN against the exact expected identity derived from the
    trusted policy -- so agreement between the binding and the policy is a
    real cross-check against an independent encoding, not a restatement.
    """
    try:
        release_trust.validate_binding(binding)
    except release_trust.PolicyError as e:
        fail_list(problems, f"signed release binding is invalid: {e}")
        return
    ok("signed release binding passes its schema")

    for field, policy_key in (("source_repository", "source_repository"),
                              ("workflow_path", "workflow_path"),
                              ("authorized_ref", "authorized_ref"),
                              ("reviewed_payload_sha256",
                               "reviewed_payload_sha256")):
        got, want = str(binding[field]), str(pol[policy_key])
        if got != want:
            fail_list(problems,
                      f"signed binding {field}={got!r} != authorized "
                      f"{want!r}")
    if binding["reviewed_revision"] != pol["pinned_revision"]:
        fail_list(problems,
                  f"signed binding reviewed_revision "
                  f"{binding['reviewed_revision']} != authorized "
                  f"pinned_revision {pol['pinned_revision']}")

    # The stable builder id is DERIVED from authenticated repository/workflow/
    # ref rather than taken from unsigned provenance, then compared against the
    # authorized list. See RELEASE_TRUST_ENFORCEMENT.md on what this does and
    # does not add over signer_identity.
    derived = release_trust.derive_stable_builder_id(
        binding["source_repository"], binding["workflow_path"],
        binding["authorized_ref"])
    if binding["stable_builder_id"] != derived:
        fail_list(problems,
                  f"signed binding stable_builder_id "
                  f"{binding['stable_builder_id']!r} is not the value derived "
                  f"from its own repository/workflow/ref ({derived!r})")
    elif derived not in list(pol["trusted_builders"]):
        fail_list(problems,
                  f"builder {derived!r} is not in trusted_builders "
                  f"{list(pol['trusted_builders'])}. Exact equality only.")
    elif derived == release_trust.LOCAL_BUILDER_ID:
        fail_list(problems,
                  "the local unsigned builder can never satisfy a hosted "
                  "signed release")
    else:
        ok(f"builder derived from authenticated content and authorized: "
           f"{derived}")

    if not zip_policy_ok:
        fail_list(problems,
                  "the policy inside the signed zip was not bound to the "
                  "external trust root")


def check_auxiliary_provenance(problems, pol: dict, binding: dict,
                               prov: dict) -> None:
    """Cross-check UNSIGNED provenance. It is never authority.

    provenance.intoto.json is generated OUTSIDE the source zip and is not
    itself signed, so it is UNTRUSTED_AUXILIARY_METADATA. It was previously
    consulted for repository, workflow, ref, builder and source revision --
    every one an authorization decision resting on unauthenticated bytes.
    Those now come from the signed binding. What remains here is a consistency
    report: a disagreement is worth surfacing, but agreement grants nothing.
    """
    ext = prov.get("predicate", {}).get("buildDefinition", {}).get(
        "externalParameters", {})
    rev = ""
    for dep in prov.get("predicate", {}).get("buildDefinition", {}).get(
            "resolvedDependencies", []):
        if "gitCommit" in dep.get("digest", {}):
            rev = str(dep["digest"]["gitCommit"])
            break
    mismatches = []
    for k in ("source_repository", "workflow_path", "authorized_ref"):
        if str(ext.get(k, "")) != str(binding[k]):
            mismatches.append(k)
    if rev and rev != binding["release_revision"]:
        mismatches.append("release_revision")
    if mismatches:
        fail_list(problems,
                  f"auxiliary provenance disagrees with the signed binding "
                  f"on {mismatches}; provenance is not authority, but a "
                  "disagreement means the bundle is inconsistent")
    else:
        ok("auxiliary provenance is consistent with the signed binding "
           "(cross-check only; provenance is not authenticated)")


def verify(zip_path: Path, bundle: Path, online: bool,
           trusted_policy=None, trusted_sha256=None) -> int:
    problems: list = []

    # ---- PHASE 1: trust root -------------------------------------------
    # Nothing from the candidate artifact has any authority until phase 3.
    trusted_pol, trusted_canon = (None, None)
    if online:
        trusted_pol, trusted_canon = load_trusted_policy(
            problems, trusted_policy, trusted_sha256)
        if problems:
            print(f"\nRESULT: {len(problems)} FAILURES")
            print("note: a signature proves origin and integrity only; "
                  "never physics or hardware validation")
            return 1

    # ---- PHASE 2: candidate artifact structure --------------------------
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
            # ---- PHASE 3: cryptographic authentication ------------------
            before = len(problems)
            _verify_sigstore(problems, zip_path, bundle, idx, sig,
                             trusted_pol)
            authenticated = len(problems) == before

            # ---- PHASE 4: authenticated-content checks ------------------
            # Only now are the archive's bytes evidence.
            if authenticated:
                zip_pol_ok = False
                try:
                    zip_pol_raw = zf.read(
                        f"{root}/{release_trust.CANONICAL_POLICY_PATH}")
                except KeyError:
                    fail_list(problems,
                              "the signed zip contains no canonical trust "
                              "policy")
                    zip_pol_raw = None
                if zip_pol_raw is not None:
                    zip_pol_ok = bind_candidate_policy(
                        problems, "signed-zip", zip_pol_raw, trusted_canon,
                        trusted_sha256)

                bundled_raw = (bundle /
                               "release_trust_policy.json").read_bytes()
                bind_candidate_policy(problems, "bundle", bundled_raw,
                                      trusted_canon, trusted_sha256)

                try:
                    binding_raw = zf.read(
                        f"{root}/{release_trust.RELEASE_BINDING_NAME}")
                    binding = json.loads(binding_raw)
                except (KeyError, json.JSONDecodeError) as e:
                    fail_list(problems,
                              f"the signed zip carries no usable "
                              f"{release_trust.RELEASE_BINDING_NAME}: {e}")
                    binding = None
                if binding is not None and trusted_pol is not None:
                    enforce_authenticated_binding(
                        problems, trusted_pol, binding, zip_pol_ok)

                    # Recompute the reviewed payload digest from the
                    # AUTHENTICATED archive -- an offline consumer can do this
                    # with no Git checkout at all.
                    payload = {}
                    for n in zf.namelist():
                        if not n.startswith(root + "/") or n.endswith("/"):
                            continue
                        rel = n[len(root) + 1:]
                        if rel == release_trust.RELEASE_BINDING_NAME:
                            continue
                        payload[rel] = zf.read(n)
                    got = release_trust.payload_digest(payload)
                    want = str(trusted_pol["reviewed_payload_sha256"])
                    if got != want:
                        fail_list(problems,
                                  f"reviewed payload digest {got} != "
                                  f"authorized {want}; the signed content is "
                                  "not the reviewed content")
                    else:
                        ok("reviewed payload digest matches the authorized "
                           "value, recomputed from the signed archive")

                    # ---- PHASE 5: auxiliary metadata --------------------
                    check_auxiliary_provenance(problems, trusted_pol,
                                               binding, prov)
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
    ap.add_argument("--trusted-policy", default=None,
                    help="PATH to an owner-authorized trust policy obtained "
                         "INDEPENDENTLY of this release. Required for "
                         "--online: the policy shipped with a release is a "
                         "candidate, never the trust root.")
    ap.add_argument("--trusted-policy-sha256", default=None,
                    help="sha256 of the owner-authorized policy, as an "
                         "alternative trust root when the file itself is not "
                         "at hand")
    a = ap.parse_args()
    return verify(Path(a.zip), Path(a.bundle), a.online,
                  a.trusted_policy, a.trusted_policy_sha256)


if __name__ == "__main__":
    sys.exit(main())
