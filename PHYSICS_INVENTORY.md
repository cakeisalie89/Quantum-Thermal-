# QTA Physics Inventory and State-Continuity Audit

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Classification vocabulary (this document): IMPLEMENTED, REDUCED_ORDER,
PARAMETERIZED, PLACEHOLDER, DOCUMENTATION_ONLY, PROPOSED, NOT_IMPLEMENTED,
EXPERIMENTALLY_UNMEASURED. Mapping to the coupling-ledger vocabulary
(`coupling_ledger_3d.ALLOWED_STATUSES`): BOUNDED_FORECAST -> PARAMETERIZED
(bounded scalar), SCAFFOLD -> PLACEHOLDER; ledger IMPLEMENTED/REDUCED_ORDER/
NOT_IMPLEMENTED map one-to-one. EXPERIMENTALLY_UNMEASURED tags parameters,
not models, and coexists with any model class.

For every IMPLEMENTED/REDUCED_ORDER entry: equations, code location,
variables/units, parameter provenance, validity conditions, numerical method,
couplings, outputs, tests, limitations, missing measurements.

## Domain inventory

**Thermal 1D (canonical gate authority)** — IMPLEMENTED. rho*cp(T)*dT/dt =
d/dx(k(T) dT/dx) + q(x,t); Kapitza-radiative sink alpha_K*(T^4-T_b^4) at the
back face. `qta_multiphysics/thermal_1d.py`; T [K], k [W/m/K], cp [J/kg/K],
q [W/m^3]; diamond property models in `material_models.py`
(literature-form, ASSUMED coefficients); FV method-of-lines, implicit BDF
(rtol 1e-6/atol 1e-9); couples laser source and Kapitza boundary; outputs
thermal profile/recovery canonicals; tests `test_multiphysics_core.py`;
limitation: 1-D geometry; missing: any in-system thermometry.

**Thermal 2D axisymmetric** — IMPLEMENTED. Same PDE in (r,z) with axis
regularity; `thermal_2d_axisymmetric.py`; verified 2D->1D reduction ~1.4%;
same method/tests; limitation: axisymmetry.

**Thermal 3D transient (additive validation layer)** — IMPLEMENTED
(forecast-only). Conservative FV on a graded structured grid, 7-point
stencil, BDF; exactly-conservative cell-integrated laser deposition
(erf x/y x Beer-Lambert z; discrete source integral exact to machine
precision); Kapitza back face; guarded bounded auxiliary channels
(`extra_volumetric_W`, `extra_front_flux_W_m2`) inside the energy closure
(composite geometric+linear quadrature; closure ~1e-5, decay phase 2.0e-2
within the 5e-2 tolerance). `mesh_3d.py`, `thermal_3d_transient.py`,
`laser_source_3d.py`, `boundaries_3d.py`, `energy_accounting_3d.py`;
reductions 3D->1D -0.44% / 3D->2D +0.12% (`reduction_checks_3d.py`);
verification suite `verification_3d.py` (all DERIVED_CHECK); meshes CI
10x10x12 / refined 14x14x18 / heavy 16x16x20 (opt-in); tests
`test_thermal_3d.py`, `test_coupled_3d.py`. Not hardware-validated; no
COMSOL; introduces no PASS gates.

**Cryogenics (stage chain and plant)** — REDUCED_ORDER + DOCUMENTATION_ONLY.
Canonical intercept chain 300/77/4/1/0.1/0.01 K with stage-attenuated
radiation (`radiation_paths.py`); dilution base via `FridgeConfig`
(T_fridge_K=0.010, DESIGN); upstream RTB/JT-class plant as a gate/registry
entry; LN2 precool, standalone JT, nuclear demagnetization, gas-gap switch:
NOT_IMPLEMENTED registry entries (`cryo_stack_3d.py`) — no numbers invented.

