#!/usr/bin/env python3
"""The harness release candidate: built, described, signed, verified, and
refused when any of it is tampered with.

``build`` writes, into one directory, for the commit checked out:

* ``source.zip``        -- every tracked file at that commit, members in
                           sorted order with a fixed timestamp and mode, so
                           the same commit always packs to the same bytes;
* ``sbom.cdx.json``     -- CycloneDX 1.5: every package in ``uv.lock``, and
                           -- as separate, labelled environments -- the
                           FEniCSx conda lock, the FMI runtime lock and the
                           sigstore runtime lock;
* ``provenance.intoto.json`` -- an in-toto Statement v1 whose predicate
                           follows the SLSA provenance v1 layout: subjects
                           by sha256, the source commit as the resolved
                           dependency, the workflow as the builder. NO SLSA
                           level is claimed (see ``index.json``);
* ``index.json``        -- the source commit, the manifest's and the
                           RO-Crate's digests as they are in that commit,
                           every artifact's digest, and what a signature
                           here does and does not attest;
* ``SHA256SUMS``        -- one line per artifact above. THIS is the file that
                           is signed: one signature binds all of them.

``verify`` checks, failing closed at the first refusal: the signature on
``SHA256SUMS`` against the EXACT signer identity and issuer of a policy
(``docs/supply_chain_ci_policy.json`` for a CI-validation run) -- online, or
offline from the bundle's own transparency-log inclusion proof; every
artifact's digest against ``SHA256SUMS``; the provenance's subjects against
the same digests; the index's commit against the provenance's resolved
dependency; the SBOM against ``uv.lock``; ``source.zip`` against the
manifest it carries (every listed file present with its size and sha256,
nothing unlisted, the detached hash); and, when a git checkout is at hand,
``source.zip`` against the tree of the commit it names, in both directions.

``tamper`` copies a built-and-signed directory once per case, changes one
thing -- a payload byte, the manifest digest, an SBOM component, the
provenance's subject digest, the signature bundle (removed), the identity,
the issuer -- and requires ``verify`` to refuse every copy.

A valid signature means origin and integrity of these bytes. It does not
mean that any number in them is correct.

    python tools/supply_chain.py build --out DIR [--allow-dirty]
    python tools/supply_chain.py verify --dir DIR --policy P \
        --mode online|offline|digests
    python tools/supply_chain.py tamper --dir DIR --policy P \
        --mode online|offline
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ("source.zip", "sbom.cdx.json", "provenance.intoto.json",
             "index.json")
SIGNED = "SHA256SUMS"
BUNDLE = "SHA256SUMS.sigstore.json"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ENVIRONMENT_LOCKS = {
    "fenicsx-conda": "integrations/fenicsx/environment.sha256.json",
    "fmi-runtime": "integrations/fmi/runtime.lock",
    "sigstore-runtime": "integrations/supply_chain/sigstore.lock",
}
ATTESTS = ("origin: the bytes were signed by the exact workflow identity the "
           "policy names", "integrity: no byte listed in SHA256SUMS changed "
           "after signing")
DOES_NOT_ATTEST = ("scientific validity of any result",
                   "that any computation is correct",
                   "an SLSA build level: none is claimed")


class Refused(RuntimeError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


# ---- build ---------------------------------------------------------------

def source_zip(dest: Path, commit: str, cwd: Path = ROOT) -> list:
    """Every file of ``commit``'s tree, read from the object store (not the
    working tree), packed deterministically."""
    names = sorted(_git("ls-tree", "-r", "-z", "--name-only", commit,
                        cwd=cwd).split("\0"))
    names = [n for n in names if n]
    modes = {}
    for line in _git("ls-tree", "-r", commit, cwd=cwd).splitlines():
        meta, _, path = line.partition("\t")
        modes[path] = meta.split()[0]
    packed = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in names:
            if modes.get(n) == "120000":
                continue            # links are not packaged
            packed.append(n)
            data = subprocess.run(["git", "cat-file", "blob",
                                   f"{commit}:{n}"], cwd=cwd, check=True,
                                  capture_output=True).stdout
            info = zipfile.ZipInfo(n, date_time=ZIP_TIME)
            info.external_attr = (0o755 if modes.get(n) == "100755"
                                  else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return packed


def commit_files(commit: str, cwd: Path = ROOT) -> set:
    """The files ``source_zip`` packs for ``commit``: every blob in its tree
    except symbolic links."""
    out = set()
    for line in _git("ls-tree", "-r", "-z", commit, cwd=cwd).split("\0"):
        meta, _, path = line.partition("\t")
        if path and meta.split()[0] != "120000":
            out.add(path)
    return out


def sbom(root: Path = ROOT) -> dict:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
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
        text = (root / rel).read_text(encoding="utf-8")
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


def sbom_problems(doc: dict, root: Path = ROOT) -> list:
    """The SBOM must name exactly what the locks name, no more, no less."""
    want = sbom(root)
    have = {(c["name"], c["version"], json.dumps(c.get("properties"),
                                                 sort_keys=True))
            for c in doc.get("components", [])}
    need = {(c["name"], c["version"], json.dumps(c.get("properties"),
                                                 sort_keys=True))
            for c in want["components"]}
    out = []
    if doc.get("bomFormat") != "CycloneDX" or doc.get("specVersion") != "1.5":
        out.append("not a CycloneDX 1.5 document")
    if have - need:
        out.append(f"components the locks do not name: "
                   f"{sorted(have - need)[:3]}")
    if need - have:
        out.append(f"locked components missing: {sorted(need - have)[:3]}")
    return out


def provenance(subjects: dict, commit: str, policy: dict) -> dict:
    run = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "0")
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": n, "digest": {"sha256": d}}
                    for n, d in sorted(subjects.items())],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://github.com/cakeisalie89/Quantum-Thermal-"
                             "/supply-chain/v1",
                "externalParameters": {
                    "repository": policy["source_repository"],
                    "workflow": policy["workflow_path"],
                    "ref": policy["ref"]},
                "resolvedDependencies": [{
                    "uri": f"git+{policy['source_repository']}@"
                           f"{policy['ref']}",
                    "digest": {"gitCommit": commit}}]},
            "runDetails": {
                "builder": {"id": f"github-actions://"
                                  f"{policy['source_repository'][19:]}/"
                                  f"{policy['workflow_path']}@"
                                  f"{policy['ref']}"},
                "metadata": {"invocationId": f"{run}/{attempt}"}}}}


def build(out: Path, *, allow_dirty: bool = False,
          policy_path: Path | None = None) -> dict:
    policy = load_policy(policy_path or ROOT / "docs" /
                         "supply_chain_ci_policy.json")
    commit = _git("rev-parse", "HEAD").strip()
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    if dirty and not allow_dirty:
        raise Refused("the working tree differs from HEAD; a release "
                      "candidate is built from a commit, not from edits")
    out.mkdir(parents=True, exist_ok=True)
    names = source_zip(out / "source.zip", commit)
    (out / "sbom.cdx.json").write_text(
        json.dumps(sbom(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    payload = {n: _sha((out / n).read_bytes())
               for n in ("source.zip", "sbom.cdx.json")}
    (out / "provenance.intoto.json").write_text(
        json.dumps(provenance(payload, commit, policy), indent=1,
                   sort_keys=True) + "\n", encoding="utf-8")

    def in_commit(rel):
        return _sha(subprocess.run(["git", "cat-file", "blob",
                                    f"{commit}:{rel}"], cwd=ROOT,
                                   check=True, capture_output=True).stdout)

    index = {"schema": "harness-release-candidate/1",
             "source_commit": commit, "working_tree_dirty": dirty,
             "files_in_source_zip": len(names),
             "final_manifest_sha256": in_commit("final_manifest.json"),
             "ro_crate_sha256": in_commit("ro-crate/ro-crate-metadata.json"),
             "artifacts": {n: _sha((out / n).read_bytes()) for n in
                           ("source.zip", "sbom.cdx.json",
                            "provenance.intoto.json")},
             "signature": {"signed_file": SIGNED, "bundle": BUNDLE,
                           "attests": list(ATTESTS),
                           "does_not_attest": list(DOES_NOT_ATTEST)},
             "slsa_level_claimed": None}
    (out / "index.json").write_text(json.dumps(index, indent=1,
                                               sort_keys=True) + "\n",
                                    encoding="utf-8")
    sums = "".join(f"{_sha((out / n).read_bytes())}  {n}\n"
                   for n in ARTIFACTS)
    (out / SIGNED).write_text(sums, encoding="utf-8")
    return {"out": str(out), "commit": commit, "dirty": dirty,
            "sha256sums_sha256": _sha(sums.encode())}


# ---- verify --------------------------------------------------------------

def load_policy(path: Path) -> dict:
    p = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(p, dict):
        raise Refused("a policy is a JSON object")
    for k in ("signer_identity", "oidc_issuer", "source_repository",
              "workflow_path", "ref"):
        v = p.get(k, "")
        if not isinstance(v, str) or not v or "*" in v or "PENDING" in v:
            raise Refused(f"policy {k} is not an exact value: {v!r}")
    want = (f"{p['source_repository']}/{p['workflow_path']}@{p['ref']}")
    if p["signer_identity"] != want:
        raise Refused(f"policy signer_identity is not derived from its "
                      f"repository, workflow and ref ({want})")
    if p["oidc_issuer"] != "https://token.actions.githubusercontent.com":
        raise Refused("policy issuer is not GitHub Actions' OIDC issuer")
    return p


def verify_signature(d: Path, policy: dict, *, mode: str,
                     sigstore_python: str | None) -> str:
    if mode == "digests":
        return "NOT_CHECKED (digests mode)"
    bundle = d / BUNDLE
    if not bundle.is_file():
        raise Refused("no signature bundle: a required signature is absent")
    exe = sigstore_python or os.environ.get("QTA_SIGSTORE_PYTHON", "")
    if not exe:
        raise Refused("no sigstore runtime (QTA_SIGSTORE_PYTHON) to verify "
                      "with; an unverifiable signature is not a verified one")
    cmd = [exe, "-I", "-m", "sigstore", "verify", "identity",
           "--bundle", str(bundle), "--cert-identity",
           policy["signer_identity"], "--cert-oidc-issuer",
           policy["oidc_issuer"]]
    if mode == "offline":
        cmd.append("--offline")
    cmd.append(str(d / SIGNED))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise Refused(f"signature verification failed ({mode}): "
                      f"{(proc.stderr or proc.stdout).strip()[-400:]}")
    return f"VERIFIED ({mode})"


def verify(d: Path, policy: dict, *, mode: str,
           sigstore_python: str | None = None, repo: Path | None = ROOT,
           root: Path = ROOT) -> dict:
    d = Path(d)
    sig = verify_signature(d, policy, mode=mode,
                           sigstore_python=sigstore_python)
    lines = (d / SIGNED).read_text(encoding="utf-8").splitlines()
    listed = {}
    for ln in lines:
        digest, _, name = ln.partition("  ")
        listed[name] = digest
    if set(listed) != set(ARTIFACTS):
        raise Refused(f"SHA256SUMS lists {sorted(listed)}, not "
                      f"{sorted(ARTIFACTS)}")
    for n, digest in listed.items():
        if _sha((d / n).read_bytes()) != digest:
            raise Refused(f"{n}: its bytes are not the signed digest")
    prov = json.loads((d / "provenance.intoto.json").read_text())
    subjects = {s["name"]: s["digest"]["sha256"] for s in prov["subject"]}
    for n in ("source.zip", "sbom.cdx.json"):
        if subjects.get(n) != listed[n]:
            raise Refused(f"provenance subject {n} is not the signed digest")
    index = json.loads((d / "index.json").read_text())
    deps = prov["predicate"]["buildDefinition"]["resolvedDependencies"]
    if deps[0]["digest"]["gitCommit"] != index["source_commit"]:
        raise Refused("the provenance's source commit is not the index's")
    for n, digest in index["artifacts"].items():
        if listed.get(n) != digest:
            raise Refused(f"index digest of {n} is not the signed digest")
    probs = sbom_problems(json.loads((d / "sbom.cdx.json").read_text()), root)
    if probs:
        raise Refused(f"SBOM: {probs}")
    manifest = archive_against_manifest(d / "source.zip", index)
    tree = "NOT_CHECKED (no checkout)"
    if repo is not None:
        commit = index["source_commit"]
        with zipfile.ZipFile(d / "source.zip") as zf:
            # BOTH DIRECTIONS. Each member is compared with the commit's blob
            # below; that alone accepts an archive that LOST files, and said
            # "MATCHES" over it (D-2026-127).
            packed = {i.filename for i in zf.infolist()}
            tracked = commit_files(commit, cwd=repo)
            if packed != tracked:
                raise Refused(
                    f"source.zip is not the commit's tree: missing "
                    f"{sorted(tracked - packed)[:5]}, not in the commit "
                    f"{sorted(packed - tracked)[:5]}")
            for info in zf.infolist():
                want = subprocess.run(["git", "cat-file", "blob",
                                       f"{commit}:{info.filename}"],
                                      cwd=repo, capture_output=True)
                if want.returncode != 0 or \
                        _sha(want.stdout) != _sha(zf.read(info)):
                    raise Refused(f"source.zip member {info.filename} is not "
                                  f"the commit's")
            for rel, key in (("final_manifest.json", "final_manifest_sha256"),
                             ("ro-crate/ro-crate-metadata.json",
                              "ro_crate_sha256")):
                if _sha(zf.read(rel)) != index[key]:
                    raise Refused(f"index {key} is not the digest of {rel} in "
                                  "the source archive")
        tree = f"MATCHES {commit}"
    return {"signature": sig, "artifacts": "MATCH SHA256SUMS",
            "provenance": "SUBJECTS MATCH", "sbom": "MATCHES THE LOCKS",
            "manifest": manifest, "source_tree": tree, "accepted": True}


#: Tracked files the manifest does not list, by its own policy: it cannot
#: hash itself, and manifest_hash.txt is that hash, written after it.
MANIFEST_DETACHED = frozenset({"final_manifest.json", "manifest_hash.txt"})


def archive_against_manifest(src: Path, index: dict) -> str:
    """The signed archive, compared with the manifest it carries.

    The signature binds source.zip's bytes. The manifest inside it says what
    the release IS: every file, its size, its sha256. Unless the two are
    compared, a signed archive can carry a manifest that describes other
    bytes, or lack files the manifest lists, and still verify (D-2026-127).
    Needs no checkout: it is the archive against itself.
    """
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise Refused("source.zip names a member more than once; which "
                          "copy is the release is not decided by the bytes")
        if len(names) != index.get("files_in_source_zip"):
            raise Refused(f"source.zip has {len(names)} members; the index "
                          f"says {index.get('files_in_source_zip')}")
        try:
            doc = json.loads(zf.read("final_manifest.json"))
            hash_txt = zf.read("manifest_hash.txt").decode("utf-8")
        except (KeyError, ValueError) as exc:
            raise Refused(f"source.zip carries no readable manifest: "
                          f"{exc}") from exc
        detached = hash_txt.splitlines()[0] if hash_txt else ""
        if detached != f"sha256: {_sha(zf.read('final_manifest.json'))}":
            raise Refused("manifest_hash.txt in source.zip is not the hash of "
                          "the final_manifest.json beside it")
        entries = {e["filename"]: e for e in doc["files"]}
        if not entries:
            raise Refused("the archived manifest lists no files; a manifest "
                          "of nothing agrees with any archive")
        absent = sorted(set(entries) - set(names))
        unlisted = sorted(set(names) - set(entries) - MANIFEST_DETACHED)
        if absent:
            raise Refused(f"the manifest lists {absent[:5]}, which source.zip "
                          f"does not contain")
        if unlisted:
            raise Refused(f"source.zip contains {unlisted[:5]}, which its "
                          f"manifest does not list")
        for name, e in entries.items():
            data = zf.read(name)
            if _sha(data) != e["sha256"] or len(data) != e["size_bytes"]:
                raise Refused(f"{name}: the archive's bytes are not the ones "
                              f"its manifest lists")
    return f"DESCRIBES THE ARCHIVE ({len(entries)} files)"


# ---- tamper suite --------------------------------------------------------

def _flip(p: Path, offset: int = 64):
    b = bytearray(p.read_bytes())
    b[offset] ^= 0x01
    p.write_bytes(bytes(b))


def tamper(d: Path, policy: dict, *, mode: str,
           sigstore_python: str | None = None) -> dict:
    def edit_json(p: Path, fn):
        doc = json.loads(p.read_text())
        fn(doc)
        p.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")

    cases = {
        "payload_byte_flipped": lambda c, pol: _flip(c / "source.zip", 200),
        "manifest_digest_changed": lambda c, pol: edit_json(
            c / "index.json",
            lambda x: x.update(final_manifest_sha256="0" * 64)),
        "sbom_component_changed": lambda c, pol: edit_json(
            c / "sbom.cdx.json",
            lambda x: x["components"][0].update(version="0.0.0-tampered")),
        "provenance_subject_changed": lambda c, pol: edit_json(
            c / "provenance.intoto.json",
            lambda x: x["subject"][0]["digest"].update(sha256="0" * 64)),
        "signature_bundle_missing": lambda c, pol: (c / BUNDLE).unlink(
            missing_ok=True),
        "wrong_identity": lambda c, pol: pol.update(
            signer_identity=pol["signer_identity"].replace(
                "supply-chain.yml", "release.yml")),
        "wrong_issuer": lambda c, pol: pol.update(
            oidc_issuer="https://accounts.google.com"),
    }
    out: dict[str, dict] = {}
    for name, mutate in cases.items():
        with tempfile.TemporaryDirectory(prefix="tamper-") as tmp:
            c = Path(tmp) / "rc"
            shutil.copytree(d, c)
            pol = dict(policy)
            mutate(c, pol)
            try:
                verify(c, pol, mode=mode, sigstore_python=sigstore_python)
                out[name] = {"refused": False}
            except Refused as exc:
                out[name] = {"refused": True, "reason": str(exc)[:300]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--allow-dirty", action="store_true")
    for name in ("verify", "tamper"):
        v = sub.add_parser(name)
        v.add_argument("--dir", required=True, type=Path)
        v.add_argument("--policy", type=Path,
                       default=ROOT / "docs" / "supply_chain_ci_policy.json")
        v.add_argument("--mode", required=True,
                       choices=("online", "offline", "digests"))
        v.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            res = build(args.out, allow_dirty=args.allow_dirty)
        elif args.cmd == "verify":
            res = verify(args.dir, load_policy(args.policy), mode=args.mode)
        else:
            res = tamper(args.dir, load_policy(args.policy), mode=args.mode)
            res = {"cases": res,
                   "all_refused": all(c["refused"] for c in res.values())}
    except (Refused, subprocess.CalledProcessError, OSError,
            json.JSONDecodeError, KeyError) as exc:
        print(f"SUPPLY CHAIN REFUSED: {exc}")
        return 1
    text = json.dumps(res, indent=1, sort_keys=True)
    if getattr(args, "report", None):
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.cmd == "tamper" and not res["all_refused"]:
        print("SUPPLY CHAIN TAMPER SUITE: a tampered copy was ACCEPTED")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
