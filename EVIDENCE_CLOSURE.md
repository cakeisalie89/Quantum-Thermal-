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

**Status: COMPLETED — see `THERMAL_3D_L3.md`.** Executed as declared: rel_error
**-6.1831e-03 (0.618 %)** at 26×26×32 / 72×96, monotone decreasing across three
levels, all inside the unchanged 0.10 tolerance, 2 624 s and a traced-allocation
peak of 1 448 MB (tracemalloc, not RSS).

A three-point Aitken transform gives ≈ **-0.568 %**. That is a **diagnostic
extrapolation, not an established continuum limit** — the reduction factor moves
0.487 → 0.773, so the sequence has not demonstrated an asymptotic law over these
points. The box/disc geometry difference is **consistent with** a persistent
non-zero residual and is not shown to be its unique cause.

What follows — this section records only what the
profiling established, because the profiling is what changed.

The blocker was never convergence, memory or a defect. It is asymptotic solver
cost. Measured on this runner, holding the scientific definition fixed:

| level | 3D cells | 3D solve | traced Python peak |
|---|---|---|---|
| L1 (10×10×12) | 1 200 | 7.6 s | 62 MB |
| L2 (18×18×22) | 7 128 | 128.1 s | 432 MB |
| interpolated (22×22×26) | 12 584 | 658.8 s | 800 MB |

Time exponent 1.59 (L1→L2) steepening to **2.88** (L2→mid). Profiling one L2 3D
solve isolates the cause directly: **72.3 % of it is `_superlu.gstrf`**, the
sparse-direct factorisation, 443 calls at 0.217 s each, with the right-hand side
at 1.7 %. The extension of that attribution to L3 is an extrapolation of a
measured mechanism, not a second measurement — L3 was not profiled.

The memory column is `tracemalloc` (traced Python allocations), not OS RSS; it
does not see SuperLU's native allocations. Calibrated at L2: traced 432.5 MB vs
`ru_maxrss` 551.8 MB, a ratio of 1.28×. No OS-level RSS was captured for L3 and
none is reconstructed. What holds: no memory exhaustion at any level, every
solve `ok`, and every measured indicator far below the 16 GB available.

Extrapolated L3 (26×26×32, 21 632 cells): **1 800–3 100 s**; actual 2 624 s. The
earlier "still running after ~22 minutes" observation is fully explained by that
curve. L3 is tractable; it is simply expensive.

**No optimisation was applied to reach it.** Coarsening the grid, loosening a
tolerance, changing the boundary condition or substituting L2 are all forbidden
and none was done.

## 2. Container runtime

**Status: `STATIC_VERIFIED`.** Six levels are tracked separately and must not
be collapsed into a boolean. The same vocabulary appears in
`container_verification.md`; these two documents, `STACK.md` and `stack.json`
must agree.

| level | value | meaning |
|---|---|---|
| `CONTAINER_DEFINITION` | `STATIC_VERIFIED` | Dockerfile, dependency and security review only |
| `BASE_DIGEST` | `RESOLVED_AND_PINNED` | `sha256:519591d6…657bf7`, pinned in the Dockerfile |
| `LOCAL_RUNTIME` | `AVAILABLE` | containerd + dockerd running, `docker info` succeeds |
| `LOCAL_BUILD` | `ATTEMPTED_BUT_BLOCKED_BY_BLOB_EGRESS` | manifests resolve; layer blobs 403 on CONNECT |
| `RUNTIME_BUILT` | `NO` | the exact declared image has never built or launched |
| `RUNTIME_SCIENTIFICALLY_REPRODUCED` | `NO` | governed verification never executed inside it |

**Adoption status corrected to `STAGED`.** `STACK.md` defines ADOPTED as "in use,
exercised by CI or the workflow, and its behaviour is verified in this
repository." The container satisfies **none** of those three clauses, so
"Reproducible container | ADOPTED" was false by the repository's own vocabulary
— the same failure mode already caught for Selective Rust, where bare ADOPTED
read as an active backend. `stack.json`, `STACK.md`, the registry model and the
Stage-10 tests now all carry `STAGED`, and ADOPTED was **not** redefined to keep
the row green.

Locally: **runtime available, build blocked by registry egress policy.**

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

