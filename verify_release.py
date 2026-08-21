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
from pathlib import Path

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
                     sig: list) -> None:
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
    for field, value in (("signer_identity", identity), ("oidc_issuer", issuer)):
        if not value or value.startswith("PENDING") or "*" in value:
            fail_list(problems,
                      f"trust policy {field}={value!r} is not an exact pin; "
                      "no signature can be trusted until it is")
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
    for entry in sig:
        name = entry.get("name") if isinstance(entry, dict) else str(entry)
        bundle_path = bundle / str(entry.get("bundle", name)) \
            if isinstance(entry, dict) else bundle / name
        if not bundle_path.exists():
            fail_list(problems, f"signature bundle missing: {bundle_path.name}")
            continue
        target = zip_path if (name or "").endswith(zip_path.name) else bundle / name
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
    lvl = idx["provenance"].get("slsa_level_claimed")
    builder = prov["predicate"]["runDetails"]["builder"]["id"]
    if lvl not in (None, "NONE") and "hosted" not in builder:
        fail_list(problems, f"SLSA level '{lvl}' claimed without a "
                             "hosted verified build")
    pol = json.loads((bundle / "release_trust_policy.json").read_text())

    def _leaves(x):                     # structural wildcard scan
        if isinstance(x, dict):
            for v in x.values():
                yield from _leaves(v)
        elif isinstance(x, list):
            for v in x:
                yield from _leaves(v)
        else:
            yield x
    if any(isinstance(v, str) and "*" in v for v in _leaves(pol)):
        fail_list(problems, "wildcard in trust policy (forbidden)")
    else:
        ok("trust policy contains no wildcards")
    if not pol.get("wildcards_forbidden"):
        fail_list(problems, "policy must declare wildcards_forbidden")
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
    if idx["claims"]["scientific_gate_PASS_count"] != 0:
        fail_list(problems, "scientific PASS count nonzero in release")
    gate_csv = zf.read(f"{root}/results_gate_table.csv").decode()
    n_pass = sum(1 for line in gate_csv.splitlines()[1:]
                 if re.search(r",PASS(,|$)", line))
    if n_pass:
        fail_list(problems, f"gate table inside zip has {n_pass} PASS")
    else:
        ok("gate table inside zip: scientific PASS count = 0")
    sig = idx.get("signature_bundles", [])
    status = idx.get("signing_status")
    if online:
        if status != "SIGNED" or not sig:
            fail_list(problems, "online verification requested but no "
                                 "real signature exists (status="
                                 f"{status}); absence is never success")
        else:
            _verify_sigstore(problems, zip_path, bundle, idx, sig)
    else:
        if status == "PENDING":
            ok("signing status honestly PENDING (blockers recorded; no "
               "simulated signatures)")
        elif status == "SIGNED" and not sig:
            fail_list(problems, "claims SIGNED with no bundles")
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
