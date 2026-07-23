# EXP-P1 -- Cryopanel sticking and capacity

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
measure sticking coefficients and areal capacity on panel coupon and F-diamond witness (closes Stage-2/3 ASSUMED/PLACEHOLDER parameters)

## 2. Scientific question
What are the actual sticking coefficients and areal capacities the Stage-2/3 models assume?

## 3. Gates served
Shield-CHEM(partial), A11(context), RESIDUAL_SPECIES_MODE_D_CHECK(context)

## 4. Matrix items served
H2_sticking, n_s, surface_coverage_proxy, surface_spin_density(indirect)

## 5. Required machine mode
MODE_A

## 6. Exact device state
MODE_A_BASELINE.READY_IDLE

## 7. Sample or witness requirements
panel-material coupon + F-terminated diamond witness

## 8. Instrument requirements
cryo-QCM at 4 K; TPD stage with mass spec; dosing manometer

## 9. Calibration chain
QCM frequency calibration; manometer calibration; TPD mass-scale calibration

## 10. Positive and negative controls
blank-crystal dose; temperature-ramp blank; isotope-free background

## 11. Prerequisites
EXP-V1

## 12. Step-by-step measurement procedure
1. Mount blank crystal; record temperature-ramp blank and isotope-free background.
2. Mount panel-material coupon; dose H2 per manometer schedule; record QCM uptake.
3. Repeat for CH4 dosing within Mode-A safe limits (no growth).
4. Mount F-diamond witness; repeat dosing series.
5. Run TPD ramps with mass-spec logging.
6. Fit Langmuir uptake vs coverage; integrate TPD.
7. Export frequency series + spectra; hash; archive.

## 13. Quantities and units
s_H2, s_CH4, areal_capacity [dimensionless; molecules/m^2 (monolayers)]

## 14. Repetition-count derivation
n = (sigma_shot/sigma_target)^2 with sigma_target sized to resolve the CSV's declared 0.3-0.8 sticking range to +-0.05

## 15. Repetition-resolution status
UNRESOLVED_MISSING_INSTRUMENT_NOISE

## 16. Exact missing datum when unresolved
cryo-QCM frequency-noise floor at 4 K

## 17. Raw-data format
QCM frequency time-series; TPD spectra

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
Langmuir-uptake fits vs coverage; TPD integration

## 20. Uncertainty treatment
frequency-noise statistics + dose systematics

## 21. Acceptance criteria
s and capacity with uncertainty and coverage-dependence curve

## 22. Rejection criteria
crystal-mount thermal artifacts

## 23. Stop criteria
panel contamination beyond regeneration capability

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
EXP-V1

## 26. Expected dossier outputs
uptake-curve dossiers per species

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
species: controlled dosing via calibrated line; forbidden species: live process flows; EIG provenance: no EXP-A..F alias; not EIG-ranked (consolidated infrastructure experiment).
