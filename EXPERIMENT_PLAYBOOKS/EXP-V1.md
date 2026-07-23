# EXP-V1 -- Bakeout + RGA all-species baseline

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
establish the post-bakeout residual-gas baseline with the FC-corrected all-species RGA protocol

## 2. Scientific question
Does the conditioned chamber reach the canonical residual targets, species-resolved, with defensible calibration?

## 3. Gates served
E01, E04, B3, D10a, D10b(enables), A11(instrumented), D2(instrumented), RESIDUAL_SPECIES_MODE_D_CHECK(instrumented), Shield-CHEM(partial)

## 4. Matrix items served
P_H2-class residual items (per matrix routing)

## 5. Required machine mode
MODE_A

## 6. Exact device state
PREP_VACUUM.POST_BAKEOUT_VERIFY; MODE_A_BASELINE.READY_IDLE

## 7. Sample or witness requirements
none (no diamond required)

## 8. Instrument requirements
quadrupole RGA; bakeout thermocouples; certified leak

## 9. Calibration chain
RGA mass+FC gain calibration against certified leak, in-window; thermocouple traceable calibration

## 10. Positive and negative controls
pre-bake baseline scan; filament-off background; empty-chamber blank

## 11. Prerequisites
none

## 12. Step-by-step measurement procedure
1. Verify interlock chain green and IVC sealed; record chamber state.
2. Execute the 250 C / 48 h bakeout per E01 with thermocouple logging.
3. Cool to operating configuration; record NEG/cryotrap conditioning steps.
4. Acquire filament-off background, then pre-analysis blank.
5. Run the FC-corrected all-species RGA protocol; log dwell and averaging.
6. Repeat scans per the repetition rule until the SE criterion is met or the missing noise-floor datum is itself measured and recorded.
7. Export vendor spectra + CSV; write sidecar metadata; hash and archive raw files.

## 13. Quantities and units
P_H2, P_CH4, P_He, P_H2O, P_CO, P_CO2, P_hydrocarbons [Pa]

## 14. Repetition-count derivation
average scans until SE(P_i) <= 1/3 of the margin to each canonical pressure threshold; n and dwell follow from the RGA minimum-detectable-partial-pressure and noise density

## 15. Repetition-resolution status
UNRESOLVED_MISSING_INSTRUMENT_NOISE

## 16. Exact missing datum when unresolved
RGA model noise density / MDPP spec (a Campaign-1 output)

## 17. Raw-data format
vendor RGA spectra; CSV export

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
background-subtracted species fits; drift monitoring

## 20. Uncertainty treatment
scan-ensemble statistics + calibration systematics stated separately

## 21. Acceptance criteria
stable post-bake spectra, backgrounds subtracted, calibration in-window, uncertainties stated

## 22. Rejection criteria
FC calibration drift beyond vendor spec; non-stationary background

## 23. Stop criteria
pressure excursion beyond interlock limits

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
none

## 26. Expected dossier outputs
hardware records + calibration records + backgrounds; dossier per gate

## 27. Human review requirements
Human review per Stage-5 governance: HARDWARE_UNVERIFIED records quarantine with deficiencies; only complete HARDWARE_REVIEWED records with a valid ACCEPT_AS_EVIDENCE review enter dossiers; tools never author or promote reviews.

## 28. Claim limitations
planning infrastructure only; not experimental evidence; no experiment performed; no hardware claim

## 29. Automatic gate effect
NONE -- no measurement, dossier, or review produced under this playbook
can change a gate, a matrix status, or a parameter; matrix changes travel
only through human-authored update requests reviewed under Stage-5
governance.

## 30. Version and provenance
version 1.0; sources: Stage-6 assessment; validation_matrix.csv; FIRST_VALIDATION_EXPERIMENTS.md; results_gate_table.csv; forbidden modes: MODE_B, MODE_D; required
species: none; forbidden species: C13_CH4 (live), He3 (live), He4 (live); EIG provenance: no EXP-A..F alias; not EIG-ranked (consolidated infrastructure experiment).
