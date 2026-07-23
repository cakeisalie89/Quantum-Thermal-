# EXP-N0 -- Bench optical pre-characterization

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
532 nm transmission/reflection, PL depth / SIMS, sample screening (dependency reducer)

## 2. Scientific question
Which candidate samples have the optical properties the architecture assumes?

## 3. Gates served
(none directly; see matrix items)

## 4. Matrix items served
eta_abs, d_NV

## 5. Required machine mode
MODE_A(bench analog)

## 6. Exact device state
bench (outside machine)

## 7. Sample or witness requirements
candidate diamond samples

## 8. Instrument requirements
bench 532 nm source; NIST-traceable power meter; PL microscope / SIMS access

## 9. Calibration chain
power-meter traceable calibration; SIMS standard

## 10. Positive and negative controls
substrate-only blank; meter dark reading

## 11. Prerequisites
none

## 12. Step-by-step measurement procedure
1. Record power-meter dark reading and substrate-only blank.
2. Measure 532 nm transmission/reflection per candidate sample (300 K, then 4 K bench stage).
3. Acquire PL maps; schedule SIMS depth profiles.
4. Screen and rank samples; record eta_abs and d_NV with uncertainty.
5. Export meter CSV + maps + profiles; hash; archive.

## 13. Quantities and units
eta_abs, d_NV [dimensionless; m]

## 14. Repetition-count derivation
power-meter-limited averaging per vendor spec

## 15. Repetition-resolution status
CONDITIONALLY_RESOLVABLE_DURING_RUN

## 16. Exact missing datum when unresolved
vendor spec sheet confirmation at run time

## 17. Raw-data format
power-meter CSV; PL maps; SIMS profiles

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
transmission/reflection arithmetic; depth-profile fits

## 20. Uncertainty treatment
vendor-spec meter uncertainty + repeat statistics

## 21. Acceptance criteria
eta_abs and d_NV with uncertainty per sample

## 22. Rejection criteria
surface-contamination artifacts

## 23. Stop criteria
sample damage

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
none

## 26. Expected dossier outputs
per-sample screening dossiers

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
species: none; forbidden species: none; EIG provenance: no EXP-A..F alias; not EIG-ranked (consolidated infrastructure experiment).
