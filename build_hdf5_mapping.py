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

#: The reviewed dimension of every governed numeric column. D-2026-57.
#:
#: This used to be a suffix table -- thirty patterns matched against the END OF
#: THE COLUMN NAME -- and the last few characters of a name are not a unit.
#: 91 of 162 numeric columns fell through it and were published as
#: "unresolved", among them every heat-source density (Q_laser_W_m3), every
#: gas density (n_CH4_modeC_1m3), and every entropy in nats, all of which
#: state their unit in their own name. Three did not fall through and came out
#: WRONG: gradient_K_per_m ends in "_m" and was published as METRES;
#: dose_flux_open_m2_s and dose_flux_closed_m2_s end in "_s" and were
#: published as SECONDS. A consumer reading those attributes would have
#: believed a temperature gradient was a length and a flux was a time.
#:
#: The declaration is reviewed and committed; tools/unit_inventory.py
#: reconciles it against the columns that actually exist, in both directions.
UNIT_INVENTORY = Path("docs/unit_inventory.json")


class UndeclaredUnit(KeyError):
    """A governed numeric column with no reviewed dimension.

    A refusal rather than a fallback. Any default here -- "unresolved",
    "dimensionless", the empty string -- is a dimension nobody reviewed,
    written into an archival artefact that a consumer will read as fact.
    """


def _inventory() -> dict:
    return json.loads(UNIT_INVENTORY.read_text())["columns"]


def unit_of(src: str, col: str, inv: dict) -> str:
    entry = inv.get(f"{src}:{col}")
    if entry is None:
        raise UndeclaredUnit(
            f"{src}:{col} has no entry in {UNIT_INVENTORY}; a new numeric "
            "column carries no dimension until somebody says what it is")
    u = entry["unit"]
    if u == "PER_ROW":
        return f"PER_ROW:{entry['unit_from']}"
    return u


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def classify() -> tuple:
    inv = _inventory()
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
                    "unit": (unit_of(p.name, c, inv) if numeric
                             else "n/a (string)"),
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
            "unit_system": "SI, plus four words for a column that carries no "
                           "unit: DIMENSIONLESS (a physical ratio), COUNT (a "
                           "count or index), ORDINAL (a rank-scale score with "
                           "no physical dimension) and PER_ROW:<column> (a "
                           "long-format table whose unit belongs to the row). "
                           "Every dimension is declared and reviewed in "
                           "docs/unit_inventory.json; none is inferred from a "
                           "column name, and there is no 'unresolved'",
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
