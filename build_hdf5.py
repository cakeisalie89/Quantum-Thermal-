#!/usr/bin/env python3
"""Deterministic, fail-closed builder of ``qta_scientific_results.h5``.

MODEL-ONLY / FORECAST-ONLY. Pure REPRESENTATION of the existing verified
governed outputs: nothing is recomputed, no solver is imported, no
scientific value can change. Fail-closed on: missing sources, source-hash
mismatch vs hdf5_output_mapping.json, duplicate dataset paths, schema
mismatch, unexpected columns, non-uniform dtype surprises, gate-state or
claim-field violations in the gate table, incomplete output set.

Determinism controls: sorted stable creation order; ``track_times=False``
on every dataset; fixed gzip level 4; UTF-8; no wall-clock timestamps;
no absolute paths; provenance attributes are hashes and versions only.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import h5py
import numpy as np

H5_NAME = "qta_scientific_results.h5"
SV = "1.0.0"
LBL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
EXEMPT = "deep_surrogate_readiness.json"


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fail(msg: str) -> None:
    print(f"[HDF5 builder FAIL-CLOSED] {msg}")
    sys.exit(1)


def build(dest: Path = Path(H5_NAME)) -> None:
    mapping = json.loads(Path("hdf5_output_mapping.json").read_text())
    schema = json.loads(Path("hdf5_schema.json").read_text())
    outs = mapping["outputs"]
    if len(outs) != 88:
        fail(f"mapping covers {len(outs)} outputs, expected 88")
    out_dir = Path("outputs")
    n_present = len([p for p in out_dir.iterdir() if p.is_file()])
    if n_present != 89:
        fail(f"outputs/ has {n_present} files; complete verified set of "
             "89 required (truncated sets are never used)")
    # gate-state / claim-boundary guard on the source itself
    rows = list(csv.DictReader(open("results_gate_table.csv")))
    if sum(1 for r in rows if r["status"] == "PASS") != 0:
        fail("gate table contains scientific PASS -- refusing to build")
    seen: set = set()
    sgrp = {d["dataset_group"]: d for d in schema["datasets"]}
    if dest.exists():
        dest.unlink()
    with h5py.File(dest, "w", libver="earliest",
                   track_order=False) as h:
        for rec in sorted(outs, key=lambda r: r["path"]):
            src = Path(rec["path"])
            if not src.exists():
                fail(f"missing required source {src}")
            if sha_file(src) != rec["sha256"]:
                fail(f"source-hash mismatch for {src} (mapping stale)")
            grp = rec["hdf5_group"]
            if grp in seen:
                fail(f"duplicate dataset path {grp}")
            seen.add(grp)
            if grp not in sgrp:
                fail(f"{grp} absent from hdf5_schema.json")
            g = h.create_group(grp)
            g.attrs["source_output"] = rec["path"]
            g.attrs["source_sha256"] = rec["sha256"]
            g.attrs["schema_version"] = SV
            if rec["format"] == "csv":
                rows2 = list(csv.reader(open(src, encoding="utf-8")))
                header, body = rows2[0], rows2[1:]
                cols = rec["columns"]
                if [c["name"] for c in cols] != header:
                    fail(f"unexpected columns in {src}")
                if len(body) != rec["n_rows"]:
                    fail(f"row-count changed in {src}")
                for c in cols:
                    j = c["index"]
                    vals = [r[j] if j < len(r) else "" for r in body]
                    if c["dtype"] == "float64":
                        try:
                            arr = np.array([float(v) for v in vals],
                                           dtype=np.float64)
                        except ValueError:
                            fail(f"dtype surprise in {src}:{c['name']}")
                    else:
                        arr = np.array(vals,
                                       dtype=h5py.string_dtype("utf-8"))
                    d = g.create_dataset(
                        c["name"], data=arr, compression="gzip",
                        compression_opts=4, track_times=False)
                    d.attrs["unit"] = c["unit"]
                    d.attrs["column_index"] = np.int64(j)
                    d.attrs["missing_value_policy"] = \
                        c["missing_value_policy"]
            else:
                raw = src.read_bytes()
                d = g.create_dataset(
                    "raw_utf8", data=np.void(raw), track_times=False)
                d.attrs["byte_length"] = np.int64(len(raw))
                d.attrs["encoding"] = "UTF-8 (opaque byte-exact)"
        prov = h.create_group("/provenance")
        prov.attrs["label"] = LBL
        prov.attrs["schema_version"] = SV
        prov.attrs["hdf5_role"] = ("structured representation of "
                                   "existing simulation outputs; NOT new "
                                   "scientific evidence")
        prov.attrs["scientific_gate_PASS_count"] = np.int64(0)
        prov.attrs["can_PASS_now"] = "NO"
        prov.attrs["measured_in_this_system"] = "false"
        prov.attrs["mapping_sha256"] = \
            sha_file(Path("hdf5_output_mapping.json"))
        prov.attrs["schema_sha256"] = sha_file(Path("hdf5_schema.json"))
        prov.attrs["uv_lock_sha256"] = sha_file(Path("uv.lock"))
        prov.attrs["snakefile_sha256"] = sha_file(Path("Snakefile"))
        prov.attrs["manifest_sha256_at_build"] = \
            sha_file(Path("final_manifest.json"))
        prov.attrs["manifest_note"] = (
            "manifest hash as of HDF5 build time; final packaging "
            "regenerates the manifest to cover this file (circular "
            "dependency documented in HDF5_DATA_MODEL.md)")
        prov.attrs["exempt_output"] = EXEMPT
        try:
            import subprocess
            head = subprocess.run(["git", "rev-parse", "HEAD"],
                                  capture_output=True,
                                  text=True).stdout.strip()
        except Exception:
            head = "unavailable"
        prov.attrs["source_tree_git_head"] = head or "unavailable"
    print(f"[HDF5] built {dest} ({dest.stat().st_size} bytes; "
          f"{len(seen)} mapped groups + /provenance; sha256 "
          f"{sha_file(dest)[:16]}...)")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(H5_NAME))