**A second route exists, and it is now wired.** The local egress policy is not
the only possible execution environment. `.github/workflows/container-verify.yml`
(added in this pass, `workflow_dispatch` only, `contents: read` and nothing
else — no `id-token`, no write of any kind) attempts the declared build on a
GitHub-hosted runner, which is a different network. It changes nothing about the
container: same Dockerfile, same digest-pinned base read from the Dockerfile
rather than restated, same `USER qta`, same entrypoint. It checks in order that
a Docker daemon exists, the pinned base pulls, the image builds, it runs as
`qta`, `sys.prefix` is `/opt/venv` so no host virtualenv is involved, and then
runs the governed in-container verification.

**Its availability claim is not verified.** GitHub documents a Docker daemon on
`ubuntu-latest`, but this repository has not observed one; the workflow's first
step fails loudly if none is present. The workflow is the *means* of settling
the question, not an assertion of the answer, and it cannot run until it reaches
the default branch because it is `workflow_dispatch`.

If it succeeds through the build and run steps, container evidence moves to
`RUNTIME_BUILT`; if the governed verification inside the container also passes
and its outputs match, `RUNTIME_SCIENTIFICALLY_REPRODUCED`. Until then the state
stays `STATIC_VERIFIED`.

**What else closes it:** allowing `production.cloudfront.docker.com` (or any
Docker Hub blob CDN) through the local egress policy. No repository change is
required for that route — the Dockerfile is not the blocker.

## 3. Signing / SLSA

**Status: PENDING.** Unchanged, and deliberately so. No signature was
simulated, and no local fixture was presented as a hosted signing result.

Offline release verification passes: SBOM matches `uv.lock` (89 packages),
provenance binds the release-zip digest, the trust policy contains no wildcards,
and the gate table inside the zip carries PASS = 0. **None of that is signature
verification.** It proves internal consistency of the bundle, not its origin.

The route itself is sound and now sits on the default branch. `release.yml`
grants `contents: read` plus `id-token: write` (keyless OIDC) and nothing else;
it builds the release zip deterministically from the git index, verifies that
exact file offline, signs **that same file** with `python -m sigstore sign`, and
then re-verifies the same file against the same bundle directory with
`verify_release.py --online`. The three-directory bug that once meant the signed
artifact was never the verified one is fixed.

**A later adversarial pass found that the trust path was not yet coherent, and
rebuilt it.** Four policy fields — `source_repository`, `workflow_path`,
`pinned_revision`, `trusted_builders` — were present in the policy and read by
**nothing** (`grep -c` in `verify_release.py` returned 0 for each). The policy
also had two hand-maintained definitions that could diverge, the CI builder id
was the placeholder `PENDING-hosted-runner` which *contains* the substring the
SLSA guard tested for, and the "every PENDING" rule was two named-field checks.

What replaced it:

| element | state |
|---|---|
| canonical policy | `QTA_stage9_release_verification/release_trust_policy.json`, schema `2.0.0` |
| loader/validator | `release_trust.py` — the only reader; `trust_policy()` is now a loader with no values of its own |
| bundled copy | written with the canonical serializer and compared **byte-for-byte** by the verifier |
| unresolved scan | structural over every leaf, case- and whitespace-insensitive, including nested `trusted_builders` |
| repository / workflow / ref | exact equality between the external policy and the **signed** release binding; also proven by the certificate, since Sigstore checks the SAN against the identity derived from those same fields. Unsigned provenance is cross-checked only |
| builder | derived again from the authenticated binding, then exact list membership; substring, prefix, suffix and case variants all fail. Never read from unsigned provenance |
| revision | `reviewed_payload_sha256` recomputed from the authenticated archive (no Git needed); plus `git rev-parse` cross-checked against tag target and Actions context and gated by `release_revision_gate.py` in the workflow |
| SLSA | any non-`NONE` value in index or provenance is a failure |

**The self-reference problem is solved rather than deferred.** A commit cannot
contain its own SHA, so `pinned_revision` names the *reviewed* revision `C`; the
released commit `A` is its descendant carrying the authorization record, and the
gate requires `C` to be an ancestor of `A` with the `C..A` diff touching **only**
the policy file. Verified by constructing the sequence in a real scratch
repository: the honest order passes, and an authorization commit that also
smuggles an unrelated file is refused by name.

**There is no bootstrap release tag.** The earlier plan — tag A, observe the
identity, pin it, release under tag B — was self-contradictory, because the ref
is part of the identity. The exact signer identity is instead derived from
(repository, workflow, tag name), all of which the owner fixes in advance, and
`validate_policy` refuses a policy whose `signer_identity` disagrees with its own
components. If a prediction is ever wrong the run fails closed; it is never TOFU.

