#!/usr/bin/env python3
"""Prove the released revision is the one the owner authorized.

WHY THIS IS SEPARATE FROM THE SHELL STEP
----------------------------------------
The workflow already compares three values it can see cheaply: the Actions
context SHA, ``git rev-parse HEAD``, and the tag's target. Those catch a
checkout that does not match its trigger. They cannot decide the question that
actually matters, which is whether the *content* being released is the content
the owner reviewed -- that needs the policy and the commit graph together.

THE SELF-REFERENCE CONSTRAINT
-----------------------------
A commit cannot contain its own SHA, so the policy committed in the released
revision cannot name that revision. The policy therefore names the REVIEWED
revision ``C``, and the released commit ``A`` is ``C``'s descendant that adds
the authorization record:

    C  reviewed content, policy still unresolved
    A  child of C, identical except that it fills in the policy
       refs/tags/<TAG> -> A

This gate proves the relationship rather than asserting it:

  1. the ref being built is exactly ``policy.authorized_ref``;
  2. ``pinned_revision`` (C) exists and is an ancestor of the checkout (A);
  3. C != A, because A must be the commit that carries the authorization;
  4. the C..A diff touches ONLY the canonical policy file. Anything else means
     content was released that was never reviewed under this authorization.

Check 4 is the load-bearing one. Without it "descendant of a reviewed commit"
would authorize arbitrary later changes.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import release_trust

#: The governed authorization closure, determined experimentally rather than
#: guessed: filling the policy at C and running every required deterministic
#: regeneration changes exactly these paths, and repeating the regeneration
#: reaches a stable fixed point.
#:
#: The earlier version allowed ONLY the policy file. That was not satisfiable
#: in this repository: generate_manifest.py hashes every tracked file except
#: its own two detached artifacts, so editing the policy necessarily changes
#: the manifest -- and release.yml runs `generate_manifest.py --check` before
#: building. Widening the closure is the fix; excluding the policy from
#: manifest coverage would not be, because that removes it from governance.
AUTHORIZATION_PATHS = release_trust.AUTHORIZATION_CLOSURE


class GateError(Exception):
    """The released revision is not the authorized one."""


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _commit_exists(rev: str) -> bool:
    try:
        return _git("cat-file", "-t", rev) == "commit"
    except GateError:
        return False



def _derivatives_match_regeneration(changed: list[str]) -> list[str]:
    """Regenerate each deterministic derivative and require byte equality.

    Runs the repository's own commands in dependency order -- RO-Crate first
    (the manifest hashes it), then the manifest (the crate records its size) --
    and compares the result against what the authorization commit actually
    contains.
    """
    derivatives = sorted(set(changed) &
                         release_trust.DETERMINISTIC_DERIVATIVE_PATHS)
    if not derivatives:
        return []
    before = {d: Path(d).read_bytes() for d in derivatives if Path(d).exists()}
    for cmd in (["python3", "ro_crate_tools.py"],
                ["python3", "generate_manifest.py"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return [f"regeneration command {' '.join(cmd)} failed: "
                    f"{r.stderr.strip()[:200]}"]
    out = []
    for d in derivatives:
        now = Path(d).read_bytes() if Path(d).exists() else b""
        if before.get(d) != now:
            out.append(
                f"{d} is in the authorization closure but does not equal an "
                "independent regeneration; deterministic derivatives may not "
                "be hand-edited")
    return out


def check(checkout: str, ref: str, policy: dict) -> list[str]:
    """Return a list of failures; empty means the gate passes."""
    problems: list[str] = []

    authorized_ref = str(policy.get("authorized_ref", ""))
    if ref != authorized_ref:
        problems.append(
            f"ref mismatch: building {ref!r} but the policy authorizes "
            f"{authorized_ref!r}. A tag other than the authorized one is not "
            "covered by this authorization.")

    pinned = str(policy.get("pinned_revision", ""))
    if not _commit_exists(pinned):
        problems.append(
            f"pinned_revision {pinned!r} is not a commit in this repository")
        return problems

    if pinned == checkout:
        problems.append(
            f"pinned_revision equals the released revision ({checkout}). "
            "pinned_revision names the REVIEWED revision; the released commit "
            "must be the descendant carrying the authorization record.")
        return problems

    # Ancestry: the reviewed revision must be reachable from what is released.
    r = subprocess.run(["git", "merge-base", "--is-ancestor", pinned, checkout],
                       capture_output=True, text=True)
    if r.returncode != 0:
        problems.append(
            f"pinned_revision {pinned[:12]} is NOT an ancestor of the released "
            f"revision {checkout[:12]}; the release does not build on the "
            "reviewed content")
        return problems

    # The only permitted difference is the authorization record itself.
    changed = [p for p in _git("diff", "--name-only", f"{pinned}..{checkout}")
               .splitlines() if p.strip()]
    unreviewed = sorted(set(changed) - AUTHORIZATION_PATHS)
    if unreviewed:
        problems.append(
            f"the released revision changes {len(unreviewed)} path(s) beyond "
            f"the authorization closure: {unreviewed[:8]}. Only "
            f"{sorted(AUTHORIZATION_PATHS)} may differ between the reviewed "
            "revision and the released commit.")
    elif not changed:
        problems.append(
            f"the released revision is identical in content to "
            f"{pinned[:12]}; the authorization record was never filled in")
    else:
        # Membership in the closure is not enough: each DETERMINISTIC
        # DERIVATIVE must equal an independent regeneration, or the closure
        # would be a licence to hand-edit the manifest and RO-Crate.
        problems.extend(_derivatives_match_regeneration(changed))
        # The authorization INPUT must actually be present, or the "closure"
        # was only derivative churn with no authorization in it.
        if not (set(changed) & release_trust.AUTHORIZATION_INPUT_PATHS):
            problems.append(
                "the C..A diff contains no change to the authorization "
                f"record {sorted(release_trust.AUTHORIZATION_INPUT_PATHS)}; "
                "derivative churn alone is not an authorization")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify the released revision against the authorized "
                    "policy. Confers no trust; refuses on any mismatch.")
    ap.add_argument("--checkout", required=True,
                    help="revision actually checked out (git rev-parse HEAD)")
    ap.add_argument("--ref", required=True,
                    help="the ref being built, e.g. refs/tags/qta-stage11")
    ap.add_argument("--policy", default=None,
                    help="canonical policy path (defaults to the canonical "
                         "location)")
    a = ap.parse_args(argv)

    try:
        policy = release_trust.load_canonical_policy(
            Path(a.policy) if a.policy else None, require_resolved=True)
    except release_trust.PolicyError as e:
        print(f"[FAIL-CLOSED] trust policy is not authorized for a release: "
              f"{e}")
        return 1

    try:
        problems = check(a.checkout, a.ref, policy)
    except GateError as e:
        print(f"[FAIL-CLOSED] {e}")
        return 1

    if problems:
        print("[FAIL-CLOSED] released revision is not the authorized one:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"[revision-gate] released {a.checkout[:12]} on {a.ref}; reviewed "
          f"revision {policy['pinned_revision'][:12]} is an ancestor and the "
          "only difference is the authorization record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
