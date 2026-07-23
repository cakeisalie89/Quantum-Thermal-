#!/usr/bin/env python3
"""Stage-8 classification of the 88 governed outputs and generation of
the HDF5 mapping + schema registries. Deterministic. MODEL-ONLY /
FORECAST-ONLY: representation planning only — no scientific content is
computed or altered here.

Classification policy (recorded per file):
- CSV governed outputs -> HDF5 table groups (one dataset per column;
  column order preserved; per-column dtype resolved by uniform exact
  ``float()`` parseability across ALL rows — any empty cell or non-
  numeric cell keeps the whole column as UTF-8 strings so nothing is
  coerced and missing values are preserved verbatim).
- JSON governed outputs -> native_reference: they are nested,
  heterogeneous governance/summary records; arrayizing them would invent
  schema. They remain authoritative compatibility exports, are hashed
  here, and are linked (not embedded) by RO-Crate. Their raw bytes are
  additionally carried in HDF5 under /native_json/<name> as opaque UTF-8
  string datasets so the HDF5 artifact is self-contained WITHOUT
  becoming a second authority (byte-exactness enforced by the
  equivalence validator).
- deep_surrogate_readiness.json -> excluded (by-design nondeterministic;
  project-wide byte-gate exemption).
Units: extracted only from canonical column-name suffixes; anything else
is explicitly "unresolved" — never invented.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

SV = "1.0.0"
LBL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
EXEMPT = "deep_surrogate_readiness.json"

UNIT_SUFFIX = {
    "_K": "K", "_Pa": "Pa", "_s": "s", "_us": "us", "_ns": "ns",
    "_W": "W", "_mW": "mW", "_uW": "uW", "_m": "m", "_mm": "mm",
    "_um": "um", "_nm": "nm", "_Hz": "Hz", "_kHz": "kHz", "_MHz": "MHz",
    "_GHz": "GHz", "_J": "J", "_T": "T", "_mT": "mT", "_A": "A",
    "_V": "V", "_kg": "kg", "_g": "g", "_ML": "monolayer",
    "_per_s": "1/s", "_frac": "dimensionless", "_ratio": "dimensionless",
    "_fraction": "dimensionless", "_dB": "dB", "_deg": "deg",
    "_percent": "percent", "_WK": "W/K", "_W_per_K": "W/K",
}


def unit_of(col: str) -> str:
    for suf, u in sorted(UNIT_SUFFIX.items(), key=lambda x: -len(x[0])):
        if col.endswith(suf):
            return u
    return "unresolved"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def classify() -> tuple:
    root = Path(".")
    files = sorted(p for p in root.iterdir() if p.is_file()
                   and p.suffix in (".csv", ".json", ".mmd"))
    # governed = the regeneration set = names also produced in outputs/
    out = {p.name for p in Path("outputs").iterdir() if p.is_file()}
    mapping, schema = [], []
    for p in files:
        if p.name not in out or p.name == EXEMPT:
            continue
        rec = {"path": p.name, "format": p.suffix[1:],
               "role": "governed scientific/governance output "
                       "(forecast-only)",
               "canonical_status": "authoritative compatibility export; "
                                    "byte-preservation REQUIRED",
               "sha256": sha(p), "byte_preservation": True,
               "referenced_by_ro_crate": True}
        if p.suffix == ".csv":
            rows = list(csv.reader(open(p, encoding="utf-8")))
            header, body = rows[0], rows[1:]
            cols = []
            for j, c in enumerate(header):
                vals = [r[j] if j < len(r) else "" for r in body]
                numeric = bool(vals) and all(_is_float(v) for v in vals)
                cols.append({
                    "name": c, "index": j,
                    "dtype": "float64" if numeric else "utf8_string",
                    "unit": unit_of(c) if numeric else "n/a (string)",
                    "missing_value_policy":
                        "no empty cells (uniform numeric)" if numeric
                        else "verbatim strings (empties preserved as "
                             "empty strings; never coerced to zero)",
                })
            grp = f"/tables/{p.stem}"
            rec.update({"in_hdf5": True, "hdf5_group": grp,
                        "n_rows": len(body), "n_columns": len(header),
                        "columns": cols})
            schema.append({
                "dataset_group": grp, "source_output": p.name,
                "source_sha256": rec["sha256"], "kind": "table",
                "n_rows": len(body),
                "columns": cols,
                "shape_per_column": [len(body)],
                "ordering_policy": "row order preserved exactly; column "
                                   "order preserved via 'index'",
                "string_encoding": "UTF-8", "compression": "gzip level 4",
                "chunking": "single chunk per column (small tables)",
                "uncertainty_representation":
                    "columns named *_sigma/*_p05/*_p50/*_p95 where the "
                    "source provides them; nothing added",
                "valid_range": "not asserted (no authoritative ranges in "
                               "sources; unresolved)",
                "schema_version": SV,
                "classification": "scientific table"})
        elif p.suffix == ".mmd":
            rec.update({"in_hdf5": True,
                        "hdf5_group": f"/native_text/{p.stem}",
                        "hdf5_representation":
                            "opaque UTF-8 raw-bytes dataset (byte-exact; "
                            "native Mermaid diagram remains the "
                            "authority)",
                        "rationale": "FSM diagram text; not tabular"})
            schema.append({
                "dataset_group": f"/native_text/{p.stem}",
                "source_output": p.name, "source_sha256": rec["sha256"],
                "kind": "raw_text_utf8", "string_encoding": "UTF-8",
                "compression": "gzip level 4",
                "ordering_policy": "byte-exact source bytes",
                "schema_version": SV,
                "classification": "diagram (native authority)"})
        else:
            rec.update({"in_hdf5": True,
                        "hdf5_group": f"/native_json/{p.stem}",
                        "hdf5_representation":
                            "opaque UTF-8 raw-bytes dataset (byte-exact; "
                            "native JSON remains the authority)",
                        "rationale":
                            "nested heterogeneous governance/summary "
                            "record; arrayization would invent schema"})
            schema.append({
                "dataset_group": f"/native_json/{p.stem}",
                "source_output": p.name, "source_sha256": rec["sha256"],
                "kind": "raw_json_utf8", "string_encoding": "UTF-8",
                "compression": "gzip level 4",
                "ordering_policy": "byte-exact source bytes",
                "schema_version": SV,
                "classification": "governance/summary record "
                                   "(native authority)"})
        mapping.append(rec)
    return mapping, schema


def _is_float(v: str) -> bool:
    if v == "" or v is None:
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


def main() -> None:
    mapping, schema = classify()
    n_csv = sum(1 for m in mapping if m["format"] == "csv")
    n_json = sum(1 for m in mapping if m["format"] == "json")
    doc = {"schema_version": SV, "label": LBL,
           "note": "representation mapping only; no scientific content "
                   "changed; existing CSV/JSON remain authoritative",
           "exempt": [EXEMPT],
           "n_governed": len(mapping), "n_csv_tables": n_csv,
           "n_json_native": n_json, "outputs": mapping}
    Path("hdf5_output_mapping.json").write_text(
        json.dumps(doc, indent=1, sort_keys=True) + "\n")
    sdoc = {"schema_version": SV, "label": LBL,
            "hdf5_file": "qta_scientific_results.h5",
            "unit_system": "SI with canonical project suffix conventions; "
                           "'unresolved' is preserved, never invented",
            "root_groups": ["/tables", "/native_json", "/native_text",
                            "/provenance"],
            "determinism": ["track_times disabled on every object",
                            "sorted stable creation order",
                            "fixed gzip level 4", "UTF-8 strings",
                            "no wall-clock timestamps",
                            "no absolute paths"],
            "datasets": schema}
    Path("hdf5_schema.json").write_text(
        json.dumps(sdoc, indent=1, sort_keys=True) + "\n")
    print(f"mapping: {len(mapping)} governed ({n_csv} csv + "
          f"{n_json} json native) | schema: {len(schema)}")


if __name__ == "__main__":
    main()
