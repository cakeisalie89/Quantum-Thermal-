# QTA — Forecast-Only Multiphysics Feasibility Package

## 1. What this is

QTA is a forecast-only, pre-experimental feasibility framework for a gated,
mode-separated 10 mK nitrogen-vacancy (NV) / helium-surface quantum-sensing
architecture with optional in-chamber laser-driven (LCVD) material processing.
It encodes the architecture as 83 explicit decision gates whose statuses are
computed deterministically by `qta_full_sim.py`, and ships a non-lumped 1D/2D
multiphysics layer (`qta_multiphysics/`) that produces spatially-resolved
thermal, transport, surface-coverage, and mode-recovery forecasts. It exists so
the gating logic can be inspected, run, and disagreed with — it is not a
validated system and reports no experimental result.

## 2. Current status and claim boundary

Current physical system: **BLOCKED**. Post-installation forecast: **CONDITIONAL**.
There is no validated hardware, no in-system measurement, and no breakthrough
claim. Stated once and enforced everywhere:

- 0 of 83 gates are PASS, and every gate has `can_PASS_now = NO`.
- Every output carries `measured_in_this_system = false`; nothing is tagged MEASURED.
- LCVD during active 10 mK sensing is NOT VIABLE and NOT CLAIMED.
- Same-chamber, mode-switched growth and sensing is PROPOSED, not demonstrated.
- The additive 3D transient layer is forecast-only numerical code (see section 5).

No gate may reach PASS from ASSUMED, UNKNOWN, INDIRECT, MANUFACTURER_SPEC,
DESIGN_SPECIFIED, NOT_INSTALLED, INSTALLED_UNVERIFIED, or UNVERIFIED inputs.
The full list of what the package does and does not claim is in `CLAIMS_BOUNDARY.md`.

## 3. Canonical architecture: A -> B -> C -> D

The platform is mode-separated and hardware-interlocked:

    stabilize  ->  process  ->  isolate/recover  ->  sense
       (A)          (B)             (C)               (D)

- **Mode A — Cryogenic Baseline / Stabilization.** Bring chamber and sample to
  the cryogenic state; verify thermal stability, vacuum, and NV baseline.
  Sensing OFF, LCVD OFF.
- **Mode B — Material Processing / LCVD Growth.** Precursor exposure,
  femtosecond-laser processing, pulsed molecular-beam delivery. Sensing OFF,
  helium absent.
- **Mode C — Isolation / Purge / Thermal Recovery.** Shut off growth inputs,
  isolate gas lines, cryopump or baffle residual species, recover thermally,
  verify contamination and heat-load limits.
- **Mode D — Sensing / Measurement.** NV / 3He-4He measurement at the
  millikelvin sensing condition. LCVD OFF, helium permitted only here.

Mode B and Mode D are mutually exclusive and never run simultaneously;
interlocks IL-01, IL-02, and IL-14 enforce this. The `C_to_D` transition is
itself a BLOCKED gate with explicit thermal, vacuum, contamination, radiation,
RF, optical, vibration, and NV-baseline sub-conditions.

## 4. Implemented deterministic 1D/2D multiphysics scope

`qta_multiphysics/` is a deterministic, non-lumped reduced-order forecast layer
with spatially-resolved backends for thermal transport, moving-boundary laser
absorption, gas/contamination transport, surface coverage, optical deposition,
microwave heating, radiation leakage, vibration transfer, and the coupled
Mode B -> C -> D recovery cycle.

- **1D is the canonical, default path.** A finite-volume moving-boundary heat
  equation `rho Cp(T) dT/dt = d/dz[k(T) dT/dz] + Q_laser + Q_mw + Q_bg` with a
  Kapitza-type backside sink, integrated by method-of-lines with a stiff (BDF) solver.
- **2D axisymmetric is the spatial refinement.** The same physics in `(r,z)` with
  cylindrical `2*pi*r` weighting, a symmetry axis at `r=0`, a cold outer contact,
  and a Gaussian radial beam. It reduces to the 1D result to ~1.4% when radial
  transport is disabled (verified).

Because the femtosecond spot (~5 um) and absorption depth (~1 um) are microscopic
relative to the mm-scale sample, the solvers resolve a near-field micro-domain
embedded in cold bulk — an explicit, documented reduced-order choice. The legacy
lumped model is retained only as a comparator (`lumped_vs_nonlumped_comparison.csv`);
the non-lumped models are the gate authority. Every multiphysics output is
model-only / forecast-only and contributes only CONDITIONAL / BLOCKED /
DERIVED_CHECK gates — none is PASS.

## Repository map and authorities

Single sources of truth are registered in `AUTHORITIES.md` /
`authorities.json` (owners, schema versions v1.0, consumers). The physics
status of every domain, with the B->C->D state-continuity audit, is in
`PHYSICS_INVENTORY.md`; terms in `GLOSSARY.md`. Canonical outputs live at
the repository root (indexed by `final_manifest.json`, byte-gated by the
package checker); historical delivery artifacts are archived under
`attic/delivery_artifacts/` and are not part of the governed project.

