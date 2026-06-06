# QTA Package — Blocked Conditional Validation Framework (v4)

## GLOBAL STATUS

| Field | Value |
|-------|-------|
| CURRENT STATE | BLOCKED |
| POST-INSTALL FORECAST | CONDITIONAL |
| VALIDATED SYSTEM | NOT AVAILABLE |
| BREAKTHROUGH CLAIM | NOT MADE |
| LCVD during active 10 mK sensing | NOT VIABLE / NOT CLAIMED |
| Same-chamber mode-switched growth and sensing | PROPOSED, not demonstrated |
| Current full-cycle MC | 0.0% |
| PASS count | 0 |
| BLOCKED count | 23 |
| CONDITIONAL count | 47 |
| UNKNOWN count | 2 |
| DERIVED_CHECK count | 11 |
| Total gates | 83 |
| Package status | COMPLETE_DRAFT |
| Source audit | REPRESENTATIVE_ONLY / INCOMPLETE_FOR_SUBMISSION |
| Bibliography | 23 entries (INCOMPLETE_DRAFT; target 100–200+) |

This package is an unvalidated, blocked conditional framework. It does not claim
working hardware. It does not claim validated LCVD. It does not claim helium spin-noise
detection. It does not claim any breakthrough. No parameter is verified in this system.

---


## Mode definitions

QTA is a zero-pass, pre-experimental, mode-separated feasibility framework.
It defines the conditions required for validation; it does not claim that
the hardware has been validated.

The architecture is mode-separated and interlocked:

- **Mode A — Cryogenic Baseline / Stabilization.** Bring the chamber and
  sample into the required cryogenic state, verify thermal stability,
  vacuum state, NV baseline, and confirm the system is ready before any
  exposure or processing.
- **Mode B — Material Processing / LCVD Growth Mode.** Higher-energy
  material-prep or LCVD step in the same chamber, but not simultaneously
  with sensing. Precursor exposure, femtosecond laser processing,
  atomizer/pulsed molecular-beam delivery, and growth-side operations
  happen here.
- **Mode C — Isolation / Purge / Thermal Recovery Mode.** Shut off growth
  inputs, isolate gas lines, cryopump or baffle residual species, let the
  sample thermally relax, verify contamination limits, and confirm the
  system has returned to a clean low-load state.
- **Mode D — Quantum Sensing Mode.** Run the NV / helium isotope
  measurement at the millikelvin sensing condition. ³He/⁴He contrast,
  NV ODMR/Ramsey/T2* behaviour, and surface-interaction signals are
  measured here.

**Mode D sensing is not performed during Mode B processing.** Mode D is
entered only after Mode C isolation, purge, cryopumping/baffling, thermal
recovery, contamination verification, radiation/RF/optical/vibration
checks, and NV-baseline requalification. The transition `C_to_D` is a
BLOCKED gate (`mode_transition_acceptance_tests.csv` row T-C2D) with
explicit thermal, vacuum, contamination, radiation, RF, optical,
vibration, and NV-baseline sub-conditions.

### Architecture evolution

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


## Non-lumped 1D/2D multiphysics forecast layer

QTA now ships a serious 1D/2D non-lumped reduced-order multiphysics forecast
layer (`qta_multiphysics/`) in addition to the original lumped estimates. It
provides spatially-resolved forecast backends for thermal transport,
moving-boundary laser absorption, gas/contamination transport, surface coverage,
optical deposition, microwave heating, radiation leakage, vibration transfer,
and the coupled Mode B → Mode C → Mode D recovery cycle.

- **1D is the canonical, fast, default non-lumped path.** A finite-volume
  moving-boundary heat equation `rho Cp(T) dT/dt = d/dz[k(T) dT/dz] + Q_laser +
  Q_mw + Q_bg` with a Kapitza-type backside sink, integrated by method-of-lines
  with a stiff (BDF) solver.
