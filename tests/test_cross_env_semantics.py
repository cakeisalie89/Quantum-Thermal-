"""The instrument that tells a changed verdict from a changed digit.

Every refusal test here is paired with a positive control. A comparator that
refused everything would pass all the refusal tests and be worthless, and a
comparator that refused nothing would pass all the acceptance tests and be
worse than worthless -- it would license the sentence "no decision changed".
"""
from __future__ import annotations

import json

import pytest

from tools.cross_env_semantics import (
    DECISION, PRECISION, SIGN_FLIP, ZERO_CROSSING, ScopeError, check_scope,
    classify, compare,
)


def _tree(root, name, payload):
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(payload if isinstance(payload, str)
                 else json.dumps(payload, indent=2))
    return p


def _kinds(before, after):
    return [k for k, _, _, _ in classify(before, after)]


# --- what counts as a decision -------------------------------------------

def test_a_changed_status_is_a_decision():
    assert _kinds("CONDITIONAL", "PASS") == [DECISION]


def test_a_changed_boolean_is_a_decision():
    assert _kinds("{'ok': True}", "{'ok': False}") == [DECISION]


def test_a_word_changing_is_a_decision_even_when_every_number_agrees():
    """The residue is compared BEFORE the numbers, and this is why.

    Both sides carry the identical quantity. Comparing them as whole strings
    would find a difference and have to guess what kind; comparing what is
    left after the numbers are removed knows immediately.
    """
    assert _kinds("status=OK; v=1.5", "status=BAD; v=1.5") == [DECISION]


def test_a_number_that_became_nan_is_not_called_a_precision_event():
    assert _kinds("1.5", "nan") == [DECISION]


# --- what counts as precision, and the control that it is not everything ---

def test_a_moved_last_digit_is_precision_and_is_not_refused():
    (kind, _, _, rel), = classify("108740.08984348577", "108740.08984349713")
    assert kind == PRECISION
    assert rel < 1e-12


def test_the_classifier_is_not_simply_calling_everything_a_decision():
    """The control for every refusal test above."""
    assert _kinds("1.0000001", "1.0000002") == [PRECISION]
    assert _kinds("CONDITIONAL", "CONDITIONAL") == []


# --- the class that is neither ------------------------------------------

def test_exactly_zero_becoming_nonzero_is_not_a_precision_event():
    """A published exact zero is a claim; a nonzero neighbour contradicts it.

    Relative difference cannot express this: against zero it is 1.0 for any
    nonzero value whatsoever, which would rank a 4e-09 underflow alongside a
    catastrophe. It gets its own class because it is its own kind of event.
    """
    assert _kinds("0.000000000e+00", "1.561645593e-02") == [ZERO_CROSSING]
    assert _kinds("1.615587134e-27", "0.000000000e+00") == [ZERO_CROSSING]


def test_opposite_signs_are_a_sign_flip_not_a_precision_event():
    assert _kinds("1.510451e-06", "-1.510451e-06") == [SIGN_FLIP]


def test_a_zero_crossing_is_not_also_counted_as_a_sign_flip():
    """Ordering inside the classifier: zero is tested before sign."""
    kinds = _kinds("0.0", "-4.0e-09")
    assert kinds == [ZERO_CROSSING]


# --- the scope check: "nothing changed" must not be true of nothing -------

def test_a_comparison_that_lines_up_no_files_at_all_is_refused(tmp_path):
    _tree(tmp_path / "other", "renamed_output.csv", "a,b\n1,2\n")
    _tree(tmp_path / "committed", "original_output.csv", "a,b\n1,2\n")
    report = compare(tmp_path / "other", tmp_path / "committed")
    assert report["files_compared"] == 0
    with pytest.raises(ScopeError, match="compared 0 files"):
        check_scope(report)


def test_the_scope_check_accepts_a_comparison_that_did_look_at_something(
        tmp_path):
    """The control. Otherwise the test above passes on a check wired to
    refuse unconditionally."""
    _tree(tmp_path / "other", "shared.csv", "a,b\n1,2\n")
    _tree(tmp_path / "committed", "shared.csv", "a,b\n1,3\n")
    report = compare(tmp_path / "other", tmp_path / "committed")
    assert report["files_compared"] == 1
    check_scope(report)


