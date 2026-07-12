# FIRST VALIDATION EXPERIMENTS

Priority order. Each experiment is one falsifiable measurement on real
hardware. None of these has been performed; this list is what the package
proposes a future experimentalist would attempt first.

The package is BLOCKED until at least the first three of these are
performed and reported.

---

## A. NV ODMR contrast at 10 mK in the relevant diamond/NV configuration

- **Purpose**: establish whether shallow NV ensembles in the intended
  diamond surface termination retain usable ODMR contrast at the operating
  temperature. This is a co-equal bottleneck with τ_c — if contrast is too
  low, the architecture fails independent of τ_c.
- **Required measurement**: ODMR contrast C_contr on the actual diamond
  sample at 10 mK, with the proposed laser, microwave, and optical
  collection geometry. No proxy at 4 K or above is sufficient.
- **Gate affected**: D13 (Detection SNR), D14 (Optical Collection),
  D12_G23 (NV Charge State UNKNOWN), and the C_contr parameter that
  package documentation already flags as a co-equal bottleneck with τ_c.
- **Advance result**: measured C_contr at 10 mK consistent with the values
  used in the forecast Monte Carlo would let the relevant CONDITIONAL
  gates advance toward measurement-backed status (still not PASS until
  every other prerequisite is also measured).
- **Kill / redesign result**: C_contr collapses or becomes unstable below
  the assumed value, or NV charge state cannot be maintained at 10 mK
  under the proposed illumination. This would force a redesign of the
  optical readout architecture or abandonment of the shallow-NV approach.

---

## B. Helium-3 / helium-4 surface-induced decoherence contrast

- **Purpose**: establish whether the proposed sensing modality can actually
  distinguish ³He from ⁴He through surface-induced decoherence, as opposed
  to being dominated by other decoherence sources.
- **Required measurement**: τ_c (or equivalent NV decoherence observable)
  on the same diamond surface, under matched conditions, with controlled
  ³He coverage and controlled ⁴He coverage. The minimal experiment is a
  paired comparison at one coverage.
- **Gate affected**: D11 (He-3 Coverage), and the τ_c canonical threshold
  used by D13 and the forecast Monte Carlo.
- **Advance result**: a measurable ³He vs ⁴He contrast on the decoherence
  observable, with the ³He signal larger than other dephasing sources at
  some accessible coverage.
- **Kill / redesign result**: ³He and ⁴He produce indistinguishable
  surface-induced decoherence in this geometry, or the signal is buried
  under other decoherence channels. Either outcome would make the whole
  sensing premise unsupported.

---

## C. τ_c measurement or constraint relative to the 292 µs canonical requirement

- **Purpose**: directly test the package's canonical τ_c ≥ 292 µs threshold.
  The 27.728 µs (v3.0) value is SUPERSEDED and is not the live threshold.
- **Required measurement**: τ_c on the actual ³He / diamond system in the
  geometry of interest. A non-trivial upper or lower bound is sufficient
  for an initial evaluation — a measured τ_c well below ~10 µs would
  immediately falsify; a measured τ_c at or above ~100 µs would justify
  proceeding to the full required precision.
- **Gate affected**: D13 (Detection SNR with τ_c) and the canonical τ_c
  threshold used throughout the gate table and Monte Carlo.
- **Advance result**: τ_c sufficient to satisfy the canonical threshold
  under the measured C_contr.
- **Kill / redesign result**: τ_c far below threshold and not improvable by
  surface treatment, geometry change, or pulse sequence.

---

## D. Residual H2 coverage after bakeout, cryotrap, and NEG conditioning

- **Purpose**: test whether the proposed vacuum conditioning sequence can
  reach the residual hydrogen coverage that the architecture assumes.
- **Required measurement**: H2 partial pressure and equivalent surface
  coverage after the proposed bakeout-plus-NEG-plus-cryotrap sequence,
  measured by a calibrated RGA with appropriate Faraday-cup correction,
  on the actual chamber and surfaces of interest.
- **Gate affected**: D10a (H2 Engineering Readiness), D10b (H2 Physical
  Result), B3 (RGA all-species verification), E04 (RGA engineering
  readiness).
- **Advance result**: post-conditioning H2 coverage below the threshold
  used in the forecast.
