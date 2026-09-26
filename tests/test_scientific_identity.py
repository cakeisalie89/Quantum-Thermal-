"""The scientific digests are the substrate's digests, restated not imported.

``scientific.identity`` restates two algorithms from ``qta_agent`` because
the scientific core must not depend on the authority layer: canonical JSON
(``qta_agent.canonical``) and the source-closure digest
(``qta_agent.projection``). A restatement can drift, so this holds the two
to the same bytes on the same inputs, and holds the implementation digest to
the behaviour it claims: a change anywhere in the closure changes it, a
change outside does not, and an unreadable module leaves no identity.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import canonical, projection  # noqa: E402
from scientific import identity  # noqa: E402


@pytest.mark.parametrize("obj", [
    {"b": 1, "a": [1.5, "x", None, True]}, [], {"é": "ü"}, 0.1, "s",
    {"nested": {"z": [1, 2, {"y": -0.0}]}},
])
def test_canonical_digests_agree(obj):
    assert identity.digest(obj) == canonical.digest(obj)
    assert identity.canonical_bytes(obj) == canonical.canonical_bytes(obj)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: object()}])
def test_both_refuse_what_is_not_json(bad):
    with pytest.raises(identity.IdentityError):
        identity.digest(bad)
    with pytest.raises(canonical.CanonicalizationError):
        canonical.digest(bad)


@pytest.mark.parametrize("modules", [
    ("qta_agent.store",), ("qta_agent.events", "qta_agent.context"),
])
def test_source_closure_digests_agree(modules):
    import importlib
    for m in modules:
        importlib.import_module(m)
    assert identity.source_closure(modules) == \
        projection.source_closure(modules)
    assert identity.source_closure_digest(modules) == \
        projection.source_closure_digest(modules)


@pytest.fixture()
def pkg(tmp_path, monkeypatch):
    """A throwaway package: a -> b (in package), c unrelated."""
    root = tmp_path / "idpkg_zz"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "a.py").write_text("from . import b\n")
    (root / "b.py").write_text("X = 1\n")
    (root / "c.py").write_text("Y = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield root
    for m in [m for m in sys.modules if m.startswith("idpkg_zz")]:
        del sys.modules[m]


def test_a_change_in_the_closure_changes_the_digest(pkg):
    before = identity.implementation_digest(("idpkg_zz.a",))
    (pkg / "b.py").write_text("X = 2\n")
    assert identity.implementation_digest(("idpkg_zz.a",)) != before


def test_a_change_outside_the_closure_does_not(pkg):
    before = identity.implementation_digest(("idpkg_zz.a",))
    (pkg / "c.py").write_text("Y = 2\n")
    assert identity.implementation_digest(("idpkg_zz.a",)) == before


def test_no_identity_without_readable_source(pkg):
    with pytest.raises(identity.IdentityError, match="cannot be imported"):
        identity.implementation_digest(("idpkg_zz.missing",))
    with pytest.raises(identity.IdentityError, match="identifies nothing"):
        identity.implementation_digest(())


def test_a_lazy_import_is_in_the_closure(pkg):
    (pkg / "a.py").write_text(textwrap.dedent("""
        def f():
            from . import c
            return c
    """))
    identity.implementation_digest(("idpkg_zz.a",))    # imports a only
    assert "idpkg_zz.c" not in sys.modules
    closure = identity.source_closure(("idpkg_zz.a",))
    assert "idpkg_zz.c" in closure


def test_an_importable_module_without_source_has_no_identity():
    """``math`` imports fine and has no Python source to read."""
    with pytest.raises(identity.IdentityError, match="cannot be read"):
        identity.implementation_digest(("math",))
