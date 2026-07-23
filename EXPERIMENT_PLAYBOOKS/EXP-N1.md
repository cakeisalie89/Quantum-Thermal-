# EXP-N1 -- In-cryostat optical collection calibration

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
eta_col via calibrated emitter / reference ensemble in the Mode-D optical path

## 2. Scientific question
What fraction of NV emission does the real cryostat path collect?

## 3. Gates served
E11

## 4. Matrix items served
eta_col

## 5. Required machine mode
MODE_D

## 6. Exact device state
MODE_D_SENSE.D_PRECONFIG

## 7. Sample or witness requirements
calibrated emitter or reference NV ensemble

## 8. Instrument requirements
calibrated emitter; SPAD/PMT chain

## 9. Calibration chain
emitter calibration certificate; detector dead-time characterization

## 10. Positive and negative controls
dark counts; misaligned-reference

## 11. Prerequisites
EXP-N0, fridge+optics

## 12. Step-by-step measurement procedure
1. Install calibrated emitter/reference in the Mode-D optical path (D_PRECONFIG, dose off).
2. Record dark counts and the misaligned-reference control.
3. Acquire counts vs known emission; apply dead-time correction.
4. Average adaptively to the pre-registered SE target (conditionally resolvable during run).
5. Report eta_col with uncertainty; export; hash; archive.

## 13. Quantities and units
eta_col [dimensionless]

## 14. Repetition-count derivation
photon-shot rule n = (sigma_shot/sigma_target)^2; per-shot sigma from the measured count rate (adaptive-n, pre-registered target)

## 15. Repetition-resolution status
CONDITIONALLY_RESOLVABLE_DURING_RUN

## 16. Exact missing datum when unresolved
measured count rate during the run itself

## 17. Raw-data format
count-rate time-series

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
ratio to known emission with dead-time correction

## 20. Uncertainty treatment
photon-shot statistics (adaptive-n to pre-registered target)

## 21. Acceptance criteria
eta_col with uncertainty

## 22. Rejection criteria
alignment drift beyond controls

## 23. Stop criteria
cryostat window damage

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
EXP-N0, fridge+optics

## 26. Expected dossier outputs
calibration dossier

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
version 1.0; sources: Stage-6 assessment; validation_matrix.csv; FIRST_VALIDATION_EXPERIMENTS.md; results_gate_table.csv; forbidden modes: MODE_B; required
species: none; forbidden species: C13_CH4, He3 (dose off), He4 (dose off); EIG provenance: no EXP-A..F alias; not EIG-ranked (consolidated infrastructure experiment).
