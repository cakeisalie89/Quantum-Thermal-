# Hardware-Validation Governance (Stage 5)

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No hardware
data exists; `measured_in_this_system` is false everywhere.

This layer defines HOW future genuine hardware measurements would move,
under human control, from raw acquisition toward gate review -- while the
tooling remains read-only and can never change a gate.

**Data classes.** `SYNTHETIC` (Stage-4 comparison layer, unchanged).
`HARDWARE_UNVERIFIED`: schema-valid hardware claims enter QUARANTINE even
when calibration, custody, controls, repetitions or uncertainty are
incomplete; every deficiency is recorded; such records have no evidentiary
standing and never enter dossiers. `HARDWARE_REVIEWED`: records whose
deficiencies (other than plan-level unresolved requirements) are cleared
AND that carry a valid human review record with decision
ACCEPT_AS_EVIDENCE may enter a gate-evidence dossier. Promotion is a human
act; tools verify review records but can never author or apply one.

**Identifiers & preservation.** Every hardware record carries
experiment_id (must exist in `validation_matrix.csv`, the plan authority),
sample_id, operator_id, instrument_id, calibration_id, run_id; a raw-data
block (filename, byte size, SHA-256, format, acquisition timestamp --
raw files stay OUTSIDE the source; references only, re-hashed when
accessible); an ordered chain of custody covering acquisition -> archive
-> ingestion (validated for ordering, stages and timestamps; a contiguous
documented chain does not prove the absence of undocumented gaps); a
calibration record (CALIBRATED, reference, date, validity window); linked
control/background measurements; an explicit uncertainty with method.

**Repetitions.** Requirements come only from the plan authority. The
matrix currently specifies none, so every repetition requirement is
"UNKNOWN (not plan-specified)" and dossier readiness is INCOMPLETE until
the plan defines counts. No default is invented.

**Gate evidence.** Dossiers assemble reviewed evidence per gate with a
completeness checklist and `review_readiness`, a vocabulary disjoint from
gate statuses; `automatic_gate_effect` is the constant `NONE`. The gate
table is written solely by the canonical generator, which imports no
measurement or governance code. Any record smuggling gate-status fields
or controlled status values in status-bearing fields is rejected
(free-text words are not policed).

**Audit.** Ingestion events append to a hash-chained JSONL log (each line
embeds the previous line's SHA-256); `verify_audit_chain` detects
tampering. The log is operator-side and non-canonical.

**Default execution** contains no hardware data and is deterministic;
hardware paths run only when an operator supplies files. Test fixtures
are labeled `TEST_FIXTURE_NOT_DATA` and never resemble evidence.
