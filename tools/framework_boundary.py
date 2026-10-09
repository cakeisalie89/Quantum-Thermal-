#!/usr/bin/env python3
"""ACTIVE FRAMEWORK vs LEGACY QTA: the boundary, measured.

    Nothing in the active framework may import, open, or take authority
    from legacy QTA.

Which side a file is on is ``FILE_DISPOSITION.csv``'s to say; which
dispositions are ACTIVE, TRANSITIONAL and LEGACY is ``docs/framework_
boundary.json``'s. This tool reads both and asks three questions of every
ACTIVE production module (tests are out of scope: a test of legacy code
imports it on purpose):

* IMPORT. Its static import closure -- every ``import`` and ``from ...
  import`` it contains, at module level or inside a function, followed
  transitively, plus the package initialisers every import executes --
  contains no LEGACY module. Function-level imports count because "only
  when called" is still a dependency, and the one such edge that made every
  module of ``qta_multiphysics`` reach the legacy orchestrator was exactly
  that: a lazy ``run_all`` in the package ``__init__``.
* OPEN. No string literal in it names a LEGACY file (by path or by file
  name): an active module does not read the BOM, a hardware registry or the
  gate table, however it spells the call.
* ONTOLOGY. No import in it names a module whose path carries a legacy-
  ontology token (``machine_fsm``, ``hardware_governance``, ...), tracked or
  not -- so a legacy module moved or renamed out of the disposition's sight
  is still recognised.

TRANSITIONAL modules are measured and reported, not enforced: each is being
rewritten or mined for generic physics, and moves to ACTIVE when it no
longer reaches legacy. ``--report`` prints the counts the convergence plan's
checkpoint quotes.

Usage::

    python tools/framework_boundary.py --check     # exit 1 on a violation
    python tools/framework_boundary.py --report    # counts, as JSON
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOUNDARY = ROOT / "docs" / "framework_boundary.json"
DISPOSITION = ROOT / "FILE_DISPOSITION.csv"

#: A Mode-letter label (``mode_d_temp_threshold_K``, "Mode B"). Counted in
#: active sources for the report; not a violation -- see the boundary's
#: legacy_ontology_note.
_MODE_LABEL = re.compile(r"(?i)\bmode[_ ]?[abcd](?:_|\b)")


def load_boundary() -> dict:
    return json.loads(BOUNDARY.read_text(encoding="utf-8"))


def dispositions() -> dict:
    with DISPOSITION.open(encoding="utf-8", newline="") as fh:
        return {r["path"]: r["disposition"] for r in csv.DictReader(fh)}


def tracked_python() -> list:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split() if p]


def module_name(path: str) -> str:
    m = path[:-3].replace("/", ".")
    return m[:-len(".__init__")] if m.endswith(".__init__") else m


def _imported_names(tree: ast.AST, package: str) -> set:
    """Every dotted name an import statement anywhere in ``tree`` names."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                if node.level > 1:
                    parts = parts[:len(parts) - (node.level - 1)]
                base = ".".join(parts)
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            names.add(mod)
            names.update(f"{mod}.{a.name}" for a in node.names)
    return {n for n in names if n}


#: Parsed trees by (path, text): a source is parsed once per process however
#: many snapshots are taken of the tree around it.
_PARSED: dict = {}


def _parse(path: str, text: str) -> ast.AST:
    key = (path, text)
    if key not in _PARSED:
        _PARSED[key] = ast.parse(text, filename=path)
    return _PARSED[key]


def _string_literals(tree: ast.AST) -> set:
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