**Interfaces & heat switches** — REDUCED_ORDER. Kapitza-radiative interface
(alpha_K=50 W/m^2/K^4, ASSUMED form+coefficient) in every thermal solve;
SC heat switch as a state-dependent lumped MC<->4K path
(`heat_switch_3d.py`; G_open=1e-8, G_closed=1e-5 W/K, DESIGN_SPECIFIED gate
A1/validation plan; A1 leak bound 2e-6 W enforced in falsification + FSM);
not resolved inside the 3-D field (stated). Missing: measured OFF-state
conductance, measured Kapitza coefficient.

**Conduction (supports/parasitics)** — PLACEHOLDER. Registry-only support
path (`cryo_stack_3d`); no canonical conductance numbers exist; none
invented.

**Radiation** — REDUCED_ORDER. Stage-chain attenuation with shutter/baffle
state factors (`radiation_paths.py`); coupled into the 3-D front face as a
uniform bounded flux (`sources_3d.radiative_front_flux_W_m2`; closed
1.42e-24 W / open 1.42e-21 W). 3-D view factors NOT_IMPLEMENTED.

**Vacuum / molecular flow / pumping** — REDUCED_ORDER + PARAMETERIZED.
1-D advection-capture-sink chain inlet->sample->cryobaffle->pump
(`gas_transport_1d.py`: v_eff, cryobaffle_capture=0.9, pump_sink_1_s=50/s,
ASSUMED); Knudsen summary for the 3-D geometry (`species_transport_3d.py`);
chamber pre/post-bakeout pressures canonical (1e-10 -> 1e-12 Pa H2).
Missing: measured pumping speeds, RGA clearances (gates E04/IL-05/06).

**Species transport & surfaces** — REDUCED_ORDER. Canonical Langmuir-type
coverage ODE dtheta/dt = s*(F/sites)*(1-theta_tot) - nu*exp(-E/kT)*theta -
(purge+cryotrap)*theta (`surface_coverage.py`, per-species specs ASSUMED);
kinetic flux F = n*vbar/4; canonical purge (purge_1_s=5.0, window 2.0 s)
collapses worst-case theta0=1 to 2.06e-9 (`species_accounting_3d.py`);
closed-form purge-decay test in `test_coupled_3d.py`. C-13 3-D arrival
footprint NOT_IMPLEMENTED (no inlet geometry). Cryopanel loading:
DOCUMENTATION_ONLY assumption ledger (`cryopanel_memory_model.csv` — every
row ASSUMED/BLOCKED/can_PASS_now=NO; measurement list per species).

**Laser heating (fs, LCVD)** — IMPLEMENTED (deposition) + PARAMETERIZED
(absorption). Gaussian x Beer-Lambert deposition, time-averaged or
pulse-envelope temporal modes (stated approximation); absorbed_fraction and
alpha=1e6 1/m ASSUMED; no electron-phonon nonequilibrium model (stated).
Outputs `laser_3d_deposition_summary.json`; damage/graphitization bound
NOT_IMPLEMENTED (no canonical threshold; falsification hook reports value).

**Microwave heating** — REDUCED_ORDER. Canonical delivery-line dissipation
model (`microwave_heating_1d.py`) -> NV-region uniform map in Mode D
(`sources_3d.microwave_nv_region_map_W`, 2.5e-12 W total, inside energy
closure). 3-D field/SAR map NOT_IMPLEMENTED.

**Vibration** — REDUCED_ORDER + PARAMETERIZED. Banded transfer chain with
ASSUMED transfer factors (`vibration_transfer.py`); outputs NV amplitude
4.651e-9 m vs Mode-D threshold 1e-10 m, settling 3.18 s, dissipated power
(BOUNDED_FORECAST -> PARAMETERIZED ledger scalar). Drives IL-08, the FSM
sensing refusal, and the BLOCKED NV eligibility. 3-D modal analysis
NOT_IMPLEMENTED. Missing: measured transfer at the NV region.

