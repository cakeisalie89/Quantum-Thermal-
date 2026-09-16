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

import json
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
    "dispatch-sensitivity": "the FULL suite on a NumPy without AVX-512. "
                            "GitHub's runners have it, so every other job "
                            "here measures one side of the dispatch and only "
                            "one; D-2026-53a and D-2026-58 both live on the "
                            "other side. Required, so that deleting the job "
                            "is a contract failure rather than a quiet loss "
                            "of the only thing that looks there",
    "cross-environment-3d": "R59: regenerate the 3D outputs on a hosted "
                            "runner and compare them against the committed "
                            "copies, emitting the result to the JOB LOG "
                            "rather than to an artifact whose storage host "
                            "an egress policy can refuse",
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
    ("analysis/collect_container_3d.py",
     "the R59 cross-environment comparison, in cross-environment-3d"),
    ("uv run python tools/corpus_allowlist.py",
     "the corpus allowlist still describes the committed corpus, so adding "
     "a governed document without reviewing it into the list fails here "
     "rather than at some later read"),
)


def _text() -> str:
    return AGENT_WF.read_text(encoding="utf-8")


def missing_jobs(text: str | None = None) -> tuple:
    body = _text() if text is None else text
    return tuple(sorted(
        f"{name}: {why}" for name, why in REQUIRED_JOBS.items()
        if not re.search(rf"^  {re.escape(name)}:\s*$", body, re.MULTILINE)))


def _run_commands(body: str) -> list:
    """Every line a workflow will actually EXECUTE, and nothing else.

    A first version of this scanned the whole file, and immediately reported
    two groups that do not exist -- both of them quoted inside COMMENTS
    explaining the defect it was written for. Matching the text of a file is
    not the same as matching what the file runs, which is the proxy error
    this repository keeps finding in its own instruments.

    So: ``run: <cmd>`` on one line, and the indented block under ``run: |``,
    with comment lines dropped. A ``#`` inside a quoted shell string would be
    dropped too; no command here has one, and the alternative is a shell
    parser.

    BACKSLASH CONTINUATIONS ARE JOINED. A command split over three lines is
    one command, and returning it as three was the same substitution one level
    down: it made the line ending in a continuation and the line carrying
    ``tests/test_agent_long_horizon.py`` two unrelated strings, so a check
    asking whether that suite runs with QTA_HORIZON_CYCLES set could never see
    both. It would have done the same to a continued ``uv sync --frozen``
    followed by its ``--group`` argument.
    """
    out, block_indent = [], None
    for line in body.splitlines():
        stripped = line.strip()
        if block_indent is not None:
            indent = len(line) - len(line.lstrip())
            if stripped and indent < block_indent:
                block_indent = None
            elif not stripped.startswith("#"):
                if out and out[-1].endswith("\\"):
                    out[-1] = out[-1][:-1].rstrip() + " " + stripped
                else:
                    out.append(stripped)
                continue
        m = re.match(r"^(\s*)-?\s*run:\s*(\|.*)?$", line)
        if m and m.group(2):
            block_indent = len(m.group(1)) + 1
            continue
        m = re.match(r"^\s*-?\s*run:\s+(\S.*)$", line)
        if m:
            block_indent = None
            out.append(m.group(1).strip())
    return out


