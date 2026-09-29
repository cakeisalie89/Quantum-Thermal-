"""Reducer identity: which code produced a projection, and would it still.

WHY A SNAPSHOT HAS TO SAY WHO MADE IT

A checkpointed load restores a snapshot taken at seq K and folds K+1..N on top
of it. The snapshot is what the reducer of THAT moment made of 0..K. Change the
reducer -- a branch added to ``_apply``, a field folded differently, a
validation tightened -- and the same log now projects to something else. The
load then produces a state no version of the code would compute from the log:
0..K folded by code that no longer exists, K+1..N by the code that does.

    Same event log + changed reducer semantics does NOT imply an old
    projection remains valid.

So a snapshot records the identity of the reducer that produced it, and a load
that finds a different one discards the snapshot and replays from genesis. A
checkpoint accelerates the truth; it is never allowed to substitute for it,
and a snapshot from other code is not an acceleration of anything.

WHAT THE DIGEST COVERS, AND WHAT IT STANDS IN FOR

A reducer's semantics are not computable; its source is. ``reducer_digest`` is
taken over the source bytes of every module that defines a class in the
projection's MRO, plus every module of the same top-level package those import,
transitively -- imports inside functions included, because the AST walk sees
them. That OVER-approximates: a comment edit invalidates every snapshot. The
error is in the only direction a proxy for semantics may err in: a spurious
replay costs time, a missed change costs correctness.

It UNDER-approximates in one stated way: code outside the package -- a
third-party library whose behaviour changes under the same import name -- is
not covered. ``reducer_version`` is the human declaration for that case and for
any change the digest cannot see; bump it on purpose.

A source that cannot be read (a zip import, bytecode only) yields no digest,
and an identity without a digest matches nothing. Fail closed: replay.
"""
from __future__ import annotations

import ast
import functools
import hashlib
import sys
from dataclasses import dataclass, fields
from pathlib import Path


class ProjectionIdentityError(ValueError):
    """A recorded reducer identity is structurally invalid."""