- **2D axisymmetric is the serious spatial refinement.** The same physics in
  `(r,z)` with cylindrical `2*pi*r` weighting, a symmetry axis at `r=0`, a cold
  outer contact, and a Gaussian radial beam profile. It reduces to the 1D result
  to ~1.4% when radial transport is disabled (verified).
- **3D is FUTURE_WORK / NOT_IMPLEMENTED.** It is neither claimed nor used by any
  gate in this pass; the 3D entry points raise `NotImplementedError`.

Because the fs spot (~5 µm) and absorption depth (~1 µm) are microscopic relative
to the mm-scale sample, the thermal solvers resolve a near-field micro-domain
embedded in cold bulk — an explicit, documented reduced-order choice.

Everything in this layer is **model-only / forecast-only / pre-experimental**.
All of its outputs carry `measured_in_this_system = false`, no output carries a
MEASURED tag, and the 20 multiphysics gates it adds are all
CONDITIONAL / BLOCKED / DERIVED_CHECK — **none is PASS**. The legacy lumped model
is retained **only** as a comparator in `lumped_vs_nonlumped_comparison.csv`; the
non-lumped models are the gate authority. The layer's numerical self-consistency
(mesh convergence, energy conservation, Kapitza sign, 2D→1D reduction, coupling)
is recorded in `mesh_convergence_summary.csv`, `numerical_stability_summary.csv`,
and `multiphysics_verification_summary.csv`. None of this constitutes hardware
validation.


## Shielding and Isolation Stack

QTA uses shielding as a layered conditional isolation stack: thermal
radiation shielding, optical scatter shielding, microwave/RF leakage
shielding, magnetic-noise shielding, and chemical line-of-sight
shielding.

Existing design-specified elements include a 4 K OFHC copper radiation
shutter, radiation shield with labyrinth baffles, and a three-shutter
stack concept. Proposed hardening additions include an RF-tight 10 mK
sample can, staged IR/microwave filters with thermal anchoring at every
stage, nested cold optical apertures, thermally anchored cold beam
dumps, a dirty/clean dual shutter stack, sacrificial cryopanels and
regenerable cold traps, cryogenic magnetic shielding with NV
bias-compatibility gate, an optional superconducting shield with
explicit trapped-flux caution, and a microwave blackbody leakage gate.

None of these elements validate QTA by themselves. They only create
additional measured gates: `Shield-RAD`, `Shield-RF`, `Shield-OPT`,
`Shield-MAG`, `Shield-CHEM`. All are BLOCKED. Each requires its own
in-system measurement.

QTA treats shielding as a layered isolation stack, not as proof of
operation. No shielding subsystem can produce PASS without measured
in-system validation. Design-specified shielding may reduce modeled
risk; it cannot create PASS.

**See:** `shielding_stack_register.csv`, `radiation_rf_leakage_budget.csv`,
`optical_line_of_sight_audit.csv`, `cryopanel_memory_model.csv`,
`mode_transition_acceptance_tests.csv`, `reviewer_attack_map.md`,
`kill_gate_ranking.csv`.


## Optional RTB/JT Upstream Cooling Plant (Design Option Only)

QTA may optionally include a 4-8 module multi-stage RTB/JT-class upstream
cooling plant (reverse-turbo-Brayton / Joule-Thomson class). The baseline
design option uses 4 modules. A derated/redundant design option uses up
to 8 modules. The module count is not fixed by assumption; it must be
calculated from the required heat lift, derating factor, vibration
constraints, integration geometry, and vendor lift curves.

Sizing equation (DESIGN_OPTION only, not validated):

```
N_modules = ceil((P_ModeA_dump + P_parasitic + P_margin) * safety_factor
                 / P_lift_per_module_derated)
```

with safety_factor 2-4 until vendor curves and integration are measured.

**Purpose (upstream only):**

