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


@pytest.mark.parametrize("row_id", ["R59"])
def test_the_known_largest_gaps_are_still_recorded_as_gaps(row_id):
    """Guards against the matrix being 'closed' without the work.

    R55 (no production caller) and R21 (no tool execution) are the two the
    directive singles out. If either is ever marked complete, that must be
    because the subsystem exists -- and then this test should be updated in
    the same change that builds it, deliberately.

    R21 WAS closed, and this test fired, which is the mechanism working:
    closing it required editing this file. It moved to the check below rather
    than being deleted, because "it was closed on purpose" is a weaker claim
    than "it was closed with the things closing it requires".

    R55 was closed the same way, and fired here the same way. What closed it
    was a SECOND workflow running a SECOND tool whose result depends on the
    workspace rather than on its own request, a route guard making the
    governed path the only writer of the governed output subtrees, and a
    verifier that re-runs the tool instead of only re-deriving its digests.
    It moves to the watchlist below.

    R59 takes its place here. It is the last row still carrying residual
    gaps, and all three of them are about EVIDENCE from a hosted container
    build rather than about code -- which is exactly the kind of row that
    could be closed by deciding it was fine.
    """
    row = next(r for r in CM.load()["rows"] if r["id"] == row_id)
    if row["classification"] == CM.COMPLETE:
        pytest.fail(
            f"{row_id} is marked complete -- update this test in the change "
            "that completed it, so closing it stays a deliberate act")


@pytest.mark.parametrize("row_id", ["R21", "R55"])
def test_a_row_closed_from_the_watchlist_carries_what_closing_it_needed(
        row_id):
    """A row that graduated must still show its work, forever.

    Moving a row off the watchlist is a one-line edit. Requiring it to keep
    naming a production caller, mutation coverage and a stated boundary means
    that edit cannot be all that happened -- and that a later change quietly
    hollowing the row out fails here rather than nowhere.
    """
    row = next(r for r in CM.load()["rows"] if r["id"] == row_id)
    assert row["classification"] == CM.COMPLETE, (
        f"{row_id} left the watchlist and is not complete; put it back")
    assert row["production_caller"], f"{row_id}: complete with no caller"
    assert row["mutation_tests"], f"{row_id}: complete with no mutations"
    assert row["boundaries"], (
        f"{row_id}: complete to a 'technically defensible limit' that states "
        "no limit")
    assert not row["residual_gaps"]


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
        "residual_gaps": ["a real remaining gap, stated"],
        # A list, not the "" that `{f: "" for f in REQUIRED}` supplies.
        # The validator refuses a non-list, correctly, and this helper is
        # meant to produce a WELL-FORMED row so each test provokes exactly
        # one guard rather than that one.
        "boundaries": [], "blocker": None,
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


# --- the boundary vocabulary must not become a completion loophole ---------
#
# A residual gap keeps a row out of COMPLETE. A boundary does not. So the
# cheapest way to finish this matrix without finishing the system is to move
# sentences from one field to the other, and the tests below are the ones
# that have to fail if anybody -- including a future session of mine -- does
# that. Each attempts the smuggle for real, one boundary category at a time.

_SMUGGLE_DETAIL = (
    "a detail long enough to satisfy the substantive-argument rule, so that "
    "the test provokes the guard it is aiming at rather than the length one")


def _boundary(reason, limit, detail=_SMUGGLE_DETAIL):
    return _problems(boundaries=[
        {"limit": limit, "reason": reason, "detail": detail}])


@pytest.mark.parametrize("reason", sorted(CM.BOUNDARY_REASONS))
def test_no_boundary_category_accepts_a_sentence_that_names_work(reason):
    """Unfinished work is refused under EVERY reason, not most of them.

    Parametrized over the vocabulary itself rather than over a hand-written
    list, so a reason added later without a negative test fails here on the
    day it is added.
    """
    problems = _boundary(
        reason,
        "the compensating write path is not implemented, so a failed "
        "external effect is reported and left in place")
    assert any("residual gap, not a limit" in p for p in problems), (
        reason, problems)


@pytest.mark.parametrize("reason", sorted(
    CM.BOUNDARY_REASONS - CM._TESTABILITY_REASONS))
def test_absent_testing_is_refused_under_a_reason_that_cannot_explain_it(
        reason):
    """"It is intended this way" explains a missing behaviour, not a missing test.

    The subtler smuggle, and the one that does not look like cheating while
    you are doing it: the behaviour really is architectural, so the reason
    reads as true, and the sentence attached to it is about a test somebody
    simply did not write.
    """
    problems = _boundary(
        reason,
        "the optimistic-concurrency path has no test across two processes, "
        "so a lost update between them would go unnoticed")
    assert any("does not explain why the test cannot be written" in p
               for p in problems), (reason, problems)


@pytest.mark.parametrize("reason", sorted(CM._TESTABILITY_REASONS))
def test_absent_testing_is_allowed_where_the_reason_explains_it(reason):
    """And it must not refuse the honest ones.

    A two-writer test on a network filesystem cannot be written on a host
    with one filesystem. A guard that refused that sentence too would push
    real limits into evasive phrasing, which is worse than not having it.
    """
    assert not _boundary(
        reason,
        "two writers on a network filesystem are not covered, because "
        "flock semantics there differ from the ones available here"), reason