@dataclass(frozen=True)
class ReducerIdentity:
    """Who produced a projection, precisely enough to refuse a stranger's."""

    #: What kind of state this is, e.g. ``authority_store``.
    projection_kind: str
    #: The reducing entry point, ``module.Class.method``.
    reducer_id: str
    #: Human-declared semantics version. Bumped on purpose.
    reducer_version: int
    #: sha256 over the reducer's source closure; None when unreadable.
    reducer_digest: str | None
    #: Shape of the serialized snapshot.
    projection_schema_version: int

    def to_record(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_record(cls, rec) -> "ReducerIdentity":
        if not isinstance(rec, dict):
            raise ProjectionIdentityError(
                f"reducer identity is {type(rec).__name__}, not an object")
        names = {f.name for f in fields(cls)}
        if set(rec) != names:
            raise ProjectionIdentityError(
                f"reducer identity fields {sorted(rec)} != {sorted(names)}")
        for name in ("projection_kind", "reducer_id"):
            if not isinstance(rec[name], str) or not rec[name]:
                raise ProjectionIdentityError(
                    f"{name} must be a non-empty str")
        for name in ("reducer_version", "projection_schema_version"):
            v = rec[name]
            if not isinstance(v, int) or isinstance(v, bool):
                raise ProjectionIdentityError(f"{name} must be an int")
        dg = rec["reducer_digest"]
        if dg is not None and not (isinstance(dg, str) and len(dg) == 64
                                   and all(c in "0123456789abcdef"
                                           for c in dg)):
            raise ProjectionIdentityError(
                "reducer_digest must be a sha256 hex digest or null")
        return cls(**rec)

    def refusals(self, other: "ReducerIdentity") -> list:
        """Why a projection made by ``other`` is not one ``self`` would make.

        Empty means the two are the same reducer. A missing digest on EITHER
        side is a refusal, not a wildcard: "we cannot tell" is not "the same".
        """
        out = []
        if self.reducer_digest is None or other.reducer_digest is None:
            out.append("the reducer's source could not be digested, so "
                       "nothing establishes that the snapshot's reducer is "
                       "this one")
        for f in fields(self):
            a, b = getattr(self, f.name), getattr(other, f.name)
            if a != b and not (f.name == "reducer_digest"
                               and (a is None or b is None)):
                out.append(f"{f.name}: snapshot has {b!r}, this code has "
                           f"{a!r}")
        return out


def _module_file(name: str) -> Path | None:
    mod = sys.modules.get(name)
    origin = getattr(mod, "__file__", None) if mod is not None else None
    if not origin or not str(origin).endswith(".py"):
        return None
    return Path(origin)


def _package_dir(root: str) -> Path | None:
    """The directory of top-level package ``root``; None for a plain module."""
    f = _module_file(root)
    return f.parent if f is not None and f.name == "__init__.py" else None


def _package_module_path(root_dir: Path, name: str) -> Path | None:
    """The file ``name`` loads from inside its top-level package, if any."""
    base = root_dir.joinpath(*name.split(".")[1:])
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _locate(name: str) -> Path | None:
    """Where ``name``'s source lives, WITHOUT importing it.

    A reducer's module imports some of its dependencies lazily, inside
    functions, so they may not be loaded when the digest is taken. Importing
    them here would make computing an identity a side effect; reading them
    from the package directory does not.
    """
    loaded = _module_file(name)
    if loaded is not None:
        return loaded
    root_dir = _package_dir(name.split(".")[0])
    return None if root_dir is None else _package_module_path(root_dir, name)


def _imports(tree, name: str, is_pkg: bool) -> set:
    """Module names ``tree`` imports, relative imports resolved."""
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
            # `from pkg import name` may name a submodule or an attribute.
            # Including a name that is an attribute costs nothing: it resolves
            # to no file and is dropped below.
            out.update(f"{base}.{a.name}" for a in node.names)
    return out


def source_closure(modules) -> dict | None:
    """``{module: sha256(source)}`` over ``modules`` and in-package imports.

    None if any module that must be covered cannot be read: a partial answer
    would be a claim about code nobody looked at.
    """
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
            continue            # a single-file module: nothing in-package
        for imp in _imports(tree, name, path.name == "__init__.py"):
            if (imp.split(".")[0] == root
                    and _package_module_path(root_dir, imp) is not None):
                todo.append(imp)
    return entries


def source_closure_digest(modules) -> str | None:
    """One digest over :func:`source_closure`; None when it is None."""
    entries = source_closure(modules)
    if entries is None:
        return None
    h = hashlib.sha256()
    for name in sorted(entries):
        h.update(f"{name}\0{entries[name]}\n".encode("utf-8"))
    return h.hexdigest()


def mro_modules(cls) -> tuple:
    """Every module defining a class in ``cls``'s MRO, builtins excluded.

    A subclass that overrides the reducer is covered by its own module; one
    that inherits it is covered by the base's. Taking only ``cls.__module__``
    would identify an inheriting subclass by a file that contains none of
    the code that folds its events.
    """
    return tuple(sorted({k.__module__ for k in cls.__mro__
                         if k.__module__ != "builtins"}))


@functools.lru_cache(maxsize=None)
def _closure_digest_once(modules: tuple) -> str | None:
    """:func:`source_closure_digest`, once per process per module set.

    A snapshot records the identity on every call to ``snapshot()``, and
    re-reading and re-parsing a dozen source files each time would make a
    cheap call expensive. Source files do not change under a running
    interpreter in any way this package supports -- the code already loaded
    would not change with them -- so the first answer is the answer for the
    life of the process.
    """
    return source_closure_digest(modules)


def identity_of(cls, *, projection_kind: str, reducer: str,
                reducer_version: int,
                projection_schema_version: int) -> ReducerIdentity:
    """The identity of ``cls.<reducer>`` as this process would run it.

    Every class in the MRO contributes its module, so a subclass that
    overrides the reducer AND one that inherits it are both covered: the
    digest changes with whichever code actually folds the events.
    """
    mods = mro_modules(cls)
    return ReducerIdentity(
        projection_kind=projection_kind,
        reducer_id=f"{cls.__module__}.{cls.__qualname__}.{reducer}",
        reducer_version=reducer_version,
        reducer_digest=_closure_digest_once(mods),
        projection_schema_version=projection_schema_version)
