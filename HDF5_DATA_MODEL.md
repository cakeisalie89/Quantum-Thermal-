# HDF5 Data Model (Stage 8)

MODEL-ONLY / FORECAST-ONLY. `qta_scientific_results.h5` is a structured
REPRESENTATION of the existing 88 governed simulation outputs — not new
scientific evidence. Native CSV/JSON/Mermaid files remain the
authoritative compatibility exports and stay byte-identical.

**Layout.** `/tables/<stem>/<column>` — one dataset per CSV column
(order preserved via `column_index`; row order exact). Per-column dtype:
float64 only when every cell parses exactly with `float()`; otherwise
UTF-8 strings verbatim (empties preserved as empty strings — missing
values are never coerced to zero; UNKNOWN stays the literal token in its
string column). `/native_json/<stem>/raw_utf8` and
`/native_text/<stem>/raw_utf8` — opaque byte-exact copies of the 27
governance/summary JSON records and the FSM Mermaid diagram (nested
heterogeneous records are not arrayized: that would invent schema).
`/provenance` — hashes (mapping, schema, uv.lock, Snakefile, manifest),
source-tree git head, schema version, and the claim fields:
`scientific_gate_PASS_count=0`, `can_PASS_now=NO`,
`measured_in_this_system=false`.

**Units.** Extracted only from canonical column-suffix conventions
(`_K, _Pa, _s, _W, _m, _ML, ...`); anything else is the literal string
`unresolved` — never invented. 34 files carry at least one
unresolved-unit numeric column (recorded in the equivalence report).

**Determinism.** Sorted creation order, `track_times=False` everywhere,
fixed gzip level 4 (zlib; library-version sensitivity documented: byte
identity is claimed only within the locked environment — h5py 3.16.0 /
libhdf5 2.0.0 / numpy 2.4.4), UTF-8, no timestamps, no absolute paths.
Demonstrated: three independent builds byte-identical
(sha256 970ad6af…, 1,974,813 B).

**Fail-closed.** The builder refuses: incomplete output sets (≠89),
missing sources, mapping-hash drift, duplicate paths, schema mismatch,
unexpected columns, dtype surprises, and any scientific PASS in the gate
table. Equivalence: exact parsed-value equality (bitwise on parsed
float64 — the source precision; no tolerance anywhere), byte-exact
natives, unit/order/count/metadata checks
(`validate_hdf5_equivalence.py` → `stage8_reports/hdf5_equivalence_report.json`).

**Uncertainty representation.** Only columns the sources provide
(`*_p05/p50/p95`, `*_sigma`); nothing synthesized. **Future
compatibility note (deferred):** mesh-field exports for VTK/XDMF
visualization would extend `/tables` with coordinate datasets; not begun.