- 4 K stage and radiation shield cooling
- Cryogenic baffle cooling
- Gas-line thermal anchoring
- Mode B growth heat-dump path
- Upstream precooling / thermal isolation
- Reducing load before the 10 mK dilution / mixing-chamber stage

**Explicit non-purposes:**

- The RTB/JT plant is NOT a 10 mK cooler.
- It is NOT a replacement for dilution refrigeration / mixing-chamber cooling.
- It is NOT part of Mode D sensing validation unless measured integration data exists.
- It is NOT allowed to convert design-only gates to PASS.

**Status (everywhere in the package):**

- `RTB_JT_4_module_baseline` = DESIGN_OPTION
- `RTB_JT_8_module_derated_redundant` = DESIGN_OPTION
- `RTB_JT_selected` = false
- `RTB_JT_installed` = false
- `RTB_JT_validated` = false

A single new gate `RTB_JT_OPTIONAL_COOLING_PLANT` is BLOCKED. It requires
vendor lift curves, measured derated thermal-link performance, vibration
spectrum at sensitive interfaces, EMI coupling against the NV/microwave
chain, heat-rejection data, and demonstrated Mode D isolation before it
can move out of BLOCKED. It cannot unlock any other PASS gate.

External basis (feasibility reference only, not a QTA hardware claim):
Creare publicly describes RTB/JT-class cryocoolers covering loads from
roughly 1 kW at 100 K down to 300 mW at 10 K, with advanced developments
targeted as low as 4 K. This makes the upstream 4 K / shield / dump-stage
role plausible; it does not validate any QTA gate.

## REVIEWER NAVIGATION

If you are a technical reviewer, the recommended read order is:

1. `README.md` — this file
2. `REVIEWER_COVER_NOTE.md` — one-page summary of scope and request
3. `qta_manuscript_v4.pdf` — full manuscript
4. `results_gate_table.csv` — the 83-gate canonical decision table
5. `FIRST_VALIDATION_EXPERIMENTS.md` — proposed first experiments (priority order)
6. `risk_register.csv` — 107 risks (84 legacy numeric IDs + 16 shielding/mode entries + 7 RTB/JT entries)
7. `interface_map.csv` — interfaces I001 through I075
8. `source_audit_status.txt` — explicit REPRESENTATIVE_ONLY status of the source audit

Additional reviewer documents:

- `CLAIMS_BOUNDARY.md` — explicit list of what the package does and does not claim
- `REVIEWER_QUESTIONS.md` — eight specific technical questions
- `SUBMISSION_EMAIL_DRAFT.md` — drafted request-for-feedback email

To regenerate and verify the package:

```
python qta_full_sim.py
python package_consistency_check.py
```

**External system dependencies**: see `INSTALL.md`. Python 3 is required; `pdftotext` (Poppler/Xpdf) is optional and only needed for full PDF text validation. Without it, that single Step 7 check is reported as skipped and all other checks still run.

```
```

Both should exit 0. The first regenerates `results_gate_table.csv`,
`monte_carlo_summary.csv`, `tau_c_sweep.csv`, `interlock_table.csv`,
`best_forecast_operating_point.json`, and other sim outputs. The second
verifies the canonical state against every packaged file.

---

## CANONICAL STATE (single source of truth)

| Quantity | Canonical value |
|----------|-----------------|
| total gates | 83 |
| PASS | 0 |
| CONDITIONAL | 47 |
| BLOCKED | 23 |
| UNKNOWN | 2 |
| DERIVED_CHECK | 11 |
| tau_c canonical threshold | 292 µs |
| tau_c v3.0 (SUPERSEDED) | 27.728 µs (NOT_CANONICAL, NOT_LIVE_GATE_LOGIC) |
| LCVD during active sensing | NOT VIABLE, NOT CLAIMED |

These values are generated by `qta_full_sim.py` directly (A6-A14 included).
`package_consistency_check.py` validates every file against this table.

---

## CANONICAL MODE MAP

