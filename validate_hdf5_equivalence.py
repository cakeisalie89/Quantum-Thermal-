#!/usr/bin/env python3
"""HDF5 <-> source equivalence validator (Stage 8). Fail-closed.

Exactness rules: numeric columns compare by exact ``float(cell)`` parse
equality (bitwise on the parsed float64 — the source representation's
precision; no tolerance); string columns and native JSON/text compare
byte/verbatim-exact; row/column counts, ordering, units, source hashes,
metadata completeness and schema version all verified. Writes
stage8_reports/hdf5_equivalence_report.json.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import h5py
import numpy as np

REP = Path("stage8_reports/hdf5_equivalence_report.json")


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(h5_path: str = "qta_scientific_results.h5",
         report_path: str = str(REP)) -> int:
    rep = Path(report_path)
    mapping = json.loads(Path("hdf5_output_mapping.json").read_text())
    problems: list = []
    stats = {"sources_checked": 0, "datasets_checked": 0,
             "exact_numeric_matches": 0,
             "exact_string_matches": 0,
             "byte_exact_natives": 0,
             "unresolved_unit_columns": 0}
    with h5py.File(h5_path, "r") as h:
        groups = set()
        def _collect(name, obj):
            if isinstance(obj, h5py.Group) and name.count("/") >= 1 \
                    and not name.startswith("provenance"):
                groups.add("/" + name)
        h.visititems(_collect)
        mapped = {o["hdf5_group"] for o in mapping["outputs"]}
        extra = {g for g in groups if g in
                 {m for m in groups} and g not in mapped
                 and g.count("/") == 2}
        extra -= {g for g in extra if not (g.startswith("/tables/")
                  or g.startswith("/native_"))}
        if extra:
            problems.append("extra unmapped dataset groups: "
                            f"{sorted(extra)[:5]}")
        for rec in mapping["outputs"]:
            src = Path(rec["path"])
            stats["sources_checked"] += 1
            if sha_file(src) != rec["sha256"]:
                problems.append(f"{src}: source hash changed vs mapping")
                continue
            g = h[rec["hdf5_group"]]
            if g.attrs["source_sha256"] != rec["sha256"]:
                problems.append(f"{rec['hdf5_group']}: stored source "
                                "hash mismatch")
            if rec["format"] == "csv":
                rows = list(csv.reader(open(src, encoding="utf-8")))
                header, body = rows[0], rows[1:]
                if sorted(g.keys()) != sorted(header):
                    problems.append(f"{src}: column set mismatch")
                    continue
                for c in rec["columns"]:
                    stats["datasets_checked"] += 1
                    d = g[c["name"]]
                    if int(d.attrs["column_index"]) != c["index"] or \
                            d.attrs["unit"] != c["unit"]:
                        problems.append(f"{src}:{c['name']}: unit/order "
                                        "attribute mismatch")
                    vals = [r[c["index"]] if c["index"] < len(r) else ""
                            for r in body]
                    if d.shape[0] != len(vals):
                        problems.append(f"{src}:{c['name']}: row count")
                        continue
                    if c["dtype"] == "float64":
                        ref = np.array([float(v) for v in vals],
                                       dtype=np.float64)
                        if not np.array_equal(d[...], ref):
                            problems.append(f"{src}:{c['name']}: numeric "
                                            "value mismatch")
                        else:
                            stats["exact_numeric_matches"] += 1
                        if c["unit"] == "unresolved":
                            stats["unresolved_unit_columns"] += 1
                    else:
                        got = [x.decode("utf-8") if isinstance(x, bytes)
                               else str(x) for x in d[...]]
                        if got != vals:
                            problems.append(f"{src}:{c['name']}: string "
                                            "value mismatch")
                        else:
                            stats["exact_string_matches"] += 1
            else:
                stats["datasets_checked"] += 1
                raw = bytes(g["raw_utf8"][()])
                if raw != src.read_bytes():
                    problems.append(f"{src}: native bytes differ in HDF5")
                else:
                    stats["byte_exact_natives"] += 1
        p = h["/provenance"]
        for a in ("mapping_sha256", "schema_sha256", "uv_lock_sha256",
                  "manifest_sha256_at_build", "scientific_gate_PASS_count",
                  "can_PASS_now", "measured_in_this_system"):
            if a not in p.attrs:
                problems.append(f"/provenance missing attr {a}")
        if int(p.attrs.get("scientific_gate_PASS_count", -1)) != 0:
            problems.append("provenance PASS-count not zero")
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps(
        {"schema_version": "1.0.0", "h5": h5_path,
         "h5_sha256": sha_file(Path(h5_path)), **stats,
         "mismatches": problems,
         "result": "EQUIVALENT" if not problems else
                   f"{len(problems)} PROBLEMS",
         "note": "exact parsed-value equality (no tolerance); natives "
                 "byte-exact; software verification only -- scientific "
                 "gate PASS remains zero"},
        indent=1, sort_keys=True) + "\n")
    print(f"equivalence: {stats} | problems {len(problems)}")
    print(f"RESULT: {'EQUIVALENT' if not problems else 'FAIL'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