def undefined_dependency_groups() -> tuple:
    """`--group NAME` in a command a workflow runs, where NAME is not a group.

    THE TRAP THIS CLOSES. The dispatch-sensitivity job shipped with
    ``uv sync --frozen --group stack``. There is no ``stack`` group -- the
    project defines ``dev`` and ``workflow`` -- and uv refused in one second
    with "Group `stack` is not defined in the project's `dependency-groups`
    table", taking the whole job with it on its first run.

    Nothing local could have caught it. A local run reuses an already-synced
    ``.venv`` and never executes the sync line at all, so the command was
    written, reviewed by eye, and first executed on a hosted runner. Reading
    what the sibling jobs use would have found it; so does this, and this does
    not depend on anyone remembering to look.
    """
    try:
        import tomllib
    except ModuleNotFoundError:                     # pragma: no cover
        return ()
    with open(ROOT / "pyproject.toml", "rb") as fh:
        groups = set(tomllib.load(fh).get("dependency-groups", {}))
    if not groups:
        return ("pyproject.toml defines no dependency-groups; a --group "
                "check against an empty set would pass everything",)
    bad, scanned = [], 0
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for cmd in _run_commands(wf.read_text(encoding="utf-8")):
            scanned += 1
            for name in re.findall(r"--group[= ]\s*([A-Za-z0-9_.-]+)", cmd):
                if name not in groups:
                    bad.append(f"{wf.name}: --group {name} in {cmd!r} "
                               f"(defined: {sorted(groups)})")
    if not scanned:
        return ("no run: command was found in any workflow; a --group check "
                "over nothing would pass everything",)
    return tuple(sorted(set(bad)))


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


def unturned_knobs(text: str | None = None) -> tuple:
    """A knob nobody turns is a comment.

    `tests/test_agent_long_horizon.py` compiles in a default cycle count and
    reads `QTA_HORIZON_CYCLES` to raise it. The workflow has a step called
    "long horizon at an elevated scale" that sets it, and nothing checked that
    the step still does: drop the assignment and the suite runs at its default,
    every assertion in it still passes, the step goes green, and its name is a
    claim the run does not support.

    The default is re-derived from the test's own source rather than repeated
    here, so raising it there cannot silently satisfy this.
    """
    src = (ROOT / "tests" / "test_agent_long_horizon.py")
    if not src.exists():
        return (f"{src.name} is gone; the long-horizon claim has no subject",)
    m = re.search(r'CYCLES\s*=\s*int\(os\.environ\.get\(\s*"QTA_HORIZON_CYCLES",\s*"(\d+)"\s*\)\)',
                  src.read_text(encoding="utf-8"))
    if not m:
        return ("cannot find the QTA_HORIZON_CYCLES default in "
                f"{src.name}; this check cannot say whether the knob is "
                "turned above it",)
    default = int(m.group(1))
    elevated = []
    for cmd in _run_commands(text if text is not None else _text()):
        if "test_agent_long_horizon.py" not in cmd:
            continue
        for value in re.findall(r"QTA_HORIZON_CYCLES=(\d+)", cmd):
            if int(value) > default:
                elevated.append(int(value))
    if not elevated:
        return (f"no workflow command runs test_agent_long_horizon.py with "
                f"QTA_HORIZON_CYCLES above its default of {default}; the "
                "elevated-scale step would run the default scale and pass",)
    return ()


#: Verifiers that are NOT expected to appear as a standalone workflow command,
#: each with the reason. Anything else carrying a mutation specification has to
#: be run against the real tree somewhere.
VERIFIER_EXEMPT = {
    "tools/mutation_matrix.py":
        "it IS the harness; its invocation is `mutation_matrix.py <spec>` and "
        "unrun_mutation_specs() already requires every specification to be "
        "passed to it",
    "tools/independent_verify.py":
        "a subprocess verifier spawned by qta_agent/separate_verify.py with a "
        "log path on argv, exercised by the agent suites; it is not a "
        "standalone gate and has no tree-wide verdict to report",
}


