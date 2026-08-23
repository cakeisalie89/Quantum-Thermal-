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

| level | 3D cells | 3D solve | traced Python peak |
|---|---|---|---|
| L1 (10×10×12) | 1 200 | 7.6 s | 62 MB |
| L2 (18×18×22) | 7 128 | 128.1 s | 432 MB |
| interpolated (22×22×26) | 12 584 | 658.8 s | 800 MB |

Time exponent 1.59 from L1→L2, steepening to **2.88** from L2→interpolated.
The traced-allocation figure scales linearly (exponent 1.08). Extrapolated L3:
1 800–3 100 s. The earlier "still running after ~22 minutes" observation is
exactly that curve, and the time extrapolation proved accurate: **2 624 s
actual.**

### What dominates the solve — measured, at L2

`cProfile` over one L2 3D solve, isolating the call rather than inferring it
from architecture:

```
1957137 function calls in 132.765 seconds
   ncalls  tottime  percall  cumtime  filename:lineno(function)
      443   96.049    0.217   96.049  {built-in method scipy.sparse.linalg._dsolve._superlu.gstrf}
     5796    2.249    0.000    5.427  thermal_3d_transient.py:192(rhs)
     1727    0.814    0.000  131.746  scipy/integrate/_ivp/bdf.py:309(_step_impl)
```

**72.3 % of the L2 3D solve is sparse-direct factorisation** (`gstrf`), 443
calls at 0.217 s each. The right-hand side is 1.7 %. That is measured at L2.

The extension to L3 is an **extrapolation of a measured mechanism**, not a
second measurement: L3 was not profiled, because profiling a 44-minute solve to
re-confirm a share already isolated at L2 is not worth the run. The observed
scaling is consistent with fill-in growth in that factorisation, and no evidence
of convergence failure or memory exhaustion was seen at any level — but the
2.88 exponent itself is a descriptive fit over two points, not a derivation.

No optimisation was applied. The permitted implementation improvements (reusing
immutable geometry, avoiding duplicate assembly, blocking large independent
work) do not touch a direct factorisation.

### Memory — what was and was not measured

The `peak` column above and in the results table is
`tracemalloc.get_traced_memory()[1]`: **traced Python-level allocations only.**
It does not see native allocations inside NumPy, SciPy or SuperLU, and SuperLU
is where most of this solve's memory goes.

Calibrated at L2 in a separate instrumented run:

| metric | L2 value |
|---|---|
| tracemalloc traced peak | 432.5 MB |
| `resource.getrusage(RUSAGE_SELF).ru_maxrss` | 551.8 MB |
| ratio maxrss / traced | **1.28×** |

**No OS-level RSS was captured during the L3 run**, and it is not reconstructed
here. Applying the L2-calibrated 1.28× ratio to L3's traced 1 448 MB would
suggest an OS peak near 1.9 GB, but that is an estimate from a ratio measured at
a different problem size, not a measurement of L3.

What can be said: no memory exhaustion occurred at any level, every solve
returned `ok`, and both the traced figure and the L2 OS figure remained far
below the 16 GB available. That is weaker than "memory was never a constraint",
and it is what the instrumentation supports.

## Result

Command, at commit `0f756c6`:

```
nr=72, nz=96, n3=(26,26,32), transverse="gaussian", t_end=pulse_window_s,
n_eval=13, lateral_adiabatic=True, method=BDF, rtol=1e-06, atol=1e-09
```

| level | 3D cells | 2D cells | T_nv 3D (K) | T_nv 2D (K) | rel_error | 3D solve | traced peak |
|---|---|---|---|---|---|---|---|
| L1 | 1 200 | 768 | 13.730152 | 13.959348 | **-1.6419e-02** | 7.7 s | 62 MB |
| L2 | 7 128 | 3 072 | 13.854448 | 13.966179 | **-8.0001e-03** | 130.0 s | 432 MB |
| L3 | 21 632 | 6 912 | 13.881402 | 13.967766 | **-6.1831e-03** | 2 624.1 s | 1 448 MB |

Total wall 2 780 s, exit 0. Both solvers report `ok` at every level. 3D energy
closure 8.99e-06 relative at L3 (8.86e-06 at L1, 8.95e-06 at L2). The 2D
adiabatic radial export is exactly **0 J** at all three levels, as required.
The `traced peak` column is tracemalloc, not RSS — see the memory note above.

L1 and L2 reproduce the previously reported values to every printed digit, so
this is an extension of the study, not a rewrite of it.

## Convergence — what the three levels do and do not show

```
|L2/L1| = 0.4873      |L3/L2| = 0.7729      monotone decreasing: true
all three levels within tolerance 0.10
```

The discrepancy decreases monotonically and every level sits well inside the
unchanged 0.10 tolerance. The reduction factor is **not stabilising** — 0.487
then 0.773 — so the sequence has not demonstrated a simple asymptotic law over
these three points.

A three-point Aitken transform gives **≈ -5.68e-03 (-0.568 %)**. That is a
**diagnostic extrapolation, not an established continuum limit.** Three levels
are enough to compute the transform; they are not enough to show that the
sequence has entered an asymptotic regime, that the transform's fixed point is
the true limit, or that any particular mechanism accounts for the remainder.

The defensible statement is:

> The finest executed level differs by **0.618 %**, and a three-point
> extrapolation suggests a residual on the order of **0.57 %**. Both are far
> inside the declared 10 % tolerance.

### Evidence chain, separated

**Demonstrated by the executed runs**

- The 3D and 2D domains differ geometrically (Cartesian box vs axisymmetric
  disc, volume ratio 4/π).
- The finest-level discrepancy is -6.1831e-03 (0.618 %).
- The discrepancy decreases monotonically across L1 → L2 → L3.
- All three levels satisfy the 0.10 tolerance.
- `thermal_3d_reduction_check.json` bounds a related lateral-equilibration
  effect at ~6 % in temperature.
- Sparse-direct factorisation is 72.3 % of the L2 3D solve (profiled).

**Suggested, and consistent with the data**

- A non-zero asymptotic discrepancy may persist rather than the residual going
  to zero.
- The domain-geometry difference may contribute materially to it.
- The ~6 % lateral-equilibration bound is consistent with a sub-1 % residual.

**Not established**

- The true continuum-limit discrepancy.
- That -5.68e-03 is that limit.
- That the sequence is in its asymptotic regime.
- Any unique decomposition of the residual into geometry versus remaining
  discretisation error. Consistent-with is not caused-by, and no run performed
  here separates the two.

Running L4 would sharpen this. It is not run: L3 already closed the outstanding
execution gap, and a ~4-hour solve to strengthen prose is not a good trade
absent a reason beyond wording.

## What this does not establish

- Not a hardware or experimental result. No gate changes; PASS remains 0.
- Not a claim that the 3D and 2D models are equivalent — they solve different
  domains, matched only in boundary condition.
- Not a demonstration that the residual is geometric in origin. That is a
  hypothesis consistent with the data, not a measured decomposition.
- Not a replacement for the production comparison, whose 2D lateral boundary is
  a cold radial contact and which is reported separately and never as
  equivalence.

## Reproducing

L3 costs ~44 minutes of single-core CPU and ~1.5 GB. It is deliberately not
wired into CI or the canonical pipeline: the reduction check in
`reduction_checks_3d.py` continues to run at the CI grid, and this document is
the record of the refined levels.
