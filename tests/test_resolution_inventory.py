"""Where the resolution discipline has reached, and where it has not.

D-2026-66. `tools/cross_env_semantics.py` reports on a column's resolution
only when that column happens to CROSS ZERO between two environments, so the
apparent coverage of D-2026-53 was a fact about which two machines had run.
Measured over every governed numeric column instead, it was 19 of 162.

Every refusal test here is paired with a positive control: a checker that
refused every inventory would pass the refusals and be worthless, and one
that refused none would license the sentence "every published number states
what its method resolves".

MODEL-ONLY / FORECAST-ONLY. Nothing here changes a gate, a threshold or a
canonical output. PASS remains 0.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tools import resolution_inventory as RI

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _declared():
    return json.loads(RI.INVENTORY.read_text(encoding="utf-8"))["columns"]


# --- the reconciliation, both directions ----------------------------------

def test_the_committed_inventory_reconciles():
    """The positive control, and the only one that runs against the tree."""
    assert RI.reconcile(RI.governed_columns(), _declared()) == []


def test_a_governed_column_with_no_basis_is_refused():
    d = _declared()
    key = next(iter(sorted(RI.governed_columns() & set(d))))
    d = {k: v for k, v in d.items() if k != key}
    problems = RI.reconcile(RI.governed_columns(), d)
    assert any(key in p and "no resolution basis" in p for p in problems)


def test_a_declaration_for_a_column_that_no_longer_exists_is_refused():
    d = dict(_declared())
    d["gone.csv:vanished_column"] = {"basis": "NO_FLOOR_DEFINED", "why": "x"}
    problems = RI.reconcile(RI.governed_columns(), d)
    assert any("schema that has moved on" in p for p in problems)


# --- a carrier is checked, not taken on trust -----------------------------

def test_a_carrier_that_names_a_missing_column_is_refused():
    d = dict(_declared())
    key = "gas_transport_profile.csv:n_CH4_modeC_1m3"
    d[key] = {"basis": "CARRIER", "resolution_from": "not_a_column"}
    problems = RI.reconcile(RI.governed_columns(), d)
    assert any("not a readable column" in p for p in problems)


def test_a_carrier_holding_something_other_than_classes_is_refused():
    """Naming a column is not carrying a class.

    The same anti-proxy rule claims_enforcement.py applies to a pattern that
    names a claim and does not match it.
    """
    d = dict(_declared())
    key = "gas_transport_profile.csv:n_CH4_modeC_1m3"
    d[key] = {"basis": "CARRIER", "resolution_from": "x_m"}
    problems = RI.reconcile(RI.governed_columns(), d)
    assert any("are not resolution classes" in p for p in problems)


def test_a_real_carrier_passes():
    """The control for the two above."""
    d = dict(_declared())
    key = "gas_transport_profile.csv:n_CH4_modeC_1m3"
    assert d[key]["basis"] == "CARRIER"
    problems = RI.reconcile(RI.governed_columns(), {key: d[key]}
                            | {k: v for k, v in d.items() if k != key})
    assert problems == []


# --- the rule that stops the example being closed for the class -----------

def test_a_bare_column_beside_a_declared_one_is_refused():
    """The within-artefact rule, and the reason it exists.

    D-2026-53 landed on `residual_mode_D_density_m3` and left
    `max_density_m3` and `sample_region_density_modeB_m3` bare in the same
    row, out of the same solve, against the same floor. Where the mechanism
    has reached an artefact, a sibling published without a class is a gap
    that can be closed rather than a limit of the method.
    """
    d = dict(_declared())
    key = "gas_transport_metrics.csv:max_density_m3"
    d[key] = {"basis": "NO_FLOOR_DEFINED", "why": "pretending nothing exists"}
    problems = RI.reconcile(RI.governed_columns(), d)
    assert any("already declares a resolution for another of its columns" in p
               for p in problems)


def test_a_bare_column_in_an_artefact_the_mechanism_has_not_reached_is_allowed():
    """The control. 120 columns are legitimately open.

    Refusing them would force a floor to be invented for every solver in the
    package, and a fabricated floor is worse than an absent one.
    """
    d = _declared()
    open_keys = [k for k, v in d.items()
                 if v.get("basis") == "NO_FLOOR_DEFINED"]
    assert open_keys, "the open set must not be empty or this proves nothing"
    assert RI.reconcile(RI.governed_columns(), d) == []


def test_an_exemption_must_say_why():
    d = dict(_declared())
    key = "gas_transport_profile.csv:x_m"
    d[key] = {"basis": "COORDINATE"}
    problems = RI.reconcile(RI.governed_columns(), d)
    assert any("states no reason" in p for p in problems)


def test_an_unknown_basis_is_refused():
    d = dict(_declared())
    key = "gas_transport_profile.csv:x_m"
    d[key] = {"basis": "PROBABLY_FINE", "why": "it looked alright"}
    problems = RI.reconcile(RI.governed_columns(), d)
    assert any("is not one of" in p for p in problems)


# --- anti-vacuity ---------------------------------------------------------

def test_an_empty_governed_set_is_refused():
    with pytest.raises(RI.ScopeError, match="would report full coverage"):
        RI.reconcile(set(), _declared())


def test_an_empty_inventory_is_refused():
    with pytest.raises(RI.ScopeError, match="declares nothing"):
        RI.reconcile(RI.governed_columns(), {})


def test_the_refusal_is_a_raise_and_not_an_assert():
    """`python -O` deletes asserts."""
    src = (ROOT / "tools" / "resolution_inventory.py").read_text(
        encoding="utf-8")
    assert "raise ScopeError" in src
    assert "\n    assert " not in src


# --- the number itself ----------------------------------------------------

def test_the_report_states_the_scope_it_covered(capsys):
    """D-2026-54's shape: a verdict without its scope is a claim on trust."""
    assert RI.main([]) == 0
    out = capsys.readouterr().out
    n = len(RI.governed_columns())
    assert f"{n} governed numeric columns" in out
    assert "have no floor defined" in out


def test_the_open_gap_is_reported_rather_than_rounded_away():
    """If this ever reads zero, check that it was closed and not redefined."""
    d = _declared()
    open_n = sum(1 for v in d.values()
                 if v.get("basis") == "NO_FLOOR_DEFINED")
    assert open_n > 0, (
        "the open set is empty -- either every solver grew a declared floor, "
        "which is worth a ledger entry, or the definition moved")
