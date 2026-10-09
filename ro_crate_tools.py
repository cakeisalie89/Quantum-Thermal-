#!/usr/bin/env python3
"""Deterministic RO-Crate 1.1 builder + validator (Stage 8).

Specification: RO-Crate 1.1 (https://w3id.org/ro/crate/1.1) — implemented
as hand-built deterministic JSON-LD with an in-repo structural validator;
no third-party crate library is used (recorded limitation: no external
conformance validator was executable in this sandbox).

MODEL-ONLY / FORECAST-ONLY. The crate is PROVENANCE METADATA connecting
existing artifacts. It asserts no experiment performed, no hardware
validation, no isotope-contrast demonstration, no completed growth, no
digital-twin validation, no COMSOL equivalence; gate PASS stays zero;
Campaign-1 stays PROPOSED_NOT_PERFORMED; experiments stay at their
Stage-7.5 statuses; outputs are simulation/forecast artifacts; the HDF5
file is a structured representation of existing outputs, not evidence.

Determinism: sorted entity/key ordering; relative POSIX paths only; no
UUIDs; no wall-clock timestamps; stable checksum ordering; fail-closed
on missing files, checksum mismatch, unresolved references.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

CRATE_DIR = Path("ro-crate")
#: RO-Crate 1.1 REQUIRES the root's datePublished (ISO 8601). A DECLARED
#: date -- when this crate was first published under the 1.1 profile with
#: external conformance checking -- not the time of generation: the crate is
#: rebuilt deterministically, and a clock value would make every rebuild a
#: different file. It changes only by an edit here.
DATE_PUBLISHED = "2026-10-08"
META = CRATE_DIR / "ro-crate-metadata.json"
SPEC = "https://w3id.org/ro/crate/1.1"
#: The checksum term. ``sha256`` is not a term of the RO-Crate 1.1 context,
#: and a key the context does not define is not allowed in the compacted
#: form the specification requires (its check 3.1) -- the community
#: validator refused the crate on exactly that, run on hosted CI
#: (D-2026-122). So the crate defines it, the way RO-Crate 1.1 adds a term:
#: in its own ``@context``, mapped to the workflow-run vocabulary's
#: ``sha256``, which the Process Run Crate profile uses for the same thing.
WFRUN_SHA256 = "https://w3id.org/ro/terms/workflow-run#sha256"
CONTEXT = [SPEC + "/context", {"sha256": WFRUN_SHA256}]
#: The RO-Crate 1.1 context terms this crate uses -- schema.org properties
#: the published context maps. A key that is neither one of these nor
#: defined in the crate's own ``@context`` is refused by :func:`validate`:
#: a short list it KNOWS, rather than the whole context it cannot fetch in
#: a sandbox, so an unlisted real term is refused (fail closed) and never
#: an undefined one accepted.
CONTEXT_TERMS = frozenset({
    "about", "conformsTo", "contentSize", "datePublished", "description",
    "hasPart", "identifier", "instrument", "license", "mainEntity",
    "mentions", "name", "object", "result", "softwareRequirements",
})

FILES = [
    ("../qta_full_sim.py", ["File", "SoftwareSourceCode"],
     "canonical simulation runner (authoritative release path)"),
    ("../qta_sim_stages.py", ["File", "SoftwareSourceCode"],
     "checkpointed staged driver (Stage 7.5; identical calls)"),
    ("../qta_multiphysics/runner_3d.py", ["File", "SoftwareSourceCode"],
     "3-D reduced-CI layer runner"),
    ("../Snakefile", ["File", "SoftwareSourceCode"],
     "Snakemake workflow wrapping authoritative commands"),
    ("../pyproject.toml", "File", "project/tooling authority"),
    ("../uv.lock", "File", "locked dependency resolution (71+ pkgs)"),
    ("../stage7_reports/environment_provenance.json", "File",
     "environment provenance record"),
    ("../hdf5_output_mapping.json", "File",
     "88-output -> HDF5 mapping registry (schema 1.0.0)"),
    ("../hdf5_schema.json", "File", "HDF5 dataset schema (1.0.0)"),
    ("../qta_scientific_results.h5", "File",
     "HDF5 representation of existing simulation outputs "
     "(NOT new scientific evidence)"),
    ("../results_gate_table.csv", "File",
     "canonical gate table (83 gates; scientific PASS count = 0)"),
    ("../validation_matrix.csv", "File", "validation matrix (43 open "
     "items dispositioned; nonlinear_threshold unresolved)"),
    ("../experiment_registry.json", "File",
     "Stage-6 experiment registry (11 designed experiments; EXP-V1 "
     "PLAYBOOK_READY; Campaign-1 PROPOSED_NOT_PERFORMED)"),
    ("../experiment_gate_coverage.json", "File", "25/25 gate coverage"),
    ("../experiment_matrix_coverage.json", "File",
     "43/43 matrix dispositions"),
    ("../campaign_registry.json", "File",
     "campaign registry (Campaign-1 PROPOSED_NOT_PERFORMED)"),
    ("../package_consistency_check.py", ["File", "SoftwareSourceCode"],
     "package checker (default full-regeneration release gate + "
     "fail-closed --verify-existing)"),
    ("../manuscript_consistency_check.py", ["File", "SoftwareSourceCode"],
     "manuscript checker"),
    ("../stage6_preservation_check.py", ["File", "SoftwareSourceCode"],
     "Stage-6 preservation checker"),
    ("../stage8_reports/hdf5_equivalence_report.json", "File",
     "HDF5<->source exact-equivalence report"),
    ("../final_manifest.json", "File", "deterministic manifest"),
    ("../manifest_hash.txt", "File", "detached manifest hash"),
    ("../RUNTIME_RESILIENCE.md", "File", "Stage-7.5 documentation"),
    ("../HDF5_DATA_MODEL.md", "File", "HDF5 data-model documentation"),
]


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(dest_dir: Path = CRATE_DIR) -> None:
    dest = dest_dir / "ro-crate-metadata.json"
    graph: list = [
        {"@id": "ro-crate-metadata.json",
         "@type": "CreativeWork",
         "about": {"@id": "./"},
         "conformsTo": {"@id": SPEC}},
        {"@id": "./", "@type": "Dataset",
         "name": "Quantum Thermal Architecture -- Stage 8 scientific "
                 "package (forecast-only, pre-hardware)",
         "description":
             "Theoretical, forecast-only, pre-hardware, validation-gated "
             "multiphysics model. No experiment has been performed; no "
             "hardware validation, isotope-contrast demonstration, "
             "completed growth, digital-twin validation, or COMSOL "
             "equivalence is claimed. Scientific gate PASS count is "
             "zero; can_PASS_now=NO; measured_in_this_system=false; "
             "automatic_gate_effect=NONE; automatic_application=false. "
             "Campaign-1 is PROPOSED_NOT_PERFORMED; experiments are "
             "DESIGNED (EXP-V1 PLAYBOOK_READY). Outputs are simulation/"
             "forecast artifacts; the HDF5 file is a structured "
             "representation of existing outputs, not new evidence.",
         "license": {"@id": "#license-unspecified"},
         "datePublished": DATE_PUBLISHED,
         "hasPart": [{"@id": rel.replace("../", "")} for rel, _, _
                     in FILES],
         "mainEntity": {"@id": "qta_full_sim.py"},
         "mentions": [{"@id": "#stage7-input-zip"},
                      {"@id": "#simulation-action"},
                      {"@id": "#environment"}]},
        {"@id": "#license-unspecified", "@type": "CreativeWork",
         "name": "license: no authoritative project license record "
                 "exists; none invented",
         "description": "the repository carries no license record; this "
                        "entity says so rather than naming one"},
        {"@id": "#stage7-input-zip", "@type": "Dataset",
         "name": "authoritative Stage-7.5 input archive",
         "identifier": "QTA_stage7_5_runtime_resilience_source.zip",
         "sha256": "c29cbc84c6155c2204ce7fb8c72d4499246cb4fdaeecd987"
                    "d9cf29448c10ac82",
         "contentSize": "20380753"},
        {"@id": "#environment", "@type": "SoftwareApplication",
         "name": "locked execution environment (uv --frozen)",
         "softwareRequirements":
             "python 3.12.3; numpy 2.4.4; scipy 1.17.1; qutip 5.2.1; "
             "h5py 3.16.0 (libhdf5 2.0.0); snakemake 9.23.1; "
             "pydantic 2.13.4; pytest 9.1.1; hypothesis 6.158.1; "
             "ruff 0.15.22; mypy 2.3.0; uv 0.11.7"},
        {"@id": "#simulation-action", "@type": "CreateAction",
         "name": "canonical simulation regeneration (forecast-only)",
         "instrument": {"@id": "qta_full_sim.py"},
         "object": {"@id": "pyproject.toml"},
         "result": [{"@id": "qta_scientific_results.h5"},
                    {"@id": "results_gate_table.csv"}],
         "description": "monolithic release verification: exit 0, "
                        "complete 89-file set, 88/88 governed outputs "
                        "byte-identical; staged checkpointed path proven "
                        "equivalent (Stage 7.5)"},
    ]
    NO_EMBED = {"final_manifest.json", "manifest_hash.txt"}
    for rel, typ, desc in FILES:
        p = Path(rel.replace("../", ""))
        if not p.exists():
            print(f"[RO-Crate FAIL-CLOSED] missing referenced file {p}")
            sys.exit(1)
        ent = {"@id": rel.replace("../", ""), "@type": typ,
               "name": p.name, "description": desc,
               "contentSize": str(p.stat().st_size)}
        if p.name in NO_EMBED:
            ent["sha256"] = ("not-embedded: circular dependency -- the "
                            "manifest covers this crate; the crate's own "
                            "hash lives in the manifest")
        else:
            ent["sha256"] = sha_file(p)
        graph.append(ent)
    doc = {"@context": CONTEXT, "@graph":
           sorted(graph, key=lambda e: str(e["@id"]))}
    dest_dir.mkdir(exist_ok=True)
    dest.write_text(json.dumps(doc, indent=1, sort_keys=True,
                               ensure_ascii=False) + "\n")
    print(f"[RO-Crate] built {dest} ({dest.stat().st_size} B; "
          f"{len(graph)} entities; sha256 {sha_file(dest)[:16]}...)")


#: Where :func:`validate` writes its verdict. A parameter rather than a
#: constant reached for inside the function, because the function writes a
#: TRACKED artefact and a caller that only wants the verdict -- a test, say --
#: must be able to say so. Without it, running the validator against a
#: throwaway crate overwrote the committed report with a verdict about the
#: throwaway, and the manifest then hashed a file that no longer existed in
#: that form. Found immediately, by the manifest completeness suite, in the
#: same session as the ledger item about tests that damage tracked files.
DEFAULT_VALIDATION_REPORT = Path(
    "stage8_reports/ro_crate_validation_report.json")


def validate(meta_path: Path = META,
             report_path: Path | None = None) -> int:
    doc: dict = json.loads(meta_path.read_text())
    g: list = doc["@graph"]
    ids = [e["@id"] for e in g]
    problems = []
    if len(ids) != len(set(ids)):
        problems.append("duplicate entity IDs")
    if not any(e["@id"] == "./" and "Dataset" in str(e["@type"])
               for e in g):
        problems.append("root dataset entity missing")
    by = {e["@id"]: e for e in g}
    # Every key a term: of the RO-Crate 1.1 context, or of the crate's own
    # (RO-Crate 1.1 s.3, the compacted form). Missed until the community
    # validator refused 25 undefined "sha256" keys on hosted CI (D-2026-122).
    ctx = doc.get("@context")
    ctx = ctx if isinstance(ctx, list) else [ctx]
    if not ctx or ctx[0] != SPEC + "/context":
        problems.append(f"@context does not start with the RO-Crate 1.1 "
                        f"context: {doc.get('@context')!r}")
    local = {k for c in ctx if isinstance(c, dict) for k in c}
    undefined: set = set()

    def _keys(o) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if not k.startswith("@") and k not in CONTEXT_TERMS \
                        and k not in local:
                    undefined.add(k)
                _keys(v)
        elif isinstance(o, list):
            for x in o:
                _keys(x)
    _keys(g)
    if undefined:
        problems.append(f"keys no context defines: {sorted(undefined)}; the "
                        "compacted form allows only defined terms")
    # The metadata descriptor (RO-Crate 1.1 s.4.1, REQUIRED): it must exist,
    # be about the root, and say which specification the crate conforms to.
    # Missed until the external validator's negative controls asked
    # (tools/ro_crate_conformance.py, D-2026-106).
    desc = by.get("ro-crate-metadata.json")
    if desc is None:
        problems.append("metadata descriptor entity missing")
    else:
        if (desc.get("about") or {}).get("@id") != "./":
            problems.append("metadata descriptor is not about the root")
        conforms = desc.get("conformsTo")
        conforms = conforms if isinstance(conforms, list) else [conforms]
        if not any(isinstance(c, dict) and str(c.get("@id", "")).startswith(
                "https://w3id.org/ro/crate/1.") for c in conforms):
            problems.append("metadata descriptor has no conformsTo an "
                            "RO-Crate 1.x specification")
    root_ = by.get("./") or {}
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",
                        str(root_.get("datePublished", ""))):
        problems.append("root dataset has no ISO 8601 datePublished "
                        "(RO-Crate 1.1, REQUIRED)")
    if "./" not in by or "#simulation-action" not in by:
        # Without these the checks below index a dict that has no such key,
        # and the validator dies with a KeyError traceback instead of
        # returning a verdict. A crash is not a refusal.
        print(f"RO-Crate validation: {len(g)} entities | problems "
              f"{len(problems)} {problems[:3]}")
        print("RESULT: FAIL")
        return 1

    # A VALIDATION OF NOTHING IS NOT A VALIDATION (D-2026-39).
    #
    # Every checksum this function verifies is verified inside the loop
    # below, over `hasPart`. Handed a crate with a well-formed root, a
    # CreateAction and `"hasPart": []`, the loop ran zero times, nothing was
    # checked, and this printed RESULT: VALID and exit 0 -- writing
    # `"result": "VALID"` into a report the manifest hashes. Reproduced
    # before this guard existed, in the same sweep that found the same shape
    # in validate_hdf5_equivalence.py, which shares this step of the
    # regeneration order.
    #
    # The second half is scope COMPLETENESS rather than mere non-emptiness:
    # an entity that carries a checksum and sits outside `hasPart` is one
    # this loop never reaches. Contextual entities -- the `#`-prefixed ones,
    # which describe things that are not files in this tree -- are excluded,
    # because `#stage7-input-zip` is legitimately one of those and a rule
    # that flagged it would be a rule about the wrong thing.
    parts = {p["@id"] for p in by["./"]["hasPart"]}
    if not parts:
        problems.append(
            "the crate references no files, so no checksum was verified; a "
            "validation with an empty scope is not a validation")
    unchecked = sorted(e["@id"] for e in g
                       if "sha256" in e and not e["@id"].startswith("#")
                       and e["@id"] not in parts)
    if unchecked:
        problems.append(
            f"entities carry a checksum that nothing verifies, because they "
            f"are outside hasPart: {unchecked}")

    for part in by["./"]["hasPart"]:
        rid = part["@id"]
        if rid not in by:
            problems.append(f"unresolved hasPart {rid}")
            continue
        # RO-Crate 1.1 s.4.1: a data entity is a File or a Dataset, whatever
        # else it also is. The scripts were typed SoftwareSourceCode alone
        # and this validator passed them; the community validator, run on
        # hosted CI, did not (D-2026-111).
        types = by[rid].get("@type")
        types = types if isinstance(types, list) else [types]
        if not {"File", "Dataset"} & set(types):
            problems.append(f"data entity {rid} is typed {types}; RO-Crate "
                            "1.1 requires File or Dataset among its types")
        p = Path(rid)
        if not p.exists():
            problems.append(f"referenced file missing: {rid}")
        elif by[rid]["sha256"].startswith("not-embedded"):
            pass  # manifest pair: existence-only (circular dependency)
        elif sha_file(p) != by[rid]["sha256"]:
            problems.append(f"checksum mismatch: {rid}")
    # the root description is the sanctioned negative-claims disclaimer;
    # scanning it for its own negated phrases would self-flag -- exclude
    # it (and only it) from the claim scan, keep it in the path scan.
    scan_doc = json.loads(json.dumps(doc))
    for e in scan_doc["@graph"]:
        if e["@id"] == "./":
            e["description"] = "(sanctioned disclaimer excluded from scan)"
    blob_paths = json.dumps(doc)
    blob = json.dumps(scan_doc)
    for bad in ("/home/", "/tmp/", "C:\\\\"):
        if bad in blob_paths:
            problems.append(f"forbidden path fragment '{bad}'")
    for claim in ("hardware-validated", "experiment performed",
                  "isotope contrast demonstrated", "completed growth",
                  "validated digital twin", "COMSOL-equivalent"):
        if claim in blob:
            problems.append(f"claim-boundary violation: '{claim}'")
    root = by["./"]["description"]
    for must in ("PASS count is zero", "PROPOSED_NOT_PERFORMED",
                 "measured_in_this_system=false",
                 "not new evidence"):
        if must not in root:
            problems.append(f"root description missing '{must}'")
    if by["#simulation-action"]["@type"] != "CreateAction":
        problems.append("simulation CreateAction missing")
    print(f"RO-Crate validation: {len(g)} entities | "
          f"{len(by['./']['hasPart'])} referenced files | "
          f"problems {len(problems)} {problems[:3]}")
    print(f"RESULT: {'VALID' if not problems else 'FAIL'}")
    rep = Path(report_path) if report_path is not None \
        else DEFAULT_VALIDATION_REPORT
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(
        json.dumps({"spec": SPEC, "entities": len(g),
                    "referenced_files": len(by["./"]["hasPart"]),
                    "problems": problems,
                    "result": "VALID" if not problems else "FAIL",
                    "limitation": "structural in-repo validator; the "
                                   "RO-Crate community validator "
                                   "(roc-validator, profile ro-crate-1.1) "
                                   "is run beside it by "
                                   "tools/ro_crate_conformance.py on a "
                                   "hosted runner, which can reach the "
                                   "JSON-LD context this sandbox cannot"},
                   indent=1, sort_keys=True) + "\n")
    return 1 if problems else 0


#: Tokens that look like subcommands. `ro_crate_tools.py build` used to be
#: parsed as "build into a directory named build/", silently creating a stray
#: tree instead of rebuilding the crate. Refuse them rather than guessing.
_LIKELY_SUBCOMMAND_TYPOS = frozenset({
    "build", "make", "generate", "gen", "create", "check", "verify", "run",
    "all", "help",
})

_USAGE = """\
usage:
  python ro_crate_tools.py                 rebuild the crate in ./ro-crate
  python ro_crate_tools.py --output DIR    rebuild into DIR
  python ro_crate_tools.py validate        validate the existing crate

