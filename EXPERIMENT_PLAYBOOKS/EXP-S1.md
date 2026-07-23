# EXP-S1 -- Environment survey: vibration at NV + shield quartet

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
vibration spectrum at the sensing location (IL-08 path) plus radiative, RF, optical-scatter and magnetic surveys

## 2. Scientific question
What are the true environmental loads -- vibration at the NV location foremost -- against the Mode-D budgets?

## 3. Gates served
Shield-RAD, Shield-RF, Shield-OPT, Shield-MAG

## 4. Matrix items served
vibration/shield-class items

## 5. Required machine mode
MODE_D(device states, sources off)

## 6. Exact device state
MODE_D_SENSE.D_PRECONFIG (all sources off)

## 7. Sample or witness requirements
none

## 8. Instrument requirements
geophone/interferometer; bolometric radiometer; RF spectrum analyzer; calibrated photodiode; magnetometer

## 9. Calibration chain
each sensor's calibration certificate in-window; self-noise measured first

## 10. Positive and negative controls
sensor self-noise floors (mandatory first); pumps-off/on pairs; shutter open/closed pairs

## 11. Prerequisites
none

## 12. Step-by-step measurement procedure
1. FIRST: record every sensor's self-noise floor (mandatory control; resolves this experiment's own repetition inputs).
2. Mount vibration sensor at the NV sensing location; acquire pumps-off/pumps-on spectral pairs.
3. Welch-average per the rule until the in-band SE criterion vs the 1e-10 m threshold is met.
4. Acquire radiative-load, RF-leakage, optical-scatter and magnetic surveys with shutter open/closed pairs.
5. Report every survey with uncertainty -- including an honest confirmation of the forecast exceedance if that is the outcome.
6. Export spectra; hash; archive.

## 13. Quantities and units
a_vib(f), P_rad, P_RF(f), P_opt_scatter, B_noise(f), B_bias_stability [m/sqrt(Hz), m in-band; W; dBm; W; T/sqrt(Hz); T]

## 14. Repetition-count derivation
Welch spectral averaging: n_avg segments until in-band amplitude SE <= 1/3 of the margin to the canonical 1e-10 m threshold (sensor self-noise measured first)

## 15. Repetition-resolution status
UNRESOLVED_MISSING_INSTRUMENT_NOISE

## 16. Exact missing datum when unresolved
each environment sensor's self-noise floor (measured as the first sub-step of this experiment)

## 17. Raw-data format
spectra + time-series per sensor

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
Welch spectral estimation; in-band integration

## 20. Uncertainty treatment
segment-averaging SE; calibration systematics

## 21. Acceptance criteria
in-band vibration amplitude with uncertainty vs the 1e-10 m threshold WHATEVER the outcome (forecast 4.65e-9 m is ABOVE threshold; honest confirmation keeps IL-08 refusing) + the four shield surveys with uncertainty

## 22. Rejection criteria
sensor overload/clipping

## 23. Stop criteria
none beyond instrument safety

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
none

## 26. Expected dossier outputs
per-survey dossiers

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
species: none; forbidden species: C13_CH4, He3, He4; EIG provenance: no EXP-A..F alias; not EIG-ranked (consolidated infrastructure experiment).
