#!/usr/bin/env python3
"""Where the legacy gate table's ``PASS = 0`` still appears, and why.

The QTA hardware-forecast package kept an 83-row gate table in which no row
could PASS, because nothing had been measured: ``PASS_count = 0`` was that
forecast's honest headline, and it stays true of it. It is NOT a measure of
the Scientific-AI harness, the learned-model substrate (NF-1T) or the agent
substrate -- software whose own status is a matter of tests, claims and
evidence -- and a current document that states ``PASS remains 0`` without
saying whose PASS it is reads as "this software passes nothing".

This audit finds every occurrence of the legacy gate-table vocabulary in
the repository and puts each file in exactly one class by an ordered rule
table (first match wins); a file no rule covers is UNCLASSIFIED and fails
the check.

Classes

``LEGACY_QTA_CANONICAL``        the legacy forecast's own evidence and the
                                code, registries and manuscript that define
                                it -- historical facts, kept as they are;
``LEGACY_COMPATIBILITY_GUARD``  a test, check or workflow that preserves the
                                legacy semantics, or a current statement
                                that the legacy gates are out of its reach
                                and SAYS they are the legacy ones;
``DOCUMENTATION_HISTORY``       prose that records history;
``TOOLING_REFERENCE``           an inventory, manifest, disposition table,
                                audit report or mutation spec naming a
                                legacy artefact or this vocabulary;
``FALSE_POSITIVE``              a match that is not the gate concept;
``CURRENT_AI_SEMANTIC_LEAK``    a current-harness statement of the legacy
                                PASS/gate vocabulary that does not label it
                                legacy -- the check requires ZERO.

In the CURRENT scope (the harness's code, tools, status documents and
registries) each occurrence is judged on its own: within two lines of it
the text must say whose gates these are (legacy, historical, hardware-era,
hardware forecast, QTA), and ``automatic_gate_effect`` must be stated as
NONE -- the substrate's no-effect invariant. Anything else is a leak.

The report records counts per file and pattern, and line numbers only for
leaks, so an unrelated edit does not make it stale.

    python tools/pass_semantics_audit.py            # print the summary
    python tools/pass_semantics_audit.py --write    # write the report
    python tools/pass_semantics_audit.py --check    # exit 1 unless 0 leaks,
                                                    # 0 unclassified and the
                                                    # committed report current
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("docs") / "pass_semantics_audit.json"

PATTERNS = {
    "pass_remains_zero": re.compile(
        r"(?i)\bPASS(?:[ _]count)?\s+(?:remains|remain|stays|stay|is)\s+"
        r"(?:permanently\s+)?(?:0|zero)\b"),
    "pass_permanently_zero": re.compile(
        r"(?i)\bPASS\s+permanently\s+(?:0|zero)\b"),
    "pass_equals_zero": re.compile(r"(?i)\bPASS(?:_count)?\s*==?\s*0\b"),
    "zero_pass": re.compile(r"(?i)\b0\s+PASS\b"),
    "pass_count": re.compile(r"(?i)(?<![A-Za-z])PASS[ _]count"),
    "automatic_gate_effect": re.compile(r"(?i)automatic_gate_effect"),
    "gate_status": re.compile(r"(?i)\bgate[ _]status"),
    "hardware_gate": re.compile(r"(?i)\bhardware[ _]gates?\b"),
    "results_gate_table": re.compile(r"(?i)results_gate_table"),
}
#: Within this many lines of an occurrence, a current-scope statement must
#: say whose gates these are.
WINDOW = 2
LEGACY_LABEL = (re.compile(r"(?i)legacy|histor|hardware[- ]era|"
                           r"hardware[- ]forecast"),
                re.compile(r"\bQTA\b|LEGACY_QTA"))
NO_EFFECT = re.compile(r"\bNONE\b")

CLASSES = ("LEGACY_QTA_CANONICAL", "LEGACY_COMPATIBILITY_GUARD",
           "DOCUMENTATION_HISTORY", "TOOLING_REFERENCE", "FALSE_POSITIVE",
           "CURRENT_AI_SEMANTIC_LEAK")
CURRENT = "CURRENT_SCOPE"
BINARY = (".pdf", ".h5", ".safetensors", ".png", ".jpg", ".zip", ".gz",
          ".npz", ".pyc", ".bundle")

#: (pattern, class, why). fnmatch path patterns; first match wins. A rule
#: whose class is CURRENT_SCOPE judges each occurrence on its own.
PATH_RULES = (
    (str(OUT), "TOOLING_REFERENCE",
     "this audit's report (counts and pattern names)"),
    ("tools/pass_semantics_audit.py", "TOOLING_REFERENCE",
     "this audit: its patterns name what it looks for"),
    ("tests/test_claim_hygiene.py", "TOOLING_REFERENCE",
     "plants leaks and labelled guards to prove this audit tells them "
     "apart"),
    ("docs/neural/legacy_semantic_audit.json", "TOOLING_REFERENCE",
     "the hardware-era mode audit's report, naming legacy files"),
    ("docs/neural/nf1t_inventory.json", "TOOLING_REFERENCE",
     "the NF-1T starting inventory: canonical outputs by name and digest"),
    ("tools/neural_legacy_audit.py", "TOOLING_REFERENCE",
     "the hardware-era mode audit"),
    ("FILE_DISPOSITION.csv", "TOOLING_REFERENCE",
     "generated: each file's disposition quotes what it holds"),
    ("tools/file_disposition.py", "TOOLING_REFERENCE",
     "classifies every file, legacy ones by what they hold"),
    ("final_manifest.json", "TOOLING_REFERENCE", "per-file manifest"),
    ("ro-crate/*", "TOOLING_REFERENCE", "RO-Crate metadata of the corpus"),
    ("docs/corpus_allowlist.json", "TOOLING_REFERENCE",
     "the governed-document allowlist"),
    ("docs/byte_reproduction_profile.json", "TOOLING_REFERENCE",
     "the witness profile: canonical outputs by name"),
    ("docs/resolution_inventory.json", "TOOLING_REFERENCE",
     "what each canonical quantity resolves, by file"),
    ("docs/reference_backend_host_a.json", "TOOLING_REFERENCE",
     "the reference backend's host record, by canonical file"),
    ("docs/blas_kernel_sensitivity.json", "TOOLING_REFERENCE",
     "the kernel-sensitivity measurement, by canonical file"),
    ("stage8_reports/*", "TOOLING_REFERENCE",
     "the legacy HDF5 equivalence report, by file"),
    ("QTA_stage9_release_verification/SHA256SUMS", "TOOLING_REFERENCE",
     "release checksums by file"),
    ("QTA_stage9_release_verification/*.json", "TOOLING_REFERENCE",
     "release index and provenance, by file"),
    ("tools/mutations/*", "TOOLING_REFERENCE",
     "mutation specs naming the code they mutate"),

    # The current harness: every occurrence judged on its own.
    ("scientific_ai/*", CURRENT, "the learned-model substrate"),
    ("scientific/*", CURRENT, "the generic scientific framework"),
    ("qta_agent/*", CURRENT, "the agent authority substrate"),
    ("tools/neural.py", CURRENT, "the substrate's evidence tool"),
    ("tools/neural_ledger.py", CURRENT, "the substrate's ledger tool"),
    ("tests/neural_support.py", CURRENT, "the substrate's test support"),
    ("tests/test_neural_*.py", CURRENT, "the substrate's tests"),
    ("docs/neural/*", CURRENT, "the substrate's evidence and status"),
    ("SCIENTIFIC_AI_STATUS.md", CURRENT, "the current status"),
    ("HARNESS_STATUS.md", CURRENT, "the derived harness completion "
     "status"),
    ("docs/harness_status.json", CURRENT, "the derived harness "
     "completion status"),
    ("docs/harness_contract.json", CURRENT, "the harness "
     "definition of done"),
    ("NEURAL_SUBSTRATE.md", CURRENT, "the substrate's specification"),
    ("AGENT_SUBSTRATE.md", CURRENT, "the agent substrate's specification"),
    ("README.md", CURRENT, "the repository's front page"),
    ("CLAIMS_BOUNDARY.md", CURRENT, "the current claims boundary"),
    ("docs/claims_boundary.json", CURRENT, "the claims boundary registry"),
    ("docs/completion_matrix.json", CURRENT, "the completion matrix"),
    ("docs/framework_boundary.json", CURRENT,
     "the active/legacy boundary registry"),
    ("AUTHORITIES.md", CURRENT, "the authority registry, narrated"),
    ("authorities.json", CURRENT, "the authority registry"),
    ("conftest.py", CURRENT, "the suite-wide test configuration"),
    ("tools/generic_consistency.py", CURRENT, "the generic verifier"),
    ("tools/audit_log.py", CURRENT, "the authority log auditor"),
    ("tools/test_isolation.py", CURRENT, "the suite's isolation check"),
    ("tools/completion_matrix.py", CURRENT, "the matrix validator"),
    ("tools/claims_enforcement.py", CURRENT, "the claims checker"),
    ("tools/framework_boundary.py", CURRENT, "the active/legacy boundary"),
    ("tools/independent_verify.py", CURRENT, "the independent verifier"),
    ("tools/model_check.py", CURRENT, "the model checker"),
    (".github/workflows/agent-substrate.yml", CURRENT,
     "the current harness's CI"),

    # The legacy forecast's own evidence and what defines it.
    ("qta_full_sim.py", "LEGACY_QTA_CANONICAL",
     "the legacy forecast's generator"),
    ("qta_multiphysics/*", "LEGACY_QTA_CANONICAL",
     "the legacy forecast's models and gate assembly"),
    ("Snakefile", "LEGACY_QTA_CANONICAL",
     "the workflow, legacy targets included"),
    ("workflow/*", "LEGACY_QTA_CANONICAL", "the legacy workflow rules"),
    ("build_*.py", "LEGACY_QTA_CANONICAL",
     "builders of the legacy registries and release"),
    ("EXPERIMENT_PLAYBOOKS/*", "LEGACY_QTA_CANONICAL",
     "the legacy experiment playbooks"),
    ("*.tex", "LEGACY_QTA_CANONICAL", "the legacy manuscript"),
    ("*.csv", "LEGACY_QTA_CANONICAL", "legacy forecast tables"),
    ("*.json", "LEGACY_QTA_CANONICAL",
     "legacy forecast registries, schemas and outputs"),
    ("matrix_update_examples/*", "LEGACY_QTA_CANONICAL",
     "examples of the legacy validation-matrix update contract"),

    # Checks that keep the legacy semantics as they were.
    ("tests/*", "LEGACY_COMPATIBILITY_GUARD",
     "tests that preserve or exercise the legacy semantics"),
    (".github/workflows/*", "LEGACY_COMPATIBILITY_GUARD",
     "legacy workflows and the steps that keep the legacy gates fixed"),
    ("tools/*.py", "LEGACY_COMPATIBILITY_GUARD",
     "a tool that reads or checks the legacy corpus"),
    ("*.py", "LEGACY_COMPATIBILITY_GUARD",
     "a root-level checker of the legacy package"),
    ("*.sh", "LEGACY_COMPATIBILITY_GUARD",
     "a script verifying the legacy package"),

    ("attic/*", "DOCUMENTATION_HISTORY", "retired delivery artefacts"),
    ("docs/*", "DOCUMENTATION_HISTORY", "records and analyses"),
    ("QTA_stage9_release_verification/*", "DOCUMENTATION_HISTORY",
     "the legacy release record"),
    ("*.md", "DOCUMENTATION_HISTORY", "prose"),
    ("*.txt", "DOCUMENTATION_HISTORY", "text records"),
)
#: path -> why its matches are not the gate concept.
FALSE_POSITIVES: dict = {}


def repository_files(root: Path) -> list:
    """Tracked plus untracked-unignored: a file is audited before it is
    committed, not after."""
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], cwd=root,
                         capture_output=True, check=True).stdout
    return sorted({p for p in out.decode().split("\0") if p})


def classify(path: str) -> tuple:
    if path in FALSE_POSITIVES:
        return "FALSE_POSITIVE", FALSE_POSITIVES[path]
    for pat, cls, why in PATH_RULES:
        if fnmatch.fnmatchcase(path, pat):
            return cls, why
    return "UNCLASSIFIED", "no rule covers this file"


def occurrences(text: str) -> list:
    """(line number, pattern name) for every occurrence, in order."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for name, rx in PATTERNS.items():
            if rx.search(line):
                out.append((n, name))
    return out


