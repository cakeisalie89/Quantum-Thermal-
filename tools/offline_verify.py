#!/usr/bin/env python3
"""Verify a harness release candidate from its bundle alone.

PORTABLE BY CONSTRUCTION. This file imports the Python standard library and
nothing else, so it can be copied by itself to a machine that has no
checkout of this repository and no network. The one other thing it runs is
a Sigstore verifier, as a subprocess, from a runtime built from a
hash-pinned lock -- a verification dependency the verifier's operator
provisions, never something the bundle supplies.

INPUTS, AND WHERE EACH MUST COME FROM
  --bundle DIR         the candidate: source.zip, sbom.cdx.json,
                       provenance.intoto.json, index.json, SHA256SUMS and
                       the Sigstore bundle over SHA256SUMS. UNTRUSTED until
                       verified; read with size bounds, never followed
                       through a link.
  --policy FILE        whose signature to accept: an exact signer identity
                       and OIDC issuer. Provisioned independently of the
                       bundle: refused if it lies inside the bundle.
  --trust-config FILE  the Sigstore client trust configuration (trusted
                       root). Provisioned independently of the bundle:
                       refused if it lies inside it, and pinned by digest
                       when the policy is a release policy. Offline
                       verification without one is refused -- the
                       verifier's own sigstore defaults are not a trust
                       anchor anyone chose.
  --sigstore-python EXE  the verifier runtime's interpreter.

WHAT IT NEVER READS: a repository checkout, a lockfile on this machine, the
caller's environment (the Sigstore subprocess gets an empty one) or the
network (offline mode passes --offline and --trust-config, so no TUF or
transparency-log request is made, and nothing here opens a socket).

WHAT IT ESTABLISHES, IN ORDER. Five levels, each needing the one before
it, reported separately and never collapsed into one boolean:

  ARTIFACT_INTEGRITY    every artifact's bytes are the digest SHA256SUMS
                        lists, and source.zip is exactly what the manifest
                        inside it says it is;
  PROVENANCE_STRUCTURE  the provenance is an in-toto statement of the
                        expected type, its subjects are the signed digests,
                        it names the policy's builder and the index's
                        commit, and the SBOM is exactly what the release's
                        OWN dependency declarations (inside source.zip) name;
  SIGNATURE_VERIFIED    a Sigstore signature over SHA256SUMS chains to the
                        provisioned trusted root, with its transparency-log
                        inclusion proven;
  SIGNER_TRUSTED        the signing certificate's identity and issuer are
                        exactly the policy's;
  RELEASE_AUTHORIZED    the policy is a RELEASE policy -- not a CI
                        validation policy -- and every level above holds.

A digest-only run establishes the first two and says NOT_CHECKED for the
rest: integrity of bytes nobody has authenticated is not acceptance.
SIGNATURE_VERIFIED and SIGNER_TRUSTED are decided by one Sigstore call,
which checks the chain, the inclusion proof, the signature and the
identity together; they are named separately because they answer
different questions.

A valid signature attests origin and integrity of these bytes. It does not
attest that any number in them is correct.

    python offline_verify.py --bundle DIR --policy P --trust-config T \\
        --sigstore-python EXE [--mode offline|online|digests] \\
        [--require LEVEL] [--report OUT]
    python offline_verify.py hostile --bundle DIR --policy P \\
        --trust-config T --sigstore-python EXE [--decoy-bundle B]

Exit status: 0 the required level holds; 1 refused (a check failed); 2
usage; 3 verified, but the required level is not established.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
import zlib
from pathlib import Path
from typing import Any, Callable

ARTIFACTS = ("source.zip", "sbom.cdx.json", "provenance.intoto.json",
             "index.json")
SIGNED = "SHA256SUMS"
BUNDLE = "SHA256SUMS.sigstore.json"
#: The release's own dependency declarations, read from source.zip.
ENVIRONMENT_LOCKS = {
    "fenicsx-conda": "integrations/fenicsx/environment.sha256.json",
    "fmi-runtime": "integrations/fmi/runtime.lock",
    "sigstore-runtime": "integrations/supply_chain/sigstore.lock",
}
#: Tracked files the manifest does not list, by its own policy: it cannot
#: hash itself, and manifest_hash.txt is that hash, written after it.
MANIFEST_DETACHED = frozenset({"final_manifest.json", "manifest_hash.txt"})

LEVELS = ("ARTIFACT_INTEGRITY", "PROVENANCE_STRUCTURE", "SIGNATURE_VERIFIED",
          "SIGNER_TRUSTED", "RELEASE_AUTHORIZED")
ESTABLISHED, NOT_CHECKED, NOT_ESTABLISHED = (
    "ESTABLISHED", "NOT_CHECKED", "NOT_ESTABLISHED")

CI_SCHEMA = "supply-chain-ci-policy/1"
RELEASE_SCHEMA = "harness-release-policy/1"
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
#: Workflows whose signatures exercise the mechanism and authorize nothing.
CI_VALIDATION_WORKFLOWS = frozenset({".github/workflows/supply-chain.yml"})
#: The workflow that runs an AI agent with write access. Its OIDC identity
#: is the agent's, and the agent proposes: it never signs a release
#: (D-2026-146).
AGENT_WORKFLOWS = frozenset({".github/workflows/claude.yml"})
#: No release policy may name any of these as its signer.
NON_RELEASE_WORKFLOWS = CI_VALIDATION_WORKFLOWS | AGENT_WORKFLOWS

#: Bounds on untrusted input, checked BEFORE the bytes are trusted.
MAX_SMALL = 1 << 20              # policy, trust config, SHA256SUMS
MAX_JSON = 16 << 20              # sbom, provenance, index, sigstore bundle
MAX_ARCHIVE = 256 << 20          # source.zip on disk
MAX_MEMBERS = 20_000
MAX_MEMBER = 64 << 20            # one member, uncompressed
MAX_TOTAL = 512 << 20            # all members, uncompressed
MAX_RATIO = 1_000                # uncompressed/compressed, for big members
_SUM_LINE = re.compile(r"\A([0-9a-f]{64})  ([A-Za-z0-9._-]+)\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
#: What the zip reader raises on hostile input: a refusal, never a crash.
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError,
               OSError, EOFError, NotImplementedError, RuntimeError,
               zlib.error)


class Refused(RuntimeError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- bounded, link-refusing reads ---------------------------------------

def read_bounded(p: Path, cap: int, what: str) -> bytes:
    """A regular file's bytes, at most ``cap`` of them. A link is refused
    rather than followed: an artifact that is another name for something
    else is not the artifact that was signed."""
    p = Path(p)
    try:
        st = p.lstat()
    except FileNotFoundError:
        raise Refused(f"{what} is absent: {p.name}") from None
    if stat.S_ISLNK(st.st_mode):
        raise Refused(f"{what} {p.name} is a symbolic link; refusing to "
                      "follow it")
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{what} {p.name} is not a regular file")
    if st.st_size > cap:
        raise Refused(f"{what} {p.name} is {st.st_size} bytes, over the "
                      f"{cap}-byte bound")
    with open(p, "rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise Refused(f"{what} {p.name} grew past the {cap}-byte bound "
                      "while being read")
    return data


def _json(raw: bytes, what: str) -> dict:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"{what} is not JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise Refused(f"{what} is not a JSON object")
    return doc


def _outside(path: Path, container: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(container).resolve())
    except ValueError:
        return True
    return False


# ---- policy --------------------------------------------------------------

def load_policy(path: Path, *, bundle_dir: Path | None = None,
                today: _dt.date | None = None) -> dict:
    """A trust policy: an exact signer identity derived from repository,
    workflow and ref, under GitHub Actions' issuer. Never a pattern.

    A CI validation policy (supply-chain-ci-policy/1, CI_VALIDATION_ONLY)
    lets the mechanism be exercised and authorizes no release. A release
    policy (harness-release-policy/1) must name a TAG ref, a workflow that
    is not a CI validation workflow, the trusted root it expects by digest,
    who issued it and when it stops being valid; it is the only kind that
    can establish RELEASE_AUTHORIZED. Either is refused when it lies inside
    the bundle it would judge."""
    path = Path(path)
    if bundle_dir is not None and not _outside(path, bundle_dir):
        raise Refused("the policy lies inside the bundle it would judge; a "
                      "trust policy is provisioned independently of the "
                      "release")
    raw = read_bounded(path, MAX_SMALL, "policy")
    p = _json(raw, "policy")
    for k in ("signer_identity", "oidc_issuer", "source_repository",
              "workflow_path", "ref"):
        v = p.get(k, "")
        if not isinstance(v, str) or not v or "*" in v or "PENDING" in v:
            raise Refused(f"policy {k} is not an exact value: {v!r}")
    want = f"{p['source_repository']}/{p['workflow_path']}@{p['ref']}"
    if p["signer_identity"] != want:
        raise Refused(f"policy signer_identity is not derived from its "
                      f"repository, workflow and ref ({want})")
    if p["oidc_issuer"] != GITHUB_ISSUER:
        raise Refused("policy issuer is not GitHub Actions' OIDC issuer")
    schema = p.get("schema")
    if schema == CI_SCHEMA:
        if p.get("label") != "CI_VALIDATION_ONLY":
            raise Refused("a CI validation policy must say so: label "
                          "CI_VALIDATION_ONLY")
    elif schema == RELEASE_SCHEMA:
        if p.get("label") != "RELEASE_TRUST_ROOT":
            raise Refused("a release policy carries label RELEASE_TRUST_ROOT")
        if not p["ref"].startswith("refs/tags/"):
            raise Refused("a release policy names a tag ref; a branch any "
                          "pusher can advance authorizes nothing")
        if p["workflow_path"] in NON_RELEASE_WORKFLOWS:
            raise Refused("a release policy may not name a CI validation "
                          "workflow or an AI agent's workflow; the first "
                          "exercises the mechanism, the second proposes, "
                          "and neither authorizes a release")
        if not _HEX64.match(str(p.get("trust_config_sha256", ""))):
            raise Refused("a release policy pins its trusted root: "
                          "trust_config_sha256")
        if not isinstance(p.get("issued_by"), str) or not p["issued_by"]:
            raise Refused("a release policy names who issued it: issued_by")
        try:
            not_after = _dt.date.fromisoformat(str(p.get("not_after", "")))
        except ValueError:
            raise Refused("a release policy says when it stops being valid: "
                          "not_after (YYYY-MM-DD)") from None
        if (today or _dt.date.today()) > not_after:
            raise Refused(f"the release policy expired on {not_after}")
    else:
        raise Refused(f"policy schema {schema!r} is neither {CI_SCHEMA} nor "
                      f"{RELEASE_SCHEMA}")
    return {**p, "_sha256": _sha(raw)}


def is_release_policy(policy: dict) -> bool:
    return policy.get("schema") == RELEASE_SCHEMA


# ---- SHA256SUMS, archive, SBOM -------------------------------------------

def parse_sums(raw: bytes) -> dict:
    """``<64 lowercase hex>  <name>`` per line, LF-terminated, each name
    once, exactly the four artifacts. Anything else is refused rather than
    read the way a lenient parser would read it."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("SHA256SUMS is not ASCII") from None
    if not text.endswith("\n"):
        raise Refused("SHA256SUMS does not end with a newline")
    out: dict = {}
    for ln in text[:-1].split("\n"):
        m = _SUM_LINE.match(ln)
        if not m:
            raise Refused(f"SHA256SUMS has a malformed line: {ln[:80]!r}")
        if m.group(2) in out:
            raise Refused(f"SHA256SUMS names {m.group(2)} twice")
        out[m.group(2)] = m.group(1)
    if set(out) != set(ARTIFACTS):
        raise Refused(f"SHA256SUMS lists {sorted(out)}, not "
                      f"{sorted(ARTIFACTS)}")
    return out