The metadata blocker remains closed by `finalize_release_signing.py`, which is
implemented and wired between signing and online verification. Signature
existence is still not authorization.

**A further review found the trust root itself was missing.** The verifier
compared the bundled policy against a repository-local path and, when that path
did not exist — the independent-consumer case — silently skipped the check.
Unsigned `provenance.intoto.json` was also being consulted for repository,
workflow, ref, builder and revision authorization, and policy-derived strings
were reported as though they were observed certificate values.

Corrected: `--online` now requires an external `--trusted-policy` and fails
closed without it; a signed `release_binding.json` inside the archive carries
the release facts so they come from authenticated bytes; provenance is
reclassified `UNTRUSTED_AUXILIARY_METADATA` and cross-checked only; and the
certificate check is named for what it is — Sigstore verifying the presented
certificate against the identity derived from the *external* policy.

**Signing status: PENDING.** `bootstrap_state` is `UNINITIALIZED`. No pin filled,
no tag cut, no signature produced. 11 mutations of the trust boundary were each
caught by 1–5 tests; the two that initially survived (deleting the ancestry check,
and relaxing repository equality to substring) exposed real coverage gaps that
were closed with tests before re-running.

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

### The directional claim was tested, and it failed

An earlier version of this document asserted that the floors are "conservative
in one direction only: they raise diffusivity, so recovery times are optimistic
and hotspot persistence understated". That inference was from α alone. It was
checked against the coupled solver and **it does not hold.**

Diagnostic run — production floors versus the raw `_cp_raw`/`_k_raw`
extrapolation the repository already ships, floors lowered only inside the
diagnostic process, production values untouched and restored afterwards.
**NON-AUTHORITATIVE: the raw law carries no authority and was not promoted.**

| configuration | α at the 50 mK probe | NV-layer peak over the recovery window | solver |
|---|---|---|---|
| floored (production) | 0.2849 m²/s | **0.013057 K** | ok |
| raw law (diagnostic) | 0.0385 m²/s | **0.010000 K** | ok |

α is 7.396× higher with the floors, exactly as the constitutive comparison says.
But the coupled NV-layer response moves the **opposite** way: floored − raw =
**+3.06e-03 K**, i.e. the floored (production) configuration ends *hotter*, not
cooler. The naive "faster diffusion ⇒ optimistic recovery" chain is therefore
not supported by the full nonlinear boundary-coupled solve.

The mechanism is visible in the numbers: the floors raise Cp by 6.8e4× at 10 mK
as well as k by 5.0e5×, and α is only their ratio. In the raw-law run Cp
underflows to ~1.5e-11 J/kg/K, the material has essentially no heat capacity,
and the NV layer is pinned to T_fridge at 0.010000 K exactly. That is a
degenerate numerical limit — which is why the Cp floor exists as a guard in the
first place — so the raw-law run is not a physically better alternative either.

**What is established:** relative to the current raw T³ extrapolation, the
paired floors increase thermal diffusivity by 7.396× in the floor-dominated
regime, and the sign of at least one coupled recovery observable is opposite to
what that alone would predict.

**What is not established:** the sign or magnitude of the full-system response
of Mode-C recool time, 50 mK crossing time, hotspot maximum or hotspot
persistence to a *physically meaningful* replacement constitutive law. The
raw-law limit is degenerate and does not stand in for one. Any replacement must
be re-tested against the coupled observables directly, not argued from α.

"Conservative" is not asserted of these floors anywhere in this document. The
word appears above only in the quotation of the claim being withdrawn: no
direction has been demonstrated to be conserved, so there is nothing to call
conservative.

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

**The prior question is what a scalar gas temperature would mean here.** A
temperature in the equilibrium sense describes a Maxwellian, and a Maxwellian is
established by intermolecular collisions. Counting them, at the modelled
pressures and L_char = 0.01 m:

| species | T | λ (m) | Kn | collisions per wall transit |
|---|---|---|---|---|
| C13_CH4 | 10 mK | 2.152e-03 | 2.152e-01 | **4.6** |
| C13_CH4 | 1 K | 2.152e-01 | 2.152e+01 | 4.6e-02 |
| C13_CH4 | 300 K | 6.456e+01 | 6.456e+03 | 1.5e-04 |
| H2 | 10 mK | 3.695e+03 | 3.695e+05 | 2.7e-06 |
| H2 | 1 K | 3.695e+05 | 3.695e+07 | 2.7e-08 |
| H2 | 300 K | 1.109e+08 | 1.109e+10 | 9.0e-11 |

