# Evidence closure status

Companion to `AUTHORITIES.md`. That file records **what is governed**; this one
records **what is still unproven, why, and exactly what would close it**.

Nothing here confers authority. Every entry is either executed evidence, a
quantified computational blocker, or an external-authority requirement. A row
moves out of this file only when real evidence arrives through the governed
process — never because a check went green.

MODEL-ONLY / FORECAST-ONLY / NOT_MEASURED_IN_THIS_SYSTEM.

## Merge record

The repository-wide remediation was merged at `0f756c6`, a true merge commit
whose parents are `8512d0b` (base) and `1fc2c7b` (the reviewed head). The merge
tree is `4b4250ba…`, byte-identical to the reviewed tree: nothing was amended,
squashed or slipped in. All 36 commits are preserved.

Post-merge on the merged result: 83 gates, PASS 0, CONDITIONAL 47, BLOCKED 23,
DERIVED_CHECK 11, UNKNOWN 2; `can_PASS_now=NO` and
`measured_in_this_system=false` for all 83; `n_falsified_in_model` 0; deep layer
`TRAINED_NOT_TRUSTED`; `results_gate_table.csv` byte-identical to base;
`package_consistency_check.py` PASS in 215 s with 85.3 s of margin; manifest in
sync at 393 files with 2 detached; 128 focused authority tests pass.

## 1. §6 L3 refinement level

**Status: see `THERMAL_3D_L3.md`** — this section records only what the
profiling established, because the profiling is what changed.

The blocker was never convergence, memory or a defect. It is asymptotic solver
cost. Measured on this runner, holding the scientific definition fixed:

| level | 3D cells | 3D solve | peak RSS |
|---|---|---|---|
| L1 (10×10×12) | 1 200 | 7.6 s | 62 MB |
| L2 (18×18×22) | 7 128 | 128.1 s | 432 MB |
| interpolated (22×22×26) | 12 584 | 658.8 s | 800 MB |

Time exponent 1.59 (L1→L2) steepening to **2.88** (L2→mid) — the fill-in
behaviour expected of a sparse LU factorisation on a 3D 7-point stencil inside
BDF. Memory scales linearly (exponent 1.08), and at 16 GB available it is not
the constraint. Extrapolated L3 (26×26×32, 21 632 cells): **1 800–3 100 s** for
the 3D solve, peak ≈ 1.4 GB.

The earlier "still running after ~22 minutes" observation is fully explained by
that curve. L3 is tractable; it is simply expensive.

**No optimisation was applied to reach it.** Coarsening the grid, loosening a
tolerance, changing the boundary condition or substituting L2 are all forbidden
and none was done.

## 2. Container runtime

**Status: RUNTIME AVAILABLE — BUILD BLOCKED BY REGISTRY EGRESS POLICY.**

This is a change from the previous review's "no usable runtime". The daemon
binaries were present all along and simply were not started. Started here:

- `containerd v2.2.2` — socket created, `ctr namespaces list` responds.
- `dockerd` — client and server both report **29.3.1**; `docker info` succeeds.

The build then fails at image acquisition, not at any instruction in the
Dockerfile:

```
docker pull python:3.12.11-slim-bookworm@sha256:519591d6…657bf7
  → failed to copy: httpReadSeeker: failed open: … Forbidden
```

The manifest resolves (`registry-1.docker.io` answers 401, the expected auth
challenge) but the layer blobs live on `production.cloudfront.docker.com`, which
the environment's egress proxy denies:

```
{"kind":"connect_rejected",
 "detail":"gateway answered 403 to CONNECT (policy denial or upstream failure)",
 "host":"production.cloudfront.docker.com:443"}
```

Reachability probe: `registry-1.docker.io` 401, `ghcr.io` 401,
`production.cloudfront.docker.com` blocked, `quay.io` blocked.

**The base image was not substituted.** Building from a different base would
test a different artifact than the Dockerfile declares, and reporting that as
container verification would be false. Static review stands; runtime execution
does not.

**What closes it:** allow `production.cloudfront.docker.com` (or any Docker Hub
blob CDN) through the egress policy, or run the build where Docker Hub is
reachable. No repository change is required — the Dockerfile is not the blocker.

## 3. Signing / SLSA

**Status: PENDING.** Unchanged, and deliberately so. No signature was
simulated, and no local fixture was presented as a hosted signing result.

Offline release verification passes: SBOM matches `uv.lock` (89 packages),
provenance binds the release-zip digest, the trust policy contains no wildcards,
and the gate table inside the zip carries PASS = 0. **None of that is signature
verification.** It proves internal consistency of the bundle, not its origin.