**Magnetic background & NV dynamics** — IMPLEMENTED (model) with
EXPERIMENTALLY_UNMEASURED noise parameters. QuTiP ground-state Hamiltonian
H = D Sz^2 + E(Sx^2-Sy^2) + gamma_e(Bx Sx + Bz Sz) (`nv_spin/model.py`;
D=ZFS, B_T applied at theta_B); dephasing via Ornstein-Uhlenbeck bath with
Lorentzian spectrum (`nv_spin/noise.py`; tau_c, amplitude ASSUMED —
canonical tau_c 292 us; 27.728 us SUPERSEDED). Missing: measured tau_c on
F-terminated diamond at 10 mK, measured C_contr.

**NV pulse sequences** — IMPLEMENTED. Ramsey / Hahn echo / XY8 filter
treatments (`nv_spin/noise.py`, `nv_spin/runner.py`; T2*, T2_echo, T2_xy8
outputs); pulse fidelity parameterized (pd^2 factor in the detection chain).
Tests `test_nv_spin.py` (11).

**Helium sensing & SNR** — REDUCED_ORDER forecast. Canonical dose
(1e-6 Pa) -> kinetic flux -> coverage window (1.0 s); detection chain ->
SNR=5.00 at tau_c=292 us definition; Mode-D MC (N=10000, seed 42);
eligibility forecast BLOCKED on vibration (consistent with the CONDITIONAL
gate). All sensing quantities EXPERIMENTALLY_UNMEASURED.

**Energy accounting** — IMPLEMENTED. Per-phase closure over every active
channel (laser + auxiliary volumetric + front flux vs Kapitza sink vs
internal-energy change; `energy_accounting_3d.py`, shared tolerance 5e-2).

**FSM coupling (operational control)** — IMPLEMENTED (control logic for the
theoretical machine). 32 states / 38+8 transitions / 18 interlocks; guards
on model quantities with gate provenance; lifecycle trace ends with the
honest IL-08 sensing refusal; never PASS (`machine_fsm.py`; 11 tests).

**Uncertainty (MC)** — IMPLEMENTED (sampling of ASSUMED distributions).
Mode-D MC N=10000 seed 42; staged MC N=5000; multiphysics mc_samples=30;
3-D MC n=120 seed 12345 (`uncertainty.py`). Results are model-uncertainty
forecasts only.

**Sensitivity** — IMPLEMENTED. Deterministic OAT +10% on four uncertain
inputs at CI mesh (`sensitivity_3d.py`); "model sensitivity, not
experimental importance" stated in every row.

**Inverse design / experimental design** — IMPLEMENTED (forecast). Bayesian
design layer + deep SBI layer (fail-closed TRAINED_NOT_TRUSTED; schemas
v1.0; dataset/model manifests frozen and hashed); EIG-based proposals;
adaptive policy JSON. All proposals PROPOSED experiments, none executed.

**Falsification** — IMPLEMENTED. 11 conditions from existing canonical
bounds (`falsification_3d.py`); damage and residual-coverage bounds
honestly NOT_IMPLEMENTED; falsified_in_model=false is never validation.

## B->C->D state-continuity audit

Carried without artificial reset (code-cited):
- Temperature / stored energy: Mode-C solve starts from the full Mode-B
  final field (`run_mode_sequence_3d`: `T_init=tB.T[:, -1]`); the Mode-D
  hold starts from the Mode-C final field (`T_init=tC.T[:, -1]`); stored
  energy is carried through the temperature field and closed per phase
  (handoff covered by `test_mode_species_3d.py` happy path and the
  sequence tests).
- Contamination / coverage: the Mode-C purge residual (worst case 2.06e-9)
  is carried into the Mode-D C-13 row of `species_accounting_3d.csv`
  (`coverage_start == coverage_end == residual`); He coverage in D starts
  from zero by physics (fresh dose), not by reset.
- Switches / valves / shutters / gates: per-mode device states are a single
  authority (`state_machine_3d`) consumed by both the physics sequence and
  the FSM; FSM guards carry cross-phase facts (`mode_b_complete`,
  clearances, IL-11) so Mode D is unreachable without the completed B->C
  history; refusals never mutate state.
- Vibration: a stateless steady-chain model evaluated identically in every
  phase — no state exists to reset (limitation, not a reset).

