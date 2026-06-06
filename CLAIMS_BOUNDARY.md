# CLAIMS BOUNDARY

This file records, explicitly, what the QTA package does and does not
claim. It is the single point of reference for the package's scientific
modesty.

## The package does NOT claim

- The non-lumped 1D/2D multiphysics layer (qta_multiphysics/) is model-only / forecast-only / pre-experimental. Its 1D and 2D thermal, optical, gas-transport, surface-coverage, microwave, radiation, vibration, and coupled Mode B->C->D backends produce forecasts only; 3D is FUTURE_WORK / NOT_IMPLEMENTED. No multiphysics output is measured in this system, none carries a MEASURED tag, and none of its 20 gates is PASS. The legacy lumped model is retained only as a comparator. Numerical convergence/verification is not hardware validation.
- No PASS gates. The current canonical state is 0 PASS out of 83 gates (including the 20 non-lumped multiphysics gates added in pass 15).
- No proof of feasibility.
- No validated hardware. Every hardware item in BOM.csv is either
  DESIGN_SPECIFIED, NOT_INSTALLED, INSTALLED_UNVERIFIED, or
  MANUFACTURER_SPEC. No item is in-system VERIFIED.
- No breakthrough claim. The package documentation does not present any
  result as a breakthrough.
- No DARPA-ready claim. The package is not presented as ready for any
  programme, milestone, or technology-readiness-level assertion.
- No Nobel-worthy claim. The package is not presented as a candidate for
  any prize, award, or extraordinary-claim status.
- No simultaneous LCVD + sensing. The architecture explicitly forbids
  concurrent operation of laser-CVD growth and 10 mK helium sensing.

## The package DOES describe

- LCVD (Mode B) and 10 mK NV sensing (Mode D) as mutually exclusive
  mode-switched operations inside the same chamber. The interlocks
  IL-01, IL-02, and IL-14 are the hardware mechanism that would enforce
  this separation. None of those interlocks is installed. Mode-switched
  operation is therefore PROPOSED, not demonstrated.
- A canonical τ_c threshold of 292 µs (v3.3) for the detection-SNR
  inequality. The earlier 27.728 µs value (v3.0) is SUPERSEDED,
  NOT_CANONICAL, and NOT_LIVE_GATE_LOGIC.
- C_contr at 10 mK as a co-equal bottleneck with τ_c. Both are UNKNOWN
  in this system.
- Mode B LCVD feasibility limits (gates A6–A14) including cryocondensation
  risk, unknown deposition yield, unknown local surface temperature
  during the deposition pulse, non-MC thermal dump requirements, thermal
  cycling acceptance criteria (N=100 engineering / N=1000 extended),
  and helium exclusion during Mode B processing (IL-14).

## Source audit status

The included source audit is REPRESENTATIVE_ONLY (7 rows from a
~100+ rows required for submission). The status note in
`source_audit_status.txt` records this explicitly. A full submission-grade
numerical source audit remains incomplete. No citation upgrades any
ASSUMED parameter to VERIFIED. Literature supports plausibility only.

The manuscript bibliography contains 23 entries (65 considered in the citation audit; 42 removed as uncited) against an
INCOMPLETE_DRAFT target of 100–200+. The manuscript is not in
publication-ready form.

## Operational rules

- No ASSUMED, UNKNOWN, LITERATURE_BOUND, MANUFACTURER_SPEC,
  DESIGN_SPECIFIED, NOT_INSTALLED, INSTALLED_UNVERIFIED, or UNVERIFIED
  parameter may produce a PASS gate.
- DERIVED_CHECK is not PASS. D9 (Knudsen number) is DERIVED_CHECK because
  it is a self-consistent first-principles gas-kinetics computation
  given the geometric inputs; it does not imply any physical
  measurement has been performed.
- Forecast Monte Carlo results carry `forecast_only = true` and
  `physically_demonstrated = false`. The current full-cycle MC is 0.0%.


