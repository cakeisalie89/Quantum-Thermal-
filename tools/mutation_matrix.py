#!/usr/bin/env python3
"""Run a declarative mutation matrix against a test suite.

WHY THIS IS A COMMITTED TOOL AND NOT A SCRATCH SCRIPT

`authorities.json` claims that every enforcement point in `agent_substrate/` is
verified by mutation testing. A claim of that kind is only as good as the
ability to re-run it, so the harness and the mutation specifications live in
the repository and are reviewable like anything else.

WHAT A MUTATION MATRIX ACTUALLY PROVES

Coverage says a line executed. It does not say anything would have noticed if
the line were deleted. Each mutation here disables exactly one check; the
suite must then fail. A mutation that SURVIVES means that check is
unprotected -- nothing distinguishes a build with it from a build without it.

THE TRAP THIS HARNESS IS BUILT TO AVOID

A mutation matrix run against a RED baseline proves nothing: all N mutations
"fail the suite", every one for the pre-existing reason, and the report reads
as a perfect score. The baseline is therefore checked first and the run
refuses to start otherwise.

Lesser versions of the same trap are also closed:

  * An anchor that no longer matches its source is an ERROR, not a skip. A
    silently skipped mutation is a mutation that tested nothing while
    appearing in the report.
  * Sources are restored in a ``finally`` and then re-hashed. "Restored
    byte-identical" is verified rather than asserted.
  * A kill is a test's (AUD-093). A mutant that does not load, or under
    which the suites cannot be collected, is STILLBORN: no test ran against
    it. A mutant that reads a name nothing defines is refused before the
    run, since the code it changes would raise NameError where its fault
    was to act. A suite a signal ended, or one that failed and named no
    test, said nothing about which check was lost. None of these is
    counted, and each fails the run. A test that errors, in its setup or
    its body, makes a kill, and is named.

USAGE

    python3 tools/mutation_matrix.py tools/mutations/agent_substrate.json

Exit status is 0 only when the baseline was green, every anchor matched, every
mutation was killed by a test the report names, and every source was restored
byte-identical.
"""
from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import pathlib
import os
import re
import shutil
import signal
import subprocess
import symtable
import sys
import time
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent

#: Written before the first mutation, removed on a clean exit. Its presence at
#: startup means a previous run died without restoring, so the sources on disk
#: may still be mutated.
#:
#: This exists because it happened. A run was killed by a sandbox timeout
#: before the signal handler below was added, and left `os.setsid()` replaced
#: by `pass` in the execution layer. The check afterwards grepped for the
#: OTHER mutations' markers and reported the tree clean, and the files were
#: new and untracked so git could not contradict it. The result was a process
#: group that was never created, so every timeout killed the CALLER's process
#: group -- the test runner terminated itself, repeatedly, and the cause looked
#: like a kernel or sandbox problem for as long as the leftover went unnoticed.
#:
#: A sidecar turns that silent state into a refusal to start.
RECOVERY = ROOT / ".mutation-recovery.json"

#: Backstop only. A mutation whose kill mechanism is the harness timing out is
#: a badly written TEST, not a well-caught mutation: it costs the timeout in
#: wall clock every run and reports nothing about which check was lost. The
#: tests are expected to bound their own blocking calls -- see the ``deadline``
#: helper in tests/test_agent_evidence.py, which turns a blocked FIFO read into
#: a five-second failure. This value exists so a mutation nobody anticipated
#: cannot stall CI indefinitely; the agent suites run in single-digit seconds.
SUITE_TIMEOUT_S = 300


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dirty_tracked_files() -> list:
    """Tracked files currently modified in the working tree, repo-relative.

    Deliberately git rather than a hash snapshot: git already knows the full
    tracked set, so this notices damage to a file the harness never touched
    and never listed.
    """
    proc = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain",
                           "--untracked-files=no"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return sorted(line[3:].strip() for line in proc.stdout.splitlines()
                  if line.strip())


#: Where content discarded by :func:`_restore` is kept.
#:
#: This exists because the harness destroyed real work, twice, in one session.
#: ``_restore`` is ``git checkout HEAD --``, which is the right way to undo
#: damage a mutated build did to a tracked file -- and it is indistinguishable
#: from discarding an uncommitted edit. Anything that becomes dirty DURING a
#: run looks like collateral damage, so edits made in another window while the
#: matrix ran were reverted with no way back: a rewritten completion matrix,
#: and then this file's own previous version of this fix.
#:
#: The restore still happens. Leaving a suite-damaged file in place is worse,
#: because every later mutation then runs against corrupted inputs and the
#: report says nothing about it. What changes is that the discarded bytes are
#: KEPT and their location printed, so a false positive costs a copy instead
#: of the work.
QUARANTINE = ROOT / ".mutation-quarantine"