The canonical QTA mode map (this is the only valid map; any older A=GROWTH /
B=PURGE_RESET / C=RECOOL labeling is obsolete and has been removed from the
package):

- **Mode A — Cryogenic Baseline / Stabilization**
  Bring the chamber and sample into the required cryogenic state; verify
  thermal stability, vacuum, NV baseline; confirm readiness for any exposure
  or processing. Sensing OFF. LCVD OFF.
- **Mode B — Material Processing / LCVD Growth Mode**
  Precursor exposure, femtosecond laser processing, pulsed molecular-beam
  delivery, growth-side operations. Sensing OFF. Helium ABSENT.
- **Mode C — Isolation / Purge / Thermal Recovery Mode**
  Shut off growth inputs; isolate gas lines; cryopump or baffle residual
  species; thermal recovery; verify contamination/heat-load limits;
  RGA/QCM/witness-coupon checks; vibration settling.
- **Mode D — Sensing / Measurement Mode**
  NV / ³He isotope measurement at the millikelvin sensing condition.
  LCVD OFF. Precursor below threshold. Helium permitted only here.

Architecture (mode-separated, interlocked):

    stabilize -> process -> isolate/recover -> sense
       (A)       (B)            (C)           (D)

Mode B (material processing / LCVD growth) and Mode D (sensing) are
mutually exclusive, hardware-interlocked operating modes. They do not occur
simultaneously. IL-01, IL-02, and IL-14 enforce this.

---

## MODE B LCVD STATUS

**LCVD at 10 mK is not demonstrated.** Mode B is the proposed pulsed, local,
beam-fed material-processing / LCVD growth mode. Conventional chamber-fill LCVD is rejected
because hydrocarbon precursors cryocondense on cold surfaces. Mode B and Mode D
are mutually exclusive: sensing is OFF during Mode B.

Legacy gate IDs A6-A14 (kept for backward compatibility; "A" is a legacy ID prefix,
not a mode-letter reference) cover Mode B feasibility:

- A6 — precursor beam-delivered, not chamber-fill (CONDITIONAL)
- A7 — precursor cryocondensation below limit (CONDITIONAL)
- A8 — local surface temperature during deposition pulse measured (BLOCKED)
- A9 — deposition yield per pulse measured (BLOCKED — PRIMARY UNKNOWN)
- A10 — growth-zone contamination does not reach sensing zone (CONDITIONAL)
- A11 — byproducts pump below RGA threshold before Mode D (BLOCKED)
- A12 - Mode B (growth) heat removed by non-MC dump path (CONDITIONAL)
- A13 — repeated A/B/C cycling passes fatigue inspection (CONDITIONAL)
- A14 - He-3/He-4 film absent before Mode B Processing (BLOCKED; hardware interlock IL-14)

None of A6–A14 may reach PASS from assumptions, simulation, or literature.

---

## MODE B / MODE D OPTICS — STRICT SEPARATION

Mode B LCVD laser parameters are not the same as Mode D NV readout parameters.

| Vector | Mode B LCVD | Mode D NV readout |
|--------|-------------|-------------------|
| E_pulse | E_pulse_B (UNKNOWN) | E_pulse_D = 50 pJ (ASSUMED) |
| f_rep | f_rep_B (UNKNOWN) | f_rep_D = 200 Hz (ASSUMED) |
| tau_pulse | tau_pulse_B (UNKNOWN) | (Mode D readout pulse) |
| eta_abs | eta_abs_B (UNKNOWN) | eta_abs_D = 0.05 (ASSUMED) |
| T_peak | local surface T (UNKNOWN; A8) | T_peak_D = 673 mK (DERIVED) |

Mode D gates D6/D7 use only the Mode D vector. Legacy gates A6-A13 (Mode B
processing) use only the Mode B vector. The two vectors are never mixed in a single gate.

The previous manuscript paragraph mixing 1 MHz with 1 ms is corrected: those
were two different rate regimes for two different modes. The package now keeps
them distinct.

