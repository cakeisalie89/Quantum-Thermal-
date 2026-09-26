"""The claims boundary is enforced, and the coverage is measured.

CLAIMS_BOUNDARY.md calls itself the strongest statement of position in the
package. Measured for the first time, 19 of its 24 **Forbidden:** entries
would have passed package_consistency_check.py verbatim -- the entire
shielding list among them, including "Mode B processing and Mode D sensing
occur simultaneously", which is the sentence the mode-exclusive architecture
exists to deny. The five that were caught were caught by patterns written to
catch stale RTB/JT module counts.

What this establishes is bounded and says so: whether the EXACT sentence would
be refused in a live document. A paraphrase is not caught and no string rule
catches one.

MODEL-ONLY / FORECAST-ONLY. No scientific value is asserted here.
"""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import claims_enforcement as ce  # noqa: E402

CHECKER_SRC = open(os.path.join(ROOT, "package_consistency_check.py"),
                   encoding="utf-8").read()


def _bullets():
    return ce.forbidden_bullets(
        open(ce.CLAIMS, encoding="utf-8").read())


def _enforcement():
    return ce.enforcement(CHECKER_SRC)


def _negations():
    tree = ast.parse(CHECKER_SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign) and \
                getattr(node.targets[0], "id", None) == "CLAIM_NEGATIONS":
            return tuple(e.value for e in node.value.elts)
    raise AssertionError("CLAIM_NEGATIONS is gone from the checker")


def test_there_are_forbidden_claims_to_reconcile():
    bullets = _bullets()
    assert len(bullets) >= 20, f"only {len(bullets)} bullets parsed"
    # The marquee one, pinned by text: if it is ever dropped from the claims
    # file that should be a deliberate act with a test failure attached.
    assert "Mode B processing and Mode D sensing occur simultaneously." in bullets


def test_every_forbidden_claim_would_be_refused_verbatim():
    pats, subs = _enforcement()
    covered, uncovered, problems = ce.reconcile(_bullets(), pats, subs)
    assert not uncovered, f"unenforced claims: {uncovered}"
    assert not problems, problems
    assert len(covered) == len(_bullets())


def test_a_reconciliation_over_nothing_is_refused():
    pats, subs = _enforcement()
    with pytest.raises(ce.ScopeError):
        ce.reconcile([], pats, subs)
    with pytest.raises(ce.ScopeError):
        ce.reconcile(_bullets(), [], subs)
    # The control: the real inputs do not refuse.
    ce.reconcile(_bullets(), pats, subs)


def test_a_new_forbidden_claim_arrives_uncovered():
    # The property that makes this useful going forward: adding a sentence to
    # the claims file does not silently inherit enforcement.
    pats, subs = _enforcement()
    invented = "QTA has demonstrated room-temperature spin coherence."
    covered, uncovered, problems = ce.reconcile(
        _bullets() + [invented], pats, subs)
    assert uncovered == [invented]
    assert any(invented in p for p in problems)


def test_naming_a_claim_is_not_enforcing_it():
    # A pattern that names a bullet and does not match it must be reported.
    # Without this the table could claim full coverage with regexes that
    # match nothing at all.
    bullets = _bullets()
    liar = [(r"this text appears in no claim", bullets[0],
             "FORBIDDEN_CLAIM_PATTERNS")]
    pats, subs = _enforcement()
    _, _, problems = ce.reconcile(bullets, pats + liar, subs)
    assert any("does not" in p and "match it" in p for p in problems), problems


def test_a_pattern_may_not_describe_a_claims_file_that_has_moved_on():
    pats, subs = _enforcement()
    ghost = [(r"Mode B processing and Mode D sensing occur simultaneously",
              "A claim nobody forbids any more.",
              "FORBIDDEN_CLAIM_PATTERNS")]
    _, _, problems = ce.reconcile(_bullets(), pats + ghost, subs)
    assert any("not a bullet" in p for p in problems), problems


@pytest.mark.parametrize("sentence", [
    "Mode B processing and Mode D sensing occur simultaneously.",
    "QTA has validated radiation shielding.",
    "The cryopanels solve Mode B to Mode D contamination.",
    "Residual methane at Mode D entry is zero.",
    "The Mode-C purge removes all methane.",
    "QTA has installed RTB/JT cooling.",
])
def test_the_checker_would_refuse_this_sentence_in_a_document(sentence):
    pats, _ = _enforcement()
    claim_pats = [(rx, lab) for rx, lab, src in pats
                  if src == "FORBIDDEN_CLAIM_PATTERNS"]
    assert any(re.search(rx, sentence) for rx, _ in claim_pats), sentence
    # ... and the negation of the same sentence is not a violation, so the
    # rule refuses the claim rather than the topic.
    denied = "This package does not say: " + sentence
    assert any(neg in denied.lower() for neg in _negations())


def test_the_two_reading_rules_are_outside_the_enforced_list():
    # They are rules about how to read a number, not sentences anyone would
    # write, and a list that must be enforced entry by entry cannot carry
    # entries nothing can enforce without misreporting its own coverage.
    text = open(ce.CLAIMS, encoding="utf-8").read()
    assert "rules of reading rather than sentences" in text
    for rule in ("does not mean the species\nis absent",
                 "without the resolution\n  class stated beside it"):
        flat = re.sub(r"\s+", " ", rule)
        assert flat in re.sub(r"\s+", " ", text), flat
    assert flat not in [re.sub(r"\s+", " ", b) for b in _bullets()]
