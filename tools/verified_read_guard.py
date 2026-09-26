"""Nothing outside the event log reads the log without verifying it.

WHY THIS IS A STANDING CHECK AND NOT A FIXED LIST OF SITES

D-2026-70 found seventeen reducers that verified one read of the log and
folded another, three helpers that read it with no verification at all, and
a primitive (``read_from``) whose correct use needed a second primitive and a
promise. They were repaired, and ``tests/test_agent_snapshot_coherence.py``
proves the repaired sites -- by name, from a table. A table proves the
reducers that exist. It says nothing about the next one somebody writes.

So this walks the parse tree of every tracked production file and refuses,
outside ``qta_agent/events.py`` (where the primitives live):

* ``RAW_READ``      -- ``<log>.read(...)``, or iterating ``<log>`` (``for ev
                       in log``, ``list(log)``): an unverified parse;
* ``TAIL_PARSE``    -- ``<anything>._read_tail(...)``: the tail parser that
                       ``verify_from`` + a second read used to pair with;
* ``READ_FROM``     -- any call or definition named ``read_from``: the removed
                       unverified-tail primitive, resurrected;
* ``FILE_READ``     -- opening or reading ``<log>.path`` directly: the same
                       thing with the EventLog API stepped around.

``<log>`` is decided syntactically, and the rule is stated rather than
hidden: an expression whose last name is ``log``, ends in ``_log``, or is a
call to ``EventLog(...)``, or a name assigned from such an expression in the
same function. That is a proxy for "this object is an EventLog", and aliasing
defeats it (``x = self.log; x.read()`` is caught; ``x = getattr(self, n)`` is
not). The runtime guard in ``EventLog.read`` is the complement: inside
``qta_agent`` it refuses any caller but ``qta_agent.events`` whatever the
caller named the object. Static scope is the whole tree; runtime scope is the
authority layer. Neither alone is the claim.

The allowlist is by (file, function, rule), each entry with its reason, and
the count is pinned by the test, so widening it is a reviewed change rather
than a quiet one.

NOT SCANNED: the Snakefile. Snakemake's rule syntax is not Python the ``ast``
module parses. Its four raw reads (in ``s10_governed`` and
``s10_governed_index``) are assertions over the run's history -- which
actions appear, that no egress grant exists, that a re-execution was
recorded -- and fold nothing into state; ``s10_governed_model`` reads only
through ``read_verified``. The runtime guard does not cover them either:
rule bodies are not ``qta_agent`` modules (D-2026-79).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Where the primitives are defined. Reading the log is its job.
EVENT_MODULE = "qta_agent/events.py"

#: (file, enclosing function, rule) -> why this site may do it.
ALLOWED = {
    ("tools/fuzz_substrate.py", "_log_reader", "RAW_READ"):
        "the fuzz harness's parser target: the unverified parse IS the "
        "subject under test, and nothing is folded into state",
}


def production_files(root: Path = ROOT) -> list:
    """Tracked + untracked-unignored .py outside tests/ and attic/."""
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached",
                        "--others", "--exclude-standard", "--", "*.py"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git could not enumerate {root}: {r.stderr[:200]}")
    return sorted(p for p in r.stdout.split("\0")
                  if p and not p.startswith(("tests/", "attic/")))


def _chain(node) -> list:
    """Names along an attribute chain: ``self.log`` -> ``[self, log]``."""
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        out.append(node.id)
    elif isinstance(node, ast.Call):
        f = node.func
        out.append((f.id if isinstance(f, ast.Name)
                    else getattr(f, "attr", "?")) + "()")
    else:
        out.append("?")
    return list(reversed(out))


def _is_logish(node, aliases: set) -> bool:
    chain = _chain(node)
    last = chain[-1]
    if last == "EventLog()":
        return True
    if len(chain) == 1 and last in aliases:
        return True
    return last == "log" or last.endswith("_log")


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.found: list = []
        self.func = "<module>"
        self.aliases: set = set()

    def _flag(self, node, rule: str, what: str) -> None:
        self.found.append((self.path, self.func, rule, node.lineno, what))

    def visit_FunctionDef(self, node):
        if node.name == "read_from":
            self._flag(node, "READ_FROM", "def read_from")
        saved = (self.func, self.aliases)
        self.func, self.aliases = node.name, set()
        self.generic_visit(node)
        self.func, self.aliases = saved

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        if _is_logish(node.value, self.aliases):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.aliases.add(t.id)
        self.generic_visit(node)

    def visit_For(self, node):
        if _is_logish(node.iter, self.aliases):
            self._flag(node, "RAW_READ", "for ... in " + ".".join(
                _chain(node.iter)))
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_comprehension(self, node):
        if _is_logish(node.iter, self.aliases):
            self._flag(node.iter, "RAW_READ", "comprehension over " +
                       ".".join(_chain(node.iter)))
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if (isinstance(f, ast.Name) and f.id in ("list", "iter", "tuple",
                                                 "sorted")
                and node.args and _is_logish(node.args[0], self.aliases)):
            self._flag(node, "RAW_READ", f"{f.id}(" + ".".join(
                _chain(node.args[0])) + ")")
        if isinstance(f, ast.Attribute):
            if f.attr == "read" and _is_logish(f.value, self.aliases):
                self._flag(node, "RAW_READ", ".".join(_chain(f)))
            elif f.attr == "_read_tail":
                self._flag(node, "TAIL_PARSE", ".".join(_chain(f)))
            elif f.attr == "read_from":
                self._flag(node, "READ_FROM", ".".join(_chain(f)))
            elif (f.attr in ("open", "read_bytes", "read_text")
                  and isinstance(f.value, ast.Attribute)
                  and f.value.attr == "path"
                  and _is_logish(f.value.value, self.aliases)):
                self._flag(node, "FILE_READ", ".".join(_chain(f)))
        elif isinstance(f, ast.Name) and f.id == "open" and node.args:
            a = node.args[0]
            if (isinstance(a, ast.Attribute) and a.attr == "path"
                    and _is_logish(a.value, self.aliases)):
                self._flag(node, "FILE_READ", "open(" + ".".join(_chain(a))
                           + ")")
        self.generic_visit(node)


def scan_source(src: str, path: str) -> list:
    """Every dangerous site in ``src``: (path, function, rule, line, what)."""
    v = _Visitor(path)
    v.visit(ast.parse(src))
    return v.found


def scan(root: Path = ROOT, files=None) -> tuple:
    """``(violations, allowed_hits, files_scanned)`` over the tree."""
    files = production_files(root) if files is None else files
    violations, allowed = [], []
    for rel in files:
        if rel == EVENT_MODULE:
            continue
        try:
            src = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for hit in scan_source(src, rel):
            key = (hit[0], hit[1], hit[2])
            (allowed if key in ALLOWED else violations).append(hit)
    return violations, allowed, len(files)


def main() -> int:
    violations, allowed, n = scan()
    if n < 100:
        print(f"only {n} production files found; the scan examined too "
              "little to say anything")
        return 1
    unused = set(ALLOWED) - {(h[0], h[1], h[2]) for h in allowed}
    if unused:
        print("allowlist entries that match nothing (stale): "
              f"{sorted(unused)}")
        return 1
    if violations:
        for path, func, rule, line, what in violations:
            print(f"{path}:{line} [{func}] {rule}: {what}")
        print(f"{len(violations)} unverified read(s) of the event log outside "
              "qta_agent/events.py. Fold from log.read_verified() / "
              "log.read_verified_from(anchor) instead, or allowlist the site "
              "with a reason.")
        return 1
    print(f"no unverified log read in {n} production files; "
          f"{len(allowed)} allowlisted site(s), each with a stated reason")
    return 0


if __name__ == "__main__":
    sys.exit(main())