## Monte Carlo: forecast / sensitivity only

The Monte Carlo runs in this package — both per-gate sensitivity sampling
and the post-bakeout forecast operating point — are forecast and
sensitivity analysis only. Specifically:

- The Monte Carlo does not unlock physical gates. A high MC pass rate
  does not move any gate from CONDITIONAL or BLOCKED to PASS.
- The Monte Carlo does not represent experimental validation. The samples
  are drawn from assumed parameter distributions, not from in-system
  measurements on the actual hardware.
- Physical gates remain CONDITIONAL or BLOCKED until in-system hardware
  measurements exist for every input that the gate consumes. This is
  enforced by `package_consistency_check.py`.
- The PASS count is 0 and remains 0 under any Monte Carlo result.
- `best_forecast_operating_point.json` carries `forecast_only=true`,
  `physically_demonstrated=false`, and `not_an_achieved_operating_point` to
  make the forecast status explicit at the data-level. The current
  full-cycle Monte Carlo pass rate is 0.0%.


## Forbidden claims about shielding and mode separation

**Forbidden:**

- "QTA has validated radiation shielding."
- "QTA shielding proves 10 mK Mode D operation."
- "Cryo-baffles prove contamination is solved."
- "Mode B processing and Mode D sensing occur simultaneously."
- "Monte Carlo validates the shielding stack."
- "RF/IR shielding is sufficient without measurement."
- "The radiation shutter stack has been experimentally proven."
- "The cryopanels solve Mode B → Mode D contamination without measurement."
- "The magnetic shield is compatible with NV sensing without bias-field validation."

**Allowed:**

- "QTA includes design-specified shielding and isolation layers."
- "Radiation, RF, optical, magnetic, and chemical shielding are
  represented as conditional gates."
- "Mode D requires measured readiness after Mode C recovery."
- "Shielding reduces modeled risk only; it does not create PASS."
- "The first iteration was cross-coupled simultaneous; the current
  rewrite is mode-separated and interlocked."

These are the canonical statements that bound what the package claims
about shielding and about the relationship between Mode B and Mode D.
The shielding gates `Shield-RAD`, `Shield-RF`, `Shield-OPT`,
`Shield-MAG`, `Shield-CHEM`, and `C_to_D_Readiness` are all BLOCKED and
remain BLOCKED until in-system measurements exist.


## Forbidden claims about the optional RTB/JT cooling plant

**Forbidden:**

- "QTA has selected RTB/JT cooling."
- "QTA has installed RTB/JT cooling."
- "QTA RTB/JT cooling is validated."
- "RTB/JT provides 10 mK cooling in QTA."
- "RTB/JT replaces the dilution refrigerator."
- "RTB/JT unlocks any PASS gate."
- "QTA uses 25 RTB modules."
- "QTA uses 25 JT modules."
- "QTA uses 25 reverse-turbo-Brayton modules."
- "RTB/JT validates Mode D."

**Allowed:**

- "QTA may optionally include a 4-8 module RTB/JT-class upstream cooling plant."
- "The baseline design option is 4 modules; the derated/redundant design option is 8 modules."
- "RTB/JT is an upstream 4 K / shield / cryobaffle / Mode B dump-stage option."
- "RTB/JT is not a 10 mK cooling source and does not replace the dilution refrigerator."
- "RTB/JT module count must be calculated from heat lift, derating, vibration, integration geometry, and vendor lift curves."
- "All RTB/JT statuses remain DESIGN_OPTION / UNKNOWN / NOT_SELECTED / NOT_INSTALLED / NOT_VALIDATED."

The gate `RTB_JT_OPTIONAL_COOLING_PLANT` is BLOCKED. It cannot unlock any
other gate. PASS count remains 0.

## How to disagree with the claims boundary

If a reviewer believes the package implicitly claims more than this file
states, the package should be considered defective and the over-claiming
language reported. The intent is that this file is the strongest
statement of position in the entire package.
