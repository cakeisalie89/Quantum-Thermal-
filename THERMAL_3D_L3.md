# §6 L3 refinement level — EXECUTED

**Status: COMPLETED.** Previously `NOT EXECUTED`. Nothing about the scientific
definition changed to get here: the grid, tolerances, boundary conditions,
geometry, materials and target quantity are exactly as declared, and no level
was substituted or relabelled.

MODEL-ONLY / FORECAST-ONLY / NOT_MEASURED_IN_THIS_SYSTEM. This is a numerical
convergence study of the model against itself; it is not validation of the
physical system.

## Why it had not run

Not convergence failure, not a defect, not memory. Asymptotic solver cost.
Measured on this runner before attempting L3:

| level | 3D cells | 3D solve | peak (tracemalloc) |
|---|---|---|---|
| L1 (10×10×12) | 1 200 | 7.6 s | 62 MB |
| L2 (18×18×22) | 7 128 | 128.1 s | 432 MB |
| interpolated (22×22×26) | 12 584 | 658.8 s | 800 MB |

Time exponent 1.59 from L1→L2, steepening to **2.88** from L2→interpolated —
the fill-in behaviour of a sparse LU factorisation on a 3D seven-point stencil
inside BDF. Memory scales linearly (exponent 1.08). Extrapolated L3:
1 800–3 100 s, peak ≈ 1.4 GB.

The earlier "still running after ~22 minutes" observation is exactly that curve.
The extrapolation proved accurate: **2 624 s and 1 448 MB actual.**

No optimisation was applied. The dominant cost is the factorisation itself, and
the permitted implementation improvements (reusing immutable geometry, avoiding
duplicate assembly, blocking large independent work) do not touch it.

## Result

Command, at commit `0f756c6`:

```
nr=72, nz=96, n3=(26,26,32), transverse="gaussian", t_end=pulse_window_s,
n_eval=13, lateral_adiabatic=True, method=BDF, rtol=1e-06, atol=1e-09
```

| level | 3D cells | 2D cells | T_nv 3D (K) | T_nv 2D (K) | rel_error | 3D solve | peak |
|---|---|---|---|---|---|---|---|
| L1 | 1 200 | 768 | 13.730152 | 13.959348 | **-1.6419e-02** | 7.7 s | 62 MB |
| L2 | 7 128 | 3 072 | 13.854448 | 13.966179 | **-8.0001e-03** | 130.0 s | 432 MB |
| L3 | 21 632 | 6 912 | 13.881402 | 13.967766 | **-6.1831e-03** | 2 624.1 s | 1 448 MB |

Total wall 2 780 s, exit 0. Both solvers report `ok` at every level. 3D energy
closure 8.99e-06 relative at L3 (8.86e-06 at L1, 8.95e-06 at L2). The 2D
adiabatic radial export is exactly **0 J** at all three levels, as required.

L1 and L2 reproduce the previously reported values to every printed digit, so
this is an extension of the study, not a rewrite of it.

## Convergence — read honestly

```
|L2/L1| = 0.4873      |L3/L2| = 0.7729      monotone decreasing: true
all three levels within tolerance 0.10
```

The residual decreases monotonically, but **the rate of decrease is slowing**:
0.487 then 0.773. That is not a clean power-law march to zero, and it should not
be reported as one.

Aitken extrapolation over the three levels puts the limit at **-5.68e-03
(-0.568 %)** — the sequence is converging toward a small non-zero residual, not
toward zero.

That is the expected answer, and the artifact already names the reason: the 3D
solve is a Cartesian box and the 2D solve an axisymmetric disc, a volume ratio
of 4/π. `thermal_3d_reduction_check.json` bounds the effect of full lateral
equilibration at ~6 % in temperature. An extrapolated residual of 0.57 % sits an
order of magnitude inside that bound, and 18× inside the 0.10 tolerance.

So the correct statement is: **the matched-boundary 3D and 2D models agree to
about 0.6 % in the continuum limit, with the remainder attributable to the
declared geometric difference between box and disc** — not "the models agree
exactly and the residual is discretization error alone". The refinement trend
distinguishes discretization-limited agreement from disagreement, which is what
the check exists to do; it does not claim the two geometries are the same
problem.

## What this does not establish

- Not a hardware or experimental result. No gate changes; PASS remains 0.
- Not a claim that the 3D and 2D models are equivalent — they solve different
  domains, matched only in boundary condition.
- Not a replacement for the production comparison, whose 2D lateral boundary is
  a cold radial contact and which is reported separately and never as
  equivalence.

## Reproducing

L3 costs ~44 minutes of single-core CPU and ~1.5 GB. It is deliberately not
wired into CI or the canonical pipeline: the reduction check in
`reduction_checks_3d.py` continues to run at the CI grid, and this document is
the record of the refined levels.
