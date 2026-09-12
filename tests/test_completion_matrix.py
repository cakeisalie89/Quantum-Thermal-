"""The completion matrix must not drift into fiction.

A self-assessment nobody checks becomes optimistic one edit at a time. This
runs the validator, so every mechanically checkable claim in
``docs/completion_matrix.json`` is checked on every test run: paths exist,
mutation specs actually mutate the row's own implementation, production
callers really reference what they claim, and a row cannot be classified above
its evidence.
"""
from __future__ import annotations

import pathlib
import re
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


#: Rows that must be COMPLETE for this file to pass, and the count that must
#: hold. Written down so that changing it is an edit somebody makes on
#: purpose, in the commit that changes it, rather than a number that drifts.
#:
#: 39 -> 38 on D-2026-35. R41 (Checkpointing) is now
#: DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS: `latest_usable` decided "describes
#: this log" from the log's SIZE, and the row said the same sentence the code
#: did. The defect is fixed; what moved the row is the gap the fix exposed
#: and did not close -- `CheckpointStore.audit()` answers a parse question
#: and is named like a health question, so a store holding checkpoints for a
#: log nobody has audits ok.
#:
#: 38 -> 37 on D-2026-37. R49 (performance regression guards) claimed "a
#: guard fails on shape, not on wall time, so a slow machine cannot fail
#: it". A hosted runner failed one at 4.74 against a 4.0 ceiling while the
#: property it guards was intact. That guard now counts re-hashed records;
#: the row moves because seven others still assert on a wall-clock ratio and
#: the argument for their robustness is a judgement, not a measurement.
#:
#: The direction of these edits is the point. The number is an output of the
#: rows, not a target to hold: a finding that shows a row is not complete
#: moves the row, and this line follows it down as readily as up.
EXPECTED_COMPLETE = 37

#: And how many rows there ARE, which is a different number and was not
#: treated as one. The assertion below used to read
#: ``len(rows) == EXPECTED_COMPLETE``, which is true only while every row is
#: complete -- an equality that held by circumstance, written down as a rule.
#: It fired the moment a row moved, saying "one of the two numbers is wrong"
#: about two numbers that were both right.
EXPECTED_ROWS = 39


def test_the_matrix_is_not_completed_silently():
    """The last row closing is the easiest thing in the tree to do quietly.

    Every earlier version of this guard watched a NAMED open row and fired
    when it closed -- R21, then R55, then R59 -- so closing one always
    required editing this file. With nothing left open there is no row to
    name, and a guard with an empty parametrize list passes over nothing,
    which is the vacuous shape this repository has shipped once.

    So the guard changes shape rather than disappearing: the number of
    complete rows is written down here. Reclassifying a row, or adding a
    fortieth, fails until somebody edits this line.
    """
    rows = CM.load()["rows"]
    complete = [r for r in rows if r["classification"] == CM.COMPLETE]
    assert len(complete) == EXPECTED_COMPLETE, (
        f"{len(complete)} rows are complete and this test expects "
        f"{EXPECTED_COMPLETE}. If that is right, say so here in the same "
        "change; if it is not, the matrix moved without anybody deciding to")
    assert len(rows) == EXPECTED_ROWS, (
        f"{len(rows)} rows against an expected {EXPECTED_ROWS}; a row was "
        "added or removed, which is a decision that belongs in this file too")
    assert EXPECTED_COMPLETE <= EXPECTED_ROWS, (
        "more complete rows expected than rows exist")

    # AND COMPLETE MEANS WHAT IT SAYS. A row cannot reach it by having
    # nothing written in it.
    for row in complete:
        assert row["boundaries"], (
            f"{row['id']}: complete to a 'technically defensible limit' that "
            "states no limit")
        assert not row["residual_gaps"], f"{row['id']}: complete with gaps"
        assert row["mutation_tests"], f"{row['id']}: complete, no mutations"


def test_the_last_row_was_closed_by_evidence_not_by_decision():
    """R59 was the last open row, and it is the easiest kind to close wrongly.

    Its three gaps were all about EVIDENCE rather than code, and evidence
    gaps close by somebody deciding they are fine. What actually closed it
    was reproducing another machine's exact result on this one -- the same
    twenty-three files by name, not two counts that were close -- so this
    asserts the row still carries the things that reproduction required.

    It has no production_caller and should not: the row is an analysis, not
    a subsystem, and its callers are the two workflows that run its
    instruments.
    """
    row = next(r for r in CM.load()["rows"] if r["id"] == "R59")
    assert row["classification"] == CM.COMPLETE
    assert row["mutation_tests"], "R59: complete with no mutation coverage"
    assert row["boundaries"] and not row["residual_gaps"]
    # A hosted claim with a run id somebody can open, not a mood.
    assert re.search(r"\b\d{8,}\b", row["hosted_ci"]), row["hosted_ci"]
    # The reproduction, not a resemblance.
    blob = " ".join(str(row[f]) for f in ("evidence", "differential")).lower()
    assert "reproduc" in blob, (
        "R59 no longer claims the other host's result was reproduced, which "
        "is the difference between an explanation and an observation")
    assert "63" in blob and "40" in blob, (
        "R59 no longer carries the numbers the attribution rests on")


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
        # The evidence axis (D-2026-44). A well-formed row has to SAY what a
        # hosted runner has checked, and "nothing yet" is a legitimate thing
        # to say -- it is saying nothing that the schema now refuses.
        "hosted_evidence": {"runs": [], "commit": None,
                            "implementation_sha256": None},
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


