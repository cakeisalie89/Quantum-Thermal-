#!/usr/bin/env python3
"""The current-active type-check scope: what mypy must pass, what is frozen
and why, and what is recorded as legacy typing debt instead.

Three sets partition every tracked Python file:

* ACTIVE -- the agent substrate, the generic scientific layer, the learned-
  model substrate, the Stage-10 stack adapters, the FMI/FEniCSx runtimes,
  every tool, and ``ro_crate_tools.py`` (the harness's RO-Crate).
  ``check`` runs mypy over all of it with the repository's own
  configuration -- no global
  ``--ignore-missing-imports``: a package without type information is
  listed by name in ``typecheck_active.ini``, each one -- and exits with mypy's
  status. CI runs it.
* TESTS -- typed by their assertions; outside mypy by policy.
* LEGACY -- the hardware-era QTA numerical tree and its release tooling.
  Their errors are typing DEBT, measured and recorded in
  ``docs/typing_scope.json`` by ``legacy --write``, never silenced and never
  counted as the active scope's.

Inside ACTIVE, three modules are FROZEN: they are the source closure whose
digest identifies ``surface.langmuir_capture``, and the NF-1T development
dataset pins that digest. Adding an annotation to one of them changes the
model's implementation identity and orphans the dataset's provenance, so
their typing-only diagnostics are disabled -- by error code, for those
modules only -- in ``typecheck_active.ini``, which is its own file
because ``pyproject.toml`` is in the witness's environment lock and may not
change. ``tests/test_typecheck_scope.py``
holds the override to exactly this list and the list to the real closure.

    python tools/typecheck_scope.py check
    python tools/typecheck_scope.py partition
    python tools/typecheck_scope.py legacy [--write]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "typing_scope.json"
CONFIG = ROOT / "typecheck_active.ini"

ACTIVE = ("qta_agent", "scientific", "scientific_ai",
          "qta_multiphysics/stack", "integrations", "tools",
          "ro_crate_tools.py")
TESTS = ("tests", "conftest.py")

#: module -> why it is not edited, not even for an annotation
FROZEN = {
    "scientific.backend_probe":
        "in the source closure of surface.langmuir_capture, whose digest "
        "the NF-1T development dataset pins",
    "scientific.identity":
        "in the source closure of surface.langmuir_capture, whose digest "
        "the NF-1T development dataset pins",
    "scientific.models.surface_adsorption":
        "the surface.langmuir_capture model itself; its closure digest is "
        "pinned by the NF-1T development dataset",
}

_SUMMARY = re.compile(r"^Found (\d+) errors? in (\d+) files?")


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], cwd=ROOT,
                         capture_output=True, check=True).stdout
    return sorted(p for p in out.decode().split("\0") if p)


def _in(path: str, scope: tuple) -> bool:
    return any(path == s or path.startswith(s + "/") for s in scope)


def partition(files: list[str]) -> dict[str, list[str]]:
    """Every file in exactly one of ACTIVE, TESTS, LEGACY."""
    out: dict[str, list[str]] = {"active": [], "tests": [], "legacy": []}
    for f in files:
        key = ("active" if _in(f, ACTIVE) else
               "tests" if _in(f, TESTS) else "legacy")
        out[key].append(f)
    return out


def config_sections(path: Path = CONFIG) -> dict[str, dict[str, str]]:
    """``{module pattern: options}`` of the configuration's per-module
    sections (``[mypy-a,b]`` gives a and b the same options)."""
    cp = configparser.ConfigParser()
    cp.read_string(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for sec in cp.sections():
        if sec.startswith("mypy-"):
            for mod in sec[len("mypy-"):].split(","):
                out[mod.strip()] = dict(cp[sec])
    return out


def config_frozen(path: Path = CONFIG) -> set[str]:
    """Modules the configuration disables error codes for, or ignores."""
    return {m for m, opts in config_sections(path).items()
            if "disable_error_code" in opts or "ignore_errors" in opts}


def _mypy(targets, *extra: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "mypy", "--config-file",
                        str(CONFIG), *extra, *targets],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def summary(output: str) -> tuple[int, int]:
    """(errors, files with errors) from mypy's last line; (0, 0) clean."""
    for line in reversed(output.splitlines()):
        m = _SUMMARY.match(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        if line.startswith("Success:"):
            return 0, 0
    raise RuntimeError("mypy printed no summary:\n" + output[-2000:])


def check() -> int:
    # the bodies of unannotated functions too: mypy skips them by default,
    # and a scope whose untyped half is never read is half a scope
    code, out = _mypy(ACTIVE, "--check-untyped-defs")
    errors, files = summary(out)
    print("\n".join(line for line in out.splitlines()
                    if ": error:" in line))
    print(f"active type-check scope ({', '.join(ACTIVE)}): "
          f"{errors} error(s) in {files} file(s); frozen and overridden: "
          f"{', '.join(sorted(FROZEN))}")
    return 1 if errors or code else 0


def legacy(write: bool) -> int:
    files = partition(_tracked())["legacy"]
    code, out = _mypy(files)
    errors, nfiles = summary(out)
    per: dict[str, int] = {}
    for line in out.splitlines():
        if ": error:" in line:
            f = line.split(":", 1)[0]
            per[f] = per.get(f, 0) + 1
    record = {
        "measured_files": len(files), "errors": errors,
        "files_with_errors": nfiles,
        "by_file": dict(sorted(per.items())),
        "command": "python tools/typecheck_scope.py legacy --write",
        "note": "typing debt of the hardware-era QTA tree and its release "
                "tooling: recorded, not enforced, never counted as the "
                "active scope's"}
    print(f"legacy typing debt: {errors} error(s) in {nfiles} of "
          f"{len(files)} file(s)")
    if write:
        doc = {"schema": "typing-scope/1", "active": list(ACTIVE),
               "tests": list(TESTS),
               "frozen": {m: FROZEN[m] for m in sorted(FROZEN)},
               "legacy": record}
        DOC.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {DOC.relative_to(ROOT)}")
    return 0 if code in (0, 1) else code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("partition")
    lg = sub.add_parser("legacy")
    lg.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return check()
    if args.cmd == "partition":
        for k, v in partition(_tracked()).items():
            print(f"{k}: {len(v)}")
        return 0
    return legacy(args.write)


if __name__ == "__main__":
    sys.exit(main())
