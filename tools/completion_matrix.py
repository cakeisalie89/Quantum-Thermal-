#!/usr/bin/env python3
"""Validate and render the §21-§59 completion matrix.

WHY THIS IS EXECUTABLE

A completion matrix is a self-assessment, and a self-assessment nobody checks
drifts toward optimism one edit at a time. Every claim a row makes that CAN be
checked mechanically IS checked here:

  * every path named in ``implementation`` exists;
  * every path named in ``tests`` exists;
  * every mutation spec named in ``mutation_tests`` exists AND actually
    mutates at least one of the row's implementation paths -- so a row cannot
    borrow another subsystem's mutation coverage;
  * a row claiming a ``production_caller`` names a real file that really
    imports the implementation;
  * classifications above PARTIALLY_IMPLEMENTED require tests;
  * COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT additionally requires
    mutation coverage and an empty ``residual_gaps``;
  * a BLOCKED row must name its missing input and what would unblock it;
  * a residual gap must not claim a subsystem "does not exist" when a module
    of that name is sitting in the tree -- the staleness check, added after a
    dozen rows were found still describing subsystems that had been built
    weeks earlier;
  * a row above PARTIALLY_IMPLEMENTED must list at least one residual gap or
    be COMPLETE, because a row with neither is claiming perfection quietly;
  * a row claiming property_tests, fuzzing or differential coverage must name
    a file that exists and actually contains that kind of testing.

What cannot be checked mechanically is whether a row's prose is honest. That
is what review is for, which is why the matrix is committed rather than
printed.

Usage:
    python3 tools/completion_matrix.py            # validate, print summary
    python3 tools/completion_matrix.py --table    # full table
    python3 tools/completion_matrix.py --open     # only unfinished rows
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "completion_matrix.json"

#: Ordered weakest to strongest. Order is meaningful: `>=` comparisons below
#: rely on it, and a new class must be inserted at its true strength.
CLASSES = (
    "ABSENT",
    "PLACEHOLDER",
    "SKELETAL",
    "PARTIALLY_IMPLEMENTED",
    "LOCALLY_MATURE_BUT_UNINTEGRATED",
    "INTEGRATED_BUT_INCOMPLETELY_VERIFIED",
    "DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS",
    "COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT",
)
BLOCKED = ("EXTERNALLY_BLOCKED", "EPISTEMICALLY_BLOCKED")
COMPLETE = "COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT"

#: Fields every row must carry, from the directive's §62 list.
REQUIRED = (
    "id", "requirement", "classification", "implementation", "callers",
    "production_caller", "persistent_state", "authority_owner", "evidence",
    "provenance", "failure_semantics", "recovery", "retry", "idempotency",
    "cancellation", "concurrency", "security_boundary", "tests",
    "property_tests", "mutation_tests", "fuzzing", "differential",
    "hosted_ci", "hosted_evidence", "residual_gaps", "boundaries", "blocker",
)

#: THE EVIDENCE AXIS, WHICH IS NOT THE IMPLEMENTATION AXIS.
#:
#: ``classification`` says how completely a requirement is BUILT.
#: ``hosted_evidence`` says what a hosted runner has actually CHECKED, and
#: conflating the two is what let 35 of 39 rows read as verified while
#: citing runs that could not have covered them (D-2026-44).
#:
#: The row records only facts about the run: which runs, at which commit,
#: and the digest of this row's implementation files AS THEY WERE when that
#: run went green. The VERDICT is derived here, by recomputing that digest
#: from the working tree. A row cannot assert that its evidence is current,
#: for the same reason a review record cannot assert that its author is a
#: human: a self-declared field is not an authority.
#:
#: Derived states, never stored:
EV_NEVER_RUN = "NEVER_RUN"
EV_COVERS = "COVERS_CURRENT_IMPLEMENTATION"
EV_PREDATES = "PREDATES_CURRENT_IMPLEMENTATION"
#: Runs are cited but the commit they ran on was never written down, so what
#: they covered cannot be recomputed. Distinct from PREDATES on purpose:
#: "this evidence is stale" and "I cannot tell whether this evidence is
#: stale" are different states, and reporting the second as the first
#: claims a measurement that was not made.
EV_UNRESOLVABLE = "COMMIT_NOT_RECORDED"


def implementation_digest(paths, read, listdir) -> str:
    """Digest the row's implementation, however the bytes are fetched.

    ``read(path)`` returns bytes or None when the path is not a file;
    ``listdir(path)`` returns the files beneath it when it is a directory,
    and an empty list otherwise. Both are supplied by the caller, so the
    same function digests a git commit and a working tree -- which is the
    point: one side is what the hosted run tested, the other is what is
    there now.

    ABSENCE IS PART OF THE DIGEST. A row whose implementation file did not
    exist at the cited commit must not hash as though it did, and that case
    is not hypothetical: 33 of the 39 rows name at least one file that
    postdates the run they cite.

    A DIRECTORY IS DIGESTED BY ITS MEMBERSHIP, for the same reason. Rows
    name `tools/mutations`, and a spec ADDED to it since the run is exactly
    the coverage the run did not have.

    Deliberately independent of git history: this has to give the same
    answer in a shallow CI checkout, where there is nothing to diff against.
    """
    h = hashlib.sha256()
    expanded = []
    for p in paths:
        members = listdir(p)
        expanded.extend(members if members else [p])
    for p in sorted(set(expanded)):
        blob = read(p)
        h.update(p.encode("utf-8"))
        h.update(b"\x00ABSENT\x00" if blob is None
                 else b"\x00" + hashlib.sha256(blob).hexdigest().encode())
    return h.hexdigest()


def evidence_state(row, read, listdir) -> str:
    """What a hosted runner has checked about THIS row, as it stands now."""
    ev = row.get("hosted_evidence") or {}
    if not ev.get("runs"):
        return EV_NEVER_RUN
    if not ev.get("commit") or ev.get("commit") == "UNRECORDED" \
            or not ev.get("implementation_sha256"):
        return EV_UNRESOLVABLE
    now = implementation_digest(row.get("implementation") or [],
                                read, listdir)
    return (EV_COVERS if now == ev.get("implementation_sha256")
            else EV_PREDATES)

#: WHY A BOUNDARY IS NOT A GAP, and why this vocabulary is closed.
#:
#: A residual gap is engineering somebody has not done. A boundary is
#: something this repository CANNOT do, and saying so is part of the row
#: rather than an excuse for it -- the classification is literally
#: "complete to current technically defensible LIMIT", so a complete row
#: without a stated limit is a row that has not said what it does not claim.
#:
#: The obvious abuse is to relabel awkward work as a boundary. The defence
#: is that every boundary must name a REASON from this closed set, and the
#: set contains no category an unfinished feature could honestly claim.
#: "no fuzzing of policy records" is not a platform primitive, not an
#: unreachable system, not hardware, not an identity authority, not an
#: epistemic limit of a method, not a design invariant, not a second host
#: and not a language runtime. It is work, so it stays a gap.
BOUNDARY_REASONS = {
    # A kernel or libc facility this interpreter does not expose.
    "platform_primitive_absent",
    # A host, service or artifact this environment cannot reach.
    "external_system_unreachable",
    # Needs physical apparatus that does not exist here.
    "requires_hardware",
    # Needs a credential authority, PKI or human identity provider that
    # this repository deliberately does not contain.
    "requires_external_identity_authority",
    # A limit of the METHOD, not of its implementation: exploration is not
    # exhaustion, an empty diff is not a proof.
    "epistemic",
    # Closing it would violate an invariant this system exists to hold.
    "architectural_by_design",
    # One machine, one filesystem: behaviour that only differs across hosts
    # or on network storage cannot be observed from here.
    "environment_single_host",
    # A property of the language runtime itself.
    "language_runtime",
}

#: Phrases that describe WORK. None of them belongs in a boundary, whatever
#: reason is attached, because each names something an engineer could sit
#: down and do in this repository.
_WORK_PHRASES = (
    "no fuzzing", "not fuzzed", "no mutation coverage", "no mutation matrix",
    "todo", "not yet", "future work", "next session", "should be added",
    "could be added", "needs to be written", "not implemented",
    "unimplemented", "no test for", "is untested",
)

#: Reasons under which ABSENT TESTING can be a genuine limit rather than
#: unfinished work. A test that needs a second host, a piece of apparatus, a
#: reachable external service or a kernel facility this interpreter does not
#: expose cannot be written here at all; one that is merely unwritten can.
#:
#: The other four reasons are excluded on purpose. "It is architecturally
#: intended" explains why a BEHAVIOUR is absent, never why a test for the
#: behaviour that IS there was not written -- and "architectural_by_design"
#: is the reason a boundary reaches for when it wants to sound settled.
_TESTABILITY_REASONS = frozenset({
    "external_system_unreachable",
    "requires_hardware",
    "environment_single_host",
    "platform_primitive_absent",
    "epistemic",
})

#: Prose that describes testing which was not done. Unlike _WORK_PHRASES
#: these are not refused outright, because some of them are true limits: a
#: two-writer test on a network filesystem cannot be written on a host with
#: one filesystem. They are refused when the reason attached does not
#: explain why the test is impossible rather than merely absent.
#:
#: "modelled" is deliberately NOT in the second pattern. "Ingress is not
#: modelled" is a statement about the scope of a model -- there is nothing
#: to test because there is no ingress -- and flagging it taught the matrix
#: to phrase real boundaries evasively, which is the opposite of the point.
_ABSENT_TESTING = (
    r"\bno\b(?:\s+[\w-]+){0,3}\s+"
    r"(?:tests?|testing|coverage|fuzzing|harness|specs?)\b",
    r"\bnot\s+(?:tested|covered|exercised|verified|checked|explored|"
    r"fuzzed|mutated)\b",
    r"\b(?:missing|absent|lacking)\s+(?:[\w-]+\s+){0,2}"
    r"(?:tests?|coverage|checks?)\b",
)


def load() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _fuzz_targets() -> frozenset:
    """Target names the fuzz harness registers, read from its source.

    Parsed rather than imported. Importing would run the harness's module
    body and drag its dependencies into a validator that has no business
    needing them; the names are string literals in one dict and reading them
    is exact enough for the question being asked.

    Returns an EMPTY set only when the file is missing, and the caller must
    treat that as "cannot tell" -- a staleness check that silently passes
    because it read nothing is the vacuous-success defect this repository
    already carries once.
    """
    src = ROOT / "tools" / "fuzz_substrate.py"
    if not src.exists():
        return frozenset()
    text = src.read_text(encoding="utf-8")
    block = text.partition("    return {")[2].partition("\n    }")[0]
    return frozenset(re.findall(r'^\s{8}"([a-z0-9_]+)":', block, re.M))


#: Words in a fuzz target's name that are too generic to imply a gap is
#: stale. "policy" in the gap and a "policy" target is a real collision;
#: "url" inside "curl" is not.
def _fuzz_gap_names(gap_low: str, target: str) -> bool:
    """Does ``gap_low`` claim the absence of fuzzing for ``target``?

    Word-boundary matching on the target's own name parts. A target named
    ``policy_decision`` is named by a gap mentioning "policy" only when the
    gap also carries a negation -- checked by the caller, which only reaches
    here for gaps containing "fuzz".
    """
    if not any(neg in gap_low for neg in
               ("no fuzz", "not fuzz", "never fuzz", "without fuzz")):
        return False
    # ANY part, not all of them. "no fuzzing of the index document parser"
    # names the ``rag_index`` target without saying "rag", and requiring both
    # halves let exactly that claim stay stale through a first attempt at
    # this check. A part is at least three characters so a target named
    # ``job`` still counts while nothing matches on a fragment.
    parts = [x for x in target.split("_") if len(x) >= 3]
    return any(re.search(rf"\b{re.escape(x)}", gap_low) for x in parts)


def _rank(cls: str) -> int:
    return CLASSES.index(cls) if cls in CLASSES else -1


def validate(doc: dict) -> list:
    problems: list = []
    rows = doc.get("rows")
    if not isinstance(rows, list) or not rows:
        return ["matrix has no rows"]

    seen = set()
    for row in rows:
        rid = row.get("id", "<no id>")
        for field in REQUIRED:
            if field not in row:
                problems.append(f"{rid}: missing field {field!r}")
        if rid in seen:
            problems.append(f"{rid}: duplicate row id")
        seen.add(rid)

        cls = row.get("classification")
        if cls not in CLASSES and cls not in BLOCKED:
            problems.append(f"{rid}: unknown classification {cls!r}")
            continue

        for field in ("implementation", "tests"):
            for rel in row.get(field, []):
                if not (ROOT / rel).exists():
                    problems.append(
                        f"{rid}: {field} path does not exist: {rel}")

        # A row may not borrow another subsystem's mutation coverage.
        impl = set(row.get("implementation", []))
        for spec_rel in row.get("mutation_tests", []):
            spec_path = ROOT / spec_rel
            if not spec_path.exists():
                problems.append(f"{rid}: mutation spec missing: {spec_rel}")
                continue
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                problems.append(f"{rid}: mutation spec unparseable: {exc}")
                continue
            mutated = {m.get("path") for m in spec.get("mutations", [])}
            if impl and not (mutated & impl):
                problems.append(
                    f"{rid}: cites {spec_rel} but that spec mutates "
                    f"{sorted(mutated)}, none of this row's implementation "
                    f"{sorted(impl)}")

        pc = row.get("production_caller")
        if pc:
            if not (ROOT / pc).exists():
                problems.append(f"{rid}: production_caller missing: {pc}")
            elif pc.startswith(("tests/", "test_")) or "/tests/" in pc:
                # THE defect this project has hit twice, in two subsystems:
                # a correct, thoroughly tested function whose only callers
                # were its own tests, and a result field populated by
                # nothing. A row whose production caller IS a test is
                # claiming production integration it does not have, and a
                # test file is the easiest thing in the tree to point at.
                problems.append(
                    f"{rid}: production_caller {pc} is a test. A defence "
                    "nothing but its own tests invokes is indistinguishable "
                    "from no defence; name the real caller or say the row "
                    "has none")
            elif impl:
                text = (ROOT / pc).read_text(encoding="utf-8",
                                             errors="replace")
                # A QUALIFIED reference, not a bare stem. Matching "memory"
                # or "tools" against a whole file is true of almost any
                # document, so the old check passed a README as a production
                # caller -- a guard that cannot fail is not a guard. Its own
                # test caught this.
                mods = {Path(q).stem for q in impl}
                forms = set()
                for m in mods:
                    # "import <stem>" covers every import spelling that
                    # actually pulls the module in, including
                    # "from qta_multiphysics.stack import rag_index as R",
                    # while still being false of ordinary prose.
                    forms.update({f"qta_agent.{m}", f"qta_agent/{m}",
                                  f"import {m}", f".{m} import"})
                forms.update(impl)
                if not any(f in text for f in forms):
                    problems.append(
                        f"{rid}: production_caller {pc} does not reference "
                        f"any of {sorted(mods)} in a form that would import "
                        "or run it")

        # STALENESS. A gap describing a subsystem as absent, when a module
        # of that name is in the tree, is documentation that has stopped
        # being true -- and a matrix nobody re-reads drifts exactly this way.
        # Twelve rows were found in that state at once, still saying "the
        # scheduler does not exist yet" months after it did.
        modules = {q.stem for q in (ROOT / "qta_agent").glob("*.py")}
        for gap in row.get("residual_gaps", []):
            low = gap.lower()
            for phrase in ("does not exist yet", "do not exist yet",
                           "does not exist", "no scheduler",
                           "no policy engine", "is skeletal",
                           "has no production caller"):
                if phrase not in low:
                    continue
                named = {m for m in modules
                         if len(m) > 4 and m in low}
                if named:
                    problems.append(
                        f"{rid}: a residual gap says {phrase!r} while "
                        f"qta_agent/{sorted(named)[0]}.py exists; refresh the "
                        "row rather than leaving documentation that has "
                        "stopped being true")

        # STALENESS, SECOND SPECIES. Three rows were found carrying "no
        # fuzzing of X" while tools/fuzz_substrate.py had a target for X --
        # a policy-record target, a URL target and an index target, all
        # already written. The first staleness check only knew about modules
        # that had appeared; this one knows about coverage that had.
        #
        # Same failure mode, and worse in one way: a stale "no fuzzing" claim
        # keeps a row out of COMPLETE for work that was already done, so the
        # matrix understates the system while looking rigorous.
        for gap in row.get("residual_gaps", []):
            low = gap.lower()
            if "fuzz" not in low:
                continue
            hits = {t for t in _fuzz_targets() if _fuzz_gap_names(low, t)}
            if hits:
                problems.append(
                    f"{rid}: a residual gap says {gap[:60]!r} while "
                    f"tools/fuzz_substrate.py registers target(s) "
                    f"{sorted(hits)}. Re-read the harness before writing a "
                    "coverage gap: an understated row is drift too")

        # A row above PARTIALLY_IMPLEMENTED with no gaps and no COMPLETE
        # claim is claiming perfection without saying so.
        if (cls not in BLOCKED and cls != COMPLETE
                and _rank(cls) > _rank("PARTIALLY_IMPLEMENTED")
                and not row.get("residual_gaps")):
            problems.append(
                f"{rid}: {cls} with no residual gaps listed. Either the row "
                f"is {COMPLETE}, or something is missing and should be said")

        # Claimed coverage must be the kind of coverage it claims to be.
        #
        # USAGE, not a mention. This matched the bare word "hypothesis"
        # anywhere in the file, so a docstring sentence like "the rule
        # Hypothesis found" satisfied a property-testing claim -- and did,
        # the moment one was written into a suite that has no property tests
        # at all. A marker a comment can supply is not evidence of coverage.
        for field, markers, what in (
            ("property_tests",
             ("@given", "from hypothesis import", "import hypothesis",
              "rulebasedstatemachine"),
             "property-based testing"),
            ("fuzzing", ("fuzz",), "fuzzing"),
        ):
            value = row.get(field)
            names = value if isinstance(value, list) else (
                [value] if value and value.lower() != "none" else [])
            for name in names:
                rel = name.split(" ", 1)[0]
                path = ROOT / rel
                if not path.is_file():
                    continue          # prose, not a path; nothing to check
                body = path.read_text(encoding="utf-8",
                                      errors="replace").lower()
                if not any(m in body for m in markers):
                    problems.append(
                        f"{rid}: {field} names {rel}, which contains no "
                        f"{what}")

        # A hosted-CI claim must cite a RUN, not a mood. "green", "passing"
        # and "should be fine" are all things this field has been tempted to
        # say; a run id is a thing somebody can open.
        # THE RULE THIS REPLACES, AND WHY IT HAD TO GO.
        #
        # It was: hosted_ci must contain a run id, "not a mood". The regex
        # `\b\d{8,}\b` then accepted "pending: added after run 33939090740"
        # -- a sentence whose MEANING is that no hosted run covers this row,
        # passing a check about citing runs because it names one while
        # denying it. Fourteen rows were classified COMPLETE on that string
        # (D-2026-44).
        #
        # The evidence now lives in `hosted_evidence`, which is checkable,
        # and the prose is held to the one thing prose can get wrong here:
        # naming a run when there is none.
        ev = row.get("hosted_evidence")
        hosted = row.get("hosted_ci") or ""
        if not isinstance(ev, dict):
            problems.append(f"{rid}: hosted_evidence must be an object")
        else:
            runs = ev.get("runs")
            if not isinstance(runs, list):
                problems.append(f"{rid}: hosted_evidence.runs must be a list")
                runs = []
            for r_id in runs:
                if not re.fullmatch(r"\d{8,}", str(r_id)):
                    problems.append(
                        f"{rid}: hosted_evidence.runs has {r_id!r}, which is "
                        "not a run id somebody can open")
            if runs:
                if not ev.get("commit"):
                    problems.append(
                        f"{rid}: cites runs with no commit. A run that is "
                        "not tied to a commit cannot be checked against the "
                        "code it is supposed to have covered")
                dig = ev.get("implementation_sha256")
                if ev.get("commit") not in (None, "UNRECORDED") and (
                        not isinstance(dig, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", dig)):
                    problems.append(
                        f"{rid}: hosted_evidence names a commit but no "
                        "well-formed implementation_sha256, so nothing can "
                        "be recomputed from it")
            elif re.search(r"\b\d{8,}\b", hosted):
                problems.append(
                    f"{rid}: hosted_evidence records NO run, and hosted_ci "
                    f"still names one: {hosted[:70]!r}. That is the exact "
                    "shape the old rule accepted -- a run id present in a "
                    "sentence saying no run applies")

        # BOUNDARIES: what the row does not claim, and why it cannot.
        bounds = row.get("boundaries")
        if bounds is None:
            bounds = []
        if not isinstance(bounds, list):
            problems.append(f"{rid}: boundaries must be a list")
            bounds = []
        for i, b in enumerate(bounds):
            where = f"{rid}: boundary {i + 1}"
            if not isinstance(b, dict):
                problems.append(
                    f"{where} must be an object with 'limit' and 'reason'; a "
                    "bare sentence can say anything, and the reason is the "
                    "part that stops a gap wearing a boundary's clothes")
                continue
            limit = b.get("limit")
            if not isinstance(limit, str) or len(limit.strip()) < 40:
                problems.append(
                    f"{where}: 'limit' must be a substantive sentence saying "
                    "what is NOT claimed")
            reason = b.get("reason")
            if reason not in BOUNDARY_REASONS:
                problems.append(
                    f"{where}: reason {reason!r} is not one of "
                    f"{sorted(BOUNDARY_REASONS)}. A boundary has to name why "
                    "no engineering here can close it; if none of these fits, "
                    "it is a residual gap and belongs in residual_gaps")
            # EVIDENCE THAT ENGINEERING IS EXHAUSTED, as a required field.
            #
            # 'limit' says what is not claimed; on its own that is a
            # sentence anybody can write about anything. 'detail' is where
            # the row has to say why nobody in this repository can close it,
            # and a boundary without one is an assertion with no argument.
            detail = b.get("detail")
            if not isinstance(detail, str) or len(detail.strip()) < 80:
                problems.append(
                    f"{where}: 'detail' must say why no engineering in this "
                    "repository closes this limit. A boundary is a claim "
                    "that work is exhausted, and a claim with no argument "
                    "behind it is how unfinished work gets reclassified")
                detail = detail if isinstance(detail, str) else ""
            elif detail.strip() in limit or limit.strip() in detail:
                problems.append(
                    f"{where}: 'detail' restates 'limit'. Saying the same "
                    "thing twice is not evidence that engineering is "
                    "exhausted")

            low = f"{limit} {detail}".lower()
            for phrase in _WORK_PHRASES:
                if phrase in low:
                    problems.append(
                        f"{where}: says {phrase!r}, which describes work "
                        "somebody could do in this repository. That is a "
                        "residual gap, not a limit of what is possible")
                    break
            else:
                # ABSENT TESTING is the loophole this catches. "No
                # cross-process test of X" is a true boundary when there is
                # one host and a residual gap when there are two, and the
                # difference is exactly what 'reason' is supposed to name.
                # A reason that explains an absent BEHAVIOUR cannot also
                # explain an absent TEST of behaviour that is present.
                for pat in _ABSENT_TESTING:
                    hit = re.search(pat, low)
                    if hit and reason not in _TESTABILITY_REASONS:
                        problems.append(
                            f"{where}: says {hit.group(0)!r} under reason "
                            f"{reason!r}, which does not explain why the "
                            "test cannot be written here -- only why the "
                            "behaviour is absent. Either the test is "
                            f"writable, and this is a residual gap, or the "
                            f"reason is one of "
                            f"{sorted(_TESTABILITY_REASONS)}")
                        break

        if cls in BLOCKED:
            if not row.get("blocker"):
                problems.append(f"{rid}: {cls} requires a blocker")
            elif not row["blocker"].get("unblocked_by"):
                problems.append(
                    f"{rid}: blocker must say what would unblock it")
        else:
            weak = _rank("PARTIALLY_IMPLEMENTED")
            if _rank(cls) > weak and not row["tests"]:
                problems.append(f"{rid}: {cls} claimed with no tests")
            if cls == COMPLETE:
                if not row["mutation_tests"]:
                    problems.append(
                        f"{rid}: {COMPLETE} claimed with no mutation coverage")
                if row["residual_gaps"]:
                    problems.append(
                        f"{rid}: {COMPLETE} claimed with residual "
                        f"gaps listed: "
                        f"{row['residual_gaps']}")
                if not bounds:
                    # The classification says "to current technically
                    # defensible LIMIT". A complete row that names no limit
                    # has not finished the sentence, and silence is the
                    # easiest way to overstate a system.
                    problems.append(
                        f"{rid}: {COMPLETE} claimed with no boundaries "
                        "stated. Every row here is complete TO A LIMIT; say "
                        "what this one does not claim, or the classification "
                        "is claiming more than the row can support")
    return problems


def summarise(doc: dict) -> dict:
    counts: dict = {}
    for row in doc["rows"]:
        cls = row["classification"]
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", action="store_true", help="print every row")
    ap.add_argument("--open", dest="open_only", action="store_true",
                    help="print only rows below the completion bar")
    args = ap.parse_args()

    doc = load()
    problems = validate(doc)

    rows = doc["rows"]
    if args.table or args.open_only:
        width = max(len(r["id"]) for r in rows)
        for row in rows:
            cls = row["classification"]
            if args.open_only and (cls == COMPLETE or cls in BLOCKED):
                continue
            print(f"{row['id']:{width}s}  {cls:48s} {row['requirement'][:60]}")
        print()

    counts = summarise(doc)
    total = len(rows)
    for cls in CLASSES + BLOCKED:
        if counts.get(cls):
            print(f"  {counts[cls]:3d}  {cls}")
    done = counts.get(COMPLETE, 0)
    blocked = sum(counts.get(c, 0) for c in BLOCKED)
    # TWO AXES, PRINTED TOGETHER, BECAUSE ONE OF THEM ALONE MISLEADS.
    # "37/39 complete" is a statement about what is BUILT. It was the only
    # number this ever printed, and a reader took it for a statement about
    # what had been CHECKED -- while zero rows had hosted evidence covering
    # their current implementation (D-2026-44).
    from pathlib import Path as _P
    def _rd(p):
        q = _P(p)
        return q.read_bytes() if q.is_file() else None
    def _ls(p):
        q = _P(p)
        return (sorted(str(x) for x in q.rglob("*") if x.is_file())
                if q.is_dir() else [])
    ev_counts = {}
    for r in rows:
        st = evidence_state(r, _rd, _ls)
        ev_counts[st] = ev_counts.get(st, 0) + 1
    print("\nhosted evidence, derived by recomputing each row's "
          "implementation digest:")
    for state in (EV_COVERS, EV_PREDATES, EV_UNRESOLVABLE, EV_NEVER_RUN):
        print(f"  {ev_counts.get(state, 0):3d}  {state}")
    print(f"  {ev_counts.get(EV_COVERS, 0)}/{total} rows have hosted "
          "evidence that covers the code they describe")

    print(f"\n{done}/{total} complete, {blocked} blocked, "
          f"{total - done - blocked} open")

    if problems:
        print(f"\nMATRIX INVALID ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("matrix self-consistent; every mechanically checkable claim holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
