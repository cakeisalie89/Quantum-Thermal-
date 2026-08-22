"""Governed-record parser contract tests (gate references and modes).

MODEL-ONLY / FORECAST-ONLY. Software verification only.

Both parsers previously lost real data silently: ``parse_gate_refs`` matched
only ``[A-Z]\\d+`` and so dropped 30 of the 83 canonical gate IDs, and
``parse_modes`` matched only word-bounded single letters and so mapped every
compact form (``AB``, ``BCD``, ``ABCD``) to "no modes at all". Neither failure
raised anything. These tests pin the parsers to the *actual* governed
vocabulary rather than to a guessed grammar.
"""
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest                                                    # noqa: E402
from hypothesis import HealthCheck, given, settings              # noqa: E402
from hypothesis import strategies as st                          # noqa: E402

from qta_multiphysics.design.registry import (                   # noqa: E402
    MODES, ModeSyntaxError, canonical_gate_ids, parse_gate_refs, parse_modes)

DET = settings(max_examples=50, deadline=None, derandomize=True,
               suppress_health_check=[HealthCheck.too_slow])
ROOT = pathlib.Path(__file__).resolve().parents[1]


# ------------------------------ gate references ----------------------------

def test_every_canonical_gate_id_round_trips():
    """The regression that mattered: 30/83 IDs used to vanish."""
    ids = [r["gate_id"] for r in
           csv.DictReader(open(ROOT / "results_gate_table.csv"))]
    assert len(ids) == 83
    lost = [g for g in ids if parse_gate_refs(g) != [g]]
    assert lost == [], f"gate IDs not recovered by the parser: {lost}"


def test_suffixed_and_compound_ids_are_recovered():
    assert parse_gate_refs("blocked by D10a and D10b") == ["D10a", "D10b"]
    assert parse_gate_refs("D12_G23") == ["D12_G23"]
    assert parse_gate_refs("Shield-RAD") == ["Shield-RAD"]
    assert parse_gate_refs("THERMAL_1D_STABILITY_CHECK") == [
        "THERMAL_1D_STABILITY_CHECK"]


def test_a_reference_to_an_absent_gate_still_surfaces():
    """The governed records say D12; the gate table says D12_G23.

    That mismatch is a real finding the design validator reports. Matching
    canonical IDs must not silently resolve it away.
    """
    assert parse_gate_refs("D12") == ["D12"]
    assert "D12" not in canonical_gate_ids()
    assert "D12_G23" in canonical_gate_ids()


def test_prose_does_not_produce_gate_references():
    for text in ("see the thermal recovery note", "", "N/A", "-", "none"):
        assert parse_gate_refs(text) == []


@given(text=st.text(alphabet=st.characters(min_codepoint=32,
                                           max_codepoint=126), max_size=120))
@DET
def test_gate_parser_is_total_and_returns_sorted_unique(text):
    out = parse_gate_refs(text)
    assert out == sorted(set(out))


# ---------------------------------- modes ----------------------------------

def test_compact_runs_parse():
    """These are the forms that used to yield an empty set."""
    assert parse_modes("AB") == ["A", "B"]
    assert parse_modes("BC") == ["B", "C"]
    assert parse_modes("CD") == ["C", "D"]
    assert parse_modes("ACD") == ["A", "C", "D"]
    assert parse_modes("BCD") == ["B", "C", "D"]
    assert parse_modes("ABCD") == list(MODES)


def test_separated_and_transition_forms_parse():
    assert parse_modes("A/B") == ["A", "B"]
    assert parse_modes("B, D") == ["B", "D"]
    assert parse_modes("A→B") == ["A", "B"]        # arrow transition
    assert parse_modes("B->C") == ["B", "C"]
    assert parse_modes("ALL") == list(MODES)


def test_annotations_are_stripped_not_guessed():
    assert parse_modes("D (optional)") == ["D"]
    assert parse_modes("A/D (both)") == ["A", "D"]
    assert parse_modes("D / shield") == ["D"]


def test_unknown_syntax_fails_closed():
    """A non-empty mode cell must never quietly become 'no modes'."""
    for bad in ("doc", "Z", "mode-X", "TBD", "??"):
        with pytest.raises(ModeSyntaxError):
            parse_modes(bad)


def test_every_governed_mode_cell_parses():
    """Sweep the real records: no governed cell may fail closed."""
    unparsed = []
    for name in ("BOM.csv", "interface_map.csv", "interlock_table.csv"):
        path = ROOT / name
        if not path.exists():
            continue
        rows = list(csv.DictReader(open(path)))
        for col in (c for c in rows[0] if "mode" in c.lower()):
            for row in rows:
                try:
                    parse_modes(row[col] or "")
                except ModeSyntaxError:
                    unparsed.append((name, col, row[col]))
    assert unparsed == [], f"governed mode cells rejected: {unparsed[:10]}"
