# REVIEWER ATTACK MAP

This file is a navigation aid for technical reviewers. It maps common
objections to the specific package files that address them. None of these
answers is a claim of validation. Every answer points to a conditional
gate, a documented risk, or an unresolved measurement requirement.

The package status — BLOCKED now, CONDITIONAL after measurement, 0 PASS,
no validated system, no breakthrough claim — is not in dispute and is
not the subject of these sections.

---

## A. "LCVD cannot happen during 10 mK sensing"

**Answer.** Correct, and QTA does not claim it can.

QTA does not perform quantum sensing during Mode B material processing.
The architecture is mode-separated and interlocked:

- **Mode A** — cryogenic baseline / stabilization
- **Mode B** — material processing / LCVD growth (no sensing)
- **Mode C** — isolation, purge, cryopump/baffle, thermal recovery, readiness verification
- **Mode D** — quantum sensing

Mode D is entered only after Mode C isolation, purge,
cryopumping/baffling, thermal recovery, contamination checks,
radiation/RF/optical/vibration checks, and NV-baseline requalification.
The transition `C_to_D` is a BLOCKED gate (`mode_transition_acceptance_tests.csv`)
with explicit thermal, vacuum, contamination, radiation, RF, optical,
vibration, and NV-baseline sub-conditions, none of which has been
measured.

**See:** `mode_transition_acceptance_tests.csv`, `interlock_table.csv`,
`results_gate_table.csv` (search for `C_to_D_Readiness`).

---

## B. "Radiation kills the 10 mK stage"

**Answer.** Possible, and the package treats it as a BLOCKED gate, not a
solved problem.

The shielding stack is layered and explicitly conditional:

- SHIELD-001 4 K OFHC copper radiation shutter (existing canonical element)
- SHIELD-002 radiation shield + labyrinth baffles
- SHIELD-003 three-shutter stack
- SHIELD-004 RF-tight 10 mK sample can (proposed)
- SHIELD-006 nested cold optical apertures (proposed)
- SHIELD-007 thermally anchored cold beam dumps (proposed)

The gate `Shield-RAD` requires *measured* radiation load into the 10 mK
region below the Mode D heat budget. None of those measurements exists.
`can_PASS_now = NO` for every shielding entry.

**See:** `shielding_stack_register.csv`, `radiation_rf_leakage_budget.csv`,
`optical_line_of_sight_audit.csv`.

---

## C. "Microwave / RF leakage kills Mode D"

**Answer.** Plausible, and not denied.

The package adds an RF-tight 10 mK sample can (SHIELD-004), staged
IR/high-frequency filters with thermal anchoring at every stage
(SHIELD-005), and an explicit microwave blackbody leakage gate
(SHIELD-012). Filters must terminate inside the RF-tight enclosure. The
gate `Shield-RF` requires measured microwave/RF leakage below the Mode D
heat *and* dephasing budget. Not measured. BLOCKED.

**See:** `shielding_stack_register.csv` rows SHIELD-004, SHIELD-005,
SHIELD-012; `radiation_rf_leakage_budget.csv` rows LK-003, LK-004,
LK-007, LK-008.

---

## D. "Contamination kills Mode D after Mode B exposure"

**Answer.** This is the cryopanel saturation / chemical memory problem
and it is explicitly tracked.

The package adds the dirty/clean dual shutter stack (SHIELD-008),
sacrificial cryopanels and regenerable cold traps (SHIELD-009), and a
per-species cryopanel memory model. The gate `Shield-CHEM` requires
RGA-species-resolved residuals, QCM mass uptake, and witness-coupon
analysis to drop below Mode D thresholds. None of those measurements
exists. The `C_to_D` transition is BLOCKED until they do.

**See:** `cryopanel_memory_model.csv`, `mode_transition_acceptance_tests.csv`
row `T-C2D`, `shielding_stack_register.csv` rows SHIELD-008, SHIELD-009.

---

## E. "Monte Carlo is fake validation"

**Answer.** Agreed. Monte Carlo is a sensitivity and falsification-priority
tool, not validation.

- The Monte Carlo does **not** unlock physical gates.
- The Monte Carlo does **not** represent experimental validation.
- Physical gates remain CONDITIONAL or BLOCKED until in-system
  hardware measurements exist for every input the gate consumes.
- The PASS count is 0 and remains 0 under any Monte Carlo result.
- `best_forecast_operating_point.json` carries
  `forecast_only=true` and `physically_demonstrated=false`.
- The full-cycle Monte Carlo pass rate is 0.0%.

The Monte Carlo's only role is to rank assumptions by their contribution
to forecast failure probability, so reviewers and experimentalists know
which assumption to attack first.

**See:** `CLAIMS_BOUNDARY.md` section "Monte Carlo: forecast /
sensitivity only", `monte_carlo_summary.csv`,
`best_forecast_operating_point.json`,
`monte_carlo_sensitivity_rankings.csv` (when populated).

---

## F. "AI wrote unsupported claims"

**Answer.** Every numerical input is traced in the package, and uncovered
inputs are visible in the source-gap register rather than hidden.

- `CLAIMS_BOUNDARY.md` lists every non-claim explicitly.
- `source_map.csv` and `validation_matrix.csv` use a fixed eight-value
  directness taxonomy (DIRECT, INDIRECT, ASSUMED, MANUFACTURER_SPEC,
  DESIGN_SPECIFIED, UNKNOWN, REQUIRES_EXPERIMENT, DERIVED). Forbidden
  drift labels are rejected by the consistency checker.
- `source_gap_register.csv` records every unresolved source gap (100
  rows after the source-hardening pass).
- `bibliography_audit.csv` records every original bibliography entry
  and its disposition (KEEP / REMOVE_UNUSED).
- `source_audit_status.txt` declares `FULL SOURCE AUDIT: INCOMPLETE` —
  honest.
- 0 PASS gates. All 63 gates carry `can_PASS_now=NO` and
  `measured_in_this_system=false`.

**See:** `CLAIMS_BOUNDARY.md`, `source_gap_register.csv`,
`validation_matrix.csv`, `source_map.csv`, `bibliography_audit.csv`,
`source_audit_status.txt`, `results_gate_table.csv`.

---

## G. "The first iteration was too simultaneous"

**Answer.** Correct. The first iteration was cross-coupled simultaneous.
The rewritten QTA architecture is mode-separated and interlocked.

The first QTA iteration was formulated as a cross-coupled simultaneous
architecture, where material processing, cryogenic transport, exposure,
surface interaction, and sensing were treated as concurrently
interacting subsystems. The rewritten QTA architecture replaces that
cross-coupled simultaneity with mode-separated, interlocked operation.
Material processing and quantum sensing are no longer claimed to operate
concurrently. QTA now asks whether the same cryogenic platform can
transition from Mode B growth/process operation through Mode C
isolation and recovery into a clean, thermally stable Mode D sensing
condition.

That transition is the entire scientific question this package opens.
It is BLOCKED.

**See:** Section A above; `mode_transition_acceptance_tests.csv`;
README `Shielding and Isolation Stack` and `Mode definitions` sections.