## Machine finite-state architecture

The hierarchical operational FSM (states, guarded transitions, interlocks,
and the nominal lifecycle trace) is documented in `MACHINE_FSM.md`; its
tables of record are the byte-gated `machine_fsm_*` canonical outputs.

## 5. 3D transient layer: forecast-only numerical code

The additive 3D transient validation layer is **FORECAST_ONLY_IMPLEMENTED**: an
additive 3D transient coupled multiphysics forecast layer -- a
reduced-resolution, deterministic, energy-conserving 3D transient solver with
mode-state-driven bounded channels (microwave NV-region map, shutter-dependent
radiative front load, DESIGN_SPECIFIED heat-switch path), species/energy
accounting, falsification and convergence reports, and an explicit coupling
ledger -- reduction-checked against the canonical 1D/2D backends. It is
not hardware-validated, not COMSOL, not measured in-system, and introduces no
PASS gates; a heavy/full-resolution pass exists only behind `--heavy-3d` and is
never required by CI or the package consistency check.

## 6. Main forecast outputs and numerical verification

`qta_full_sim.py` regenerates every output from a single source, including:

- `results_gate_table.csv` — the 83-gate decision table (per-gate status,
  evidence class, `measured_in_this_system`, and `can_PASS_now`);
- `monte_carlo_summary.csv`, `monte_carlo_gate_failure_rates.csv`,
  `monte_carlo_parameter_registry.csv` — forecast-only Monte-Carlo behaviour over
  175 categorized parameters;
- `tau_c_sweep.csv`, `interlock_table.csv`, `best_forecast_operating_point.json`.

Numerical self-consistency of the multiphysics layer (mesh convergence, energy
conservation, Kapitza sign, 2D->1D reduction, and inter-domain coupling) is
recorded in `mesh_convergence_summary.csv`, `numerical_stability_summary.csv`,
and `multiphysics_verification_summary.csv`. None of this constitutes hardware
validation.

## 7. Current gate breakdown

| Quantity | Canonical value |
|----------|-----------------|
| total gates | 83 |
| PASS | 0 |
| CONDITIONAL | 47 |
| BLOCKED | 23 |
| UNKNOWN | 2 |
| DERIVED_CHECK | 11 |
| tau_c canonical threshold | 292 us |

All values are generated by `qta_full_sim.py`; `package_consistency_check.py`
validates every packaged file against them.

## 8. Installation and run commands

Python 3 is required. `pdftotext` (Poppler/Xpdf) is optional and only needed for
full PDF-text validation; without it that single check is reported as skipped and
all others still run. See `INSTALL.md` for environment details.

    python qta_full_sim.py
    python package_consistency_check.py

Both should exit 0. The first regenerates all simulation outputs; the second
verifies the canonical state against every packaged file.

## 9. Most important unresolved measurements and first experiments

These are the load-bearing quantities that no in-system measurement yet supports.
They — not the gate logic — are what would falsify or confirm the architecture:

- **tau_c (surface-state coherence time).** Canonical threshold tau_c >= 292 us
  (combined SNR >= 5 with pulse dephasing). Its plausibility on a terminated
  diamond surface at 10 mK is open. (A superseded v3.0 threshold of 27.728 us is
  retained only as NOT_CANONICAL.)
- **C_contr (NV ODMR contrast at 10 mK).** Treated as UNKNOWN and co-equal with
  tau_c; if it is too low, detection fails regardless of tau_c.
- **3He vs 4He separability.** Whether the 3He decoherence signature can be
  separated from phonon, paramagnetic-impurity, and 13C nuclear-spin
  contributions to NV dephasing; matched 4He dosing is the natural control.
- **Residual H2 surface coverage.** Assumed well below 0.1% after bakeout, NEG,
  and cryotrap conditioning; real outgassing may not reach it.
- **Mode D femtosecond-pulse thermal recovery.** Gate D7 assumes
  tau_recovery_D = 42 ns and T_peak_D = 673 mK; diamond phonon transport at base
  temperature may differ.
- **Mode B deposition yield per pulse (gate A9)** and the BLOCKED gate most likely
  to kill the architecture (the package's own non-authoritative guess is
  C_contr-at-10-mK or A9).

`FIRST_VALIDATION_EXPERIMENTS.md` lists candidate first experiments in priority order.

## 10. Repository map

- **Code:** `qta_full_sim.py` (single-source deterministic simulator),
  `qta_multiphysics/` (28-module 1D/2D forecast package),
  `package_consistency_check.py` (consistency checker).
- **Gate / sim outputs:** `results_gate_table.csv`, `monte_carlo_summary.csv`,
  `monte_carlo_gate_failure_rates.csv`, `monte_carlo_parameter_registry.csv`,
  `monte_carlo_sensitivity_rankings.csv`, `tau_c_sweep.csv`, `interlock_table.csv`,
  `engineering_fixes.csv`, `failed_gate_samples.csv`, `kill_gate_ranking.csv`,
  `best_forecast_operating_point.json`.