def judge(lines: list, n: int, name: str) -> str:
    """A current-scope occurrence: a labelled guard, or a leak."""
    lo, hi = max(0, n - 1 - WINDOW), min(len(lines), n + WINDOW)
    window = "\n".join(lines[lo:hi])
    if any(rx.search(window) for rx in LEGACY_LABEL):
        return "LEGACY_COMPATIBILITY_GUARD"
    if name == "automatic_gate_effect" and NO_EFFECT.search(window):
        return "LEGACY_COMPATIBILITY_GUARD"
    return "CURRENT_AI_SEMANTIC_LEAK"


def audit(root: Path = ROOT, files=None) -> dict:
    findings, binary = [], []
    for path in (files if files is not None else repository_files(root)):
        if path == str(OUT):
            continue
        p = root / path
        if path.endswith(BINARY):
            binary.append(path)
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            binary.append(path)
            continue
        occ = occurrences(text)
        if not occ:
            continue
        cls, why = classify(path)
        counts: dict = {}
        leaks = []
        if cls == CURRENT:
            lines = text.splitlines()
            for n, name in occ:
                c = judge(lines, n, name)
                counts.setdefault(c, {}).setdefault(name, 0)
                counts[c][name] += 1
                if c == "CURRENT_AI_SEMANTIC_LEAK":
                    leaks.append({"line": n, "pattern": name})
        else:
            for _, name in occ:
                counts.setdefault(cls, {}).setdefault(name, 0)
                counts[cls][name] += 1
        findings.append({"path": path, "rule_class": cls, "why": why,
                         "occurrences": counts, "leaks": leaks})
    by_class = {c: {"files": 0, "occurrences": 0}
                for c in CLASSES + ("UNCLASSIFIED",)}
    for f in findings:
        for c, per in f["occurrences"].items():
            by_class[c]["files"] += 1
            by_class[c]["occurrences"] += sum(per.values())
    return {
        "schema": "pass-semantics-audit/1",
        "patterns": {k: v.pattern for k, v in PATTERNS.items()},
        "window_lines": WINDOW,
        "legacy_label": [rx.pattern for rx in LEGACY_LABEL],
        "rules": [{"path": p, "class": c, "why": w}
                  for p, c, w in PATH_RULES],
        "by_class": by_class,
        "current_ai_semantic_leaks":
            by_class["CURRENT_AI_SEMANTIC_LEAK"]["occurrences"],
        "unclassified": by_class["UNCLASSIFIED"]["files"],
        "binary_not_scanned": len(binary),
        "findings": findings,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    rep = audit()
    if args.write:
        (ROOT / OUT).parent.mkdir(parents=True, exist_ok=True)
        (ROOT / OUT).write_text(json.dumps(rep, indent=1, sort_keys=True)
                                + "\n", encoding="utf-8")
    for c, v in rep["by_class"].items():
        if v["files"]:
            print(f"  {c:28s} {v['files']:4d} files "
                  f"{v['occurrences']:5d} occurrences")
    for f in rep["findings"]:
        for leak in f["leaks"]:
            print(f"  LEAK {f['path']}:{leak['line']} ({leak['pattern']})")
    ok = rep["current_ai_semantic_leaks"] == 0 and rep["unclassified"] == 0
    print(f"PASS semantics audit: {rep['current_ai_semantic_leaks']} "
          f"current-AI leak(s), {rep['unclassified']} unclassified -- "
          f"{'PASS' if ok else 'FAIL'}")
    if args.check:
        path = ROOT / OUT
        if not path.exists():
            print("no committed report: run --write")
            ok = False
        elif json.loads(path.read_text(encoding="utf-8")) != rep:
            print("the committed report is stale: run --write")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
