# EXP-N2 -- ODMR contrast and charge state at 10 mK

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
C_contr and NV-/NV0 charge stability vs optical power at base temperature

## 2. Scientific question
Is the NV charge state stable, and what ODMR contrast exists at 10 mK on this sample?

## 3. Gates served
E12, D12_G23

## 4. Matrix items served
C_contr_10mK, charge_stability

## 5. Required machine mode
MODE_D

## 6. Exact device state
MODE_D_SENSE.D_PRECONFIG (dose off)

## 7. Sample or witness requirements
screened sample from EXP-N0

## 8. Instrument requirements
ODMR chain (MW + optics); SPAD/PMT

## 9. Calibration chain
EXP-N1 collection calibration in-window; MW chain via directional coupler

## 10. Positive and negative controls
off-resonance MW; power-sweep hysteresis check; room-T reference on the same sample

## 11. Prerequisites
EXP-N1, EXP-N0

## 12. Step-by-step measurement procedure
1. Confirm EXP-N1 calibration in-window; sample from EXP-N0 screening.
2. Record room-T reference on the same sample.
3. At 10 mK, D_PRECONFIG dose-off: run the optical power sweep up and down (hysteresis check); identify the charge plateau.
4. Acquire ODMR with off-resonance control interleaved.
5. Average per the photon-shot rule once N_ph is measured (missing datum resolved by EXP-N1/this run).
6. Report C_contr and charge stability; export; hash; archive.

## 13. Quantities and units
C_contr, NV_charge_ratio [dimensionless]

## 14. Repetition-count derivation
photon-shot-limited: sigma_C per readout ~ sqrt(2/N_ph); n_avg = (sigma_C,shot/sigma_target)^2 with sigma_target = 1/10 of the contrast threshold margin

## 15. Repetition-resolution status
UNRESOLVED_MISSING_COUNT_RATE

## 16. Exact missing datum when unresolved
in-cryostat count rate N_ph per readout (from EXP-N1)

## 17. Raw-data format
photon-count records; sweep tables

## 18. Sidecar metadata requirements
Stage-5 sidecar metadata per raw_data_standard.md

## 19. Analysis method
contrast fits; plateau identification

## 20. Uncertainty treatment
photon-shot per-readout sigma_C ~ sqrt(2/N_ph)

## 21. Acceptance criteria
C_contr with uncertainty + stable charge plateau

## 22. Rejection criteria
unbounded photo-ionization drift

## 23. Stop criteria
sample damage signatures

## 24. Interlock and safety conditions
All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain authoritative; any interlock trip is an immediate stop; no procedure step may bypass a guard, valve, shutter or device-state rule; species policy absolute (methane only in Mode B; helium only in Mode D; no simultaneous processing and sensing).

## 25. Dependencies
EXP-N1, EXP-N0

## 26. Expected dossier outputs
contrast + charge dossiers

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
species: none; forbidden species: C13_CH4, He3 (dose off), He4 (dose off); EIG provenance: validation_experiment_ranking.csv via plan alias EXP-A.
