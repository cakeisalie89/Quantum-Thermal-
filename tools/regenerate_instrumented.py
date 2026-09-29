#!/usr/bin/env python3
"""Run a canonical generator IN THIS PROCESS; record what it read and ran on.

A byte-reproduction claim is about ONE computation: these bytes, from this
code and these inputs, on this numerical backend. Probing a parent process
and regenerating in a child that may not share its environment would pair
one computation's bytes with another's identity. So the generator runs here,
under ``runpy``, exactly as ``python <entry>`` would run it -- its own
directory first on ``sys.path``, its own argv -- and the record is taken in
the same process afterwards:

  * ``closure``: every file under the root the generator IMPORTED or OPENED
    FOR READING, by the sha256 of its bytes -- measured with an audit hook,
    not listed by hand. Its outputs, byte-code caches and this file are not
    inputs. A generator that reads one more file next year has a different
    closure without anybody remembering to say so.
  * ``outputs``: every file it left in ``outputs/``, by sha256.
  * ``environment``: ``scientific.backend_probe.run_environment`` over every
    installed distribution with compiled code that the generator loaded --
    probed only AFTER the generator finished, so nothing the probe loads is
    in the closure and nothing it does precedes the computation.

Nothing from the repository is imported before the generator runs.

Usage:
    python tools/regenerate_instrumented.py --entry qta_full_sim.py \\
        --record /tmp/record.json [-- generator args ...]

Exit status is the generator's; the record is written only for a generator
that exited 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import sys
import time

RECORD_SCHEMA = "regeneration-record/1"
_OPENED: set = set()


def _sha256(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _audit(event, args):
    if event != "open" or not args:
        return
    path, mode = args[0], (args[1] if len(args) > 1 else "r")
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if isinstance(path, str):
        _OPENED.add(os.path.abspath(path))


def _inputs(root: str, outputs: str, me: str) -> dict:
    """The generator's input closure under ``root``, relative path -> sha."""
    paths = set(_OPENED)
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if isinstance(f, str):
            paths.add(os.path.abspath(f))
    # The interpreter's own tree is the ENVIRONMENT, identified by its
    # distributions and native bytes -- not an input, even when a virtual
    # environment happens to live inside the repository.
    prefixes = tuple(os.path.join(os.path.abspath(x), "") for x in {
        sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix})
    out = {}
    for p in sorted(paths):
        rel = os.path.relpath(p, root)
        if rel.startswith("..") or os.path.isabs(rel):
            continue
        parts = rel.split(os.sep)
        if p.startswith(prefixes) or "site-packages" in parts \
                or "dist-packages" in parts:
            continue
        if p == me or (p + os.sep).startswith(outputs + os.sep) \
                or "__pycache__" in rel.split(os.sep) or p.endswith(".pyc"):
            continue
        if not os.path.isfile(p):
            continue
        sha = _sha256(p)
        if sha is not None:
            out[rel.replace(os.sep, "/")] = sha
    return out


def _native_distributions() -> list:
    """Installed distributions with compiled code whose modules the
    generator loaded -- the ones whose native bytes decide its arithmetic."""
    import importlib.metadata as md
    top = {name.split(".", 1)[0] for name in list(sys.modules)}
    mapping = md.packages_distributions()
    dists = sorted({d for t in top for d in mapping.get(t, [])})
    native = []
    for d in dists:
        try:
            files = md.files(d) or []
        except md.PackageNotFoundError:
            continue
        if any(str(f).endswith((".so", ".pyd", ".dylib")) or ".so." in str(f)
               for f in files):
            native.append(d)
    return native


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--entry", required=True)
    ap.add_argument("--record", required=True)
    args = ap.parse_args(argv)

    entry = os.path.abspath(args.entry)
    root = os.path.dirname(entry)
    outputs = os.path.join(root, "outputs")
    me = os.path.abspath(__file__)
    record_path = os.path.abspath(args.record)
    if (record_path + os.sep).startswith(outputs + os.sep):
        print("REFUSED: the record may not be written into the output set "
              "it describes", file=sys.stderr)
        return 2

    sys.argv = [entry, *extra]
    sys.path[0] = root
    sys.addaudithook(_audit)
    t0 = time.monotonic()
    code = 0
    try:
        runpy.run_path(entry, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (
            0 if exc.code is None else 1)
    elapsed = time.monotonic() - t0
    sys.stdout.flush()
    if code != 0:
        return code

    closure = _inputs(root, outputs, me)
    produced = {}
    if os.path.isdir(outputs):
        for name in sorted(os.listdir(outputs)):
            p = os.path.join(outputs, name)
            if os.path.isfile(p):
                produced[name] = _sha256(p)
    # Only now: the probe imports NumPy and loads libraries, and none of
    # that may precede the computation or enter its closure. The probe is
    # this repository's own, whatever directory the generator lives in.
    sys.path.insert(1, os.path.dirname(os.path.dirname(me)))
    distributions = _native_distributions()
    from scientific.backend_probe import run_environment
    environment = run_environment(distributions=tuple(distributions))
    record = {"schema": RECORD_SCHEMA,
              "entry": os.path.relpath(entry, root).replace(os.sep, "/"),
              "argv": extra, "exit": code, "elapsed_s": round(elapsed, 1),
              "closure": closure, "outputs": produced,
              "native_distributions": distributions,
              "environment": environment}
    tmp = record_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, sort_keys=True, indent=1)
    os.replace(tmp, record_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