def test_a_row_with_no_run_may_not_name_one_in_its_prose():
    """The defect the OLD rule was defeated by, kept as its replacement.

    The old rule was "hosted_ci must contain a run id, not a mood", and the
    regex that enforced it accepted

        pending: added after run 33939090740

    -- a sentence whose meaning is that NO hosted run covers this row,
    passing a check about citing runs because it names one while denying
    it. Fourteen rows were classified COMPLETE on that string (D-2026-44).

    So the prose is no longer where the claim lives. What remains checkable
    about prose is the contradiction: recording no run, and naming one.
    """
    row = _row(hosted_ci="pending: added after run 33939090740",
               hosted_evidence={"runs": [], "commit": None,
                                "implementation_sha256": None})
    problems = CM.validate({"rows": [row]})
    assert any("records NO run, and hosted_ci still names one" in p
               for p in problems), problems


def test_a_run_cited_without_a_commit_is_refused():
    """A run not tied to a commit cannot be checked against any code.

    This is the whole finding in one rule: 21 rows cited two runs, and the
    commit those runs were for was never written down anywhere in the row.
    """
    row = _row(hosted_evidence={"runs": ["34015444218"], "commit": None,
                                "implementation_sha256": None})
    assert any("no commit" in p for p in CM.validate({"rows": [row]})), \
        CM.validate({"rows": [row]})


def test_a_commit_cited_without_a_digest_is_refused():
    row = _row(hosted_evidence={"runs": ["34015444218"], "commit": "abc1234",
                                "implementation_sha256": None})
    assert any("nothing can be recomputed" in p
               for p in CM.validate({"rows": [row]}))


def test_a_well_formed_evidence_record_is_accepted():
    """The control: the guards above must name a condition, not refuse all."""
    row = _row(hosted_evidence={"runs": ["34015444218"], "commit": "abc1234",
                                "implementation_sha256": "a" * 64})
    assert not [p for p in CM.validate({"rows": [row]})
                if "hosted_evidence" in p or "run id" in p]


# --- the digest is what makes the evidence axis measurable ----------------

def test_absence_is_part_of_the_implementation_digest():
    """A file that did not exist then must not hash as though it did.

    Not hypothetical: 33 of the 39 rows name at least one implementation
    file that postdates the run they cited. R22 cited two runs as its
    evidence while naming five files that were not in the tree when those
    runs went green.
    """
    paths = ["a.py", "b.py"]
    both = CM.implementation_digest(
        paths, lambda p: b"x", lambda p: [])
    one = CM.implementation_digest(
        paths, lambda p: (b"x" if p == "a.py" else None), lambda p: [])
    assert both != one, (
        "a row whose implementation file was absent at the cited commit "
        "hashes the same as one where it was present")


def test_the_absent_marker_is_a_DELIMITER_and_not_only_a_flag():
    """The test above is weaker than its name, and the harness said so.

    C15 removes the ``ABSENT`` marker and writes nothing for a missing
    file. The test above still passes over that mutation: the PATH is
    hashed either way, so present-vs-absent still changes the digest, and
    the assertion never notices what was lost.

    What the marker actually provides is a DELIMITER. Without it, the
    digest input for a list of absent files is their names concatenated,
    and concatenation is ambiguous -- reproduced rather than argued:

        implementation ['ab']     and ['a', 'b']
        both absent, no marker -> fb8e20fc2e4c3f24...  (identical)
        both absent, marker    -> 42c13e28... / 4f2a96d0...  (distinct)

    Two different implementation lists hashing the same is a digest that
    cannot say which row's evidence it is. Kept as its own test because
    the first one reads as though it covers this and does not.
    """
    absent = lambda p: None                                    # noqa: E731
    nodirs = lambda p: []                                      # noqa: E731
    assert CM.implementation_digest(["ab"], absent, nodirs) != \
        CM.implementation_digest(["a", "b"], absent, nodirs), (
            "two different implementation lists hash identically, so the "
            "digest cannot identify the code a run covered")


def test_a_file_added_to_a_named_directory_changes_the_digest():
    """Rows name directories, and a spec ADDED to one is coverage the run
    did not have."""
    before = CM.implementation_digest(
        ["d"], lambda p: b"x", lambda p: ["d/one.json"])
    after = CM.implementation_digest(
        ["d"], lambda p: b"x", lambda p: ["d/one.json", "d/two.json"])
    assert before != after


