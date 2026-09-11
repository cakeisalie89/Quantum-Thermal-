"""The identity inventory has to describe the code that exists.

WHY THIS FILE EXISTS

The sibling sweep meant to find every actor-bearing payload field missed two
of them -- the escalation raiser and the message sender -- in the same file
and the same defect class it was written for. It missed them because a sweep
that enumerates by READING stops where attention stops, and nothing existed
that could be checked for completeness independently of somebody having
looked.

These tests are that something.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.identity_inventory import (  # noqa: E402
    INVENTORY, ROLES, durable_actions, problems, reconstructed_actions,
)


def test_the_inventory_agrees_with_the_code():
    """The whole check, in one assertion, with the reasons printed."""
    found = problems()
    assert not found, "identity inventory is out of step:\n  - " + \
        "\n  - ".join(found)


def test_every_durable_action_is_classified():
    """A new ACT_* constant fails here until somebody says what it carries.

    This is the guard the premature closure needed and did not have.
    """
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    classified = {e["action"] for e in doc["actions"]}
    missing = sorted(set(durable_actions()) - classified)
    assert not missing, (
        f"{len(missing)} durable action(s) carry no identity classification: "
        f"{missing}")


def test_the_inventory_does_not_describe_actions_that_are_gone():
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    stale = sorted({e["action"] for e in doc["actions"]}
                   - set(durable_actions()))
    assert not stale, f"the inventory still describes {stale}"


def test_every_ACTOR_field_names_a_test_that_exists():
    """An ACTOR field must be bound, and the binding must be defended.

    Five entries in the first draft named tests that did not exist -- names
    that read plausibly and were guessed rather than checked. The guard
    caught them within a minute of existing, which is the argument for it.
    """
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    actors = [(e["action"], f) for e in doc["actions"]
              for f in e["fields"] if f["role"] == "ACTOR"]
    assert actors, "no ACTOR fields recorded at all; the table is vacuous"
    for action, f in actors:
        for key in ("write_path", "replay", "regression_test"):
            assert f.get(key), f"{action}.{f['name']} records no {key}"


def test_independent_reader_coverage_is_MEASURED_not_claimed():
    """"The second reader covers every subsystem" was a claim. This measures.

    It is not true, and the number is recorded rather than rounded up: the
    reader reconstructs 30 of 37 durable actions. The seven that are not are
    named in the inventory, each classified as not authority-changing with
    its reason. Saying so is the alternative to a step title that overstates
    it -- and the step title itself is checked below, so the label cannot
    drift away from the measurement either.
    """
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    recorded = {e["action"] for e in doc["actions"]
                if e["independent_reader"] == "YES"}
    assert recorded == reconstructed_actions(), (
        "the recorded coverage disagrees with what reconstruct.py "
        "dispatches on")
    assert recorded != set(durable_actions()), (
        "coverage is now total -- update the inventory note and the CI step "
        "title, which both say it is not")


def test_every_role_used_is_one_the_schema_defines():
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    used = {f["role"] for e in doc["actions"] for f in e["fields"]}
    assert used <= ROLES, f"undefined role(s): {sorted(used - ROLES)}"
    assert "ACTOR" in used and "SUBJECT" in used, (
        "a table with no ACTOR or no SUBJECT rows is not distinguishing "
        "anything")


def test_the_checker_actually_fails_on_a_broken_inventory(tmp_path,
                                                          monkeypatch):
    """Anti-vacuity: a checker that always passes proves nothing.

    Drop one action from the table and the completeness check must notice.
    """
    import tools.identity_inventory as II

    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    doc["actions"] = doc["actions"][1:]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(II, "INVENTORY", broken)
    found = II.problems()
    assert any("not classified" in p for p in found), found


# ---------------------------------------------------------------------------
# D-2026-28 (P0-R13): the coverage number measured string presence.
#
#     return {a for a in durable_actions() if f'"{a}"' in src}
#
# An action counted as independently reconstructed when its name appeared
# ANYWHERE in reconstruct.py -- in the module docstring that lists what the
# reader does not cover, in an anomaly message, in a comment explaining why
# something is deliberately left alone. The answer it gave was right. It was
# right by luck, and nothing would have said so when it stopped being.
#
# These tests are about the MEASUREMENT, so they run it against planted
# sources: a test that only ever sees the real file, where the two
# definitions happen to agree, cannot tell them apart.
#
# The planted source is passed IN rather than written to a temporary package,
# because pointing PKG at a temporary directory also empties
# durable_actions() -- and a classifier asked to classify nothing agrees with
# everything. The first version of these tests did exactly that and failed
# with a KeyError, which is the honest outcome: it was measuring an empty
# universe.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

import tools.identity_inventory as II  # noqa: E402
from tools.identity_inventory import (  # noqa: E402
    ABSENT, DISPATCHED, MENTIONED, mentioned_but_not_dispatched,
    reconstruction_coverage,
)

#: A real durable action the second reader does NOT dispatch on, so every
#: planted case below starts from a known negative rather than from one that
#: would have counted anyway. It was ``agent.claim`` until P0-R14 gave that
#: one a reader, at which point every planted case quietly stopped proving
#: anything -- and ``test_the_planted_action_is_uncovered_in_the_real_reader``
#: is what said so.
UNCOVERED = "agent.message"

#: What the measurement this replaced would have said, reproduced here so
#: the difference between the two is exercised rather than described.
def _old_measurement(source: str, action: str) -> bool:
    return f'"{action}"' in source


def test_the_planted_action_is_uncovered_in_the_real_reader():
    """Anti-vacuity for every planted case below.

    If ``agent.claim`` were already dispatched on, planting it would prove
    nothing: the measurement would answer DISPATCHED either way.
    """
    assert reconstruction_coverage()[UNCOVERED] != DISPATCHED


def test_an_action_named_only_in_a_comment_is_not_coverage():
    """The negative case, and the one the old measurement got wrong."""
    source = f'# nothing here handles "{UNCOVERED}", and that is deliberate\n'
    assert reconstruction_coverage(source)[UNCOVERED] == MENTIONED
    assert UNCOVERED not in reconstructed_actions(source)
    # The old rule counted it AS COVERAGE. Asserted, so this test fails if
    # somebody "simplifies" the measurement back to a substring search.
    assert _old_measurement(source, UNCOVERED)


def test_an_action_named_only_in_a_docstring_is_not_coverage():
    source = f'"""This reader does not cover "{UNCOVERED}" at all."""\n'
    assert reconstruction_coverage(source)[UNCOVERED] == MENTIONED
    assert UNCOVERED not in reconstructed_actions(source)
    assert _old_measurement(source, UNCOVERED)


def test_an_action_named_only_in_a_message_is_reported_as_mentioned():
    """Present, branched on by nothing: its own category, not either answer.

    This is the case somebody could read and reasonably believe was covered,
    so it is named rather than quietly counted either way.
    """
    source = (
        "def note(out, ev):\n"
        f'    out.append(f"seq {{ev.seq}}: {UNCOVERED} is not replayed")\n')
    assert reconstruction_coverage(source)[UNCOVERED] == MENTIONED
    assert UNCOVERED in mentioned_but_not_dispatched(source)


def test_an_action_compared_against_is_coverage():
    """Anti-vacuity: a classifier answering ABSENT to everything would pass
    every negative test above and measure nothing at all."""
    source = (
        "def replay(ev):\n"
        f'    if ev.action == "{UNCOVERED}":\n'
        "        return 1\n")
    assert reconstruction_coverage(source)[UNCOVERED] == DISPATCHED


def test_an_action_in_a_membership_vocabulary_is_coverage():
    """How ``owned`` and ``_AUTHORITY_ACTIONS`` are actually consulted."""
    source = (
        f'OWNED = {{"{UNCOVERED}", "task.create"}}\n'
        "def replay(ev):\n"
        "    if ev.action in OWNED:\n"
        "        return 1\n")
    assert reconstruction_coverage(source)[UNCOVERED] == DISPATCHED


def test_an_action_dispatched_on_in_single_quotes_is_coverage():
    """The old rule's OTHER error: it could understate coverage too.

    ``f'"{a}"' in src`` matched only the double-quoted spelling, so a
    handler written with single quotes was invisible to it. Reading the
    parse tree makes the quote style stop being a fact about coverage.
    """
    source = (
        "def replay(ev):\n"
        f"    if ev.action == '{UNCOVERED}':\n"
        "        return 1\n")
    assert reconstruction_coverage(source)[UNCOVERED] == DISPATCHED
    # ...and the old rule would have said this file handles nothing.
    assert not _old_measurement(source, UNCOVERED)


def test_a_commented_out_handler_is_not_coverage():
    """A handler somebody disabled is a handler that is not there.

    Free, rather than by a rule somebody has to remember: comments are not
    in the parse tree at all, so nothing has to recognise this shape.
    """
    source = (
        "def replay(ev):\n"
        f'    # if ev.action == "{UNCOVERED}":\n'
        "    #     return 1\n"
        "    return 0\n")
    assert reconstruction_coverage(source)[UNCOVERED] == MENTIONED
    assert UNCOVERED not in reconstructed_actions(source)
    assert _old_measurement(source, UNCOVERED)


def test_an_action_the_reader_never_names_is_absent():
    """The third category, so MENTIONED is a real distinction and not a
    synonym for "not dispatched"."""
    source = "def replay(ev):\n    return 0\n"
    assert reconstruction_coverage(source)[UNCOVERED] == ABSENT


def test_a_dict_dispatch_table_is_coverage():
    """The other shape a dispatch can take."""
    source = (
        f'HANDLERS = {{"{UNCOVERED}": None}}\n'
        "def replay(ev):\n"
        "    return HANDLERS.get(ev.action)\n")
    assert reconstruction_coverage(source)[UNCOVERED] == DISPATCHED


def test_every_recorded_YES_is_dispatched_not_merely_mentioned():
    """The inventory's claim, against the stricter measurement.

    ``test_independent_reader_coverage_is_MEASURED_not_claimed`` compares the
    two sets. This says WHICH measurement it compares against, so weakening
    the measurement back to string presence fails here even while that test
    goes on passing.
    """
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    recorded = {e["action"] for e in doc["actions"]
                if e["independent_reader"] == "YES"}
    coverage = reconstruction_coverage()
    not_dispatched = sorted(a for a in recorded
                            if coverage.get(a) != DISPATCHED)
    assert not not_dispatched, (
        "recorded as independently reconstructed but branched on by nothing "
        f"in reconstruct.py: {not_dispatched}")


def test_nothing_is_currently_mentioned_without_being_dispatched():
    """True today, and a finding rather than a surprise if it stops being.

    An action named in the second reader and handled by nothing there is
    exactly what the old measurement counted as coverage. Asserting it is
    empty means a future edit that introduces one has to come past this line.
    """
    stragglers = sorted(mentioned_but_not_dispatched())
    assert not stragglers, (
        f"named in reconstruct.py and handled by nothing there: {stragglers}. "
        "Either dispatch on it or stop naming it; that ambiguity is what "
        "P0-R13 was about")


@pytest.mark.parametrize("kind", [DISPATCHED, MENTIONED, ABSENT])
def test_the_three_categories_are_distinct(kind):
    """A classifier whose categories collapsed would pass much of the above."""
    assert len({DISPATCHED, MENTIONED, ABSENT}) == 3
    assert isinstance(kind, str) and kind


# --- the checker's own refusals, each exercised -----------------------------
#
# ``test_the_checker_actually_fails_on_a_broken_inventory`` covered one branch
# of ``problems()``. The rest were unexercised, which for a file whose whole
# job is to refuse is the same defect in miniature: a checker nobody has seen
# refuse anything is a checker nobody knows refuses anything.

def _with_inventory(tmp_path, monkeypatch, mutate):
    """Run ``problems()`` against an inventory ``mutate`` has damaged."""
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    mutate(doc)
    broken = tmp_path / "inventory.json"
    broken.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(II, "INVENTORY", broken)
    return II.problems()


def test_the_unmodified_inventory_produces_no_problems(tmp_path, monkeypatch):
    """Anti-vacuity for every case below: the harness itself is clean.

    Without this, a ``_with_inventory`` that damaged the file on the way in
    would make all four of the following pass for the wrong reason.
    """
    assert _with_inventory(tmp_path, monkeypatch, lambda doc: None) == []


def test_the_checker_refuses_overstated_reader_coverage(tmp_path, monkeypatch):
    """Recording an uncovered action as independently reconstructed.

    This is the claim P0-R13 was about, made directly rather than by a
    measurement that could be fooled.
    """
    def overstate(doc):
        for e in doc["actions"]:
            if e["action"] == UNCOVERED:
                e["independent_reader"] = "YES"
    found = _with_inventory(tmp_path, monkeypatch, overstate)
    assert any("claimed as independently reconstructed" in p for p in found), \
        found


def test_the_checker_refuses_an_ACTOR_field_naming_a_test_that_is_not_there(
        tmp_path, monkeypatch):
    """Five entries in the first draft named tests that did not exist."""
    def rename(doc):
        for e in doc["actions"]:
            for f in e["fields"]:
                if f["role"] == "ACTOR":
                    f["regression_test"] = "test_a_name_that_reads_plausibly"
                    return
    found = _with_inventory(tmp_path, monkeypatch, rename)
    assert any("which does not exist" in p for p in found), found


def test_the_checker_refuses_a_role_the_schema_does_not_define(tmp_path,
                                                               monkeypatch):
    def invent(doc):
        doc["actions"][0]["fields"][0]["role"] = "MADE_UP"
    found = _with_inventory(tmp_path, monkeypatch, invent)
    assert any("is not one of" in p for p in found), found


def test_the_checker_refuses_an_action_recorded_in_the_wrong_module(
        tmp_path, monkeypatch):
    """The table must describe where the action actually is."""
    def move(doc):
        doc["actions"][0]["module"] = "somewhere_else.py"
    found = _with_inventory(tmp_path, monkeypatch, move)
    assert any("defined in" in p for p in found), found


def test_the_checker_refuses_an_entry_for_an_action_that_is_gone(
        tmp_path, monkeypatch):
    def invent(doc):
        doc["actions"].append({"action": "task.teleport", "module": "x.py",
                               "independent_reader": "NO", "fields": []})
    found = _with_inventory(tmp_path, monkeypatch, invent)
    assert any("do not exist" in p for p in found), found


@pytest.mark.parametrize("key", ["write_path", "replay", "regression_test"])
def test_the_checker_refuses_an_ACTOR_field_missing_any_of_its_bindings(
        tmp_path, monkeypatch, key):
    """Each of the three, separately.

    ``test_every_ACTOR_field_names_a_test_that_exists`` asserts on the
    document directly, so the branch in ``problems()`` that says the same
    thing was never run -- and the mutation that deleted it survived. One
    case per key, because a check that fired for ``regression_test`` alone
    would satisfy a single blanked-field test while leaving the write path
    and the replay unbound.

    Blanking ``regression_test`` in particular is not caught by the
    "names a test that does not exist" rule below it: that one skips an
    empty name, which is exactly the shape this refuses.
    """
    def blank(doc):
        for e in doc["actions"]:
            for f in e["fields"]:
                if f["role"] == "ACTOR":
                    f[key] = ""
                    return
    found = _with_inventory(tmp_path, monkeypatch, blank)
    assert any(f"records no {key}" in p for p in found), found


# ---------------------------------------------------------------------------
# D-2026-29 (P0-R14): "28 of 37 have a second reader" stood beside a
# completion matrix reading "39/39 complete, 0 residual gaps", and nothing
# reconciled the two. Both were true. Neither said whether the nine
# uncovered actions mattered.
#
# The reconciliation is a CLASSIFICATION: an action is authority-changing
# when a forged record of it would change what the system permits, what it
# treats as canonical, or whom it attributes a decision to. Those may not be
# uncovered; the rest may, and each says why in its own entry.
# ---------------------------------------------------------------------------

def test_every_action_is_classified_authority_changing_or_not():
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    unjudged = sorted(e["action"] for e in doc["actions"]
                      if not isinstance(e.get("authority_changing"), bool))
    assert not unjudged, (
        f"{len(unjudged)} action(s) carry no authority_changing judgement: "
        f"{unjudged}")


def test_every_classification_states_its_reason():
    """A bare boolean is a claim nobody can review."""
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    thin = sorted(e["action"] for e in doc["actions"]
                  if len(str(e.get("authority_note", "")).strip()) < 20)
    assert not thin, f"{thin} record no reason for their classification"


def test_the_classification_is_not_all_one_way():
    """Anti-vacuity. A table that says everything changes authority, or that
    nothing does, has not distinguished anything -- and either answer would
    satisfy every other assertion in this section."""
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    values = [e["authority_changing"] for e in doc["actions"]]
    assert any(values) and not all(values), (
        "the authority_changing column partitions nothing")


def test_every_authority_changing_action_has_a_second_reader():
    """The rule that makes the coverage gap answerable rather than merely
    reported."""
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    gaps = sorted(e["action"] for e in doc["actions"]
                  if e["authority_changing"]
                  and e["independent_reader"] != "YES")
    assert not gaps, (
        f"{gaps} change authority and have no independent reader. Either "
        "reconstruct them or say why they do not change authority")


def test_the_uncovered_actions_are_exactly_the_ones_judged_harmless():
    """And the set is named, so shrinking coverage has to come past here."""
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    uncovered = {e["action"] for e in doc["actions"]
                 if e["independent_reader"] != "YES"}
    assert uncovered == {
        "agent.message", "file.read", "network.result", "secret.access",
        "secret.provision", "task.reexecution",
        "task.separate_verification"}, sorted(uncovered)
    assert uncovered == set(durable_actions()) - reconstructed_actions()


def test_the_checker_refuses_an_unjudged_action(tmp_path, monkeypatch):
    def unjudge(doc):
        doc["actions"][0].pop("authority_changing", None)
    found = _with_inventory(tmp_path, monkeypatch, unjudge)
    assert any("no authority_changing classification" in p for p in found), \
        found


def test_the_checker_refuses_an_uncovered_authority_changing_action(
        tmp_path, monkeypatch):
    """The whole point of the classification, exercised.

    ``agent.message`` is uncovered and judged harmless. Reclassify it as
    authority-changing without giving it a reader and the inventory has to
    refuse -- which is what stops the classification from being a way to
    wave a gap through.
    """
    def reclassify(doc):
        for e in doc["actions"]:
            if e["action"] == "agent.message":
                e["authority_changing"] = True
    found = _with_inventory(tmp_path, monkeypatch, reclassify)
    assert any("authority-changing and has no independent reader" in p
               for p in found), found


def test_the_checker_refuses_a_classification_with_no_reason(tmp_path,
                                                             monkeypatch):
    def blank(doc):
        doc["actions"][0]["authority_note"] = ""
    found = _with_inventory(tmp_path, monkeypatch, blank)
    assert any("no reason for its authority_changing" in p for p in found), \
        found


def test_the_labels_that_quote_the_coverage_number_still_match_it():
    """A number in a CI step title is a claim that drifts silently.

    D-2026-19 put it there on purpose -- "for every subsystem" was the
    overclaim it replaced -- and then the number moved when P0-R14 added two
    readers. A label nothing checks is the same defect one layer out, so the
    label is checked.
    """
    covered = len(reconstructed_actions())
    total = len(durable_actions())
    phrase = f"{covered} of {total}"

    workflow = (ROOT / ".github" / "workflows"
                / "agent-substrate.yml").read_text(encoding="utf-8")
    assert phrase in workflow, (
        f"the workflow's step title does not say {phrase!r}; it reports a "
        "coverage number that is no longer the measured one")

    spec = json.loads(
        (ROOT / "tools" / "mutations"
         / "agent_second_reader.json").read_text(encoding="utf-8"))
    assert phrase in spec["title"], (
        f"the mutation spec's title does not say {phrase!r}")
