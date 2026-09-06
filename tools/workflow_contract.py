"""What the hosted workflows must still contain, checked locally.

WHY A ROW ABOUT CI NEEDS CODE

The completion matrix claims things about hosted verification: that every
mutation matrix runs, that the full pytest suite runs, that the substrate
is exercised on a second interpreter. Those claims live in YAML, and YAML
is not something a test suite notices changing. Delete the full-suite job
and every local check still passes; the row keeps claiming coverage that
no longer exists, and the only thing that would tell you is a hosted run
that no longer fails.

So the claims are written down as a CONTRACT and checked here. This is the
same discipline as the completion validator: a statement about the system
is worth having only if something refuses when it stops being true.

WHAT THIS IS NOT

It does not run the workflows, and it does not know whether a hosted run
passed -- that is read from the API, per SHA, and no local check can
substitute for it. It answers one narrower question: does the workflow
still SAY it does what the matrix says it does.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
MUTATIONS = ROOT / "tools" / "mutations"

AGENT_WF = WORKFLOWS / "agent-substrate.yml"

#: Jobs agent-substrate.yml must define, and why the matrix leans on each.
REQUIRED_JOBS = {
    "agent-substrate": "the mutation matrices and the agent suites",
    "full-suite": "the FULL pytest suite and package_consistency_check.py, "
                  "which R58 used to record as release-only",
    "second-interpreter": "the substrate on a Python other than the pin",
}

#: Commands that must appear somewhere in agent-substrate.yml.
REQUIRED_COMMANDS = (
    ("uv run python tools/completion_matrix.py",
     "the matrix is self-consistent"),
    ("uv run python generate_manifest.py --check",
     "derived artifacts are in step with their sources"),
    ("uv run python ro_crate_tools.py validate",
     "the RO-Crate validates"),
    ("uv run python package_consistency_check.py",
     "package consistency, in full-suite"),
    ("uv run python -m pytest tests/ -q",
     "the complete pytest suite, in full-suite"),
)


def _text() -> str:
    return AGENT_WF.read_text(encoding="utf-8")


def missing_jobs(text: str | None = None) -> tuple:
    body = _text() if text is None else text
    return tuple(sorted(
        f"{name}: {why}" for name, why in REQUIRED_JOBS.items()
        if not re.search(rf"^  {re.escape(name)}:\s*$", body, re.MULTILINE)))


def missing_commands(text: str | None = None) -> tuple:
    body = _text() if text is None else text
    flat = " ".join(body.split())
    return tuple(sorted(
        f"{cmd}: {why}" for cmd, why in REQUIRED_COMMANDS
        if " ".join(cmd.split()) not in flat))


def unrun_mutation_specs(text: str | None = None) -> tuple:
    """Mutation specs on disk that no workflow step runs.

    THE failure this catches: adding a matrix, running it locally, and
    never wiring it in. It then protects nothing on any push, and the row
    citing it is claiming hosted coverage it does not have.
    """
    body = _text() if text is None else text
    flat = " ".join(body.split())
    out = []
    for spec in sorted(MUTATIONS.glob("*.json")):
        rel = spec.relative_to(ROOT).as_posix()
        if rel not in flat:
            out.append(rel)
    return tuple(out)


def uses_unpinned_actions(text: str | None = None) -> tuple:
    """Any ``uses:`` that is not a 40-hex commit object.

    RELEASE_POLICY #3. A moving tag is a supply-chain hole: the workflow
    that verifies this repository would be running whatever that tag points
    at today.
    """
    body = _text() if text is None else text
    bad = []
    # Anchored to a real YAML key, and NOT to end-of-line. Both halves were
    # learned the hard way in one sitting.
    #
    # Unanchored, the pattern matched the workflow's own header comment --
    # "every `uses:` below is an immutable 40-hex commit object" -- and
    # reported that sentence's backtick as an unpinned action. A guard
    # whose first finding is its own documentation is a guard nobody
    # believes.
    #
    # Then anchoring it to `\s*$` was worse and quieter: every real `uses:`
    # in this repository carries a trailing `# v4` comment, so the pattern
    # matched NOTHING and the checker reported "every action pinned" having
    # examined zero actions. Its own test caught that, which is the whole
    # reason the negative cases are tested at all.
    for m in re.finditer(r"^\s*(?:-\s+)?uses:\s*(\S+)", body,
                         re.MULTILINE):
        ref = m.group(1)
        if ref.startswith("#"):                      # a bare comment line
            continue
        if "@" not in ref:
            bad.append(ref)
            continue
        _, _, sha = ref.rpartition("@")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            bad.append(ref)
    return tuple(sorted(set(bad)))


def problems() -> tuple:
    body = _text()
    out = []
    out += [f"missing job -- {x}" for x in missing_jobs(body)]
    out += [f"missing command -- {x}" for x in missing_commands(body)]
    out += [f"mutation spec never runs in CI -- {x}"
            for x in unrun_mutation_specs(body)]
    out += [f"action is not pinned to a commit -- {x}"
            for x in uses_unpinned_actions(body)]
    return tuple(out)


def main() -> int:
    found = problems()
    if not found:
        specs = len(list(MUTATIONS.glob("*.json")))
        print(f"workflow contract holds: {len(REQUIRED_JOBS)} jobs, "
              f"{len(REQUIRED_COMMANDS)} commands, {specs} mutation specs "
              "all wired in, every action pinned")
        return 0
    print(f"WORKFLOW CONTRACT BROKEN ({len(found)} problem(s)):")
    for f in found:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