@pytest.mark.parametrize("bad_detail", ["", "   ", "too short to argue"])
def test_a_boundary_with_no_argument_behind_it_is_refused(bad_detail):
    problems = _boundary(
        "architectural_by_design",
        "the substrate mediates rather than contains, so a subprocess that "
        "opens its own socket is not stopped",
        detail=bad_detail)
    assert any("why no engineering in this repository closes" in p
               for p in problems), problems


def test_a_missing_detail_key_is_refused():
    problems = _problems(boundaries=[{
        "limit": "the substrate mediates rather than contains, so a "
                 "subprocess that opens its own socket is not stopped",
        "reason": "architectural_by_design"}])
    assert any("why no engineering in this repository closes" in p
               for p in problems), problems


def test_a_detail_that_restates_the_limit_is_not_an_argument():
    limit = ("the substrate mediates rather than contains, so a subprocess "
             "that opens its own socket is not stopped by the egress guard")
    problems = _boundary("architectural_by_design", limit, detail=limit)
    assert any("restates" in p for p in problems), problems


def test_a_reason_outside_the_vocabulary_is_refused():
    problems = _boundary(
        "out_of_scope",
        "the substrate mediates rather than contains, so a subprocess that "
        "opens its own socket is not stopped")
    assert any("is not one of" in p for p in problems), problems


def test_a_boundary_that_is_a_bare_sentence_is_refused():
    problems = _problems(boundaries=["we did not get to this one"])
    assert any("must be an object" in p for p in problems), problems


def test_an_honest_boundary_still_passes_every_guard():
    """The control. Each guard above must be refusing the smuggle and not
    the category, or the vocabulary becomes unusable and the next person
    writes residual gaps as prose in some other field."""
    assert not _boundary(
        "language_runtime",
        "zeroing a bytearray does not guarantee that no copy of the "
        "plaintext remains: str is immutable and the interpreter may have "
        "interned or copied it",
        detail="CPython exposes no primitive for erasing a value it has "
               "already copied, and no way to observe whether it did; what "
               "this repository can do -- bytearray storage, a load path "
               "that never builds a str, an explicit wipe -- it does")


# --- the guards that decide what COMPLETE means ---------------------------
#
# Added because tools/mutations/completion_matrix.json found them naked: the
# three checks that stand between "39 rows" and "39 finished rows" had no
# negative test at all, so deleting any of them changed nothing anybody
# would notice. That is the exact shape of defect this repository exists to
# refuse, occurring in the file that does the refusing.

def _complete(**over):
    over.setdefault("residual_gaps", [])
    over.setdefault("mutation_tests", ["tools/mutations/agent_scheduler.json"])
    over.setdefault("boundaries", [{
        "limit": "leases are checked against the log's position rather than "
                 "against a clock, so a host whose clock is wrong is refused "
                 "rather than trusted",
        "reason": "architectural_by_design",
        "detail": "a wall clock is a thing each host decides for itself, and "
                  "an authority that trusted one would be delegating its "
                  "verdict to whichever machine happened to run the job"}])
    return _problems(classification=CM.COMPLETE, **over)


def test_complete_may_not_be_claimed_while_gaps_are_still_listed():
    """The one arithmetic relation the whole matrix rests on.

    If COMPLETE can coexist with an open gap list, "39/39 complete, 0
    residual gaps" stops being a statement about the system and becomes a
    statement about how the rows were filled in.
    """
    problems = _complete(residual_gaps=["the compensation path is a stub"])
    assert any("claimed with residual" in p for p in problems), problems


def test_complete_may_not_be_claimed_without_mutation_coverage():
    problems = _complete(mutation_tests=[])
    assert any("no mutation coverage" in p for p in problems), problems


def test_complete_may_not_be_claimed_without_stating_a_limit():
    """The classification ends in "to current technically defensible LIMIT".

    A row that names no limit has not finished the sentence, and silence is
    the cheapest way to overstate a system: nobody reads an absent field.
    """
    problems = _complete(boundaries=[])
    assert any("no boundaries stated" in p for p in problems), problems


def test_an_otherwise_sound_complete_row_passes():
    """The control for the three above."""
    assert not _complete()


def test_the_testability_reasons_are_a_strict_subset_of_the_vocabulary():
    """Guarding the guard, because widening a set reads as tidying up.

    test_absent_testing_is_refused_under_a_reason_that_cannot_explain_it
    parametrizes over the COMPLEMENT of this set. Widen the set to the whole
    vocabulary and that test is parametrized zero times -- it passes by
    running nothing, which is the vacuous-success defect wearing a green
    tick. So the complement is asserted to be non-empty and to contain, by
    name, the reasons that explain an absent BEHAVIOUR and can never explain
    an absent TEST.
    """
    cannot_excuse = CM.BOUNDARY_REASONS - CM._TESTABILITY_REASONS
    assert cannot_excuse >= {
        "architectural_by_design",
        "language_runtime",
        "requires_external_identity_authority",
    }, cannot_excuse


def test_the_fuzz_staleness_guard_names_the_target_it_found():
    """The second staleness species, which had no test either.

    Three rows were once found saying "no fuzzing of X" while a target for X
    was already registered -- an understated row is drift too, and it keeps
    finished work out of COMPLETE while looking rigorous.
    """
    problems = _problems(residual_gaps=[
        "no fuzzing of the policy decision records"])
    assert any("registers target" in p for p in problems), problems


def test_the_fuzz_staleness_guard_leaves_an_honest_claim_alone():
    assert not _problems(residual_gaps=[
        "no fuzzing of the LCVD solver, which lives outside this package"])