- **Kill / redesign result**: H2 coverage that cannot be brought below
  threshold with reasonable conditioning, requiring either a different
  surface termination or abandoning the H2-sensitivity assumption.

---

## E. Optical / femtosecond-pulse thermal recovery to sensing baseline

- **Purpose**: test whether the assumed τ_recovery_D between Mode D
  readout pulses is physically realistic. The packaged value is ASSUMED,
  not measured.
- **Required measurement**: pump-probe or fast thermometric measurement
  of surface and sample-bulk temperature recovery after a representative
  Mode D readout pulse, at the operating laser fluence and at base
  temperature.
- **Gate affected**: D7 (Laser Transient — T_peak vs T_baseline), D6
  (Laser Average Heating).
- **Advance result**: measured recovery time short enough that cumulative
  pulse heating stays below the budget assumed in D7.
- **Kill / redesign result**: thermal recovery far longer than assumed,
  forcing either a much lower repetition rate, lower per-pulse energy,
  different surface termination, or abandonment of the pulsed-readout
  scheme at 10 mK.

---

## F. Dosing / purge isolation between growth and sensing modes

- **Purpose**: test whether the proposed mode-switched architecture can
  actually achieve sufficient growth-zone to sensing-zone isolation
  during dosing, purge, and recool — i.e. whether Mode B and Mode D can
  share the same chamber without cross-contamination corrupting Mode D
  measurements.
- **Required measurement**: witness coupon characterisation (AFM, Raman,
  XPS) and RGA all-species inventory of the sensing zone after a full
  Mode B pulse train and Mode C purge sequence, with the IL-02 and IL-14
  interlocks engaged.
- **Gate affected**: A6, A7, A10, A11, A14, and IL-02 / IL-14
  enforcement.
- **Advance result**: sensing-zone witness coupon and RGA indistinguishable
  from a clean baseline after the cycle.
- **Kill / redesign result**: measurable cross-contamination of the
  sensing zone or measurable helium presence during Mode B. This would
  force either separate chambers (abandoning the same-chamber premise)
  or a substantially different mode-switching architecture.

---

## Notes

- These experiments are listed in priority order. A, B, and C address the
  three quantities the package documentation flags as primary bottlenecks
  (C_contr at 10 mK, ³He vs ⁴He distinguishability, τ_c). D and E address
  the largest remaining ASSUMED parameters. F addresses the architectural
  premise itself.
- No PASS gate is achievable until the relevant in-system measurement is
  performed. Literature values, manufacturer specs, and design intent
  cannot upgrade any gate to PASS.
- The non-lumped 1D/2D multiphysics layer (qta_multiphysics/) sharpens what
  several of these experiments must measure, but does not substitute for any of
  them. Its forecasts are model-only. In particular: experiment E should measure
  the NV-layer temperature and post-recovery drift that the 1D/2D thermal solver
  forecasts (feeding NV_LAYER_TEMPERATURE_CHECK, HOTSPOT_MARGIN_CHECK,
  POST_PULSE_DRIFT_CHECK); experiment D should measure the residual gas/coverage
  the transport and surface-coverage models forecast (GAS_TRANSPORT_STABILITY_CHECK,
  CRYOBAFFLE_CAPTURE_CHECK, SURFACE_COVERAGE_DECAY_CHECK, RESIDUAL_SPECIES_MODE_D_CHECK);
  and the reduced-model inputs k(T), alpha_K, the absorption coefficient,
  sticking coefficients, and desorption energies (now registered in
  source_gap_register.csv) each require their own measurement before the
  corresponding gate can carry anything other than a forecast status. The
  layer's mesh-convergence / energy-conservation / Kapitza-sign / 2D->1D checks
  are numerical self-consistency only and are not hardware validation.

## Note on the deep experimental-design layer

The deep simulation-based-inference layer can rank candidate experiments by expected information
gain and is numerically validated against the direct nested-Monte-Carlo reference on
simulator-generated data. This is a computational acceleration, not a measurement: the
experiments listed here remain the first *physical* validations, and the deep layer does not
change any gate status, which stays forecast-only and never PASS.

> QTA includes direct Bayesian experimental design. A deep simulation-based inference and EIG layer may be trained and numerically validated against the direct reference estimator. This does not constitute experimental validation of the physical architecture.
