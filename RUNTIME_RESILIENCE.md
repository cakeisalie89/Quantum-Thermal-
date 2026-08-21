# Runtime Resilience (Stage 7.5)

MODEL-ONLY / FORECAST-ONLY. Additive tooling; no scientific code, setting,
or output changed (88/88 governed outputs byte-identical, proven).

## Why
Under sandbox load troughs the four-stage canonical run needs more wall
time (measured: core 93.0 s + layers 96.8 s + three_d 120.1 s = 309.9 s)
than the ~282 s external per-command ceiling. Profiling (cProfile, scratch
copies): core = 143 solve_ivp / 102,655 BDF steps (72x thermal-1D under
the Monte-Carlo driver + 6x thermal-2D) — 118/120 s; three_d = 16
canonical 3-D transients (convergence study 56 s, sensitivity 33 s) with
5,712 SuperLU factorizations (63 s) inside BDF's reuse heuristic; layers
is QuTiP-mesolve-bound. Every hotspot is the science at canonical
tolerances — no optimization is safe under byte-identity, so none was
made.

## Staged driver — `qta_sim_stages.py`
Executes the identical four calls, same arguments, same seed, one stage
per invocation: `core` (qta_full_sim.main + engineering fixes), `layers`
(run_integrated_layers, profile=standard, seed=42), `three_d`
(run_3d_all). Checkpoints: written only after success, atomically;
bind driver+orchestrator source hashes, the full qta_multiphysics tree
hash, interpreter/numpy/scipy/qutip versions, and every stage-output
hash (packaging metadata excluded); ANY difference fails closed; partial
stages never complete; prerequisite ordering enforced; incompatible runs
cannot merge. `python3 qta_sim_stages.py status` reports marker validity.

**The single-command `python3 qta_full_sim.py` remains the authoritative
release path.** The staged path exists to produce the byte-identical set
when the ceiling binds — demonstrated: full staged sequence -> 89 files,
88/88 byte-identical to the canonical roots.

## Checker `--verify-existing`
`python3 package_consistency_check.py --verify-existing --sim-log LOG`
validates an EXISTING COMPLETE set: exactly 89 files required (truncated
or foreign sets refused), a captured simulation stdout log required for
the Step-3 stale-language audit (never skipped), then every downstream
check runs unchanged. Banner states it is NOT the release gate; the
default full-regeneration branch is preserved and remains the sole
release verification.

## Operational baseline policy (approved)
In ceiling-bound sandboxes: staged complete regeneration + 88/88
comparison + `--verify-existing` = the operational baseline. The
monolithic command is mandatory release verification whenever an
adequate uninterrupted window or unrestricted environment is available.

## package_consistency_check.py's 300 s subprocess budget — VERIFICATION TOOL/PERFORMANCE DEFECT (margin)

`package_consistency_check.py:155` runs `qta_full_sim.py` as a subprocess with
`timeout=300`.

**An earlier version of this section claimed the subprocess "does not finish on
this machine" and classified the result UNRESOLVED ENVIRONMENTAL VERIFICATION.
That was wrong and is withdrawn.** The checker does finish, and passes:

    RESULT: PASS (all consistency checks passed)      exit 0, elapsed 229 s

The real defect is **margin**, not capability. Measured wall times for the
canonical generator on this machine (4 x Intel Xeon @ 2.80 GHz, container):

| Sample | Wall time | Outcome |
|---|---|---|
| under heavy concurrent load | 323 s | TimeoutExpired at 300 s |
| instrumented, moderate load | 262 s | completed |
| inside the checker, quiet | 229 s | **PASS, exit 0** |

The spread straddles the 300 s budget, so the checker fails **intermittently
under contention** rather than persistently. Progress continues throughout —
there is no hang, no deadlock, and no stage that stops making progress.

**Stage breakdown** (from the 262 s instrumented run):

| Stage | Elapsed | Share |
|---|---|---|
| core gates / Monte Carlo / engineering fixes | 0–50 s | 19% |
| integrated forecast layers | 55–179 s | **47%** |
| 3D layer | 179–262 s | 32% |

**Why the budget was not raised.** Raising a timeout so a check goes green is
the change this project's rules forbid without owner authority, and the
evidence says the computation is genuinely close to the limit rather than
wrongly bounded.

**Why no speed-up was applied.** The checker invokes `qta_full_sim.py` exactly
once, so there is no duplicate subprocess to remove. Instrumenting every
`solve_thermal_3d` / `solve_thermal_2d` call in the 3D verification layer found
**no exact duplicate solves**; the closest was one CI-mesh 3D solve computed in
both `reduction_checks_3d` and `convergence_3d` (~3.7 s of a 262 s run, 1.4%).
The dominant single cost is the refined-mesh convergence solve (~28 s), which
exists to make the mesh-refinement check meaningful. Every remaining lever —
fewer samples, coarser meshes, shallower models — reduces verification depth
and is therefore not allowed. **No material margin can be created without
weakening verification, so the 300 s budget stands unchanged.**

**What would resolve it:** an owner decision to widen the budget against a
stated target machine, or running CI on hardware with headroom above ~330 s.

Every other governed checker passes in this environment: `pytest tests/`,
`manuscript_consistency_check.py`, `stage6_preservation_check.py`,
`generate_manifest.py --check`, `validate_hdf5_equivalence.py`, and
`ro_crate_tools.py validate`.
