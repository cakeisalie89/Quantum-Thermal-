#!/usr/bin/env python3
"""Deterministic RO-Crate 1.1 builder + validator for the Scientific AI
Harness.

Specification: RO-Crate 1.1 (https://w3id.org/ro/crate/1.1) -- implemented
as hand-built deterministic JSON-LD with an in-repo structural validator;
the community validator (roc-validator) is run beside it on a hosted runner
by tools/ro_crate_conformance.py.

The crate is PROVENANCE METADATA connecting the harness's own files: the
workflow, its locked environment, its registries and derived status, the
forensic inventory and content audit, and the learned-model evidence
manifests. It asserts no experiment performed and no physical validation;
a checksum here attests bytes, never that anything in them is correct.

Determinism: sorted entity/key ordering; relative POSIX paths only; no
UUIDs; no wall-clock timestamps; stable checksum ordering; fail-closed
on missing files, checksum or size mismatch, unresolved references, and a
crate the checks cannot read.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
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
    ("../Snakefile", ["File", "SoftwareSourceCode"],
     "the governed scientific workflow (Snakemake); its default target runs "
     "the harness end to end"),
    ("../tools/harness_demo.py", ["File", "SoftwareSourceCode"],
     "the end-to-end demonstration, proposal to decision, with its negative "
     "twin"),
    ("../scientific/catalog.py", ["File", "SoftwareSourceCode"],
     "the admitted scientific models and their independent checks"),
    ("../pyproject.toml", "File", "project and tooling authority"),
    ("../uv.lock", "File", "locked dependency resolution"),
    ("../stack.json", "File", "software stack registry: what is in use and "
                              "how it is checked"),
    ("../authorities.json", "File", "which module owns which concept"),
    ("../docs/harness_contract.json", "File",
     "the software completion contract (definition of done)"),
    ("../docs/harness_status.json", "File",
     "the completion status derived from the contract's inputs"),
    ("../docs/completion_matrix.json", "File", "the completion matrix"),
    ("../docs/repository_inventory.json", "File",
     "forensic inventory: every baseline file classified, with its "
     "disposition"),
    ("../docs/repo_content_audit.json", "File",
     "the repository content audit of the current tree"),
    ("../docs/equivalence_contracts.json", "File",
     "scientific equivalence contracts by model output"),
    ("../docs/neural/dev/dataset_manifest.json", "File",
     "NF-1T development dataset manifest (generator identity and digest)"),
    ("../docs/neural/claims.json", "File",
     "NF-1T claims, recorded through the authority substrate"),
    ("../final_manifest.json", "File", "deterministic manifest"),
    ("../manifest_hash.txt", "File", "detached manifest hash"),
    ("../HDF5_DATA_MODEL.md", "File", "HDF5 result-bundle data model"),
    ("../README.md", "File", "what the harness is and how to run it"),
]

#: The locked packages the environment entity names, read from uv.lock so
#: the crate cannot state a version the lock does not resolve.
ENVIRONMENT_PACKAGES = ("numpy", "scipy", "h5py", "snakemake", "pydantic",
                        "pytest", "hypothesis", "ruff", "mypy")


def locked_versions(lock: Path = Path("uv.lock")) -> str:
    doc = tomllib.loads(lock.read_text(encoding="utf-8"))
    have = {p["name"]: p["version"] for p in doc["package"]
            if "version" in p}
    missing = [n for n in ENVIRONMENT_PACKAGES if n not in have]
    if missing:
        raise KeyError(f"uv.lock resolves no {missing}")
    return "python 3.12; " + "; ".join(
        f"{n} {have[n]}" for n in ENVIRONMENT_PACKAGES) + "; uv 0.11.7"


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


#: What the root description must say, checked by :func:`validate`: the
#: crate is software provenance and nothing more.
ROOT_DISCLAIMERS = (
    "No experiment has been performed",
    "not experimental validation",
    "NON_AUTHORITATIVE",
    "attests bytes, not correctness",
)

#: The manifest and its detached hash. The manifest hashes this crate, so the
#: crate cannot hash them -- a circular dependency -- and states their sizes
#: alone. Nothing else may go without a checksum (AUD-077).
NO_EMBED = frozenset({"final_manifest.json", "manifest_hash.txt"})
NOT_EMBEDDED = ("not-embedded: circular dependency -- the manifest covers "
                "this crate; the crate's own hash lives in the manifest")


def build(dest_dir: Path = CRATE_DIR) -> None:
    dest = dest_dir / "ro-crate-metadata.json"
    graph: list = [
        {"@id": "ro-crate-metadata.json",
         "@type": "CreativeWork",
         "about": {"@id": "./"},
         "conformsTo": {"@id": SPEC}},
        {"@id": "./", "@type": "Dataset",
         "name": "Scientific AI Harness -- governed scientific-agent "
                 "software and its provenance",
         "description":
             "A governed scientific-agent harness: an event-sourced "
             "authority substrate, replaceable scientific models checked "
             "by independent implementations, a learned-model substrate "
             "and its evidence, and the software assurance that holds "
             "them. No experiment has been performed; a simulation result "
             "here is not experimental validation; learned outputs are "
             "NON_AUTHORITATIVE. Every checksum in this crate attests "
             "bytes, not correctness.",
         "license": {"@id": "#license-unspecified"},
         "datePublished": DATE_PUBLISHED,
         "hasPart": [{"@id": rel.replace("../", "")} for rel, _, _
                     in FILES],
         "mainEntity": {"@id": "Snakefile"},
         "mentions": [{"@id": "#workflow-action"},
                      {"@id": "#environment"}]},
        {"@id": "#license-unspecified", "@type": "CreativeWork",
         "name": "license: no authoritative project license record "
                 "exists; none invented",
         "description": "the repository carries no license record; this "
                        "entity says so rather than naming one"},
        {"@id": "#environment", "@type": "SoftwareApplication",
         "name": "locked execution environment (uv --frozen)",
         "softwareRequirements": locked_versions()},
        {"@id": "#workflow-action", "@type": "CreateAction",
         "name": "the governed workflow's default target",
         "instrument": {"@id": "Snakefile"},
         "object": {"@id": "pyproject.toml"},
         "result": [{"@id": "docs/harness_status.json"}],
         "description": "snakemake --cores 1 runs the demonstration, the "
                        "governed model, artifact and index paths, the "
                        "representations and the tree check, writing only "
                        "under verification/; the completion status is "
                        "derived from the registries and evidence by "
                        "tools/harness_status.py"},
    ]
    for rel, typ, desc in FILES:
        rid = rel.replace("../", "")
        p = Path(rid)
        if not p.exists():
            print(f"[RO-Crate FAIL-CLOSED] missing referenced file {p}")
            sys.exit(1)
        ent = {"@id": rid, "@type": typ,
               "name": p.name, "description": desc,
               "contentSize": str(p.stat().st_size)}
        ent["sha256"] = NOT_EMBEDDED if rid in NO_EMBED else sha_file(p)
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
    "verification/ro_crate/validation_report.json")


#: How deep the metadata may nest. The committed crate nests five levels; the
#: checks below walk it and ``json.dumps`` recurses, so a document nested past
#: the interpreter's limit raised RecursionError out of the validator rather
#: than a verdict (AUD-078).
_MAX_DEPTH = 32


def _depth(doc) -> int:
    """How deep ``doc`` nests, measured without recursion."""
    deepest, todo = 0, [(doc, 1)]
    while todo:
        o, d = todo.pop()
        if isinstance(o, dict):
            o = list(o.values())
        if isinstance(o, list):
            deepest = max(deepest, d)
            todo.extend((x, d + 1) for x in o)
    return deepest


def _shape(doc) -> list:
    """What the checks index, in the validator's own words. A crate they
    could not read raised KeyError, TypeError or AttributeError out of the
    validator, and a crash is not a refusal (AUD-078)."""
    if not isinstance(doc, dict):
        return ["the metadata is not a JSON object"]
    if _depth(doc) > _MAX_DEPTH:
        return [f"the metadata nests deeper than {_MAX_DEPTH} levels"]
    g = doc.get("@graph")
    if not isinstance(g, list):
        return ["the metadata has no @graph that is a list of entities"]
    out = [f"entity {i} is not an object with a string @id"
           for i, e in enumerate(g)
           if not (isinstance(e, dict) and isinstance(e.get("@id"), str))]
    if not out:
        for e in g:
            parts = e.get("hasPart") if e["@id"] == "./" else []
            if not (isinstance(parts, list) and all(
                    isinstance(x, dict) and isinstance(x.get("@id"), str)
                    for x in parts)):
                out.append("the root's hasPart is not a list of references, "
                           "each an object with a string @id")
    return out


def _in_tree(rid: str) -> bool:
    """A data entity names a file of this tree: a relative POSIX path that
    does not climb out of it. The validator hashes the file at the entity's
    ``@id``, so a crate naming ``/etc/hostname`` had it hash bytes the
    repository does not hold, and say VALID (AUD-077)."""
    segments = rid.split("/")
    return (rid != "" and "\x00" not in rid and not rid.startswith("/")
            and ".." not in segments and ":" not in segments[0])


def _file_problems(rid: str, ent: dict) -> list:
    """What is wrong with what the crate states about the file at ``rid``:
    its checksum and its size. A file that cannot be read -- a directory, a
    name too long for the system -- raised IsADirectoryError or OSError out
    of the validator (AUD-078); it is refused."""
    p = Path(rid)
    try:
        if not p.exists():
            return [f"referenced file missing: {rid}"]
        out = []
        # The manifest and its detached hash go without a checksum -- the
        # manifest covers this crate -- and nothing else may: an exemption
        # any entity could claim was a checksum nobody verified, under VALID
        # (AUD-077).
        sha = ent.get("sha256")
        if not isinstance(sha, str):
            out.append(f"data entity {rid} states no checksum")
        elif sha.startswith("not-embedded"):
            if rid not in NO_EMBED:
                out.append(f"data entity {rid} claims the manifest's "
                           f"exemption from a checksum; only "
                           f"{sorted(NO_EMBED)} may")
        elif sha_file(p) != sha:
            out.append(f"checksum mismatch: {rid}")
        # Every size the crate states is the file's, the manifest's too,
        # which no checksum here covers. Unchecked, the committed crate
        # stated the size the manifest had before its last regeneration
        # (AUD-076).
        size = str(p.stat().st_size)
        if ent.get("contentSize") != size:
            out.append(f"contentSize mismatch: {rid} is stated as "
                       f"{ent.get('contentSize')!r}; the file has {size} "
                       f"bytes")
        return out
    except OSError as exc:
        return [f"data entity {rid} cannot be read: {exc}"]


def _verdict(problems: list, entities: int, files: int, rep: Path) -> int:
    """Print the verdict and write it to ``rep`` -- every verdict, a refusal
    before the checks included. Those wrote nothing, and the file kept the
    last verdict: VALID, over a crate just refused (AUD-078)."""
    print(f"RO-Crate validation: {entities} entities | {files} referenced "
          f"files | problems {len(problems)} {problems[:3]}")
    print(f"RESULT: {'VALID' if not problems else 'FAIL'}")
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(
        json.dumps({"spec": SPEC, "entities": entities,
                    "referenced_files": files,
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


def validate(meta_path: Path = META,
             report_path: Path | None = None) -> int:
    rep = Path(report_path) if report_path is not None \
        else DEFAULT_VALIDATION_REPORT
    try:
        doc = json.loads(meta_path.read_text())
    except (OSError, ValueError, RecursionError) as exc:
        return _verdict([f"the metadata is not readable JSON: "
                         f"{type(exc).__name__}: {exc}"], 0, 0, rep)
    shape = _shape(doc)
    if shape:
        g_ = doc.get("@graph") if isinstance(doc, dict) else None
        return _verdict(shape, len(g_) if isinstance(g_, list) else 0, 0,
                        rep)
    g: list = doc["@graph"]
    ids = [e["@id"] for e in g]
    problems = []
    if len(ids) != len(set(ids)):
        problems.append("duplicate entity IDs")
    if not any(e["@id"] == "./" and "Dataset" in str(e.get("@type"))
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
        about = desc.get("about")
        if not (isinstance(about, dict) and about.get("@id") == "./"):
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
    if "./" not in by or "#workflow-action" not in by:
        # Without these the checks below index a dict that has no such key,
        # and the validator dies with a KeyError traceback instead of
        # returning a verdict. A crash is not a refusal.
        if "#workflow-action" not in by:
            problems.append("workflow CreateAction missing")
        return _verdict(problems, len(g), 0, rep)

    # A VALIDATION OF NOTHING IS NOT A VALIDATION (D-2026-39).
    #
    # Every checksum this function verifies is verified inside the loop
    # below, over `hasPart`. Handed a crate with a well-formed root, a
    # CreateAction and `"hasPart": []`, the loop ran zero times, nothing was
    # checked, and this printed RESULT: VALID and exit 0 -- writing
    # `"result": "VALID"` into a report the manifest hashes. Reproduced
    # before this guard existed, in the same sweep that found the same shape
    # in a since-retired equivalence check.
    #
    # The second half is scope COMPLETENESS rather than mere non-emptiness:
    # an entity that carries a checksum and sits outside `hasPart` is one
    # this loop never reaches. Contextual entities -- the `#`-prefixed ones,
    # which describe things that are not files in this tree -- are excluded:
    # a rule that flagged them would be a rule about the wrong thing.
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
        if not {"File", "Dataset"} & {t for t in types
                                      if isinstance(t, str)}:
            problems.append(f"data entity {rid} is typed {types}; RO-Crate "
                            "1.1 requires File or Dataset among its types")
        if not _in_tree(rid):
            problems.append(f"data entity {rid!r} is not a path inside "
                            "this tree")
            continue
        problems += _file_problems(rid, by[rid])
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
    for claim in ("experimentally validated", "experiment performed",
                  "validated digital twin", "certified", "proves "
                  "correctness"):
        if claim in blob:
            problems.append(f"claim-boundary violation: '{claim}'")
    root = by["./"].get("description")
    if not isinstance(root, str):
        problems.append("the root description is not text")
        root = ""
    for must in ROOT_DISCLAIMERS:
        if must not in root:
            problems.append(f"root description missing '{must}'")
    if by["#workflow-action"].get("@type") != "CreateAction":
        problems.append("workflow CreateAction missing")
    return _verdict(problems, len(g), len(by["./"]["hasPart"]), rep)


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