Carried since Stage 2 (campaign-continuity layer):
1. Cryopanel loading -- CLOSED: `cryopanel_dynamics_3d.py` evolves per-area
   panel inventory (exact Langmuir capture; ASSUMED sticking from the
   canonical CSV; PLACEHOLDER 1-monolayer capacity) across B->C->D and
   across cycles; outputs `cryopanel_loading_3d.csv`; the static CSV remains
   the assumption/measurement ledger. Capacity and sticking remain
   EXPERIMENTALLY_UNMEASURED.
3. Cumulative energy -- CLOSED: `campaign_state_3d.attach_energy_ledger`
   emits `energy_ledger_cumulative_3d.csv` (identical-cycle sums of the
   existing phase closures; shared tolerance).

Carried since Stage 3:
2. Uncertainty propagation -- CLOSED: `campaign_uncertainty_3d.py`
   propagates a fixed deterministic ensemble (M=120, dedicated seed
   20260717) through the analytic campaign layer with within-member
   parameter persistence across all phases and cycles (no resampling, no
   new PDE solves). Successor limitations (recorded): parameters drawn
   independently (no authoritative cross-parameter correlations exist);
   thermal uncertainty composed as MARGINAL quantiles from the canonical
   3-D MC (not joint); tau_c, C_contr, panel capacity, H2/He panel
   sticking and vibration factors remain fixed/excluded for lack of
   authoritative ranges. The deep layer stays fail-closed by design.

There are no remaining non-carried campaign states.

**Hardware-validation governance (Stage 5)** -- IMPLEMENTED
(read-only record-keeping; no hardware data exists). Three data classes
with quarantine-with-recorded-deficiencies for HARDWARE_UNVERIFIED,
human-only review promotion to dossiers, plan-sourced (currently UNKNOWN)
repetition requirements, custody/calibration/raw-hash validation with the
documented-chain caveat, hash-chained audit logging, and the constant
automatic_gate_effect=NONE. Docs: HARDWARE_GOVERNANCE.md; tests:
tests/test_hardware_governance.py (14).

**Measurement ingestion & comparison (Stage 4)** -- IMPLEMENTED
(read-only, SYNTHETIC-only, forecast context). Fail-closed registry-driven
ingestion (13 quantities; process quantities phase-locked, residual-gas/
vibration/timing diagnostics any-mode -- measuring never activates a
species or touches machine state); corrected residual semantics
(normalized residual only from supplied stddev; p05-p95 bands reported as
band_position / inside_band / band_scaled_residual, never as sigma);
HARDWARE records refused by design pending the validation-plan governance
step. Outputs: `measurement_comparison_3d.json`,
`measurement_comparison_rows.csv`; canonical input
`synthetic_measurements_example.json`; tests:
`tests/test_measurement_ingest.py` (13). No gate, parameter, or canonical-
physics effect exists by construction.

**Campaign uncertainty propagation (Stage 3)** -- IMPLEMENTED
(forecast-only). Deterministic ensemble over the verified-source
distributions only: s_CH4_panel ~ U(0.3,0.8) (CSV verbatim range);
coverage sticking lognormal(center 0.5 canonical, sigma 0.2 declared in
uncertainty.py); purge lognormal(center 5.0 canonical, sigma 0.3
declared). Everything else fixed/excluded machine-readably. Outputs:
`campaign_uncertainty_3d.json`, `campaign_uncertainty_quantiles.csv`;
tests: `tests/test_campaign_uncertainty.py` (10).

**Campaign continuity (Stage 2)** -- IMPLEMENTED (forecast-only).
Three-cycle deterministic campaign (`campaign_state_3d.build_campaign`,
separate API; single-cycle canon untouched) carrying temperature
(identical-cycle approximation, recorded delta ~1e-12 K), coverage
residual, device state, panel loading, cumulative energy; per-cycle IL-08
sensing refusal recurs honestly; IL-05/06 gain derived context with
FORECAST-basis semantics preserved. Outputs: the four campaign canonicals;
tests: `tests/test_campaign_3d.py` (10).
