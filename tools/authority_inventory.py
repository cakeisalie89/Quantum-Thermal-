#!/usr/bin/env python3
"""Every durable write in agent_substrate, and whether it is decided.

WHY THIS EXISTS

T-1 and T-2 were one defect class found twice by reading: a writer checked
its rules against the projection it had loaded and then appended outside the
writer lock, so the loser of an ordinary race wrote a record every later
replay refused (D-2026-147, D-2026-156). Each time, "the other writers are
fine" was an unverifiable self-assertion, and each time it was wrong about
some of them. So the set of writers is an artefact, and the mechanical part
of it is measured:

  * an AST walk of ``agent_substrate/*.py`` finds every call that appends to
    an event log -- ``.append(..., action=...)``, ``append_decided(...)`` and
    a writer's own ``_append_decided(...)`` -- and attributes each to the
    function containing it and the action it writes;
  * every write found must be classified in
    ``docs/security/authority_inventory.json``, and every classified write
    must still exist, so the table cannot describe a past version;
  * a write classified DECISION or USE must be DECIDED -- written by
    ``append_decided`` with its decision taken under the writer lock --
    unless an OPEN finding in ``docs/security/findings.json`` names it.

WHAT IS REVIEWED RATHER THAN DERIVED

The class of each write:

  DECISION   changes what someone may do, or what state a replay holds
  USE        exercises or spends an authority
  MINT       creates an entity under an identifier the writer generated at
             random, so no other writer's record can invalidate it
  FACT       records an observation; permits nothing
  TELEMETRY  diagnostic only

Deriving that from source would be guessing at intent. The judgement is
committed with its reason; the facts around it are measured.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "agent_substrate"
INVENTORY = ROOT / "docs" / "security" / "authority_inventory.json"
FINDINGS = ROOT / "docs" / "security" / "findings.json"
SCHEMA = "harness-authority-inventory/1"

CLASSES = ("DECISION", "USE", "MINT", "FACT", "TELEMETRY")
MUST_DECIDE = frozenset({"DECISION", "USE"})
DECIDED, APPEND = "DECIDED", "APPEND"
#: The decided-append machinery itself: a writer's wrapper around
#: append_decided, which carries no action of its own.
WRAPPERS = frozenset({"_append_decided", "append_decided", "signed_append",
                      "_append_reducer_authorized"})
#: Helpers that decide under the lock on their caller's behalf, called with
#: the action as a keyword: the write is the CALLER's, and it is decided.
DECIDING_HELPERS = frozenset({"_append_reducer_authorized"})


class _Walker(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.stack: list = []
        self.found: list = []

    def _visit_scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = _visit_scope

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else None)
        if name == "append" and any(k.arg == "action"
                                    for k in node.keywords):
            act = next(k.value for k in node.keywords if k.arg == "action")
            self.found.append((self._owner(), ast.unparse(act), APPEND,
                               node.lineno))
        elif name == "append" and isinstance(f, ast.Attribute) and any(
                k.arg is None for k in node.keywords):
            # log.append(**record): the action is in the record, built here
            # or by a method of the same class.
            self.found.append((self._owner(), None, APPEND, node.lineno))
        elif name in DECIDING_HELPERS and any(k.arg == "action"
                                              for k in node.keywords):
            act = next(k.value for k in node.keywords if k.arg == "action")
            self.found.append((self._owner(), ast.unparse(act), DECIDED,
                               node.lineno))
        elif name in ("append_decided", "_append_decided"):
            self.found.append((self._owner(), None, DECIDED, node.lineno))
        self.generic_visit(node)

    def _owner(self) -> str:
        return ".".join(self.stack)


def _function_scopes(tree: ast.Module) -> dict:
    """qualified name -> node, for every function at any depth."""
    out: dict = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                q = f"{prefix}.{child.name}" if prefix else child.name
                if not isinstance(child, ast.ClassDef):
                    out[q] = child
                walk(child, q)
            else:
                walk(child, prefix)
    walk(tree, "")
    return out


def _dict_actions(fn: ast.AST, scopes: dict | None = None,
                  cls: str = "") -> list:
    """The ``"action": X`` values of dict literals anywhere in ``fn``.

    When ``fn`` spells none, the record is built by a helper: then the
    methods of ``cls`` that ``fn`` calls on ``self`` are read, one level
    down. Only then -- a writer that spells its own record and also calls
    another writer is not credited with that writer's action."""
    acts = _spelled(fn)
    if acts or scopes is None:
        return acts
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and \
                isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and \
                node.func.value.id == "self":
            helper = scopes.get(f"{cls}.{node.func.attr}")
            if helper is not None and helper is not fn:
                acts.extend(_spelled(helper))
    return sorted(set(acts))