def _restore(paths: list) -> None:
    """Revert tracked files, keeping a copy of what is discarded.

    For collateral damage only -- files the harness did not mutate and holds
    no original for. The mutated sources are restored from ``original``.
    """
    if not paths:
        return
    stamp = time.strftime("%Y%m%dT%H%M%S")
    saved = []
    for rel in paths:
        src = ROOT / rel
        if not src.is_file():
            continue
        dest = QUARANTINE / stamp / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            saved.append(dest)
        except OSError as exc:      # keeping a copy must never block a restore
            print(f"  WARNING: could not quarantine {rel}: {exc}")
    if saved:
        print(f"  discarded content kept under {QUARANTINE.name}/{stamp}/")
    subprocess.run(["git", "-C", str(ROOT), "checkout", "HEAD", "--",
                    *paths], capture_output=True, text=True)


class SuiteRun(NamedTuple):
    """How one run of the suites ended (AUD-093).

    ``failed`` and ``errored`` are tests, by name: a test whose body failed,
    and one that raised in its setup, its body or its teardown. ``errored``
    used to be dropped, so a kill made in a fixture's setup was reported as a
    kill with no test, the same line a mutant no test ever ran under got.
    ``uncollected`` is files pytest could not collect: no test in them ran.
    """

    rc: int
    failed: list
    errored: list
    uncollected: list

    @property
    def named(self) -> list:
        return self.failed + self.errored

    def said(self) -> list:
        """What the run said, for a report: the tests, the files it could
        not collect, or failing that its exit status."""
        return (self.named + [f"{f} (not collected)" for f in self.uncollected]
                or [f"pytest exit {self.rc}, no test named"])


def _summary(stdout: str) -> tuple:
    """``(failed, errored, uncollected)`` from pytest's short summary:
    ``FAILED path::test``, ``ERROR path::test`` and, for a file it could not
    collect, ``ERROR path`` with no test in it."""
    failed: list[str] = []
    errored: list[str] = []
    uncollected: list[str] = []
    for line in stdout.splitlines():
        word = line.split(" ", 1)[0]
        if word not in ("FAILED", "ERROR"):
            continue
        node = line[len(word):].strip().split(" ")[0]
        if "::" in node:
            (failed if word == "FAILED" else errored).append(
                node.split("::")[-1])
        elif word == "ERROR" and node:
            uncollected.append(node)
    return failed, errored, uncollected


def run_suite(suites: list, python: str, *,
              only: list | None = None) -> SuiteRun:
    """Run the suites once, and say how they ended. ``-x`` stops at the first
    test that does not pass.

    ``only`` narrows the run to named tests (``-k``), for re-asking a
    question about a specific failure rather than re-running everything.

    THE STALE-BYTECODE TRAP, AND WHY EVERY RUN GETS A PRIVATE CACHE

    CPython invalidates a cached ``.pyc`` on (source mtime, source SIZE). A
    mutation that swaps ``if cond:`` for ``if False:`` usually changes the
    file's size -- but two mutations in the same matrix very often change it
    by the SAME number of bytes, and the matrix applies them seconds apart.
    When the sizes match and both writes land in the same mtime second, the
    second mutation runs against the FIRST one's compiled code.

    That is not a cosmetic reporting problem. The report then says a mutation
    was killed when nothing tested it, which is precisely the false negative
    this whole harness exists to prevent: an enforcement point with no test
    behind it, certified as protected. It was found by noticing that a kill
    was attributed to a test that could not possibly have failed for that
    reason, and it affected 32 of the 158 mutations then committed.

    ``PYTHONPYCACHEPREFIX`` points every run at a fresh directory, so nothing
    on disk can be reused across mutations and the compile always starts from
    the source the harness just wrote. Redirecting rather than deleting also
    keeps the repository's own ``__pycache__`` out of the collateral-damage
    check.
    """
    import tempfile

    cache = tempfile.mkdtemp(prefix="mutation-pycache-")
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = cache
    try:
        # start_new_session puts the suite in ITS OWN process group. Without
        # it a mutant that signals "its" process group signals the HARNESS
        # too -- which is not hypothetical: mutating away the execution
        # layer's `setsid` makes every timeout in the suite kill the caller's
        # group, and the caller is this harness. The run then dies looking
        # like an infrastructure problem rather than like the mutation doing
        # exactly what it was written to do.
        select = ["-k", " or ".join(only)] if only else []
        proc = subprocess.run(
            [python, "-m", "pytest", *suites, "-q", "--no-header", "-x",
             "--tb=no", "-p", "no:randomly", *select],
            cwd=str(ROOT), capture_output=True, text=True,
            timeout=SUITE_TIMEOUT_S, start_new_session=True, env=env)
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    return SuiteRun(proc.returncode, *_summary(proc.stdout))


