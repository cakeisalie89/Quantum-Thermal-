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
  * every field classified ACTOR must name a regression test, and that test
    must exist.

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


def reconstructed_actions() -> set:
    """Actions the independent reader names, measured from its source.

    Every string literal in reconstruct.py that is also a known action. A
    reader that mentions an action without handling it would be counted here
    wrongly -- which is why the mutations attack the handlers rather than
    this list.
    """
    src = (PKG / "reconstruct.py").read_text(encoding="utf-8")
    return {a for a in durable_actions() if f'"{a}"' in src}


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
    recon = reconstructed_actions()
    return {"durable_actions": len(actions),
            "independently_reconstructed": len(recon),
            "not_reconstructed": sorted(set(actions) - recon)}


def main() -> int:
    s = summary()
    print(f"durable actions: {s['durable_actions']}")
    print(f"independently reconstructed: {s['independently_reconstructed']}")
    if s["not_reconstructed"]:
        print(f"NOT reconstructed ({len(s['not_reconstructed'])}):")
        for a in s["not_reconstructed"]:
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
