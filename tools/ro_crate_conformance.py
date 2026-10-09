#!/usr/bin/env python3
"""The RO-Crate checked by two validators: ours, and a maintained one.

``ro_crate_tools.py validate`` is this repository's structural validator --
written here, so a misunderstanding of the specification it shares with the
generator would pass both. This tool adds the RO-Crate community's own
validator (``roc-validator``, crs4/rocrate-validator, the ``ro-crate-1.1``
profile at REQUIRED severity), installed into an isolated runtime from a
hash-pinned lock (``integrations/ro_crate/validator.lock``), and reports the
two verdicts side by side:

* the crate is PACKAGED for validation: ``ro-crate-metadata.json`` at the
  root of a temporary directory and every data entity it names copied in at
  its ``@id`` -- the crate as a recipient would receive it. An entity whose
  file does not exist is not invented; the validators see the hole;
* AGREE when both accept, or both refuse; DISAGREE otherwise -- reported,
  exit 1, never resolved in favour of the convenient answer;
* negative controls: the same packaging of four broken crates (no root
  dataset, no ``conformsTo``, no ``datePublished``, a dangling data
  entity) must be refused by BOTH validators, or the one that accepts is
  not checking what it is run for.

The external validator expands JSON-LD against the RO-Crate context at
w3id.org, so it needs the network; where that is unreachable the tool says
EXTERNAL_UNAVAILABLE with the error -- it never reports agreement it did not
measure.

    python tools/ro_crate_conformance.py --validator PATH/rocrate-validator \\
        --out report.json
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ro_crate_tools as RC  # noqa: E402

PROFILE = "ro-crate-1.1"
SCHEMA = "ro-crate-conformance/1"


def package(meta: dict, dest: Path, root: Path = ROOT) -> list:
    """The crate as a recipient receives it; returns @ids with no file."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "ro-crate-metadata.json").write_text(
        json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
    missing = []
    for ent in meta.get("@graph", []):
        types = ent.get("@type")
        types = types if isinstance(types, list) else [types]
        if "File" not in types:
            continue
        rel = ent["@id"]
        if rel.startswith(("#", "http://", "https://")) or ".." in rel:
            continue
        src = root / rel
        if not src.is_file():
            missing.append(rel)
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    return sorted(missing)


def internal(meta: dict) -> list:
    """Our validator's verdict on this metadata, as it judges the repo:
    no problems, or its printed refusal."""
    import contextlib
    import io
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ro-crate-metadata.json"
        p.write_text(json.dumps(meta), encoding="utf-8")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = RC.validate(p, report_path=Path(tmp) / "report.json")
        except Exception as exc:                     # noqa: BLE001
            return [f"the internal validator raised {type(exc).__name__}: "
                    f"{exc}"]
        return [] if rc == 0 else [ln for ln in buf.getvalue().splitlines()
                                   if ln.strip()][-5:]


def external(validator: str, crate: Path) -> dict:
    out = crate.parent / f"{crate.name}.report.json"
    r = subprocess.run([validator, "-y", "--disable-color", "validate",
                        str(crate), "-p", PROFILE, "-l", "required",
                        "-f", "json", "-o", str(out)],
                       capture_output=True, text=True, timeout=900)
    if not out.is_file():
        return {"status": "EXTERNAL_UNAVAILABLE",
                "error": (r.stderr or r.stdout).strip()[-1500:]}
    rep = json.loads(out.read_text(encoding="utf-8"))
    return {"status": "ACCEPTED" if rep.get("passed") else "REFUSED",
            "issues": [{k: i.get(k) for k in ("severity", "message",
                                              "check")}
                       for i in rep.get("issues", [])][:50],
            "validator_version": rep.get("validation_settings", {}).get(
                "rocrate_validator_version")}


def _broken(meta: dict) -> dict:
    out = {}
    m = copy.deepcopy(meta)
    m["@graph"] = [e for e in m["@graph"] if e.get("@id") != "./"]
    out["no_root_dataset"] = m
    m = copy.deepcopy(meta)
    for e in m["@graph"]:
        if e.get("@id") == "ro-crate-metadata.json":
            e.pop("conformsTo", None)
    out["no_conformsTo"] = m
    m = copy.deepcopy(meta)
    for e in m["@graph"]:
        if e.get("@id") == "./":
            e.pop("datePublished", None)
    out["no_datePublished"] = m
    m = copy.deepcopy(meta)
    m["@graph"].append({"@id": "does/not/exist.csv", "@type": "File",
                        "name": "a data entity with no file"})
    for e in m["@graph"]:
        if e.get("@id") == "./":
            e["hasPart"] = list(e.get("hasPart", [])) + [
                {"@id": "does/not/exist.csv"}]
    out["dangling_data_entity"] = m
    return out


def run(validator: str | None) -> dict:
    meta = json.loads((ROOT / RC.META).read_text(encoding="utf-8"))
    rep: dict = {"schema": SCHEMA, "profile": PROFILE, "cases": {}}
    cases = {"committed_crate": meta, **_broken(meta)}
    with tempfile.TemporaryDirectory(prefix="rocrate-") as tmp:
        for name, m in cases.items():
            crate = Path(tmp) / name
            missing = package(m, crate)
            probs = internal(m) + [f"no file for data entity {x}"
                                   for x in missing]
            ext = (external(validator, crate) if validator else
                   {"status": "EXTERNAL_UNAVAILABLE",
                    "error": "no validator runtime given"})
            ok_int = not probs
            if ext["status"] == "EXTERNAL_UNAVAILABLE":
                agreement = "NOT_MEASURED"
            else:
                agreement = ("AGREE" if ok_int == (ext["status"] ==
                                                   "ACCEPTED")
                             else "DISAGREE")
            rep["cases"][name] = {
                "internal": "ACCEPTED" if ok_int else "REFUSED",
                "internal_problems": probs[:50], "external": ext,
                "agreement": agreement}
    c = rep["cases"]
    expected = {"committed_crate": "ACCEPTED"}
    rep["accepted"] = all(
        v["agreement"] == "AGREE"
        and v["internal"] == expected.get(k, "REFUSED")
        for k, v in c.items())
    rep["measured"] = all(v["agreement"] != "NOT_MEASURED"
                          for v in c.values())
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--validator", help="path to rocrate-validator")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    rep = run(args.validator)
    Path(args.out).write_text(json.dumps(rep, indent=1, sort_keys=True)
                              + "\n", encoding="utf-8")
    for name, v in rep["cases"].items():
        print(f"{name}: internal {v['internal']}, external "
              f"{v['external']['status']} -> {v['agreement']}")
    if not rep["measured"]:
        print("EXTERNAL VALIDATION NOT MEASURED")
        return 1
    print("RO-Crate conformance: " + ("ACCEPTED" if rep["accepted"]
                                      else "NOT ACCEPTED"))
    return 0 if rep["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
