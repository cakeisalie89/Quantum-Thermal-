# EXP-N3 -- Rabi calibration in cryostat

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
Omega_R vs MW power with directional-coupler P_mw

## 2. Scientific question
What Rabi frequency does the delivered microwave power produce?

## 3. Gates served
E13

## 4. Matrix items served
P_mw, Omega_R-class items

## 5. Required machine mode
MODE_D

## 6. Exact device state
MODE_D_SENSE.D_PRECONFIG (dose off)

## 7. Sample or witness requirements
as EXP-N2

## 8. Instrument requirements
EXP-N2 chain; directional coupler

## 9. Calibration chain
coupler calibration; EXP-N2 chain in-window

## 10. Positive and negative controls
detuned drive; power-off

## 11. Prerequisites
EXP-N2

## 12. Step-by-step measurement procedure
1. Confirm EXP-N2 chain in-window; log directional-coupler P_mw.
2. Acquire Rabi fringes vs P_mw with detuned-drive and power-off controls.
3. Fit fringes; derive Omega_R(P_mw) with CRLB-based uncertainty.
4. Repeat per rule; export; hash; archive.

## 13. Quantities and units
Omega_R, P_mw [rad/s; W]

## 14. Repetition-count derivation
CRLB on Rabi-fringe fit; repeats from per-point sigma

## 15. Repetition-resolution status
UNRESOLVED_MISSING_COUNT_RATE

## 16. Exact missing datum when unresolved
per-point contrast sigma (from EXP-N2)

## 17. Raw-data format
fringe records

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
Rabi-fringe fits

## 20. Uncertainty treatment
CRLB on fringe fit

## 21. Acceptance criteria
Omega_R(P_mw) with uncertainty

## 22. Rejection criteria
chirp/heating artifacts

## 23. Stop criteria
MW-heating interlock trip

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
EXP-N2

## 26. Expected dossier outputs
Rabi dossier

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
