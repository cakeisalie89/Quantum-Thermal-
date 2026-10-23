#!/usr/bin/env python3
"""Isolated Python runtimes for third-party tools the harness drives, not
imports: every wheel installed only if its sha256 is one the lock lists.

Three runtimes, each kept out of the project environment because of what it
brings with it, and each reached only through a JSON-in / JSON-out runner or
a command line:

* ``fmi``      -- fmpy, the FMI runtime that loads, validates and steps an
                  FMU (``integrations/fmi/runtime.lock``);
* ``sigstore`` -- sigstore-python, which signs with a GitHub Actions OIDC
                  identity and verifies a signature against an exact
                  identity and issuer (``integrations/supply_chain/
                  sigstore.lock``);
* ``rocrate``  -- roc-validator, the RO-Crate community's validator, run
                  against the packaged crate beside this repository's own
                  (``integrations/ro_crate/validator.lock``).

    python tools/isolated_runtime.py create NAME --prefix P   # prints JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIMES = {
    "fmi": (ROOT / "integrations" / "fmi" / "runtime.lock", "fmpy"),
    "sigstore": (ROOT / "integrations" / "supply_chain" / "sigstore.lock",
                 "sigstore"),
    "rocrate": (ROOT / "integrations" / "ro_crate" / "validator.lock",
                "roc-validator"),
}


def create(name: str, prefix: Path) -> dict:
    lock, module = RUNTIMES[name]
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is not on PATH")
    subprocess.run([uv, "venv", "--quiet", "--python", "3.12", str(prefix)],
                   check=True)
    py = prefix / "bin" / "python"
    subprocess.run([uv, "pip", "install", "--quiet", "--python", str(py),
                    "--require-hashes", "--no-deps", "-r", str(lock)],
                   check=True)
    probe = subprocess.run(
        [str(py), "-I", "-c",
         f"import importlib.metadata as m; print(m.version({module!r}))"],
        check=True, capture_output=True, text=True).stdout.strip()
    return {"runtime": name, "python": str(py), module: probe,
            "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("name", choices=sorted(RUNTIMES))
    c.add_argument("--prefix", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        print(json.dumps(create(args.name, args.prefix), sort_keys=True))
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        # stderr, for the reason tools/fenicsx_env.py gives (D-2026-121)
        print(f"ISOLATED RUNTIME REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