def test_evidence_that_still_covers_the_code_is_reported_as_covering():
    """ANTI-VACUITY. Every row in the live matrix derives PREDATES or worse,
    so a derivation that always said PREDATES would look identical.
    """
    row = _row(implementation=["a.py"])
    read, listdir = (lambda p: b"unchanged"), (lambda p: [])
    row["hosted_evidence"] = {
        "runs": ["34015444218"], "commit": "abc1234",
        "implementation_sha256": CM.implementation_digest(
            ["a.py"], read, listdir)}
    assert CM.evidence_state(row, read, listdir) == CM.EV_COVERS
    # and one byte later it does not
    assert CM.evidence_state(row, lambda p: b"changed", listdir) == \
        CM.EV_PREDATES


def test_a_cited_run_with_no_recorded_commit_is_not_called_stale():
    """"I cannot tell" is not "I checked and it is old".

    Four rows cite real runs whose commit was never recorded. Reporting
    them as PREDATES would claim a measurement nobody made.
    """
    row = _row(hosted_evidence={"runs": ["34015444218"],
                                "commit": "UNRECORDED",
                                "implementation_sha256": None})
    assert CM.evidence_state(row, lambda p: b"x", lambda p: []) == \
        CM.EV_UNRESOLVABLE


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


# --- what the SHIPPED matrix says on the evidence axis ---------------------

#: Rows whose hosted evidence covers the implementation they describe, in the
#: matrix as committed. It is zero, and zero is the finding (D-2026-44): 21
#: rows cite two runs from a commit 99 behind head, 4 cite runs whose commit
#: was never recorded, and 14 have never had a hosted run at all.
EXPECTED_EVIDENCE_COVERS = 0


def test_the_shipped_matrix_reports_its_evidence_axis_honestly():
    """The number that has to be read next to "37/39 complete".

    "37/39 complete" is a statement about what is BUILT, and it was the only
    number this matrix printed. A reader takes it for a statement about what
    has been CHECKED. Those are different axes and the gap between them is
    total: every row is complete on the first and none is covered on the
    second.

    Pinned here so that improving it is a deliberate act somebody has to
    come and change this constant for, rather than something that drifts in
    either direction unnoticed -- which is exactly how 21 rows came to cite
    runs 33905260267 and 33909571694 long after the code moved away from
    them.
    """
    rows = CM.load()["rows"]

    def _rd(p):
        q = pathlib.Path(p)
        return q.read_bytes() if q.is_file() else None

    def _ls(p):
        q = pathlib.Path(p)
        return (sorted(str(x) for x in q.rglob("*") if x.is_file())
                if q.is_dir() else [])

    states = [CM.evidence_state(r, _rd, _ls) for r in rows]
    covers = [s for s in states if s == CM.EV_COVERS]
    assert len(covers) == EXPECTED_EVIDENCE_COVERS, (
        f"{len(covers)} rows now have hosted evidence covering their "
        f"implementation, not {EXPECTED_EVIDENCE_COVERS}. If a hosted run "
        "has genuinely caught up with the code, say so here in the same "
        "commit that records it")
    assert len(states) == len(rows), "every row must derive a state"
    # And the derivation must be reaching every row, not defaulting.
    assert set(states) <= {CM.EV_COVERS, CM.EV_PREDATES,
                           CM.EV_UNRESOLVABLE, CM.EV_NEVER_RUN}


# --- counts the matrix asserts about artifacts it can be checked against ---

def test_the_spec_count_the_matrix_claims_is_the_count_that_runs():
    """R51 said "34 committed specs run in CI" while 39 ran (D-2026-47).

    Nothing was missing -- all 39 specs exist and all 39 are invoked. The
    number simply stopped tracking its subject as specs were added, in the
    row whose whole job is to say how much mutation coverage there is.

    A count in prose is a claim with no owner. This gives it one.

    WORTH RECORDING ABOUT THE MEASUREMENT ITSELF: the first attempt to check
    this used `tools/mutations/[a-z_]*\\.json` and reported that
    stage10_authority.json was never invoked -- because the character class
    has no digits and could not match its name. A scan that silently drops
    part of its subject reads exactly like a finding about that part. The
    pattern below takes digits, and the two-way comparison means a spec that
    is invoked but absent is caught as well as one present but never run.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    on_disk = {p.name for p in (root / "tools" / "mutations").glob("*.json")}
    invoked = set()
    for wf in (root / ".github" / "workflows").glob("*.yml"):
        invoked |= {m.split("/")[-1] for m in re.findall(
            r"tools/mutations/[A-Za-z0-9_]+\.json",
            wf.read_text(encoding="utf-8"))}

    assert on_disk == invoked, (
        f"specs on disk but never invoked: {sorted(on_disk - invoked)}; "
        f"invoked but absent: {sorted(invoked - on_disk)}")

    row = next(r for r in CM.load()["rows"] if r["id"] == "R51")
    claimed = re.search(r"(\d+) committed specs run in CI", row["evidence"])
    assert claimed, (
        "R51 no longer states how many specs run in CI; that sentence is "
        "the coverage claim this row exists to make")
    assert int(claimed.group(1)) == len(on_disk), (
        f"R51 claims {claimed.group(1)} committed specs run in CI; "
        f"{len(on_disk)} do. Update the row in the commit that adds or "
        "removes a spec, so the change is reviewed rather than absorbed")
