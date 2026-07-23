# EXP-T1 -- Thermal conductance and recovery

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
step-response G_eff and support load after Ag-sinter fabrication; pulsed recovery-to-baseline

## 2. Scientific question
What is the real thermal conductance and recovery behaviour of the fabricated link?

## 3. Gates served
COUPLED_MODE_RECOVERY_CHECK(constituent), C_to_D_Readiness(constituent)

## 4. Matrix items served
G_eff, support_thermal_load

## 5. Required machine mode
MODE_A, MODE_B

## 6. Exact device state
MODE_A_BASELINE.READY_IDLE; MODE_B_PROCESS.B_GROWTH_ACTIVE(attenuated)

## 7. Sample or witness requirements
instrumented stage (no diamond growth)

## 8. Instrument requirements
RuOx thermometer chain; calibrated heater; photodiode-monitored laser

## 9. Calibration chain
RuOx calibration against reference at base; heater power calibration

## 10. Positive and negative controls
heater-off drift record; zero-power baseline; closed-shutter laser control

## 11. Prerequisites
fridge commissioning

## 12. Step-by-step measurement procedure
1. Record heater-off drift and zero-power baseline at base.
2. Apply calibrated heater steps; log probe response to steady state.
3. Fit step responses for G_eff and support load.
4. Apply attenuated 532 nm pulses (closed-shutter control first); record recovery to baseline.
5. Compare recovery to the forecast band; report honestly either way.
6. Repeat per the repetition rule.
7. Export thermometry series; hash; archive.

## 13. Quantities and units
G_eff, support_load, t_recovery [W/K; W; s]

## 14. Repetition-count derivation
sigma_G/G from thermometer noise and Delta-T by standard propagation; repeat until SE <= target margin vs the ASSUMED G=1e-5 W/K comparison

## 15. Repetition-resolution status
UNRESOLVED_MISSING_INSTRUMENT_NOISE

## 16. Exact missing datum when unresolved
RuOx thermometer noise at the 10 mK operating point

## 17. Raw-data format
thermometry time-series CSV

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
step-response fits; recovery-curve comparison to the forecast band

## 20. Uncertainty treatment
thermometer-noise propagation to sigma_G/G

## 21. Acceptance criteria
G_eff with uncertainty; recovery curve honestly matching or failing the forecast

## 22. Rejection criteria
unexplained drift exceeding controls

## 23. Stop criteria
base-temperature loss

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
fridge commissioning

## 26. Expected dossier outputs
step-response dossiers

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
version 1.0; sources: Stage-6 assessment; validation_matrix.csv; FIRST_VALIDATION_EXPERIMENTS.md; results_gate_table.csv; forbidden modes: MODE_D; required
species: attenuated 532 nm only; forbidden species: He3, He4, C13_CH4; EIG provenance: validation_experiment_ranking.csv via plan alias EXP-E.
