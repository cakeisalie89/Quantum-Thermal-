# EXP-T2 -- In-situ surface T and per-pulse yield

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
surface temperature during Mode-B pulses + deposition yield per pulse

## 2. Scientific question
What surface temperature and per-pulse yield does Mode-B processing actually produce?

## 3. Gates served
A8, A9

## 4. Matrix items served
A8/A9-class items

## 5. Required machine mode
MODE_B

## 6. Exact device state
MODE_B_PROCESS.B_GROWTH_ACTIVE

## 7. Sample or witness requirements
growth witness + diamond substrate

## 8. Instrument requirements
thermoreflectance or pyrometer; QCM witness; ellipsometer/AFM (post)

## 9. Calibration chain
detector radiometric calibration; QCM calibration

## 10. Positive and negative controls
no-precursor pulses (pure heating); no-pulse dosing

## 11. Prerequisites
EXP-V1, EXP-V2, laser line

## 12. Step-by-step measurement procedure
1. Verify EXP-V1/V2 clean baseline and laser-line calibration.
2. Run no-precursor pulses (pure heating control) with thermoreflectance/pyrometry logging.
3. Run no-pulse dosing control.
4. Run pulsed growth sets in B_GROWTH_ACTIVE; log surface-T traces per pulse ensemble.
5. Measure per-pulse yield via QCM witness and post-growth ellipsometry/AFM.
6. Repeat ensembles per the repetition rule; watch stop criteria continuously.
7. Export, hash, archive.

## 13. Quantities and units
T_surface(t), yield_per_pulse [K; nm/pulse]

## 14. Repetition-count derivation
pulse-ensemble averaging vs detector shot noise; n from (sigma_shot/sigma_target)^2

## 15. Repetition-resolution status
UNRESOLVED_MISSING_INSTRUMENT_NOISE

## 16. Exact missing datum when unresolved
thermoreflectance/pyrometer detector noise floor

## 17. Raw-data format
detector time-series; post-growth metrology files

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
pulse-ensemble averaging; yield regression vs pulse count

## 20. Uncertainty treatment
detector-shot statistics + radiometric systematics

## 21. Acceptance criteria
T_surface and yield with uncertainty

## 22. Rejection criteria
window fouling artifacts

## 23. Stop criteria
sample damage signatures

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
EXP-V1, EXP-V2, laser line

## 26. Expected dossier outputs
per-pulse-set dossiers

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
species: C13_CH4 (Mode-B only); forbidden species: He3, He4; EIG provenance: no EXP-A..F alias; not EIG-ranked (consolidated infrastructure experiment).