def _spelled(fn: ast.AST) -> list:
    acts: list = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "action":
                    acts.append(ast.unparse(v))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "dict":
            acts.extend(ast.unparse(k.value) for k in node.keywords
                        if k.arg == "action")
    return sorted(set(acts))


def _writer(qualname: str) -> str:
    """The method a nested decide() belongs to: the outermost function."""
    parts = qualname.split(".")
    for i, part in enumerate(parts):
        if part and part[0].islower() or part.startswith("_"):
            return ".".join(parts[:i + 1])
    return qualname


def scan(package: Path = PACKAGE) -> list:
    """Every log write: ``{"module", "function", "action", "mode"}``."""
    rows: dict = {}
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = _function_scopes(tree)
        w = _Walker(path.name)
        w.visit(tree)
        for owner, action, mode, _line in w.found:
            writer = _writer(owner)
            if writer.split(".")[-1] in WRAPPERS:
                continue
            if action is None:
                fn = scopes.get(writer)
                cls = writer.rsplit(".", 1)[0] if "." in writer else ""
                acts = (_dict_actions(fn, scopes, cls) if fn is not None
                        else []) or ["?"]
            else:
                acts = [action]
            for act in acts:
                key = (path.name, writer, act)
                # A function that both decides and appends one action is
                # reported as appending it: the weaker write is the one a
                # reader has to worry about.
                rows[key] = APPEND if APPEND in (mode, rows.get(key)) \
                    else DECIDED
    return [{"module": m, "function": f, "action": a, "mode": mode}
            for (m, f, a), mode in sorted(rows.items())]


def _open_findings(findings_path: Path) -> dict:
    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    return {f["id"]: f["status"] for f in doc["findings"]}


def problems(doc: dict, found: list, findings: dict) -> list:
    out: list = []
    if doc.get("schema") != SCHEMA:
        out.append(f"schema is {doc.get('schema')!r}, not {SCHEMA!r}")
    entries = doc.get("writes") or []
    key = lambda r: (r["module"], r["function"], r["action"])  # noqa: E731
    have = {key(r): r for r in found}
    listed: dict = {}
    for e in entries:
        k = key(e)
        if k in listed:
            out.append(f"{k} is classified twice")
        listed[k] = e
        if e.get("class") not in CLASSES:
            out.append(f"{k}: class {e.get('class')!r} is not one of "
                       f"{CLASSES}")
        if not (e.get("why") or "").strip():
            out.append(f"{k}: no reason given for its class")
        if k not in have:
            out.append(f"{k} is classified but no such write exists; the "
                       "table describes a past version of the code")
            continue
        if e.get("mode") != have[k]["mode"]:
            out.append(f"{k}: recorded as {e.get('mode')!r}, measured as "
                       f"{have[k]['mode']!r}")
        if e.get("class") in MUST_DECIDE and have[k]["mode"] != DECIDED:
            fid = e.get("open_finding")
            if not fid:
                out.append(
                    f"{k} is a {e['class']} written outside append_decided "
                    "and no OPEN finding names it: it is decided against a "
                    "projection loaded before other writers' records")
            elif findings.get(fid) != "OPEN":
                out.append(
                    f"{k} cites finding {fid!r} for writing outside "
                    f"append_decided, and that finding is "
                    f"{findings.get(fid, 'absent')!r}, not OPEN")
    for k in have:
        if k not in listed:
            out.append(f"{k} writes to the log ({have[k]['mode']}) and is "
                       "not classified")
    return out


def summary(doc: dict) -> dict:
    counts: dict = {c: 0 for c in CLASSES}
    open_: list = []
    for e in doc.get("writes") or []:
        counts[e.get("class")] = counts.get(e.get("class"), 0) + 1
        if e.get("open_finding"):
            open_.append(f"{e['module']}::{e['function']} {e['action']}")
    return {"writes": len(doc.get("writes") or []), "by_class": counts,
            "undecided_under_open_findings": open_}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="the committed inventory agrees with the "
                                 "code and its rule holds")
    sub.add_parser("scan", help="print every write the walk finds")
    args = ap.parse_args(argv)
    found = scan()
    if args.cmd == "scan":
        print(json.dumps(found, indent=2))
        return 0
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    bad = problems(doc, found, _open_findings(FINDINGS))
    for p in bad:
        print(f"FAIL {p}")
    s = summary(doc)
    print(f"{'FAIL' if bad else 'OK'}: {s['writes']} durable writes "
          f"classified {s['by_class']}; "
          f"{len(s['undecided_under_open_findings'])} undecided under an "
          "OPEN finding")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
