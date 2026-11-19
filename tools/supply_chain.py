#!/usr/bin/env python3
"""The harness release candidate: built, described, signed, verified, and
refused when any of it is tampered with.

``build`` writes, into one directory, for the commit checked out:

* ``source.zip``        -- every tracked file at that commit, members in
                           sorted order with a fixed timestamp and mode, and
                           STORED, not compressed: a deflate stream depends
                           on the zlib build that wrote it, so a compressed
                           archive's bytes would be a property of the
                           builder's machine as well as of the commit
                           (D-2026-145, docs/security/
                           reproducibility_contract.json);
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

``verify`` is ``tools/offline_verify.py`` -- the portable verifier, which
needs no checkout -- plus, when a git checkout is at hand, ``source.zip``
against the tree of the commit it names, in both directions. It reports
five levels that are NOT interchangeable (D-2026-143): ARTIFACT_INTEGRITY,
PROVENANCE_STRUCTURE, SIGNATURE_VERIFIED, SIGNER_TRUSTED and
RELEASE_AUTHORIZED. A ``--mode digests`` run establishes the first two and
nothing else; no report carries a single "accepted" flag.

``admit`` is the release-admission gate: it verifies against a RELEASE
policy (``harness-release-policy/1``) provisioned outside both the bundle
and this checkout, and succeeds only when RELEASE_AUTHORIZED is
established. No release policy exists; the CI validation policy can never
satisfy it, and the hosted job proves that by requiring the refusal.

``compare`` holds two builds of one commit to
``docs/security/reproducibility_contract.json``: the files it names
byte-identical must be, the files it names identical-except must differ in
no field outside the ones it names, and the run-specific files are not
compared. The hosted job rebuilds on another runner image and compares
with the signed candidate.

``tamper`` copies a built-and-signed directory once per case, changes one
thing -- a payload byte, the manifest digest, an SBOM component, the
provenance's subject digest, the signature bundle (removed), the identity,
the issuer -- and requires ``verify`` to refuse every copy.

A valid signature means origin and integrity of these bytes. It does not
mean that any number in them is correct.

    python tools/supply_chain.py build --out DIR [--allow-dirty]
    python tools/supply_chain.py verify --dir DIR --policy P \
        --mode online|offline|digests [--trust-config T]
    python tools/supply_chain.py admit --dir DIR --release-policy P \
        --trust-config T --mode online|offline
    python tools/supply_chain.py tamper --dir DIR --policy P \
        --mode online|offline [--trust-config T]
    python tools/supply_chain.py compare --a DIR --b DIR [--report OUT]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import offline_verify as OV  # noqa: E402

# One definition of each, in the portable verifier.
ARTIFACTS = OV.ARTIFACTS
SIGNED = OV.SIGNED
BUNDLE = OV.BUNDLE
ENVIRONMENT_LOCKS = OV.ENVIRONMENT_LOCKS
MANIFEST_DETACHED = OV.MANIFEST_DETACHED
LEVELS = OV.LEVELS
Refused = OV.Refused
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ATTESTS = ("origin: the bytes were signed by the exact workflow identity the "
           "policy names", "integrity: no byte listed in SHA256SUMS changed "
           "after signing")
CONTRACT = ROOT / "docs" / "security" / "reproducibility_contract.json"
DOES_NOT_ATTEST = ("scientific validity of any result",
                   "that any computation is correct",
                   "an SLSA build level: none is claimed")


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
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
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
            info.compress_type = zipfile.ZIP_STORED
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
    """The SBOM a checkout's declarations name -- the same derivation the
    verifier applies to the declarations inside the signed archive."""
    return OV.sbom_from(lambda rel: (root / rel).read_bytes())


def sbom_problems(doc: dict, root: Path = ROOT) -> list:
    """The SBOM against a CHECKOUT's declarations. Verification does not
    use this: it compares against the release's own (D-2026-144)."""
    return OV.sbom_problems(doc, sbom(root))


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
            "deterministic_payload": {
                n: _sha((out / n).read_bytes())
                for n in ("source.zip", "sbom.cdx.json")},
            "sha256sums_sha256": _sha(sums.encode())}


# ---- verify --------------------------------------------------------------

def load_policy(path: Path, *, bundle_dir: Path | None = None) -> dict:
    return OV.load_policy(path, bundle_dir=bundle_dir)


def verify(d: Path, policy: dict, *, mode: str,
           sigstore_python: str | None = None, repo: Path | None = ROOT,
           trust_config: Path | None = None,
           signature_checker=None) -> dict:
    """The portable verification, then -- when a checkout is at hand --
    source.zip against the tree of the commit it names."""
    d = Path(d)
    rep = OV.verify_bundle(
        d, policy, mode=mode, trust_config=trust_config,
        sigstore_python=(sigstore_python
                         or os.environ.get("HARNESS_SIGSTORE_PYTHON", "")),
        signature_checker=signature_checker)
    members = rep.pop("_members")
    tree = "NOT_CHECKED (no checkout)"
    if repo is not None:
        commit = rep["source_commit"]
        # BOTH DIRECTIONS. Each member is compared with the commit's blob
        # below; that alone accepts an archive that LOST files, and said
        # "MATCHES" over it (D-2026-127).
        packed = set(members)
        tracked = commit_files(commit, cwd=repo)
        if packed != tracked:
            raise Refused(
                f"source.zip is not the commit's tree: missing "
                f"{sorted(tracked - packed)[:5]}, not in the commit "
                f"{sorted(packed - tracked)[:5]}")
        for name, data in members.items():
            want = subprocess.run(["git", "cat-file", "blob",
                                   f"{commit}:{name}"],
                                  cwd=repo, capture_output=True)
            if want.returncode != 0 or _sha(want.stdout) != _sha(data):
                raise Refused(f"source.zip member {name} is not the "
                              f"commit's")
        tree = f"MATCHES {commit}"
    rep["source_tree"] = tree
    rep["highest_established"] = OV.highest(rep)
    return rep