**H₂.** Between a million and a hundred billion wall transits per intermolecular
collision across the whole span. The evidence strongly argues against assuming a
single collisionally equilibrated Maxwellian: there is no gas-phase mechanism to
establish one, and the population's velocity/energy distribution may depend on
wall temperatures, source apportionment and accommodation history.

That is **not** a claim that no temperature-like quantity can be defined. A
kinetic temperature can be defined from moments of a non-equilibrium velocity
distribution even when that distribution is not Maxwellian; whether such a
scalar would be useful or authoritative here is a separate question this
repository has not answered. What `T_eval_K: None` records is therefore:

> **no authoritative scalar gas temperature is assigned by this model.**

It does not record that temperature is mathematically undefinable, that no
kinetic-temperature moment exists, that the gas carries no energy distribution,
or that every molecule necessarily retains exactly its last wall temperature.

The robust result stands independently of any of that: H₂ classifies
MOLECULAR_FLOW at **every** temperature in the evaluated span, so its regime
conclusion does not depend on resolving the population temperature at all.

**CH₄.** At 10 mK it reaches ~4.6 intermolecular collisions per characteristic
wall transit — substantially more collisional than H₂ and capable of more
gas-phase redistribution — but 4.6 collisions per transit does not by itself
demonstrate an established Maxwellian equilibrium. It is transitional, and that
is the point: CH₄ is far more sensitive to the unresolved population model than
H₂, and it is the one species that crosses a transport-regime boundary within
the declared span (between 0.1 K and 1 K). Its classification would follow from
whatever population model is assumed, which is why no scalar is chosen.

A source-population or distribution-based treatment is the plausible future
architecture, but that is a modelling requirement to be implemented and
evidenced, not a conclusion this pass reaches.

| Quantity required | Physical definition | Evidence / model required | Consumers | Authority consequence |
|---|---|---|---|---|
| Wall temperature map | surface temperature of every desorbing/reflecting surface | installation geometry + stage thermometry | impingement flux, coverage | replaces a scalar with a source spectrum |
| Thermal accommodation coefficient α(T, surface, species) | fraction of the wall–gas energy difference exchanged per strike | literature for the actual surface finish, or measurement | whether reflected molecules carry wall or incident energy | decides whether "gas temperature" is even definable |
| Source apportionment | fraction of flux from each wall population | conductance/geometry model | the weighting of the distribution | required before any average is meaningful |
| Collision frequency | intermolecular collision rate at the modelled pressure | already derivable from P, d, T | confirms whether equilibration can occur at all | at Kn ≫ 1 it cannot |

**Recommended disposition:** for H₂ the regime conclusion holds without a
temperature, so `None` should remain. For CH₄ the output should continue to be
an explicit statement that no single equilibrium gas temperature is claimed,
with the regime reported parameterised — which is what the artifact already
does. Assigning a scalar T is a modelling decision requiring owner authority,
not a defect fix.

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
| §6 L3 refinement | **CLOSED** | L1, L2, L3 executed | none | reduction check | §6 convergence claim | done |
| Container runtime | `STATIC_VERIFIED` | daemon 29.3.1 running locally | a runner that can pull the pinned base | release/reproducibility | container claim | partly — hosted workflow added, unproven |
| Signing | PENDING | offline bundle verified; metadata blocker closed by finalize_release_signing.py; bootstrap corrected after review | discovery dispatch, then bootstrap tag for the real SAN, then owner pin, then a new tag | release | provenance claim | no — owner + infrastructure |
| Cp(T) below 0.407 K | floored | none | measurement or validated law | all thermal solvers | Mode-C recool, 50 mK readiness | no — measurement |
| k(T) below 0.794 K | floored | none | measurement or validated law | all thermal solvers | same | no — measurement |
| CH₄ gas temperature | unresolved | span parameterisation | accommodation + wall map, or a decision not to claim one | diagnostic only | none (quarantined) | no — owner decision |
| H₂ gas temperature | unresolved | robust across span | none needed for the regime conclusion | diagnostic only | none | n/a |
| Deep-layer trust | not trusted | thresholds measured and failing | independent validation programme | none (opt-in) | none | no — validation |
| RGA / P_H2 measurement | not performed | assumption only | bakeout + NEG + RGA campaign | B4, D10a/b, E04 | Mode D interlock | no — physical |

**Where further coding stops helping:** every remaining row. L3 was the only
item additional computation could close by itself, and it is now closed. Nothing
else in this table yields to more code.
