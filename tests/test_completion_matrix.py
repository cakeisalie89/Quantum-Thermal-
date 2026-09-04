"""The completion matrix must not drift into fiction.

A self-assessment nobody checks becomes optimistic one edit at a time. This
runs the validator, so every mechanically checkable claim in
``docs/completion_matrix.json`` is checked on every test run: paths exist,
mutation specs actually mutate the row's own implementation, production
callers really reference what they claim, and a row cannot be classified above
its evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tools"))
import completion_matrix as CM  # noqa: E402


def test_the_matrix_is_self_consistent():
    problems = CM.validate(CM.load())
    assert not problems, "completion matrix is invalid:\n  " + "\n  ".join(
        problems)


def test_every_requirement_row_is_present():
    """R21-R59, no gaps. A missing row is a requirement quietly dropped."""
    ids = {r["id"] for r in CM.load()["rows"]}
    expected = {f"R{n}" for n in range(21, 60)}
    assert ids == expected, f"missing {sorted(expected - ids)}, " \
                            f"unexpected {sorted(ids - expected)}"


def test_no_row_claims_completion_without_mutation_coverage():
    """Restated as its own test because it is the claim most worth pinning.

    'Complete' is the only classification that closes a row, so it is the one
    an optimistic edit would reach for first.
    """
    for row in CM.load()["rows"]:
        if row["classification"] == CM.COMPLETE:
            assert row["mutation_tests"], f"{row['id']}: complete, no mutations"
            assert not row["residual_gaps"], f"{row['id']}: complete with gaps"


def test_blocked_rows_name_what_would_unblock_them():
    """A blocker without an exit is an excuse."""
    for row in CM.load()["rows"]:
        if row["classification"] in CM.BLOCKED:
            b = row["blocker"]
            assert b and b.get("unblocked_by"), \
                f"{row['id']}: blocked with no stated route out"
            assert b.get("missing_input"), \
                f"{row['id']}: blocked without naming the missing input"


def test_the_matrix_does_not_claim_scientific_authority():
    doc = CM.load()
    blob = " ".join(str(v) for v in (doc["label"], doc["does_not_mean"],
                                     doc["rule"])).lower()
    assert "pass remains 0" in blob or "pass" in blob
    assert "model_only" in blob or "model-only" in blob


@pytest.mark.parametrize("row_id", ["R55", "R21"])
def test_the_known_largest_gaps_are_still_recorded_as_gaps(row_id):
    """Guards against the matrix being 'closed' without the work.

    R55 (no production caller) and R21 (no tool execution) are the two the
    directive singles out. If either is ever marked complete, that must be
    because the subsystem exists -- and then this test should be updated in
    the same change that builds it, deliberately.
    """
    row = next(r for r in CM.load()["rows"] if r["id"] == row_id)
    if row["classification"] == CM.COMPLETE:
        pytest.fail(
            f"{row_id} is marked complete -- update this test in the change "
            "that completed it, so closing it stays a deliberate act")