def unwired_verifiers(text: str | None = None) -> tuple:
    """A verifier that exists, is tested, has a mutation matrix -- and that
    nothing requires the workflow to actually RUN.

    K1 of the repo-contract specification made this concrete one level down: a
    check can be deleted from `problems()` while every test still passes,
    because the tests called the function and nothing tested its use. The same
    hole exists one level out, for the tools themselves. Delete the workflow
    step that runs `tools/test_isolation.py` and keep its mutation step, and
    everything here is still satisfied -- the tool exists, its tests pass, its
    specification is wired -- while the 107 collections it performs never run
    again. A mutation matrix scores a tool's TESTS, never its verdict on the
    actual repository.

    Measured when this was written: 13 runnable verifiers carried a mutation
    specification and exactly one, completion_matrix.py, was named in
    REQUIRED_COMMANDS. Eleven were being run and nothing said they had to be.

    The candidate set is re-derived from the specifications on disk rather than
    listed here, so a verifier added with a matrix is covered without anyone
    remembering to add it.
    """
    candidates = set()
    for spec in sorted(MUTATIONS.glob("*.json")):
        for m in json.loads(spec.read_text(encoding="utf-8"))["mutations"]:
            path = m["path"]
            if (path.startswith("tools/") and path.endswith(".py")
                    and "/mutations/" not in path):
                candidates.add(path)
    if not candidates:
        return ("no mutation specification names a tools/*.py file; this "
                "check would pass over an empty set",)

    runnable = set()
    for path in candidates:
        src = (ROOT / path)
        if not src.exists():
            return (f"{path} is mutated by a specification and does not exist",)
        body = src.read_text(encoding="utf-8")
        if "def main" in body and '__main__' in body:
            runnable.add(path)
    if not runnable:
        return ("no mutated tools/*.py is runnable; the set this check "
                "reasons about is empty",)

    # EXECUTED, not merely mentioned.
    #
    # The first version asked whether the path appeared in a command that was
    # not a mutation_matrix invocation. The matrix reported that mutation as a
    # SURVIVOR and it was right to: a specification is passed as
    # `tools/mutations/x.json`, which does not contain `tools/x.py`, so the
    # clause could never fire. An unfalsifiable guard, aimed at a risk that
    # does not exist in this configuration.
    #
    # The risk that DOES exist is the opposite one: `ruff check
    # tools/test_isolation.py` mentions the tool without running it, and a
    # substring test counts that as the verifier having reported on the tree.
    # So match the shape of execution.
    executed = set()
    cmds = _run_commands(text if text is not None else _text())
    for cmd in cmds:
        for m in re.finditer(r"(?:^|\s)(?:uv run )?python3?\s+(\S+)", cmd):
            executed.add(m.group(1))
    out = []
    for path in sorted(runnable - set(VERIFIER_EXEMPT)):
        if path not in executed:
            out.append(
                f"{path} carries a mutation specification and is never run "
                "directly by the workflow; its tests would keep passing and "
                "its verdict on the real tree would never be taken")
    return tuple(out)


def problems() -> tuple:
    body = _text()
    out = []
    out += [f"missing job -- {x}" for x in missing_jobs(body)]
    out += [f"undefined dependency group -- {x}"
            for x in undefined_dependency_groups()]
    out += [f"missing command -- {x}" for x in missing_commands(body)]
    out += [f"mutation spec never runs in CI -- {x}"
            for x in unrun_mutation_specs(body)]
    out += [f"action is not pinned to a commit -- {x}"
            for x in uses_unpinned_actions(body)]
    out += [f"knob is never turned -- {x}" for x in unturned_knobs(body)]
    out += [f"verifier is never run -- {x}" for x in unwired_verifiers(body)]
    return tuple(out)


def main() -> int:
    found = problems()
    if not found:
        specs = len(list(MUTATIONS.glob("*.json")))
        # "{n} jobs" used to read as a census of the workflow and is a
        # count of the REQUIRED set -- the workflow may hold more. Saying
        # which is the same repair as D-2026-54's.
        print(f"workflow contract holds: {len(REQUIRED_JOBS)} required jobs "
              f"all present, every --group defined, "
              f"{len(REQUIRED_COMMANDS)} commands, {specs} mutation specs "
              "all wired in, every action pinned, the long-horizon knob "
              "turned above its default, every verifier with a matrix also "
              "run against the real tree")
        return 0
    print(f"WORKFLOW CONTRACT BROKEN ({len(found)} problem(s)):")
    for f in found:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
