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

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Prefixes never worth scanning even when git would list them. Kept short
#: on purpose: every entry is a hole, so each one has to earn its place.
#:
#: ``attic/`` is the one that does the work here, because it is TRACKED --
#: it is the retired corpus, kept for history and excluded from linting for
#: the same reason. The others are belt-and-braces: git already ignores
#: .venv and build output, so ``--exclude-standard`` drops them before this
#: list is consulted.
_SKIP = (".venv/", "attic/", "build/", "dist/", "node_modules/")


def repository_files(pattern: str = "*", *, root: Path | None = None,
                     include_tests: bool = True) -> tuple:
    """Repo-relative paths git considers present and not ignored.

    ``pattern`` is a git pathspec, e.g. ``"*.py"``. Set ``include_tests``
    False for guards that ask about production code specifically.
    """
    base = Path(root or ROOT)
    r = subprocess.run(
        ["git", "-C", str(base), "ls-files", "--cached", "--others",
         "--exclude-standard", "--", pattern],
        capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        rel = line.strip()
        if not rel or rel.startswith(_SKIP):
            continue
        if not include_tests and (rel.startswith("tests/")
                                  or "/tests/" in rel):
            continue
        out.append(rel)
    return tuple(sorted(set(out)))


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
