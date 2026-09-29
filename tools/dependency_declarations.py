"""Every package the tree imports is declared, in a group that ships it.

D-2026-74: two trust tests imported ``yaml`` and nothing declared PyYAML.
It arrived through ``snakemake`` (the ``workflow`` group), so an environment
synced without that group failed collection while CI -- which syncs every
group -- stayed green. The class is a direct import satisfied only
transitively, and a green CI cannot see it because CI installs more than the
minimum.

THE RULES, stated so the check cannot mean more than it does

* MINIMAL is ``[project.dependencies]`` plus the default dependency groups
  (uv's default is ``dev``; ``[tool.uv] default-groups`` overrides it): what
  a bare ``uv sync --frozen`` installs.
* An import executed at module import time -- not inside a ``def`` and not
  under a ``try`` that catches ImportError / Exception -- must resolve to a
  distribution in MINIMAL.
* Any other import (inside a function, or guarded) is optional by
  construction; its distribution must still be declared somewhere (a group
  or an extra), or be listed in ENVIRONMENTAL with the reason the
  environment, not this project, supplies it.
* A name that is a tracked ``.py`` stem or directory is local, not a
  package.

It does not prove a function-level import is reachable only where its group
is installed; it proves nothing about imports by string (``importlib``).

It also regenerates ``stage7_reports/dependency_inventory.json`` from
``pyproject.toml`` and ``uv.lock`` (``--write``), and ``--check`` refuses a
committed inventory that no longer matches them.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = "stage7_reports/dependency_inventory.json"

#: Import name -> distribution, for optional packages that are declared but
#: not installed by ``--all-groups`` (extras), where metadata cannot say.
STATIC = {"SALib": "salib", "openmdao": "openmdao", "pxr": "usd-core"}

#: Import name -> why the environment, not pyproject, supplies it.
ENVIRONMENTAL = {
    "dolfinx": "FEniCSx is not a plain wheel; it comes from conda-forge, "
               "spack or the dolfinx container, and fem_fenicsx.py stays "
               "STAGED when it is absent (pyproject says so)",
    "sigstore": "installed by release.yml's verification step only; "
                "verify_release.py fails closed with a named error when "
                "it is absent",
}

_GUARDS = {"ImportError", "ModuleNotFoundError", "Exception",
           "BaseException"}


def norm(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_name(req: str) -> str:
    return norm(re.split(r"[\s<>=!~;\[]", req.strip(), maxsplit=1)[0])


def declared(pyproject: dict) -> dict:
    """``{class: [normalized names]}``: runtime, each group, each extra."""
    out = {"runtime": [_req_name(r) for r in
                       pyproject["project"].get("dependencies", [])]}
    for g, reqs in pyproject.get("dependency-groups", {}).items():
        out[g] = [_req_name(r) for r in reqs if isinstance(r, str)]
    for e, reqs in pyproject["project"].get(
            "optional-dependencies", {}).items():
        out["extra:" + e] = [_req_name(r) for r in reqs]
    return out


def minimal(pyproject: dict) -> set:
    groups = (pyproject.get("tool", {}).get("uv", {})
              .get("default-groups", ["dev"]))
    d = declared(pyproject)
    return set(d["runtime"]).union(*(d.get(g, []) for g in groups))


def tracked_py(root: Path = ROOT) -> list:
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z",
                        "--cached", "--others", "--exclude-standard"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git could not enumerate {root}: {r.stderr[:200]}")
    return sorted(p for p in r.stdout.split("\0")
                  if p and not p.startswith("attic/"))


def local_names(paths: list) -> set:
    names = set()
    for p in paths:
        parts = p.split("/")
        names.update(parts[:-1])
        if p.endswith(".py"):
            names.add(parts[-1][:-3])
    return names


def _guarded(handlers) -> bool:
    for h in handlers:
        t = h.type
        if t is None:
            return True
        elts = t.elts if isinstance(t, ast.Tuple) else [t]
        if any(isinstance(e, ast.Name) and e.id in _GUARDS for e in elts):
            return True
    return False


def scan_source(src: str, path: str) -> list:
    """``(path, line, top-level name, eager)`` for each absolute import."""
    found = []

    def walk(node, eager):
        for child in ast.iter_child_nodes(node):
            e = eager
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                e = False
            elif isinstance(node, ast.Try) and child in node.body \
                    and _guarded(node.handlers):
                e = False
            if isinstance(child, ast.Import):
                for a in child.names:
                    found.append((path, child.lineno,
                                  a.name.split(".")[0], e))
            elif (isinstance(child, ast.ImportFrom) and child.level == 0
                  and child.module):
                found.append((path, child.lineno,
                              child.module.split(".")[0], e))
            walk(child, e)

    walk(ast.parse(src), True)
    return found


def distribution_of(name: str, dists: dict) -> str | None:
    if name in dists:
        return norm(dists[name][0])
    return STATIC.get(name)


def check(root: Path = ROOT, pyproject: dict | None = None,
          files: list | None = None) -> tuple:
    """``(problems, imports_seen)`` for the tree against its declarations."""
    if pyproject is None:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    paths = tracked_py(root) if files is None else files
    local = local_names(paths)
    std = set(sys.stdlib_module_names) | {"__future__"}
    dists = importlib.metadata.packages_distributions()
    mini = minimal(pyproject)
    anywhere = set().union(*declared(pyproject).values())
    problems, seen = [], []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        try:
            src = (root / rel).read_text(encoding="utf-8")
            hits = scan_source(src, rel)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for path, line, name, eager in hits:
            if name in std or name in local:
                continue
            seen.append((path, name, eager))
            if name in ENVIRONMENTAL:
                continue
            dist = distribution_of(name, dists)
            where = f"{path}:{line} imports {name}"
            if dist is None:
                problems.append(f"{where}: no known distribution provides it")
            elif eager and dist not in mini:
                problems.append(
                    f"{where} at import time, but {dist} is not in the "
                    f"minimal environment (runtime + default groups)")
            elif dist not in anywhere:
                problems.append(f"{where}: {dist} is declared nowhere")
    return problems, seen


# --- the inventory ----------------------------------------------------------

def _comments(text: str) -> dict:
    """Normalized requirement name -> its inline pyproject comment."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'\s*"([^"]+)",?\s*#\s*(.+)$', line)
        if m:
            out.setdefault(_req_name(m.group(1)), m.group(2).strip())
    return out


def _use(paths: set) -> str:
    if not paths:
        return "not imported by Python (a tool or CLI)"
    p = sorted(paths)
    return ", ".join(p[:3]) + (f" (+{len(p) - 3} more)" if len(p) > 3 else "")


def build_inventory(root: Path = ROOT, previous: dict | None = None) -> dict:
    text = (root / "pyproject.toml").read_text()
    pyproject = tomllib.loads(text)
    lock_bytes = (root / "uv.lock").read_bytes()
    lock = tomllib.loads(lock_bytes.decode())
    versions = {norm(p["name"]): p.get("version")
                for p in lock.get("package", [])}
    notes = _comments(text)
    old = {norm(e["package"]): e
           for e in (previous or {}).get("direct_dependencies", [])}
    dists = importlib.metadata.packages_distributions()
    users: dict = {}
    for path, name, _ in check(root, pyproject)[1]:
        d = distribution_of(name, dists)
        if d:
            users.setdefault(d, set()).add(path)
    entries = []
    for cls, names in declared(pyproject).items():
        for n in names:
            prev = old.get(n, {})
            entries.append({
                "package": n,
                "version": versions.get(n),
                "dependency_class": cls,
                "reason": prev.get("reason", notes.get(n, "")),
                "use": prev.get("use", _use(users.get(n, set()))),
                # null = not assessed; only a recorded judgement is carried
                "behavior_sensitive": prev.get("behavior_sensitive"),
                "version_authority": prev.get("version_authority",
                                              "uv.lock"),
            })
    return {
        "generated_by": "tools/dependency_declarations.py --write",
        "sources": {
            "pyproject.toml": hashlib.sha256(text.encode()).hexdigest(),
            "uv.lock": hashlib.sha256(lock_bytes).hexdigest()},
        "direct_dependencies": entries,
        "transitive": f"see uv.lock ({len(lock.get('package', []))} "
                      "packages; lockfile is the resolution authority)",
    }


def _render(inv: dict) -> str:
    return json.dumps(inv, indent=1, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="regenerate the dependency inventory")
    args = ap.parse_args(argv)
    path = ROOT / INVENTORY
    current = json.loads(path.read_text()) if path.exists() else None
    fresh = _render(build_inventory(ROOT, current))
    if args.write:
        path.write_text(fresh, encoding="utf-8")
        print(f"wrote {INVENTORY}")
    rc = 0
    if not args.write and path.read_text() != fresh:
        print(f"{INVENTORY} does not match pyproject.toml + uv.lock; "
              "regenerate with tools/dependency_declarations.py --write")
        rc = 1
    problems, seen = check()
    if len(seen) < 50:
        print(f"only {len(seen)} third-party imports seen; the scan "
              "examined too little to say anything")
        return 1
    for p in problems:
        print(p)
    if problems:
        return 1
    print(f"{len(seen)} third-party imports, each declared where it is "
          f"needed; {len(ENVIRONMENTAL)} supplied by the environment, each "
          "with a stated reason")
    return rc


if __name__ == "__main__":
    sys.exit(main())
