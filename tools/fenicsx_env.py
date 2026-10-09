#!/usr/bin/env python3
"""The FEniCSx verification environment: created from a lock, checked by hash.

dolfinx is not a wheel and does not belong in the project's uv environment:
it brings MPI, PETSc and a compiler toolchain's runtime with it. It lives in
its own conda-forge prefix, created from an explicit lock
(``integrations/fenicsx/environment.lock``: every package by URL and md5),
and nothing in the core environment imports it. The harness talks to it only
through ``integrations/fenicsx/runner.py``, a JSON-in / JSON-out program run
with that prefix's interpreter.

What this tool establishes, step by step, and refuses otherwise:

* ``check-lock``  -- the lock and ``environment.sha256.json`` name the same
  packages, every one from conda-forge over https, every md5 agreeing; so
  the sha256 record is a record of THIS lock.
* ``create``      -- micromamba is downloaded from conda-forge and its
  tarball's sha256 compared with the pinned value BEFORE it is unpacked;
  the environment is created from the lock (micromamba checks each md5);
  then every package file in the cache is hashed and compared with the
  sha256 conda-forge publishes for it. A missing file or a mismatch fails.
* ``verify-cache`` -- the last step on its own.

Nothing here decides a scientific result. It answers "is the environment the
one the lock names", so a FEniCSx check's provenance can say which build ran.

    python tools/fenicsx_env.py check-lock
    python tools/fenicsx_env.py create --prefix P --root R
    python tools/fenicsx_env.py verify-cache --root R
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "integrations" / "fenicsx" / "environment.lock"
SHA_RECORD = ROOT / "integrations" / "fenicsx" / "environment.sha256.json"
CHANNEL = "https://conda.anaconda.org/conda-forge/"
MICROMAMBA = {
    "url": CHANNEL + "linux-64/micromamba-2.9.0-0.tar.bz2",
    "sha256": "8761c382127e6363bd9e0a2451aa3ef9"
              "0d071a79133f736e2f759a3bf13040dd",
}


class EnvError(RuntimeError):
    pass


def lock_entries(text: str) -> dict:
    """{url: md5} from an explicit lock; refuses anything else."""
    out = {}
    explicit = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "@EXPLICIT":
            explicit = True
            continue
        url, sep, md5 = line.partition("#")
        if not explicit or not sep:
            raise EnvError(f"not an explicit, hashed lock line: {line!r}")
        if not url.startswith(CHANNEL):
            raise EnvError(f"{url}: only conda-forge over https is admitted")
        if len(md5) != 32 or any(c not in "0123456789abcdef" for c in md5):
            raise EnvError(f"{url}: md5 {md5!r} is not 32 hex digits")
        if url in out:
            raise EnvError(f"{url}: listed twice")
        out[url] = md5
    if not explicit or not out:
        raise EnvError("the lock has no @EXPLICIT section or no packages")
    return out


def sha_record(text: str) -> dict:
    rec = json.loads(text)
    if rec.get("schema") != "conda-explicit-sha256/1" or \
            rec.get("platform") != "linux-64":
        raise EnvError("environment.sha256.json: unknown schema or platform")
    out = {}
    for p in rec["packages"]:
        sha = p["sha256"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise EnvError(f"{p['url']}: sha256 is not 64 hex digits")
        out[p["url"]] = (p["md5"], sha)
    return out


def check_lock(lock_text: str, record_text: str) -> int:
    lock = lock_entries(lock_text)
    rec = sha_record(record_text)
    if set(lock) != set(rec):
        raise EnvError(f"lock and sha256 record differ: only in lock "
                       f"{sorted(set(lock) - set(rec))[:3]}, only in record "
                       f"{sorted(set(rec) - set(lock))[:3]}")
    bad = [u for u in lock if lock[u] != rec[u][0]]
    if bad:
        raise EnvError(f"md5 differs between lock and record: {bad[:3]}")
    return len(lock)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_cache(root: Path, record_text: str) -> int:
    rec = sha_record(record_text)
    pkgs = root / "pkgs"
    problems = []
    for url, (_, sha) in sorted(rec.items()):
        # micromamba keeps an archive named in an explicit lock under its
        # URL path; a solved install keeps it flat. Either is accepted, and
        # whichever is present is the one hashed.
        name = url.rsplit("/", 1)[1]
        found = [f for f in (pkgs / "https" / url[len("https://"):],
                             pkgs / name) if f.is_file()]
        if not found:
            problems.append(f"missing {name}")
        elif _sha256(found[0]) != sha:
            problems.append(f"sha256 mismatch {name}")
    if problems:
        raise EnvError(f"{len(problems)} package(s) fail: {problems[:5]}")
    return len(rec)


def bootstrap_micromamba(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tarball = root / "micromamba.tar.bz2"
    if not tarball.exists():
        with urllib.request.urlopen(MICROMAMBA["url"], timeout=300) as r:
            tarball.write_bytes(r.read())
    got = _sha256(tarball)
    if got != MICROMAMBA["sha256"]:
        tarball.unlink()
        raise EnvError(f"micromamba sha256 {got} is not the pinned "
                       f"{MICROMAMBA['sha256']}; nothing was unpacked")
    dest = root / "micromamba-bin"
    with tarfile.open(tarball, "r:bz2") as tf:
        member = tf.getmember("bin/micromamba")
        if not member.isfile():
            raise EnvError("bin/micromamba in the tarball is not a file")
        tf.extract(member, dest, filter="data")
    exe = dest / "bin" / "micromamba"
    exe.chmod(0o755)
    return exe


def create(prefix: Path, root: Path) -> dict:
    n = check_lock(LOCK.read_text(encoding="utf-8"),
                   SHA_RECORD.read_text(encoding="utf-8"))
    exe = bootstrap_micromamba(root)
    env = dict(os.environ, MAMBA_ROOT_PREFIX=str(root))
    subprocess.run([str(exe), "create", "-y", "-p", str(prefix),
                    "--file", str(LOCK)], check=True, env=env)
    verified = verify_cache(root, SHA_RECORD.read_text(encoding="utf-8"))
    py = prefix / "bin" / "python"
    probe = subprocess.run(
        [str(py), "-I", "-c",
         "import dolfinx, petsc4py; from petsc4py import PETSc; "
         "print(dolfinx.__version__, PETSc.Sys.getVersion())"],
        check=True, capture_output=True, text=True).stdout.strip()
    return {"packages_locked": n, "packages_sha256_verified": verified,
            "python": str(py), "probe": probe,
            "lock_sha256": _sha256(LOCK),
            "micromamba_sha256": MICROMAMBA["sha256"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-lock")
    c = sub.add_parser("create")
    c.add_argument("--prefix", required=True, type=Path)
    c.add_argument("--root", required=True, type=Path)
    v = sub.add_parser("verify-cache")
    v.add_argument("--root", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check-lock":
            n = check_lock(LOCK.read_text(encoding="utf-8"),
                           SHA_RECORD.read_text(encoding="utf-8"))
            print(f"FENICSX LOCK: {n} packages, each from conda-forge, md5 "
                  "and sha256 recorded and in agreement")
        elif args.cmd == "create":
            print(json.dumps(create(args.prefix, args.root), sort_keys=True))
        else:
            n = verify_cache(args.root, SHA_RECORD.read_text(encoding="utf-8"))
            print(f"FENICSX CACHE: {n} packages match their sha256")
    except (EnvError, subprocess.CalledProcessError, OSError) as exc:
        print(f"FENICSX ENVIRONMENT REFUSED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
