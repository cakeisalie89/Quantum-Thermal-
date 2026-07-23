# EXP-V2 -- Purge/dosing isolation dynamics

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
time-resolved purge/valve isolation between growth and sensing modes (no NV sensing)

## 2. Scientific question
How fast and how completely do purge/valve sequences isolate processing from sensing?

## 3. Gates served
A11, D1, D2, RESIDUAL_SPECIES_MODE_D_CHECK, Shield-CHEM(partial), COUPLED_MODE_RECOVERY_CHECK(constituent), C_to_D_Readiness(constituent)

## 4. Matrix items served
molecular_flux, purge-class items

## 5. Required machine mode
MODE_A, MODE_B, MODE_C, MODE_D

## 6. Exact device state
MODE_B_PROCESS.B_SOURCE_OFF; MODE_C_RECOVERY.C_PURGE; MODE_D_SENSE.D_PRECONFIG

## 7. Sample or witness requirements
none

## 8. Instrument requirements
EXP-V1 RGA; calibrated leak

## 9. Calibration chain
as EXP-V1 + leak certificate in-window

## 10. Positive and negative controls
no-injection sequence; valve-closed leak-through scan

## 11. Prerequisites
EXP-V1

## 12. Step-by-step measurement procedure
1. Confirm EXP-V1 baseline in force.
2. Walk device states B_SOURCE_OFF -> C_PURGE -> D_PRECONFIG with the calibrated leak as surrogate injection.
3. Record P_i(t) through each transition; repeat the no-injection control sequence.
4. Acquire valve-closed leak-through scans.
5. Fit exponential decays per species; log residuals.
6. Repeat sequences per the repetition rule.
7. Export, hash, archive.

## 13. Quantities and units
P_i(t), tau_purge, isolation_ratio [Pa; 1/s; dimensionless]

## 14. Repetition-count derivation
fit tau_purge per sequence; n_sequences from target sigma_tau/tau via fit-residual scatter

## 15. Repetition-resolution status
UNRESOLVED_MISSING_REPEATABILITY

## 16. Exact missing datum when unresolved
per-scan RGA repeatability (from EXP-V1)

## 17. Raw-data format
RGA time-series CSV

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
exponential decay fits per species

## 20. Uncertainty treatment
fit-residual statistics; systematic leak-rate uncertainty

## 21. Acceptance criteria
tau_purge and isolation ratios with stated uncertainty

## 22. Rejection criteria
unexplained non-exponential contamination signatures

## 23. Stop criteria
interlock-limit pressures

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
EXP-V1

## 26. Expected dossier outputs
sequence dossiers binding V1 baseline

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
version 1.0; sources: Stage-6 assessment; validation_matrix.csv; FIRST_VALIDATION_EXPERIMENTS.md; results_gate_table.csv; forbidden modes: none; required
species: surrogate/calibrated leak injection only; forbidden species: C13_CH4 (live at full process rates), He3 (live), He4 (live); EIG provenance: validation_experiment_ranking.csv via plan alias EXP-F.