#: What a fresh interpreter runs to say whether a file loads at all. A
#: package module is imported by its dotted name; any other file is run under
#: a name that is not ``__main__``, so a script's main block does not run.
_LOADER = r"""
import importlib, runpy, sys
root, rel, dotted = sys.argv[1:4]
sys.path.insert(0, root)
try:
    if dotted:
        importlib.import_module(dotted)
    else:
        runpy.run_path(root + "/" + rel, run_name="__mutation_load_check__")
except BaseException as exc:
    text = (type(exc).__name__ + ": " + str(exc)).replace("\n", " ")
    print("LOAD-FAILED " + text[:300])
    raise SystemExit(1)
print("LOADED")
"""

#: Backstop for the load check, as SUITE_TIMEOUT_S is for a suite.
LOAD_TIMEOUT_S = 120


def _dotted(rel: str) -> str:
    """``rel``'s module name, as the suites import it, when the directory it
    is in is a package; else the empty string, for a file run on its own. A
    directory above that package with no ``__init__.py`` is a namespace
    package, which is how the suites import ``tools/grounding_bench``."""
    path = Path(rel)
    if not (ROOT / path.parent / "__init__.py").is_file():
        return ""
    parts = path.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def load_problem(rel: str, python: str) -> str | None:
    """None if the file at ``rel`` loads in a fresh ``python``, else what
    loading it raised (AUD-093).

    A SUITE THAT IMPORTS A MODULE INSIDE ITS TESTS CANNOT SAY THIS. A mutant
    whose module fails at import -- a dataclass field with a default made to
    precede one without, say -- fails every test that reaches the import, and
    each failure names a test. The kill reads as that test's, and the test
    never saw the fault. With a private bytecode prefix, for the reason
    ``run_suite`` gives.
    """
    import tempfile

    cache = tempfile.mkdtemp(prefix="mutation-load-pycache-")
    env = dict(os.environ, PYTHONPYCACHEPREFIX=cache)
    try:
        proc = subprocess.run(
            [python, "-c", _LOADER, str(ROOT), rel, _dotted(rel)],
            cwd=str(ROOT), capture_output=True, text=True,
            timeout=LOAD_TIMEOUT_S, start_new_session=True, env=env)
    except subprocess.TimeoutExpired:
        return f"loading it did not finish in {LOAD_TIMEOUT_S}s"
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    if proc.returncode == 0 and proc.stdout.rstrip().endswith("LOADED"):
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("LOAD-FAILED "):
            return line[len("LOAD-FAILED "):]
    return f"the interpreter loading it exited {proc.returncode}"


#: A star import binds names no symbol table lists, so a source holding one
#: cannot be told. It can only stand at module level, so a line that begins
#: with one is enough; a docstring line that reads like one makes the rule
#: pass that source by, and no mutated file holds such a line.
_STAR_IMPORT = re.compile(r"(?m)^[ \t]*from[ \t]+[\w.]+[ \t]+import[ \t]+\*")

#: Names every module has without binding them.
_MODULE_NAMES = frozenset({
    "__name__", "__file__", "__doc__", "__spec__", "__loader__",
    "__package__", "__builtins__", "__path__", "__cached__",
    "__annotations__", "__dict__", "__debug__"})


def names_nothing_defines(source: str) -> frozenset | None:
    """The global names ``source``'s code reads that it never binds and that
    builtins do not supply, or None where that cannot be told: a source
    this interpreter does not parse, or a star import.

    Only ever compared (AUD-093). A name read in code that never runs is in
    it all the same, so what matters is a name a mutant ADDS: read by the
    mutated code, defined nowhere.
    """
    if _STAR_IMPORT.search(source):
        return None
    try:
        top = symtable.symtable(source, "<mutant>", "exec")
    except (SyntaxError, ValueError):
        return None
    bound, read = set(), set()

    def walk(table, module: bool) -> None:
        for s in table.get_symbols():
            binds = s.is_assigned() or s.is_imported() or s.is_namespace()
            if module:
                if binds:
                    bound.add(s.get_name())
                elif s.is_referenced():
                    read.add(s.get_name())
            else:
                if s.is_declared_global() and binds:
                    bound.add(s.get_name())
                if s.is_global() and s.is_referenced():
                    read.add(s.get_name())
        for child in table.get_children():
            walk(child, False)

    walk(top, True)
    return frozenset(read - bound - set(dir(builtins)) - _MODULE_NAMES)


