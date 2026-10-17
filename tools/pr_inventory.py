#!/usr/bin/env python3
"""The PR-level threat and dependency inventory: every tracked file, what it
is, which trust boundary it sits on, what it needs at run time, and what
verifies it.

WHAT IT RECORDS, PER FILE
  change            ADDED or MODIFIED relative to the default branch commit
                    recorded in the document (every file at the head differs
                    from it; the files it deleted are counted, not listed --
                    docs/repository_inventory.json classifies each).
  subsystem, trust_boundary, risk
                    from ordered path rules below. Risk is a statement about
                    what a defect HERE could do, not about how likely one is.
  runtime_dependencies
                    third-party top-level modules a Python file imports
                    (static and function-local), measured from its source.
  verification      the test modules that reference it (by dotted module
                    name, by path, or by the bare module name a tool is
                    imported as), the mutation specifications that mutate
                    it, and the findings (docs/security/findings.json) that
                    name it.

A file every test passes is not thereby safe: the coverage columns say what
CHECKS a file, and the risk column says what is at stake if the checks are
wrong. Read them together.

    python tools/pr_inventory.py derive [--main SHA]   # writes the document
    python tools/pr_inventory.py check                 # CI: still current?
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "security" / "pr17_inventory.json"
FINDINGS = ROOT / "docs" / "security" / "findings.json"
SCHEMA = "harness-pr-inventory/1"
LOCAL_PACKAGES = {"agent_substrate", "scientific", "scientific_ai",
                  "integrations", "tools", "tests"}

#: (glob, subsystem, trust boundary, risk). FIRST match wins, so specific
#: rules precede general ones. Every tracked file must match one.
RULES = (
    (".github/workflows/*", "ci-workflows", "CI authority: tokens, OIDC, "
     "secrets, what may push or sign", "CRITICAL"),
    ("tools/supply_chain.py", "supply-chain", "release integrity and "
     "admission", "CRITICAL"),
    ("tools/offline_verify.py", "supply-chain", "release integrity and "
     "admission", "CRITICAL"),
    ("docs/supply_chain_*", "supply-chain", "release integrity and "
     "admission", "CRITICAL"),
    ("integrations/supply_chain/*", "supply-chain", "verifier dependency "
     "pinning", "CRITICAL"),
    ("tools/isolated_runtime.py", "supply-chain", "hash-pinned runtimes",
     "HIGH"),
    ("agent_substrate/authority.py", "authority-core", "authority decision "
     "rules", "CRITICAL"),
    ("agent_substrate/store.py", "authority-core", "authority decision "
     "rules and replay", "CRITICAL"),
    ("agent_substrate/events.py", "authority-core", "hash-chained log, "
     "writer lock, head witness", "CRITICAL"),
    ("agent_substrate/reconstruct.py", "authority-core", "independent second "
     "reader", "CRITICAL"),
    ("agent_substrate/result_rules.py", "scientific-authority", "admission "
     "of scientific results", "CRITICAL"),
    ("agent_substrate/learned_*", "learned-outputs", "learned outputs are "
     "never authority", "CRITICAL"),
    ("agent_substrate/principals.py", "identity", "who wrote an event",
     "CRITICAL"),
    ("agent_substrate/signature.py", "identity", "event signatures",
     "CRITICAL"),
    ("agent_substrate/ed25519.py", "identity", "event signatures",
     "CRITICAL"),
    ("agent_substrate/secrets.py", "boundaries", "secrets", "CRITICAL"),
    ("agent_substrate/netauth.py", "boundaries", "network authority",
     "CRITICAL"),
    ("agent_substrate/execution.py", "execution", "process isolation",
     "CRITICAL"),
    ("agent_substrate/capability.py", "identity", "capabilities", "CRITICAL"),
    ("agent_substrate/policy.py", "identity", "policy decisions", "CRITICAL"),
    ("agent_substrate/proposals.py", "ai-ingress", "AI proposals enter as "
     "data", "HIGH"),
    ("agent_substrate/context.py", "ai-ingress", "what an agent is shown",
     "HIGH"),
    ("agent_substrate/rag_index.py", "ai-ingress", "retrieval is not "
     "evidence", "HIGH"),
    ("agent_substrate/safeio.py", "boundaries", "filesystem confinement",
     "HIGH"),
    ("agent_substrate/readpath.py", "boundaries", "verified reads", "HIGH"),
    ("agent_substrate/evidence.py", "boundaries", "content-addressed "
     "evidence", "HIGH"),
    ("agent_substrate/governed_model.py", "scientific-authority", "model "
     "execution and independent checking", "HIGH"),
    ("agent_substrate/*", "agent-substrate", "event-sourced projections",
     "HIGH"),
    ("scientific/checks/*", "scientific-verification", "independent "
     "implementations", "HIGH"),
    ("scientific/workspace.py", "boundaries", "filesystem confinement",
     "HIGH"),
    ("scientific/*", "scientific-core", "model execution, identity, "
     "results", "MEDIUM"),
    ("scientific_ai/*", "learned-outputs", "learned-model substrate",
     "MEDIUM"),
    ("tools/independent_verify.py", "authority-core", "separate-process "
     "verifier", "HIGH"),
    ("tools/attach_hosted_evidence.py", "evidence", "hosted evidence "
     "attachment", "HIGH"),
    ("tools/harness_status.py", "evidence", "derived completion status",
     "HIGH"),
    ("tools/readiness.py", "evidence", "derived readiness status", "HIGH"),
    ("tools/mutations/*", "assurance", "mutation specifications", "MEDIUM"),
    ("tools/*", "tooling", "repository tooling", "MEDIUM"),
    ("generate_manifest.py", "manifest", "byte binding of the tree", "HIGH"),
    ("final_manifest.json", "manifest", "byte binding of the tree", "HIGH"),
    ("manifest_hash.txt", "manifest", "byte binding of the tree", "HIGH"),
    ("ro_crate_tools.py", "packaging", "RO-Crate description", "MEDIUM"),
    ("ro-crate/*", "packaging", "RO-Crate description", "MEDIUM"),
    ("integrations/*", "integrations", "isolated runtimes and FMU sources",
     "MEDIUM"),
    ("Dockerfile", "container", "container image", "HIGH"),
    ("container_verify.sh", "container", "container image", "HIGH"),
    ("Snakefile", "workflow", "governed production path", "MEDIUM"),
    ("uv.lock", "dependencies", "dependency pinning", "HIGH"),
    ("pyproject.toml", "dependencies", "dependency declaration", "HIGH"),
    ("tests/*", "tests", "verification only", "LOW"),
    ("docs/security/*", "evidence", "security audit records", "MEDIUM"),
    ("docs/neural/*", "learned-outputs", "learned-model evidence", "MEDIUM"),
    ("docs/*", "evidence", "derived documents and evidence", "MEDIUM"),
    ("verification/*", "evidence", "committed verification artefacts",
     "MEDIUM"),
    ("*", "documentation", "documentation and configuration", "LOW"),
)

#: Questions the review could not settle from the repository and the API
#: this environment can read. Each is an owner action or a measurement the
#: owner must make; none is answered by assumption.
UNRESOLVED = (
    "Which permissions does the installed Claude GitHub App hold on this "
    "repository (in particular workflows: write)? The installation is not "
    "readable from this environment.",
    "Does a branch protection rule or ruleset exist that this environment "
    "cannot see? The branch API reports protected: false for main.",
    "Which repository secrets exist (ANTHROPIC_API_KEY and any other)? The "
    "secrets API is not reachable from this environment.",
    "What are the repository's Actions settings: default workflow "
    "permissions, whether Actions may approve pull requests, and the "
    "allowed-actions policy? Not reachable from this environment.",
    "Who will own the release trust root, and through which out-of-band "
    "channel will verifiers receive the release policy and trusted root?",
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def tracked() -> list:
    return sorted(p for p in _git("ls-files", "-z").split("\0") if p)


def classify(path: str) -> tuple:
    for glob, sub, boundary, risk in RULES:
        if fnmatch.fnmatchcase(path, glob):
            return sub, boundary, risk
    raise AssertionError("the catch-all rule matches everything")


def runtime_dependencies(path: str, root: Path = ROOT) -> list:
    if not path.endswith(".py"):
        return []
    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 \
                and node.module:
            tops.add(node.module.split(".")[0])
    tool_modules = {Path(p).stem for p in tracked_cache()
                    if p.startswith("tools/") and p.endswith(".py")}
    test_helpers = {Path(p).stem for p in tracked_cache()
                    if p.startswith("tests/") and p.endswith(".py")}
    root_modules = {Path(p).stem for p in tracked_cache()
                    if "/" not in p and p.endswith(".py")}
    return sorted(t for t in tops
                  if t not in sys.stdlib_module_names
                  and t not in LOCAL_PACKAGES and t not in tool_modules
                  and t not in test_helpers and t not in root_modules
                  and t != "__future__")


_TRACKED: list = []


def tracked_cache() -> list:
    if not _TRACKED:
        _TRACKED.extend(tracked())
    return _TRACKED


def _reference_keys(path: str) -> tuple:
    """How a test would name this file: its path, its dotted module name for
    package modules, and the bare module name for tools and root scripts,
    which tests import from a sys.path entry."""
    keys = [path]
    if path.endswith(".py"):
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        keys.append(mod)
        if path.startswith("tools/") or "/" not in path:
            stem = Path(path).stem
            keys.extend([f"import {stem}", f"from {stem} import"])
    return tuple(keys)


def _imported_modules(text: str) -> set:
    """Every dotted module a test imports, resolved from its syntax tree:
    ``from agent_substrate import store, events`` imports
    ``agent_substrate.store`` and ``agent_substrate.events``, which a text
    search for the dotted name does not see."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module \
                and node.level == 0:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def coverage(files: list, root: Path = ROOT) -> dict:
    tests = {p: (root / p).read_text(encoding="utf-8", errors="replace")
             for p in files if p.startswith("tests/") and p.endswith(".py")}
    imports = {t: _imported_modules(text) for t, text in tests.items()}
    specs = {}
    for p in files:
        if p.startswith("tools/mutations/") and p.endswith(".json"):
            doc = json.loads((root / p).read_text(encoding="utf-8"))
            specs[p] = {m["path"] for m in doc.get("mutations", [])}
    out = {}
    for f in files:
        keys = _reference_keys(f)
        mod = (f[:-3].replace("/", ".").removesuffix(".__init__")
               if f.endswith(".py") else None)
        out[f] = {
            "tests": sorted(t for t, text in tests.items()
                            if t != f and (any(k in text for k in keys)
                                           or mod in imports[t])),
            "mutation_specs": sorted(s for s, paths in specs.items()
                                     if f in paths)}
    return out