def read_archive(data: bytes) -> dict:
    """source.zip's members, bounded and checked for shape BEFORE anything
    is extracted: no absolute or parent-escaping name, no link, no member
    over its bound, no total over its bound, no member whose compression
    ratio says it is a bomb, and no member whose bytes disagree with the
    size its header declares."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except _ZIP_ERRORS as exc:
        raise Refused(f"source.zip is not a readable zip archive: {exc}") \
            from None
    if len(infos) > MAX_MEMBERS:
        raise Refused(f"source.zip has {len(infos)} members, over the "
                      f"{MAX_MEMBERS} bound")
    names = [i.filename for i in infos]
    if len(names) != len(set(names)):
        raise Refused("source.zip names a member more than once; which "
                      "copy is the release is not decided by the bytes")
    total = 0
    members: dict = {}
    for i in infos:
        n = i.filename
        parts = n.split("/")
        if (not n or n.startswith("/") or "\\" in n or "\0" in n
                or n.endswith("/") or any(x in ("", ".", "..")
                                          for x in parts)):
            raise Refused(f"source.zip member name {n[:80]!r} is not a "
                          "plain relative path")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise Refused(f"source.zip member {n} is a symbolic link")
        if i.file_size > MAX_MEMBER:
            raise Refused(f"source.zip member {n} declares {i.file_size} "
                          f"bytes, over the {MAX_MEMBER}-byte bound")
        total += i.file_size
        if total > MAX_TOTAL:
            raise Refused(f"source.zip declares over {MAX_TOTAL} bytes "
                          "uncompressed")
        if (i.file_size > (1 << 20) and i.compress_size
                and i.file_size / i.compress_size > MAX_RATIO):
            raise Refused(f"source.zip member {n} expands "
                          f"{i.file_size // i.compress_size}x; refusing a "
                          "decompression bomb")
        try:
            with zf.open(i) as fh:
                b = fh.read(MAX_MEMBER + 1)
        except _ZIP_ERRORS as exc:
            raise Refused(f"source.zip member {n} is unreadable: "
                          f"{exc}") from None
        if len(b) != i.file_size:
            raise Refused(f"source.zip member {n} holds {len(b)} bytes but "
                          f"declares {i.file_size}")
        members[n] = b
    return members


def sbom_from(read: Callable[[str], bytes]) -> dict:
    """The CycloneDX 1.5 document the declarations name: every package in
    uv.lock (editable and virtual sources excepted) and, as separately
    labelled environments, the FEniCSx conda lock, the FMI runtime lock and
    the sigstore runtime lock. ``read(relpath)`` supplies the declarations:
    a checkout's files at build time, the signed archive's members at
    verification time -- one derivation, two sources."""
    lock = tomllib.loads(read("uv.lock").decode("utf-8"))
    comps = []
    for pkg in sorted(lock.get("package", []), key=lambda p: p["name"]):
        src = pkg.get("source", {})
        if "editable" in src or "virtual" in src:
            continue
        comps.append({"type": "library", "name": pkg["name"],
                      "version": pkg["version"],
                      "purl": f"pkg:pypi/{pkg['name']}@{pkg['version']}",
                      "properties": [{"name": "harness:environment",
                                      "value": "project (uv.lock)"}]})
    for env, rel in ENVIRONMENT_LOCKS.items():
        text = read(rel).decode("utf-8")
        if rel.endswith(".json"):
            for p in json.loads(text)["packages"]:
                comps.append({"type": "library", "name": p["name"],
                              "version": p["version"],
                              "purl": f"pkg:conda/conda-forge/{p['name']}@"
                                      f"{p['version']}",
                              "hashes": [{"alg": "SHA-256",
                                          "content": p["sha256"]}],
                              "properties": [{"name": "harness:environment",
                                              "value": env}]})
        else:
            for line in text.splitlines():
                if "==" in line and not line.startswith((" ", "#")):
                    name, _, ver = line.split()[0].partition("==")
                    comps.append({"type": "library", "name": name,
                                  "version": ver,
                                  "purl": f"pkg:pypi/{name}@{ver}",
                                  "properties": [{"name":
                                                  "harness:environment",
                                                  "value": env}]})
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"component": {
                "type": "application", "name": "scientific-ai-harness"}},
            "components": comps}


def sbom_problems(doc: dict, want: dict) -> list:
    """The SBOM must name exactly what the declarations name."""
    def triples(d):
        return {(c.get("name"), c.get("version"),
                 json.dumps(c.get("properties"), sort_keys=True))
                for c in d.get("components", []) if isinstance(c, dict)}
    have, need = triples(doc), triples(want)
    out = []
    if doc.get("bomFormat") != "CycloneDX" or doc.get("specVersion") != "1.5":
        out.append("not a CycloneDX 1.5 document")
    if have - need:
        out.append(f"components the locks do not name: "
                   f"{sorted(have - need, key=str)[:3]}")
    if need - have:
        out.append(f"locked components missing: "
                   f"{sorted(need - have, key=str)[:3]}")
    return out


def archive_against_manifest(members: dict, index: dict) -> str:
    """The signed archive, compared with the manifest it carries: every
    listed file present with its size and sha256, nothing unlisted, the
    detached hash, and the index's manifest and RO-Crate digests."""
    names = list(members)
    if len(names) != index.get("files_in_source_zip"):
        raise Refused(f"source.zip has {len(names)} members; the index "
                      f"says {index.get('files_in_source_zip')}")
    try:
        man_raw = members["final_manifest.json"]
        doc = json.loads(man_raw)
        hash_txt = members["manifest_hash.txt"].decode("utf-8")
    except (KeyError, ValueError) as exc:
        raise Refused(f"source.zip carries no readable manifest: "
                      f"{exc}") from None
    detached = hash_txt.splitlines()[0] if hash_txt else ""
    if detached != f"sha256: {_sha(man_raw)}":
        raise Refused("manifest_hash.txt in source.zip is not the hash of "
                      "the final_manifest.json beside it")
    entries = {e["filename"]: e for e in doc.get("files", [])}
    if not entries:
        raise Refused("the archived manifest lists no files; a manifest of "
                      "nothing agrees with any archive")
    absent = sorted(set(entries) - set(names))
    unlisted = sorted(set(names) - set(entries) - MANIFEST_DETACHED)
    if absent:
        raise Refused(f"the manifest lists {absent[:5]}, which source.zip "
                      f"does not contain")
    if unlisted:
        raise Refused(f"source.zip contains {unlisted[:5]}, which its "
                      f"manifest does not list")
    for name, e in entries.items():
        data = members[name]
        if _sha(data) != e.get("sha256") or len(data) != e.get("size_bytes"):
            raise Refused(f"{name}: the archive's bytes are not the ones "
                          f"its manifest lists")
    for rel, key in (("final_manifest.json", "final_manifest_sha256"),
                     ("ro-crate/ro-crate-metadata.json", "ro_crate_sha256")):
        if rel not in members or _sha(members[rel]) != index.get(key):
            raise Refused(f"index {key} is not the digest of {rel} in the "
                          "source archive")
    return f"DESCRIBES THE ARCHIVE ({len(entries)} files)"