- **Multiphysics outputs:** `distributed_thermal_profile.csv`,
  `distributed_thermal_metrics.csv`, `distributed_thermal_2d_slices.csv`,
  `surface_coverage_profile.csv`, `surface_coverage_metrics.csv`,
  `surface_coverage_2d_map.csv`, `gas_transport_*.csv`, `optical_absorption_*.csv`,
  `microwave_heating_*.csv`, `radiation_leakage_*.csv`, `vibration_transfer_*.csv`,
  `coupled_mode_recovery_metrics.csv`, `coupled_mode_state_summary.json`,
  `mesh_convergence_summary.csv`, `numerical_stability_summary.csv`,
  `multiphysics_verification_summary.csv`, `lumped_vs_nonlumped_comparison.csv`,
  `fidelity_comparison.csv`, `multiphysics_summary.json`.
- **Engineering data:** `BOM.csv`, `rejected_baseline_BOM.csv`, `risk_register.csv`,
  `interface_map.csv`, `validation_matrix.csv`, `mode_transition_acceptance_tests.csv`,
  `shielding_stack_register.csv`, `radiation_rf_leakage_budget.csv`,
  `optical_line_of_sight_audit.csv`, `cryopanel_memory_model.csv`,
  `assumed_parameters.json`, `parameter_registry.csv`, `measured_parameters.json`,
  `hardware_registry.json`.
- **Provenance / audit:** `source_map.csv`, `source_gap_register.csv`,
  `representative_source_audit.csv`, `bibliography_audit.csv`,
  `source_audit_status.txt` (REPRESENTATIVE_ONLY),
  `superseded_best_operating_point_v3.json` (quarantined, NOT_CANONICAL).
- **Documentation:** `CLAIMS_BOUNDARY.md`, `FIRST_VALIDATION_EXPERIMENTS.md`,
  `INSTALL.md`, `qta_manuscript_v4.tex` / `qta_manuscript_v4.pdf`.
- **Integrity:** `final_manifest.json` lists every canonical file with size and
  SHA-256 (it does not list itself or `manifest_hash.txt`, per `self_hash_policy`);
  `manifest_hash.txt` holds the detached SHA-256 of the manifest.

---

Current physical system: BLOCKED. Post-installation forecast: CONDITIONAL.
Mode D 10 mK sensing requires measured tau_c and measured C_contr; Mode B LCVD
requires measured deposition yield, precursor control, heat dumping, contamination
recovery, and fatigue survival.

## Deep Bayesian experimental design (optional, fail-closed)

`qta_multiphysics/deep_expdesign/` adds an optional simulation-based-inference and
expected-information-gain layer: a deterministic, CPU-only (pure NumPy) conditional
mixture-density posterior plus a learned EIG estimator, trained on simulator-generated data and
numerically validated against the direct nested-Monte-Carlo reference (calibration, EIG error,
ranking agreement, OOD, repeated-seed stability) using conservative thresholds. It is fail-closed:
it controls experiment ordering only as a `VALIDATED_SURROGATE`; otherwise it falls back to the
direct estimator. Run with `python qta_full_sim.py --ci --deep`, or
`from qta_multiphysics.deep_expdesign import run_deep_expdesign_full`.

> QTA includes direct Bayesian experimental design. A deep simulation-based inference and EIG layer may be trained and numerically validated against the direct reference estimator. This does not constitute experimental validation of the physical architecture.

## Scientific stack and adoption ladder (Stage 10)

The tooling around the numerical core is governed like everything else here.
`STACK.md` (with its machine-readable mirror `stack.json`) places every stack
element on one of three rungs — **ADOPTED** (in use and verified in this
repository), **STAGED** (interface and acceptance criteria written and
executable, tool not installed and producing nothing authoritative), or
**DEFERRED** (deliberately not built; contract recorded) — and lists the open
items for each.

Stage 10 adds the adapters in `qta_multiphysics/stack/`: deterministic
ParaView/VTK and OpenUSD export of the solved 3D fields and geometry, a SALib
global-sensitivity cross-check against the canonical one-at-a-time ranking, an
OpenMDAO design-space exploration that refuses to optimise over ASSUMED
parameters, an offline read-only retrieval index over this repository's own
documents (verbatim cited spans, no generation, no network), a staged FEniCSx
acceptance harness, a selective Rust kernel path admitted only on bit-for-bit
parity with its NumPy reference, and a deferred FMI 3.0 interface contract.

All of it is additive: no stack module is imported by the solvers or by
`qta_full_sim.py`, every writer refuses to write into the canonical tree,
`automatic_gate_effect = NONE` throughout, and the scientific gate PASS count
remains zero. Run it with:

    snakemake --cores 1 s10_full

whose final rule re-verifies every entry in `final_manifest.json` and asserts
the canonical tree was untouched. Optional packages are project *extras*
(`uv sync --extra viz --extra uq`); without them the adapters report
`UNAVAILABLE` and defer to the in-repo authority.