def test_scope_is_refused_when_files_differ_but_share_no_shape(tmp_path):
    _tree(tmp_path / "other", "s.json", {"only_here": 1})
    _tree(tmp_path / "committed", "s.json", {"only_there": 1})
    report = compare(tmp_path / "other", tmp_path / "committed")
    assert report["files_differing"] == 1
    with pytest.raises(ScopeError, match="do not share a shape"):
        check_scope(report)


def test_scope_refusal_is_a_raise_and_not_an_assert():
    """`python -O` deletes asserts. An enforcement point a flag removes is
    not an enforcement point -- the lesson D-2026-45 recorded."""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "tools" / "cross_env_semantics.py").read_text()
    body = src.split("def check_scope")[1].split("\ndef ")[0]
    assert "assert " not in body
    assert "raise ScopeError" in body


# --- end to end -----------------------------------------------------------

def test_a_flipped_readiness_status_is_refused_end_to_end(tmp_path):
    """The exact claim this package exists to prevent, smuggled in as a
    byte difference that `cmp` would report the same as a moved digit."""
    _tree(tmp_path / "committed", "r.json",
          {"status": "FORECAST_ONLY_IMPLEMENTED", "value": 1.25})
    _tree(tmp_path / "other", "r.json",
          {"status": "VALIDATED_ON_HARDWARE", "value": 1.25})
    report = compare(tmp_path / "other", tmp_path / "committed")
    assert report["counts"][DECISION] == 1


def test_the_same_file_differing_only_in_digits_is_not_refused(tmp_path):
    """The control for the test above: same shape, same words, moved float."""
    _tree(tmp_path / "committed", "r.json",
          {"status": "FORECAST_ONLY_IMPLEMENTED", "value": 1.2500000000001})
    _tree(tmp_path / "other", "r.json",
          {"status": "FORECAST_ONLY_IMPLEMENTED", "value": 1.2500000000002})
    report = compare(tmp_path / "other", tmp_path / "committed")
    assert report["counts"][DECISION] == 0
    assert report["counts"][PRECISION] == 1


def test_a_key_present_on_one_side_only_is_reported_not_ignored(tmp_path):
    _tree(tmp_path / "committed", "s.json", {"a": 1, "b": 2})
    _tree(tmp_path / "other", "s.json", {"a": 1, "c": 2})
    report = compare(tmp_path / "other", tmp_path / "committed")
    assert report["shape_changes"], "a changed shape must not pass silently"
    assert report["shape_changes"][0]["file"] == "s.json"


# --- the exemption must not drift away from the one it mirrors ------------

def test_the_exemption_matches_the_one_the_byte_gate_already_uses():
    """`REGEN_EXEMPT` is duplicated from `package_consistency_check.py`
    because importing that module runs its entire check. A duplicated
    constant with no owner is the defect D-2026-47 recorded, so this is the
    owner: the two sets must be equal, and a name added to either one alone
    fails here rather than quietly changing what one gate looks at.
    """
    import ast
    import pathlib

    from tools.cross_env_semantics import REGEN_EXEMPT

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "package_consistency_check.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "_REGEN_EXEMPT"
                        for t in node.targets)):
            theirs = frozenset(ast.literal_eval(node.value.args[0]))
            break
    else:                                       # pragma: no cover - defensive
        raise AssertionError(
            "package_consistency_check.py no longer defines _REGEN_EXEMPT; "
            "the exemption this tool mirrors has moved or gone")
    assert REGEN_EXEMPT == theirs, (
        f"exemptions have drifted: this tool skips {sorted(REGEN_EXEMPT)}, "
        f"the byte gate skips {sorted(theirs)}")


def test_an_exempt_file_is_reported_rather_than_silently_dropped(tmp_path):
    _tree(tmp_path / "committed", "x.json", {"status": "A"})
    _tree(tmp_path / "other", "x.json", {"status": "B"})
    report = compare(tmp_path / "other", tmp_path / "committed",
                     exempt=frozenset({"x.json"}))
    assert report["exempted"] == ["x.json"]
    assert report["counts"][DECISION] == 0


def test_exempting_a_file_is_the_only_reason_it_is_skipped(tmp_path):
    """The control: without the exemption the same pair IS refused, so the
    test above is measuring the exemption and not an empty comparison."""
    _tree(tmp_path / "committed", "x.json", {"status": "A"})
    _tree(tmp_path / "other", "x.json", {"status": "B"})
    report = compare(tmp_path / "other", tmp_path / "committed",
                     exempt=frozenset())
    assert report["counts"][DECISION] == 1
