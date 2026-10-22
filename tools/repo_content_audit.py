#!/usr/bin/env python3
"""Does the current tree carry anything of the retired hardware programme?

The forensic inventory (``tools/repo_inventory.py``) says what happened to
each file of the baseline. This asks the other question, of the tree as it
is NOW, and answers it by reading every tracked file:

* RETIRED_PATH -- a file under a retired root, or at a path the inventory
  says was removed, or with the name of a retired output anywhere;
* RETIRED_IMPORT -- Python that imports, or names for dynamic import, a
  module the inventory retired (the old namespace included);
* GATE_DEPENDENCY -- anything that reads, computes or reports the retired
  gate table, its PASS count or a gate-effect label;
* HARDWARE_TERM -- the retired programme's vocabulary (its name, its
  apparatus, species, sensing layer, machine controller, BOM, modes);
* UNCLASSIFIED -- a tracked file no rule places in an area of the current
  harness.

Exceptions are declared, never inferred. :data:`PROTOCOL_IDENTIFIERS` are
exact strings recorded in events or signed bytes, which a history must still
verify under; they are struck from the text before it is scanned, so a NEW
identifier in the old style is still a finding. :data:`ALLOWANCES` name the
few files whose purpose is to record or detect the retired content (the
inventory, this audit, their tests) or that hold recorded findings verbatim;
each says why, and an allowance nothing uses is itself a finding, so the
list cannot outlive its reason.

    python tools/repo_content_audit.py          # report; exit 1 on a finding
    python tools/repo_content_audit.py --write  # and write the report

The committed report (``docs/repo_content_audit.json``) is what the derived
harness status reads; ``--check`` re-runs the audit and requires the report
to be current.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("docs") / "repo_content_audit.json"
INVENTORY = Path("docs") / "repository_inventory.json"
SCHEMA = "repo-content-audit/1"

#: Roots the owner retired wholesale. Nothing may be tracked under them.
RETIRED_ROOTS = ("qta_multiphysics/", "qta_agent/", "attic/",
                 "EXPERIMENT_PLAYBOOKS/", "QTA_stage9_release_verification/",
                 "stage7_reports/", "stage8_reports/",
                 "matrix_update_examples/", "docs/legacy/",
                 "reference_backend/", "rust/", "analysis/", "workflow/")

#: Exact strings recorded in events or in signed bytes. Changing one would
#: make an existing history fail to verify, so they are kept verbatim and
#: are the ONLY place the retired programme's name may appear in code.
PROTOCOL_IDENTIFIERS = (
    "qta-agent/event-attestation/v1",
    "qta-agent/test-identity/v1",
    "qta-agent/key-registry/v1",
    "qta-agent/key-registry/v2",
    "qta-agent/reference-ed25519/v1",
)

#: (category, pattern). Case-insensitive; matched after the protocol
#: identifiers are struck out.
TERMS = (
    ("GATE_DEPENDENCY", r"results_gate|gate[ _-]table|pass[_ ]count|"
                        r"scientific_pass|gate[ _]effect|legacy_qta"),
    ("HARDWARE_TERM", r"(?<![a-z0-9])qta(?![a-z0-9])|qta_|_qta|"
                      r"cryo[_ -]?(panel|trap|baffle|stat|stack|genic)|"
                      r"nv[_ -]?spin|nv[ -]cent(er|re)|odmr|lcvd|methane|"
                      r"(?<![a-z0-9])ch4(?![a-z0-9])|helium|"
                      r"(?<![a-z0-9])he-?[34](?![a-z0-9])|machine[_ ]fsm|"
                      r"interlock|(?<![a-z0-9])bom(?![a-z0-9])|"
                      r"(?<![a-z0-9])mode[_ ][abcd](?![a-z0-9])|kapitza|"
                      r"dilution (fridge|refrigerator)"),
)
_TERMS = [(c, re.compile(p, re.IGNORECASE)) for c, p in TERMS]

#: path glob -> why it may contain the vocabulary. Every allowance must be
#: used by at least one match, or it is reported as stale.
ALLOWANCES = {
    "docs/repository_inventory.json":
        "the forensic inventory: it records every baseline file by its "
        "baseline path, which is what makes the removal reviewable",
    "tools/repo_inventory.py":
        "the inventory's rules name the retired paths in order to "
        "classify them",
    "tests/test_repo_inventory.py":
        "the inventory's tests assert edges between baseline paths",
    "tools/repo_content_audit.py":
        "this audit: its vocabulary is what it looks for",
    "docs/repo_content_audit.json":
        "this audit's committed report: it lists the retired roots and "
        "names it refuses, and --check requires it to be exactly what the "
        "tree produces",
    "tests/test_repo_content_audit.py":
        "this audit's controls reintroduce retired content on purpose",
    "tools/mutations/repository_content.json":
        "this audit's mutation specification: two of its mutations plant "
        "retired content in the learned-model substrate on purpose, to "
        "show the boundary tests refuse it",
    "tests/fuzz_corpus/*.json":
        "recorded fuzzing findings, kept verbatim: their tracebacks name "
        "the module path the finding was made in",
}

#: Retired outputs by FILE NAME, refused anywhere in the tree: a copy at a
#: new path is the same file.
RETIRED_NAMES = ("results_gate_table.csv", "BOM.csv", "interlock_table.csv",
                 "rejected_baseline_BOM.csv", "qta_full_sim.py",
                 "package_consistency_check.py", "qta_scientific_results.h5")

#: Where each current file belongs. First match wins; a file no rule
#: matches is UNCLASSIFIED.
AREAS = (
    ("agent_substrate/*", "AGENT_SUBSTRATE"),
    ("scientific/*", "SCIENTIFIC_COMPUTING"),
    ("scientific_ai/*", "NEURAL_INFRASTRUCTURE"),
    ("integrations/*", "INTEGRATION_ASSETS"),
    ("tools/mutations/*", "SOFTWARE_ASSURANCE"),
    ("tools/*", "SOFTWARE_ASSURANCE"),
    ("tests/*", "TESTS"),
    # the TLA+ models, their record and their simulated traces (Assurance B)
    ("docs/formal/*", "SOFTWARE_ASSURANCE"),
    ("docs/neural/dev/*", "LEARNED_MODEL_EVIDENCE"),
    ("docs/neural/*", "LEARNED_MODEL_SPECIFICATION"),
    ("docs/*.json", "REGISTRY_OR_DERIVED_STATUS"),
    ("docs/*.md", "DOCUMENTATION"),
    (".github/workflows/*", "HOSTED_CI"),
    ("ro-crate/*", "PROVENANCE"),
    ("final_manifest.json", "PROVENANCE"),
    ("manifest_hash.txt", "PROVENANCE"),
    ("generate_manifest.py", "PROVENANCE"),
    ("ro_crate_tools.py", "PROVENANCE"),
    ("stack.json", "REGISTRY_OR_DERIVED_STATUS"),
    ("authorities.json", "REGISTRY_OR_DERIVED_STATUS"),
    ("*.md", "DOCUMENTATION"),
    ("pyproject.toml", "PACKAGING"),
    ("uv.lock", "PACKAGING"),
    ("typecheck_active.ini", "PACKAGING"),
    ("conftest.py", "TESTS"),
    ("Snakefile", "WORKFLOW"),
    ("Dockerfile", "CONTAINER"),
    (".dockerignore", "CONTAINER"),
    ("container_verify.sh", "CONTAINER"),
    (".gitignore", "REPOSITORY_CONFIGURATION"),
)


def tracked(root: Path = ROOT) -> list:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z",
                          "--cached", "--others", "--exclude-standard"],
                         check=True, capture_output=True, text=True).stdout
    return sorted(p for p in out.split("\0")
                  if p and (root / p).is_file())


def area(path: str) -> str | None:
    for pattern, name in AREAS:
        if fnmatch.fnmatch(path, pattern):
            return name
    return None


def allowance(path: str) -> str | None:
    for pattern in ALLOWANCES:
        if fnmatch.fnmatch(path, pattern):
            return pattern
    return None


def retired_from_inventory(root: Path = ROOT) -> tuple:
    """(paths that must be absent, module names that must not be imported),
    both from the committed inventory: one source of truth for what was
    retired."""
    doc = json.loads((root / INVENTORY).read_text(encoding="utf-8"))
    gone, modules = set(), set()
    for f in doc["files"]:
        if f["class"] in ("REMOVE", "REIMPLEMENT", "EXTRACT_GENERIC") or \
                f["destinations"]:
            gone.add(f["path"])
            if f["path"].endswith(".py"):
                m = f["path"][:-3].replace("/", ".")
                modules.add(m[:-len(".__init__")]
                            if m.endswith(".__init__") else m)
    # A retired package's root is here through its __init__.py row, and an
    # import is matched by its longest retired prefix, so a submodule no row
    # names is still a retired import.
    return gone, modules


def _strip_protocol(text: str) -> str:
    for ident in PROTOCOL_IDENTIFIERS:
        text = text.replace(ident, "")
    return text


def _imports(source: str) -> set:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            found.add(n.module)
            found.update(f"{n.module}.{a.name}" for a in n.names)
        elif (isinstance(n, ast.Call)
              and getattr(n.func, "attr", getattr(n.func, "id", ""))
              in ("import_module", "__import__") and n.args
              and isinstance(n.args[0], ast.Constant)
              and isinstance(n.args[0].value, str)):
            found.add(n.args[0].value)
    return found


def audit(root: Path = ROOT, files: list | None = None) -> dict:
    files = tracked(root) if files is None else sorted(files)
    gone, retired_modules = retired_from_inventory(root)
    findings: list = []
    used_allowances: dict = {k: 0 for k in ALLOWANCES}
    areas: dict = {}

    def find(category, path, detail):
        findings.append({"category": category, "path": path,
                         "detail": detail})

    for path in files:
        a = area(path)
        if a is None:
            find("UNCLASSIFIED", path, "no rule places this file in an area "
                                       "of the current harness")
        else:
            areas[a] = areas.get(a, 0) + 1
        if path.startswith(RETIRED_ROOTS):
            find("RETIRED_PATH", path, "under a retired root")
        elif path in gone:
            find("RETIRED_PATH", path, "the inventory records this path as "
                                       "removed or moved")
        if path.rsplit("/", 1)[-1] in RETIRED_NAMES:
            find("RETIRED_PATH", path, "the name of a retired output")
        try:
            raw = (root / path).read_bytes()
        except OSError as exc:
            find("UNCLASSIFIED", path, f"unreadable: {exc}")
            continue
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", "replace")
        if path.endswith(".py") or path == "Snakefile":
            for mod in sorted(_imports(text) if path.endswith(".py")
                              else set()):
                parts = mod.split(".")
                hit = next((".".join(parts[:i])
                            for i in range(len(parts), 0, -1)
                            if ".".join(parts[:i]) in retired_modules), None)
                if hit:
                    find("RETIRED_IMPORT", path, f"imports {mod} "
                                                 f"(retired: {hit})")
        allowed = allowance(path)
        scanned = _strip_protocol(text)
        for category, rx in _TERMS:
            hits = sorted({m.group(0).lower() for m in rx.finditer(scanned)})
            if not hits:
                continue
            if allowed:
                used_allowances[allowed] += 1
                continue
            find(category, path, "contains " + ", ".join(hits[:8]))
    for pattern, n in sorted(used_allowances.items()):
        if n == 0:
            find("STALE_ALLOWANCE", pattern,
                 "an allowance no file needs: remove it")
    counts: dict = {}
    for f in findings:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    return {
        "schema": SCHEMA,
        "label": "The current tree read for retired content. Zero findings "
                 "is the transformation's acceptance condition for repository "
                 "content; it says nothing about scientific correctness.",
        "files": len(files),
        "by_area": dict(sorted(areas.items())),
        "protocol_identifiers": list(PROTOCOL_IDENTIFIERS),
        "allowances": {k: {"why": v, "files_using": used_allowances[k]}
                       for k, v in sorted(ALLOWANCES.items())},
        "retired_roots": list(RETIRED_ROOTS),
        "finding_counts": dict(sorted(counts.items())),
        "findings": findings,
        "ok": not findings,
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=1, sort_keys=True,
                      ensure_ascii=False) + "\n"


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="also require the committed report to be current")
    a = ap.parse_args(argv)
    doc = audit()
    for f in doc["findings"]:
        print(f"{f['category']}: {f['path']}: {f['detail']}")
    if a.write:
        (ROOT / OUT).write_text(render(doc), encoding="utf-8")
    stale = False
    if a.check:
        p = ROOT / OUT
        stale = not p.is_file() or p.read_text(encoding="utf-8") != \
            render(doc)
        if stale:
            print("the committed report is stale: run --write")
    print(f"repository content: {doc['files']} files, "
          f"{len(doc['findings'])} finding(s) -- "
          + ("CLEAN" if doc["ok"] else "NOT CLEAN"))
    return 0 if doc["ok"] and not stale else 1


if __name__ == "__main__":
    sys.exit(main())
