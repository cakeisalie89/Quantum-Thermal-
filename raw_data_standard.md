# Raw-Data Standard (Stage 6)

MODEL-ONLY / FORECAST-ONLY. Planning infrastructure; no data exists yet.

**Retention.** Vendor-native raw acquisition files are immutable and are
retained unmodified outside the canonical source tree; processed files
never overwrite them; every transformation appends to a recorded
transformation history with input/output hashes. CSV and/or HDF5 exports
accompany vendor formats where appropriate.

**Hashes.** SHA-256 for every raw file and every transformed file,
recorded at creation and re-verified at ingestion (Stage-5 rules:
inaccessible raw ⇒ reference-only deficiency; mismatch ⇒ rejection).

**Mandatory metadata (Stage-5-compatible sidecar JSON per file):**
experiment_id (registry key) · campaign_id · run_id · sample/witness id ·
machine mode · exact device state · instrument model · instrument
identifier · firmware and acquisition-software versions · calibration
references (ids into calibration records) · operator or review-safe
operator identifier · timestamp with timezone (ISO-8601) · units and
column definitions · missing-value representation (explicit token, never
blank ambiguity) · environmental conditions at acquisition ·
interlock state snapshot · deviations from the playbook · stop events ·
transformation history · raw and transformed hashes.

**Sidecars** are the Stage-5 hardware-measurement records: the same
schema, the same fail-closed validation, the same quarantine/dossier
path, the same custody rules (documented chains do not prove the absence
of undocumented gaps), the same `automatic_gate_effect = NONE`.

**Prohibitions.** No processed-over-raw overwrites; no metadata-free
files; no status vocabulary inside data records (record-level status
fields are rejected by governance); no representation of any file as
evidence before human review.
