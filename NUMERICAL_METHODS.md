# QTA Numerical Methods

This file documents the numerical backends in `qta_multiphysics/`. **Numerical
self-consistency does not imply physical validation.** No COMSOL/external solver and no hardware
validation are used or claimed; the additive 3D transient layer is
forecast-only numerical code (reduction-checked against the 1D/2D backends).

## Backends
1. **Lumped state-vector model** (legacy comparator only): ODEs for node
   temperatures/coverages. Retained solely as a fast comparator.
2. **1D finite-volume method-of-lines** thermal backend (`thermal_1d`,
   `grids.py`, `material_models.py`): cell-centred finite-volume mesh
   (`n_cells_1d = 200`), spatial discretization reduced to an ODE system in time,
   integrated with a **stiff BDF** solver via `scipy.integrate.solve_ivp`
   (`method="BDF"`, `rtol = 1e-6`, `atol = 1e-9`). No explicit/CFL-limited time
   stepping is used.
3. **2D axisymmetric refinement** (`thermal_2d`): same physics in `(r,z)` with
   `2*pi*r` control-volume weighting and a regularised symmetry axis at `r=0`.
4. **1D gas/contamination transport** (`gas_transport_1d.py`): upwind advection +
   central diffusion (finite volume), method-of-lines, BDF, for CH4/H2/He3/He4
   (`dn_i/dt = D_eff d^2n/dx^2 - v_eff dn/dx - S_i n + source_i`).
5. **Coupled Mode B -> Mode C -> Mode D** cycle (`coupled_mode_solver.py`):
   process sources ON in Mode B; sources OFF and field re-initialised in Mode C
   recovery; sensing condition in Mode D.

## Governing equation (1D thermal)
`rho*Cp(T) dT/dt = d/dz[k(T) dT/dz] + Q_laser + Q_mw + Q_bg`

## Boundary conditions
- Kapitza-type radiative backside sink: `-k dT/dz|_L = alpha_K (T_L^4 - T_fridge^4)`
  (`alpha_K` ASSUMED).
- Front face carries the deposited/averaged flux.
- Zero-flux / symmetry at `r=0` (2D).

## Source terms
Averaged laser absorption `Q_laser`, microwave dissipation `Q_mw`, background load
`Q_bg`; gas transport adds inlet source, distributed pump sink, and cryobaffle
capture.

## Solver tolerances
`rtol = 1e-6`, `atol = 1e-9`, stiff integrator in `{BDF, Radau, LSODA}` (default BDF).

## Verification (numerical only)
- mesh-convergence refinement,
- source/energy conservation accounting,
- Kapitza-sign correctness,
- axis non-singularity (no `r=0` singularity),
- 2D->1D reduction agreeing to ~1.4%.

These are self-consistency checks, not physical validation. All outputs carry
`measured_in_this_system = false` and remain forecast-only.

## Coupled 3D transient layer

The additive 3D layer is an additive 3D transient coupled multiphysics
forecast layer for QTA, extending spatially resolved thermal, source-term,
mode-sequencing, recovery, accounting, and bounded auxiliary physics forecasts
while preserving zero-PASS validation gating and pre-hardware claim
boundaries. "Coupled" is defined explicitly by `coupling_ledger_3d.json`:
every arrow between physics domains is enumerated with its transferred
quantity, units, direction, update scheme (staggered explicit unless stated),
honesty status (`IMPLEMENTED` / `REDUCED_ORDER` / `BOUNDED_FORECAST` /
`SCAFFOLD` / `NOT_IMPLEMENTED`), and the conservation or test check covering
it. Implemented channels reuse existing canonical values only: the microwave
NV-region map (canonical line-model dissipation, uniform over the NV-layer
cells), the shutter/baffle-dependent radiative front-face load (canonical
stage chain), the DESIGN_SPECIFIED superconducting heat-switch conductances
(gate A1) as a lumped stage-level path, and the canonical coverage/purge
model for species accounting. Energy accounting covers every applied channel
(composite geometric+linear quadrature on the solver's dense-output
interpolant); the falsification report evaluates only existing canonical
bounds and reports `NOT_IMPLEMENTED` where no canonical bound exists;
mesh-refinement and tightened-time-integration convergence checks and a
one-at-a-time model-sensitivity ranking are lightweight numerical
verification only. Nothing in this layer is hardware validation, and the NV
eligibility output is a forecast that remains BLOCKED/CONDITIONAL/UNKNOWN.
