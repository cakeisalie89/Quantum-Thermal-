#!/usr/bin/env python3
"""Establish, check and consult the byte-reproduction witness profile.

``docs/byte_reproduction_profile.json`` records that a RESOLVED numerical
backend was OBSERVED to regenerate this exact canonical corpus byte-for-byte
from this generator closure and this locked environment
(``scientific/reproduction.py`` says what it binds and what it does not
mean). This tool is the only thing that writes it, and it writes it only
from an observation:

    establish            regenerate through tools/regenerate_instrumented.py,
                         require the exact declared output set, every
                         non-exempt output byte-identical to its committed
                         copy and the backend RESOLVED -- then write a
                         profile with that backend as its witness. Nothing
                         in the canonical tree is written.
    establish --add      the same, adding a witness to a profile that
                         already applies (same corpus, closure and lock).
    check                does the committed profile apply to this tree?
                         (valid; corpus, exemptions, lock and every recorded
                         closure file unchanged) -- static, no regeneration.
    require-reference    is THIS environment a witnessed backend for this
                         tree? The release path's early refusal:
                         CANONICAL_REPRODUCTION_ENVIRONMENT_REQUIRED.
    identity             this environment's backend identity, and how it
                         differs from each witness.

A profile is never edited by hand and never written for a backend that was
not observed. When none can be established here, strict reproduction is
EXTERNALLY_BLOCKED rather than faked.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scientific import reproduction as R  # noqa: E402

PROFILE = Path("docs") / "byte_reproduction_profile.json"
LOCK_FILES = ("uv.lock", "pyproject.toml")
ENTRY = "qta_full_sim.py"
RECORD_SCHEMA = "regeneration-record/1"
#: A hang detector for the regeneration, as in package_consistency_check.
REGEN_TIMEOUT_S = 900
PROBE_TIMEOUT_S = 180


def declared_scope(root: Path):
    import cross_env_semantics
    return cross_env_semantics.declared_scope(root)


def lock_manifest(root: Path) -> dict:
    return R.manifest(root, LOCK_FILES)


def load_profile(root: Path):
    try:
        return json.loads((root / PROFILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return {"__unreadable__": f"{type(exc).__name__}: {exc}"}


def profile_for_tree(root: Path, declared, exempt, *, closure=None):
    """``(profile, problems, inapplicable)`` for the committed profile
    against this tree. ``closure`` is the MEASURED closure of a
    regeneration when there was one; otherwise every file the profile
    recorded is re-hashed from the tree."""
    doc = load_profile(root)
    if doc is None:
        return None, ["no witness profile at " + str(PROFILE)], []
    if "__unreadable__" in doc:
        return None, [f"the witness profile is unreadable: "
                      f"{doc['__unreadable__']}"], []
    problems = R.validate_profile(doc)
    if problems:
        return doc, problems, []
    if closure is None:
        closure = R.manifest(root, doc["generator"]["closure"])
    corpus = R.manifest(root, set(declared) - set(exempt))
    return doc, [], R.applicability(doc, declared=declared, exempt=exempt,
                                    corpus=corpus, closure=closure,
                                    lock=lock_manifest(root))


def probe_environment(root: Path, distributions) -> dict | None:
    """The environment record of a fresh process with THIS process's
    environment -- the one a regeneration started from here would get."""
    code = ("import json, sys; sys.path.insert(0, %r);"
            "from scientific.backend_probe import run_environment;"
            "print(json.dumps(run_environment(distributions=tuple(%r))))"
            % (str(root), list(distributions)))
    try:
        out = subprocess.run([sys.executable, "-c", code], cwd=str(root),
                             capture_output=True, text=True, check=True,
                             timeout=PROBE_TIMEOUT_S)
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _distributions(doc) -> list:
    for w in (doc or {}).get("witnesses", []):
        return sorted((w.get("backend") or {}).get("native", {}))
    return ["numpy", "scipy"]


def reference_environment(root: Path) -> dict:
    """Whether this environment may answer the strict question for this
    tree: a valid profile that applies, and this backend among its
    witnesses. Used BEFORE any regeneration."""
    declared, exempt = declared_scope(root)
    doc, problems, inapplicable = profile_for_tree(root, declared, exempt)
    if problems:
        return {"ok": False, "reasons": [f"witness profile: {p}"
                                         for p in problems]}
    if inapplicable:
        return {"ok": False, "reasons": [
            f"the witness profile does not apply to this tree: {r} -- "
            "re-establish it on a backend that reproduces the corpus "
            "(tools/reproduction_witness.py establish)"
            for r in inapplicable]}
    record = probe_environment(root, _distributions(doc))
    identity = R.backend_identity(record)
    if identity is None:
        return {"ok": False, "backend_status": R.UNRESOLVED,
                "reasons": R.unresolved_parts(record)}
    witness = R.witness_for(doc, identity)
    if witness is None:
        diffs = {w["backend_digest"][:16]:
                 R.differences(w["backend"], identity)
                 for w in doc["witnesses"]}
        mine = R.identity_digest(identity)
        return {"ok": False, "backend_status": R.RESOLVED,
                "backend_digest": mine,
                "reasons": [f"this backend ({mine[:16]}) is not one the "
                            f"profile witnessed; it differs from witness "
                            f"{k} in {v}" for k, v in diffs.items()]}
    return {"ok": True, "backend_status": R.RESOLVED,
            "backend_digest": witness["backend_digest"], "reasons": []}


def generation_record_for(path, outputs_dir: Path):
    """A verify-existing tree's generation record, or None. A supplied
    tree does not inherit the identity of the process verifying it: the
    record counts only if it is well formed and names EXACTLY the bytes of
    the outputs it is offered for."""
    if not path:
        return None
    try:
        rec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("schema") != RECORD_SCHEMA:
        return None
    produced = {p.name: R.sha256_file(p) for p in sorted(outputs_dir.iterdir())
                if p.is_file()} if outputs_dir.is_dir() else {}
    if not produced or rec.get("outputs") != produced:
        return None
    return rec if isinstance(rec.get("environment"), dict) else None


def regenerate(root: Path) -> dict:
    """Run the instrumented regeneration in ``root``; its record."""
    with tempfile.TemporaryDirectory(prefix="qta-witness-") as tmp:
        rec_path = Path(tmp) / "record.json"
        proc = subprocess.run(
            [sys.executable, str(root / "tools" /
                                 "regenerate_instrumented.py"),
             "--entry", str(root / ENTRY), "--record", str(rec_path)],
            cwd=str(root), capture_output=True, text=True,
            timeout=REGEN_TIMEOUT_S)
        if proc.returncode != 0 or not rec_path.is_file():
            raise RuntimeError(f"regeneration failed (exit "
                               f"{proc.returncode}): {proc.stderr[-800:]}")
        return json.loads(rec_path.read_text(encoding="utf-8"))


def _git(root: Path, *args) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=str(root),
                              capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def establish(root: Path, *, add: bool = False) -> dict:
    declared, exempt = declared_scope(root)
    rec = regenerate(root)
    produced = set(rec["outputs"])
    if produced != set(declared):
        raise RuntimeError(
            f"the regeneration did not produce exactly the declared set: "
            f"missing {sorted(set(declared) - produced)}, foreign "
            f"{sorted(produced - set(declared))}")
    corpus = R.manifest(root, set(declared) - set(exempt))
    differ = sorted(n for n in corpus if rec["outputs"].get(n) != corpus[n])
    if differ:
        raise RuntimeError(
            f"this backend does not reproduce the committed corpus: "
            f"{len(differ)} file(s) differ, e.g. {differ[:8]} -- no witness "
            "is written for a regeneration that did not reproduce")
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    observed = {"head": _git(root, "rev-parse", "HEAD"),
                "tracked_tree_clean": status == "" if status is not None
                else None,
                "regenerated": len(produced), "compared": len(corpus),
                "byte_identical": len(corpus) - len(differ),
                "on": datetime.datetime.now(datetime.timezone.utc)
                .date().isoformat(),
                "by": "tools/reproduction_witness.py establish"}
    env = rec["environment"]
    if add:
        doc, problems, inapplicable = profile_for_tree(
            root, declared, exempt, closure=rec["closure"])
        if doc is None or problems or inapplicable:
            raise RuntimeError(f"--add needs a valid profile that applies: "
                               f"{problems or inapplicable}")
        doc = R.add_witness(doc, env, observed)
    else:
        doc = R.build_profile(declared=declared, exempt=exempt,
                              corpus=corpus, closure=rec["closure"],
                              lock=lock_manifest(root), entry=ENTRY,
                              argv=rec.get("argv", []), record=env,
                              observed=observed)
    problems = R.validate_profile(doc)
    if problems:
        raise RuntimeError(f"refusing to write an invalid profile: "
                           f"{problems}")
    (root / PROFILE).write_text(json.dumps(doc, indent=1, sort_keys=True)
                                + "\n", encoding="utf-8")
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("establish")
    e.add_argument("--add", action="store_true")
    sub.add_parser("check")
    sub.add_parser("require-reference")
    sub.add_parser("identity")
    args = ap.parse_args(argv)
    root = ROOT

    if args.cmd == "establish":
        try:
            doc = establish(root, add=args.add)
        except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(f"NOT ESTABLISHED: {exc}", file=sys.stderr)
            return 1
        w = doc["witnesses"][-1]
        print(f"witness {w['backend_digest'][:16]} established: "
              f"{w['observed']['byte_identical']} of "
              f"{w['observed']['compared']} canonical files byte-identical; "
              f"closure {len(doc['generator']['closure'])} files; "
              f"{len(doc['witnesses'])} witness(es)")
        return 0

    if args.cmd == "check":
        declared, exempt = declared_scope(root)
        doc, problems, inapplicable = profile_for_tree(root, declared,
                                                       exempt)
        for p in problems + inapplicable:
            print(f"  - {p}")
        if problems or inapplicable:
            print("WITNESS_PROFILE: DOES NOT APPLY to this tree -- "
                  "re-establish it (establish) on a backend that "
                  "reproduces the corpus")
            return 1
        print(f"WITNESS_PROFILE: applies -- {len(doc['corpus']['files'])} "
              f"canonical files, {len(doc['generator']['closure'])} closure "
              f"files, {len(doc['witnesses'])} witness(es)")
        return 0

    if args.cmd == "require-reference":
        r = reference_environment(root)
        if r["ok"]:
            print(f"REFERENCE_ENVIRONMENT: witnessed backend "
                  f"{r['backend_digest'][:16]}")
            return 0
        print("REPRODUCTION_STATUS=CANONICAL_REPRODUCTION_ENVIRONMENT_"
              "REQUIRED")
        for reason in r["reasons"]:
            print(f"  - {reason}")
        return 1

    doc = load_profile(root)
    record = probe_environment(root, _distributions(doc))
    identity = R.backend_identity(record)
    if identity is None:
        print("BACKEND_STATUS=UNRESOLVED")
        for p in R.unresolved_parts(record):
            print(f"  - {p}")
        return 1
    rt = record["runtime"]
    print(f"BACKEND_STATUS=RESOLVED backend={R.identity_digest(identity)}")
    print(f"  numpy dispatch targets: {rt['numpy']['dispatch']['targets']}")
    for b in rt["blas"]["bundled"]:
        print(f"  {b['distribution']} BLAS: kernel {b['kernel']}, threads "
              f"{b['threads']}, {b['config']}")
    for w in (doc or {}).get("witnesses", []):
        d = R.differences(w["backend"], identity)
        print(f"  vs witness {w['backend_digest'][:16]}: "
              + ("SAME BACKEND" if not d else f"differs in {d}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