def _findings_by_file() -> dict:
    doc = json.loads(FINDINGS.read_text(encoding="utf-8"))
    out: dict = {}
    for fnd in doc["findings"]:
        for f in fnd["files"]:
            if f.get("ref", "HEAD") == "HEAD":
                out.setdefault(f["path"], []).append(fnd["id"])
    return out


def derive(main_sha: str) -> dict:
    files = tracked()
    in_main = set(p for p in _git("ls-tree", "-r", "-z", "--name-only",
                                  main_sha).split("\0") if p)
    cov = coverage(files)
    fnd = _findings_by_file()
    entries = []
    for f in files:
        sub, boundary, risk = classify(f)
        entries.append({
            "path": f, "change": "MODIFIED" if f in in_main else "ADDED",
            "subsystem": sub, "trust_boundary": boundary, "risk": risk,
            "runtime_dependencies": runtime_dependencies(f),
            "verification": {**cov[f], "findings": sorted(fnd.get(f, []))}})
    deleted = sorted(in_main - set(files))
    inv = json.loads((ROOT / "docs" / "repository_inventory.json")
                     .read_text(encoding="utf-8"))
    inv_paths = {e["path"] for e in inv.get("files", [])}

    def count(key):
        c: dict = {}
        for e in entries:
            c[e[key]] = c.get(e[key], 0) + 1
        return dict(sorted(c.items()))

    # Named by no test module -- by path, dotted name or import. Such a file
    # may still be exercised indirectly (a check run through the catalog);
    # this list says only that no test asks for it by name.
    unverified = [e["path"] for e in entries
                  if e["risk"] in ("CRITICAL", "HIGH")
                  and e["path"].endswith(".py")
                  and not e["path"].startswith("tests/")
                  and not e["verification"]["tests"]]
    return {
        "schema": SCHEMA,
        "label": "Every file tracked at the head of PR #17, classified by "
                 "subsystem, trust boundary and risk, with its third-party "
                 "runtime dependencies and the tests, mutation "
                 "specifications and findings that bear on it. Derived by "
                 "tools/pr_inventory.py; never edited by hand.",
        "default_branch_commit": main_sha,
        "counts": {"files": len(entries), "by_change": count("change"),
                   "by_subsystem": count("subsystem"),
                   "by_risk": count("risk"),
                   "deleted_from_default_branch": len(deleted),
                   "deleted_classified_in_repository_inventory":
                       sum(1 for d in deleted if d in inv_paths)},
        "critical_or_high_python_no_test_names": unverified,
        "unresolved_questions": list(UNRESOLVED),
        "files": entries,
    }


def _render(doc: dict) -> str:
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("derive")
    d.add_argument("--main", default=None,
                   help="default-branch commit (default: origin/main)")
    sub.add_parser("check")
    args = ap.parse_args(argv)
    if args.cmd == "derive":
        main_sha = args.main or _git("rev-parse", "origin/main").strip()
        doc = derive(main_sha)
        OUT.write_text(_render(doc), encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)}: {doc['counts']['files']} "
              f"files, {doc['counts']['by_risk']}")
        return 0
    have = json.loads(OUT.read_text(encoding="utf-8"))
    main_sha = have["default_branch_commit"]
    probe = subprocess.run(["git", "cat-file", "-e", f"{main_sha}^{{tree}}"],
                           cwd=ROOT, capture_output=True)
    if probe.returncode != 0:
        print(f"pr inventory: default-branch commit {main_sha[:12]} is not "
              "in this checkout; fetch it to check the inventory")
        return 1
    want = derive(main_sha)
    if _render(want) != _render(have):
        print("pr inventory is STALE: run tools/pr_inventory.py derive")
        return 1
    print(f"pr inventory holds: {want['counts']['files']} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