#: Every field a mutation must carry. ``rationale`` is required because a
#: SURVIVED line prints it, and a survivor whose report cannot say what was
#: lost is a finding nobody can act on.
_MUTATION_FIELDS = ("name", "path", "find", "replace", "rationale")

#: The one field a mutation may add: why its mutant may read a name nothing
#: defines. For a mutant whose code never runs -- a call a static guard has
#: to find -- and for nothing else (AUD-093).
_READS_UNDEFINED = "reads_undefined"


def validate_spec(spec) -> list:
    """Return every structural problem with a specification. Empty is good."""
    problems: list = []
    if not isinstance(spec, dict):
        return [f"specification is {type(spec).__name__}, not an object"]
    for key in ("title", "suites", "mutations"):
        if key not in spec:
            problems.append(f"missing top-level key {key!r}")
    suites = spec.get("suites")
    if not isinstance(suites, list) or not suites:
        problems.append("'suites' must be a non-empty list of test paths")
    else:
        for suite in suites:
            if not isinstance(suite, str) or not suite:
                problems.append(f"suite entry {suite!r} is not a path")
            elif not (ROOT / suite).exists():
                problems.append(f"suite {suite!r} does not exist")
    mutations = spec.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        problems.append("'mutations' must be a non-empty list")
        return problems
    seen: set = set()
    made: dict = {}
    sources: dict = {}
    undefined: dict = {}
    for i, m in enumerate(mutations):
        where = f"mutation {i}"
        if not isinstance(m, dict):
            problems.append(f"{where} is {type(m).__name__}, not an object")
            continue
        for field in _MUTATION_FIELDS:
            if not isinstance(m.get(field), str) or not m.get(field):
                problems.append(
                    f"{where} ({m.get('name', '?')}): {field!r} must be a "
                    "non-empty string")
        name = m.get("name")
        if name in seen:
            problems.append(
                f"duplicate mutation name {name!r}; a report naming it would "
                "not say which mutation it describes")
        seen.add(name)
        if m.get("find") == m.get("replace"):
            problems.append(
                f"{where} ({name}): 'find' and 'replace' are identical, so "
                "the mutation changes nothing and is killed by nothing")
        path = m.get("path")
        if isinstance(path, str) and path and not (ROOT / path).exists():
            problems.append(f"{where} ({name}): {path!r} does not exist")
            continue
        # TWO ENTRIES, ONE MUTANT (AUD-087). Anchors that differ can make the
        # same source, and re-anchoring onto a rule two checks were merged
        # into does exactly that: the run then counts one fault twice and
        # reports a second fault covered that is the first.
        mutant = _mutant(path, m.get("find"), m.get("replace"), sources)
        if mutant is not None:
            if mutant in made:
                problems.append(
                    f"{where} ({name}) makes the same mutant as "
                    f"{made[mutant]!r}, so the run would count one fault "
                    "twice; give it a fault of its own, or fold its "
                    "rationale into the other")
            else:
                made[mutant] = name
        # A MUTANT THAT CRASHES WHERE ITS FAULT WAS TO ACT (AUD-093). An
        # entry that calls a function its module never imported, or that
        # takes away the import of a name the module still uses, raises
        # NameError on the path it was written to change. Every test that
        # reaches the path fails, so it is counted as killed, and no test saw
        # the fault: one such entry was killed by its NameError from the day
        # it was written. A crash the runner catches is worse still: the test
        # fails on what the crash did, and names itself.
        why = m.get(_READS_UNDEFINED)
        if why is not None and not (isinstance(why, str) and why.strip()):
            problems.append(f"{where} ({name}): {_READS_UNDEFINED!r} must "
                            "say why, as a non-empty string")
            why = None
        if mutant is not None and str(path).endswith(".py"):
            added = _names_added(path, m["find"], m["replace"], sources,
                                 undefined)
            if added and why is None:
                problems.append(
                    f"{where} ({name}): the mutant reads {sorted(added)}, "
                    f"which nothing in {path} defines, so the code it changes "
                    "raises NameError where its fault was to act, and a kill "
                    "would be that crash; bind the name, or say in "
                    f"{_READS_UNDEFINED!r} why the mutant never runs it")
            elif added is not None and not added and why is not None:
                problems.append(
                    f"{where} ({name}) says in {_READS_UNDEFINED!r} that its "
                    "mutant reads a name nothing defines, and it reads none")
    return problems