**What closes it:** a genuine hosted run of `release.yml` on a tag, producing a
Sigstore bundle whose certificate identity matches the expected issuer,
repository, workflow and ref, over the actual release artifact digest, with
Rekor inclusion. Verification must be independent: a cryptographically valid
signature over the wrong artifact, commit, workflow or repository is a failure,
not a pass.

## 4. Material-property floors

**Status: NO AUTHORITATIVE REPLACEMENT IN REPOSITORY.** The floors stand.

| | `CP_FLOOR_J_KG_K` | `K_FLOOR_W_M_K` |
|---|---|---|
| source | `qta_multiphysics/material_models.py` | same |
| value | 1e-6 J/kg/K | 1e-3 W/m/K |
| guards | division by ρ·Cp in the heat equation | vanishing face conductance at ultra-low T |
| crossover | 0.407378 K | 0.793701 K |
| below crossover | `EFFECTIVE_MATERIAL_PROPERTY_ASSUMPTION` | same |
| above crossover | `NUMERICAL_REGULARIZATION` | same |

`dominant_in_canonical_regime: true`. Direct consumers are `diamond_cp` and
`diamond_k`, which are called by `thermal_1d`, `thermal_2d_axisymmetric`,
`thermal_3d_transient`, `materials_3d` and `verification`.

Two figures that must not be conflated:

- **Constitutive substitution.** Floored α = 0.2849 m²/s against raw
  0.0385 m²/s — **7.396×**, flat from 10 mK to 100 mK because both k and Cp go
  as T³ — and exactly 1× at 1 K, where the floors stop biting.
- **Sensitivity.** Under a ±1-decade sweep of the floor values at the 50 mK
  readiness probe, α spans 0.002849 → 28.49 m²/s, a factor of **10⁴**. α tracks
  `k_floor / cp_floor` while both dominate, so a decade of uncertainty in either
  moves the result by a decade. That is constitutive sensitivity, not numerical.

Affected predictions, named by the artifact itself: Mode-C recool time and the
50 mK readiness threshold (~8× below both crossovers), and the Mode-A/Mode-D
10 mK stage temperatures.

**Evidence required to replace them** — any one of these is an authority
upgrade, and a plausible external number is not:

| Replacement route | What it must supply | Authority class |
|---|---|---|
| Measured Cp(T) for this diamond grade | specific heat, 10 mK – 1 K, with uncertainty | component-specific measurement |
| Measured k(T) for this geometry | thermal conductivity over the same range, including boundary-scattering regime | component-specific measurement |
| Peer-reviewed constitutive law | validated below 1 K for CVD diamond of this isotopic purity and defect density | peer-reviewed constitutive law |
| Manufacturer data | controlled, traceable, with stated measurement conditions | manufacturer-controlled documentation |
| Owner design input | an explicitly ASSUMED replacement, declared as such | owner design decision |

Whichever arrives, the floors are conservative in one direction only: they
*raise* diffusivity, so recovery times are optimistic and hotspot persistence is
understated. Any replacement must be checked for that sign.

## 5. CH₄ / H₂ gas temperature

**Status: UNRESOLVED_REQUIRES_OWNER_AUTHORITY — and mechanically quarantined.**

Preserved invariants, all asserted by tests: unresolved species carry
`T_eval_K: None` and `Kn: None` (absent, not defaulted); the summary's return
value may not be bound in production code; no gate-producing module may import
it; multiplying the diagnostic's `T_EVAL_K` by 1000 leaves D9's Knudsen number
bit-identical.

| species | mode | P (Pa) | d (m) | Kn across the declared span | regimes |
|---|---|---|---|---|---|
| C13_CH4 | B | 1.0e-4 | 3.8e-10 | 2.152e-01 (10 mK) → 6.456e+03 (300 K) | TRANSITIONAL, MOLECULAR_FLOW |
| H2 | all | 1.0e-10 | 2.9e-10 | 3.695e+05 → 1.109e+10 | MOLECULAR_FLOW only |
| He3/He4 | D | 1.0e-6 | 2.6e-10 | resolved at the sensing-stage temperature | MOLECULAR_FLOW |

H₂ is temperature-insensitive: one regime across six decades of temperature, so
its classification is robust despite the unresolved population temperature.
CH₄ is not: it crosses a transport-regime boundary between 0.1 K and 1 K, so its
classification would follow entirely from the assumption.

