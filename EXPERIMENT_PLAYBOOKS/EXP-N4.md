# EXP-N4 -- Ramsey/Hahn T2*, T2, tau_c (He-3 vs He-4)

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
decay constants and bath correlation time with the decisive helium-isotope surface-dose comparison; A14 film-absence monitored

## 2. Scientific question
What are T2*, T2 and tau_c on this surface, and does the He-3 vs He-4 comparison show the predicted isotope effect?

## 3. Gates served
E14, A14(monitored)

## 4. Matrix items served
tau_c, T2_star, T2, surface_spin_density(indirect)

## 5. Required machine mode
MODE_D

## 6. Exact device state
MODE_D_SENSE.D_HE_DOSE; MODE_D_SENSE.D_PRECONFIG

## 7. Sample or witness requirements
as EXP-N2

## 8. Instrument requirements
EXP-N2/N3 chain; He dosing line

## 9. Calibration chain
chains in-window; dosing manometer calibration

## 10. Positive and negative controls
no-dose baseline; He-4-only; sequence-order randomization

## 11. Prerequisites
EXP-N3

## 12. Step-by-step measurement procedure
1. Confirm EXP-N3 calibration; record no-dose baseline sequences (Ramsey/Hahn/XY8).
2. Dose He-4 per canonical window; repeat the sequence family.
3. Dose He-3; repeat; randomize sequence order across repeats.
4. Monitor film-absence signatures (A14) throughout.
5. >=5 delay points/decade over >=2 decades; repeat until sigma_tauc/tau_c <= 25% (per-point sigma from EXP-N2).
6. Filter-function analysis across sequences; report T2*, T2, tau_c and the isotope contrast honestly either sign.
7. Export decay records; hash; archive.

## 13. Quantities and units
T2_star, T2, tau_c, isotope_contrast [s; s; s; dimensionless]

## 14. Repetition-count derivation
CRLB on the decay-constant fit: >=5 delay points/decade over >=2 decades; repeat until sigma_tauc/tau_c <= 25% (enough to place tau_c against the 292 us canonical definition and the 27.7 us SUPERSEDED value)

## 15. Repetition-resolution status
UNRESOLVED_MISSING_COUNT_RATE

## 16. Exact missing datum when unresolved
per-point sigma from EXP-N2's measured contrast noise

## 17. Raw-data format
decay records per sequence

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
filter-function analysis across sequences; exponential/stretched fits

## 20. Uncertainty treatment
CRLB on decay-constant fits; >=5 delay points/decade over >=2 decades

## 21. Acceptance criteria
tau_c bound or value with uncertainty + isotope-contrast result either sign, reported honestly

## 22. Rejection criteria
charge instability per EXP-N2 criteria

## 23. Stop criteria
EXP-N2 stop conditions

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
EXP-N3

## 26. Expected dossier outputs
sequence-family dossiers incl. isotope comparison

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
species: He3 (Mode-D dose), He4 (Mode-D dose); forbidden species: C13_CH4; EIG provenance: validation_experiment_ranking.csv via plan alias EXP-B.