def _names_added(path, find, replace, sources: dict, undefined: dict):
    """The names the mutant of ``path`` reads that nothing defines and its
    original does not read, or None where that cannot be told."""
    if path not in undefined:
        src = sources.get(path)
        undefined[path] = None if src is None else names_nothing_defines(src)
    before = undefined[path]
    if before is None:
        return None
    after = names_nothing_defines(sources[path].replace(find, replace))
    return None if after is None else after - before


def _mutant(path, find, replace, sources: dict):
    """A digest of the source ``find`` and ``replace`` make of ``path``, or
    None where a rule above already objects or the anchor does not match
    exactly once, which the run reports itself."""
    if not all(isinstance(x, str) and x for x in (path, find, replace)) \
            or find == replace:
        return None
    if path not in sources:
        try:
            sources[path] = (ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            sources[path] = None
    src = sources[path]
    if src is None or src.count(find) != 1:
        return None
    return hashlib.sha256(
        (path + "\0" + src.replace(find, replace)).encode("utf-8")
    ).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", type=Path, nargs="?",
                    help="mutation specification (JSON)")
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter used to run the suite")
    ap.add_argument("--list", action="store_true",
                    help="print the mutations and exit without running them")
    ap.add_argument("--recover", action="store_true",
                    help="restore sources from a previous run's recovery file")
    ap.add_argument("--null-only", action="store_true",
                    help="run the baseline and the null control and stop; "
                         "a fast pre-flight over every specification, which "
                         "answers 'would a kill here mean anything?' without "
                         "paying for the mutations themselves")
    args = ap.parse_args()

    if args.recover:
        if not RECOVERY.exists():
            print("nothing to recover; no interrupted run recorded")
            return 0
        saved = json.loads(RECOVERY.read_text(encoding="utf-8"))
        for rel, text in saved.items():
            path = ROOT / rel
            if path.read_text(encoding="utf-8") != text:
                print(f"  restored {rel}")
                path.write_text(text, encoding="utf-8")
            else:
                print(f"  already clean: {rel}")
        RECOVERY.unlink()
        print("sources restored from the interrupted run")
        return 0

    if args.spec is None:
        ap.error("a mutation specification is required unless --recover")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    problems = validate_spec(spec)
    if problems:
        # Checked BEFORE the baseline and before the recovery sidecar exists,
        # so a typo in a specification can never get as far as writing to a
        # source file. A KeyError in the middle of the loop fails closed too,
        # but it fails closed with a traceback, on a tree that has already
        # been written to once.
        print(f"refusing to run {args.spec}:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
    suites = spec["suites"]
    mutations = spec["mutations"]
    name_w = max(len(m["name"]) for m in mutations)

    if args.list:
        for m in mutations:
            print(f"{m['name']:{name_w}s}  {m['path']}  -- {m['rationale']}")
        return 0

    print(f"{spec['title']}\n{'=' * len(spec['title'])}")
    print(f"{len(mutations)} mutations over {len(suites)} suite(s)\n")

    # --- 1. the baseline must be green, and STABLY so ---------------------
    base = run_suite(suites, args.python)
    if base.rc != 0:
        # A red baseline is refused; a FLAKY one is refused separately and
        # loudly. The distinction matters because a baseline that can flake
        # red can also flake green, and a matrix run on a green flake reports
        # kills for mutations nothing actually tested. This branch exists
        # because a baseline went red once, in a suite that then passed on
        # every one of the next several runs, and an unexplained transient in
        # the one check that makes every other result meaningful is not
        # something to run past.
        again = run_suite(suites, args.python)
        if again.rc == 0:
            print("BASELINE NON-DETERMINISTIC -- refusing.")
            print(f"The first run failed ({base.said()}) and an immediate "
                  "re-run passed.")
            print("A baseline that can flake red can flake green, and a "
                  "matrix run on a green flake reports kills for mutations "
                  "that nothing tested. Fix the flake first.")
            return 4
        print("BASELINE RED -- mutation results would be meaningless.")
        print("Every mutation would 'fail the suite' for this pre-existing")
        print("reason and the report would read as a perfect score: "
              f"{base.said()}")
        return 2
    print("baseline green")

    paths = sorted({m["path"] for m in mutations})
    original = {p: (ROOT / p).read_text(encoding="utf-8") for p in paths}
    before = {p: _sha(ROOT / p) for p in paths}

    # Which files the load check can speak for: those that load unmutated.
    loadable = set()
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        problem = load_problem(rel, args.python)
        if problem is None:
            loadable.add(rel)
        else:
            print(f"load check SKIPPED for {rel}: unmutated, it does not load "
                  f"in a fresh interpreter on its own ({problem})")

    if RECOVERY.exists():
        print(f"REFUSING TO START: {RECOVERY.name} exists, so a previous run "
              "died without restoring its sources. They may still be mutated "
              "-- and a leftover mutation is a DISABLED SAFETY GUARD that no "
              "test is currently reporting.")
        print("Restore them with:")
        print(f"    python3 {Path(__file__).name} --recover")
        return 3
    RECOVERY.write_text(json.dumps(original, indent=2), encoding="utf-8")

    # BEFORE THE NULL CONTROL, not after it (D-2026-82). The null control is
    # the first thing that writes a target, and it used to run before the
    # sidecar existed and before the handler below was installed: a run
    # stopped during it left the control's comment in the source, wrote no
    # sidecar, and --recover said "nothing to recover". Everything that
    # writes a target now happens inside the one try/finally that restores.
    #
    # SIGTERM does not run `finally`, so a harness killed by a timeout or a
    # CI cancellation would leave a MUTATED source on disk -- a disabled safety
    # guard, committed by whoever ran `git add -A` next. Turning the signal
    # into an exception makes the restore path run. This came up for real: a
    # run was SIGTERMed mid-matrix and the sources survived only because the
    # kill happened to land between mutations.
    def _restore_and_die(signum, frame):
        for rel, text in original.items():
            (ROOT / rel).write_text(text, encoding="utf-8")
        print(f"\ninterrupted by signal {signum}; sources restored")
        raise SystemExit(130)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _restore_and_die)
        except (ValueError, OSError):          # pragma: no cover - platform
            pass

    results: dict = {}
    collateral: dict = {}
    try:
        # --- 1b. THE NULL CONTROL
        # ---------------------------------------------
        #
        # A green baseline says the suites pass on the unmutated tree. It does
        # NOT say that a kill means anything. If any test in these suites fails
        # whenever the mutated FILE changes -- because it hashes the source, or
        # compares it against a manifest, a crate, an allowlist or a checked-in
        # digest -- then every mutation in this specification is killed by the
        # edit rather than by the behaviour removed, and the report reads as a
        # perfect score for a matrix that measured nothing.
        #
        # That is the same defect this harness already refuses in its
        # red-baseline branch, one step along: there the suite fails for ALL
        # mutations because it was already failing; here it fails for all
        # mutations because it cannot tell a semantic change from a byte
        # change. A baseline check cannot see it, because the baseline tree is
        # unedited by construction.
        #
        # So: edit each file in a way that changes its bytes and nothing else,
        # and require the suites to stay green. A comment appended to a Python
        # or shell source is behaviour-preserving by definition of the
        # language; if the suites go red under it, they are measuring the
        # bytes.
        for rel in paths:
            if pathlib.Path(rel).suffix not in (".py", ".sh"):
                # Only claim it where a trailing comment is guaranteed inert.
                print(f"null control SKIPPED for {rel}: no comment syntax "
                      "this harness is willing to assume is "
                      "behaviour-preserving")
                continue
            (ROOT / rel).write_text(
                original[rel] + "\n# mutation-harness null control: this line "
                "changes the bytes of this file and nothing it does\n",
                encoding="utf-8")
            try:
                null = run_suite(suites, args.python)
            except subprocess.TimeoutExpired:
                null = SuiteRun(1, ["(timeout)"], [], [])
            finally:
                (ROOT / rel).write_text(original[rel], encoding="utf-8")
            if null.rc != 0:
                print("NULL CONTROL RED -- mutation results would be "
                      "meaningless.")
                print(f"Appending a comment to {rel} -- a change with no "
                      "effect on what it does -- turned this specification's "
                      f"suites red: {null.said()}")
                print("Every mutation here would then be 'killed' by having "
                      "edited the file, and the report would read as a "
                      "perfect score. Take the byte-sensitive test out of the "
                      "suite list or out of this specification.")
                return 5
        print(f"null control green on {len(paths)} file(s); a kill here is a "
              "kill for the behaviour, not for the edit\n")

        if args.null_only:
            return 0
        print("mutating\n")

        # A mutation that disables a safety guard lets the SUITE damage files
        # the harness never touched. Verifying only that the mutated sources
        # were restored misses that entirely -- and it happened: removing the
        # workspace write guard let a test replace a 261-line governed README
        # with one line of forged text, and the corrupted file was nearly
        # committed alongside a manifest regenerated over it. The tracked
        # working tree is therefore checked after every mutation, not just the
        # files being mutated.
        baseline_dirty = set(_dirty_tracked_files())

        for m in mutations:
            name, path = m["name"], m["path"]
            src = original[path]
            found = src.count(m["find"])
            if found != 1:
                print(f"{name:{name_w}s}  ANCHOR DRIFT  matched {found}x in "
                      f"{path}")
                results[name] = "ANCHOR_DRIFT"
                continue

            (ROOT / path).write_text(src.replace(m["find"], m["replace"]),
                                     encoding="utf-8")
            problem = run = None
            try:
                if path in loadable:
                    problem = load_problem(path, args.python)
                if problem is None:
                    run = run_suite(suites, args.python)
            except subprocess.TimeoutExpired:
                # Counted as killed -- the suite did not go green -- but
                # flagged, because a test that bounds its own blocking calls
                # would have said WHICH check was lost, in seconds.
                print(f"{name:{name_w}s}  KILLED (TIMEOUT) <-- no test bounds "
                      f"this; it cost {SUITE_TIMEOUT_S}s of wall clock")
                results[name] = "KILLED_BY_TIMEOUT"
                continue
            finally:
                (ROOT / path).write_text(src, encoding="utf-8")
                damaged = [f for f in _dirty_tracked_files()
                           if f not in baseline_dirty and f != path]
                if damaged:
                    collateral[name] = damaged
                    _restore(damaged)

            if problem is not None:
                print(f"{name:{name_w}s}  STILLBORN   <-- the mutant does not "
                      f"load ({problem}), so no test ran against it")
                results[name] = "STILLBORN"
            elif run is None:
                # Not reached: a mutant that loads is run, and a run that
                # timed out has already gone on to the next mutant.
                raise RuntimeError(f"{name}: its suites were not run")
            elif run.rc < 0:
                # A SIGNAL IS NOT A KILL EITHER. The suite's process ended
                # without a verdict from any test: a native library that
                # faulted in it, or a process group signalled from inside.
                # A test that ran the same thing in a process of its own
                # would have said which check was lost.
                try:
                    how = signal.Signals(-run.rc).name
                except ValueError:
                    how = f"signal {-run.rc}"
                print(f"{name:{name_w}s}  KILLED BY A SIGNAL ({how}) <-- no "
                      "test named the check that was lost")
                results[name] = "KILLED_BY_SIGNAL"
            elif run.named:
                by = run.named[0] + ("" if run.failed
                                     else " (an error, not a failure)")
                print(f"{name:{name_w}s}  KILLED        by {by}")
                results[name] = "KILLED"
            elif run.uncollected:
                files = ", ".join(run.uncollected)
                print(f"{name:{name_w}s}  STILLBORN   <-- the suites could "
                      f"not be collected under it ({files}), so no test ran "
                      "against it")
                results[name] = "STILLBORN"
            elif run.rc != 0:
                print(f"{name:{name_w}s}  NO VERDICT  <-- pytest exited "
                      f"{run.rc} and named no test")
                results[name] = "NO_VERDICT"
            else:
                # "NOTHING DETECTS THIS" IS NOT WHAT WAS MEASURED.
                #
                # What was measured is that the suites in THIS specification
                # went green with the check removed. A test that would have
                # caught it may exist in a suite the spec does not list, and
                # that is not a hypothetical: four survivors in
                # `agent_second_reader` were reported this way while a test
                # covering two of them sat in tests/test_agent_substrate.py,
                # which that spec does not run (D-2026-36). The stronger
                # sentence sent the reader looking for an unprotected check
                # instead of a mis-scoped spec, and both are real findings
                # with different repairs.
                print(f"{name:{name_w}s}  SURVIVED    <-- no test in this "
                      f"spec's suites detects this: {m['rationale']}")
                results[name] = "SURVIVED"
    finally:
        for p, src in original.items():
            (ROOT / p).write_text(src, encoding="utf-8")
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):      # pragma: no cover - platform
                pass
        RECOVERY.unlink(missing_ok=True)

    # --- 2. restoration is verified, not assumed --------------------------
    drifted = [p for p in paths if _sha(ROOT / p) != before[p]]
    survived = [k for k, v in results.items() if v == "SURVIVED"]
    anchors = [k for k, v in results.items() if v == "ANCHOR_DRIFT"]
    timeouts = [k for k, v in results.items() if v == "KILLED_BY_TIMEOUT"]
    stillborn = [k for k, v in results.items() if v == "STILLBORN"]
    signalled = [k for k, v in results.items() if v == "KILLED_BY_SIGNAL"]
    unnamed = [k for k, v in results.items() if v == "NO_VERDICT"]
    # A TIMEOUT IS NOT A KILL, AND THE COUNT SAYS SO.
    #
    # It used to be counted as one and reported as a note, so a mutation
    # nothing bounded appeared in the score as coverage -- while costing
    # SUITE_TIMEOUT_S of wall clock per run and saying nothing about which
    # check was lost. Two were in that state and were bounded by hand; what
    # was missing was anything to stop a third. This is that: a timeout-only
    # kill is excluded from the count AND fails the run.
    killed = sum(1 for v in results.values() if v == "KILLED")

    print()
    print(f"killed:   {killed}/{len(mutations)}")
    if timeouts:
        print(f"KILLED ONLY BY TIMEOUT (not counted as coverage; write a "
              f"bounded test): {timeouts}")
    if stillborn:
        print("STILLBORN (the mutant does not load, or the suites cannot be "
              "collected under it; it tested nothing and is not counted): "
              f"{stillborn}")
    if signalled:
        print("KILLED ONLY BY A SIGNAL (not counted as coverage; a test that "
              "runs it in a process of its own would name the check): "
              f"{signalled}")
    if unnamed:
        print(f"NO VERDICT (pytest failed and named no test; not counted): "
              f"{unnamed}")
    if survived:
        print(f"SURVIVED: {survived}")
        print(f"  (not detected by: {suites}. Before recording an "
              "unprotected check, confirm no suite OUTSIDE this list "
              "already covers it -- a mis-scoped spec and a missing test "
              "look identical from here.)")
    if anchors:
        print(f"ANCHOR DRIFT (tested nothing): {anchors}")
    if collateral:
        # WHO CHANGED THE FILE IS NOT SOMETHING THIS CAN SEE. All it
        # observed is that a tracked file differed from the snapshot taken
        # before this mutation ran. The usual cause is a test writing
        # outside its tmp_path -- and it said so as a fact, naming the test,
        # which is a claim it cannot support: an editor, a regeneration
        # script or another shell touching the tree during the run produces
        # exactly the same observation. That misattribution sent a reader
        # hunting an innocent test once, which is the whole cost of stating
        # an inference as a finding (D-2026-43).
        print("TRACKED FILES CHANGED DURING A MUTATION (restored; discarded "
              "content kept under .mutation-quarantine/). If a test wrote "
              "them it must undo its own writes in a finally; if something "
              "outside this run did, nothing here can tell them apart:")
        for name, files in sorted(collateral.items()):
            print(f"  changed while {name} was running: {files}")
    if drifted:
        print(f"SOURCES NOT RESTORED: {drifted}")
    else:
        print("all sources restored byte-identical (verified by re-hashing)")

    still_dirty = [f for f in _dirty_tracked_files()
                   if f not in baseline_dirty]
    if still_dirty:
        print(f"WORKING TREE LEFT DIRTY: {still_dirty}")

    # --- 3. the baseline must STILL be green ------------------------------
    # Byte-identical restoration is necessary and not sufficient: a mutation
    # can leave behind state the source hash cannot see -- a data file a test
    # wrote, a cache, a directory that now exists. Re-running the baseline
    # asks the only question that matters, which is whether the tree the
    # harness hands back still behaves like the one it was given.
    post = run_suite(suites, args.python)
    if post.rc != 0:
        print("POST-RUN BASELINE RED -- the matrix left this tree in a state "
              f"the suite rejects: {post.said()}")
        # WHICH OF THREE EXPLANATIONS, rather than asserting one.
        #
        # This block used to print "something outside the mutated sources
        # survived the run" as a conclusion. It is a hypothesis, and a
        # NONDETERMINISTIC TEST produces the identical signature: green
        # before, red after, sources byte-identical, nothing left behind.
        # That is not hypothetical either -- it happened on four hosted runs
        # of this spec, and the message sent the reader hunting a stray file
        # that was never there (D-2026-34).
        #
        # So the harness asks one more question it can actually answer: does
        # the named test still fail on the tree as restored? Re-running it
        # alone cannot distinguish nondeterminism from suite-order coupling,
        # and the message says so instead of picking one.
        recheck = run_suite(suites, args.python, only=post.named)
        if recheck.rc == 0:
            print("...but re-running those tests alone on the SAME restored "
                  "tree passes. The tree is not simply broken: either the "
                  "test is nondeterministic or it depends on state the rest "
                  "of the suite sets up. Do not conclude that something "
                  "survived the run without evidence of the thing.")
        else:
            print("...and it fails again on the restored tree, in isolation. "
                  "Restoration was byte-identical but not complete; look for "
                  "state outside the mutated sources -- a data file a test "
                  "wrote, a cache, a directory that now exists.")
    return 0 if not (survived or anchors or timeouts or stillborn or signalled
                     or unnamed or drifted or collateral or still_dirty
                     or post.rc) else 1


if __name__ == "__main__":
    raise SystemExit(main())