---

## MODE B NON-MC THERMAL DUMP ARCHITECTURE

The MC cannot remove Mode B laser heat in real time. Hardware (all NOT_INSTALLED):

- MC-protection switch (held open during Mode B)
- Growth thermal dump switch (closed during Mode B; growth-stage <-> 1 K or 4 K dump)
- Radiation shutter stack (closed during Mode B; protects 10 mK region)
- Independent thermometers on growth surface, sample bulk, MC, 1 K, 4 K stages

Legacy gate A12 (Mode B processing) covers the requirement. G_growth_to_dump and
G_growth_to_MC_leak are UNKNOWN.

---

## THERMAL CYCLING, STRESS, FATIGUE

Acceptance criteria: no cracks, no delamination, no conductance drift greater than
threshold, no RGA contamination increase, no measurable alignment drift, after
N = 100 cycles (engineering qualification) and N = 1000 cycles (extended qualification).
Status: CONDITIONAL until physically cycled. Gate A13.

---

## CANONICAL τ_c THRESHOLD

τ_c ≥ 292 µs (v3.3; combined SNR ≥ 5 + ε_thermo < 1% with pulse dephasing).
27.7 µs (v3.0) is SUPERSEDED / NOT_CANONICAL / NOT_LIVE_GATE_LOGIC.
The tau_c sweep gates against 292 µs; the 27.728 µs row is labeled SUPERSEDED_V30,
not PASS.

---

## C_contr CO-EQUAL BOTTLENECK

C_contr at 10 mK is UNKNOWN and co-equal with τ_c. If C_contr = 0 or too low,
detection fails regardless of τ_c.

---

## NO-PASS RULE

No gate reaches PASS from: ASSUMED, UNKNOWN, INDIRECT, MANUFACTURER_SPEC,
DESIGN_SPECIFIED, NOT_INSTALLED, INSTALLED_UNVERIFIED, or UNVERIFIED inputs.
All 83 gates: can_PASS_now = NO.

---

## THERMAL FEEDBACK FRAMEWORK

The canonical framework is mode-local secondary thermal feedback (Mode D only).
There is no live framework for between-mode coupling because Mode B and Mode D
are mutually exclusive.

---

## VESPEL STATUS

Vespel SP-22 direct support rods: REJECTED_BASELINE_ONLY.

- Vespel mentions in BOM.csv: 0
- Vespel mentions in rejected_baseline_BOM.csv: 1 row (RB001)
- Committed support: Kevlar 49 + G-10CR + OFHC Cu intercepts (DESIGN_SPECIFIED, NOT verified)

---

## ANCHORING STATUS

All mechanical, thermal, electrical, optical, microwave, gas-path, sensor,
heater, valve, sample-stage, and Mode B LCVD elements have anchoring fields
documented in BOM.csv. All anchoring paths DESIGN_SPECIFIED. No anchoring path
is verified until physically installed and measured.

---

## SIMULATION OUTPUT PATH STATUS

qta_full_sim.py uses `Path(__file__).resolve().parent / "outputs"`. Runs from
any directory. No hardcoded `/mnt/user-data/outputs`. Duplicate function
definitions removed: each of `make_mode_D_state`, `thermal_D`, `detection_D`,
`support_loads`, `snr_tc`, `intK` is defined exactly once.

---

## OLD best_operating_point.json STATUS

DELETED from live package. Quarantined as `superseded_best_operating_point_v3.json`
with `status: SUPERSEDED`, `canonical_status: NOT_CANONICAL`,
`live_gate_logic: NOT_LIVE_GATE_LOGIC`. Sim no longer writes that filename.
Live operating-point file is `best_forecast_operating_point.json` with
`canonical_tau_c_threshold_us = 292`.

---

## INCLUDED FILES

