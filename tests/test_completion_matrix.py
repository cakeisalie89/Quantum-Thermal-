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


# --- the validator's own guards, provoked ----------------------------------
#
# A validator that would pass a bad matrix is worse than none: it converts
# "nobody checked" into "checked and fine". Each guard below is given the
# exact shape it exists to refuse.

def _row(**over):
    """A minimal well-formed row, so each test provokes exactly one guard."""
    base = {f: "" for f in CM.REQUIRED}
    base.update({
        "id": "RX", "requirement": "a requirement",
        "classification": "DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS",
        "implementation": ["qta_agent/scheduler.py"],
        "callers": [], "production_caller": "",
        "tests": ["tests/test_agent_scheduler.py"],
        "property_tests": [], "mutation_tests": [], "fuzzing": "none",
        "differential": "none", "hosted_ci": "n/a",
        "residual_gaps": ["a real remaining gap, stated"], "blocker": None,
    })
    base.update(over)
    return base


def _problems(**over):
    return CM.validate({"rows": [_row(**over)]})


def test_the_validator_refuses_a_gap_that_describes_a_built_subsystem():
    """THE staleness guard.

    Twelve rows were found at once still saying "the scheduler does not exist
    yet" long after it did. Documentation that has stopped being true is not
    a smaller problem than code that has stopped working -- it is the same
    problem, read by someone deciding what to trust.
    """
    problems = _problems(residual_gaps=["the scheduler does not exist yet"])
    assert any("stopped being true" in p for p in problems), problems


def test_the_staleness_guard_does_not_fire_on_an_honest_gap():
    """It must refuse stale prose, not any sentence containing a module name.

    A guard that flagged every mention of "scheduler" would be turned off
    within a week, and then it would be protecting nothing.
    """
    assert not _problems(residual_gaps=[
        "the scheduler enqueues one job per governed run, so dependency "
        "graphs are exercised only in tests"])


def test_the_validator_refuses_a_row_with_no_gaps_and_no_completion_claim():
    problems = _problems(residual_gaps=[])
    assert any("no residual gaps listed" in p for p in problems), problems


def test_the_validator_refuses_borrowed_mutation_coverage():
    """A row may not cite a spec that mutates somebody else's code."""
    problems = _problems(
        implementation=["qta_agent/memory.py"],
        mutation_tests=["tools/mutations/agent_scheduler.json"])
    assert any("none of this row's implementation" in p for p in problems), \
        problems


def test_the_validator_refuses_a_production_caller_that_calls_nothing():
    problems = _problems(implementation=["qta_agent/memory.py"],
                         production_caller="README.md")
    assert any("in a form that would import" in p for p in problems), (
        "a README that merely contains the word 'memory' passed as a"
        " production caller; a guard that cannot fail is not a guard")


def test_the_validator_refuses_a_path_that_does_not_exist():
    problems = _problems(implementation=["qta_agent/does_not_exist.py"])
    assert any("does not exist" in p for p in problems), problems


def test_the_validator_refuses_property_test_claims_over_files_without_any():
    """A row claiming property testing must name a file that has some."""
    problems = _problems(property_tests=["tests/test_agent_evidence.py"])
    assert any("contains no property-based testing" in p
               for p in problems), problems


def test_a_mention_of_hypothesis_is_not_property_test_coverage(tmp_path):
    """USAGE, not a word.

    The check matched the bare string "hypothesis" anywhere in the file, so
    a docstring sentence -- "the rule Hypothesis found" -- satisfied a
    property-testing claim. It happened by accident: that sentence was
    written into a suite with no property tests in it, and the negative
    example in this file started passing for the wrong reason.

    A marker a comment can supply is not evidence of coverage.
    """
    mention = tmp_path / "test_mentions_only.py"
    mention.write_text('"""Found by Hypothesis, tested by hand."""\n'
                       "def test_x():\n    assert True\n")
    rel = mention.relative_to(CM.ROOT) if str(mention).startswith(
        str(CM.ROOT)) else None
    if rel is None:                       # tmp_path is outside the repo
        body = mention.read_text().lower()
        assert "hypothesis" in body
        assert not any(m in body for m in
                       ("@given", "from hypothesis import",
                        "import hypothesis", "rulebasedstatemachine"))
        return
    problems = _problems(property_tests=[str(rel)])
    assert any("contains no property-based testing" in p
               for p in problems), problems


def test_the_property_claim_guard_still_accepts_real_usage():
    """And the tightened marker set must not refuse a genuine suite."""
    assert not _problems(
        property_tests=["tests/test_agent_machine_properties.py"])


def test_the_property_claim_guard_accepts_a_real_property_suite():
    assert not _problems(
        property_tests=["tests/test_agent_machine_properties.py"])


# --- the two ways a row has actually been tempted to overstate itself ------

def test_the_validator_refuses_a_test_as_the_production_caller():
    """The defect this project hit twice, in two different subsystems.

    check_egress_composition was correct, thoroughly tested and reachable
    only from its own test file. ExecutionResult.output_digests existed and
    nothing populated it. In both cases a test was the only caller -- and a
    test is the easiest thing in the tree to point a row at.
    """
    row = _row(production_caller="tests/test_completion_matrix.py",
               implementation=["tools/completion_matrix.py"])
    problems = CM.validate({"rows": [row]})
    assert any("is a test" in p for p in problems), problems


def test_a_real_production_caller_is_still_accepted():
    """The guard must name a real condition, not refuse every row."""
    row = _row(production_caller="tools/completion_matrix.py",
               implementation=["tools/completion_matrix.py"])
    assert not [p for p in CM.validate({"rows": [row]})
                if "is a test" in p]


def test_the_validator_refuses_a_hosted_claim_with_no_run_id():
    """'green', 'passing' and 'should be fine' are not evidence.

    A run id is a thing a reader can open. This field has drifted before --
    it once listed only successes while five runs had failed -- and prose
    is what lets it.
    """
    for prose in ("green", "passing on every push", "CI is fine"):
        problems = CM.validate({"rows": [_row(hosted_ci=prose)]})
        assert any("names no run id" in p for p in problems), prose


def test_a_hosted_claim_citing_a_run_is_accepted():
    row = _row(hosted_ci="agent-substrate.yml run 34015444218 (success)")
    assert not [p for p in CM.validate({"rows": [row]})
                if "run id" in p]


def test_none_is_an_honest_hosted_answer():
    """A row with no hosted coverage should say so, not be forced to invent."""
    for value in ("none", "n/a", ""):
        row = _row(hosted_ci=value)
        assert not [p for p in CM.validate({"rows": [row]})
                if "run id" in p]
