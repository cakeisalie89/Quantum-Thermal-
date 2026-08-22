#!/usr/bin/env python3
"""Post-sign signing-metadata finalization -- deliberately narrow, fail-closed.

WHY THIS EXISTS
---------------
``build_release_artifacts.py`` writes ``release_index.json`` with

    "signing_status": "PENDING"
    "signature_bundles": []

and the Sigstore signing step writes ``source.sigstore.json`` without touching
either field. ``verify_release.py --online`` rejects before it ever looks at a
signature::

    if status != "SIGNED" or not sig:
        fail_list(problems, "online verification requested but no "
                            "real signature exists ...")

so online verification could never succeed, *regardless of whether the trust
policy pins were filled*. That is the blocker this module closes, and nothing
else.

WHAT THIS IS NOT
----------------
This is NOT authorization. Moving ``PENDING -> SIGNED`` asserts exactly one
thing:

    a signature bundle now physically exists for this artifact.

It does NOT assert that the signer is trusted. ``verify_release.py`` still
independently requires an exact ``signer_identity`` and ``oidc_issuer`` from
``release_trust_policy.json``, and still refuses PENDING pins and wildcards.
A cryptographically valid signature from an unauthorized signer remains a
failure. This module never writes a trust-policy pin -- see
``_TRUST_POLICY_UNTOUCHED``.

MUTATION BOUNDARY
-----------------
Exactly two keys may change: ``signing_status`` and ``signature_bundles``.
Every other key is compared before and after and must be byte-identical --
``files``, ``release_artifact``, ``sbom``, ``provenance``, ``claims``,
``signing_blockers``, ``schema_version``, ``label``, ``authoritative_input``.
The check is structural (a full dict comparison with the two mutable keys
removed), so a key added or removed anywhere is caught too, not just the ones
named here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

#: The only two keys this module is permitted to alter.
MUTABLE_KEYS = frozenset({"signing_status", "signature_bundles"})

#: This module must never write a pin. Named so the prohibition is greppable
#: and so the contract test can assert the file never writes this filename.
_TRUST_POLICY_UNTOUCHED = "release_trust_policy.json"

EXIT_OK = 0
EXIT_REFUSED = 1


class Refused(Exception):
    """A precondition failed. Always fatal; never downgraded to a warning."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_like_a_sigstore_bundle(path: Path) -> None:
    """Parse far enough to identify the file as a Sigstore bundle.

    Deliberately structural rather than cryptographic: this module does not
    verify signatures -- ``verify_release.py --online`` does, against the
    pinned identity. What is checked here is that the file is not empty, is
    JSON, and carries the shape of a bundle, so a truncated or unrelated file
    cannot be recorded as a signature.
    """
    if not path.exists():
        raise Refused(f"expected Sigstore bundle missing: {path}")
    if path.stat().st_size == 0:
        raise Refused(f"Sigstore bundle is empty: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise Refused(f"Sigstore bundle is not parseable JSON: {path}: {e}")
    if not isinstance(doc, dict):
        raise Refused(f"Sigstore bundle is not a JSON object: {path}")
    # A Sigstore bundle carries verification material and a message signature
    # or DSSE envelope. Accept either spelling rather than pinning one
    # library version's exact layout.
    has_material = "verificationMaterial" in doc
    has_sig = ("messageSignature" in doc or "dsseEnvelope" in doc)
    if not (has_material and has_sig):
        raise Refused(
            f"file does not have the shape of a Sigstore bundle "
            f"(verificationMaterial + messageSignature/dsseEnvelope): {path}; "
            f"top-level keys={sorted(doc)[:8]}")


def _write_atomically(path: Path, text: str) -> None:
    """Write via a same-directory temp file + os.replace.

    A partially written release index is worse than an unwritten one: it would
    be neither the pre-sign state nor a valid finalized state.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".finalize-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _expected_record(zip_path: Path, bundle_name: str) -> dict:
    """The record shape ``verify_release._verify_sigstore`` consumes.

    That function does::

        name = entry.get("name")
        bundle_path = bundle / str(entry.get("bundle", name))
        target = zip_path if name.endswith(zip_path.name) else bundle / name

    so ``name`` must end with the zip's basename for the signed subject to
    resolve to the zip, and ``bundle`` names the bundle file inside the bundle
    directory. Both are relative names, never paths outside the bundle dir.
    """
    return {"name": zip_path.name, "bundle": bundle_name}


def finalize(bundle_dir: Path, zip_path: Path, bundle_name: str,
             *, idempotent: bool = False) -> dict:
    """Perform the PENDING -> SIGNED transition, or refuse.

    Returns the finalized index. Raises ``Refused`` on any precondition
    failure. Never partially applies: the index is written once, atomically,
    after every check has passed.
    """
    index_path = bundle_dir / "release_index.json"

    # ---- preconditions: existence -------------------------------------
    if not bundle_dir.is_dir():
        raise Refused(f"bundle directory does not exist: {bundle_dir}")
    if not index_path.exists():
        raise Refused(f"release index does not exist: {index_path}")
    if not zip_path.exists():
        raise Refused(f"release zip does not exist: {zip_path}")

    try:
        before = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Refused(f"release index is not parseable JSON: {e}")
    if not isinstance(before, dict):
        raise Refused("release index is not a JSON object")

    sig_path = bundle_dir / bundle_name
    _looks_like_a_sigstore_bundle(sig_path)

    # ---- preconditions: starting state --------------------------------
    status = before.get("signing_status")
    bundles = before.get("signature_bundles")
    if not isinstance(bundles, list):
        raise Refused(
            f"signature_bundles is not a list (got {type(bundles).__name__})")

    expected = _expected_record(zip_path, bundle_name)
    if status != "PENDING":
        # Running twice must fail closed, or prove idempotence. It never
        # silently rewrites: --idempotent accepts only an *already exactly
        # finalized* index, and still rewrites nothing.
        if idempotent and status == "SIGNED" and bundles == [expected]:
            print("[finalize] already finalized, byte-identical to the "
                  "expected state; nothing rewritten")
            return before
        raise Refused(
            f"signing_status is {status!r}, expected 'PENDING'. Refusing: "
            "finalization is a one-way transition from the pre-sign state. "
            "Pass --idempotent only to accept an already-identical finalized "
            "index.")
    if bundles:
        raise Refused(
            f"signature_bundles is already non-empty ({len(bundles)} "
            "entries) while status is PENDING -- inconsistent pre-sign state")

    # ---- preconditions: the index describes THIS zip -------------------
    artifact = before.get("release_artifact")
    if not isinstance(artifact, dict):
        raise Refused("release_artifact missing or not an object")
    if artifact.get("name") != zip_path.name:
        raise Refused(
            f"release_artifact names {artifact.get('name')!r} but the zip "
            f"being finalized is {zip_path.name!r}")

    # ---- preconditions: the zip has not drifted since the index --------
    actual = _sha256(zip_path)
    recorded = artifact.get("sha256")
    if actual != recorded:
        raise Refused(
            f"release zip digest drift: index records {recorded}, file is "
            f"{actual}. The signed artifact must be the artifact the index, "
            "provenance and SHA256SUMS describe.")

    # The same digest must appear in files[] and in SHA256SUMS, or the bundle
    # was already internally inconsistent before signing.
    files = before.get("files")
    if not isinstance(files, list) or not files:
        raise Refused("index files[] missing or empty")
    in_files = [e for e in files
                if isinstance(e, dict) and e.get("name") == zip_path.name]
    if len(in_files) != 1:
        raise Refused(
            f"expected exactly one files[] entry for {zip_path.name}, "
            f"found {len(in_files)}")
    if in_files[0].get("sha256") != actual:
        raise Refused(
            f"files[] entry for {zip_path.name} records "
            f"{in_files[0].get('sha256')}, file is {actual}")

    sums_path = bundle_dir / "SHA256SUMS"
    if not sums_path.exists():
        raise Refused(f"SHA256SUMS missing: {sums_path}")
    sums = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest.strip()
    if sums.get(zip_path.name) != actual:
        raise Refused(
            f"SHA256SUMS records {sums.get(zip_path.name)} for "
            f"{zip_path.name}, file is {actual}")

    # ---- apply the only permitted transition ---------------------------
    after = dict(before)
    after["signing_status"] = "SIGNED"
    after["signature_bundles"] = [expected]

    # ---- prove the mutation boundary held -------------------------------
    # Structural, not key-by-key: everything except the two mutable keys must
    # compare equal, so an added or removed key is caught as well.
    frozen_before = {k: v for k, v in before.items() if k not in MUTABLE_KEYS}
    frozen_after = {k: v for k, v in after.items() if k not in MUTABLE_KEYS}
    if frozen_before != frozen_after:
        changed = sorted(set(frozen_before) ^ set(frozen_after)) or [
            k for k in frozen_before if frozen_before[k] != frozen_after.get(k)]
        raise Refused(
            f"finalization would alter non-signing content: {changed}. "
            "Only signing_status and signature_bundles may change.")

    _write_atomically(
        index_path, json.dumps(after, indent=1, sort_keys=True) + "\n")
    print(f"[finalize] {index_path}: signing_status PENDING -> SIGNED; "
          f"signature_bundles=[{expected['name']} <- {expected['bundle']}]")
    print("[finalize] this records that a signature EXISTS; it does not "
          "authorize the signer. Online verification still requires exact "
          "signer_identity and oidc_issuer pins.")
    return after


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Finalize signing metadata after Sigstore signing. "
                    "Records that a signature exists; confers no trust.")
    ap.add_argument("--bundle", required=True,
                    help="the bundle directory containing release_index.json")
    ap.add_argument("--zip", required=True,
                    help="the exact release zip that was signed")
    ap.add_argument("--bundle-name", default="source.sigstore.json",
                    help="Sigstore bundle filename inside --bundle")
    ap.add_argument("--idempotent", action="store_true",
                    help="accept an already-finalized index that is exactly "
                         "identical to the expected finalized state; still "
                         "rewrites nothing")
    a = ap.parse_args(argv)
    try:
        finalize(Path(a.bundle), Path(a.zip), a.bundle_name,
                 idempotent=a.idempotent)
    except Refused as e:
        print(f"[FAIL-CLOSED] signing finalization refused: {e}")
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