**The prior question is whether a scalar gas temperature is meaningful here at
all.** At Kn ≫ 1 the gas is in molecular flow: molecules cross the chamber
without colliding with each other, so there is no mechanism to establish a
Maxwellian at a single temperature. What a molecule "has" is the accommodation
history of the last surface it struck, and those surfaces span 300 K feedthroughs
to a 10 mK stage. The physically honest object is a **distribution over source
populations**, not one T.

| Quantity required | Physical definition | Evidence / model required | Consumers | Authority consequence |
|---|---|---|---|---|
| Wall temperature map | surface temperature of every desorbing/reflecting surface | installation geometry + stage thermometry | impingement flux, coverage | replaces a scalar with a source spectrum |
| Thermal accommodation coefficient α(T, surface, species) | fraction of the wall–gas energy difference exchanged per strike | literature for the actual surface finish, or measurement | whether reflected molecules carry wall or incident energy | decides whether "gas temperature" is even definable |
| Source apportionment | fraction of flux from each wall population | conductance/geometry model | the weighting of the distribution | required before any average is meaningful |
| Collision frequency | intermolecular collision rate at the modelled pressure | already derivable from P, d, T | confirms whether equilibration can occur at all | at Kn ≫ 1 it cannot |

**Recommended disposition:** for H₂ the regime conclusion stands without a
temperature and the `None` should remain. For CH₄ the correct output is not a
chosen temperature but an explicit statement that no single equilibrium gas
temperature is claimed, with the regime reported parameterised — which is what
the artifact already does. Assigning a scalar T would be a modelling decision
requiring owner authority, not a defect fix.

## 6. Deep experimental-design layer

**Status: TRAINED_NOT_TRUSTED. Not close.**

Six of seven trust thresholds fail, several by three to four orders of
magnitude, against a direct nested-Monte-Carlo reference:

| metric | required | measured | passed |
|---|---|---|---|
| max abs EIG error (nats) | ≤ 0.15 | 1588.42 | no |
| mean abs EIG error (nats) | ≤ 0.05 | 598.15 | no |
| normalised EIG error | ≤ 1.0 | 1292.17 | no |
| repeat-seed EIG std (nats) | ≤ 0.02 | 57.36 | no |
| Spearman rank corr | ≥ 0.95 | 0.486 | no |
| top-1 agreement | = 1.0 | 0.0 | no |
| top-3 agreement | ≥ 0.66 | 0.667 | yes |
| max calibration error | ≤ 0.1 | 0.394 | no |

`controls_experiment_ordering: false`, `fallback: direct_monte_carlo`, opt-in
behind `--deep`, absent from the canonical pipeline.

Isolation is verified by test: no deep module names a canonical authority
artifact, and the layer cannot write parameters, move thresholds, promote gate
states, change measured flags or certify its own output.

**Trust promotion would require, and tests cannot supply:** thresholds actually
met against the direct reference; held-out evaluation on designs not in the
training distribution; simulation-based calibration with documented coverage;
adversarial/OOD evaluation with a fail-closed trigger that is itself tested; and
an independent verifier that remains outside the model's authority. Passing the
isolation tests is evidence of containment, never of correctness.

## 7. Remaining-authority matrix

| Quantity | State | Current evidence | Missing evidence | Consumer | Affected | Software-closable? |
|---|---|---|---|---|---|---|
| §6 L3 refinement | see `THERMAL_3D_L3.md` | L1, L2 executed | compute time only | reduction check | §6 convergence claim | **yes — compute** |
| Container runtime | build blocked | daemon 29.3.1 running | Docker Hub blob egress | release/reproducibility | container claim | no — infrastructure |
| Signing | PENDING | offline bundle verified | hosted Sigstore run + identity match | release | provenance claim | no — infrastructure |
| Cp(T) below 0.407 K | floored | none | measurement or validated law | all thermal solvers | Mode-C recool, 50 mK readiness | no — measurement |
| k(T) below 0.794 K | floored | none | measurement or validated law | all thermal solvers | same | no — measurement |
| CH₄ gas temperature | unresolved | span parameterisation | accommodation + wall map, or a decision not to claim one | diagnostic only | none (quarantined) | no — owner decision |
| H₂ gas temperature | unresolved | robust across span | none needed for the regime conclusion | diagnostic only | none | n/a |
| Deep-layer trust | not trusted | thresholds measured and failing | independent validation programme | none (opt-in) | none | no — validation |
| RGA / P_H2 measurement | not performed | assumption only | bakeout + NEG + RGA campaign | B4, D10a/b, E04 | Mode D interlock | no — physical |

**Where further coding stops helping:** everything below the first row. L3 is
the only remaining item that additional computation can close by itself.
