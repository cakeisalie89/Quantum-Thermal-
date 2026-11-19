"""The evidence vocabulary is held to the code it describes.

``agent_substrate.evidence_terms`` names nine properties and says, for each
registered check, which one it establishes. These tests refuse the ways that
statement goes stale or starts to overclaim:

* a registered check that no longer exists;
* a ``#key`` that the function no longer answers under;
* an implication beyond the one the module states, which would let one check
  be read as establishing another;
* a check that lists as a mistaken reading the very property it establishes;
* an ASSESSED or OBSERVED check required by an authority decision;
* the rendered table in ``docs/EVIDENCE_TERMS.md`` drifting from the registry.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate.evidence_terms import (  # noqa: E402
    CHECKS, IMPLIES, Property, Strength, establishing, implied_by,
    render_table,
)

DOC = ROOT / "docs" / "EVIDENCE_TERMS.md"


def _resolve(target: str):
    module, _, rest = target.partition(":")
    attr, _, key = rest.partition("#")
    obj = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj, key


def test_every_registered_check_exists_and_answers_under_its_key():
    for c in CHECKS:
        obj, key = _resolve(c.target)
        assert callable(obj), c.target
        if key:
            src = inspect.getsource(inspect.getmodule(obj))
            assert f'"{key}"' in src, (
                f"{c.target}: the module no longer answers under {key!r}")


def test_the_only_implication_is_that_verified_bytes_were_cited():
    assert IMPLIES == {
        Property.CITED_BYTES_VERIFY: frozenset({Property.CITATION_EXISTS})}
    for prop in Property:
        others = implied_by(prop) - {prop}
        assert others <= {Property.CITATION_EXISTS}, (prop, others)


def test_nothing_implies_a_verdict_about_meaning_science_authority_or_world():
    for target in (Property.SEMANTICALLY_SUPPORTED,
                   Property.SCIENTIFICALLY_VERIFIED,
                   Property.DECISION_AUTHORIZED, Property.CURRENT_WORLD,
                   Property.CITATION_RELEVANT, Property.HISTORY_RECORDS):
        for prop in Property:
            if prop is not target:
                assert target not in implied_by(prop), (prop, target)


def test_no_check_disclaims_what_it_establishes():
    for c in CHECKS:
        assert c.not_, f"{c.target} names nothing it is mistaken for"
        assert not set(c.not_) & implied_by(c.establishes), c.target
        assert c.establishes not in c.requires, c.target


def test_an_authority_decision_relies_only_on_deterministic_checks():
    for c in CHECKS:
        if c.establishes is Property.DECISION_AUTHORIZED:
            for prop in c.requires:
                assert establishing(prop), (
                    f"{c.target} requires {prop.value}, which nothing "
                    "establishes")
                assert all(x.strength is Strength.DETERMINISTIC
                           for x in establishing(prop)), (c.target, prop)


def test_the_historical_names_are_pinned_to_what_they_establish():
    by_target = {c.target: c.establishes for c in CHECKS}
    assert by_target["agent_substrate.memory_trust:assess#evidence_supported"] \
        is Property.CITED_BYTES_VERIFY
    assert by_target["tools.grounding_bench.claims:verify"] \
        is Property.HISTORY_RECORDS


def test_the_current_world_is_established_by_no_check_here():
    """A clean history cannot establish an unobserved external fact. If a
    check is ever registered as establishing CURRENT_WORLD it must be an
    OBSERVED one, and this test is where that decision gets made."""
    for c in establishing(Property.CURRENT_WORLD):
        assert c.strength is Strength.OBSERVED, c.target


def test_the_document_carries_the_rendered_registry():
    text = DOC.read_text(encoding="utf-8")
    begin = "<!-- BEGIN GENERATED: evidence_terms.render_table -->\n"
    end = "<!-- END GENERATED -->"
    assert begin in text and end in text
    body = text.split(begin, 1)[1].split(end, 1)[0]
    assert body == render_table(), (
        "docs/EVIDENCE_TERMS.md is stale: regenerate it from "
        "evidence_terms.render_table()")
