"""Canonical digests, and the digest of the code that computed something.

CANONICAL FORM. The same rules as ``qta_agent.canonical``, restated rather
than imported because this package must not depend on the agent substrate:
sorted keys, ASCII, no whitespace, and no NaN or infinity -- a value that is
not JSON is refused, not written in a form another reader cannot parse.
``tests/test_scientific_identity.py`` holds the two implementations to the
same bytes, so the restatement cannot drift silently.

IMPLEMENTATION DIGEST. A model's semantics are not computable; its source is.
``implementation_digest(modules)`` is taken over the source bytes of each
named module and of every module of the same top-level package it imports,
transitively, imports inside functions included -- the algorithm
``qta_agent.projection`` uses for reducer identity, restated here for the
same reason as above and held to it by the same test.

Its limits, stated because a digest is a proxy:

* it OVER-approximates: a comment edit changes it. The only cost is a run
  that is not reused, which is the safe direction;
* it UNDER-approximates code outside the named packages -- numpy, scipy, the
  interpreter. Those are covered by the environment digest in
  ``run_identity``, not here, and a model must name every package of its
  own it runs (a model adapter in ``scientific`` wrapping a solver in
  ``qta_multiphysics`` names a module in each);
* a module whose source cannot be read (a zip import, bytecode only) yields
  no digest, and then there is no identity: ``implementation_digest``
  raises rather than returning something that identifies nothing.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import re
import sys
from pathlib import Path

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class IdentityError(ValueError):
    """A value or a piece of code that cannot be identified."""


def canonical_bytes(obj) -> bytes:
    try:
        text = json.dumps(obj, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False)
    except ValueError as exc:
        raise IdentityError(f"not canonically serializable: {exc}") from exc
    except TypeError as exc:
        raise IdentityError(f"a non-JSON type: {exc}") from exc
    return text.encode("utf-8")


def digest(obj) -> str:
    """SHA-256 of the canonical form."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def is_digest(value) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def require_digest(name: str, value) -> str:
    if not is_digest(value):
        raise IdentityError(f"{name} is not a sha256 digest: {value!r}")
    return value


# --- source closure ----------------------------------------------------------

def _module_file(name: str) -> Path | None:
    mod = sys.modules.get(name)
    origin = getattr(mod, "__file__", None) if mod is not None else None
    if not origin or not str(origin).endswith(".py"):
        return None
    return Path(origin)


def _package_dir(root: str) -> Path | None:
    f = _module_file(root)
    return f.parent if f is not None and f.name == "__init__.py" else None


def _package_module_path(root_dir: Path, name: str) -> Path | None:
    base = root_dir.joinpath(*name.split(".")[1:])
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _locate(name: str) -> Path | None:
    loaded = _module_file(name)
    if loaded is not None:
        return loaded
    root_dir = _package_dir(name.split(".")[0])
    return None if root_dir is None else _package_module_path(root_dir, name)


def _imports(tree, name: str, is_pkg: bool) -> set:
    out = set()
    pkg = name if is_pkg else name.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                base = f"{base}.{node.module}" if node.module else base
            else:
                base = node.module or ""
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names)
    return out


def source_closure(modules) -> dict | None:
    """``{module: sha256(source)}`` over ``modules`` and their in-package
    imports; None if any module that must be covered cannot be read."""
    entries: dict = {}
    todo = list(modules)
    while todo:
        name = todo.pop()
        if name in entries:
            continue
        path = _locate(name)
        if path is None:
            return None
        try:
            src = path.read_bytes()
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            return None
        entries[name] = hashlib.sha256(src).hexdigest()
        root = name.split(".")[0]
        root_dir = _package_dir(root)
        if root_dir is None:
            continue
        for imp in _imports(tree, name, path.name == "__init__.py"):
            if (imp.split(".")[0] == root
                    and _package_module_path(root_dir, imp) is not None):
                todo.append(imp)
    return entries


def source_closure_digest(modules) -> str | None:
    entries = source_closure(modules)
    if entries is None:
        return None
    h = hashlib.sha256()
    for name in sorted(entries):
        h.update(f"{name}\0{entries[name]}\n".encode("utf-8"))
    return h.hexdigest()


def implementation_digest(modules) -> str:
    """The digest of the code in ``modules``; raises when there is none.

    The NAMED modules are imported (they are the code about to run, so this
    has no effect a run would not have); what they import is read from
    source, not imported. A module that cannot be imported, or whose source
    cannot be read, leaves no identity. An empty list identifies nothing and
    is refused too.
    """
    modules = tuple(modules)
    if not modules:
        raise IdentityError("no implementation modules named; an identity "
                            "over no code identifies nothing")
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise IdentityError(f"{name} cannot be imported: {exc}") from exc
    d = source_closure_digest(modules)
    if d is None:
        raise IdentityError(
            f"the source of {modules} (or of a module they import) cannot be "
            "read; there is no implementation identity to record")
    return d