def admit(d: Path, release_policy: Path, *, mode: str,
          trust_config: Path | None, sigstore_python: str | None = None,
          checkout: Path = ROOT, signature_checker=None) -> dict:
    """The release-admission gate. Admits only what RELEASE_AUTHORIZED
    establishes, against a release policy that neither the bundle nor this
    checkout supplies: a policy anyone who can push here can edit
    authorizes nothing (D-2026-143)."""
    if mode == "digests":
        raise Refused("a digest-only verification never admits a release: "
                      "nothing in it is authenticated")
    if not OV._outside(release_policy, checkout):
        raise Refused("the release policy lies inside the repository "
                      "checkout; anyone who can push here could edit it")
    policy = load_policy(release_policy, bundle_dir=d)
    rep = verify(d, policy, mode=mode, sigstore_python=sigstore_python,
                 repo=None, trust_config=trust_config,
                 signature_checker=signature_checker)
    rep["admitted"] = rep["release_authorized"]
    return rep


# ---- reproducibility ----------------------------------------------------

def _pop_field(doc: dict, dotted: str) -> None:
    """Remove ``dotted`` from ``doc``. A key may itself contain dots (the
    index's artifact names do), so each step takes the longest run of
    parts that is a key. A field the contract names but the document lacks
    is refused: the contract would be describing some other build."""
    parts, cur, i = dotted.split("."), doc, 0
    while True:
        for j in range(len(parts), i, -1):
            key = ".".join(parts[i:j])
            if isinstance(cur, dict) and key in cur:
                break
        else:
            raise Refused(f"the contract names {dotted!r}, which the "
                          "build does not have")
        if j == len(parts):
            cur.pop(key)
            return
        cur, i = cur[key], j


def compare_builds(a: Path, b: Path, contract: dict) -> dict:
    """Two builds of one commit, held to the reproducibility contract. Byte
    identity of files only; nothing here is a numerical comparison."""
    files: dict[str, dict] = {}
    for name in contract["byte_identical"]:
        ra, rb = (Path(a) / name).read_bytes(), (Path(b) / name).read_bytes()
        files[name] = {"class": "byte_identical", "holds": ra == rb,
                       "sha256": sorted({_sha(ra), _sha(rb)})}
    for name, spec in contract["identical_except"].items():
        ra, rb = (Path(a) / name).read_bytes(), (Path(b) / name).read_bytes()
        da, db = json.loads(ra), json.loads(rb)
        for f in spec["fields"]:
            _pop_field(da, f)
            _pop_field(db, f)
        files[name] = {"class": "identical_except", "fields": spec["fields"],
                       "holds": da == db, "byte_identical": ra == rb}
    for name in contract["run_specific"]:
        files[name] = {"class": "run_specific", "compared": False}
    return {"schema": "harness-release-reproduction/1",
            "contract": contract["schema"], "files": files,
            "holds": all(f.get("holds", True) for f in files.values())}


# ---- tamper suite --------------------------------------------------------

def _flip(p: Path, offset: int = 64):
    b = bytearray(p.read_bytes())
    b[offset] ^= 0x01
    p.write_bytes(bytes(b))


def tamper(d: Path, policy: dict, *, mode: str,
           sigstore_python: str | None = None,
           trust_config: Path | None = None) -> dict:
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
                verify(c, pol, mode=mode, sigstore_python=sigstore_python,
                       trust_config=trust_config)
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
        v.add_argument("--trust-config", type=Path)
        v.add_argument("--report", type=Path)
    a = sub.add_parser("admit")
    a.add_argument("--dir", required=True, type=Path)
    a.add_argument("--release-policy", required=True, type=Path)
    a.add_argument("--trust-config", type=Path)
    a.add_argument("--mode", required=True, choices=("online", "offline"))
    a.add_argument("--report", type=Path)
    c = sub.add_parser("compare")
    c.add_argument("--a", required=True, type=Path)
    c.add_argument("--b", required=True, type=Path)
    c.add_argument("--contract", type=Path, default=CONTRACT)
    c.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            res = build(args.out, allow_dirty=args.allow_dirty)
        elif args.cmd == "verify":
            res = verify(args.dir,
                         load_policy(args.policy, bundle_dir=args.dir),
                         mode=args.mode, trust_config=args.trust_config)
        elif args.cmd == "admit":
            res = admit(args.dir, args.release_policy, mode=args.mode,
                        trust_config=args.trust_config)
        elif args.cmd == "compare":
            res = compare_builds(args.a, args.b, json.loads(
                args.contract.read_text(encoding="utf-8")))
            res["builder"] = {"python": sys.version.split()[0],
                              "platform": platform.platform()}
        else:
            res = tamper(args.dir, load_policy(args.policy), mode=args.mode,
                         trust_config=args.trust_config)
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
    if args.cmd == "compare" and not res["holds"]:
        broken = sorted(n for n, f in res["files"].items()
                        if f.get("holds") is False)
        print(f"REPRODUCIBILITY CONTRACT BROKEN: {broken}")
        return 1
    if args.cmd == "admit" and not res["admitted"]:
        print("RELEASE NOT ADMITTED: "
              f"{res['levels']['RELEASE_AUTHORIZED']['detail']}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