# ---- the Sigstore call ----------------------------------------------------

def sigstore_check(bundle: Path, sums: Path, policy: dict, *, mode: str,
                   trust_config: Path | None,
                   sigstore_python: str | None) -> str:
    """Run the Sigstore verifier over SHA256SUMS: chain to the trusted
    root, transparency-log inclusion, the signature, and the certificate's
    identity and issuer against the policy -- in one call, which fails
    closed. The subprocess gets an empty environment and an empty home, so
    nothing on this machine but the arguments decides the verdict."""
    if not sigstore_python:
        raise Refused("no sigstore runtime (HARNESS_SIGSTORE_PYTHON) to "
                      "verify with; an unverifiable signature is not a "
                      "verified one")
    cmd = [sigstore_python, "-I", "-m", "sigstore"]
    if trust_config is not None:
        cmd += ["--trust-config", str(trust_config)]
    cmd += ["verify", "identity", "--bundle", str(bundle),
            "--cert-identity", policy["signer_identity"],
            "--cert-oidc-issuer", policy["oidc_issuer"]]
    if mode == "offline":
        cmd.append("--offline")
    cmd.append(str(sums))
    with tempfile.TemporaryDirectory(prefix="ov-home-") as home:
        env = {"HOME": home, "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=300, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refused(f"the sigstore verifier did not run: {exc}") \
                from None
    if proc.returncode != 0:
        raise Refused(f"signature verification failed ({mode}): "
                      f"{(proc.stderr or proc.stdout).strip()[-400:]}")
    return f"sigstore verify identity ({mode}) exit 0"


# ---- the verifier ---------------------------------------------------------

def _level(status: str, detail: str) -> dict:
    return {"status": status, "detail": detail}


def verify_bundle(d: Path, policy: dict, *, mode: str,
                  trust_config: Path | None = None,
                  sigstore_python: str | None = None,
                  signature_checker: Callable | None = None) -> dict:
    """Verify the candidate in ``d`` against ``policy``; fail closed at the
    first refusal; report what was ESTABLISHED level by level.

    ``signature_checker`` replaces the Sigstore subprocess. It exists so the
    level logic can be tested without a signature; the CLI never sets it,
    and a report made with one says so."""
    if mode not in ("online", "offline", "digests"):
        raise Refused(f"unknown mode {mode!r}")
    d = Path(d)
    levels = {n: _level(NOT_CHECKED, "not reached") for n in LEVELS}
    sums_raw = read_bounded(d / SIGNED, MAX_SMALL, "SHA256SUMS")
    listed = parse_sums(sums_raw)

    sig_detail: dict[str, Any] | None = None
    if mode != "digests":
        bundle = d / BUNDLE
        if not (bundle.is_file() or bundle.is_symlink()):
            raise Refused("no signature bundle: a required signature is "
                          "absent")
        bundle_raw = read_bounded(bundle, MAX_JSON, "signature bundle")
        if mode == "offline" and trust_config is None:
            raise Refused("offline verification needs an independently "
                          "provisioned trust configuration (--trust-config);"
                          " the verifier's own sigstore defaults are not a "
                          "trust anchor anyone chose")
        tc_sha = tc_raw = None
        if trust_config is not None:
            if not _outside(trust_config, d):
                raise Refused("the trust configuration lies inside the "
                              "bundle; a release does not supply its own "
                              "trust root")
            tc_raw = read_bounded(Path(trust_config), MAX_JSON,
                                  "trust configuration")
            tc_sha = _sha(tc_raw)
            if is_release_policy(policy) and \
                    tc_sha != policy["trust_config_sha256"]:
                raise Refused("the trust configuration is not the one the "
                              "release policy pins")
        elif is_release_policy(policy):
            raise Refused("a release is admitted only against the trust "
                          "configuration its policy pins")
        check = signature_checker or sigstore_check
        # Sigstore judges private copies of the bytes read above. Handing it
        # the bundle's own paths would let whoever controls the directory
        # swap in a genuinely signed SHA256SUMS after the forged one was
        # parsed: the signature would verify one list and the digests below
        # would be checked against another.
        with tempfile.TemporaryDirectory(prefix="ov-signed-") as priv:
            p_sums, p_bundle = Path(priv) / SIGNED, Path(priv) / BUNDLE
            p_sums.write_bytes(sums_raw)
            p_bundle.write_bytes(bundle_raw)
            p_tc = None
            if tc_raw is not None:
                p_tc = Path(priv) / "trust_config.json"
                p_tc.write_bytes(tc_raw)
            sig_call = check(p_bundle, p_sums, policy, mode=mode,
                             trust_config=p_tc,
                             sigstore_python=sigstore_python)
        sig_detail = {"call": sig_call, "trust_config_sha256": tc_sha,
                      "checker": ("sigstore" if signature_checker is None
                                  else "INJECTED (not a signature)")}

    # ARTIFACT_INTEGRITY
    blobs = {}
    for n in ARTIFACTS:
        cap = MAX_ARCHIVE if n == "source.zip" else MAX_JSON
        blobs[n] = read_bounded(d / n, cap, "artifact")
        if _sha(blobs[n]) != listed[n]:
            raise Refused(f"{n}: its bytes are not the signed digest")
    index = _json(blobs["index.json"], "index.json")
    members = read_archive(blobs["source.zip"])
    manifest = archive_against_manifest(members, index)
    levels["ARTIFACT_INTEGRITY"] = _level(
        ESTABLISHED, f"four artifacts match SHA256SUMS; {manifest}")

    # PROVENANCE_STRUCTURE
    prov = _json(blobs["provenance.intoto.json"], "provenance")
    if prov.get("_type") != "https://in-toto.io/Statement/v1" or \
            prov.get("predicateType") != "https://slsa.dev/provenance/v1":
        raise Refused("the provenance is not an in-toto v1 statement with "
                      "an SLSA provenance v1 predicate")
    try:
        subjects = {s["name"]: s["digest"]["sha256"] for s in prov["subject"]}
        bdef = prov["predicate"]["buildDefinition"]
        deps = bdef["resolvedDependencies"]
        ext = bdef["externalParameters"]
        commit = deps[0]["digest"]["gitCommit"]
    except (KeyError, TypeError, IndexError) as exc:
        raise Refused(f"the provenance lacks a required field: {exc}") \
            from None
    if set(subjects) != {"source.zip", "sbom.cdx.json"}:
        raise Refused(f"provenance subjects are {sorted(subjects)}")
    for n in ("source.zip", "sbom.cdx.json"):
        if subjects.get(n) != listed[n]:
            raise Refused(f"provenance subject {n} is not the signed digest")
    if not _HEX40.match(str(index.get("source_commit", ""))) or \
            commit != index["source_commit"]:
        raise Refused("the provenance's source commit is not the index's")
    for n, digest in (index.get("artifacts") or {}).items():
        if listed.get(n) != digest:
            raise Refused(f"index digest of {n} is not the signed digest")
    if (ext.get("repository"), ext.get("workflow"), ext.get("ref")) != (
            policy["source_repository"], policy["workflow_path"],
            policy["ref"]):
        raise Refused("the provenance names a different builder than the "
                      "policy")
    if index.get("slsa_level_claimed") is not None:
        raise Refused("the index claims an SLSA level; none is claimed here")

    def declared(rel: str) -> bytes:
        if rel not in members:
            raise Refused(f"source.zip lacks {rel}, a declaration the SBOM "
                          "is derived from")
        return members[rel]
    try:
        want = sbom_from(declared)
    except (KeyError, TypeError, ValueError, AttributeError,
            UnicodeDecodeError) as exc:
        raise Refused(f"the release's own dependency declarations do not "
                      f"parse: {exc}") from None
    probs = sbom_problems(_json(blobs["sbom.cdx.json"], "sbom"), want)
    if probs:
        raise Refused(f"SBOM: {probs}")
    levels["PROVENANCE_STRUCTURE"] = _level(
        ESTABLISHED, f"subjects, commit {index['source_commit'][:12]}, "
                     "builder and the SBOM against the release's own "
                     "declarations")

    if sig_detail is None:
        why = "digests mode: nothing authenticated these bytes"
        levels["SIGNATURE_VERIFIED"] = _level(NOT_CHECKED, why)
        levels["SIGNER_TRUSTED"] = _level(NOT_CHECKED, why)
        levels["RELEASE_AUTHORIZED"] = _level(NOT_ESTABLISHED, why)
    else:
        levels["SIGNATURE_VERIFIED"] = _level(
            ESTABLISHED, f"{sig_detail['call']}; trust configuration "
                         f"{sig_detail['trust_config_sha256'] or 'NONE'}; "
                         f"checker {sig_detail['checker']}")
        levels["SIGNER_TRUSTED"] = _level(
            ESTABLISHED, f"identity {policy['signer_identity']} issuer "
                         f"{policy['oidc_issuer']} under a "
                         f"{policy.get('label')} policy")
        if not is_release_policy(policy):
            levels["RELEASE_AUTHORIZED"] = _level(
                NOT_ESTABLISHED, "the policy is CI_VALIDATION_ONLY: its "
                                 "signer exercises the mechanism and "
                                 "authorizes no release")
        else:
            levels["RELEASE_AUTHORIZED"] = _level(
                ESTABLISHED, f"release policy issued by "
                             f"{policy['issued_by']}, valid until "
                             f"{policy['not_after']}; signature checker "
                             f"{sig_detail['checker']}")
    return {
        "schema": "harness-release-verification/1",
        "mode": mode,
        "levels": levels,
        "release_authorized":
            levels["RELEASE_AUTHORIZED"]["status"] == ESTABLISHED,
        "policy": {"schema": policy.get("schema"),
                   "label": policy.get("label"),
                   "sha256": policy.get("_sha256")},
        "source_commit": index["source_commit"],
        "artifacts": dict(listed),
        "manifest": manifest,
        "does_not_attest": ["scientific validity of any result",
                            "that any computation is correct",
                            "an SLSA build level: none is claimed"],
        "_members": members,
    }


def highest(report: dict) -> str:
    """The last level in order whose status is ESTABLISHED, with every
    level before it ESTABLISHED too."""
    top = "NONE"
    for n in LEVELS:
        if report["levels"][n]["status"] != ESTABLISHED:
            break
        top = n
    return top


def meets(report: dict, required: str) -> bool:
    return LEVELS.index(required) <= (
        LEVELS.index(highest(report)) if highest(report) != "NONE" else -1)


# ---- hostile inputs, against a real signed candidate ----------------------

def _copy(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, symlinks=True)
    return dst


def _resum(c: Path) -> None:
    (c / SIGNED).write_text("".join(
        f"{_sha((c / n).read_bytes())}  {n}\n" for n in ARTIFACTS))


def _flip(p: Path, at: int = 200) -> None:
    b = bytearray(p.read_bytes())
    b[at] ^= 0x01
    p.write_bytes(bytes(b))


def hostile(d: Path, policy_path: Path, trust_config: Path,
            sigstore_python: str, *, decoy_bundle: Path | None = None,
            mode: str = "offline") -> dict:
    """Every case changes ONE input and must be refused (or, for the
    release cases, verified but not authorized). The genuine candidate is
    the control and must verify up to SIGNER_TRUSTED."""
    load_policy(policy_path, bundle_dir=d)   # the control's policy loads
    out: dict = {}

    def run(name, mutate, *, expect="refused"):
        with tempfile.TemporaryDirectory(prefix="ov-hostile-") as tmp:
            t = Path(tmp)
            c = _copy(d, t / "rc")
            tc = t / "trust_config.json"
            shutil.copy(trust_config, tc)
            pol = t / "policy.json"
            shutil.copy(policy_path, pol)
            args: dict[str, Any] = {"bundle": c, "policy": pol, "tc": tc,
                                    "mode": mode}
            mutate(args, t)
            try:
                p = load_policy(args["policy"], bundle_dir=args["bundle"])
                rep = verify_bundle(args["bundle"], p, mode=args["mode"],
                                    trust_config=args["tc"],
                                    sigstore_python=sigstore_python)
                got = ("authorized" if rep["release_authorized"]
                       else f"verified to {highest(rep)}")
                reason = ""
            except Refused as exc:
                got, reason = "refused", str(exc)[:240]
        ok = (got == expect) if expect != "verified" else \
            got.startswith("verified to SIGNER_TRUSTED")
        out[name] = {"expected": expect, "got": got, "ok": ok,
                     "reason": reason}

    def edit_json(p: Path, fn):
        doc = json.loads(p.read_text())
        fn(doc)
        p.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")

    def tc_without(key):
        def m(a, t):
            edit_json(a["tc"], lambda x: x["trustedRoot"].update({key: []}))
        return m

    run("control_genuine", lambda a, t: None, expect="verified")
    run("payload_byte_flipped", lambda a, t: _flip(a["bundle"] / "source.zip"))
    def forged_sbom(a, t):
        edit_json(a["bundle"] / "sbom.cdx.json",
                  lambda x: x["components"][0].update(version="0.0.0"))
        _resum(a["bundle"])

    def forged_commit(a, t):
        edit_json(a["bundle"] / "provenance.intoto.json",
                  lambda x: x["predicate"]["buildDefinition"]
                  ["resolvedDependencies"][0]["digest"].update(
                      gitCommit="f" * 40))
        _resum(a["bundle"])

    run("sums_forged_consistently", forged_sbom)
    run("provenance_commit_inconsistent", forged_commit)
    run("signature_bundle_missing",
        lambda a, t: (a["bundle"] / BUNDLE).unlink())
    run("signature_bundle_corrupted", lambda a, t: (a["bundle"] / BUNDLE)
        .write_text((a["bundle"] / BUNDLE).read_text()[:-40]))
    if decoy_bundle is not None:
        run("signature_bundle_substituted", lambda a, t: shutil.copy(
            decoy_bundle, a["bundle"] / BUNDLE))
    run("trust_config_missing", lambda a, t: a.update(tc=None))
    run("trust_config_without_certificate_authorities",
        tc_without("certificateAuthorities"))
    run("trust_config_without_transparency_logs", tc_without("tlogs"))
    run("trust_config_supplied_by_the_bundle", lambda a, t: (
        shutil.copy(a["tc"], a["bundle"] / "trust_config.json"),
        a.update(tc=a["bundle"] / "trust_config.json")))
    run("policy_supplied_by_the_bundle", lambda a, t: (
        shutil.copy(a["policy"], a["bundle"] / "policy.json"),
        a.update(policy=a["bundle"] / "policy.json")))
    run("policy_wrong_identity", lambda a, t: edit_json(
        a["policy"], lambda x: x.update(
            workflow_path=".github/workflows/claude.yml",
            signer_identity=x["signer_identity"].replace(
                "supply-chain.yml", "claude.yml"))))
    run("policy_wrong_issuer", lambda a, t: edit_json(
        a["policy"], lambda x: x.update(
            oidc_issuer="https://accounts.google.com")))

    def release(not_after, ref_tail="refs/tags/v1", workflow=None):
        def m(a, t):
            def fn(x):
                if workflow:
                    x["workflow_path"] = workflow
                x.update(schema=RELEASE_SCHEMA, label="RELEASE_TRUST_ROOT",
                         ref=ref_tail, issued_by="owner",
                         not_after=not_after,
                         trust_config_sha256=_sha(a["tc"].read_bytes()))
                x["signer_identity"] = (f"{x['source_repository']}/"
                                        f"{x['workflow_path']}@{x['ref']}")
            edit_json(a["policy"], fn)
        return m
    run("release_policy_naming_the_ci_workflow", release("2999-01-01"))
    run("release_policy_expired", release(
        "2000-01-01", workflow=".github/workflows/release.yml"))
    # A release policy that is valid, external and pinned -- but the
    # candidate was signed by the CI identity, so the signature does not
    # verify against the release identity: CI signatures never release.
    run("release_policy_against_a_ci_signature", release(
        "2999-01-01", workflow=".github/workflows/release.yml"))
    run("release_policy_on_a_branch", release(
        "2999-01-01", "refs/heads/claude/scientific-python-stack-7rsq3m"))
    return {"cases": out, "all_as_expected": all(c["ok"]
                                                 for c in out.values())}


# ---- CLI -------------------------------------------------------------------

def _report_text(rep: dict) -> str:
    return json.dumps({k: v for k, v in rep.items() if not k.startswith("_")},
                      indent=1, sort_keys=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("verify", "hostile"):
        p = sub.add_parser(name)
        p.add_argument("--bundle", required=True, type=Path)
        p.add_argument("--policy", required=True, type=Path)
        p.add_argument("--trust-config", type=Path)
        p.add_argument("--sigstore-python",
                       default=os.environ.get("HARNESS_SIGSTORE_PYTHON"))
        p.add_argument("--mode", default="offline",
                       choices=("offline", "online", "digests"))
        p.add_argument("--report", type=Path)
    sub.choices["verify"].add_argument("--require", default="SIGNER_TRUSTED",
                                       choices=LEVELS)
    sub.choices["hostile"].add_argument("--decoy-bundle", type=Path)
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_usage()
        return 2
    try:
        if args.cmd == "hostile":
            if args.trust_config is None or not args.sigstore_python:
                print("hostile needs --trust-config and --sigstore-python")
                return 2
            res = hostile(args.bundle, args.policy, args.trust_config,
                          args.sigstore_python,
                          decoy_bundle=args.decoy_bundle, mode=args.mode)
            text = json.dumps(res, indent=1, sort_keys=True)
            if args.report:
                args.report.write_text(text + "\n", encoding="utf-8")
            print(text)
            return 0 if res["all_as_expected"] else 1
        policy = load_policy(args.policy, bundle_dir=args.bundle)
        rep = verify_bundle(args.bundle, policy, mode=args.mode,
                            trust_config=args.trust_config,
                            sigstore_python=args.sigstore_python)
    except Refused as exc:
        print(f"RELEASE VERIFICATION REFUSED: {exc}")
        return 1
    rep["highest_established"] = highest(rep)
    text = _report_text(rep)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not meets(rep, args.require):
        print(f"REQUIRED LEVEL NOT ESTABLISHED: {args.require} "
              f"(highest: {rep['highest_established']})")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
