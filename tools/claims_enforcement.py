#!/usr/bin/env python3
"""The framework's claims boundary: every boundary held by code that exists.

``CLAIMS_BOUNDARY.md`` states what results produced here may never be
presented as -- a simulation result is not a measurement, independent
numerical agreement is not experimental validation, and nine more (directive
14). A boundary nobody enforces reads exactly like one somebody does, so each
is registered in ``docs/claims_boundary.json`` with the code that enforces it
(a module and a symbol defined in it) and the tests that hold it. This checks,
from the source rather than from anyone's say-so:

* every boundary the directive requires is registered, as a negation in the
  right order -- dropping one is a finding, not a quiet shrink, and so is
  inverting one ("a simulation result is a measurement" names both terms);
* every named module exists and DEFINES the named symbol (a class, function
  or assignment at module level, read from the parse tree);
* every named test exists as a function in the named file;
* ``CLAIMS_BOUNDARY.md`` states every registered boundary, verbatim.

What it cannot establish: that the named test tests the boundary. That is
what the mutation spec is for (``tools/mutations/claims_boundary.json``), and
the claim here is exactly the one above.

The hardware-era QTA claims -- gate counts, Mode B / Mode D exclusivity, BOM
vocabulary -- are the legacy verifier's, reconciled by
``tools/legacy_qta_claims.py`` against ``docs/legacy/qta/CLAIMS_BOUNDARY.md``.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "claims_boundary.json"
DOCUMENT = ROOT / "CLAIMS_BOUNDARY.md"

#: The boundaries directive 14 requires, as (what a result is, what it is
#: not). Held here rather than in the registry, so the registry cannot
#: satisfy the check by losing one.
REQUIRED = (
    ("simulation result", "measurement"),
    ("synthetic observation", "raw observation"),
    ("numerical convergence", "physical validation"),
    ("independent numerical agreement", "experimental validation"),
    ("signed", "scientifically correct"),
    ("ai proposal", "verified result"),
    ("tool completion", "accepted scientific result"),
    ("retrieved rag text", "evidence authority"),
    ("surrogate prediction", "ground truth"),
    ("optimization result", "physical optimum"),
    ("model calibration", "model validation"),
    # NF-1T: what a learned model is not, and what a counted, abstractly
    # validated architecture is not.
    ("learned prediction", "authority"),
    ("meta-validated architecture", "allocated, trained or validated model"),
    ("development model's training", "flagship's"),
    # NF-1T closure: the legacy gate table is not the current status;
    # simulated is not hardware; decision stability is not byte identity.
    ("legacy qta gate-table pass count", "status of the current harness"),
    ("simulated distributed execution", "distributed hardware validation"),
    ("decision stability under numeric drift", "byte identity"),
)


def _states(statement: str, is_: str, is_not: str) -> bool:
    """Whether ``statement`` says a ``is_`` is not a ``is_not``, in that
    order. Naming both is not enough."""
    head, sep, tail = " ".join(statement.lower().split()).partition(
        " is not ")
    return bool(sep) and is_ in head and is_not in tail


def _defined(module: Path, symbol: str) -> bool:
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name == symbol:
            return True
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == symbol
                for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name) and node.target.id == symbol:
            return True
    return False


def _test_exists(ref: str) -> bool:
    path, _, name = ref.partition("::")
    f = ROOT / path
    if not name or not f.is_file():
        return False
    name = name.split("[", 1)[0]
    tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
    return any(isinstance(n, ast.FunctionDef) and n.name == name
               for n in tree.body)


def problems(registry: dict | None = None,
             document: str | None = None) -> list:
    reg = registry if registry is not None else json.loads(
        REGISTRY.read_text(encoding="utf-8"))
    doc = document if document is not None else DOCUMENT.read_text(
        encoding="utf-8")
    flat_doc = " ".join(doc.split())
    out = []
    boundaries = reg.get("boundaries") or []
    if not boundaries:
        return ["the registry holds no boundaries; nothing is enforced"]
    for is_, is_not in REQUIRED:
        if not any(_states(e.get("statement", ""), is_, is_not)
                   for e in boundaries):
            out.append(f"REQUIRED: no boundary says a {is_} is not a "
                       f"{is_not}")
    seen = set()
    for e in boundaries:
        bid = e.get("id", "?")
        if bid in seen:
            out.append(f"{bid}: registered twice")
        seen.add(bid)
        statement = e.get("statement", "")
        if not statement or " ".join(statement.split()) not in flat_doc:
            out.append(f"{bid}: CLAIMS_BOUNDARY.md does not state "
                       f"{statement!r}")
        code = e.get("enforced_in") or []
        if not code:
            out.append(f"{bid}: names no code that enforces it")
        for c in code:
            mod = ROOT / c.get("module", "")
            if not mod.is_file():
                out.append(f"{bid}: {c.get('module')} does not exist")
            elif not _defined(mod, c.get("symbol", "")):
                out.append(f"{bid}: {c.get('module')} defines no "
                           f"{c.get('symbol')!r}")
        tests = e.get("tests") or []
        if not tests:
            out.append(f"{bid}: names no test that holds it")
        for t in tests:
            if not _test_exists(t):
                out.append(f"{bid}: test {t} does not exist")
    return out


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(
        argv)
    found = problems()
    for p in found:
        print(p)
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    n = len(reg.get("boundaries") or [])
    if found:
        print(f"claims boundary NOT held: {len(found)} problem(s)")
        return 1
    print(f"claims boundary held: {n} boundaries, each stated, each naming "
          "code that defines it and tests that exist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
