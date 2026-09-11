#!/usr/bin/env python3
"""Every durable action, every identity-bearing field, and who checks it.

WHY THIS EXISTS

The sibling sweep that was supposed to find every actor-bearing payload field
missed two of them -- the escalation raiser and the message sender -- in the
same file and the same defect class as the one it was written for. It missed
them because a sweep that enumerates by READING stops where attention stops,
and nothing existed that could be checked for completeness independently of
my having looked. "I swept the siblings" was an unverifiable self-assertion
of exactly the kind this repository distrusts everywhere else.

So the inventory is an artefact, and the mechanical parts of it are checked
rather than asserted:

  * every ``ACT_*`` constant in ``qta_agent/`` must appear. A new durable
    action fails this until somebody classifies its identity fields;
  * no entry may name an action that does not exist, so the table cannot
    drift into describing a past version of the code;
  * the recorded independent-reader coverage must match what
    ``reconstruct.py`` actually dispatches on. That is the check that turns
    "the second reader covers every subsystem" from a claim into a
    measurement -- and the first time it ran, it said 28 of 37;

    "dispatches on" is meant literally, and did not used to be. The first
    version of that measurement asked whether the action's name occurred
    anywhere in the second reader's TEXT, so a name in a docstring, in an
    anomaly message, or in a comment saying the action is deliberately not
    handled counted as coverage -- and an action dispatched on in single
    quotes counted as nothing. It is read from the parse tree now, and only
    from positions that decide which branch runs. An action whose name is
    present but branched on by nothing is reported as its own category
    rather than folded into either answer;
  * every field classified ACTOR must name a regression test, and that test
    must exist;
  * every action must be classified AUTHORITY-CHANGING or not, with a
    reason -- and an authority-changing action with no independent reader
    fails. That is the rule that reconciles "28 of 37 have a second reader"
    with a completion matrix reading 39/39: the gap is acceptable exactly
    where a forged record changes nothing anyone is permitted to do, and
    each such action has to say so in its own entry.

WHAT IS REVIEWED RATHER THAN DERIVED

The classification itself: whether a field is the actor of this event, the
subject it is about, a recipient, or a diagnostic duplicate. Deriving that
from source would be guessing at intent, and a guess that looks mechanical
is worse than a judgement that says it is one. The judgement is committed;
the facts around it are measured.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "qta_agent"
INVENTORY = ROOT / "docs" / "identity_inventory.json"

#: Roles a payload field can play. ACTOR fields must derive from or equal
#: ``ev.actor``; the rest may legitimately name somebody else and must never
#: be read as authority for who performed THIS action.
ROLES = {
    "ACTOR",                 # who performed this action
    "SUBJECT",               # who or what the action is about
    "RECIPIENT",             # who it is addressed to
    "DIAGNOSTIC_DUPLICATE",  # a copy of a fact whose authority is elsewhere
    "NONE",                  # the action carries no identity-bearing field
}


def durable_actions() -> dict:
    """``{action string: module}`` for every ACT_* constant in the package."""
    out = {}
    for f in sorted(PKG.glob("*.py")):
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target = node.targets[0]
            if (isinstance(target, ast.Name)
                    and target.id.startswith("ACT_")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                out[node.value.value] = f.name
    return out


#: What this module can say about an action from the second reader's source.
#:
#: The middle one is the whole point. Its first version had two categories --
#: the action string occurs in the file, or it does not -- and reported the
#: first as "independently reconstructed".
DISPATCHED = "DISPATCHED"    # the reader branches on it
MENTIONED = "MENTIONED"      # the string is there; nothing branches on it
ABSENT = "ABSENT"            # not in the source at all


def _dispatch_literals(source: str) -> set:
    """String constants in a DISPATCH position, from the parsed source.

    A dispatch position is one whose value decides which branch runs:

      * either side of a comparison -- ``action == "task.create"``, and the
        containers of an ``in`` test, which is how ``owned`` and
        ``_AUTHORITY_ACTIONS`` are consulted;
      * an element of a set, list or tuple literal -- the vocabularies those
        membership tests are written against;
      * a key of a dict literal, for a table-driven dispatch.

    Everything else -- a docstring, a comment, an error message, a name in
    prose -- is not here, which is the entire difference between this and
    what it replaced.

    Comments never appear in the tree at all, so a commented-out handler is
    excluded for free rather than by a rule somebody has to remember.
    """
    out: set = set()

    def literals(node) -> set:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            return {e.value for e in node.elts
                    if isinstance(e, ast.Constant)
                    and isinstance(e.value, str)}
        return set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            out |= literals(node.left)
            for c in node.comparators:
                out |= literals(c)
        elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            out |= literals(node)
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                out |= literals(k)
    return out


def reconstruction_coverage(source: str | None = None) -> dict:
    """``{action: DISPATCHED | MENTIONED | ABSENT}`` for every known action.

    ``source`` overrides the second reader's text, so the MEASUREMENT can be
    tested against planted files. It is a parameter rather than a patched
    module global because the alternative -- pointing ``PKG`` at a temporary
    directory -- also empties ``durable_actions()``, and a classifier asked
    to classify nothing agrees with everything.

    MEASURED FROM THE PARSE TREE, NOT FROM THE TEXT.

    This function used to be one line::

        return {a for a in durable_actions() if f'"{a}"' in src}

    which counted an action as independently reconstructed when its name
    appeared ANYWHERE in reconstruct.py -- including in the module docstring
    that lists what the reader does not cover, in an anomaly message, or in a
    comment explaining why something is deliberately not handled. It was also
    quote-sensitive: an action dispatched on in single quotes did not count
    at all, so the same rule could overstate coverage and understate it.

    The number it produced happened to be right. "Happened to be right" is
    the state this repository treats as a defect, because nothing would have
    said so when it stopped being.
    """
    src = (source if source is not None
           else (PKG / "reconstruct.py").read_text(encoding="utf-8"))
    dispatched = _dispatch_literals(src)
    out = {}
    for action in durable_actions():
        if action in dispatched:
            out[action] = DISPATCHED
        elif action in src:
            # A bare substring search, deliberately: this category is not
            # coverage, so over-reporting is the safe direction. Requiring
            # quotes here would reproduce the old rule's other defect -- a
            # name inside an f-string, or written with the other quote
            # character, would read as absent.
            out[action] = MENTIONED
        else:
            out[action] = ABSENT
    return out


def reconstructed_actions(source: str | None = None) -> set:
    """Actions the independent reader actually branches on."""
    return {a for a, kind in reconstruction_coverage(source).items()
            if kind == DISPATCHED}


def mentioned_but_not_dispatched(source: str | None = None) -> set:
    """Named in the second reader's source, and handled by nothing there.

    Reported rather than folded into either side: an action in this set is
    one somebody might reasonably believe is covered, and the whole finding
    behind this function is that believing it was once enough.
    """
    return {a for a, kind in reconstruction_coverage(source).items()
            if kind == MENTIONED}


def problems() -> list:
    """Everything the inventory gets wrong about the code as it is."""
    found = []
    if not INVENTORY.exists():
        return [f"{INVENTORY.relative_to(ROOT)} does not exist"]
    doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
    entries = {e["action"]: e for e in doc["actions"]}
    actions = durable_actions()

    missing = sorted(set(actions) - set(entries))
    if missing:
        found.append(
            f"{len(missing)} durable action(s) are not classified: {missing}. "
            "A new action is not covered by this inventory until somebody "
            "says what identity it carries")
    unknown = sorted(set(entries) - set(actions))
    if unknown:
        found.append(
            f"{len(unknown)} entr(y/ies) name actions that do not exist: "
            f"{unknown}. The table is describing a past version of the code")

    for act, e in sorted(entries.items()):
        if act not in actions:
            continue
        if e.get("module") != actions[act]:
            found.append(
                f"{act}: recorded in {e.get('module')!r}, defined in "
                f"{actions[act]!r}")
        # AUTHORITY-CHANGING ACTIONS MUST HAVE A SECOND READER.
        #
        # "28 of 37" was a true number beside a completion matrix reading
        # 39/39 with no residual gaps, and nothing reconciled the two. The
        # reconciliation is this: an action is authority-changing when a
        # forged record of it would change what the system permits, treats
        # as canonical, or attributes to a person. Those may not be
        # uncovered. The rest may, and each says why in the table.
        #
        # The judgement is reviewed, not derived -- deriving "does this
        # change authority" from source would be guessing at intent. What is
        # MECHANICAL is that the judgement and the coverage cannot drift
        # apart without something refusing.
        changing = e.get("authority_changing")
        if not isinstance(changing, bool):
            found.append(
                f"{act}: records no authority_changing classification. An "
                "action nobody has judged is one nobody can say needs a "
                "second reader")
        elif changing and e.get("independent_reader") != "YES":
            found.append(
                f"{act} is classified as authority-changing and has no "
                "independent reader. Either reconstruct it or say why it "
                "does not change authority; leaving both is the gap the "
                "coverage number was reporting without reconciling")
        if changing is not None and len(
                str(e.get("authority_note", "")).strip()) < 20:
            found.append(
                f"{act}: records no reason for its authority_changing "
                "classification. A bare boolean is a claim nobody can review")

        for fld in e.get("fields", []):
            role = fld.get("role")
            if role not in ROLES:
                found.append(f"{act}.{fld.get('name')}: role {role!r} is not "
                             f"one of {sorted(ROLES)}")
            if role == "ACTOR":
                for key in ("write_path", "replay", "regression_test"):
                    if not fld.get(key):
                        found.append(
                            f"{act}.{fld.get('name')} is an ACTOR field and "
                            f"records no {key}")
                test = fld.get("regression_test")
                if test and not _test_exists(test):
                    found.append(
                        f"{act}.{fld.get('name')} names regression test "
                        f"{test!r}, which does not exist")

    recorded = {e["action"] for e in doc["actions"]
                if e.get("independent_reader") == "YES"}
    actual = reconstructed_actions()
    if recorded != actual:
        only_claimed = sorted(recorded - actual)
        only_real = sorted(actual - recorded)
        if only_claimed:
            found.append(
                f"claimed as independently reconstructed but absent from "
                f"reconstruct.py: {only_claimed}")
        if only_real:
            found.append(
                f"reconstructed by reconstruct.py but not recorded as such: "
                f"{only_real}")
    return found


def _test_exists(name: str) -> bool:
    for f in (ROOT / "tests").glob("test_*.py"):
        if f"def {name}(" in f.read_text(encoding="utf-8"):
            return True
    return False


def summary() -> dict:
    actions = durable_actions()
    coverage = reconstruction_coverage()
    recon = {a for a, k in coverage.items() if k == DISPATCHED}
    return {"durable_actions": len(actions),
            "independently_reconstructed": len(recon),
            "not_reconstructed": sorted(set(actions) - recon),
            "mentioned_but_not_dispatched": sorted(
                a for a, k in coverage.items() if k == MENTIONED)}


def main() -> int:
    s = summary()
    print(f"durable actions: {s['durable_actions']}")
    print(f"independently reconstructed: {s['independently_reconstructed']}")
    if s["not_reconstructed"]:
        print(f"NOT reconstructed ({len(s['not_reconstructed'])}):")
        for a in s["not_reconstructed"]:
            print(f"    {a}")
    if s["mentioned_but_not_dispatched"]:
        # Named in the second reader and handled by nothing in it. Printed
        # separately because this is the category the old measurement
        # silently counted as coverage.
        print(f"named but not dispatched on "
              f"({len(s['mentioned_but_not_dispatched'])}):")
        for a in s["mentioned_but_not_dispatched"]:
            print(f"    {a}")
    found = problems()
    if found:
        print(f"\n{len(found)} problem(s):")
        for p in found:
            print(f"  - {p}")
        return 1
    print("\ninventory agrees with the code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