| File | Status | Description |
|------|--------|-------------|
| qta_manuscript_v4.pdf | COMPLETE_DRAFT | Manuscript with Mode B LCVD feasibility-limits section, gates A6–A14, IL-14, thermal dump, cycling |
| qta_manuscript_v4.tex | COMPLETE_DRAFT | LaTeX source. verdictbox macro uses \bfseries inside parbox (paragraph-safe) |
| qta_full_sim.py | COMPLETE | Relative output path; canonical tau_c=292; duplicate defs removed; tau_c_sweep gates against 292 µs |
| results_gate_table.csv | COMPLETE_DRAFT | 83 gates total — 0P 47C 23 BLOCKED 2U 11DC; all can_PASS_now=NO |
| monte_carlo_summary.csv | COMPLETE | 14 rows; forecast_only=true; current=0.0% |
| BOM.csv | COMPLETE_DRAFT | 121 rows with anchoring columns; 0 Vespel (added 51 validation/isolation entries B081-B131) |
| rejected_baseline_BOM.csv | COMPLETE | 2 rows (Vespel SP-22, AOM) |
| risk_register.csv | COMPLETE_DRAFT | 107 rows (84 legacy numeric IDs + 16 shielding/mode + 7 RTB/JT named entries) |
| validation_matrix.csv | COMPLETE_DRAFT | 157 rows (was 147; added RTB/JT optional cooling-plant rows) |
| interface_map.csv | COMPLETE_DRAFT | 75 rows (was 68; added I069-I075 for RTB/JT optional cooling plant) |
| interlock_table.csv | COMPLETE_DRAFT | 14 interlocks (added IL-14) |
| assumed_parameters.json | COMPLETE_DRAFT | 111 entries (was 81; added Mode B optics + Mode D readout split) |
| source_map.csv | COMPLETE_DRAFT | 60 rows; new schema |
| best_forecast_operating_point.json | COMPLETE | forecast_only=true; tau_c_canonical=292; Mode B status documented |
| superseded_best_operating_point_v3.json | QUARANTINED | SUPERSEDED labels |
| tau_c_sweep.csv | COMPLETE | 9 rows; canonical 292 µs gate; 27.728 µs row = SUPERSEDED_V30 |
| representative_source_audit.csv | REPRESENTATIVE_ONLY | 7 rows |
| source_audit_status.txt | COMPLETE | Explains audit incompleteness |
| output_sync_report.txt | COMPLETE | Real check |
| README.md | COMPLETE | This file |
| monte_carlo_parameter_registry.csv | COMPLETE_DRAFT | 175 parameters categorized FIXED_DESIGN/ASSUMED_UNCERTAIN/MEASURED_REQUIRED/DERIVED/MODE_DEPENDENT; sampling rules per category |
| monte_carlo_gate_failure_rates.csv | COMPLETE_DRAFT | 83 gates with forecast failure rates; BLOCKED=100% (hardware/measurement not present); CONDITIONAL=NOT_QUANTIFIED |
| monte_carlo_sensitivity_rankings.csv | COMPLETE_DRAFT | 13 ranked sensitivity parameters; tau_c rank 1, C_contr rank 2, T2* rank 3 |

---



**Detached manifest hashing.** `final_manifest.json` lists every canonical file in `files[]` with size and SHA-256. It does **not** list itself or `manifest_hash.txt` (per `self_hash_policy` field). The SHA-256 of `final_manifest.json` is recorded in the detached file `manifest_hash.txt`, written immediately after the manifest. This avoids any circular self-hash placeholder.

---

## FINAL PACKAGE TRUTH STATEMENT

Current physical system: BLOCKED.
Post-installation forecast: CONDITIONAL.
LCVD during active 10 mK sensing: NOT VIABLE / NOT CLAIMED.
Same-chamber mode-switched growth and sensing: PROPOSED, not demonstrated.
Mode D 10 mK sensing requires measured tau_c and measured C_contr.
Mode B LCVD requires measured deposition yield, precursor control, heat dumping,
contamination recovery, and fatigue survival.
