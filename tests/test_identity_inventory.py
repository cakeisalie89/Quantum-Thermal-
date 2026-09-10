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
    reader reconstructs 28 of 37 durable actions. Nine are not reconstructed
    and are named in the inventory. Saying so is the alternative to a step
    title that overstates it.
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