note: there is no 'build' subcommand -- rebuilding is the default action.
      A bare directory argument is still accepted for backward compatibility,
      but tokens that look like subcommands or options are refused."""


def _cli(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    if args and args[0] == "validate":
        if len(args) > 1:
            print(f"validate takes no arguments; got {args[1:]}")
            return 2
        return validate()
    if args and args[0] == "--output":
        if len(args) != 2:
            print("--output needs exactly one directory\n\n" + _USAGE)
            return 2
        build(Path(args[1]))
        return 0
    if len(args) > 1:
        print(f"unexpected arguments {args[1:]}\n\n" + _USAGE)
        return 2
    if args:
        # A mistyped or unsupported FLAG must never be taken as a directory
        # name. `--validate` (there is no such flag; the subcommand is
        # `validate`) was silently accepted as an output path and created a
        # directory literally named "--validate".
        if args[0].startswith("-"):
            print(f"unknown option {args[0]!r}; it is not an output "
                  f"directory.\n\n" + _USAGE)
            return 2
        if args[0].lower() in _LIKELY_SUBCOMMAND_TYPOS:
            print(f"refusing to treat {args[0]!r} as an output directory -- "
                  f"it reads as a subcommand.\n\n" + _USAGE)
            return 2
        build(Path(args[0]))
        return 0
    build(CRATE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
