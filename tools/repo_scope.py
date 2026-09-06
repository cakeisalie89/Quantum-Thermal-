"""What counts as "a file in this repository", asked once.

WHY THIS IS A MODULE AND NOT A ONE-LINER IN EACH GUARD

Three separate pushes went red for the same reason. A structural guard
asked ``git grep``, which sees TRACKED files only; a new test importing
something it should not was invisible until it was staged; the suite went
green locally and the tree that got pushed was broken. Each time the fix
was local and the next new file re-armed the trap.

The mistake was in the question. "Tracked" is not the set these guards
mean. A file that is untracked and NOT ignored is not a scratch file -- it
is a file that will be part of the repository the moment anybody commits,
which is exactly when the guard would start failing. An IGNORED file is a
different thing entirely: a quarantined mutation copy or a build artifact
cannot make the repository's import graph wrong, and sweeping it in would
make these guards fail for reasons that have nothing to do with the code.

So the set is what git itself calls the working tree minus ignored files:

    git ls-files --cached --others --exclude-standard

which is the same rule ``generate_manifest.py --check`` applies when it
says a file "would be required in the manifest the moment it is
committed". One rule, two places, same reason.

WHAT DELIBERATELY STAYS TRACKED-ONLY

The manifest and the release artifacts. Those describe what a RELEASE
contains, and a release contains committed files -- an uncommitted file is
not part of it, however close to being so. That is a different question
with a different right answer, and conflating the two would put
uncommitted work into a released manifest.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

class RepositoryScopeUnavailable(RuntimeError):
    """The file set could not be established, so no guard may run over it.

    RAISED RATHER THAN RETURNING AN EMPTY SET, which is the whole point.

    This was a real hole, and the container found it. `git ls-files` in a
    directory that is not a repository exits 128 and prints nothing to
    stdout; the old code returned () and every structural guard built on it
    passed having examined ZERO files. A verifier that reports success over
    an empty population is worse than no verifier: it produces a green
    result and a false sense that the property was checked.

    So an unanswerable scope is an error. A caller that can legitimately
    proceed without it must say so explicitly, and say why.
    """


#: The two scopes, kept apart because they answer different questions and
#: have different right answers.
#:
#: PRECOMMIT_STRUCTURAL  tracked + untracked-unignored: everything that will
#:                       be in the repository the moment anybody commits.
#:                       This is what a structural guard means, because a
#:                       file one `git add` away can already violate the
#:                       property. Needs git.
#: COMMITTED_RELEASE     what a release actually contains: committed files
#:                       only. An uncommitted file is not part of a release
#:                       however close to being one. Answerable from the
#:                       manifest alone, so it survives an environment with
#:                       no git and no .git -- which is exactly the
#:                       container.
PRECOMMIT_STRUCTURAL = "PRECOMMIT_STRUCTURAL"
COMMITTED_RELEASE = "COMMITTED_RELEASE"

#: Below this, something is wrong with the scan rather than with the
#: repository. A guard reporting success over three files has not checked
#: what it claims to check.
MIN_PLAUSIBLE_PY_FILES = 50

#: Prefixes never worth scanning even when git would list them. Kept short
#: on purpose: every entry is a hole, so each one has to earn its place.
#:
#: ``attic/`` is the one that does the work here, because it is TRACKED --
#: it is the retired corpus, kept for history and excluded from linting for
#: the same reason. The others are belt-and-braces: git already ignores
#: .venv and build output, so ``--exclude-standard`` drops them before this
#: list is consulted.
_SKIP = (".venv/", "attic/", "build/", "dist/", "node_modules/")


def _filter(names, pattern: str, include_tests: bool) -> tuple:
    import fnmatch

    out = []
    for rel in names:
        rel = rel.strip()
        if not rel or rel.startswith(_SKIP):
            continue
        if not include_tests and (rel.startswith("tests/")
                                  or "/tests/" in rel):
            continue
        if pattern != "*" and not fnmatch.fnmatch(rel, pattern) \
                and not fnmatch.fnmatch(Path(rel).name, pattern):
            continue
        out.append(rel)
    return tuple(sorted(set(out)))


def committed_release_files(pattern: str = "*", *,
                            root: Path | None = None,
                            include_tests: bool = True) -> tuple:
    """What a RELEASE contains, from the manifest rather than from git.

    The manifest is the committed inventory: generate_manifest.py builds it
    from tracked files and refuses to be stale, so it answers this question
    in an environment that has no git and no .git -- which is precisely the
    container, where the governance suite otherwise cannot run at all.
    """
    base = Path(root or ROOT)
    manifest = base / "final_manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepositoryScopeUnavailable(
            f"the committed inventory at {manifest} could not be read "
            f"({exc}); a guard over an unknown file set must not run") \
            from exc
    files = data.get("files")
    if not isinstance(files, (dict, list)) or not files:
        raise RepositoryScopeUnavailable(
            f"{manifest} lists no files; refusing to report success over an "
            "empty population")
    # The manifest's entries carry "filename", not "path". Reading the wrong
    # key returned an empty list, which the anti-vacuity assertion is there
    # to catch -- and did, immediately.
    if isinstance(files, dict):
        names = list(files)
    else:
        names = [f.get("filename") or f.get("path")
                 for f in files if isinstance(f, dict)]
    found = _filter([n for n in names if n], pattern, include_tests)
    if not found:
        raise RepositoryScopeUnavailable(
            f"the committed inventory matched no {pattern!r}; a guard over an "
            "empty population reports success without checking anything")
    return found


def repository_files(pattern: str = "*", *, root: Path | None = None,
                     include_tests: bool = True,
                     scope: str = PRECOMMIT_STRUCTURAL) -> tuple:
    """Repo-relative paths in the requested SCOPE.

    ``pattern`` is a glob, e.g. ``"*.py"``. Set ``include_tests`` False for
    guards that ask about production code specifically.

    Raises :class:`RepositoryScopeUnavailable` rather than returning an
    empty tuple when the scope cannot be established -- see that class for
    the hole this closes.
    """
    base = Path(root or ROOT)
    if scope == COMMITTED_RELEASE:
        return committed_release_files(pattern, root=base,
                                       include_tests=include_tests)
    if scope != PRECOMMIT_STRUCTURAL:
        raise RepositoryScopeUnavailable(f"unknown scope {scope!r}")
    try:
        r = subprocess.run(
            ["git", "-C", str(base), "ls-files", "--cached", "--others",
             "--exclude-standard", "--", pattern],
            capture_output=True, text=True)
    except OSError as exc:
        raise RepositoryScopeUnavailable(
            f"git is not available, so the pre-commit structural scope "
            f"cannot be established ({exc}). Use COMMITTED_RELEASE if the "
            "question is about released content instead") from exc
    if r.returncode != 0:
        raise RepositoryScopeUnavailable(
            f"git could not enumerate {base} (exit {r.returncode}: "
            f"{r.stderr.strip()[:200]}). This is the container's case -- git "
            "present, .git absent -- and it used to return an empty set, so "
            "every structural guard passed having examined nothing")
    return _filter(r.stdout.splitlines(), pattern, include_tests)


def assert_scope_is_plausible(files, *, what: str = "python files",
                              minimum: int = MIN_PLAUSIBLE_PY_FILES) -> None:
    """Refuse a suspiciously small population.

    ANTI-VACUITY. A guard that reports success is claiming it looked; this
    is the assertion that it looked at something. Cheap, and it turns the
    quiet failure -- a scan that matched nothing and said "all clear" --
    into a loud one.
    """
    if len(files) < minimum:
        raise RepositoryScopeUnavailable(
            f"only {len(files)} {what} were found, below the {minimum} this "
            "repository is known to contain. A verifier reporting success "
            "over a population this small has not checked what it claims to")


def files_matching(regex: str, pattern: str = "*.py", *,
                   root: Path | None = None,
                   include_tests: bool = True) -> tuple:
    """Files whose CONTENT matches ``regex``.

    Reads in Python rather than shelling out to ``git grep`` so the file
    set and the search agree: git grep would apply its own idea of which
    files exist, which is the disagreement this module exists to remove.
    """
    base = Path(root or ROOT)
    rx = re.compile(regex, re.MULTILINE)
    hits = []
    for rel in repository_files(pattern, root=base,
                                include_tests=include_tests):
        try:
            body = (base / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:                              # pragma: no cover
            continue
        if rx.search(body):
            hits.append(rel)
    return tuple(hits)


def non_test_references(symbol: str, *, root: Path | None = None) -> tuple:
    """Production files mentioning ``symbol``.

    The question behind "does this defence have a caller that is not its
    own test", which this project has had to ask three times: an egress
    composition check reachable only from its own tests, a result field
    populated by nothing, and a projection method with no callers at all.
    """
    return files_matching(re.escape(symbol), root=root, include_tests=False)