class Boundary:
    """The measurement over one snapshot of sources.

    ``sources`` maps a tracked path to its text; any path it names replaces
    the file on disk (and a new path adds a module), which is how the tests
    plant a violation without touching the tree.
    """

    def __init__(self, sources: dict | None = None):
        self.spec = load_boundary()
        self.disp = dispositions()
        paths = set(tracked_python()) | set(sources or ())
        self.text = {}
        for p in sorted(paths):
            if sources and p in sources:
                self.text[p] = sources[p]
            else:
                self.text[p] = (ROOT / p).read_text(encoding="utf-8")
        self.path_of = {module_name(p): p for p in self.text}
        self.trees = {p: _parse(p, t) for p, t in self.text.items()}
        classes = self.spec["dispositions"]
        self.side = {}
        for p in self.text:
            d = self.disp.get(p)
            self.side[p] = next((k for k, ds in classes.items() if d in ds),
                                "UNCLASSIFIED")
        out = tuple(self.spec.get("out_of_scope_prefixes", ()))
        self.in_scope = {p for p in self.text if not p.startswith(out)}
        self.legacy_modules = {module_name(p) for p in self.text
                               if self.side[p] == "LEGACY"}
        self.legacy_files = {p for p, d in self.disp.items()
                             if d in classes["LEGACY"]
                             and not p.endswith(".py")}
        # Root-level legacy files are recognised by name anywhere; nested
        # ones only with their directory (see the boundary's file_rule_note).
        self.legacy_basenames = {p for p in self.legacy_files
                                 if "/" not in p}
        self.exceptions: dict[tuple[str, str], list[dict]] = {}
        for e in self.spec.get("declared_exceptions", ()):
            self.exceptions.setdefault((e["module"], e["rule"]), []).append(e)
        self.tokens = tuple(self.spec["legacy_ontology_tokens"])
        self._edges = {m: self._direct(m) for m in self.path_of}

    # -- the graph --------------------------------------------------------

    def _parents(self, mod: str) -> set:
        parts = mod.split(".")
        return {".".join(parts[:i]) for i in range(1, len(parts))} \
            & set(self.path_of)

    def _resolve(self, name: str) -> str | None:
        while name and name not in self.path_of:
            name = name.rpartition(".")[0]
        return name or None

    def _direct(self, mod: str) -> set:
        path = self.path_of[mod]
        package = (mod if path.endswith("__init__.py")
                   else mod.rpartition(".")[0])
        out = set()
        for name in _imported_names(self.trees[path], package):
            target = self._resolve(name)
            if target:
                out.add(target)
                out |= self._parents(target)
        out |= self._parents(mod)
        out.discard(mod)
        return out

    def closure(self, mod: str) -> dict:
        """``{reached module: the module it was reached from}``."""
        via: dict[str, str | None] = {mod: None}
        stack = [mod]
        while stack:
            cur = stack.pop()
            for nxt in self._edges.get(cur, ()):
                if nxt not in via:
                    via[nxt] = cur
                    stack.append(nxt)
        return via

    @staticmethod
    def _chain(via: dict, end: str) -> list:
        chain = [end]
        while via[chain[-1]] is not None:
            chain.append(via[chain[-1]])
        return list(reversed(chain))

    # -- the questions ----------------------------------------------------

    def modules(self, side: str) -> list:
        return sorted(module_name(p) for p in self.in_scope
                      if self.side[p] == side)

    def reaches_legacy(self, mod: str) -> list:
        via = self.closure(mod)
        return [self._chain(via, m)
                for m in sorted(set(via) & self.legacy_modules)]

    def opens_legacy(self, mod: str, *, declared: bool = False) -> list:
        """String literals in ``mod`` naming a legacy file. Declared
        exceptions are left out unless ``declared``."""
        found = []
        for lit in _string_literals(self.trees[self.path_of[mod]]):
            name = lit.replace("\\", "/").strip("./")
            if any(name == f or name.endswith("/" + f)
                   for f in self.legacy_files) \
                    or Path(name).name in self.legacy_basenames:
                found.append(lit)
        if not declared:
            found = [lit for lit in found
                     if not self._declared(mod, "OPEN", lit)]
        return sorted(found)

    def _declared(self, mod: str, rule: str, name: str) -> bool:
        return any("names" not in e or name in e["names"]
                   for e in self.exceptions.get((mod, rule), ()))

    def stale_exceptions(self) -> list:
        """Declared exceptions that no longer excuse anything: an exception
        outliving its reason is how a boundary rots."""
        out = []
        for (mod, rule), entries in sorted(self.exceptions.items()):
            if mod not in self.path_of:
                out.append(f"{mod}: declared exception for a module that "
                           "does not exist")
                continue
            hits = self.opens_legacy(mod, declared=True)
            for e in entries:
                names = e.get("names")
                if (names and not set(names) <= set(hits)) or not hits:
                    out.append(f"{mod}: declared {rule} exception excuses "
                               "nothing any more; remove it")
        return out

    def ontology_imports(self, mod: str) -> list:
        path = self.path_of[mod]
        package = (mod if path.endswith("__init__.py")
                   else mod.rpartition(".")[0])
        return sorted(n for n in _imported_names(self.trees[path], package)
                      if any(t in n for t in self.tokens))

    def problems(self) -> list:
        out = []
        for mod in self.modules("ACTIVE"):
            for chain in self.reaches_legacy(mod):
                out.append(f"IMPORT: {mod} reaches legacy {chain[-1]} via "
                           f"{' > '.join(chain)}")
            for lit in self.opens_legacy(mod):
                out.append(f"OPEN: {mod} names legacy file {lit!r}")
            for name in self.ontology_imports(mod):
                out.append(f"ONTOLOGY: {mod} imports {name!r}, a "
                           "legacy-ontology module")
        out += [f"STALE: {s}" for s in self.stale_exceptions()]
        unclassified = sorted(p for p in self.in_scope
                              if self.side[p] == "UNCLASSIFIED")
        for p in unclassified:
            out.append(f"UNCLASSIFIED: {p} has no disposition the boundary "
                       "places on a side")
        return out

    def report(self) -> dict:
        active = self.modules("ACTIVE")
        trans = self.modules("TRANSITIONAL")
        return {
            "active_modules": len(active),
            "active_importing_legacy": sum(
                1 for m in active if self.reaches_legacy(m)),
            "active_opening_legacy_files": sum(
                1 for m in active if self.opens_legacy(m)),
            "active_importing_legacy_ontology": sum(
                1 for m in active if self.ontology_imports(m)),
            "active_mode_letter_labels": sum(
                len(_MODE_LABEL.findall(self.text[self.path_of[m]]))
                for m in active),
            "transitional_modules": len(trans),
            "transitional_reaching_legacy": sorted(
                m for m in trans if self.reaches_legacy(m)),
            "legacy_modules": len(self.legacy_modules),
        }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    b = Boundary()
    if args.report:
        print(json.dumps(b.report(), indent=2))
        return 0
    problems = b.problems()
    for p in problems:
        print(p)
    r = b.report()
    print(f"{r['active_modules']} active modules; "
          f"{r['active_importing_legacy']} import legacy, "
          f"{r['active_opening_legacy_files']} name a legacy file, "
          f"{r['active_importing_legacy_ontology']} import legacy ontology; "
          f"{len(r['transitional_reaching_legacy'])} of "
          f"{r['transitional_modules']} transitional modules still reach "
          "legacy (reported, not enforced)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
